"""DO_AUTH_OR_DETAILS: sign-in walls, guest checkout, and contact / passenger / guest details forms.

1. A guest path ('Continue as guest', 'Guest checkout', 'Continue without an account', 'Skip sign in') is taken in
   code when the DOM shows one, else by the model when its reading names one.
2. Otherwise the required details fields are listed (one extraction) and filled from the dummy `persona`. Typed
   details go in as Stagehand `%name%` variables, substituted after the model has answered, so names, email and
   phone never reach a prompt. Only list choices (title, gender,
   country) are named to the model, and none of them identifies anyone.
3. A filled form is submitted with its own continue button (never a payment button: cdp_act brakes on those).
4. A sign-in wall with no guest path pauses a headed, interactive run so the operator can sign in by hand (the
   persistent profile keeps it for next time); otherwise it raises LoginWallBlocked. Accounts are never created.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

from pydantic import BaseModel, Field

from checkout_navigator.config import CHOSEN_PERSONA_FIELDS
from checkout_navigator.engine.cdp_guard import (
    LoginWallBlocked,
    cdp_extract,
    dom_click,
    settle,
    try_cdp_act,
)

log = logging.getLogger(__name__)

MAX_FIELDS = 20
PERSONA_DETAILS = ("title", "first_name", "last_name", "full_name", "gender", "date_of_birth", "email", "phone",
                   "country")  # fmt: skip

JS_GUEST = r"""() => {
    const clean = s => (s || '').replace(/\s+/g, ' ').trim();
    const vis = el => { const r = el.getBoundingClientRect(); return r.width > 4 && r.height > 4; };
    const want = /continue as (a )?guest|guest checkout|check ?out as (a )?guest|book as (a )?guest|checkout without (an )?account|continue without (an account|signing in|logging in|log ?in|sign ?in)|skip (sign ?in|log ?in|login)|^guest$/i;
    const el = Array.from(document.querySelectorAll('button, a, [role="button"], [role="radio"], [role="tab"], label'))
        .filter(vis).find(e => want.test(clean(e.innerText || e.getAttribute('aria-label'))));
    if (!el) return null;
    el.scrollIntoView({ block: 'center' });
    const r = el.getBoundingClientRect();
    return { text: clean(el.innerText || el.getAttribute('aria-label')).slice(0, 80), x: r.left + r.width / 2, y: r.top + r.height / 2 };
}"""


class DetailField(BaseModel):
    label: str = Field(description="The field's label exactly as displayed")
    kind: str | None = Field(default=None, description="One of: text, dropdown, radio, checkbox, date; null if unclear")
    detail: str | None = Field(
        default=None,
        description=f"Which detail belongs in it. One of: {', '.join(PERSONA_DETAILS)}, other; null if unclear. "
        "'Confirm email' is email; 'Mobile' is phone; 'Name' alone is full_name",
    )


class AuthState(BaseModel):
    guest_option: str | None = Field(
        description="Label of a control that continues WITHOUT signing in (e.g. 'Continue as guest', 'Guest "
        "checkout', 'Checkout without an account'); null if none"
    )
    sign_in_required: bool = Field(
        description="True only if the page demands a sign-in or account and offers no guest path and no details form"
    )
    fields: list[DetailField] = Field(
        description="Required contact / passenger / guest fields that are still empty or unselected, in page order. "
        "Exclude passwords, optional fields, loyalty numbers, marketing opt-ins, promo codes and every payment field"
    )
    submit_button: str | None = Field(
        description="Label of the button that submits these details and continues (e.g. 'Continue', 'Next', "
        "'Confirm details'); null if none. Never a payment / place-order button"
    )


QUESTION = (
    "This screen asks for sign-in or customer details. Report any option to continue as a guest, whether a sign-in "
    "is strictly required, the required contact / passenger / guest fields still empty (label, kind, which detail "
    "goes in each), and the button that submits them. Do not suggest values and do not interact with the page."
)


def field_instruction(field: DetailField, persona: dict[str, str]) -> tuple[str, dict[str, str]] | None:
    """The act instruction and Stagehand variables for one field; None if we have nothing for it."""
    if field.detail not in PERSONA_DETAILS or field.detail not in persona:
        return None
    target = f"the '{field.label}' field"
    if field.detail in CHOSEN_PERSONA_FIELDS or field.kind in ("dropdown", "radio"):
        return f"Select '{persona[field.detail]}' for {target}", {}
    return f"Type %{field.detail}% into {target}", {field.detail: persona[field.detail]}


async def pause_for_login(page: Any, interactive: bool) -> bool:
    """Let the operator sign in by hand in a headed browser. False if this run cannot pause."""
    if not interactive or not sys.stdin.isatty():
        return False
    log.warning("\n  [SIGN-IN REQUIRED] The site needs an account and offers no guest checkout.")
    log.warning("  Sign in in the open browser window (it is kept in the persistent profile), then come back.")
    await asyncio.to_thread(input, "  Press Enter here to continue... ")
    await settle(page, 2500)
    return True


async def handle_auth_or_details(
    stagehand: Any, page: Any, persona: dict[str, str], context: dict[str, Any] | None = None
) -> bool:
    """Take the guest path, or fill and submit the required details. True if anything was done."""
    context = context or {}
    hints = context.get("hints") or {}
    timeout = context.get("llm_timeout_ms", 90_000)

    if clicked := await dom_click(page, JS_GUEST, None, hints):
        log.info("  Auth: took the guest path (%r)", clicked.get("text"))
        return True

    state: AuthState = await cdp_extract(stagehand, QUESTION, AuthState, timeout)
    if state.guest_option:
        outcome = await try_cdp_act(stagehand, page, f"Click '{state.guest_option}'", hints=hints, timeout_ms=timeout)
        if outcome and outcome.success:
            log.info("  Auth: %s", outcome.description)
            return True

    did = False
    for field in state.fields[:MAX_FIELDS]:
        planned = field_instruction(field, persona)
        if planned is None:
            log.debug("No persona detail for field %r (%s)", field.label, field.detail)
            continue
        instruction, variables = planned
        outcome = await try_cdp_act(
            stagehand, page, instruction, variables=variables or None, hints=hints, timeout_ms=timeout,
            settle_after=False,
        )  # fmt: skip
        if outcome and outcome.success:
            log.info("  Details: %s", field.label)  # never the value
            did = True
        await settle(page, 400)

    if state.submit_button and (did or not state.fields):
        outcome = await try_cdp_act(
            stagehand, page, f"Click the '{state.submit_button}' button to submit these details and continue",
            hints=hints, timeout_ms=timeout,
        )  # fmt: skip
        if outcome and outcome.success:
            log.info("  Details: submitted with %s", outcome.description)
            return True

    if state.sign_in_required and not did and not state.fields:
        if await pause_for_login(page, bool(context.get("interactive"))):
            return True
        raise LoginWallBlocked("the site requires a sign-in and offers no guest checkout")
    return did
