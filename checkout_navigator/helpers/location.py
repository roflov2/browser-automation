"""DO_LOCATION: delivery-address modals and landing-page search bars (origin/destination, city, suburb).

Values come from the config (`default_address`, `search_defaults`). Each field is typed through a Stagehand
`%variable%` act; React/Angular type-aheads that ignore a programmatic fill get real, key-by-key presses instead.
The first matching autocomplete suggestion is then clicked, found in the DOM first and by the model only if the DOM
finds none.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from checkout_navigator.engine.cdp_guard import (
    cdp_act,
    cdp_extract,
    guarded_click_point,
    press_key,
    safe_evaluate,
    scan_dom,
    settle,
    try_cdp_act,
)

log = logging.getLogger(__name__)

SUGGESTION_WAIT_S = 8.0  # type-aheads fetch suggestions after typing; they take a second or two
SUGGESTION_POLL_S = 0.25
TYPE_DELAY_MS = 80  # between key presses, so the type-ahead sees each one
MAX_FIELDS = 4


class LocationField(BaseModel):
    label: str = Field(description="The field's label or placeholder exactly as displayed")
    role: Literal["address", "origin", "destination", "city", "suburb", "other"] = Field(
        description="address: a street/delivery address; origin: 'From' / departure; destination: 'To' / arrival / "
        "'Where to?'; city: a city or destination search for stays/events; suburb: a suburb or postcode; other"
    )
    filled_correctly: bool = Field(
        description="True if the field already shows a real value (not a placeholder) that suits the search"
    )


class LocationForm(BaseModel):
    kind: Literal["delivery_address", "travel_search", "city_search", "none"] = Field(
        description="delivery_address: a delivery address modal/prompt/input; travel_search: origin and destination "
        "fields (flights, trains, buses); city_search: a single destination/city/location search (hotels, events, "
        "stores); none: no location input is visible"
    )
    fields: list[LocationField] = Field(description="The visible location inputs, in page order")
    opener: str | None = Field(
        default=None,
        description="If no location input is visible yet but a button opens one (e.g. 'Enter delivery address', "
        "the current address in the header, 'Where to?'), its label; else null",
    )


FORM_QUESTION = (
    "Which location inputs block progress on this screen (a delivery address prompt, or a search bar with from/to, "
    "destination or city fields)? List each visible one with its label and role. Do not interact with the page."
)

# The visible autocomplete suggestion that best matches the typed value: inside the list the focused field
# controls (aria-controls / aria-owns), else any visible listbox / suggestion list (Google Places .pac-item included).
JS_SUGGESTION = r"""({ tokens, code }) => {
    const clean = s => (s || '').replace(/\s+/g, ' ').trim();
    const vis = el => { const r = el.getBoundingClientRect(); return r.width > 4 && r.height > 4
        && getComputedStyle(el).visibility !== 'hidden'; };
    const sel = '[role="option"], [role="listbox"] li, .pac-item, [class*="suggestion" i] li, '
        + '[class*="suggestion" i] [role="button"], [class*="autocomplete" i] li, [id*="autocomplete" i] li, '
        + '[class*="typeahead" i] li, [class*="results" i] [role="option"]';
    const active = document.activeElement;
    const ids = [active].filter(Boolean).map(e => e.getAttribute('aria-controls') || e.getAttribute('aria-owns')).filter(Boolean);
    const roots = ids.map(id => document.getElementById(id)).filter(Boolean);
    let options = [];
    for (const root of [...roots, document]) {
        options = Array.from(root.querySelectorAll(sel)).filter(el => vis(el) && clean(el.innerText));
        if (options.length) break;
    }
    if (!options.length) return null;
    const score = el => {
        const t = clean(el.innerText);
        let s = 0;
        if (code && new RegExp(`(^|[^A-Za-z])${code}([^A-Za-z]|$)`).test(t)) s += 10;
        for (const tok of tokens) if (t.toLowerCase().includes(tok)) s += 1;
        return s;
    };
    const best = options.map((el, i) => ({ el, s: score(el) - i * 0.01 })).sort((a, b) => b.s - a.s)[0].el;
    best.scrollIntoView({ block: 'nearest' });
    const r = best.getBoundingClientRect();
    return { text: clean(best.innerText).slice(0, 120), x: r.left + r.width / 2, y: r.top + r.height / 2 };
}"""
# Select whatever the focused text field holds so a Backspace clears it.
JS_SELECT_FOCUSED = r"""(() => {
    const el = document.activeElement;
    if (!el || !(el.matches('input, textarea') || el.isContentEditable)) return false;
    if (el.select) el.select();
    return true;
})()"""


def _value_for(field: LocationField, kind: str, context: dict[str, Any]) -> str | None:
    search = context.get("search_defaults") or {}
    if field.role in ("address", "suburb"):
        return str(context.get("default_address") or "")
    if field.role == "origin":
        return str(search.get("origin") or "")
    if field.role == "destination":
        return str(search.get("city") if kind == "city_search" else search.get("destination") or "")
    if field.role == "city":
        return str(search.get("city") or "")
    return None


def _match_tokens(value: str) -> dict[str, Any]:
    """What a good suggestion contains: an airport/station code as an upper-case word, or the value's words."""
    code = value.upper() if re.fullmatch(r"[A-Za-z]{3}", value) else None
    tokens = [t.lower() for t in re.findall(r"[A-Za-z]{3,}", value)][:4]
    return {"tokens": tokens, "code": code}


async def wait_for_suggestion(page: Any, value: str, seconds: float = SUGGESTION_WAIT_S) -> dict[str, Any] | None:
    arg = json.dumps(_match_tokens(value))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while True:
        found = await safe_evaluate(page, f"({JS_SUGGESTION})({arg})", default=None)
        if isinstance(found, dict):
            await asyncio.sleep(SUGGESTION_POLL_S)  # let the list finish rendering before clicking
            again = await safe_evaluate(page, f"({JS_SUGGESTION})({arg})", default=None)
            return again if isinstance(again, dict) else found
        if loop.time() >= deadline:
            return None
        await asyncio.sleep(SUGGESTION_POLL_S)


async def _type_with_keys(stagehand: Any, page: Any, label: str, value: str, context: dict[str, Any]) -> None:
    """Focus the field (one act), clear it, and type real key presses."""
    await cdp_act(stagehand, page, f"Click the '{label}' input field to focus it", hints=context.get("hints"),
                  timeout_ms=context.get("llm_timeout_ms", 90_000), settle_after=False)  # fmt: skip
    await safe_evaluate(page, JS_SELECT_FOCUSED, default=False)
    await press_key(page, "Backspace")
    await page.type(value, delay=TYPE_DELAY_MS)


async def fill_location_field(
    stagehand: Any, page: Any, field: LocationField, value: str, context: dict[str, Any]
) -> bool:
    """Type `value` into the field and pick the first matching suggestion. True if a suggestion was chosen."""
    hints = context.get("hints") or {}
    timeout = context.get("llm_timeout_ms", 90_000)
    key = {"address": "address", "suburb": "address"}.get(field.role, field.role)
    await cdp_act(
        stagehand, page, f"Clear the '{field.label}' field and type %{key}% into it",
        variables={key: value}, hints=hints, timeout_ms=timeout, settle_after=False,
    )  # fmt: skip
    suggestion = await wait_for_suggestion(page, value, seconds=3.0)
    if suggestion is None:  # a type-ahead that ignored the programmatic fill: type real key presses
        log.info("  Location: no suggestions after a fill; typing key presses into '%s'", field.label)
        await _type_with_keys(stagehand, page, field.label, value, context)
        suggestion = await wait_for_suggestion(page, value)
    if suggestion is not None:
        await guarded_click_point(page, suggestion, hints)
        log.info("  Location: %s -> picked suggestion %r", field.label, suggestion.get("text"))
        await settle(page, int(hints.get("settle_ms") or 1500))
        return True
    outcome = await try_cdp_act(
        stagehand, page, f"Click the first suggestion in the open autocomplete list below the '{field.label}' field",
        hints=hints, timeout_ms=timeout,
    )  # fmt: skip
    log.info("  Location: %s -> %s", field.label, outcome.description if outcome else "no suggestion list")
    return bool(outcome and outcome.success)


async def handle_location(stagehand: Any, page: Any, context: dict[str, Any]) -> bool:
    """Fill every empty location input on screen from the defaults. True if anything was done."""
    hints = context.get("hints") or {}
    timeout = context.get("llm_timeout_ms", 90_000)
    form: LocationForm = await cdp_extract(stagehand, FORM_QUESTION, LocationForm, timeout)
    if not form.fields and form.opener:
        await cdp_act(stagehand, page, f"Click '{form.opener}' to open the location input", hints=hints,
                      timeout_ms=timeout)  # fmt: skip
        form = await cdp_extract(stagehand, FORM_QUESTION, LocationForm, timeout)

    did = False
    for field in [f for f in form.fields if not f.filled_correctly][:MAX_FIELDS]:
        value = _value_for(field, form.kind, context)
        if not value:
            log.debug("No default for location field %r (%s)", field.label, field.role)
            continue
        await fill_location_field(stagehand, page, field, value, context)
        did = True

    if form.kind == "delivery_address" and did and (await scan_dom(page)).get("dialogs"):
        outcome = await try_cdp_act(
            stagehand, page,
            "In the open address dialog, click the button that confirms the address (e.g. 'Save', 'Done', 'Confirm "
            "address', 'Deliver here', 'Continue'). If it asks for a building type or drop-off option instead, click "
            "'Skip' or 'Not now', or choose the first option and save. Never edit or delete a saved address.",
            hints=hints, timeout_ms=timeout,
        )  # fmt: skip
        if outcome:
            log.info("  Location: %s", outcome.description)
    return did
