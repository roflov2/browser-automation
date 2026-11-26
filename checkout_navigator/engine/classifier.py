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
    "result.\n"
    "Choose the single next step. Prefer a helper route whenever its widget is what blocks progress; use DO_ACT "
    "only for one plain click.\n\n"
    "Current URL: {url}\nPage headings: {headings}\nOverlay visible: {overlay}\n"
    "Observed elements (use their [index] as target_index):\n{elements}\n\n"
    "Recent steps (do not repeat one that changed nothing):\n{history}"
)


def _decision_prompt(observed: Observation, history: Sequence[str]) -> str:
    return DECISION_PROMPT.format(
        url=observed.url,
        headings="; ".join(observed.dom.get("headings") or []) or "(none)",
        overlay=bool(observed.dom.get("overlay")),
        elements=observed.describe(),
        history="\n".join(f"- {h}" for h in history[-6:]) or "(none yet)",
    )


async def determine_next_step(
    stagehand: Any, observed_elements: Observation, history: Sequence[str] = (), timeout_ms: int = 90_000
) -> NextAction:
    """One extract() call that returns the route (and, for DO_ACT, the exact instruction and target)."""
    prompt = _decision_prompt(observed_elements, history)
    try:
        result = await with_frame_retry(
            lambda: stagehand.extract(prompt, NextActionModel, timeout=timeout_ms), "decide next step"
        )
        decision: NextActionModel = result.data
    except Exception as error:  # noqa: BLE001 - any failed decision falls back to a safe default step
        log.warning("  Decision call failed (%s); falling back to the first observed element", str(error)[:160])
        return _fallback(observed_elements, f"decision failed: {error}")

    target = None
    if decision.route == "DO_ACT":
        index = decision.target_index
        if index is not None and 0 <= index < len(observed_elements.actions):
            target = observed_elements.actions[index]
        if not decision.instruction:
            if target is None:
                return _fallback(observed_elements, decision.reason)
            decision.instruction = f"Click {getattr(target, 'description', 'the element')}"
    return NextAction(
        route=decision.route,
        instruction=decision.instruction,
        target=target,
        blocked_reason=decision.blocked_reason,
        reason=decision.reason,
    )


def _fallback(observed: Observation, reason: str) -> NextAction:
    """Without a decision: dismiss an overlay if one is up, else act on the first observed element."""
    if observed.dom.get("overlay"):
        return NextAction(route="DO_DISMISS_OVERLAY", reason=reason)
    if observed.actions:
        first = observed.actions[0]
        return NextAction(
            route="DO_ACT", instruction=f"Click {getattr(first, 'description', 'the element')}", target=first,
            reason=reason,
        )  # fmt: skip
    return NextAction(
        route="DO_ACT", instruction="Click the primary button that continues to the next step", reason=reason
    )
