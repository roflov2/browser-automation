"""DO_OPTIONS: required selections (sizes, sides, variants, fare bundles, ticket tiers, quantities) and add-ons.

1. In code, no model call: every radio group with nothing chosen gets its free / default / cheapest option (the
   legacy Uber Eats `JS_SELECT_REQUIRED`, generalised: a 'No' / 'No thanks' / '$0' / 'Included' / 'Standard' label
   wins, else the first option without a '+$' surcharge), and every empty required native <select> its first real
   option. Payment-method groups are never touched.
2. One extraction finds what is still unmet, the optional extras to decline, and the confirm button.
3. Safety-checked acts choose each unmet option, decline each extra, then click the confirm button.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from checkout_navigator.engine.cdp_guard import cdp_extract, safe_evaluate, settle, try_cdp_act

log = logging.getLogger(__name__)

MAX_GROUPS = 4
MAX_DECLINES = 3

JS_SELECT_DEFAULTS = r"""(() => {
    const clean = s => (s || '').replace(/\s+/g, ' ').trim();
    const vis = el => { const r = el.getBoundingClientRect(); return r.width > 2 && r.height > 2; };
    const dialogs = Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"]'))
        .filter(d => vis(d) && d.getBoundingClientRect().height > 200);
    const root = dialogs[dialogs.length - 1] || document.body;
    const PAYMENT = /card|paypal|afterpay|klarna|zip ?pay|apple ?pay|google ?pay|bank|payment|gift/i;
    const FREE = /^(no\b|no,|no thanks|none|not now|decline|skip|i don'?t)|\$\s*0(\.00)?\b|\bfree\b|\bincluded\b|\bstandard\b|\bregular\b|\bsmall\b/i;
    const SURCHARGE = /\+\s*(A?\$|AUD|£|€)\s*\d/i;
    const labelOf = r => clean(txt(r.closest('label') || r.parentElement?.parentElement || r.parentElement || r));
    const txt = el => (el && el.innerText) || '';
    const chosen = [];
    const groups = new Map();
    for (const r of root.querySelectorAll('input[type="radio"]')) {
        const key = r.name || r.closest('[role="radiogroup"]')?.id || '';
        if (!key) continue;
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(r);
    }
    for (const list of groups.values()) {
        if (list.some(r => r.checked) || list.every(r => r.disabled)) continue;
        const groupText = clean(txt(list[0].closest('fieldset, [role="radiogroup"]') || list[0].closest('div')?.parentElement));
        if (PAYMENT.test(groupText.slice(0, 300))) continue;
        const usable = list.filter(r => !r.disabled);
        const best = usable.find(r => FREE.test(labelOf(r))) || usable.find(r => !SURCHARGE.test(labelOf(r))) || usable[0];
        const target = best.closest('label') || best;
        target.scrollIntoView({ block: 'center', behavior: 'instant' });
        target.click();
        chosen.push(labelOf(best).split('\n')[0].slice(0, 60));
    }
    for (const s of root.querySelectorAll('select')) {
        if (!vis(s) || s.disabled || PAYMENT.test(clean(s.name + ' ' + s.id + ' ' + (s.getAttribute('aria-label') || '')))) continue;
        const empty = !s.value || s.selectedIndex <= 0 && /select|choose|please|--/i.test(clean(s.options[0]?.text));
        if (!empty || !(s.required || s.getAttribute('aria-required') === 'true' || /quantity|qty|size|tickets?/i.test(s.name + s.id))) continue;
        const opt = Array.from(s.options).find((o, i) => i > 0 && o.value && !o.disabled && o.value !== '0');
        if (!opt) continue;
        s.value = opt.value;
        s.dispatchEvent(new Event('input', { bubbles: true }));
        s.dispatchEvent(new Event('change', { bubbles: true }));
        chosen.push(`${s.name || s.id || 'select'}=${clean(opt.text).slice(0, 40)}`);
    }
    return chosen;
})()"""


class RequiredGroup(BaseModel):
    name: str = Field(description="The heading of the required choice, e.g. 'Choose your size', 'Select a fare'")
    option: str = Field(
        description="The option to choose: the free, default, smallest or cheapest one (for a quantity, 1); "
        "for travel insurance or protection, the 'No' / decline option"
    )


class OptionsState(BaseModel):
    unmet_required: list[RequiredGroup] = Field(
        description="Required choices on screen that still have nothing selected (marked Required, 'Choose 1', or "
        "that block the Continue/Add button). Empty if none"
    )
    declines: list[str] = Field(
        description="Labels of controls that decline an optional paid extra currently offered (e.g. 'No thanks', "
        "'I don't want insurance', 'Skip seats', 'Continue without bags'). Empty if none"
    )
    confirm_button: str | None = Field(
        description="The label of the primary button that confirms these selections and moves on (e.g. 'Add to "
        "cart', 'Add 1 to order', 'Select', 'Continue', 'Next'); null if none. Never a payment button"
    )


QUESTION = (
    "Look at the choices on this screen (item options, sizes, sides, variants, fare bundles, ticket types, "
    "quantities, optional extras, insurance). Report which required choices are still unmet and what to choose for "
    "each (free/default/cheapest, quantity 1, decline insurance), which controls decline optional paid extras, and "
    "the button that confirms and moves on. Do not interact with the page."
)


async def handle_options(stagehand: Any, page: Any, context: dict[str, Any] | None = None) -> bool:
    """Satisfy required choices with the cheapest/default option, decline extras, confirm. True if anything was done."""
    context = context or {}
    hints = context.get("hints") or {}
    timeout = context.get("llm_timeout_ms", 90_000)
    did = False

    picked = await safe_evaluate(page, JS_SELECT_DEFAULTS, default=[])
    if picked:
        log.info("  Options: chose in code %s", ", ".join(map(str, picked)))
        await settle(page, 800)
        did = True

    state: OptionsState = await cdp_extract(stagehand, QUESTION, OptionsState, timeout)
    for group in state.unmet_required[:MAX_GROUPS]:
        outcome = await try_cdp_act(
            stagehand, page, f"In the required section '{group.name}', select the option '{group.option}'",
            hints=hints, timeout_ms=timeout,
        )  # fmt: skip
        if outcome and outcome.success:
            log.info("  Options: %s -> %s", group.name, outcome.description)
            did = True
    for label in state.declines[:MAX_DECLINES]:
        outcome = await try_cdp_act(
            stagehand, page, f"Click '{label}' to decline the optional extra", hints=hints, timeout_ms=timeout
        )
        if outcome and outcome.success:
            log.info("  Options: declined via %s", outcome.description)
            did = True
    if state.confirm_button:
        outcome = await try_cdp_act(
            stagehand, page, f"Click the '{state.confirm_button}' button", hints=hints, timeout_ms=timeout
        )
        if outcome and outcome.success:
            log.info("  Options: confirmed with %s", outcome.description)
            did = True
    return did
