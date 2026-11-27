"""The loop's two questions: "Are we at checkout?" and "What must we do to get there?".

`is_at_checkout` is deterministic (no model call): it reads payment-processor iframes, card fields and final-payment
buttons from the page and from the observed elements. `determine_next_step` makes ONE structured model call that
returns either a helper route or the exact atomic instruction (and, when it can, the index of the observed element
to act on, so `cdp_act` needs no second model call).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

from checkout_navigator.engine.cdp_guard import Observation, safe_evaluate, with_frame_retry
from checkout_navigator.engine.progress import action_key, is_skip_link

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Are we at checkout?
# ---------------------------------------------------------------------------
PAYMENT_PROCESSORS = re.compile(
    r"stripe\.com|stripe\.network|paypal\.com|paypalobjects\.com|adyen|braintree|afterpay|klarna|checkout\.com"
    r"|squareup\.com|square\.site|zip\.co|eway\.com\.au|cybersource|worldpay|securepay|paymentexpress|windcave"
    r"|__privateStripeFrame",
    re.IGNORECASE,
)
PAYMENT_ACTION = re.compile(
    r"\bplace\s*(my\s*|your\s*)?order\b|\bpay\s*now\b|\bcomplete\s*(my\s*)?purchase\b|\bpay\s*with\s*card\b"
    r"|\bsubmit\s*payment\b|\bconfirm\s*(and|&)\s*pay\b|\bpay\s*\$\s*\d",
    re.IGNORECASE,
)
# Which kind of card field a label names, so card number + expiry + CVV count as three signals, not one each time.
CARD_KINDS = (
    ("card_number", re.compile(r"card\s*number|card\s*no\b|cc-number|cardnumber", re.IGNORECASE)),
    ("expiry", re.compile(r"expir(y|ation)|cc-exp|\bmm\s*/\s*yy\b", re.IGNORECASE)),
    ("cvv", re.compile(r"\bcvv\b|\bcvc\b|\bcsc\b|cc-csc|security\s*code", re.IGNORECASE)),
    ("billing_postcode", re.compile(r"billing\s*(post\s*code|postal|zip)", re.IGNORECASE)),
)


@dataclass(frozen=True)
class CheckoutSignals:
    processor_iframes: tuple[str, ...]
    input_signals: tuple[str, ...]
    action_signals: tuple[str, ...]

    @property
    def at_checkout(self) -> bool:
        """A payment-processor iframe beside a payment button, or two or more input/action signals."""
        if self.processor_iframes and self.action_signals:
            return True
        return len(self.input_signals) + len(self.action_signals) >= 2

    def summary(self) -> str:
        return (
            f"iframes={list(self.processor_iframes)} inputs={list(self.input_signals)} "
            f"actions={list(self.action_signals)}"
        )


# Fresh read of every frame's src/name/title (they are readable from the parent across origins) and of any frame
# nested inside a same-origin frame.
JS_FRAMES = r"""(() => {
    const out = [];
    const walk = (doc, depth) => {
        for (const f of doc.querySelectorAll('iframe, frame')) {
            out.push([f.src || f.getAttribute('src') || '', f.name || '', f.title || ''].join(' '));
            if (depth < 3) { try { if (f.contentDocument) walk(f.contentDocument, depth + 1); } catch (e) {} }
        }
    };
    walk(document, 0);
    return out;
})()"""


def _card_kinds(text: str) -> set[str]:
    return {name for name, pattern in CARD_KINDS if pattern.search(text)}


def checkout_signals(frames: Sequence[str], observed: Observation, url: str = "") -> CheckoutSignals:
    """Pure function over the page's frames, DOM scan and observed elements."""
    processors = {m.group(0).lower() for f in frames if (m := PAYMENT_PROCESSORS.search(f))}
    if (m := PAYMENT_PROCESSORS.search(url)) is not None:
        processors.add(m.group(0).lower())  # a hosted checkout page (checkout.stripe.com, paypal.com/checkoutnow)

    inputs: set[str] = set()
    actions: set[str] = set()
    for frame in frames:  # Stripe/Adyen/Braintree hosted-field iframes are titled after their field
        inputs |= _card_kinds(frame)
    for control in observed.controls:
        label = f"{control.get('label', '')} {control.get('text', '')}"
        if control.get("tag") in ("input", "select", "textarea") or control.get("role") == "combobox":
            inputs |= _card_kinds(label)
        elif (m := PAYMENT_ACTION.search(label)) is not None:
            actions.add(" ".join(m.group(0).lower().split()))
    for action in observed.actions:
        description = str(getattr(action, "description", "") or "")
        method = str(getattr(action, "method", "") or "")
        if method in ("fill", "type"):
            inputs |= _card_kinds(description)
        elif (m := PAYMENT_ACTION.search(description)) is not None:
            actions.add(" ".join(m.group(0).lower().split()))
    return CheckoutSignals(tuple(sorted(processors)), tuple(sorted(inputs)), tuple(sorted(actions)))


async def is_at_checkout(page: Any, observed_elements: Observation) -> bool:
    """True once the page is the terminal payment wall."""
    frames = await safe_evaluate(page, JS_FRAMES, default=[])
    if not isinstance(frames, list):
        frames = []
    frames = [str(f) for f in frames] + [
        " ".join(str(f.get(k, "")) for k in ("src", "name", "title"))
        for f in observed_elements.dom.get("iframes") or []
    ]
    signals = checkout_signals(frames, observed_elements, observed_elements.url)
    if signals.at_checkout:
        log.info("  Checkout reached: %s", signals.summary())
    elif signals.processor_iframes or signals.input_signals or signals.action_signals:
        log.info("  Payment signals, not enough yet: %s", signals.summary())
    return signals.at_checkout


# ---------------------------------------------------------------------------
# What must we do to get there?
# ---------------------------------------------------------------------------
Route = Literal["DO_DISMISS_OVERLAY", "DO_LOCATION", "DO_CALENDAR", "DO_OPTIONS", "DO_AUTH_OR_DETAILS", "DO_ACT"]
ROUTES: tuple[str, ...] = Route.__args__  # type: ignore[attr-defined]


class NextActionModel(BaseModel):
    """The schema the model fills in: one decision per step."""

    route: Route = Field(
        description="DO_DISMISS_OVERLAY: a cookie banner, promo/newsletter popup, app banner, region selector or age "
        "gate covers or blocks the page. DO_LOCATION: a delivery-address modal or prompt, OR a landing-page search "
        "bar (origin/destination, city/destination, suburb) must be filled before products, fares or listings are "
        "shown. DO_CALENDAR: a date, departure/return or check-in/check-out picker must be filled. DO_OPTIONS: a "
        "required choice (size, side, variant, fare bundle, ticket tier, add-on decline, insurance yes/no) blocks "
        "progress. DO_AUTH_OR_DETAILS: a sign-in wall, a 'continue as guest' choice, or a required contact / "
        "passenger / guest details form. DO_ACT: none of these; one plain action (usually a click) moves on."
    )
    target_index: int | None = Field(
        default=None,
        description="For DO_ACT: the [index] of the observed element to act on, if one of them is exactly right; "
        "otherwise null",
    )
    instruction: str | None = Field(
        default=None,
        description="For DO_ACT (required): ONE atomic action in imperative form naming the exact element, e.g. "
        "\"Click the 'Continue to seat selection' button\" or \"Click the 'Add to cart' button\". Never a payment, "
        "place-order or submit-payment button. Do not mention buttons you are NOT clicking. Null for other routes",
    )
    blocked_reason: str | None = Field(
        default=None,
        description="Only if a CAPTCHA, 'verify you are human', access-denied or bot-check page blocks all progress: "
        "describe it. Otherwise null",
    )
    reason: str = Field(description="One short sentence: what the screen shows and why this is the next step")


@dataclass(frozen=True)
class NextAction:
    route: str
    instruction: str | None = None
    target: Any = None  # the observed stagehand Action to act on directly, if the model chose one
    blocked_reason: str | None = None
    reason: str = ""


DECISION_PROMPT = (
    "GOAL: move this purchase or booking flow forward until the PAYMENT step is showing (card/payment details "
    "requested), choosing the cheapest/default option at every choice, as a guest, with no optional extras. "
    "We stop as soon as the payment step shows, so never choose to pay, place, submit or confirm the order.\n"
    "If the store or listing page shows products, pick the first available reasonably priced item and add one "
    "to the cart; then go to the cart and checkout. On a search results page pick the first/cheapest bookable "
    "result. If a search or booking form is already filled in, click its submit button (Search, Find, Continue) "
    "rather than refilling it. Inline error or info messages are not overlays. Ignore 'Skip to ...' links.\n"
    "Choose the single next step. Prefer a helper route whenever its widget is what blocks progress; use DO_ACT "
    "only for one plain click.\n\n"
    "Current URL: {url}\nPage headings: {headings}\nOverlay visible: {overlay}\n"
    "Form fields on the page:\n{fields}\nVisible buttons: {buttons}\n"
    "Observed elements (use their [index] as target_index):\n{elements}\n\n"
    "Recent steps (do not repeat one that changed nothing):\n{history}{avoid}"
)

# Fields whose value is shopper data: shown as "(filled)", never sent to the model.
PERSONAL_FIELD = re.compile(
    r"e-?mail|phone|mobile|\btel\b|password|passcode|\bname\b|first|last|surname|given|family|birth|\bdob\b"
    r"|card|cvv|cvc|expir|postcode|post code|zip|address|street|passport",
    re.IGNORECASE,
)
DATE_OR_PICKER = re.compile(
    r"\bdates?\b|calendar|picker|depart|return|check-?in|check-?out|when\b|travel"
    r"|\b\d{1,2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b"
    r"|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}\b|\d{1,2}/\d{1,2}(/\d{2,4})?",
    re.IGNORECASE,
)
FIELD_TYPES_SKIPPED = frozenset({"checkbox", "radio", "submit", "button", "reset", "image", "file", "hidden"})
BUTTON_TYPES = frozenset({"submit", "button"})


def _field_line(control: dict[str, Any]) -> str | None:
    tag, role, kind = control.get("tag", ""), control.get("role", ""), (control.get("type") or "").lower()
    label = str(control.get("label") or "").split(" | ")[0][:50] or str(control.get("text") or "")[:50]
    if (tag in ("input", "textarea") and kind not in FIELD_TYPES_SKIPPED) or tag == "select" or role == "combobox":
        value = str(control.get("value") or control.get("text") or "")
        if value and (kind in ("email", "tel", "password") or PERSONAL_FIELD.search(str(control.get("label") or ""))):
            value = "(filled)"
        kind_name = role or (tag if tag != "input" else kind or "text")
    elif tag == "button" or role == "button":
        text = str(control.get("text") or "")
        if not DATE_OR_PICKER.search(f"{text} {control.get('label', '')}"):
            return None
        value, kind_name = text, "picker button"
    else:
        return None
    flags = " *required" if control.get("required") else ""
    flags += " (disabled)" if control.get("disabled") else ""
    return f"- {label or '(unlabelled)'} [{kind_name}] = {value[:40] or '(empty)'}{flags}"


def form_summary(controls: Sequence[dict[str, Any]], max_chars: int = 900) -> str:
    """Visible inputs, comboboxes, selects and date/picker buttons as 'label [kind] = value', capped in length."""
    lines: list[str] = []
    seen: set[str] = set()
    for control in controls:
        line = _field_line(control)
        if line is None or line in seen:
            continue
        seen.add(line)
        if sum(len(x) + 1 for x in lines) + len(line) > max_chars:
            lines.append("- ...")
            break
        lines.append(line)
    return "\n".join(lines) or "(none)"


def button_summary(controls: Sequence[dict[str, Any]], limit: int = 12, max_chars: int = 400) -> str:
    """The visible, enabled buttons' texts, de-duplicated, in page order."""
    names: list[str] = []
    for control in controls:
        is_button = (
            control.get("tag") == "button"
            or control.get("role") == "button"
            or (control.get("tag") == "input" and (control.get("type") or "").lower() in BUTTON_TYPES)
        )
        text = str(control.get("text") or control.get("value") or control.get("label") or "").split(" | ")[0][:40]
        if not is_button or control.get("disabled") or not text or is_skip_link(text) or text in names:
            continue
        names.append(text)
        if len(names) >= limit:
            break
    out = "; ".join(f"'{n}'" for n in names)
    return (out[:max_chars] + " ...") if len(out) > max_chars else (out or "(none)")


def _decision_prompt(
    observed: Observation, history: Sequence[str], avoid: Sequence[str] = (), overlay_false_alarm: bool = False
) -> str:
    overlay = bool(observed.dom.get("overlay")) and not overlay_false_alarm
    overlay_text = "False"
    if overlay:
        matches = [str(m) for m in observed.dom.get("overlay_matches") or []][:2]
        overlay_text = "True" + (f" ({'; '.join(matches)})" if matches else "")
    avoid_text = ""
    if avoid:
        avoid_text = "\nAvoid (already tried on this exact screen with no effect): " + "; ".join(avoid)
    return DECISION_PROMPT.format(
        url=observed.url,
        headings="; ".join(observed.dom.get("headings") or []) or "(none)",
        overlay=overlay_text,
        fields=form_summary(observed.controls),
        buttons=button_summary(observed.controls),
        elements=observed.describe(),
        history="\n".join(f"- {h}" for h in history[-6:]) or "(none yet)",
        avoid=avoid_text,
    )


async def _ask(stagehand: Any, prompt: str, timeout_ms: int) -> NextActionModel:
    result = await with_frame_retry(
        lambda: stagehand.extract(prompt, NextActionModel, timeout=timeout_ms), "decide next step"
    )
    decision: NextActionModel = result.data
    return decision


def _key(decision: NextActionModel, observed: Observation) -> str:
    instruction = decision.instruction
    index = decision.target_index if decision.target_index is not None else -1
    if decision.route == "DO_ACT" and not instruction and 0 <= index < len(observed.actions):
        instruction = f"Click {getattr(observed.actions[index], 'description', '')}"
    return action_key(decision.route, instruction)


def _unwanted(decision: NextActionModel, observed: Observation, avoid: Sequence[str], no_overlay: bool) -> str | None:
    """Why a decision must not run as given (a banned action, a known false overlay, a skip link), else None."""
    if decision.blocked_reason:
        return None
    if _key(decision, observed) in avoid:
        return f"{_key(decision, observed)} already had no effect on this screen"
    if no_overlay and decision.route == "DO_DISMISS_OVERLAY":
        return "the overlay helper already found nothing to close on this screen; it is unobstructed"
    if decision.route == "DO_ACT" and is_skip_link(decision.instruction):
        return "'Skip to ...' links do not move the flow forward"
    return None


async def determine_next_step(
    stagehand: Any,
    observed_elements: Observation,
    history: Sequence[str] = (),
    timeout_ms: int = 90_000,
    *,
    avoid: Sequence[str] = (),
    overlay_false_alarm: bool = False,
) -> NextAction:
    """One extract() call that returns the route (and, for DO_ACT, the exact instruction and target).

    `avoid` lists action keys (see `progress.action_key`) that already had no effect on this screen; with
    `overlay_false_alarm` the overlay flag is known to be wrong here. A decision that picks one of those anyway is
    re-asked once, then replaced by the fallback.
    """
    avoid = list(avoid)
    no_overlay = overlay_false_alarm or "DO_DISMISS_OVERLAY" in avoid
    prompt = _decision_prompt(observed_elements, history, avoid, overlay_false_alarm)
    try:
        decision = await _ask(stagehand, prompt, timeout_ms)
        if (why := _unwanted(decision, observed_elements, avoid, no_overlay)) is not None:
            log.info("  Decision %s rejected (%s); asking again", decision.route, why)
            decision = await _ask(
                stagehand, f"{prompt}\nYour previous answer was rejected: {why}. Choose a different step.", timeout_ms
            )
            if (why := _unwanted(decision, observed_elements, avoid, no_overlay)) is not None:
                return _fallback(observed_elements, f"model kept choosing a rejected step: {why}", avoid, no_overlay)
    except Exception as error:  # noqa: BLE001 - any failed decision falls back to a safe default step
        log.warning("  Decision call failed (%s); falling back to the first observed element", str(error)[:160])
        return _fallback(observed_elements, f"decision failed: {error}", avoid, no_overlay)

    target = None
    if decision.route == "DO_ACT":
        index = decision.target_index
        if index is not None and 0 <= index < len(observed_elements.actions):
            target = observed_elements.actions[index]
        if not decision.instruction:
            if target is None:
                return _fallback(observed_elements, decision.reason, avoid, no_overlay)
            decision.instruction = f"Click {getattr(target, 'description', 'the element')}"
    return NextAction(
        route=decision.route,
        instruction=decision.instruction,
        target=target,
        blocked_reason=decision.blocked_reason,
        reason=decision.reason,
    )


def _fallback(
    observed: Observation, reason: str, avoid: Sequence[str] = (), overlay_false_alarm: bool = False
) -> NextAction:
    """Without a usable decision: dismiss a (real) overlay if one is up, else act on the first observed element
    that has not already failed on this screen."""
    if observed.dom.get("overlay") and not overlay_false_alarm and "DO_DISMISS_OVERLAY" not in avoid:
        return NextAction(route="DO_DISMISS_OVERLAY", reason=reason)
    for action in observed.actions:
        instruction = f"Click {getattr(action, 'description', 'the element')}"
        if action_key("DO_ACT", instruction) in avoid or is_skip_link(instruction):
            continue
        return NextAction(route="DO_ACT", instruction=instruction, target=action, reason=reason)
    return NextAction(
        route="DO_ACT", instruction="Click the primary button that continues to the next step", reason=reason
    )
