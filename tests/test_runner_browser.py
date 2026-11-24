"""The whole runner, end to end, in a real browser against the local fixture with a stub model.

No real site and no real LLM are involved. This is the closest thing to a live run that the test
suite can do: launch, a workflow with extract/act/JS steps over a multi-site run, and every file the
runner writes.
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
from audit.actions import llm_act
from audit.config import Settings
from audit.core import SiteResult, Status, drip_outcome, new_item
from audit.page import js, settle
from audit.workflow import Workflow

from .browser_support import StubModel, find_chrome, serve

FIXTURE = Path(__file__).parent / "fixtures" / "shop"
pytestmark = pytest.mark.browser


class Info(BaseModel):
    heading: str
    total: str | None


class FixtureWorkflow(Workflow):
    """Adds a product, goes to checkout and stops there."""

    name = "fixture"
    help = "test workflow"
    system_prompt = "Never buy anything."
    clear_cookies = "domain"
    base = ""

    def targets(self) -> list[str]:
        return [f"{self.base}/index.html", f"{self.base}/index.html?again"]

    def sensitive_values(self) -> dict[str, str]:
        return {"heading": "Guard fixture"}

    async def run(self, stagehand, page, url: str, result: SiteResult) -> None:
        await page.goto(url)
        await settle(page, 1000)
        info = (await stagehand.extract("Read the heading and the total.", Info)).data
        added = await llm_act(stagehand, "Click the 'Add to cart' button.")
        result.items.append({**new_item("Thing", info.total), "status": "added" if added.success else "add_failed"})
        await llm_act(stagehand, "Click the 'Next' link.")
        await settle(page, 1000)
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


def test_every_site_is_recorded(run_output):
    runs = run_output["runs"]
    assert [(r["status"], r["failure_reason"]) for r in runs] == [("ok", ""), ("ok", "")]
    assert [r["drip_detected"] for r in runs] == ["False", "False"]
    assert {r["workflow"] for r in runs} == {"fixture"}
    assert len(list(csv.DictReader((run_output["out"] / "summary.csv").open(encoding="utf-8")))) == 2


def test_the_run_stops_on_the_checkout_page(run_output):
    for folder in run_output["folders"]:
        ok = load(folder, "price_comparison.json")
        assert ok["clicked"] == []  # the checkout page's own log: nothing was clicked there
        assert "/checkout/" in ok["checkout_url"]
        assert [i["status"] for i in ok["items"]] == ["added"]


def test_session_json_is_per_site(run_output):
    sessions = [load(folder, "session.json") for folder in run_output["folders"]]
    for session in sessions:
        assert {"site", "workflow", "status", "message", "failure_reason", "execution_time_ms", "tokens_used", "metrics",
                "metrics_cumulative", "resource_usage", "history"} <= set(session)  # fmt: skip
        assert session["tokens_used"] > 0
        assert session["metrics"]["total_prompt_tokens"] > 0
    first, second = sessions
    # Telemetry and token counts restart for every site instead of accumulating (REVIEW M1).
    assert second["tokens_used"] < first["tokens_used"] * 1.5
    assert second["metrics_cumulative"]["total_prompt_tokens"] > second["metrics"]["total_prompt_tokens"]
    assert second["resource_usage"]["data_points"] < first["resource_usage"]["data_points"] * 3 + 20


def test_extractions_are_saved_and_redacted(run_output):
    for folder in run_output["folders"]:
        (entry,) = [
            json.loads(line) for line in (folder / "extractions.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert entry["schema"] == "Info"
        assert entry["instruction"] == "Read the heading and the total."
        assert entry["data"] == {"heading": "%heading%: shop page", "total": "$54.20"}  # sensitive_values() applied


def test_llm_calls_are_only_observes_and_extracts(run_output):
    """An llm_act costs one model call (the observe; acting on the observed Action costs none).

    Stagehand 4.4.0 makes two calls per extract(): the extraction and a progress check.
    """
    # Each site: 1 extract (2 calls) + 2 llm_act (1 call each).
    assert run_output["stub"].calls == 8


def test_streams_are_restored_after_the_session(run_output):
    assert run_output["streams_restored"] is True
