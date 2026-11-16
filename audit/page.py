"""Browser page helpers shared by every workflow. All DOM work goes through `js()` (page.evaluate only)."""

import asyncio
import contextlib
import json
import logging
import os
import sys
from pathlib import Path

from stagehand import Page, Stagehand

# ---------------------------------------------------------------------------
# Harmless transient CDP frame/context race errors that Stagehand logs loudly
# ---------------------------------------------------------------------------
NOISY_CDP_PHRASES = (
    "CDP response failed",
    "Frame with the given id was not found",
    "Cannot find context with specified id",
    "Session with given id not found",
)


def is_noise(text: str) -> bool:
    return any(phrase in text for phrase in NOISY_CDP_PHRASES)


class _NoiseFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not is_noise(record.getMessage())


class _FilteredStream:
    """Drops the raw JSON CDP race errors Stagehand writes straight to stdout/stderr."""

    def __init__(self, stream):
        self._stream = stream

    def write(self, text: str) -> int:
        return len(text) if is_noise(text) else self._stream.write(text)

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


def install_noise_filter() -> None:
    for name in ("", "stagehand", "stagehand.cdp_client", "websockets"):
        logging.getLogger(name).addFilter(_NoiseFilter())
    if not isinstance(sys.stdout, _FilteredStream):
        sys.stdout, sys.stderr = _FilteredStream(sys.stdout), _FilteredStream(sys.stderr)


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
        if (b) { b.click(); return true; }
    }
    return false;
};
"""


async def js(page: Page, fn: str, arg=None, default=None, lib: str = ""):
    """Run the JS arrow function `fn(arg)` in the page with JS_LIB (+ `lib`) in scope; `default` on failure or null."""
    try:
        res = await page.evaluate(f"(() => {{ {JS_LIB}{lib}\nreturn ({fn})({json.dumps(arg)}); }})()")
    except Exception:
        return default
    return default if res is None else res


# ---------------------------------------------------------------------------
# Waiting & screenshots
# ---------------------------------------------------------------------------
async def settle(page: Page, ms: int = 1500) -> None:
    """Wait for the load event, then a fixed pause for client-side rendering."""
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("load")
    await page.wait_for_timeout(ms)


JS_RENDERED = r"""() => document.readyState === 'complete' && !!document.body
    && document.body.innerText.trim().length > 200
    && !Array.from(document.querySelectorAll('[class*="spinner" i], [class*="loader" i], [aria-busy="true"]'))
        .some(el => el.offsetParent !== null)"""


async def wait_for_render(page: Page, seconds: int = 20) -> None:
    """Wait for a client-side app to hydrate and its loading spinner to go away."""
    for _ in range(seconds):
        await page.wait_for_timeout(1000)
        if await js(page, JS_RENDERED, default=False):
            return


async def shot(page: Page, path: Path, full_page: bool = True) -> None:
    try:
        await page.screenshot(path=str(path), full_page=full_page)
    except Exception:
        with contextlib.suppress(Exception):
            await page.screenshot(path=str(path))


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


async def dismiss_popups(stagehand: Stagehand, targets: str, caution: str = "") -> None:
    """Ask the LLM to close whichever of `targets` (e.g. 'cookie banner or newsletter popup') is visible."""
    with contextlib.suppress(Exception):
        found = await stagehand.observe(
            "Find the close, accept, dismiss, 'No thanks', 'Maybe later' or 'Skip' button of any visible "
            f"{targets}. {caution}Return nothing if there is none."
        )
        for action in found.data[:2]:
            print(f"  Dismissing popup: {action.description}")
            await stagehand.act(action)


async def maybe_dismiss_popups(
    stagehand: Stagehand, page: Page, targets: str, caution: str = "", selectors: str = OVERLAY_SELECTORS
) -> None:
    """dismiss_popups, but the LLM is only invoked if a cheap DOM check finds a visible overlay."""
    if await js(page, JS_HAS_OVERLAY, selectors, default=False):
        await dismiss_popups(stagehand, targets, caution)


def is_sso_redirect(url: str) -> bool:
    """A corporate proxy bouncing the browser through Microsoft SSO; it resolves on its own."""
    return "login.microsoftonline.com" in url or "login.windows.net" in url


async def pause_for_login(page: Page, message: str) -> bool:
    """Pause a headed, interactive run so the user can sign in in the browser window. False if we can't pause."""
    if os.getenv("HEADLESS") == "true" or not sys.stdin.isatty():
        return False
    print(f"\n  [LOGIN REQUIRED] {message}")
    print("  1. Complete the sign-in (and any SMS/email OTP) in the open browser window.")
    print("  2. Wait until the browser redirects back to the site.")
    await asyncio.to_thread(input, "  3. Press Enter here to continue... ")
    await settle(page, 2500)
    return True
