"""The purchase guard: nothing in this package may place an order, pay, or start a paid membership.

It covers the basics only:

- Clicks on controls that pay, place an order or start a trial/membership are refused.
- Typing into card fields, or typing anything that looks like a card number, is refused.
- `Guard.freeze()`: once a workflow has reached the final checkout/payment page, nothing more may be
  clicked or typed on that site; the page is only read.

These are enforced in three places: `GUARD_JS` (an init script that cancels denied clicks, key presses,
form submits and typing on every page, whoever caused them), `safe_act()` (the only way to run an LLM
action: observe, vet, act), and `safeClick()` / `safeSelect()` / `safeFill()` (the only way in-page
JavaScript may click or type).

The rules are data, written once here and injected into the page scripts, so Python and JavaScript
cannot drift. Patterns use only syntax that Python `re` and JavaScript `RegExp` read the same way.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import urlsplit

from .core import BROWSER_ERRORS, UnsafeActionRefused

log = logging.getLogger(__name__)

LLM_TIMEOUT_MS = 90_000  # Stagehand timeouts are in milliseconds
LABEL_LIMIT = 160  # only the start of a long label is examined

# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------
# Searched (case-insensitively) in each part of a control's label: its text, aria-label, title, value, alt.
DENY = (
    # placing and paying for an order
    r"\bplace\s+(your\s+|my\s+)?order\b",
    r"^pay$",
    r"\bpay\s+now\b",
    r"\bpay\s+(a?\$|aud\b|usd\b|nzd\b|€|£|\d)",
    r"\bpay\s+(with|by|using|in)\b",
    r"\b(confirm|complete|finish|submit|finalise|finalize|make)\s+(and\s+|&\s+)?(your\s+|my\s+|the\s+)?(pay|payment|purchase|order|booking)\b",
    r"^purchase(?!\s+(options?|type|history))(\s+\S+){0,2}$",
    r"\bpurchase\s+now\b",
    r"^buy$",
    r"\bbuy\s+(it\s+)?now\b",
    r"\bbuy\s+with\b",
    r"\bbook\s+with\b",
    r"\bbook\s+(and|&)\s+pay\b",
    # one-click wallets and pay-later providers
    r"\b(apple|google|shop|amazon|g)\s*pay\b",
    r"\bgpay\b",
    r"\bpaypal\b",
    r"\bafterpay\b",
    r"\bklarna\b",
    r"\bpayid\b",
    r"^zip$",
    r"\bzip\s?pay\b",
    # trials, memberships and accounts
    r"\bfree\s+trial\b",
    r"\bstart\s+(your\s+|my\s+)?(free\s+)?(trial|membership)\b",
    r"\b(join|try)\s+uber\s+one\b",
    r"\bbecome\s+a\s+member\b",
)
# Searched in a form field's autocomplete, name, id, placeholder, aria-label and <label> text.
SENSITIVE_FIELD = (
    r"\bcc[-_ ]?(number|num|csc|cvc|exp|name)",
    r"card[-_ ]?(number|num|no|holder)",
    r"\bcvv|\bcvc|security[-_ ]?code",
    r"expir|\bexp[-_ ]?(date|month|year)|\bmm\s*/\s*yy",
)

TYPING_METHODS = frozenset({"fill", "type"})  # Stagehand's text-entry methods; everything else is treated as a click
FROZEN_REASON = "the final checkout/payment page has been identified; no further actions are allowed on it"

_RULES: dict[str, tuple[str, ...]] = {"deny": DENY, "sensitiveField": SENSITIVE_FIELD}
_COMPILED = {name: tuple(re.compile(p, re.IGNORECASE) for p in patterns) for name, patterns in _RULES.items()}


class Mode(StrEnum):
    NORMAL = "normal"
    FROZEN = "frozen"  # the final page: nothing


def normalise(label: str | None) -> str:
    return " ".join((label or "").split())[:LABEL_LIMIT]


def _hit(rules: str, parts: Iterable[str]) -> str | None:
    return next((part for part in parts for pattern in _COMPILED[rules] if pattern.search(part)), None)


def click_verdict(parts: str | Sequence[str], *, mode: Mode = Mode.NORMAL) -> str | None:
    """Why a click on a control labelled `parts` must be refused, or None if it may go ahead."""
    labels = [p for p in map(normalise, [parts] if isinstance(parts, str) else parts) if p]
    if mode is Mode.FROZEN:
        return FROZEN_REASON
    if hit := _hit("deny", labels):
        return f"purchase, payment or membership control: {hit}"
    return None


def field_verdict(descriptor: str | None) -> str | None:
    """Why typing into a field described by `descriptor` must be refused, or None."""
    text = normalise(descriptor)
    return f"payment card field: {text}" if _hit("sensitiveField", [text]) else None


def looks_like_card_number(text: str) -> bool:
    digits = re.sub(r"[\s-]", "", text)
    if not digits.isdigit() or not 13 <= len(digits) <= 19:
        return False
    doubled = [int(d) * 2 - 9 if int(d) * 2 > 9 else int(d) * 2 for d in digits[-2::-2]]
    return (sum(doubled) + sum(int(d) for d in digits[-1::-2])) % 10 == 0


# ---------------------------------------------------------------------------
# Per-run state: which hosts are frozen
# ---------------------------------------------------------------------------
class _Evaluates(Protocol):
    async def evaluate(self, expression: str) -> Any: ...

    async def url(self) -> str: ...


def _host(url: str) -> str:
    return urlsplit(url).netloc


@dataclass
class Guard:
    """The hard stop for one audited site. `freeze` only ever tightens."""

    frozen: set[str] = field(default_factory=set)

    def mode_for(self, url: str) -> Mode:
        return Mode.FROZEN if _host(url) in self.frozen else Mode.NORMAL

    def state_json(self) -> str:
        return json.dumps({"frozen": sorted(self.frozen)})

    async def freeze(self, page: _Evaluates) -> None:
        """The final checkout/payment page: from now on only reads and screenshots on this host."""
        host = _host(await page.url())
        if host and host not in self.frozen:
            self.frozen.add(host)
            log.info("  Guard: %s is now frozen", host)
        await push_state(page, self)


_ACTIVE: ContextVar[Guard | None] = ContextVar("audit_guard", default=None)


def current_guard() -> Guard:
    """The guard of the run in progress. Outside a run there is no host state, but the rules still apply."""
    return _ACTIVE.get() or Guard()


@contextmanager
def active_guard() -> Iterator[Guard]:
    """Scope a fresh Guard to one audited site."""
    guard = Guard()
    token = _ACTIVE.set(guard)
    try:
        yield guard
    finally:
        _ACTIVE.reset(token)


# ---------------------------------------------------------------------------
# In-page JavaScript
# ---------------------------------------------------------------------------
# The rule engine. Shared by the init script and by every snippet run through page.js().
RULES_JS = (
    "const AUDIT_RULES = "
    + json.dumps(_RULES)
    + ";\nconst AUDIT_FROZEN = "
    + json.dumps(FROZEN_REASON)
    + ";\nconst AUDIT_LIMIT = "
    + str(LABEL_LIMIT)
    + r""";
const auditRx = Object.fromEntries(Object.entries(AUDIT_RULES).map(([k, list]) => [k, list.map(s => new RegExp(s, 'i'))]));
const auditNorm = s => (s || '').replace(/\s+/g, ' ').trim().slice(0, AUDIT_LIMIT);
const auditHit = (rules, parts) => { for (const p of parts) for (const r of auditRx[rules]) if (r.test(p)) return p; return null; };
const auditClickVerdict = (parts, mode) => {
    const labels = parts.map(auditNorm).filter(Boolean);
    if (mode === 'frozen') return AUDIT_FROZEN;
    const hit = auditHit('deny', labels);
    return hit ? 'purchase, payment or membership control: ' + hit : null;
};
const auditFieldVerdict = descriptor => {
    const text = auditNorm(descriptor);
    return auditHit('sensitiveField', [text]) ? 'payment card field: ' + text : null;
};
const AUDIT_ACTIONABLE = 'button, a, summary, label, select, input[type="submit"], input[type="button"], input[type="image"], '
    + 'input[type="checkbox"], input[type="radio"], [role="button"], [role="link"], [role="menuitem"], [role="option"], '
    + '[role="checkbox"], [role="radio"], [role="switch"], [role="tab"]';
const auditParts = el => {
    const parts = [el.innerText, el.getAttribute('aria-label'), el.getAttribute('title'), el.getAttribute('alt')];
    if (el.matches('button, input[type="submit"], input[type="button"]')) parts.push(el.value);
    for (const img of el.querySelectorAll('img[alt], svg[aria-label]')) parts.push(img.getAttribute('alt') || img.getAttribute('aria-label'));
    for (const label of el.labels || []) parts.push(label.innerText);
    return parts.filter(p => typeof p === 'string').map(auditNorm).filter(Boolean);
};
const auditField = el => [el.getAttribute('autocomplete'), el.getAttribute('name'), el.id, el.getAttribute('placeholder'),
    el.getAttribute('aria-label'), ...Array.from(el.labels || []).map(l => l.innerText)].filter(Boolean).join(' ');
const auditEditable = el => el.matches(
    'input:not([type="button"]):not([type="submit"]):not([type="checkbox"]):not([type="radio"]):not([type="image"]), textarea, [contenteditable]');
const auditState = () => {
    try { if (window.top.__auditGuard) return window.top.__auditGuard.state; } catch (e) { /* cross-origin frame */ }
    return window.__auditGuard ? window.__auditGuard.state : { frozen: [] };
};
const auditMode = () => auditState().frozen.includes(location.host) ? 'frozen' : 'normal';
// The element and every actionable ancestor, nearest first, through open shadow roots.
const auditChain = el => {
    const chain = [];
    for (let n = el; n && n.nodeType === 1; n = n.parentElement || (n.getRootNode() || {}).host)
        if (n === el || n.matches(AUDIT_ACTIONABLE)) chain.push(n);
    return chain;
};
// Why a click on `el` must be refused, or null.
const auditElementVerdict = el => {
    if (auditMode() === 'frozen') return AUDIT_FROZEN;
    for (const n of auditChain(el)) { const why = auditClickVerdict(auditParts(n), 'normal'); if (why) return why; }
    return null;
};
"""
)

# Layer 1. Installed with add_init_script, so it runs before any page script in every document.
GUARD_JS = (
    "(() => {\nif (window.__auditGuard) return;\n"
    + RULES_JS
    + r"""
const G = window.__auditGuard = {
    state: { frozen: [] },
    blocked: [],
    drain() { const out = this.blocked; this.blocked = []; return out; },
};
const record = (why, kind) => {
    const last = G.blocked[G.blocked.length - 1];
    if (!last || last.why !== why) G.blocked.push({ why, kind });
};
const block = (event, why) => { event.preventDefault(); event.stopImmediatePropagation(); record(why, event.type); };
const targetOf = event => {
    const first = event.composedPath ? event.composedPath()[0] : event.target;
    return first && first.nodeType === 1 ? first : null;
};
const onPointer = event => {
    const el = targetOf(event);
    const why = el ? auditElementVerdict(el) : (auditMode() === 'frozen' ? AUDIT_FROZEN : null);
    if (why) block(event, why);
};
for (const type of ['click', 'auxclick', 'dblclick', 'mousedown', 'mouseup', 'pointerdown', 'pointerup', 'touchstart', 'touchend'])
    window.addEventListener(type, onPointer, true);

const onTyping = event => {
    const el = targetOf(event);
    if (!el || !auditEditable(el)) return false;
    const why = auditMode() === 'frozen' ? AUDIT_FROZEN : auditFieldVerdict(auditField(el));
    if (why) block(event, why);
    return !!why;
};
for (const type of ['beforeinput', 'paste', 'drop']) window.addEventListener(type, onTyping, true);
window.addEventListener('keydown', event => {
    if (onTyping(event) || (event.key !== 'Enter' && event.key !== ' ')) return;
    const el = targetOf(event);
    if (!el) return;
    if (el.matches(AUDIT_ACTIONABLE) || auditMode() !== 'normal') { const why = auditElementVerdict(el); if (why) block(event, why); }
}, true);

const submitVerdict = (form, submitter) => {
    if (auditMode() === 'frozen') return AUDIT_FROZEN;
    const button = submitter || form.querySelector('button:not([type="button"]):not([type="reset"]), input[type="submit"], input[type="image"]');
    return button ? auditElementVerdict(button) : null;
};
window.addEventListener('submit', event => {
    const why = event.target && event.target.querySelector ? submitVerdict(event.target, event.submitter) : null;
    if (why) block(event, why);
}, true);
for (const name of ['submit', 'requestSubmit']) {
    const original = HTMLFormElement.prototype[name];
    HTMLFormElement.prototype[name] = function (...args) {
        const why = submitVerdict(this, args[0]);
        if (why) { record(why, name); return undefined; }
        return original.apply(this, args);
    };
}
})();"""
)

# Layer 3. Prepended to every snippet run through page.js(): the only way in-page code may click or type.
SAFE_JS = (
    RULES_JS
    + r"""
const auditRefused = [];
const guardAllows = el => !auditElementVerdict(el);
const safeClick = el => {
    const why = auditElementVerdict(el);
    if (why) { auditRefused.push({ why, kind: 'safeClick' }); return false; }
    el.click();
    return true;
};
const safeSelect = (el, value) => {
    const option = Array.from(el.options || []).find(o => o.value === value);
    const why = auditElementVerdict(el) || (option ? auditClickVerdict([option.text], auditMode()) : null);
    if (why) { auditRefused.push({ why, kind: 'safeSelect' }); return false; }
    el.value = value;
    el.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
};
const safeFill = (el, text) => {
    const why = auditMode() === 'frozen' ? AUDIT_FROZEN : auditFieldVerdict(auditField(el));
    if (why) { auditRefused.push({ why, kind: 'safeFill' }); return false; }
    el.focus();
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
    if (setter && setter.set && el instanceof HTMLInputElement) setter.set.call(el, text); else el.value = text;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
};
"""
)

_DESCRIBE_JS = r"""selector => {
    let el = null;
    try {
        const xpath = selector.replace(/^xpath=/i, '');
        el = /^\(?\//.test(xpath)
            ? document.evaluate(xpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue
            : document.querySelector(selector.replace(/^css=/i, ''));
    } catch (e) { el = null; }
    if (!el || el.nodeType !== 1) return { found: false };
    return { found: true, labelled: auditChain(el).some(n => auditParts(n).length > 0), click: auditElementVerdict(el),
             field: auditEditable(el) ? auditFieldVerdict(auditField(el)) : null };
}"""


def _state_js(guard: Guard) -> str:
    return f"if (window.__auditGuard) window.__auditGuard.state = {guard.state_json()};"


def wrap_snippet(guard: Guard, body: str, call: str) -> str:
    """The expression page.js() evaluates: sync the hard-stop state, run `call`, report refusals."""
    return (
        f"(() => {{ {_state_js(guard)}\n{SAFE_JS}{body}\n"
        "let value = null, error = null;\n"
        f"try {{ value = {call}; }} catch (e) {{ error = String(e); }}\n"
        "const G = window.__auditGuard;\n"
        "return { value: value === undefined ? null : value, error, refused: auditRefused, background: G ? G.drain() : [] }; })()"
    )


def raise_if_refused(refused: Sequence[Mapping[str, Any]] | None) -> None:
    """Turn refusals reported by the page (safeClick or the in-page blocker) into the run-ending exception."""
    if refused:
        raise UnsafeActionRefused("; ".join(f"{r.get('why')} [{r.get('kind')}]" for r in refused))


def log_page_refusals(blocked: Sequence[Mapping[str, Any]] | None) -> None:
    """Log what the in-page blocker cancelled on its own while no action of ours was running.

    That is usually the page's own script (Shopify's checkout submits a form as it loads). It was
    cancelled and nothing was sent, so the run goes on.
    """
    for refusal in blocked or ():
        log.info(f"  Guard cancelled the page's own {refusal.get('kind')} ({refusal.get('why')}); nothing was sent")


async def push_state(page: _Evaluates, guard: Guard) -> None:
    """Tell the page which hosts are frozen."""
    await page.evaluate(f"(() => {{ {_state_js(guard)} }})()")


async def drain_blocked(page: _Evaluates) -> None:
    """Raise if the in-page blocker cancelled anything since the last check."""
    blocked = await page.evaluate("(() => window.__auditGuard ? window.__auditGuard.drain() : [])()")
    raise_if_refused(blocked)


class _InitScripts(Protocol):
    async def add_init_script(self, source: str) -> None: ...


async def install(context: _InitScripts, page: _Evaluates) -> None:
    """Install the in-page blocker for every future document, and for the page that is already open."""
    await context.add_init_script(GUARD_JS)
    await page.evaluate(GUARD_JS)


# ---------------------------------------------------------------------------
# Layer 2: vetted LLM actions
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Acted:
    """What safe_act did. `success` is False when the model found nothing to act on."""

    success: bool
    description: str
    selector: str = field(default="", compare=False)  # what was acted on, for the run's step log


# The Stagehand client, the runner's recording wrapper around it, or a test double: anything with observe() and act().
_Actions = Any


def _selector_words(selector: str) -> str:
    """'[data-testid="place-order-btn"]' -> 'data testid place order btn', so label rules can read it."""
    return " ".join(re.sub(r"[^a-z0-9$]+", " ", selector.lower()).split())


def check_action(
    description: str,
    selector: str,
    method: str | None,
    arguments: Sequence[str],
    element: Mapping[str, Any] | None,
    *,
    mode: Mode,
) -> None:
    """Raise UnsafeActionRefused unless the action proposed by observe() is safe to run.

    `element` is what the page reported for the selector (None if it could not be resolved, e.g.
    inside a cross-origin frame); the element's own label is trusted over the model's description.
    """
    found = bool(element and element.get("found"))
    info = element if found and element else {}
    typing = (method or "").lower() in TYPING_METHODS
    why: str | None = FROZEN_REASON if mode is Mode.FROZEN else info.get("field")  # never touch a payment field
    if not why and typing:
        why = None if found else field_verdict(description)
        if not why and any(looks_like_card_number(a) for a in arguments):
            why = "typing what looks like a card number"
    elif not why:
        why = info.get("click") or click_verdict(_selector_words(selector))
        if not why and not info.get("labelled"):  # no readable label: go by the model's description
            why = click_verdict(description)
    if why:
        raise UnsafeActionRefused(f"{why} (proposed action: {description!r}, {selector!r})")


async def describe_element(page: _Evaluates, guard: Guard, selector: str) -> Mapping[str, Any] | None:
    """Ask the page what `selector` points at; None if the page cannot say."""
    script = f"(() => {{ {_state_js(guard)}\n{RULES_JS}\nreturn ({_DESCRIBE_JS})({json.dumps(selector)}); }})()"
    try:
        found = await page.evaluate(script)
    except BROWSER_ERRORS as error:  # any failure to read the page means "unknown", which fails closed
        log.debug("Could not resolve %r in the page: %s", selector, error)
        return None
    return found if isinstance(found, dict) else None


async def vet(page: _Evaluates, action: Any) -> None:
    """Raise UnsafeActionRefused unless `action` (from observe()) is safe to run. Never acts."""
    guard = current_guard()
    mode = guard.mode_for(await page.url())
    element = await describe_element(page, guard, action.selector)
    check_action(
        action.description, action.selector, action.method, list(action.arguments or []), element, mode=mode
    )  # fmt: skip


async def act_on(stagehand: _Actions, page: _Evaluates, action: Any, **options: Any) -> Acted:
    """Vet one action returned by observe(), then run it. The only place an action is executed."""
    await vet(page, action)
    options.setdefault("timeout", LLM_TIMEOUT_MS)
    result = await stagehand.act(action, **options)
    await drain_blocked(page)
    return Acted(
        bool(result.data.success), result.data.action_description or action.description, str(action.selector or "")
    )


async def dismiss_on(stagehand: _Actions, page: _Evaluates, action: Any, **options: Any) -> Acted | None:
    """act_on() for a control proposed to close a popup; None, without acting, if vetting refuses it.

    A popup's close button and its 'Subscribe' / 'Sign up' button sit side by side, and the model sometimes
    proposes the wrong one. That is a misread of the popup, not a site steering us into a purchase, and
    nothing has been clicked yet, so the control is skipped and the caller tries another way to close it.
    A click the in-page blocker cancels after vetting passed still raises, as everywhere else.
    """
    try:
        await vet(page, action)
    except UnsafeActionRefused as refusal:
        log.warning(f"  Skipping popup control the guard refuses ({refusal}); not clicked")
        return None
    return await act_on(stagehand, page, action, **options)


class _Clicks(_Evaluates, Protocol):
    async def click(self, x: float, y: float) -> None: ...


_AT_POINT_JS = r"""([x, y]) => {
    const el = document.elementFromPoint(x, y);
    if (!el) return { why: 'nothing on screen at that point' };
    return { why: auditElementVerdict(el), label: auditParts(el).join(' ').slice(0, 80) };
}"""


async def safe_click_at(page: _Clicks, x: float, y: float) -> None:
    """A real mouse click at viewport point (x, y), for controls that ignore a DOM click.

    The element under the point (and every actionable ancestor) is vetted with the same rules as
    safeClick first; the in-page blocker still sees the click itself. Raises UnsafeActionRefused
    instead of clicking anything the guard refuses.
    """
    guard = current_guard()
    if guard.mode_for(await page.url()) is Mode.FROZEN:
        raise UnsafeActionRefused(f"{FROZEN_REASON} (click at {x:.0f},{y:.0f})")
    script = f"(() => {{ {_state_js(guard)}\n{RULES_JS}\nreturn ({_AT_POINT_JS})({json.dumps([x, y])}); }})()"
    try:
        found = await page.evaluate(script)
    except BROWSER_ERRORS as error:  # cannot say what is there: fail closed
        raise UnsafeActionRefused(f"could not check the element at {x:.0f},{y:.0f}: {error}") from error
    if not isinstance(found, dict) or found.get("why"):
        why = found.get("why") if isinstance(found, dict) else "unknown element"
        raise UnsafeActionRefused(f"{why} (click at {x:.0f},{y:.0f})")
    await page.click(x, y)
    await drain_blocked(page)


async def safe_act(
    stagehand: _Actions, page: _Evaluates, instruction: str, *, follow_up: bool = False, **options: Any
) -> Acted:
    """Run one LLM action: observe, vet, act. Raises UnsafeActionRefused instead of doing anything unsafe.

    `options` are passed to both observe() and act() (e.g. `variables`, `timeout`, `locator`).

    Stagehand's own act() on a plain instruction can take a second step by itself (open a custom
    dropdown, then pick the option). Vetting needs each step to be observed first, so that second
    step is opt-in here: with `follow_up`, the instruction is observed once more after the first
    action and, if the model now points at a different element, that action is vetted and run too.
    """
    if current_guard().mode_for(await page.url()) is Mode.FROZEN:
        raise UnsafeActionRefused(f"{FROZEN_REASON} (instruction: {instruction!r})")
    options.setdefault("timeout", LLM_TIMEOUT_MS)
    found = await stagehand.observe(instruction, **options)
    if not found.data:
        log.debug("Nothing to act on for %r", instruction)
        return Acted(False, "no matching element found")
    first = found.data[0]
    done = await act_on(stagehand, page, first, **options)
    if follow_up and done.success and first.method == "click":
        again = await stagehand.observe(instruction, **options)
        if again.data and again.data[0].selector != first.selector:
            done = await act_on(stagehand, page, again.data[0], **options)
    return done


async def try_act(stagehand: _Actions, page: _Evaluates, instruction: str, **options: Any) -> Acted | None:
    """safe_act() for an optional step: an ordinary failure is logged at debug level and returns None.

    A guard refusal is never an ordinary failure; it still raises and ends the run.
    """
    try:
        return await safe_act(stagehand, page, instruction, **options)
    except BROWSER_ERRORS as error:  # the caller treats this step as best-effort
        log.debug("Optional action failed (%r): %s", instruction, error)
        return None
