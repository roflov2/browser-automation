"""Runs any workflow: one browser and Stagehand session, then one audited run per target URL."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import sys
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any, cast
from urllib.parse import urlparse

from browserbase import Browserbase
from pydantic import BaseModel, ValidationError
from stagehand import (
    LLMGenerateCallback,
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
from .azure import open_azure_llm
from .config import Settings
from .core import (
    BROWSER_ERRORS,
    OUTPUT_ROOT,
    AuditError,
    ExtractFailed,
    SiteResult,
    Status,
    StepTimeout,
    UnsafeActionRefused,
    classify_failure,
    is_timeout,
    update_summary,
    write_json,
    write_reports,
)
from .page import cdp_noise_filtered, is_noise, is_sso_redirect, pause_for_login, settle, shot, wait_out_sso
from .workflow import Workflow

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is a pinned dependency; this keeps a bare checkout usable
    psutil = None

log = logging.getLogger(__name__)

CHROME_PROFILE_DIR = Path(".chrome-profile")
SAMPLE_SECONDS = 1.0


def redact(value: Any, secrets: Mapping[str, str]) -> Any:
    """Replace every secret value inside a JSON-like structure with its `%name%` placeholder."""
    if isinstance(value, str):
        for name, secret in secrets.items():
            if len(secret) >= 3:  # shorter values ('Mr') would match ordinary text
                value = value.replace(secret, f"%{name}%")
        return value
    if isinstance(value, dict):
        return {key: redact(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    return value


class ResourceMonitor:
    """Samples this process's memory and CPU while one site is audited."""

    def __init__(self) -> None:
        self.cpu_usage: list[float] = []
        self.memory_usage: list[float] = []
        self._process = psutil.Process(os.getpid()) if psutil else None

    async def _sample(self) -> None:
        if self._process is None:
            return
        self._process.cpu_percent(interval=None)  # prime baseline measurement
        while True:
            await asyncio.sleep(SAMPLE_SECONDS)
            self.memory_usage.append(self._process.memory_info().rss / (1024 * 1024))
            self.cpu_usage.append(self._process.cpu_percent(interval=None))

    @contextlib.asynccontextmanager
    async def running(self) -> AsyncIterator[ResourceMonitor]:
        task = asyncio.create_task(self._sample())
        try:
            yield self
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def summary(self) -> dict[str, float] | None:
        """Averages and peak for this site; None when psutil is not installed (zeros would look measured)."""
        if self._process is None:
            return None
        if not self.memory_usage:
            return {"avg_memory_mb": 0.0, "peak_memory_mb": 0.0, "avg_cpu_percent": 0.0, "data_points": 0}
        return {
            "avg_memory_mb": round(sum(self.memory_usage) / len(self.memory_usage), 2),
            "peak_memory_mb": round(max(self.memory_usage), 2),
            "avg_cpu_percent": round(sum(self.cpu_usage) / len(self.cpu_usage), 2),
            "data_points": len(self.cpu_usage),
        }


@dataclass
class SessionLog:
    """What is recorded about the Stagehand session for the site being audited."""

    secrets: Mapping[str, str] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    extractions: Path | None = None

    def on_log(self, entry: Any) -> None:
        """Stagehand's log callback: keep everything except the CDP race noise, without secret values."""
        if is_noise(getattr(entry, "message", "") or ""):
            return
        record = entry.model_dump(mode="json")
        record["timestamp"] = datetime.now(UTC).isoformat()
        self.history.append(redact(record, self.secrets))

    def start_site(self) -> None:
        self.history.clear()
        self.extractions = None

    def extraction(self, instruction: str, schema: type[BaseModel], result: Any) -> None:
        """Append one extract() result to extractions.jsonl: the evidence of what the model actually read."""
        if self.extractions is None:
            return
        metadata = getattr(result, "metadata", None)
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "schema": schema.__name__,
            "instruction": instruction,
            "data": result.data.model_dump(mode="json"),
            "metadata": metadata.model_dump(mode="json") if metadata is not None else None,
        }
        with self.extractions.open("a", encoding="utf-8") as f:
            f.write(json.dumps(redact(entry, self.secrets), ensure_ascii=False) + "\n")


# Page furniture that never holds a price we audit: the site footer and the common cookie-consent
# dialogs. Left out of every extraction to make the prompt smaller. A selector that matches nothing
# costs nothing. Headers are kept on purpose: Uber Eats shows the delivery address there.
EXTRACT_IGNORE = (
    "body > footer",
    '[role="contentinfo"]',
    "#onetrust-consent-sdk",
    "#CybotCookiebotDialog",
    "#usercentrics-root",
)


class RecordingStagehand:
    """A Stagehand stand-in handed to the workflows.

    extract() gets an explicit timeout, leaves out the page furniture in EXTRACT_IGNORE, raises a
    specific error when it fails, and has every result appended to the run's extractions.jsonl.
    Everything else is passed straight through.
    """

    def __init__(self, stagehand: Stagehand, session_log: SessionLog | None = None, page: Page | None = None) -> None:
        self._stagehand = stagehand
        self._log = session_log or SessionLog()
        self._page = page

    def record_to(self, path: Path | None) -> None:
        """Start writing extractions to `path` (one file per run); None stops recording."""
        self._log.extractions = path

    async def extract(self, instruction: str, schema: type[BaseModel], **options: Any) -> Any:
        options.setdefault("timeout", safety.LLM_TIMEOUT_MS)
        if self._page is not None and "locator" not in options:
            options.setdefault("ignore_locators", [self._page.locator(selector) for selector in EXTRACT_IGNORE])
        for attempt in range(2):
            try:
                result = await self._stagehand.extract(instruction, schema, **options)
                break
            except ValidationError as error:
                raise ExtractFailed(
                    f"{schema.__name__} did not fit the schema: {error.error_count()} error(s)"
                ) from error
            except Exception as error:
                msg = str(error)
                if attempt == 0 and "locator must belong to the target page" in msg:
                    options.pop("ignore_locators", None)
                    continue
                if attempt == 0 and any(
                    p in msg
                    for p in (
                        "Session with given id not found",
                        "Cannot find context with specified id",
                        "Execution context was destroyed",
                    )
                ):
                    await asyncio.sleep(1.0)
                    continue
                if is_timeout(error):
                    if attempt == 0:
                        log.warning("  extract(%s) timed out; retrying once...", schema.__name__)
                        continue
                    raise StepTimeout(f"extract({schema.__name__}) timed out: {error}") from error
                raise ExtractFailed(f"extract({schema.__name__}) failed: {error}") from error
        self._log.extraction(instruction, schema, result)
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stagehand, name)


@contextlib.asynccontextmanager
async def open_browser(
    workflow: Workflow, settings: Settings, local: bool, headless: bool
) -> AsyncIterator[tuple[StagehandBrowser, Callable[[], dict[str, Any]]]]:
    """Browserbase if its credentials are set (and not `local`), otherwise local Chrome. Closed on exit.

    Yields (browser, describe), where describe() gives the Browserbase session details for session.json.
    """
    describe: Callable[[], dict[str, Any]] = dict
    if settings.use_browserbase and not local:
        client = Browserbase(api_key=settings.browserbase_api_key)
        session_id = client.sessions.create(project_id=settings.browserbase_project_id or "").id
        try:
            browser = await stagehand_browserbase.connect(
                api_key=settings.browserbase_api_key or "", session_id=session_id
            )
        except BaseException:
            with contextlib.suppress(Exception):
                client.sessions.update(
                    session_id, project_id=settings.browserbase_project_id or "", status="REQUEST_RELEASE"
                )
            raise

        def describe() -> dict[str, Any]:
            info = client.sessions.retrieve(session_id)
            return {"browserbase_session": {"id": info.id, "status": info.status, "region": info.region}}
    else:
        options: dict[str, Any] = {"headless": headless}
        if settings.chrome_path:
            options["executable_path"] = str(settings.chrome_path)
        # Every workflow shares one profile, so a sign-in (a site's, or the network's Microsoft one) is done once
        CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        options["user_data_dir"] = str(CHROME_PROFILE_DIR.resolve())
        if workflow.viewport:
            options["viewport_width"], options["viewport_height"] = workflow.viewport
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            options["chromium_sandbox"] = False  # Chrome refuses to start sandboxed as root (containers, CI)
        browser = await local_browser.launch(**options)
    try:
        yield browser, describe
    finally:
        try:
            await browser.close()
        except BROWSER_ERRORS as error:  # nothing useful can be done about a failed close
            log.debug("Browser close failed: %s", error)


@contextlib.asynccontextmanager
async def open_stagehand(
    browser: StagehandBrowser,
    workflow: Workflow,
    settings: Settings,
    session_log: SessionLog,
    llm: LLMGenerateCallback | None,
) -> AsyncIterator[Stagehand]:
    """The Stagehand client for this session, closed on exit.

    The model is Azure OpenAI when `AZURE_OPENAI_ENDPOINT` is set, else Gemini; `llm` replaces both (tests).
    """
    async with contextlib.AsyncExitStack() as stack:
        if llm is None and settings.use_azure:
            # AzureLLM returns plain dicts shaped like Stagehand's result TypedDicts; Stagehand validates them
            llm = cast(LLMGenerateCallback, await stack.enter_async_context(open_azure_llm(settings)))
            log.info(f"Model: Azure OpenAI deployment {settings.azure_openai_deployment}")
        model: dict[str, Any] = (
            {"model": llm} if llm else {"model": settings.model, "model_api_key": settings.require_api_key()}
        )
        stagehand = await Stagehand.create(
            browser=browser,
            system_prompt=workflow.system_prompt,
            logging=StagehandClientLoggingConfig(level=workflow.log_level, format="json", on_log=session_log.on_log),
            **model,
        )
        try:
            yield stagehand
        finally:
            try:
                await stagehand.close()
            except BROWSER_ERRORS as error:  # nothing useful can be done about a failed close
                log.debug("Stagehand close failed: %s", error)


async def clear_cookies(workflow: Workflow, browser: StagehandBrowser, host: str) -> None:
    """Start the site from a clean session, as far as the workflow asks for.

    Only the site's own cookies (its domain and subdomains) are cleared: the profile is shared, so the
    corporate SSO login and other sites' sign-ins stay.
    """
    if not workflow.clear_cookies:
        return
    domain = re.compile(rf"(^|\.){re.escape(host.removeprefix('www.'))}$", re.IGNORECASE)
    try:
        await browser.context.clear_cookies(domain=domain)
    except BROWSER_ERRORS as error:  # the run can go on, but a stale cart may skew it: say so
        log.warning("  Could not clear cookies for %s (%s); a previous session's cart may still be there", host, error)


async def on_sso_page(page: Page) -> bool:
    """True when the browser is (still) on a Microsoft sign-in page after any automatic SSO bounce."""
    try:
        if not is_sso_redirect(await page.url()):
            return False
        await wait_out_sso(page, 5000)
        return is_sso_redirect(await page.url())
    except BROWSER_ERRORS as error:
        log.debug("Could not read the page URL: %s", error)
        return False


async def network_login(page: Page, interactive: bool) -> bool:
    """Let the user complete a Microsoft sign-in the network demands. True once the browser has left it."""
    if not await pause_for_login(
        page,
        "The network asks for a Microsoft (Azure) sign-in before the browser can reach the site. "
        "It is kept in the shared Chrome profile for later runs.",
        interactive,
    ):
        return False
    return not await on_sso_page(page)


async def check_network_login(page: Page, url: str, interactive: bool) -> None:
    """Before the first site: open it and, if a Microsoft sign-in comes up instead, wait for the user."""
    try:
        await page.goto(url)
        await settle(page, 2000)
    except BROWSER_ERRORS as error:  # the site's own run will record what is wrong with it
        log.debug("Network check could not open %s: %s", url, error)
        return
    if not await on_sso_page(page):
        return
    if await network_login(page, interactive):
        log.info("  Network sign-in done")
    else:
        log.warning(
            "  The network wants a Microsoft sign-in and this run cannot pause for it "
            "(it needs a visible browser and an interactive terminal); sites may be recorded as blocked."
        )


async def audit_site(
    workflow: Workflow, stagehand: Any, page: Page, browser: StagehandBrowser, url: str, interactive: bool = False
) -> SiteResult:
    """One run of the workflow against one URL. Never raises: the outcome is in the result's status.

    A run that ends on a Microsoft sign-in page (the network's, not the site's) pauses for the user to
    sign in, when `interactive`, and is then run again once.
    """
    result = await audit_once(workflow, stagehand, page, browser, url)
    if result.status != Status.OK and interactive and await on_sso_page(page) and await network_login(page, True):
        log.info(f"  Network sign-in done; running {url} again")
        result = await audit_once(workflow, stagehand, page, browser, url)
    return result


async def audit_once(workflow: Workflow, stagehand: Any, page: Page, browser: StagehandBrowser, url: str) -> SiteResult:
    """One attempt at the site, with its own output folder and reports."""
    out_dir = OUTPUT_ROOT / workflow.output_folder(url) / datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    result = workflow.result_class(site=url, out_dir=out_dir)
    if isinstance(stagehand, RecordingStagehand):
        stagehand.record_to(out_dir / "extractions.jsonl")
    await clear_cookies(workflow, browser, urlparse(url).hostname or "")

    try:
        with safety.active_guard():
            await workflow.run(stagehand, page, url, result)
    except UnsafeActionRefused as error:
        # The purchase guard stopped the run. Record it and move on to the next site.
        result.status, result.failure_reason, result.message = error.status, error.reason, f"Guard refused: {error}"
        log.error(f"  UNSAFE ACTION REFUSED: {error}")
        await shot(page, out_dir / "error.png", full_page=False)
    except AuditError as error:
        result.status, result.failure_reason, result.message = error.status, error.reason, str(error)
        log.error(f"  {error.status.capitalize()} ({error.reason}): {result.message}")
        if error.status is Status.ERROR:
            await shot(page, out_dir / "error.png", full_page=False)
    except Exception as error:
        result.status, result.failure_reason = Status.ERROR, classify_failure(error)
        result.message = f"{type(error).__name__}: {error}"
        log.error(f"  ERROR: {result.message}")
        log.debug("Unexpected failure", exc_info=error)
        await shot(page, out_dir / "error.png", full_page=False)

    write_reports(result, workflow.item_fields)
    if result.comparison:
        workflow.summarise(result)
    return result


def token_total(metrics: Any) -> float:
    return float(metrics.total_prompt_tokens + metrics.total_completion_tokens)


def metrics_delta(before: Any, after: Any) -> dict[str, float]:
    """This site's share of Stagehand's cumulative counters."""
    start, end = before.model_dump(mode="json"), after.model_dump(mode="json")
    return {name: end[name] - start.get(name, 0) for name in end if isinstance(end[name], int | float)}


async def audit_and_record(
    workflow: Workflow, stagehand: Any, page: Page, browser: StagehandBrowser, url: str,
    session_log: SessionLog, describe_browser: Callable[[], dict[str, Any]], interactive: bool = False,
) -> None:  # fmt: skip
    """Audit one site and write its session.json, runs.csv row and summary."""
    start = perf_counter()
    before = await stagehand.metrics()
    async with ResourceMonitor().running() as monitor:
        result = await audit_site(workflow, stagehand, page, browser, url, interactive)
    try:
        after = await stagehand.metrics()
    except BROWSER_ERRORS as error:  # the audit itself is already saved; say that the usage figures are not
        log.warning("  Could not read usage metrics (%s); tokens_used is unknown for this run", error)
        after = None

    session = {
        "site": url,
        "workflow": workflow.name,
        "status": result.status,
        "message": result.message,
        "failure_reason": result.failure_reason,
        "execution_time_ms": round((perf_counter() - start) * 1000),
        "tokens_used": token_total(after) - token_total(before) if after else None,
        "metrics": metrics_delta(before, after) if after else None,
        "metrics_cumulative": (after or before).model_dump(mode="json"),
        "resource_usage": monitor.summary(),
        "history": list(session_log.history),
        **describe_browser(),
    }
    write_json(result.out_dir / "session.json", session)
    update_summary(result, workflow.name, workflow.target_key(url, result))
    log.info(f"  Status: {result.status} {result.message} -> {result.out_dir}")


async def fresh_tab(browser: StagehandBrowser) -> Page:
    """One new tab to work in, with every other tab closed (the shared profile can restore old ones)."""
    old = await browser.context.pages()
    page = await browser.context.new_page()
    for tab in old:
        try:
            await tab.close()
        except BROWSER_ERRORS as error:  # a tab that will not close is in the way, not fatal
            log.debug("Could not close a leftover tab: %s", error)
    await browser.context.set_active_page(page)
    return page


async def preload_login(page: Page, url: str) -> None:
    """`--login`: open the site in the persistent profile and wait for the user to sign in."""
    log.info(f"\n=== Preloading a login for {url} into {CHROME_PROFILE_DIR.resolve()} ===")
    await page.goto(url)
    await settle(page, 2000)
    if await pause_for_login(page, "Sign in to the site in the open browser window.", interactive=True):
        log.info(f"  Login session saved to {CHROME_PROFILE_DIR.resolve()}")
    else:
        log.error("  --login needs an interactive terminal and a visible browser (unset HEADLESS).")


async def run(
    workflow: Workflow, args: argparse.Namespace, settings: Settings, llm: LLMGenerateCallback | None = None
) -> None:
    """Audit every target of `workflow` in one browser session.

    `llm` is a Stagehand LLM callback used instead of Gemini or Azure; the tests drive the whole runner with it.
    """
    if llm is None:
        settings.require_model()
    targets = [url if url.startswith("http") else f"https://{url}" for url in workflow.targets()]
    session_log = SessionLog(secrets=workflow.sensitive_values())

    with cdp_noise_filtered():
        async with (
            open_browser(workflow, settings, args.local or args.login, settings.headless and not args.login) as (
                browser,
                describe,
            ),
            open_stagehand(browser, workflow, settings, session_log, llm) as client,
        ):
            page = await fresh_tab(browser)
            if args.login:
                await preload_login(page, workflow.login_url())
                return
            interactive = not settings.headless and sys.stdin.isatty()
            if targets:
                await check_network_login(page, targets[0], interactive)
            # The in-page purchase blocker, on every page from here on (not for --login, where a person signs in).
            await safety.install(browser.context, page)
            stagehand = RecordingStagehand(client, session_log, page)
            for url in targets:
                session_log.start_site()
                await audit_and_record(workflow, stagehand, page, browser, url, session_log, describe, interactive)

    log.info(f"\nSummary written to {OUTPUT_ROOT / 'summary.csv'} (history in {OUTPUT_ROOT / 'runs.csv'})")
