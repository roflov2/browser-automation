"""Browser page helpers shared by every workflow. All DOM work goes through `js()` (page.evaluate only)."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any, cast

from stagehand import Page, Stagehand

from . import safety
from .core import BROWSER_ERRORS, PageScriptError

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Harmless transient CDP frame/context race errors that Stagehand logs loudly
# ---------------------------------------------------------------------------
NOISY_CDP_PHRASES = (
    "CDP response failed",
    "Frame with the given id was not found",
    "Cannot find context with specified id",
    "Session with given id not found",
)
NOISY_LOGGERS = ("", "stagehand", "stagehand.cdp_client", "websockets")
# evaluate() failing for one of these reasons means the page went away under us (a navigation), not a bug.
TRANSIENT_EVALUATE_ERRORS = (
    *NOISY_CDP_PHRASES,
    "Execution context was destroyed",
    "Inspected target navigated or closed",
    "Target closed",
    "No frame for given id",
)


def is_noise(text: str) -> bool:
    return any(phrase in text for phrase in NOISY_CDP_PHRASES)


class _NoiseFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not is_noise(record.getMessage())


class _FilteredStream:
    """Drops the raw JSON CDP race errors Stagehand writes straight to stdout/stderr, line by line."""

    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream

    def write(self, text: str) -> int:
        kept = "".join(line for line in text.splitlines(keepends=True) if not is_noise(line))
        if kept:
            self._stream.write(kept)
        return len(text)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


@contextmanager
def cdp_noise_filtered() -> Iterator[None]:
    """Hide the CDP race noise for the duration of a browser session, then put the streams and loggers back."""
    noise_filter = _NoiseFilter()
    stdout, stderr = sys.stdout, sys.stderr
    for name in NOISY_LOGGERS:
        logging.getLogger(name).addFilter(noise_filter)
    sys.stdout, sys.stderr = cast(IO[str], _FilteredStream(stdout)), cast(IO[str], _FilteredStream(stderr))
    try:
        yield
    finally:
        sys.stdout, sys.stderr = stdout, stderr
        for name in NOISY_LOGGERS:
            logging.getLogger(name).removeFilter(noise_filter)


# ---------------------------------------------------------------------------
# JavaScript
# ---------------------------------------------------------------------------
JS_LIB = r"""
const vis = (el, w = 8, h = 8) => { const r = el.getBoundingClientRect(); return r.width > w && r.height > h; };
const txt = el => (el.innerText || '').trim();
const clean = s => (s || '').replace(/\s+/g, ' ').trim();
const dialogs = () => Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"]'));
const pressEsc = () => {
    for (const t of [document, document.body]) {
        t.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', code: 'Escape', keyCode: 27, bubbles: true }));
    }
};
const closeDialog = () => {
    for (const d of dialogs()) {
        const b = d.querySelector('button[aria-label*="close" i]');
        if (b && guardAllows(b)) return safeClick(b);
    }
    return false;
};
"""


async def js(page: Page, fn: str, arg: Any = None, default: Any = None, lib: str = "") -> Any:
    """Run the JS arrow function `fn(arg)` in the page with JS_LIB (+ `lib`) in scope.

    Returns `default` when the function returns null/undefined, or when the page went away while it
    ran (a navigation). Raises PageScriptError when the snippet itself threw or did not parse, so a
    broken snippet is never mistaken for "element not found".

    Snippets may click and type only through safeClick/safeSelect/safeFill (see audit.safety). If the
    guard refused anything, in the snippet or in the page since the last call, UnsafeActionRefused is raised.
    """
    script = safety.wrap_snippet(safety.current_guard(), JS_LIB + lib, f"({fn})({json.dumps(arg)})")
    try:
        out = await page.evaluate(script)
    except Exception as error:
        if any(phrase in str(error) for phrase in TRANSIENT_EVALUATE_ERRORS):
            log.debug("Page went away during a script (%s); using the default", error)
            return default
        raise PageScriptError(f"{error} (in: {fn[:80]!r})") from error
    if not isinstance(out, dict):
        raise PageScriptError(f"unexpected evaluate result {out!r} (in: {fn[:80]!r})")
    safety.raise_if_refused(cast(list[dict[str, Any]], out["refused"]))
    if out["error"]:
        raise PageScriptError(f"{out['error']} (in: {fn[:80]!r})")
    return default if out["value"] is None else out["value"]


# ---------------------------------------------------------------------------
# Waiting & screenshots
# ---------------------------------------------------------------------------
LOAD_TIMEOUT_MS = 15_000
POLL_MS = 250
# True once the page has looked the same for three samples in a row (about half a second of quiet).
JS_QUIET = r"""() => {
    const body = document.body;
    const now = [document.readyState, location.href, document.getElementsByTagName('*').length,
                 body ? body.innerText.length : 0].join('|');
    const seen = window.__auditQuiet || { sample: null, repeats: 0 };
    window.__auditQuiet = { sample: now, repeats: seen.sample === now ? seen.repeats + 1 : 0 };
    return document.readyState === 'complete' && window.__auditQuiet.repeats >= 2;
}"""
JS_RENDERED = r"""() => document.readyState === 'complete' && !!document.body
    && document.body.innerText.trim().length > 200
    && !Array.from(document.querySelectorAll('[class*="spinner" i], [class*="loader" i], [aria-busy="true"]'))
        .some(el => el.offsetParent !== null)"""


async def wait_until(page: Page, condition: str, timeout_ms: int) -> bool:
    """Poll the JS arrow function `condition` until it is truthy; False if `timeout_ms` passes first."""
    try:
        await page.wait_for_function(condition, timeout=timeout_ms, polling=POLL_MS)
    except BROWSER_ERRORS as error:  # a wait that runs out, or a page that navigates meanwhile, is "not yet"
        log.debug("Stopped waiting after up to %d ms: %s", timeout_ms, error)
        return False
    return True


async def settle(page: Page, ms: int = 1500) -> None:
    """Wait for the load event, then until the page stops changing, for at most `ms` more.

    `ms` used to be slept in full after every navigation and click; it is now only the upper bound.
    """
    try:
        await page.wait_for_load_state("load", timeout=LOAD_TIMEOUT_MS)
    except BROWSER_ERRORS as error:  # a slow or failed load is judged by what the page then shows
        log.debug("Load event not seen: %s", error)
    await js(page, "() => { window.__auditQuiet = null; }")  # start counting quiet samples afresh
    await wait_until(page, JS_QUIET, ms)


async def wait_for_render(page: Page, seconds: int = 20) -> bool:
    """Wait for a client-side app to hydrate and its loading spinner to go away. False if it never does."""
    return await wait_until(page, JS_RENDERED, seconds * 1000)


async def fetch_json(page: Page, path: str, timeout_ms: int = 5000) -> Any:
    """GET a same-origin JSON endpoint from inside the page (with its cookies); None if it is not there.

    No model call and no click: used to read prices straight from a shop's own API where it has one.
    """
    script = (
        "(async () => { const ctrl = new AbortController(); setTimeout(() => ctrl.abort(), "
        f"{timeout_ms}); try {{ const r = await fetch({json.dumps(path)}, {{ credentials: 'same-origin', "
        "headers: { Accept: 'application/json' }, signal: ctrl.signal }); "
        "if (!r.ok) return null; const body = await r.text(); try { return JSON.parse(body); } catch (e) { return null; } } "
        "catch (e) { return null; } })()"
    )
    try:
        return await page.evaluate(script)
    except BROWSER_ERRORS as error:  # the page navigated away while fetching
        log.debug("fetch %s failed: %s", path, error)
        return None


# What a person would see change after an action: the URL, the main heading, the open dialogs and the
# amount of text. Used to tell whether an action did anything before paying for another extraction.
JS_FINGERPRINT = r"""() => {
    const heading = document.querySelector('h1, [role="heading"][aria-level="1"], h2');
    const dialogs = Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"]')).filter(d => vis(d, 50, 50));
    const text = document.body ? document.body.innerText : '';
    let hash = 0;
    for (let i = 0; i < text.length; i++) hash = (hash * 31 + text.charCodeAt(i)) | 0;
    return [location.href, heading ? clean(heading.innerText) : '', dialogs.length, text.length, hash].join('|');
}"""


async def fingerprint(page: Page) -> str | None:
    """A cheap summary of what is on screen; equal fingerprints mean nothing visible changed."""
    value = await js(page, JS_FINGERPRINT)
    return value if isinstance(value, str) else None


async def try_goto(page: Page, url: str) -> bool:
    """Navigate where a failure is survivable (slow sites can time out yet still render). False if it failed."""
    try:
        await page.goto(url)
    except BROWSER_ERRORS as error:  # the caller looks at the page it ended up on
        log.debug("Navigation to %s failed: %s", url, error)
        return False
    return True


async def shot(page: Page, path: Path, full_page: bool = True) -> None:
    """Screenshot to `path`; falls back to the viewport if a full-page capture fails. Never raises."""
    for whole_page in dict.fromkeys((full_page, False)):
        try:
            await page.screenshot(path=str(path), full_page=whole_page)
        except BROWSER_ERRORS as error:  # evidence is best-effort; a failed capture must not end a run
            log.debug("Screenshot %s failed (full_page=%s): %s", path.name, whole_page, error)
        else:
            return


# ---------------------------------------------------------------------------
# Popups & logins
# ---------------------------------------------------------------------------
OVERLAY_SELECTORS = (
    '[role="dialog"], [aria-modal="true"], [class*="popup" i], [class*="modal" i], [class*="newsletter" i], '
    '[id*="klaviyo" i], [class*="klaviyo" i], [class*="cookie" i], [id*="cookie" i], [class*="privy" i], '
    '[class*="age-gate" i]'
)

JS_HAS_OVERLAY = r"""selectors => {
    for (const el of document.querySelectorAll(selectors)) {
        const s = getComputedStyle(el);
        if (s.display !== 'none' && s.visibility !== 'hidden' && parseFloat(s.opacity || '1') > 0.1 && vis(el, 120, 50)) return true;
    }
    return false;
}"""


async def dismiss_popups(stagehand: Stagehand, page: Page, targets: str, caution: str = "") -> None:
    """Ask the LLM to close whichever of `targets` (e.g. 'cookie banner or newsletter popup') is visible.

    A control the guard refuses (the model picked a popup's 'Subscribe' instead of its close button) is
    skipped, not clicked; if that leaves the popup open, Escape is pressed so the journey can carry on.
    """
    try:
        found = await stagehand.observe(
            "Find the close, accept, dismiss, 'No thanks', 'Maybe later' or 'Skip' button of any visible "
            f"{targets}. Prefer an 'X' or 'Close' icon. Never return a Subscribe, Sign up, Join, Submit or "
            f"Reveal code button, and never an email field. {caution}Return nothing if there is none.",
            timeout=safety.LLM_TIMEOUT_MS,
        )
        skipped = False
        for action in found.data[:2]:
            log.info(f"  Dismissing popup: {action.description}")
            skipped |= await safety.dismiss_on(stagehand, page, action) is None
        if skipped:
            log.info("  Closing popup with Escape instead")
            await page.key_press("Escape")
            await settle(page, 500)
    except BROWSER_ERRORS as error:  # a popup that cannot be dismissed is not fatal; a guard refusal still is
        log.debug("Popup dismissal failed: %s", error)


async def maybe_dismiss_popups(
    stagehand: Stagehand, page: Page, targets: str, caution: str = "", selectors: str = OVERLAY_SELECTORS
) -> None:
    """dismiss_popups, but the LLM is only invoked if a cheap DOM check finds a visible overlay."""
    if await js(page, JS_HAS_OVERLAY, selectors, default=False):
        await dismiss_popups(stagehand, page, targets, caution)


def is_sso_redirect(url: str) -> bool:
    """A corporate proxy bouncing the browser through Microsoft SSO; it resolves on its own."""
    return "login.microsoftonline.com" in url or "login.windows.net" in url


async def wait_out_sso(page: Page, ms: int = 3000) -> None:
    """Give a corporate SSO bounce up to `ms` to return to the site."""
    log.info("  Waiting for corporate SSO redirect...")
    await wait_until(page, "() => !/login\\.(microsoftonline\\.com|windows\\.net)/.test(location.host)", ms)


async def pause_for_login(page: Page, message: str, interactive: bool) -> bool:
    """Pause a headed (`interactive`) run so the user can sign in in the browser window. False if we can't pause."""
    if not interactive or not sys.stdin.isatty():
        return False
    log.warning(f"\n  [LOGIN REQUIRED] {message}")
    log.warning("  1. Complete the sign-in (and any SMS/email OTP) in the open browser window.")
    log.warning("  2. Wait until the browser redirects back to the site.")
    await asyncio.to_thread(input, "  3. Press Enter here to continue... ")
    await settle(page, 2500)
    return True
