"""The whole runner, end to end, in a real browser against the local fixture with a stub model.

No real site and no real LLM are involved. This is the closest thing to a live run that the test
suite can do: launch, guard installation, a workflow with extract/act/JS steps, a guard refusal in
the middle of a multi-site run, and every file the runner writes.
"""

import argparse
import asyncio
import csv
import json
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

from audit import core, runner
from audit.config import Settings
from audit.core import SiteResult, Status, drip_outcome, new_item
from audit.page import js, settle
from audit.safety import current_guard, safe_act
from audit.workflow import Workflow

from .browser_support import StubModel, find_chrome, serve

FIXTURE = Path(__file__).parent / "fixtures" / "guard"
pytestmark = pytest.mark.browser


class Info(BaseModel):
    heading: str
    total: str | None


class FixtureWorkflow(Workflow):
    """Adds a product, goes to checkout and stops there. `?refuse` makes it try to place the order."""

    name = "fixture"
    help = "test workflow"
    system_prompt = "Never buy anything."
    clear_cookies = "domain"
    base = ""

    def targets(self) -> list[str]:
        return [f"{self.base}/index.html", f"{self.base}/index.html?refuse", f"{self.base}/index.html?again"]

    def sensitive_values(self) -> dict[str, str]:
        return {"heading": "Guard fixture"}

    async def run(self, stagehand, page, url: str, result: SiteResult) -> None:
        await page.goto(url)
        await settle(page, 1000)
        info = (await stagehand.extract("Read the heading and the total.", Info)).data
        added = await safe_act(stagehand, page, "Click the 'Add to cart' button.")
        result.items.append({**new_item("Thing", info.total), "status": "added" if added.success else "add_failed"})
        if "refuse" in url:
            await safe_act(stagehand, page, "Click the 'Place order' button.")  # the guard must stop this
        await safe_act(stagehand, page, "Click the 'Next' link.")
        await settle(page, 1000)
        await current_guard().freeze(page)
        result.checkout_url = await page.url()
        result.comparison = {"items_added": 1, "clicked": await js(page, "() => window.log"), **drip_outcome([], True)}
        result.status = Status.OK


@pytest.fixture(scope="module")
def run_output(tmp_path_factory) -> dict:
    chrome = find_chrome()
    if chrome is None:
        pytest.skip("no Chrome/Chromium found (set CHROME_PATH to run the browser tests)")
    out = tmp_path_factory.mktemp("output")
    patch = pytest.MonkeyPatch()
    patch.setattr(core, "OUTPUT_ROOT", out)
    patch.setattr(runner, "OUTPUT_ROOT", out)
    patch.setattr(runner, "SAMPLE_SECONDS", 0.05)
    patch.chdir(out)
    streams = (sys.stdout, sys.stderr)
    stub = StubModel({"Info": {"heading": "Guard fixture: shop page", "total": "$54.20"}})
    settings = Settings(headless=True, chrome_path=Path(chrome))
    try:
        with serve(FIXTURE) as port:
            FixtureWorkflow.base = f"http://127.0.0.1:{port}"
            workflow = FixtureWorkflow(argparse.Namespace(), settings)
            asyncio.run(runner.run(workflow, argparse.Namespace(local=True, login=False), settings, llm=stub))
    finally:
        patch.undo()
    folders = sorted((out / f"127.0.0.1:{port}").iterdir())
    with (out / "runs.csv").open(newline="", encoding="utf-8") as f:
        runs = list(csv.DictReader(f))
    return {
        "out": out,
        "folders": folders,
        "runs": runs,
        "stub": stub,
        "streams_restored": (sys.stdout, sys.stderr) == streams,
    }


def load(folder: Path, name: str) -> dict:
    return json.loads((folder / name).read_text(encoding="utf-8"))


def test_every_site_is_recorded_and_a_refusal_ends_only_its_own_site(run_output):
    runs = run_output["runs"]
    assert [(r["status"], r["failure_reason"]) for r in runs] == [
        ("ok", ""),
        ("error", "unsafe_action_refused"),
        ("ok", ""),
    ]
    assert "Place order" in runs[1]["message"]
    assert [r["drip_detected"] for r in runs] == ["False", "", "False"]
    assert {r["workflow"] for r in runs} == {"fixture"}
    assert len(list(csv.DictReader((run_output["out"] / "summary.csv").open(encoding="utf-8")))) == 3


def test_the_order_button_was_never_clicked(run_output):
    ok = load(run_output["folders"][0], "price_comparison.json")
    assert ok["clicked"] == []  # the checkout page's own log: nothing was clicked there after the freeze
    assert "/checkout/" in ok["checkout_url"]
    refused = load(run_output["folders"][1], "price_comparison.json")
    assert refused["failure_reason"] == "unsafe_action_refused"
    assert refused["checkout_url"] is None  # the run stopped on the spot
    assert [i["status"] for i in refused["items"]] == ["added"]  # what was captured before is kept
    assert (run_output["folders"][1] / "error.png").stat().st_size > 1000


def test_session_json_is_per_site(run_output):
    sessions = [load(folder, "session.json") for folder in run_output["folders"]]
    for session in sessions:
        assert {"site", "workflow", "status", "message", "failure_reason", "execution_time_ms", "tokens_used", "metrics",
                "metrics_cumulative", "resource_usage", "history"} <= set(session)  # fmt: skip
        assert session["tokens_used"] > 0
        assert session["metrics"]["total_prompt_tokens"] > 0
    first, _, third = sessions
    # Telemetry and token counts restart for every site instead of accumulating (REVIEW M1).
    assert third["tokens_used"] < first["tokens_used"] * 1.5
    assert third["metrics_cumulative"]["total_prompt_tokens"] > third["metrics"]["total_prompt_tokens"]
    assert third["resource_usage"]["data_points"] < first["resource_usage"]["data_points"] * 3 + 20


def test_extractions_are_saved_and_redacted(run_output):
    for folder in run_output["folders"]:
        (entry,) = [
            json.loads(line) for line in (folder / "extractions.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert entry["schema"] == "Info"
        assert entry["instruction"] == "Read the heading and the total."
        assert entry["data"] == {"heading": "%heading%: shop page", "total": "$54.20"}  # sensitive_values() applied


def test_llm_calls_are_only_observes_and_extracts(run_output):
    """A safe_act costs one model call (the observe; acting on the vetted Action costs none).

    Stagehand 4.4.0 makes two calls per extract(): the extraction and a progress check.
    """
    # Each site: 1 extract (2 calls) + 2 safe_act (1 call each). On the refused site the third safe_act never runs.
    assert run_output["stub"].calls == 12


def test_streams_are_restored_after_the_session(run_output):
    assert run_output["streams_restored"] is True
