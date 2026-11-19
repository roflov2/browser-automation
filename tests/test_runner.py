"""The runner: failure mapping, per-site records, redaction and extraction errors (no browser)."""

import argparse
import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ValidationError

from audit import core, runner
from audit.config import Settings
from audit.core import (
    Blocked,
    ElementNotFound,
    ExtractFailed,
    FailureReason,
    SiteResult,
    Status,
    StepTimeout,
    UnsafeActionRefused,
)
from audit.page import cdp_noise_filtered, is_noise
from audit.runner import RecordingStagehand, ResourceMonitor, SessionLog, audit_site, redact
from audit.workflow import Workflow


class FakePage:
    def __init__(self) -> None:
        self.screenshots: list[str] = []

    async def screenshot(self, path: str, **options) -> None:
        self.screenshots.append(Path(path).name)


class FakeBrowser:
    def __init__(self, fail_clear: bool = False) -> None:
        self.cleared: list[dict] = []
        self.context = SimpleNamespace(clear_cookies=self._clear)
        self._fail = fail_clear

    async def _clear(self, **scope) -> None:
        if self._fail:
            raise RuntimeError("cdp gone")
        self.cleared.append(scope)


def workflow_that(outcome, **attrs) -> Workflow:
    class Scripted(Workflow):
        name = "scripted"
        help = "test"
        clear_cookies = attrs.get("clear_cookies")

        def targets(self) -> list[str]:
            return ["https://shop.test"]

        async def run(self, stagehand, page, url, result) -> None:
            if isinstance(outcome, BaseException):
                raise outcome
            outcome(result)

    return Scripted(argparse.Namespace(), Settings())


@pytest.fixture
def output_root(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(core, "OUTPUT_ROOT", tmp_path)
    monkeypatch.setattr(runner, "OUTPUT_ROOT", tmp_path)
    return tmp_path


def audit(workflow: Workflow, page=None, browser=None) -> tuple[SiteResult, FakePage]:
    page = page or FakePage()
    result = asyncio.run(audit_site(workflow, object(), page, browser or FakeBrowser(), "https://www.shop.test/x"))
    return result, page


@pytest.mark.parametrize(
    ("error", "status", "reason", "screenshot"),
    [
        (Blocked("Verify you are human"), "blocked", "blocked", False),
        (ElementNotFound("Couldn't open the shopping cart"), "error", "element_not_found", True),
        (ExtractFailed("CheckoutDetails did not fit the schema"), "error", "extract_failed", True),
        (StepTimeout("extract(Products) timed out"), "error", "timeout", True),
        (RuntimeError("page.goto: Navigation timeout of 30000 ms exceeded"), "error", "timeout", True),
        (
            UnsafeActionRefused("purchase, payment or membership control: Place order"),
            "error",
            "unsafe_action_refused",
            True,
        ),
        (KeyError("surprise"), "error", None, True),
    ],
)
def test_failures_get_a_specific_reason(output_root, error, status, reason, screenshot):
    result, page = audit(workflow_that(error))
    assert (result.status, result.failure_reason) == (status, reason)
    assert str(error.args[0]) in result.message
    assert (page.screenshots == ["error.png"]) is screenshot
    report = json.loads((result.out_dir / "price_comparison.json").read_text(encoding="utf-8"))
    assert (report["status"], report["failure_reason"]) == (status, reason)  # written however the run ended


def test_early_exits_carry_a_reason_too(output_root):
    result, _ = audit(workflow_that(lambda r: r.fail(Status.NO_COFFEE_FOUND, "No in-stock coffee products")))
    assert (result.status, result.failure_reason) == ("no_coffee_found", FailureReason.OUT_OF_STOCK)
    ok, _ = audit(workflow_that(lambda r: setattr(r, "status", Status.OK)))
    assert (ok.status, ok.failure_reason) == ("ok", None)


def test_run_folder_and_cookie_scope(output_root):
    browser = FakeBrowser()
    result, _ = audit(workflow_that(lambda r: None, clear_cookies="domain"), browser=browser)
    assert result.out_dir.parent == output_root / "shop.test"
    [scope] = browser.cleared
    domain = scope["domain"]  # the site and its subdomains, never the shared profile's other logins
    assert all(domain.search(d) for d in ("shop.test", ".shop.test", "checkout.shop.test", "WWW.Shop.Test"))
    assert not any(domain.search(d) for d in ("login.microsoftonline.com", "notshop.test", "shop.test.evil"))
    untouched = FakeBrowser()
    audit(workflow_that(lambda r: None), browser=untouched)
    assert untouched.cleared == []


class SsoPage(FakePage):
    """A page that sits on the Microsoft sign-in page until `sign_in()` is called."""

    def __init__(self) -> None:
        super().__init__()
        self.location = "https://login.microsoftonline.com/common/oauth2/authorize"

    def sign_in(self) -> None:
        self.location = "https://www.shop.test/x"

    async def url(self) -> str:
        return self.location


def test_a_network_sign_in_pauses_then_runs_the_site_again(output_root, monkeypatch):
    page, runs, pauses = SsoPage(), [], []

    async def no_wait(*args) -> None:
        return None

    async def user_signs_in(pg, message, interactive) -> bool:
        pauses.append(interactive)
        pg.sign_in()
        return True

    def outcome(result) -> None:
        runs.append(page.location)
        if "microsoftonline" in page.location:
            raise Blocked("A Microsoft Sign-in page is displayed")
        result.status = Status.OK

    monkeypatch.setattr(runner, "wait_out_sso", no_wait)
    monkeypatch.setattr(runner, "pause_for_login", user_signs_in)
    workflow = workflow_that(outcome)
    result = asyncio.run(audit_site(workflow, object(), page, FakeBrowser(), "https://www.shop.test/x", True))
    assert result.status == "ok" and pauses == [True] and len(runs) == 2

    headless = SsoPage()
    page = headless
    result = asyncio.run(audit_site(workflow, object(), headless, FakeBrowser(), "https://www.shop.test/x", False))
    assert result.status == "blocked" and pauses == [True]  # no pause without a person to sign in


def test_a_run_starts_in_one_fresh_tab():
    class Tab:
        def __init__(self, tabs: list) -> None:
            self.tabs = tabs
            tabs.append(self)

        async def close(self) -> None:
            self.tabs.remove(self)

    tabs: list = []
    restored = [Tab(tabs), Tab(tabs)]
    active = []

    async def pages() -> list:
        return list(tabs)

    async def new_page() -> Tab:
        return Tab(tabs)

    async def set_active_page(page) -> None:
        active.append(page)

    browser = SimpleNamespace(context=SimpleNamespace(pages=pages, new_page=new_page, set_active_page=set_active_page))
    page = asyncio.run(runner.fresh_tab(browser))
    assert tabs == [page] == active and page not in restored


def test_failed_cookie_clearing_is_reported_not_hidden(output_root, caplog):
    with caplog.at_level(logging.WARNING, logger="audit"):
        audit(workflow_that(lambda r: None, clear_cookies="domain"), browser=FakeBrowser(fail_clear=True))
    assert "Could not clear cookies" in caplog.text


def test_a_refusal_ends_only_that_site(output_root):
    """After a guard refusal the runner carries on with the next target."""
    refused, _ = audit(workflow_that(UnsafeActionRefused("Place order")))
    following, _ = audit(workflow_that(lambda r: setattr(r, "status", Status.OK)))
    assert (refused.failure_reason, following.status) == ("unsafe_action_refused", "ok")


# --- telemetry -------------------------------------------------------------------------------------
def test_resource_monitor_is_scoped_to_one_site(monkeypatch):
    monkeypatch.setattr(runner, "SAMPLE_SECONDS", 0.01)

    async def one_site() -> dict:
        async with ResourceMonitor().running() as monitor:
            await asyncio.sleep(0.08)
        return monitor.summary()

    async def two_sites():
        return await one_site(), await one_site()

    first, second = asyncio.run(two_sites())
    assert 1 <= first["data_points"] <= 12
    assert second["data_points"] <= 12  # starts from zero again, not from the first site's samples
    assert first["peak_memory_mb"] > 0


def test_metrics_delta_is_this_sites_share():
    def metrics(**values):
        return SimpleNamespace(model_dump=lambda mode="json": values, **values)

    before = metrics(total_prompt_tokens=100, total_completion_tokens=10, extract_prompt_tokens=60)
    after = metrics(total_prompt_tokens=350, total_completion_tokens=40, extract_prompt_tokens=200)
    assert runner.metrics_delta(before, after) == {
        "total_prompt_tokens": 250,
        "total_completion_tokens": 30,
        "extract_prompt_tokens": 140,
    }
    assert runner.token_total(after) - runner.token_total(before) == 280


# --- what is written about the session ---------------------------------------------------------------
SECRETS = {"first_name": "Samantha", "email": "sam@real.example", "title": "Mr"}


def test_redact_replaces_values_everywhere():
    entry = {"message": "Typing Samantha", "data": {"args": ["sam@real.example", 3], "note": "Mr Smith"}}
    assert redact(entry, SECRETS) == {
        "message": "Typing %first_name%",
        "data": {"args": ["%email%", 3], "note": "Mr Smith"},
    }


def test_history_is_redacted_and_noise_free():
    session = SessionLog(secrets=SECRETS)
    entry = SimpleNamespace(
        message="act", model_dump=lambda mode="json": {"message": "act", "data": {"instruction": "Type Samantha"}}
    )
    noise = SimpleNamespace(message="CDP response failed: Frame with the given id was not found", model_dump=dict)
    session.on_log(entry)
    session.on_log(noise)
    assert len(session.history) == 1
    assert session.history[0]["data"] == {"instruction": "Type %first_name%"}
    assert "timestamp" in session.history[0]
    session.start_site()
    assert session.history == []


class Total(BaseModel):
    total: str | None


def extraction_with(session: SessionLog, behaviour) -> RecordingStagehand:
    class Fake:
        async def extract(self, instruction, schema, **options):
            return behaviour(schema, options)

    return RecordingStagehand(Fake(), session)


def test_extractions_are_redacted_and_get_a_timeout(tmp_path):
    seen = {}

    def answer(schema, options):
        seen.update(options)
        return SimpleNamespace(data=schema(total="Samantha owes $5"), metadata=None)

    stagehand = extraction_with(SessionLog(secrets=SECRETS), answer)
    stagehand.record_to(tmp_path / "extractions.jsonl")
    result = asyncio.run(stagehand.extract("Read the total", Total))
    assert result.data.total == "Samantha owes $5"  # the workflow still gets the real value
    assert seen == {"timeout": 90_000}
    (entry,) = [json.loads(line) for line in (tmp_path / "extractions.jsonl").read_text(encoding="utf-8").splitlines()]
    assert entry["data"] == {"total": "%first_name% owes $5"}


def test_extraction_failures_are_specific(tmp_path):
    def bad_schema(schema, options):
        try:
            schema.model_validate({"total": ["not", "a", "string"]})
        except ValidationError as error:
            raise error from None

    with pytest.raises(ExtractFailed, match="Total did not fit the schema"):
        asyncio.run(extraction_with(SessionLog(), bad_schema).extract("x", Total))

    def timeout(schema, options):
        raise RuntimeError("extract() timed out after 90000ms")

    with pytest.raises(StepTimeout):
        asyncio.run(extraction_with(SessionLog(), timeout).extract("x", Total))

    def broken(schema, options):
        raise RuntimeError("model returned no content")

    with pytest.raises(ExtractFailed, match="no content"):
        asyncio.run(extraction_with(SessionLog(), broken).extract("x", Total))


# --- stdout/stderr filter ------------------------------------------------------------------------------
def test_noise_filter_is_scoped_and_line_based(capsys):
    import sys

    before = (sys.stdout, sys.stderr)
    root_filters = list(logging.getLogger().filters)
    with cdp_noise_filtered():
        assert sys.stdout is not before[0]
        print('{"error": "CDP response failed"}\nTraceback: something real happened')  # noqa: T201
        print("  Step 1: review")  # noqa: T201
    assert (sys.stdout, sys.stderr) == before  # restored
    assert logging.getLogger().filters == root_filters
    out = capsys.readouterr().out
    assert "CDP response failed" not in out
    assert "something real happened" in out and "Step 1: review" in out
    assert is_noise("Cannot find context with specified id") and not is_noise("Place order")
