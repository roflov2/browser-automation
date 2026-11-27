"""Browser stability, DOM-state hashing and the hard payment safety brake.

Everything that touches Stagehand's observe()/act() goes through `cdp_observe` and `cdp_act` here, so that:

* a CDP frame or session replaced mid-call (an SPA swapping views, a navigation) is retried, not fatal;
* the page is given a bounded, early-exiting chance to settle after every action;
* no click or keystroke can reach a final-purchase control: `cdp_act` checks the instruction, the model's chosen
  element and the element's live text against final-purchase keywords, and raises `CheckoutReachedInterrupt`
  instead of acting. The loop treats that as "we are at the payment wall" and stops.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import socket
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, TypeVar, cast
from urllib.parse import urlparse

from stagehand import LLMGenerateCallback, Stagehand, local_browser

from checkout_navigator.config import SITE_HINTS_PATH, NavigatorConfig
from checkout_navigator.engine.progress import is_skip_link

log = logging.getLogger(__name__)

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
BROWSER_ERRORS: tuple[type[Exception], ...] = (RuntimeError, TimeoutError, OSError)
NOISY_CDP_PHRASES = (
    "CDP response failed",
    "Frame with the given id was not found",
    "Cannot find context with specified id",
    "Session with given id not found",
)
# The page (or one of its frames) went away under us: a hiccup to retry, not a bug.
TRANSIENT_ERRORS = (
    *NOISY_CDP_PHRASES,
    "Execution context was destroyed",
    "Inspected target navigated or closed",
    "Target closed",
    "No frame for given id",
    "frame was detached",
    "Frame has been detached",
)
SCHEMA_REJECTED = "structuredContent"  # Stagehand's "the model's answer did not fit the schema" error
HICCUP_PAUSE_S = 1.0
MAX_HICCUP_RETRIES = 2


def is_transient(error: BaseException) -> bool:
    return any(phrase in str(error) for phrase in TRANSIENT_ERRORS)


class CheckoutReachedInterrupt(Exception):
    """The next action would pay, place or submit the order: the navigator has reached the payment wall."""

    def __init__(self, label: str, matched: str) -> None:
        super().__init__(f"refused final-purchase action {label!r} (matched {matched!r})")
        self.label = label
        self.matched = matched


class LoginWallBlocked(Exception):
    """A mandatory sign-in, CAPTCHA or bot check stands in the way and there is no guest path."""


class ActionRefused(Exception):
    """The chosen element is on the never-click list (sign-up, trial, loyalty...). Not a stop, just not done."""


# ---------------------------------------------------------------------------
# Hard payment safety brake
# ---------------------------------------------------------------------------
NEGATIVE_CONSTRAINT = (
    " NEVER click Pay, Place Order, Complete Purchase, Confirm & Pay, or Submit Payment, and never type into a "
    "payment card, expiry, CVV or card-holder field."
)
FINAL_PURCHASE = re.compile(
    r"\bpay\s*now\b|\bplace\s*(my\s*|your\s*)?order\b|\bcomplete\s*(my\s*|the\s*)?(purchase|order|booking|payment)\b"
    r"|\bsubmit\s*(my\s*)?(payment|order)\b|\bconfirm\s*(and|&|\+)\s*pay\b|\bpay\s*(securely|and\s*book)\b"
    # "Buy now" is deliberately absent: on product pages it is an express route TO checkout, not a payment.
    r"|\bpurchase\s*now\b|\bpay\s*\$\s*\d|\bpay\s*(a\$|aud|usd)\s*\d|\bconfirm\s*(the\s*)?booking\b"
    r"|\bbook\s*and\s*pay\b|\bauthori[sz]e\s*payment\b",
    re.IGNORECASE,
)
# Fields that take payment details: never typed into.
PAYMENT_FIELD = re.compile(
    r"card\s*(number|no\b|holder)|name\s*on\s*card|\bcvv\b|\bcvc\b|\bcsc\b|security\s*code|expir(y|ation)"
    r"|\bmm\s*/\s*yy\b|cc-(number|exp|csc|name)",
    re.IGNORECASE,
)
# Wording in which the model or a helper tells us what NOT to do; stripped before the keyword check so that a
# sentence like "Click Continue (do not click Place Order)" is not mistaken for a purchase.
NEGATED_CLAUSE = re.compile(
    r"\b(never|do\s*not|don'?t|without|avoid|not|instead\s*of|rather\s*than)\b[^.;:()\n]*", re.IGNORECASE
)
# A label that declines something ("No thanks, I don't want points") is not what the never-click list targets.
DECLINE_WORDS = re.compile(
    r"^\s*(no\b|no,|no thanks|not now|decline|skip|remove|without|continue without|maybe later|i don'?t)",
    re.IGNORECASE,
)
TYPING_METHODS = frozenset({"fill", "type", "press", "selectOptionFromDropdown"})


def final_purchase_match(text: str | None, *, strip_negations: bool = True) -> str | None:
    """The final-purchase phrase `text` asks for, if any (negated clauses ignored)."""
    if not text:
        return None
    haystack = NEGATED_CLAUSE.sub(" ", text) if strip_negations else text
    found = FINAL_PURCHASE.search(" ".join(haystack.split()))
    return found.group(0) if found else None


def never_click_match(text: str | None, pattern: re.Pattern[str] | None) -> str | None:
    if not text or pattern is None or DECLINE_WORDS.search(text):
        return None
    found = pattern.search(text)
    return found.group(0) if found else None


# ---------------------------------------------------------------------------
# Site hints (data, not code)
# ---------------------------------------------------------------------------
def load_site_hints(url: str, path: Path = SITE_HINTS_PATH) -> dict[str, Any]:
    """The `default` hints merged with every entry whose key is the URL's host or a parent domain of it."""
    try:
        import yaml  # noqa: PLC0415 - optional dependency
    except ImportError:
        log.warning("PyYAML is not installed; site_hints.yaml is ignored (pip install pyyaml)")
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as error:
        log.warning("Could not read %s (%s); using no site hints", path, error)
        return {}
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    merged: dict[str, Any] = dict(data.get("default") or {})
    for key, hints in data.items():
        if key == "default" or not isinstance(hints, dict):
            continue
        domain = str(key).lower().removeprefix("www.")
        if host == domain or host.endswith("." + domain):
            for name, value in hints.items():
                if name in ("never_click", "overlay_hint") and merged.get(name):
                    joiner = "|" if name == "never_click" else " "
                    merged[name] = f"{merged[name]}{joiner}{value}"
                elif name == "dismiss_selectors":
                    merged[name] = [*(merged.get(name) or []), *(value or [])]
                else:
                    merged[name] = value
    if merged.get("never_click"):
        try:
            merged["never_click_re"] = re.compile(str(merged["never_click"]), re.IGNORECASE)
        except re.error as error:
            log.warning("never_click hint is not a valid regex (%s); ignored", error)
    return merged


# ---------------------------------------------------------------------------
# CDP noise
# ---------------------------------------------------------------------------
NOISY_LOGGERS = ("", "stagehand", "stagehand.cdp_client", "websockets")


def _is_noise(text: str) -> bool:
    return any(phrase in text for phrase in NOISY_CDP_PHRASES)


class _NoiseFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _is_noise(record.getMessage())


class _FilteredStream:
    def __init__(self, stream: IO[str]) -> None:
        self._stream = stream

    def write(self, text: str) -> int:
        kept = "".join(line for line in text.splitlines(keepends=True) if not _is_noise(line))
        if kept:
            self._stream.write(kept)
        return len(text)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


@contextlib.contextmanager
def cdp_noise_filtered() -> Iterator[None]:
    """Hide the harmless CDP frame/context race errors Stagehand prints while pages navigate."""
    noise = _NoiseFilter()
    stdout, stderr = sys.stdout, sys.stderr
    for name in NOISY_LOGGERS:
        logging.getLogger(name).addFilter(noise)
    sys.stdout, sys.stderr = cast(IO[str], _FilteredStream(stdout)), cast(IO[str], _FilteredStream(stderr))
    try:
        yield
    finally:
        sys.stdout, sys.stderr = stdout, stderr
        for name in NOISY_LOGGERS:
            logging.getLogger(name).removeFilter(noise)


# ---------------------------------------------------------------------------
# Environment: persistent profile, proxy, sandbox
# ---------------------------------------------------------------------------
PROFILE_LOCKS = ("SingletonLock", "SingletonCookie", "SingletonSocket")


def _lock_owner_alive(lock: Path) -> bool:
    """Chrome's SingletonLock is a symlink to '<hostname>-<pid>'. True if that process still runs on this host."""
    try:
        target = str(lock.readlink())
    except OSError:
        return False
    host, _, pid = target.rpartition("-")
    if host and host != socket.gethostname():
        return True  # another machine (a shared/network profile): never assume it is stale
    try:
        os.kill(int(pid), 0)
    except (ValueError, ProcessLookupError):
        return False
    except PermissionError:
        return True
    return True


def prepare_user_data_dir(path: Path) -> Path:
    """Create the persistent Chrome profile, clearing a stale lock left by a crashed run.

    A Chrome that was killed leaves SingletonLock/SingletonCookie/SingletonSocket behind and the next launch on the
    same profile fails ("profile in use"). They are removed only when the process that owns them is gone.
    """
    path = path.expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    lock = path / "SingletonLock"
    if lock.is_symlink() or lock.exists():
        if _lock_owner_alive(lock):
            raise RuntimeError(
                f"The Chrome profile {path} is in use by a running browser; close it or set CHROME_PROFILE_DIR"
            )
        for name in PROFILE_LOCKS:
            with contextlib.suppress(OSError):
                (path / name).unlink()
        log.info("Removed a stale Chrome profile lock in %s", path)
    return path


def keep_loopback_off_proxy() -> None:
    """The local browser is driven over a loopback CDP socket: keep loopback out of any HTTP(S) proxy."""
    current = [h.strip() for h in (os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or "").split(",") if h]
    merged = ",".join(dict.fromkeys([*filter(None, current), "127.0.0.1", "localhost"]))
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = merged


@contextlib.asynccontextmanager
async def open_session(config: NavigatorConfig) -> AsyncIterator[tuple[Any, Any]]:
    """Launch local Chrome on the persistent profile and a Stagehand client on it. Yields (stagehand, page)."""
    keep_loopback_off_proxy()
    browser_cfg = config.browser
    options: dict[str, Any] = {
        "headless": browser_cfg.headless,
        "user_data_dir": str(prepare_user_data_dir(browser_cfg.user_data_dir)),
        "viewport_width": browser_cfg.viewport[0],
        "viewport_height": browser_cfg.viewport[1],
    }
    if browser_cfg.chrome_path:
        options["executable_path"] = str(browser_cfg.chrome_path)
    if browser_cfg.proxy_server:
        options["proxy_server"] = browser_cfg.proxy_server
        if browser_cfg.proxy_bypass:
            options["proxy_bypass"] = browser_cfg.proxy_bypass
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        options["chromium_sandbox"] = False  # Chrome refuses to start sandboxed as root (containers, CI)

    with cdp_noise_filtered():
        async with contextlib.AsyncExitStack() as stack:  # closed in reverse: Stagehand, Chrome, then Azure
            model = await _model_options(config, stack)  # an Azure sign-in failure stops before Chrome opens
            browser = await local_browser.launch(**options)
            stack.push_async_callback(_close_quietly, browser)
            stagehand = await Stagehand.create(browser=browser, system_prompt=config.system_prompt, **model)
            stack.push_async_callback(_close_quietly, stagehand)
            page = await _fresh_tab(browser)
            yield stagehand, page


async def _close_quietly(resource: Any) -> None:
    with contextlib.suppress(*BROWSER_ERRORS):
        await resource.close()


async def _model_options(config: NavigatorConfig, stack: contextlib.AsyncExitStack) -> dict[str, Any]:
    """Stagehand's model arguments: the Azure OpenAI callback when AZURE_OPENAI_ENDPOINT is set, else a model name."""
    azure = config.llm.azure
    if azure is None:
        return config.llm.stagehand_kwargs
    from checkout_navigator.engine.azure_llm import open_azure_llm  # noqa: PLC0415 - optional extras

    llm = await stack.enter_async_context(open_azure_llm(azure))
    log.info("Model: Azure OpenAI deployment %s", azure.deployment)
    # AzureLLM returns plain dicts shaped like Stagehand's result TypedDicts; Stagehand validates them.
    return {"model": cast(LLMGenerateCallback, llm)}


async def _fresh_tab(browser: Any) -> Any:
    """One new tab, with every tab the persistent profile restored closed."""
    old = await browser.context.pages()
    page = await browser.context.new_page()
    for tab in old:
        with contextlib.suppress(*BROWSER_ERRORS):
            await tab.close()
    await browser.context.set_active_page(page)
    return page


# ---------------------------------------------------------------------------
# Waits
# ---------------------------------------------------------------------------
LOAD_TIMEOUT_MS = 15_000
POLL_MS = 250
JS_QUIET = r"""() => {
    const body = document.body;
    const now = [document.readyState, location.href, document.getElementsByTagName('*').length,
                 body ? body.innerText.length : 0].join('|');
    const seen = window.__navQuiet || { sample: null, repeats: 0 };
    window.__navQuiet = { sample: now, repeats: seen.sample === now ? seen.repeats + 1 : 0 };
    return document.readyState === 'complete' && window.__navQuiet.repeats >= 2;
}"""
JS_RENDERED = r"""() => document.readyState === 'complete' && !!document.body
    && document.body.innerText.trim().length > 200
    && !Array.from(document.querySelectorAll('[class*="spinner" i], [class*="loader" i], [aria-busy="true"]'))
        .some(el => el.offsetParent !== null)"""
JS_IN_QUEUE = r"""() => {
    const t = (document.body ? document.body.innerText : '').slice(0, 4000);
    return /queue-it|you are (now )?in (the )?(line|queue)|virtual (waiting room|queue)|waiting room|your estimated wait/i.test(t)
        || /queue-it\.net/i.test(location.host);
}"""


async def wait_until(page: Any, condition: str, timeout_ms: int) -> bool:
    try:
        await page.wait_for_function(condition, timeout=timeout_ms, polling=POLL_MS)
    except BROWSER_ERRORS as error:
        log.debug("Stopped waiting after up to %d ms: %s", timeout_ms, error)
        return False
    return True


async def settle(page: Any, ms: int = 1500) -> None:
    """Wait for the load event, then until the DOM stops changing, for at most `ms` more."""
    try:
        await page.wait_for_load_state("load", timeout=LOAD_TIMEOUT_MS)
    except BROWSER_ERRORS as error:
        log.debug("Load event not seen: %s", error)
    with contextlib.suppress(*BROWSER_ERRORS):
        await page.evaluate("window.__navQuiet = null")
    await wait_until(page, JS_QUIET, ms)


async def wait_for_hydration(page: Any, seconds: float = 10) -> bool:
    """Wait for an SPA to render its content and drop its loading spinners. False if it never does."""
    return await wait_until(page, JS_RENDERED, int(seconds * 1000))


async def wait_out_queue(page: Any, seconds: float = 300) -> bool:
    """Sit in a virtual waiting room until it lets us through. True if we are (now) out of any queue."""
    if not await safe_evaluate(page, f"({JS_IN_QUEUE})()", default=False):
        return True
    log.info("  In a virtual queue / waiting room; waiting up to %ds", int(seconds))
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        await asyncio.sleep(10)
        if not await safe_evaluate(page, f"({JS_IN_QUEUE})()", default=True):
            await settle(page, 3000)
            return True
    return False


async def safe_evaluate(page: Any, expression: str, default: Any = None) -> Any:
    """page.evaluate that returns `default` when the page navigated away mid-script."""
    for attempt in range(MAX_HICCUP_RETRIES + 1):
        try:
            value = await page.evaluate(expression)
        except BROWSER_ERRORS as error:
            if is_transient(error) and attempt < MAX_HICCUP_RETRIES:
                await asyncio.sleep(HICCUP_PAUSE_S / 2)
                continue
            log.debug("evaluate failed: %s", error)
            return default
        return default if value is None else value
    return default


async def with_frame_retry(call: Callable[[], Awaitable[T]], what: str) -> T:
    """Run a Stagehand call, retrying when a frame/session was detached under it or the answer was off-schema."""
    for attempt in range(MAX_HICCUP_RETRIES + 1):
        try:
            return await call()
        except Exception as error:
            retryable = is_transient(error) or SCHEMA_REJECTED in str(error)
            if not retryable or attempt == MAX_HICCUP_RETRIES:
                raise
            log.warning("  %s: browser hiccup (%s); retrying", what, str(error)[:120])
            await asyncio.sleep(HICCUP_PAUSE_S * (attempt + 1))
    raise AssertionError("unreachable")  # pragma: no cover


# ---------------------------------------------------------------------------
# Observation: one deterministic DOM scan + one observe() call
# ---------------------------------------------------------------------------
# Everything the classifier and the stagnation guard need, read in one evaluate. Iframes are read from their
# parent (src/name/title work across origins); same-origin frames are descended into for controls.
JS_SCAN = r"""(() => {
    const clean = s => (s || '').replace(/\s+/g, ' ').trim();
    const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
        return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none' && parseFloat(s.opacity || '1') > 0.05; };
    const labelOf = (el, doc) => {
        const parts = [el.getAttribute('aria-label'), el.getAttribute('placeholder'), el.getAttribute('name'),
                       el.getAttribute('autocomplete'), el.getAttribute('title')];
        (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
            .forEach(id => { const n = doc.getElementById(id); if (n) parts.push(n.innerText); });
        if (el.id) { const l = doc.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) parts.push(l.innerText); }
        const wrap = el.closest('label'); if (wrap) parts.push(wrap.innerText);
        return clean(parts.filter(Boolean).join(' | ')).slice(0, 160);
    };
    const out = { url: location.href, title: document.title, iframes: [], controls: [], dialogs: 0, overlay: false,
                  overlay_matches: [], overlay_rejected: 0, headings: [], text_len: 0 };
    // "Skip to main content" style accessibility links: in-page anchors that never move a funnel forward.
    const skipLink = el => el.tagName === 'A' && (el.getAttribute('href') || '').startsWith('#')
        && /^skip (to|navigation)\b/i.test(clean(el.innerText || el.getAttribute('aria-label') || ''));
    const docs = [document];
    for (const f of document.querySelectorAll('iframe, frame')) {
        out.iframes.push({ src: f.src || f.getAttribute('src') || '', name: f.name || '', title: f.title || '',
                           visible: vis(f) });
        try { if (f.contentDocument && f.contentDocument.body) docs.push(f.contentDocument); } catch (e) {}
    }
    for (const doc of docs) {
        const sel = 'button, a[href], [role="button"], [role="link"], [role="tab"], [role="option"], [role="radio"], '
            + '[role="checkbox"], [role="combobox"], input:not([type="hidden"]), select, textarea';
        for (const el of doc.querySelectorAll(sel)) {
            if (out.controls.length >= 200) break;
            if (!vis(el) || skipLink(el)) continue;
            const tag = el.tagName.toLowerCase();
            const text = clean(el.innerText || el.value || '').slice(0, 100);
            const control = { tag, role: el.getAttribute('role') || '', type: el.getAttribute('type') || '',
                text: tag === 'input' || tag === 'textarea' ? '' : text, label: labelOf(el, doc),
                value: tag === 'input' || tag === 'select' || tag === 'textarea' ? clean(String(el.value || '')).slice(0, 60) : '',
                checked: !!(el.checked || el.getAttribute('aria-checked') === 'true' || el.getAttribute('aria-selected') === 'true'),
                disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true'),
                required: !!(el.required || el.getAttribute('aria-required') === 'true'),
                in_dialog: !!el.closest('[role="dialog"], [aria-modal="true"]') };
            if (!control.text && !control.label) continue;
            out.controls.push(control);
        }
    }
    // Overlay = something that BLOCKS the page, not any element whose class happens to say popup/modal/cookie
    // (design systems name date-picker triggers "popup-field", links "cookie-policy-link", and so on).
    const vw = window.innerWidth || document.documentElement.clientWidth || 1;
    const vh = window.innerHeight || document.documentElement.clientHeight || 1;
    const NOT_OVERLAY = 'button, a, input, select, textarea, label, option, [role="button"], [role="link"], '
        + '[role="combobox"], [role="option"], [role="alert"], [role="status"], [role="tooltip"]';
    const LANDMARK = 'header, nav, footer, main, [role="banner"], [role="navigation"], [role="contentinfo"], [role="main"]';
    // 'fixed' or 'sticky' when the element or an ancestor is pinned to the viewport, else ''.
    const pinned = el => { let found = ''; for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
        const p = getComputedStyle(n).position; if (p === 'fixed') return 'fixed'; if (p === 'sticky') found = 'sticky'; }
        return found; };
    const clip = r => ({ l: Math.max(0, r.left), t: Math.max(0, r.top), r: Math.min(vw, r.right), b: Math.min(vh, r.bottom) });
    const onTop = (el, c) => { let hits = 0;
        for (const [fx, fy] of [[0.5, 0.5], [0.15, 0.2], [0.85, 0.2], [0.15, 0.8], [0.85, 0.8]]) {
            const hit = document.elementFromPoint(c.l + (c.r - c.l) * fx, c.t + (c.b - c.t) * fy);
            if (hit && (hit === el || el.contains(hit))) hits++;
        }
        return hits >= 2; };
    const describeEl = el => { const cls = typeof el.className === 'string' ? el.className.trim().split(/\s+/)[0] : '';
        return clean(el.tagName.toLowerCase() + (el.id ? '#' + el.id : cls ? '.' + cls : '') + ' "'
            + clean(el.innerText || el.getAttribute('aria-label') || '').slice(0, 70) + '"'); };
    const modalSel = '[role="dialog"], [role="alertdialog"], [aria-modal="true"], dialog[open]';
    const nameSel = '[class*="popup" i], [class*="modal" i], [class*="newsletter" i], [id*="klaviyo" i], '
        + '[class*="cookie" i], [id*="cookie" i], [id*="consent" i], [class*="consent" i], [class*="age-gate" i], '
        + '[id*="onetrust" i], [class*="overlay" i], [class*="backdrop" i], [class*="lightbox" i], '
        + '[class*="interstitial" i], [id*="popup" i], [id*="modal" i]';
    const seen = new Set();
    for (const el of document.querySelectorAll(modalSel + ', ' + nameSel)) {
        if (seen.has(el)) continue; seen.add(el);
        if (!vis(el)) continue;
        if (el.matches(NOT_OVERLAY) || el.matches(LANDMARK) || el.closest('[role="alert"], [role="status"]')) {
            out.overlay_rejected++; continue; }
        const c = clip(el.getBoundingClientRect());
        const w = c.r - c.l, h = c.b - c.t;
        if (w < 40 || h < 20) { out.overlay_rejected++; continue; }  // off-screen or a sliver
        let why = '';
        if (el.matches(modalSel)) {
            let modal = el.getAttribute('aria-modal') === 'true';
            try { modal = modal || el.matches(':modal'); } catch (e) {}
            if (modal || onTop(el, c)) why = 'dialog';
        } else {
            // Pinned and on top, and either covering a quarter of the viewport or a full-width fixed bar at the
            // top/bottom edge (cookie bars are 'fixed'; a 'sticky' full-width bar is usually the site header).
            const pin = pinned(el);
            const big = (w * h) / (vw * vh) >= 0.25;
            const bar = pin === 'fixed' && w >= vw * 0.9 && h >= 40 && (c.t <= 2 || c.b >= vh - 2);
            if (pin && (big || bar) && onTop(el, c)) why = big ? 'covers the page' : 'edge bar';
        }
        if (!why) { out.overlay_rejected++; continue; }
        out.overlay = true;
        if (why === 'dialog') out.dialogs++;
        if (out.overlay_matches.length < 3) out.overlay_matches.push(describeEl(el) + ' (' + why + ')');
    }
    out.headings = Array.from(document.querySelectorAll('h1, h2, [role="heading"]')).filter(vis)
        .map(h => clean(h.innerText).slice(0, 80)).filter(Boolean).slice(0, 8);
    out.text_len = document.body ? document.body.innerText.length : 0;
    return out;
})()"""

OBSERVE_INSTRUCTION = (
    "Find the elements a shopper would interact with next to move this purchase or booking towards checkout: "
    "the primary call-to-action buttons (Continue, Next, Checkout, Add to cart, Select, Book, Search), any required "
    "form fields or option choices that are still empty, date pickers, location/search inputs, sign-in or 'continue "
    "as guest' choices, and the close/accept/dismiss button of any cookie banner, popup or modal covering the page. "
    "Return at most 15 elements, most important first."
)


@dataclass
class Observation:
    """What the page offers right now: the model's observed actions plus a deterministic DOM scan."""

    actions: list[Any] = field(
        default_factory=list
    )  # stagehand Action objects (selector, description, method, arguments)
    dom: dict[str, Any] = field(default_factory=dict)  # JS_SCAN output
    url: str = ""

    @property
    def controls(self) -> list[dict[str, Any]]:
        return list(self.dom.get("controls") or [])

    def describe(self, limit: int = 15) -> str:
        """Numbered list of the observed actions, for the decision prompt."""
        lines = []
        for index, action in enumerate(self.actions[:limit]):
            method = getattr(action, "method", "") or ""
            lines.append(f"[{index}] {getattr(action, 'description', '')} ({method})")
        return "\n".join(lines) or "(no elements observed)"


async def scan_dom(page: Any) -> dict[str, Any]:
    scanned = await safe_evaluate(page, JS_SCAN, default={})
    return scanned if isinstance(scanned, dict) else {}


async def cdp_observe(stagehand: Any, page: Any, instruction: str = OBSERVE_INSTRUCTION, **options: Any) -> Observation:
    """observe() with frame-retry, plus the DOM scan. An observe that fails outright leaves `actions` empty."""
    options.setdefault("timeout", 90_000)
    try:
        result = await with_frame_retry(lambda: stagehand.observe(instruction, **options), "observe")
        actions = [a for a in result.data or [] if not is_skip_link(str(getattr(a, "description", "") or ""))]
    except BROWSER_ERRORS as error:
        log.warning("  observe failed (%s); deciding from the DOM scan alone", str(error)[:160])
        actions = []
    dom = await scan_dom(page)
    try:
        url = await page.url()
    except BROWSER_ERRORS:
        url = str(dom.get("url") or "")
    return Observation(actions=actions, dom=dom, url=url)


def compute_dom_hash(observed: Observation | list[Any]) -> str:
    """A stable hash of what the user can interact with, for stagnation detection on SPAs.

    Built from the deterministic DOM scan (control text, label, value, checked/disabled state, dialogs, headings) so
    that the same screen always hashes the same even when the model's observe() returns a different subset. Filling
    a field, opening a drawer or ticking a box changes it. A bare list of observed actions is hashed by their
    normalised descriptions and selectors.
    """
    parts: list[str] = []
    if isinstance(observed, Observation) and observed.dom:
        dom = observed.dom
        for c in dom.get("controls") or []:
            parts.append(
                "|".join(str(c.get(k, "")) for k in ("tag", "role", "text", "label", "value", "checked", "disabled"))
            )
        parts.append(f"dialogs={dom.get('dialogs')}|overlay={dom.get('overlay')}")
        parts.extend(dom.get("headings") or [])
        parts.append(f"text≈{int(dom.get('text_len') or 0) // 50}")
    else:
        actions = observed.actions if isinstance(observed, Observation) else observed
        for action in actions:
            description = " ".join(str(getattr(action, "description", "")).lower().split())
            parts.append(f"{description}|{getattr(action, 'selector', '')}")
        parts.sort()
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Acting
# ---------------------------------------------------------------------------
JS_ELEMENT_TEXT = r"""(xpath => {
    let el = null;
    try { el = document.evaluate(xpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue; }
    catch (e) { return null; }
    if (!el || el.nodeType !== 1) return null;
    const clean = s => (s || '').replace(/\s+/g, ' ').trim();
    const control = el.closest('button, a, [role="button"], input, select, textarea, label') || el;
    return clean([control.innerText, control.value, control.getAttribute('aria-label'), control.getAttribute('name'),
                  control.getAttribute('placeholder'), control.getAttribute('autocomplete'), control.getAttribute('id'),
                  control.getAttribute('title')].filter(Boolean).join(' | ')).slice(0, 300);
})"""
JS_PAGE_STATE = r"""(() => {
    const text = document.body ? document.body.innerText : '';
    let hash = 0;
    for (let i = 0; i < text.length; i++) hash = (hash * 31 + text.charCodeAt(i)) | 0;
    return location.href + '|' + text.length + '|' + hash;
})()"""


def _xpath(selector: str) -> str | None:
    if not selector:
        return None
    if selector.startswith("xpath="):
        return selector.removeprefix("xpath=")
    return selector if selector.startswith("/") else None


async def element_text(page: Any, selector: str) -> str:
    """The live text/labels of the element a selector names (main frame only); '' when it cannot be read."""
    xpath = _xpath(selector)
    if not xpath:
        return ""
    value = await safe_evaluate(page, f"{JS_ELEMENT_TEXT}({json.dumps(xpath)})", default="")
    return value if isinstance(value, str) else ""


async def brake_check(page: Any, action: Any, instruction: str | None, never_click: re.Pattern[str] | None) -> None:
    """Raise CheckoutReachedInterrupt if the action would pay; ActionRefused if it is on the never-click list.

    Checked, in order: the caller's instruction (negated clauses ignored), the model's description of the element
    it chose, and the element's live text in the page. Typing into a payment-card field is refused as a purchase too.
    """
    description = str(getattr(action, "description", "") or "") if action is not None else ""
    method = str(getattr(action, "method", "") or "") if action is not None else ""
    live = await element_text(page, str(getattr(action, "selector", "") or "")) if action is not None else ""

    for label, text, strip in (
        ("instruction", instruction, True),
        ("element", description, True),
        ("page", live, False),
    ):
        matched = final_purchase_match(text, strip_negations=strip)
        if matched:
            log.warning("  SAFETY BRAKE (%s): %r matched %r", label, (text or "")[:120], matched)
            raise CheckoutReachedInterrupt(text or "", matched)
    if method in TYPING_METHODS and (PAYMENT_FIELD.search(description) or PAYMENT_FIELD.search(live)):
        log.warning("  SAFETY BRAKE: refusing to type into a payment field (%s)", description[:120])
        raise CheckoutReachedInterrupt(description, "payment field")
    for text in (description, live):
        refused = never_click_match(text, never_click)
        if refused:
            raise ActionRefused(f"{text[:120]!r} is on the never-click list ({refused!r})")


@dataclass(frozen=True)
class ActOutcome:
    success: bool
    description: str
    selector: str = ""


async def _page_state(page: Any) -> str | None:
    value = await safe_evaluate(page, JS_PAGE_STATE, default=None)
    return value if isinstance(value, str) else None


async def cdp_act(
    stagehand: Any,
    page: Any,
    instruction: str,
    *,
    action: Any = None,
    variables: dict[str, Any] | None = None,
    hints: dict[str, Any] | None = None,
    timeout_ms: int = 90_000,
    settle_after: bool = True,
) -> ActOutcome:
    """One atomic, safety-checked action.

    With `action` (an element already returned by observe()), it is acted on directly: no extra model call. Without,
    the instruction (plus NEGATIVE_CONSTRAINT) is observed first so the chosen element can be checked before it is
    touched. Raises CheckoutReachedInterrupt instead of acting on anything that would pay.
    """
    hints = hints or {}
    never_click = hints.get("never_click_re")
    options: dict[str, Any] = {"timeout": timeout_ms}
    if variables:
        options["variables"] = variables

    # 1. The instruction itself (before the constraint is appended: it names the forbidden buttons).
    await brake_check(page, None, instruction, None)
    guarded = instruction.rstrip() + NEGATIVE_CONSTRAINT

    # 2. Resolve the element.
    if action is None:
        found = await with_frame_retry(lambda: stagehand.observe(guarded, **options), "observe")
        if not found.data:
            log.debug("Nothing to act on for %r", instruction)
            return ActOutcome(False, "no matching element found")
        action = found.data[0]

    # 3. Check the element, then act, retrying once after a detached frame without ever clicking twice.
    await brake_check(page, action, None, never_click)
    before = await _page_state(page)
    try:
        result = await stagehand.act(action, **options)
    except Exception as error:
        if not is_transient(error):
            raise
        log.warning("  act: browser hiccup (%s); checking whether it took effect", str(error)[:120])
        await asyncio.sleep(HICCUP_PAUSE_S)
        after = await _page_state(page)
        if before is not None and after is not None and after != before:
            outcome = ActOutcome(True, "acted (page changed despite a CDP error)", str(action.selector or ""))
        else:
            again = await with_frame_retry(lambda: stagehand.observe(guarded, **options), "observe")
            if not again.data:
                return ActOutcome(False, "element gone after a browser hiccup")
            action = again.data[0]
            await brake_check(page, action, None, never_click)
            result = await stagehand.act(action, **options)
            outcome = _outcome(result, action)
    else:
        outcome = _outcome(result, action)
    if settle_after:
        await settle(page, int(hints.get("settle_ms") or 1500))
    return outcome


def _outcome(result: Any, action: Any) -> ActOutcome:
    data = getattr(result, "data", None)
    return ActOutcome(
        bool(getattr(data, "success", False)),
        str(getattr(data, "action_description", "") or getattr(action, "description", "")),
        str(getattr(action, "selector", "") or ""),
    )


async def try_cdp_act(stagehand: Any, page: Any, instruction: str, **kwargs: Any) -> ActOutcome | None:
    """cdp_act for a best-effort step: ordinary failures and refusals return None. The brake still propagates."""
    try:
        return await cdp_act(stagehand, page, instruction, **kwargs)
    except CheckoutReachedInterrupt:
        raise
    except ActionRefused as refused:
        log.info("  Refused: %s", refused)
        return None
    except BROWSER_ERRORS as error:
        log.debug("Optional action failed (%r): %s", instruction, error)
        return None


async def click_point(page: Any, x: float, y: float) -> None:
    """A real mouse click at a viewport point, for controls that ignore DOM clicks (React/Angular widgets)."""
    await page.click(x, y)


async def guarded_click_point(page: Any, target: dict[str, Any], hints: dict[str, Any] | None = None) -> None:
    """Click a DOM-located target ({text, x, y}) after the same keyword brake cdp_act applies."""
    text = str(target.get("text") or "")
    matched = final_purchase_match(text, strip_negations=False)
    if matched:
        raise CheckoutReachedInterrupt(text, matched)
    refused = never_click_match(text, (hints or {}).get("never_click_re"))
    if refused:
        raise ActionRefused(f"{text[:120]!r} is on the never-click list ({refused!r})")
    await click_point(page, float(target["x"]), float(target["y"]))


async def cdp_extract(stagehand: Any, instruction: str, schema: Any, timeout_ms: int = 90_000) -> Any:
    """extract() with frame-retry; returns the parsed model (result.data)."""
    result = await with_frame_retry(lambda: stagehand.extract(instruction, schema, timeout=timeout_ms), "extract")
    return result.data


async def press_key(page: Any, key: str) -> None:
    with contextlib.suppress(*BROWSER_ERRORS):
        await page.key_press(key)


async def dom_click(page: Any, js_finder: str, arg: Any, hints: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Run a finder snippet returning {text, x, y} (or null) and mouse-click it behind the brake. The target or None."""
    found = await safe_evaluate(page, f"({js_finder})({json.dumps(arg)})", default=None)
    if not isinstance(found, dict) or "x" not in found:
        return None
    await guarded_click_point(page, found, hints)
    await settle(page, int((hints or {}).get("settle_ms") or 1500))
    return found
