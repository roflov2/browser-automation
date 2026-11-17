"""Runs any workflow: one browser and Stagehand session, then one audited run per target URL."""

import argparse
import asyncio
import contextlib
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel

try:
    import psutil
except ImportError:
    psutil = None

from browserbase import Browserbase
from stagehand import (
    Page,
    Stagehand,
    StagehandBrowser,
    StagehandClientLoggingConfig,
    local_browser,
)
from stagehand import (
    browserbase as stagehand_browserbase,
)

from . import safety
from .config import Settings
from .core import OUTPUT_ROOT, AuditError, SiteResult, UnsafeActionRefused, update_summary, write_reports
from .page import install_noise_filter, is_noise, pause_for_login, settle
from .workflow import Workflow

CHROME_PROFILE_DIR = Path(".chrome-profile")


class ResourceMonitor:
    """Samples this process's memory and CPU once a second (needs psutil; empty without it)."""

    def __init__(self) -> None:
        self.cpu_usage: list[float] = []
        self.memory_usage: list[float] = []
        self._process = psutil.Process(os.getpid()) if psutil else None

    def start(self) -> asyncio.Task[None]:
        async def sample() -> None:
            if self._process is None:
                return
            self._process.cpu_percent(interval=None)  # prime baseline measurement
            while True:
                await asyncio.sleep(1)
                self.memory_usage.append(self._process.memory_info().rss / (1024 * 1024))
                self.cpu_usage.append(self._process.cpu_percent(interval=None))

        return asyncio.create_task(sample())

    def summary(self) -> dict[str, float]:
        if not self.memory_usage:
            return {"avg_memory_mb": 0.0, "peak_memory_mb": 0.0, "avg_cpu_percent": 0.0, "data_points": 0}
        return {
            "avg_memory_mb": round(sum(self.memory_usage) / len(self.memory_usage), 2),
            "peak_memory_mb": round(max(self.memory_usage), 2),
            "avg_cpu_percent": round(sum(self.cpu_usage) / len(self.cpu_usage), 2),
            "data_points": len(self.cpu_usage),
        }


class RecordingStagehand:
    """A Stagehand stand-in that appends every extract() result to the run's extractions.jsonl.

    The file is the evidence of what the model actually read, and doubles as a test fixture.
    Everything else is passed straight through to the real client.
    """

    def __init__(self, stagehand: Stagehand) -> None:
        self._stagehand = stagehand
        self._path: Path | None = None

    def record_to(self, path: Path | None) -> None:
        """Start writing to `path` (one file per run); None stops recording."""
        self._path = path

    async def extract(self, instruction: str, schema: type[BaseModel], **options: Any) -> Any:
        result = await self._stagehand.extract(instruction, schema, **options)
        if self._path is not None:
            metadata = getattr(result, "metadata", None)
            entry = {
                "timestamp": datetime.now(UTC).isoformat(),
                "schema": schema.__name__,
                "instruction": instruction,
                "data": result.data.model_dump(mode="json"),
                "metadata": metadata.model_dump(mode="json") if metadata is not None else None,
            }
            with self._path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stagehand, name)


async def launch_browser(
    workflow: Workflow, settings: Settings, local: bool, headless: bool
) -> tuple[StagehandBrowser, Callable[[], dict[str, Any]]]:
    """Browserbase if its credentials are set (and not `local`), otherwise local Chrome.

    Returns (browser, describe), where describe() gives the Browserbase session details for session.json.
    """
    if settings.use_browserbase and not local:
        client = Browserbase(api_key=settings.browserbase_api_key)
        session_id = client.sessions.create(project_id=settings.browserbase_project_id or "").id
        browser = await stagehand_browserbase.connect(api_key=settings.browserbase_api_key, session_id=session_id)

        def describe() -> dict[str, Any]:
            info = client.sessions.retrieve(session_id)
            return {"browserbase_session": {"id": info.id, "status": info.status, "region": info.region}}

        return browser, describe

    options: dict[str, Any] = {"headless": headless}
    if settings.chrome_path:
        options["executable_path"] = str(settings.chrome_path)
    if workflow.persistent_profile:
        CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        options["user_data_dir"] = str(CHROME_PROFILE_DIR.resolve())
    if workflow.viewport:
        options["viewport_width"], options["viewport_height"] = workflow.viewport
    return await local_browser.launch(**options), dict


async def audit_site(workflow: Workflow, stagehand: Stagehand, page: Page, browser, url: str) -> SiteResult:
    """One run of the workflow against one URL. Never raises: the outcome is in the result's status."""
    domain = urlparse(url).netloc.removeprefix("www.")
    out_dir = OUTPUT_ROOT / workflow.output_folder(url) / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    result = workflow.result_class(site=url, out_dir=out_dir)
    if isinstance(stagehand, RecordingStagehand):
        stagehand.record_to(out_dir / "extractions.jsonl")

    if workflow.clear_cookies:
        with contextlib.suppress(Exception):
            # "domain" clears only the storefront's cookies, so corporate SSO/proxy tokens persist
            await browser.context.clear_cookies(**({"domain": domain} if workflow.clear_cookies == "domain" else {}))

    try:
        with safety.active_guard():
            await workflow.run(stagehand, page, url, result)
    except UnsafeActionRefused as error:
        # The purchase guard stopped the run. Record it and move on to the next site.
        result.status, result.failure_reason, result.message = error.status, error.reason, f"Guard refused: {error}"
        print(f"  UNSAFE ACTION REFUSED: {error}")
        with contextlib.suppress(Exception):
            await page.screenshot(path=str(out_dir / "error.png"))
    except AuditError as error:
        result.status, result.failure_reason, result.message = error.status, error.reason, str(error)
        print(f"  {error.status.capitalize()}: {result.message}")
    except Exception as error:
        result.status, result.message = "error", f"{type(error).__name__}: {error}"
        print(f"  ERROR: {result.message}")
        with contextlib.suppress(Exception):
            await page.screenshot(path=str(out_dir / "error.png"))

    write_reports(result, workflow.item_fields)
    if result.comparison:
        workflow.summarise(result)
    return result


async def run(workflow: Workflow, args: argparse.Namespace, settings: Settings) -> None:
    """Audit every target of `workflow` in one browser session."""
    api_key, model = settings.require_api_key(), settings.model
    targets = [url if url.startswith("http") else f"https://{url}" for url in workflow.targets()]

    install_noise_filter()
    history: list[dict] = []

    def record(log) -> None:
        if is_noise(getattr(log, "message", "") or ""):
            return
        entry = log.model_dump(mode="json")
        entry["timestamp"] = datetime.now(UTC).isoformat()
        history.append(entry)

    browser, describe_browser = await launch_browser(
        workflow, settings, local=args.local or args.login, headless=settings.headless and not args.login
    )
    monitor = ResourceMonitor()
    monitor_task = monitor.start()
    try:
        stagehand = RecordingStagehand(
            await Stagehand.create(
                browser=browser,
                model=model,
                model_api_key=api_key,
                system_prompt=workflow.system_prompt,
                logging=StagehandClientLoggingConfig(level=workflow.log_level, format="json", on_log=record),
            )
        )
        try:
            page = (await browser.context.pages())[0]

            if args.login:
                print(f"\n=== Preloading a login for {targets[0]} into {CHROME_PROFILE_DIR.resolve()} ===")
                await page.goto(targets[0])
                await settle(page, 2000)
                if await pause_for_login(page, "Sign in to the site in the open browser window.", interactive=True):
                    print(f"  Login session saved to {CHROME_PROFILE_DIR.resolve()}")
                else:
                    print("  --login needs an interactive terminal and a visible browser (unset HEADLESS).")
                return

            # The in-page purchase blocker, on every page from here on (not for --login, where a person signs in).
            await safety.install(browser.context, page)
            for url in targets:
                history.clear()
                start = perf_counter()
                before = await stagehand.metrics()
                result = await audit_site(workflow, stagehand, page, browser, url)
                after = before
                with contextlib.suppress(Exception):
                    after = await stagehand.metrics()

                session = {
                    "site": url,
                    "workflow": workflow.name,
                    "status": result.status,
                    "message": result.message,
                    "failure_reason": result.failure_reason,
                    "execution_time_ms": round((perf_counter() - start) * 1000),
                    "tokens_used": (after.total_prompt_tokens + after.total_completion_tokens)
                    - (before.total_prompt_tokens + before.total_completion_tokens),
                    "metrics_cumulative": after.model_dump(mode="json"),
                    "resource_usage": monitor.summary(),
                    "history": list(history),
                    **describe_browser(),
                }
                (result.out_dir / "session.json").write_text(json.dumps(session, indent=2), encoding="utf-8")
                update_summary(result)
                print(f"  Status: {result.status} {result.message} -> {result.out_dir}")
        finally:
            with contextlib.suppress(Exception):
                await stagehand.close()
    finally:
        monitor_task.cancel()
        with contextlib.suppress(Exception):
            await browser.close()

    print(f"\nSummary written to {OUTPUT_ROOT / 'summary.csv'}")
