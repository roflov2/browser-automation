"""Qantas flight booking.

Start on the Qantas homepage, search for a one-way flight, pick the cheapest flight and fare, then
carry on until the payment page. The prompts state the goal (reach payment with the cheapest fare and
no optional extras) and the rules, not the route through the site: the model decides which control
moves the booking on, so a redesigned page or a reordered booking chain does not break the run.
Every optional extra and fee offered along the way (baggage, seats, insurance, carbon offset, service
and card fees, ...) is recorded, and the advertised fare is compared with the total at checkout.

The run always stops on the payment page: no payment details are entered and nothing is purchased.
"""

import argparse
import contextlib
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from pydantic import BaseModel, Field
from stagehand import Page, Stagehand

from ..actions import click_at, llm_act
from ..config import Settings
from ..core import (
    BROWSER_ERRORS,
    Blocked,
    Fee,
    FlowChanged,
    Report,
    SiteResult,
    Status,
    compare_item_prices,
    diff,
    drip_outcome,
    drip_verdict,
    final_price_was_read,
    money_sum,
    new_item,
    parse_amount,
    parse_price,
    positive,
    record_offers,
    slug,
    summarise_extras,
    write_csv,
)
from ..page import dismiss_popups, fingerprint, js, settle, shot, wait_for_render
from ..workflow import Workflow

log = logging.getLogger(__name__)

SITE_URL = "https://www.qantas.com/au/en.html"
SETTLE_MS = 2500  # fixed wait after navigation/clicks; airline booking apps are slow single-page apps
RESULTS_RENDER_SECONDS = 30  # flight results often show a blank or loading page for a while first
NO_RESEARCH = (
    "The flight search is already done once flight results are showing: never edit, modify or restart the "
    "search from then on."
)
MAX_SEARCH_STEPS = 14
MAX_BOOKING_STEPS = 25
MAX_STUCK_REPEATS = 3
# Paging the date picker on with the same arrow is progress the search state cannot show (the date stays empty
# until a day is picked), so it gets a year of months before it counts as stuck.
MAX_MONTH_REPEATS = 12
MONTH_NAVIGATION = re.compile(
    r"\b(next|previous|prev|forward|back|later|earlier)\b.{0,20}\bmonths?\b|\bmonths?\b.{0,20}\b(next|previous|forward|back)\b",
    re.IGNORECASE,
)
TRIP_SETTLE_MS = 800  # the trip-type list opens and closes in place; no navigation to wait for
AIRPORT_SETTLE_MS = 1000  # the airport type-ahead fetches its suggestions after the code is typed
TYPE_DELAY_MS = 80  # between key presses, so the type-ahead sees each one
# A marketing page ("Flights from Sydney to Ballina": fare deals, no flights) the search can land on.
DEALS_PAGE = re.compile(r"/flight-deals/|/deals?/", re.IGNORECASE)
MAX_WRONG_RESULTS = 2  # a second wrong "results" page ends the search
POPUPS = "cookie banner, promotional/newsletter popup, app-install banner or region selector modal"

SAFETY_PROMPT = (
    "You are auditing an airline booking flow for price transparency. Never enter payment card, voucher, "
    "gift card, travel credit or frequent-flyer points details, and never click a button that pays for, purchases or confirms a booking "
    "(e.g. 'Pay now', 'Purchase', 'Confirm and pay', 'Book now' on a payment page). Never log in or create an account."
)

STEP_TYPES = (
    "flight_results, fare_selection, login_prompt, passenger_details, seats, baggage, extras, insurance, "
    "review, payment, confirmation, upsell_modal, error, other"
)
EXTRA_CATEGORIES = (
    "checked_baggage, carry_on_baggage, seat_selection, fare_upgrade, flexibility, travel_insurance, carbon_offset, "
    "lounge, meal, priority_boarding, car_hire, hotel, charity_donation, points_or_status, service_fee, "
    "payment_surcharge, other"
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class SearchState(BaseModel):
    on_results_page: bool = Field(
        description="True only if the page now lists bookable flights with times and prices for the searched route and date"
    )
    loading: bool = Field(
        default=False,
        description="True only for a loading spinner, a 'searching for flights' message or a blank results page; "
        "never for the home page or its flight search form",
    )
    form_visible: bool = Field(description="True if the flight search form/widget is visible")
    trip_type: str | None = Field(description="'one_way', 'return' or 'multi_city' as currently set, if shown")
    origin: str | None = Field(
        description="Departure airport shown in the search form's 'from' field, e.g. 'Sydney (SYD)'; null if "
        "the field is empty or only shows its placeholder"
    )
    destination: str | None = Field(
        description="Arrival airport shown in the search form's 'to' field; null if the field is empty or only "
        "shows its placeholder"
    )
    depart_date: str | None = Field(
        description="Departure date shown in the search form's date field; null if no date has been picked yet"
    )
    guests: str | None = Field(description="The text on the passengers/guests button, e.g. '1 Adult'")
    using_points: bool = Field(
        description="True if booking with frequent-flyer points (a reward / points search) is switched on"
    )
    blocked_reason: str | None = Field(
        description="If progress is blocked by a CAPTCHA, 'verify you are human', access-denied or bot check, describe it; otherwise null"
    )
    reason: str | None = Field(
        default=None, description="One short sentence explaining this reading of the screen; null if unsure"
    )


class FareOption(BaseModel):
    fare_name: str = Field(description="Fare brand as displayed, e.g. 'Economy Sale', 'Economy Flex', 'Business'")
    price: str | None = Field(description="Price as displayed, e.g. '$129'; null if unavailable/sold out")


class Flight(BaseModel):
    flight_number: str | None = Field(description="e.g. 'QF 401'; null if not shown")
    departs: str | None = Field(
        default=None, description="Departure time as displayed, e.g. '6:00am'; null if not shown"
    )
    arrives: str | None = Field(default=None, description="Arrival time as displayed; null if not shown")
    stops: int | None = Field(default=None, description="Number of stops; 0 for a direct flight; null if not shown")
    duration: str | None
    price: str | None = Field(description="Lowest price displayed for this flight, e.g. 'From $129'; null if sold out")
    sold_out: bool
    fares: list[FareOption] = Field(description="Per-fare prices if they are visible for this flight; otherwise empty")


class Flights(BaseModel):
    route: str | None = Field(description="Route and date shown on the page, e.g. 'Sydney to Melbourne, Mon 14 Dec'")
    price_basis: str | None = Field(
        description="What the displayed prices cover, e.g. 'per adult, one way, including taxes and fees', if stated"
    )
    flights: list[Flight]


class SelectedFare(BaseModel):
    flight_selected: bool = Field(
        description="True only if a specific flight and fare is now selected (shown as selected, or in the trip "
        "summary / cart) AND no fare-choice or upgrade popup is still open waiting for an answer"
    )
    flight: str | None = Field(description="Selected flight, e.g. 'QF 401 06:00 SYD - 07:35 MEL'")
    fare_name: str | None = Field(description="Selected fare brand, e.g. 'Economy Sale'")
    fare_price: str | None = Field(description="Price shown for the selected fare, e.g. '$129'")
    running_total: str | None = Field(description="Trip total / cart total currently displayed, if any")
    reason: str | None = Field(
        default=None, description="One short sentence explaining this reading of the screen; null if unsure"
    )


class Offer(BaseModel):
    category: str = Field(description=f"One of: {EXTRA_CATEGORIES}")
    label: str = Field(
        description="Name of the extra or fee as displayed, e.g. '23kg checked bag', 'Extra legroom seat'"
    )
    price: str | None = Field(
        description="Price as displayed, e.g. '$45', 'From $9', 'Included', '1.3%'; null if not shown"
    )
    price_basis: str | None = Field(description="e.g. 'per passenger per flight', 'per booking', if stated")
    selected: bool = Field(
        description="True if this extra is currently added/ticked/selected and so included in the running total"
    )
    mandatory: bool = Field(description="True if it is a compulsory charge that cannot be declined")


class StepState(BaseModel):
    step_type: str = Field(description=f"Which booking step this is. One of: {STEP_TYPES}")
    heading: str | None = Field(
        description="Main heading or step name shown, e.g. 'Guest details', 'Choose your seats'"
    )
    is_payment_page: bool = Field(
        description="True only on the final payment step where a payment method / card details are requested to complete the booking"
    )
    blocked_reason: str | None = Field(
        description="If progress is blocked by a mandatory login, CAPTCHA, 'verify you are human', access-denied, "
        "session-expired or bot check, describe it; otherwise null"
    )
    offers: list[Offer] = Field(
        description="Every optional extra, upsell, add-on, fee or surcharge shown on this step (in the page or an open modal), "
        "with its price. Summarise seat maps as one offer per seat type/price band. Empty if none."
    )
    price_lines: list[Fee] = Field(
        description="Lines of the price summary / trip total panel currently displayed, if any"
    )
    total: str | None = Field(description="Running trip total currently displayed, e.g. '$129.00'")
    validation_errors: list[str] = Field(description="Any form validation or error messages currently displayed")
    summary: str | None = Field(default=None, description="One short sentence describing this screen; null if unsure")


PASSENGER_DETAILS = ("title", "first_name", "last_name", "gender", "date_of_birth", "email", "phone", "country")
# Chosen from a list, so the model has to be told the value. None of these identifies a person.
CHOSEN_DETAILS = ("title", "gender", "country")


class FieldToFill(BaseModel):
    label: str = Field(description="Label of the form field exactly as displayed")
    kind: str | None = Field(default=None, description="One of: text, dropdown, radio, checkbox, date; null if unclear")
    detail: str | None = Field(
        default=None,
        description=f"Which passenger detail belongs in this field. One of: {', '.join(PASSENGER_DETAILS)}, other; "
        "null if unclear",
    )


class PassengerForm(BaseModel):
    fields: list[FieldToFill] = Field(
        description="Required passenger/contact fields that are still empty or unselected, in page order. "
        "Exclude optional fields, frequent-flyer / loyalty numbers, marketing opt-ins and any payment fields."
    )
    complete: bool = Field(description="True if every required field is already filled in")


class LineItem(BaseModel):
    name: str = Field(description="Flight as displayed, e.g. 'Sydney to Melbourne QF 401'")
    variant: str | None = Field(description="Fare brand, e.g. 'Economy Sale'")
    quantity: int = Field(description="Number of passengers")
    price: str | None = Field(description="Fare amount as displayed for this flight")


class Surcharge(BaseModel):
    payment_method: str = Field(description="e.g. 'Visa/Mastercard credit', 'Amex', 'PayPal', 'Debit card'")
    fee: str | None = Field(
        default=None, description="Fee as displayed, e.g. '1.3%', '$3.50', 'No fee'; null if no fee is shown"
    )


class CheckoutDetails(BaseModel):
    is_checkout_page: bool = Field(
        description="True if on the payment / review-and-pay page showing the booking's price summary"
    )
    blocked_reason: str | None = Field(
        description="If progress is blocked by a required login, CAPTCHA, 'verify you are human' or bot check, describe it; otherwise null"
    )
    items: list[LineItem]
    subtotal: str | None = Field(description="Flight/fare total before optional extras and payment fees, as displayed")
    extras: list[Fee] = Field(
        description="Optional extras currently included in the total (bags, seats, insurance, carbon offset, ...)"
    )
    other_fees: list[Fee] = Field(
        description="Any compulsory charge added on top of the fare that is NOT a government tax and NOT an optional extra, "
        "e.g. booking fee, service fee, card payment fee already applied"
    )
    payment_surcharges: list[Surcharge] = Field(
        description="Card/payment-method fees advertised on the page, whether or not applied to the total yet"
    )
    tax_amount: str | None = Field(description="Taxes/airport charges if itemised")
    tax_included: bool | None = Field(
        default=None,
        description="True if taxes and charges are included in the fare shown; False if they are added as a "
        "separate line; null if not stated",
    )
    total: str | None = Field(description="Total amount due as displayed")
    currency: str | None = Field(description="Currency code if shown, e.g. 'AUD'")
    summary: str | None = Field(default=None, description="One short sentence describing this screen; null if unsure")


@dataclass
class Trip:
    origin: str
    destination: str
    depart: date

    @property
    def depart_text(self) -> str:
        return f"{self.depart.strftime('%A')} {self.depart.day} {self.depart.strftime('%B %Y')}"


@dataclass
class Passenger:
    title: str = "Mr"
    first_name: str = "Alex"
    last_name: str = "Testbooking"
    gender: str = "Male"
    date_of_birth: str = "15 June 1988"
    email: str = "alex.testbooking@example.com"
    phone: str = "0400 000 000"
    country: str = "Australia"

    @classmethod
    def from_settings(cls, settings: Settings) -> "Passenger":
        return cls(**{name: getattr(settings, f"pax_{name}") for name in cls.__dataclass_fields__})

    def typed_details(self) -> dict[str, str]:
        """The details that are typed into the form. They reach the page as Stagehand variables and
        are never part of a prompt, a log or a saved file."""
        return {name: getattr(self, name) for name in PASSENGER_DETAILS if name not in CHOSEN_DETAILS}


# ---------------------------------------------------------------------------
# Site steps
# ---------------------------------------------------------------------------
def wrong_results(state: SearchState, trip: Trip, url: str) -> str | None:
    """Why a page the extraction calls 'results' is not our search's results; None if it may be.

    A real results page may hide the search form, so only an airport that is shown and names another code
    counts against it: an empty (null) field never does.
    """
    if DEALS_PAGE.search(url):
        return f"a flight-deals page ({url}), not search results"
    for field, code in (("origin", trip.origin), ("destination", trip.destination)):
        shown = getattr(state, field)
        if shown and not re.search(rf"\b{re.escape(code)}\b", shown, re.IGNORECASE):
            return f"{field} shows '{shown}', not {code}"
    return None


async def wait_for_results(stagehand: Stagehand, page: Page, question: str, trip: Trip) -> SearchState:
    """Wait (without clicking anything in the page) for a loading flight list to appear."""
    state: SearchState | None = None
    for attempt in range(3):
        await wait_for_render(page, RESULTS_RENDER_SECONDS)
        state = with_airports((await stagehand.extract(question, SearchState)).data, await page_airports(page))
        if state.on_results_page and (wrong := wrong_results(state, trip, await page.url())):
            log.info(f"  The loaded page is not our search's results: {wrong}")
            break  # search_flights goes back to the search form
        if state.blocked_reason or state.on_results_page or not state.loading:
            break
        log.info(f"  Waiting for the flight results to load ({attempt + 1}/3): {state.reason}")
        await settle(page, SETTLE_MS)
    assert state is not None
    return state


# The trip-type control on qantas.com is a custom React widget (a `div` combobox and a `ul/li` list), so it is
# found by role and text, not by position. It is marked with a data attribute so the follow-up scripts find it again.
JS_TRIP_TYPE = r"""() => {
    const trip = s => /^one[ -]?way$/i.test(s) ? 'one_way' : /^return$/i.test(s) ? 'return'
        : /^multi[ -]?city$/i.test(s) ? 'multi_city' : null;
    // The value it shows: its first line that names a trip type ('Trip type\nReturn' -> 'Return').
    const shown = el => {
        const text = el.tagName === 'SELECT' ? ((el.selectedOptions[0] || {}).text || '') : (el.innerText || el.value || '');
        return text.split('\n').map(clean).find(line => trip(line)) || null;
    };
    const found = Array.from(document.querySelectorAll(
        '[role="combobox"], [aria-haspopup="listbox"], [aria-haspopup="true"], button[aria-haspopup], select'
    )).filter(el => vis(el) && shown(el));
    found.sort((a, b) => Number(!!b.closest('form')) - Number(!!a.closest('form')));
    const el = found[0];
    if (!el) return null;
    document.querySelectorAll('[data-audit-trip]').forEach(e => e.removeAttribute('data-audit-trip'));
    el.setAttribute('data-audit-trip', '1');
    el.scrollIntoView({ block: 'center' });
    const r = el.getBoundingClientRect();
    return { trip: trip(shown(el)), text: shown(el), tag: el.tagName.toLowerCase(),
             expanded: el.getAttribute('aria-expanded'), x: r.left + r.width / 2, y: r.top + r.height / 2,
             controls: el.getAttribute('aria-controls') || el.getAttribute('aria-owns') };
}"""
# The visible "One way" entry of the open list: in the list the combobox names, else anywhere on the page.
JS_ONE_WAY_OPTION = r"""controls => {
    const pick = root => Array.from(root.querySelectorAll('[role="option"], [role="menuitem"], [role="menuitemradio"], li'))
        .find(el => vis(el, 4, 4) && /^one[ -]?way$/i.test(clean(el.innerText)));
    const list = controls ? document.getElementById(controls) : null;
    const el = (list && pick(list)) || pick(document);
    if (!el) return null;
    document.querySelectorAll('[data-audit-trip-option]').forEach(e => e.removeAttribute('data-audit-trip-option'));
    el.setAttribute('data-audit-trip-option', '1');
    el.scrollIntoView({ block: 'nearest' });
    const r = el.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
}"""
JS_DOM_CLICK = (
    r"""attribute => { const el = document.querySelector(`[${attribute}]`); return el ? clickEl(el) : false; }"""
)
JS_SELECT_ONE_WAY = r"""() => {
    const el = document.querySelector('[data-audit-trip]');
    const option = el && Array.from(el.options || []).find(o => /^one[ -]?way$/i.test(clean(o.text)));
    return option ? selectOption(el, option.value) : false;
}"""


async def read_trip_type(page: Page) -> dict | None:
    """The trip-type control as the page shows it ({trip: 'one_way'|'return'|'multi_city', x, y, ...}); None if not found."""
    found = await js(page, JS_TRIP_TYPE)
    return found if isinstance(found, dict) else None


async def _open_trip_list(page: Page, control: dict) -> dict | None:
    """Open the trip-type list (a real click, a DOM click if that showed nothing) and find its 'One way' entry."""
    await click_at(page, control["x"], control["y"])
    await settle(page, TRIP_SETTLE_MS)
    option = await js(page, JS_ONE_WAY_OPTION, control.get("controls"))
    if option is None:
        await js(page, JS_DOM_CLICK, "data-audit-trip", default=False)
        await settle(page, TRIP_SETTLE_MS)
        option = await js(page, JS_ONE_WAY_OPTION, control.get("controls"))
    return option if isinstance(option, dict) else None


async def set_one_way(page: Page) -> bool:
    """Set the search form's trip type to one way in code, without the model. True if the page now shows 'One way'.

    The model cannot be relied on here: it calls selectOptionFromDropdown on the `div` combobox (which changes
    nothing yet reports success), or clicks it open and closed again without choosing from the list.
    """
    try:
        control = await read_trip_type(page)
        if control is None:
            log.info("  Trip type: no trip-type control found in the page; leaving it to the model")
            return False
        if control["trip"] == "one_way":
            log.info("  Trip type: already 'One way'")
            return True
        if control["tag"] == "select":
            await js(page, JS_SELECT_ONE_WAY, default=False)
        else:
            option = await _open_trip_list(page, control)
            if option is None:
                log.info(
                    f"  Trip type: opened '{control['text']}' but found no 'One way' option; leaving it to the model"
                )
                return False
            await click_at(page, option["x"], option["y"])
            await settle(page, TRIP_SETTLE_MS)
            after = await read_trip_type(page)
            if not after or after["trip"] != "one_way":  # React ignored the mouse click: try a DOM click
                await js(page, JS_DOM_CLICK, "data-audit-trip-option", default=False)
        await settle(page, TRIP_SETTLE_MS)
        after = await read_trip_type(page)
    except BROWSER_ERRORS as error:  # a failed helper leaves the trip type to the model, as before
        log.info(f"  Trip type: could not be set in the page ({error}); leaving it to the model")
        return False
    done = bool(after and after["trip"] == "one_way")
    log.info(
        "  Trip type: set to 'One way'"
        if done
        else f"  Trip type: still '{(after or control)['text']}' after choosing 'One way'"
    )
    return done


HELD_FIELDS = ("trip_type", "origin", "destination", "depart_date", "using_points")


def search_held(state: SearchState, before: tuple | None = None) -> tuple:
    """What the search form holds, as far as progress goes: trip type, airports, date and points.

    Normalised so 'Return'/'return' and ''/None read the same. Passengers are left out, and a field the
    extraction left empty keeps its value from `before`: the extraction drops fields from one read to the
    next on an unchanged screen, and that must not look like the search moving on.
    """
    held = []
    for index, name in enumerate(HELD_FIELDS):
        value = getattr(state, name)
        value = (str(value).strip().lower() or None) if isinstance(value, str) else value
        held.append(before[index] if value is None and before is not None else value)
    return tuple(held)


def is_month_navigation(step_description: str) -> bool:
    """True for the date picker's next/previous-month arrow, which is clicked again and again on purpose."""
    return bool(MONTH_NAVIGATION.search(step_description))


async def page_trip_type(page: Page) -> str | None:
    """The trip type the page's own control shows ('one_way', 'return', ...); None if it can't be read."""
    try:
        control = await read_trip_type(page)
    except BROWSER_ERRORS as error:  # a failed read falls back to the extraction's trip type
        log.debug("Trip type read failed: %s", error)
        return None
    return control["trip"] if control else None


def with_trip_type(state: SearchState, trip_type: str | None) -> SearchState:
    """`state` with the trip type read from the DOM, which is reliable, in place of the extraction's guess."""
    return state.model_copy(update={"trip_type": trip_type}) if trip_type else state


# The airport fields are React type-ahead comboboxes labelled "Departure location" / "Arrival location". A filled
# one shows 'SYD, Sydney (Kingsford Smith), Australia'; an empty one its placeholder. The field is found by its
# label (aria-label, aria-labelledby, <label for>, a wrapping or nearby label), never by position, and marked
# with data-audit-airport so the follow-up scripts find it again.
JS_AIRPORT_FIELD = r"""which => {
    const want = which === 'departure' ? /departure|\bfrom\b/i : /arrival|\bto\b/i;
    const other = which === 'departure' ? /arrival|\bto\b/i : /departure|\bfrom\b/i;
    const named = text => !!text && want.test(text) && !other.test(text);
    // The field's own label first, then labels further away: the first one that names a side decides.
    const labels = el => {
        const found = [el.getAttribute('aria-label'), el.getAttribute('placeholder'), el.getAttribute('name')];
        (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
            .forEach(id => { const n = document.getElementById(id); if (n) found.push(n.innerText); });
        if (el.id) {
            const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
            if (l) found.push(l.innerText);
        }
        const wrap = el.closest('label');
        if (wrap) found.push(wrap.innerText);
        let up = el.parentElement;
        for (let i = 0; i < 3 && up; i++, up = up.parentElement) {
            const l = up.querySelector('label, [class*="label" i]');
            if (l && l !== el) { found.push(l.innerText); break; }
        }
        return found.map(clean).filter(Boolean);
    };
    const isField = el => vis(el) && !el.closest('header, nav, footer') && el.type !== 'hidden';
    const found = Array.from(document.querySelectorAll('input, [role="combobox"]'))
        .filter(isField).filter(el => { const l = labels(el).find(t => want.test(t) || other.test(t)); return named(l); });
    const inForm = el => Number(!!el.closest('form, [role="search"], [class*="search" i], [id*="search" i]'));
    found.sort((a, b) => inForm(b) - inForm(a));
    const el = found[0];
    if (!el) return null;
    document.querySelectorAll(`[data-audit-airport="${which}"]`).forEach(e => e.removeAttribute('data-audit-airport'));
    el.setAttribute('data-audit-airport', which);
    el.scrollIntoView({ block: 'center' });
    // What it shows: an input's value, else the line of its text that starts with an airport code.
    const input = el.matches('input') ? el : el.querySelector('input');
    const lines = (el.innerText || '').split('\n').map(clean);
    const value = clean(input ? input.value : (lines.find(l => /^[A-Z]{3}\b/.test(l)) || '')) || null;
    // Its clear (x) button, if the widget has one next to it: looked for no higher than the field's own wrapper
    // (an ancestor holding another airport field holds that field's clear button too).
    let clear = null;
    let up = el.parentElement;
    const own = node => node.querySelectorAll('input, [role="combobox"]').length <= (el.matches('input') ? 1 : 2);
    for (let i = 0; i < 3 && up && !clear && own(up); i++, up = up.parentElement) {
        clear = Array.from(up.querySelectorAll('button, [role="button"]')).find(b => vis(b, 4, 4)
            && (/clear|remove|delete|reset/i.test(b.getAttribute('aria-label') || b.title || '')
                || /^[✕✖×xX]$/.test(clean(b.innerText))));
    }
    const box = e => { const r = e.getBoundingClientRect(); return { x: r.left + r.width / 2, y: r.top + r.height / 2 }; };
    return { value, tag: el.tagName.toLowerCase(), ...box(el), clear: clear ? box(clear) : null,
             controls: el.getAttribute('aria-controls') || el.getAttribute('aria-owns') };
}"""
# Select whatever the focused text field holds (else focus the marked field), so a Backspace empties it.
JS_FOCUS_AIRPORT = r"""which => {
    const typeable = e => e && (e.matches('input, textarea') || e.isContentEditable);
    let el = document.activeElement;
    if (!typeable(el)) {
        const field = document.querySelector(`[data-audit-airport="${which}"]`);
        el = field && (field.matches('input') ? field : field.querySelector('input'));
        if (!el) return false;
        el.focus();
    }
    if (el.select) el.select();
    return document.activeElement === el;
}"""
# The visible suggestion for `code` ('BNE, Brisbane, Australia'): in the list the field controls, else anywhere.
JS_AIRPORT_OPTION = r"""({ which, code }) => {
    const starts = new RegExp(`^${code}\\b`, 'i');
    const pick = root => Array.from(root.querySelectorAll('[role="option"], li'))
        .find(el => vis(el, 4, 4) && starts.test(clean(el.innerText)));
    const field = document.querySelector(`[data-audit-airport="${which}"]`);
    const ids = [document.activeElement, field].filter(Boolean)
        .map(e => e.getAttribute('aria-controls') || e.getAttribute('aria-owns')).filter(Boolean);
    const lists = ids.map(id => document.getElementById(id)).filter(Boolean);
    const el = lists.map(pick).find(Boolean) || pick(document);
    if (!el) return null;
    document.querySelectorAll('[data-audit-airport-option]').forEach(e => e.removeAttribute('data-audit-airport-option'));
    el.setAttribute('data-audit-airport-option', '1');
    el.scrollIntoView({ block: 'nearest' });
    const r = el.getBoundingClientRect();
    return { text: clean(el.innerText), x: r.left + r.width / 2, y: r.top + r.height / 2 };
}"""


def shows_code(value: str | None, code: str) -> bool:
    """True if an airport field's text names `code` first ('BNE, Brisbane, Australia' for BNE)."""
    return bool(value and re.match(rf"{re.escape(code)}\b", value.strip(), re.IGNORECASE))


async def read_airport(page: Page, which: str) -> dict | None:
    """The departure or arrival field as the page shows it ({value, x, y, clear, ...}); None if not found."""
    found = await js(page, JS_AIRPORT_FIELD, which)
    return found if isinstance(found, dict) else None


async def set_airport(page: Page, which: str, code: str) -> bool:
    """Set the search form's departure or arrival airport to `code` in code, without the model.

    True if the field now shows the code. The model cannot be relied on here: the departure field is pre-filled
    from geolocation, and the model picked the wrong entries from the type-ahead list over and over. A React
    type-ahead ignores a `.value =` assignment, so the code is typed with real key presses.
    """
    label = which.capitalize()
    try:
        field = await read_airport(page, which)
        if field is None:
            log.info(f"  {label}: no {which} field found in the page; leaving it to the model")
            return False
        if shows_code(field["value"], code):
            log.info(f"  {label}: already '{field['value']}'")
            return True
        await click_at(page, field["x"], field["y"])
        await settle(page, TRIP_SETTLE_MS)
        opened = await read_airport(page, which) or field  # the clear button may only show once focused
        if opened.get("clear"):
            await click_at(page, opened["clear"]["x"], opened["clear"]["y"])
            await settle(page, TRIP_SETTLE_MS)
        if not await js(page, JS_FOCUS_AIRPORT, which, default=False):
            await click_at(page, field["x"], field["y"])  # clearing moved the focus away
            await settle(page, TRIP_SETTLE_MS)
            await js(page, JS_FOCUS_AIRPORT, which, default=False)
        await page.key_press("Backspace")
        await page.type(code, delay=TYPE_DELAY_MS)
        await settle(page, AIRPORT_SETTLE_MS)
        option = await js(page, JS_AIRPORT_OPTION, {"which": which, "code": code})
        if not isinstance(option, dict):
            log.info(f"  {label}: typed '{code}' but found no '{code}, ...' option; leaving it to the model")
            return False
        await click_at(page, option["x"], option["y"])
        await settle(page, TRIP_SETTLE_MS)
        after = await read_airport(page, which)
        if not after or not shows_code(after["value"], code):  # React ignored the mouse click: try a DOM click
            await js(page, JS_DOM_CLICK, "data-audit-airport-option", default=False)
            await settle(page, TRIP_SETTLE_MS)
            after = await read_airport(page, which)
    except BROWSER_ERRORS as error:  # a failed helper leaves the airport to the model, as before
        log.info(f"  {label}: could not be set in the page ({error}); leaving it to the model")
        return False
    done = bool(after and shows_code(after["value"], code))
    log.info(
        f"  {label}: set to '{code}'"
        if done
        else f"  {label}: shows '{(after or field)['value']}' after choosing '{code}'"
    )
    return done


async def set_airports(page: Page, trip: Trip) -> bool:
    """Departure first, then arrival (choosing a departure can move the focus on to the arrival field)."""
    departure = await set_airport(page, "departure", trip.origin)
    arrival = await set_airport(page, "arrival", trip.destination)
    return departure and arrival


async def page_airports(page: Page) -> dict[str, str | None]:
    """The airports the page's own fields show ({'origin': ..., 'destination': ...}); None where unreadable."""
    shown: dict[str, str | None] = {}
    for field, which in (("origin", "departure"), ("destination", "arrival")):
        try:
            control = await read_airport(page, which)
        except BROWSER_ERRORS as error:  # a failed read falls back to the extraction's airport
            log.debug("%s read failed: %s", which, error)
            control = None
        shown[field] = control["value"] if control else None
    return shown


def with_airports(state: SearchState, airports: dict[str, str | None]) -> SearchState:
    """`state` with the airports the DOM shows in place of the extraction's reading, where the DOM has one."""
    update = {field: value for field, value in airports.items() if value}
    return state.model_copy(update=update) if update else state


def airports_set(airports: dict[str, str | None], trip: Trip) -> bool:
    return shows_code(airports.get("origin"), trip.origin) and shows_code(airports.get("destination"), trip.destination)


def form_rules(trip_type: str | None, airports: dict[str, str | None], trip: Trip) -> str:
    """What the model is told to leave alone: the parts of the form already set in code."""
    rules = ""
    if trip_type == "one_way":
        rules += " The trip type is already set to One way: do not touch the trip-type control."
    if airports_set(airports, trip):
        rules += (
            f" The departure and arrival airports are already set to {trip.origin} and {trip.destination}: "
            "do not touch, clear or swap them."
        )
    return rules


async def start_again(stagehand: Stagehand, page: Page, wrong: str, wrong_pages: int) -> int:
    """Back to the home page's search form after a page that is not our results; the count of such pages so far.

    Raises FlowChanged on the second one: the search keeps landing somewhere else, and carrying on would read
    a page of fare deals as flights.
    """
    wrong_pages += 1
    if wrong_pages >= MAX_WRONG_RESULTS:
        raise FlowChanged(f"The search reached the wrong page twice: {wrong}")
    log.info(f"  Not our search's results ({wrong}); starting the search again from {SITE_URL}")
    await page.goto(SITE_URL)
    await settle(page, SETTLE_MS)
    await dismiss_popups(stagehand, page, POPUPS)
    return wrong_pages


@dataclass
class SearchForm:
    """The parts of the search form set and read in code, without the model: trip type and airports."""

    stagehand: Stagehand
    page: Page
    trip: Trip
    question: str
    helper_works: bool = True  # False once set_airports failed: the airports are then the model's, not fought over

    async def prepare(self) -> None:
        """Set the trip type, then both airports."""
        await set_one_way(self.page)
        self.helper_works = await set_airports(self.page, self.trip)

    async def keep_airports(self, airports: dict[str, str | None]) -> dict[str, str | None]:
        """Set the airports back if the last action changed one (while the helper works); what they show now."""
        if not self.helper_works or airports_set(airports, self.trip):
            return airports
        self.helper_works = await set_airports(self.page, self.trip)
        return await page_airports(self.page)

    async def read(self) -> tuple[SearchState, str | None, dict[str, str | None]]:
        """The search state, with the trip type and airports the DOM shows in place of the extraction's reading."""
        state = (await self.stagehand.extract(self.question, SearchState)).data
        trip_type, airports = await page_trip_type(self.page), await page_airports(self.page)
        return with_airports(with_trip_type(state, trip_type), airports), trip_type, airports


async def search_flights(stagehand: Stagehand, page: Page, trip: Trip) -> None:
    """Work the site's flight search, one action at a time, until a list of flights shows.

    The trip type and both airports are set in code first (set_one_way, set_airports). For the rest, the model
    is given the goal and what the form currently holds, not a route through the form: it chooses each next
    action itself, so the order of fields and the shape of the widget do not matter.

    A page the extraction calls "results" is checked against the search (wrong_results): a flight-deals page,
    or one that shows other airports, sends the search back to the home page; a second one ends it.
    """
    goal = (
        f"a ONE-WAY flight for 1 adult in economy from {trip.origin} to {trip.destination} departing "
        f"{trip.depart_text}, paying with money (not frequent-flyer points)"
    )
    question = (
        f"We are searching for {goal}. Report the current state of the flight search, "
        "or whether a list of flights for that search is already showing."
    )

    form = SearchForm(stagehand, page, trip, question)
    await form.prepare()
    state, trip_type, airports = await form.read()
    held: tuple | None = None  # what the search held after the last action, empty fields carried over
    last: tuple[str, tuple] | None = None  # (control acted on, what the search held after it)
    repeats = 0
    wrong_pages = 0
    hint = ""
    for _ in range(MAX_SEARCH_STEPS):
        if state.blocked_reason:
            raise Blocked(state.blocked_reason)
        if state.on_results_page:
            wrong = wrong_results(state, trip, await page.url())
            if wrong is None:
                return
            wrong_pages = await start_again(stagehand, page, wrong, wrong_pages)
            await form.prepare()
            state, trip_type, airports = await form.read()
            held, last, repeats, hint = None, None, 0, ""
            continue
        if state.loading:
            # A submitted search renders slowly; acting on a half-loaded page is how a search gets restarted.
            state = await wait_for_results(stagehand, page, question, trip)
            if state.loading:
                break
            continue
        if (trip_type or state.trip_type) != "one_way" and await set_one_way(page):
            trip_type = "one_way"
        airports = await form.keep_airports(airports)
        state = with_airports(with_trip_type(state, trip_type), airports)
        step = await llm_act(
            stagehand,
            f"Goal: show the list of flights for {goal}. Take the single next action that moves towards that goal. "
            f"The search currently holds: trip type={state.trip_type}, from={state.origin}, to={state.destination}, "
            f"date={state.depart_date}, guests={state.guests}, using points={state.using_points}. Correct whatever "
            "does not match the goal (opening the search form first if it is hidden, and choosing airports and dates "
            f"from the site's own lists or calendar), then submit the search once everything matches."
            f"{form_rules(trip_type, airports, trip)}{hint}",
            follow_up=True,  # a custom dropdown needs opening before its option can be picked
        )
        await settle(page, SETTLE_MS)
        state, trip_type, airports = await form.read()
        log.info(f"  {step.description} -> {state.reason}")
        held = search_held(state, held)
        now = (step.selector, held)
        repeats = repeats + 1 if now == last else 0
        last = now
        paging = is_month_navigation(step.description)
        if repeats >= (MAX_MONTH_REPEATS if paging else MAX_STUCK_REPEATS):
            raise FlowChanged(
                f"Stuck on the flight search: '{step.description}' was acted on {repeats + 1} times in a row "
                f"and the search did not change (trip type={state.trip_type}, from={state.origin}, to={state.destination})"
            )
        hint = (
            f" Your last action ('{step.description}') changed nothing in the search. If it opened a list or menu, "
            "choose the right option from that list now; otherwise use a different control. Do not act on the same "
            "control again."
            if repeats and not paging
            else ""
        )
    if state.blocked_reason:
        raise Blocked(state.blocked_reason)
    wrong = wrong_results(state, trip, await page.url()) if state.on_results_page else None
    if not state.on_results_page or wrong:
        raise FlowChanged(f"Couldn't reach the flight results page: {wrong or state.reason or 'no reason given'}")


def choose_flight(flights: list[Flight]) -> Flight | None:
    """Cheapest available flight, preferring direct services."""
    available = [f for f in flights if not f.sold_out and parse_price(f.price) is not None]
    direct = [f for f in available if f.stops == 0]
    return min(direct or available, key=lambda f: parse_price(f.price) or 0.0, default=None)


def describe_flight(flight: Flight) -> tuple[str, str]:
    """The item name for a flight, and how to describe it in a prompt, from whatever the listing showed."""
    times = " - ".join(filter(None, [flight.departs, flight.arrives]))
    name = " ".join(filter(None, [flight.flight_number, times])) or f"Flight at {flight.price}"
    legs = [
        f"departing {flight.departs}" if flight.departs else "",
        f"arriving {flight.arrives}" if flight.arrives else "",
    ]
    when = " and ".join(filter(None, legs))
    described = " ".join(filter(None, ["the flight", when, flight.flight_number and f"({flight.flight_number})"]))
    if not when and not flight.flight_number:
        described += f" priced {flight.price}"
    return name, described


async def select_flight(stagehand: Stagehand, page: Page, flight: Flight) -> dict:
    name, described = describe_flight(flight)
    item = new_item(name, flight.price)
    question = (
        f"We want {described} on its cheapest fare. Is a flight and fare now selected (shown as selected, or in the "
        "trip summary / cart)? Report the selected flight, fare brand, fare price and any running total."
    )
    selected = None
    for _ in range(4):
        step = await llm_act(
            stagehand,
            f"Goal: {described} selected on its CHEAPEST available fare. Take the single next action towards that "
            "goal (for example revealing the flight's fares, or choosing the cheapest one). If a popup offers a more "
            f"expensive fare or an upgrade, decline it and keep the cheapest fare. {NO_RESEARCH}",
        )
        log.info(f"    Flight selection: {step.description}")
        await settle(page, SETTLE_MS)
        selected = (await stagehand.extract(question, SelectedFare)).data
        if selected.flight_selected:
            break
    if selected is not None:
        item["selected_variant"] = selected.fare_name
        item["selected_price_display"] = selected.fare_price
        item["selected_price"] = parse_price(selected.fare_price)
        item["cart_total_display"] = selected.running_total
        item["status"] = "added" if selected.flight_selected else "add_failed"
    return item


def passenger_field_instruction(entry: FieldToFill, passenger: Passenger) -> tuple[str, dict[str, str]] | None:
    """The instruction and Stagehand variables for one form field; None if we have nothing for it.

    Typed details go in as `%name%` variables, which Stagehand substitutes after the model has
    answered, so names, date of birth, email and phone never appear in a prompt. A value picked
    from a list (title, gender, country) has to be named for the model to find the option.
    """
    if entry.detail not in PASSENGER_DETAILS:
        return None
    field = f"the '{entry.label}' field of the passenger details form"
    if entry.detail in CHOSEN_DETAILS or entry.kind in ("dropdown", "radio", "checkbox"):
        return f"Select '{getattr(passenger, entry.detail)}' for {field}.", {}
    return f"Type %{entry.detail}% into {field}.", {entry.detail: getattr(passenger, entry.detail)}


async def fill_passenger_details(stagehand: Stagehand, page: Page, passenger: Passenger) -> None:
    """Fill the required guest fields, two passes at most (a first pass can reveal more fields)."""
    for _ in range(2):
        form = (
            await stagehand.extract(
                "List the required passenger and contact fields on this page that are still empty or unselected. "
                "For each, give its label, its kind, and which passenger detail belongs in it "
                f"({', '.join(PASSENGER_DETAILS)}). Do not suggest values.",
                PassengerForm,
            )
        ).data
        if form.complete or not form.fields:
            return
        for entry in form.fields[:20]:
            planned = passenger_field_instruction(entry, passenger)
            if planned is None:
                log.debug("No passenger detail for field %r (%s)", entry.label, entry.detail)
                continue
            instruction, variables = planned
            try:
                step = await llm_act(
                    stagehand,
                    instruction,
                    follow_up=not variables,  # a custom dropdown needs opening before the option can be picked
                    **({"variables": variables} if variables else {}),
                )
                log.info(f"    Guest details: {step.description}")
            except BROWSER_ERRORS as error:  # one field failing must not stop the rest; the step check reports it
                log.warning(f"    Guest details: could not fill '{entry.label}' ({type(error).__name__}: {error})")
            await settle(page, 400)


BOOKING_GOAL = (
    "Goal: reach the payment page of this booking with the cheapest fare and NO optional extras (no paid bags, "
    "seats, insurance, carbon offset, upgrades, car hire, hotels or donations), continuing as a guest without "
    "logging in or joining a loyalty program. Take the single next action that moves the booking towards the "
    "payment page. Where the page insists on a choice, choose the free or decline option. Never click a button "
    "that pays or completes the purchase."
)


async def next_booking_action(
    stagehand: Stagehand, page: Page, passenger: Passenger, state: StepState, step_type: str
) -> None:
    """Take the one action that moves the booking on from `step_type`, declining every extra."""
    if step_type == "passenger_details":
        await fill_passenger_details(stagehand, page, passenger)
    instruction = f"{BOOKING_GOAL} The current screen looks like: {state.heading or step_type}."
    if state.validation_errors:
        instruction += (
            f" The page shows these errors: {'; '.join(state.validation_errors[:3])}. Fix the first one if it is "
            "a missing choice or field (never by adding a paid extra)."
        )
    step = await llm_act(stagehand, f"{instruction} {NO_RESEARCH}")
    log.info(f"    {step.description}")
    await settle(page, SETTLE_MS)


async def walk_to_payment(stagehand: Stagehand, page: Page, passenger: Passenger, result: SiteResult) -> bool:
    """Step through the booking flow, recording every extra offered, until the payment page. True if reached."""
    out = result.out_dir
    last_signature, repeats = None, 0
    last_state: StepState | None = None
    last_seen: str | None = None
    for index in range(1, MAX_BOOKING_STEPS + 1):
        await wait_for_render(page, 10)
        seen = await fingerprint(page)
        if last_state is not None and seen is not None and seen == last_seen:
            # The last action changed nothing on screen: the step is the same, so is what an extraction
            # would report. Count it as a repeat without paying for another extraction.
            repeats += 1
            log.info(f"  Step {index}: unchanged after the last action (repeat {repeats})")
            if repeats >= MAX_STUCK_REPEATS:
                errors = "; ".join(last_state.validation_errors) or last_state.summary or "no summary"
                result.message = f"Stuck on '{last_state.heading or last_state.step_type}': {errors}"
                return False
            await next_booking_action(
                stagehand,
                page,
                passenger,
                last_state,
                "payment" if last_state.is_payment_page else last_state.step_type,
            )
            continue
        state = (
            await stagehand.extract(
                "We are part-way through booking a flight and want to reach the payment page without buying any "
                "optional extras. Identify the current booking step, list every optional extra, add-on, upsell, fee or "
                "surcharge offered on it with prices (including in any open popup), and report the price summary, "
                "running total, validation errors and anything blocking progress.",
                StepState,
            )
        ).data
        step_type = "payment" if state.is_payment_page else state.step_type
        log.info(f"  Step {index}: {step_type} ({state.heading}) | total {state.total} | {len(state.offers)} offer(s)")
        await shot(page, out / f"step-{index:02d}-{slug(step_type)}.png")
        record_offers(result, index, state.step_type, state.heading, state.offers)
        result.steps.append(
            {
                "step": index,
                "step_type": step_type,
                "heading": state.heading,
                "url": await page.url(),
                "total_display": state.total,
                "total": parse_price(state.total),
                "price_lines": [line.model_dump() for line in state.price_lines],
                "offers": len(state.offers),
                "validation_errors": state.validation_errors,
                "summary": state.summary,
            }
        )
        if index == 1:
            await shot(page, out / "cart.png")  # first view of the selected trip and its running total

        if state.blocked_reason:
            raise Blocked(state.blocked_reason)
        if state.is_payment_page or step_type == "payment":
            return True
        if step_type == "confirmation":
            raise FlowChanged("Reached a confirmation page unexpectedly; stopping")

        signature = (step_type, state.heading, await page.url())
        repeats = repeats + 1 if signature == last_signature else 0
        last_signature = signature
        if repeats >= MAX_STUCK_REPEATS:
            result.message = f"Stuck on '{state.heading or step_type}': {'; '.join(state.validation_errors) or state.summary or 'no summary'}"
            return False

        # Remember what the screen looked like when it was read; if the action leaves it exactly so,
        # the next turn knows without asking the model.
        last_state, last_seen = state, seen
        await next_booking_action(stagehand, page, passenger, state, step_type)

    result.message = f"Payment page not reached within {MAX_BOOKING_STEPS} steps"
    return False


def compare_prices(result: SiteResult, checkout: CheckoutDetails | None) -> Report:
    """Advertised fare against the payment page. `checkout` is None if the payment page was never read."""
    added = result.added
    fare_line = checkout.items[0] if checkout and checkout.items else None
    expected_total = compare_item_prices(added, lambda _item: fare_line)
    subtotal = parse_price(checkout.subtotal) if checkout else None
    total = parse_price(checkout.total) if checkout else None
    final_read = final_price_was_read(checkout, total)
    if total is None:  # payment page not reached: fall back to the last running total seen
        total = next((s["total"] for s in reversed(result.steps) if s["total"] is not None), None)
    fees = [{"label": f.label, "amount": parse_amount(f.amount)} for f in (checkout.other_fees if checkout else [])]
    fees_total = money_sum(parse_amount(f.amount) for f in (checkout.other_fees if checkout else [])) or 0.0
    included = [{"label": f.label, "amount": parse_amount(f.amount)} for f in (checkout.extras if checkout else [])]
    surcharges = [s.model_dump() for s in (checkout.payment_surcharges if checkout else [])]
    chargeable = [s for s in surcharges if positive(parse_price(s["fee"]))]
    extras = summarise_extras(result.extras)
    preselected = extras["extras_preselected"]

    reasons = [
        reason
        for reason, hit in (
            ("fare increased at checkout", any(i["price_increased"] for i in added)),
            ("checkout fare subtotal higher than selected fare", positive(diff(subtotal, expected_total))),
            (
                "total at checkout higher than advertised fare with no extras chosen",
                positive(diff(total, expected_total)),
            ),
            ("non-tax fees added", fees_total > 0),
            ("optional extras pre-selected: " + ", ".join(preselected), bool(preselected)),
            (
                "payment surcharge applies: " + ", ".join(f"{s['payment_method']} {s['fee']}" for s in chargeable),
                bool(chargeable),
            ),
        )
        if hit
    ]
    return {
        "items_added": len(added),
        "expected_subtotal": expected_total,
        "cart_subtotal_display": added[0].get("cart_total_display") if added else None,
        "checkout_subtotal_display": checkout.subtotal if checkout else None,
        "checkout_subtotal": subtotal,
        "subtotal_vs_expected_diff": diff(subtotal, expected_total),
        "total_vs_expected_diff": diff(total, expected_total),
        "shipping": "n/a (flight booking)",
        "other_fees": fees,
        "other_fees_total": fees_total,
        "extras_in_total": included,
        "payment_surcharges": surcharges,
        **extras,
        "tax_amount_display": checkout.tax_amount if checkout else None,
        "tax_included": checkout.tax_included if checkout else None,
        "checkout_total_display": checkout.total if checkout else None,
        "checkout_total": total,
        "currency": checkout.currency if checkout else None,
        **drip_outcome(reasons, final_read),
        "checkout_summary": checkout.summary if checkout else None,
    }


async def read_payment_page(stagehand: Stagehand, page: Page, result: SiteResult) -> CheckoutDetails:
    await shot(page, result.out_dir / "checkout.png")
    checkout = (
        await stagehand.extract(
            "Extract the booking's price summary from this payment page: flights and fare, fare subtotal, optional "
            "extras included, any other fees, taxes, total due and currency. Also list every card or payment-method "
            "surcharge/fee advertised (e.g. credit card fee percentages), and report if a login, CAPTCHA or bot "
            "check is blocking the page. Do not interact with the page.",
            CheckoutDetails,
        )
    ).data
    if checkout.payment_surcharges:  # replace the payment step's own offers with the per-method list
        result.extras = [
            e for e in result.extras if not (e["step_type"] == "payment" and e["category"] == "payment_surcharge")
        ]
    for s in checkout.payment_surcharges:
        result.extras.append(
            {
                "step": len(result.steps),
                "step_type": "payment",
                "step_heading": "Payment",
                "category": "payment_surcharge",
                "label": s.payment_method,
                "price_display": s.fee,
                "price": parse_amount(s.fee),
                "price_basis": "per booking",
                "selected_by_default": False,
                "mandatory": False,
            }
        )
    return checkout


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------
def today() -> date:
    """Today in the local timezone, where the airline's calendar and the user both are."""
    return datetime.now().astimezone().date()


def future_date(value: str) -> date:
    day = date.fromisoformat(value)
    if day <= today():
        raise argparse.ArgumentTypeError("must be in the future")
    return day


class QantasWorkflow(Workflow):
    name = "qantas"
    help = "Qantas: book a one-way flight up to the payment page and record every extra and fee offered"
    system_prompt = SAFETY_PROMPT
    log_level = "info"
    viewport = (1440, 1000)
    clear_cookies = "domain"  # every booking starts without the airline's cookies from the last one

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser, settings: Settings) -> None:
        parser.add_argument("--origin", default="SYD", help="Departure airport code (default SYD)")
        parser.add_argument("--destination", default="MEL", help="Arrival airport code (default MEL)")
        parser.add_argument(
            "--date",
            type=future_date,
            default=today() + timedelta(days=30),
            help="Departure date as YYYY-MM-DD (default: 30 days from today)",
        )
        parser.add_argument("--url", default=SITE_URL, help=f"Start page (default {SITE_URL})")

    def targets(self) -> list[str]:
        return [self.args.url]

    async def run(self, stagehand: Stagehand, page: Page, url: str, result: SiteResult) -> None:
        out = result.out_dir
        trip = Trip(self.args.origin.upper(), self.args.destination.upper(), self.args.date)
        result.search = {
            "origin": trip.origin,
            "destination": trip.destination,
            "depart_date": trip.depart.isoformat(),
            "passengers": "1 adult",
            "trip_type": "one_way",
        }
        checkout: CheckoutDetails | None = None
        log.info(f"\n=== {url} | {trip.origin} -> {trip.destination} on {trip.depart_text} ===")
        try:
            await page.goto(url)
            await settle(page, SETTLE_MS)
            await dismiss_popups(stagehand, page, POPUPS)

            await search_flights(stagehand, page, trip)
            await wait_for_render(page, RESULTS_RENDER_SECONDS)
            result.shop_url = await page.url()
            log.info(f"  Results page: {result.shop_url}")

            listing = (
                await stagehand.extract(
                    "Extract every flight listed on this results page with its times, flight number, stops, duration, lowest "
                    "displayed price, availability and per-fare prices where visible. Also report what the prices cover.",
                    Flights,
                )
            ).data
            rows = [
                {**f.model_dump(exclude={"fares"}), "fares": "; ".join(f"{o.fare_name} {o.price}" for o in f.fares)}
                for f in listing.flights
            ]
            write_csv(out / "products.csv", list(Flight.model_fields), rows)
            await shot(page, out / "flights.png")
            log.info(f"  Extracted {len(listing.flights)} flights ({listing.price_basis or 'price basis not stated'})")
            if listing.flights and not any(f.departs or f.flight_number for f in listing.flights):
                raise FlowChanged(
                    "Results page lists no flights with times or flight numbers (likely a deals page): "
                    f"{listing.route or 'route not shown'}"
                )

            flight = choose_flight(listing.flights)
            if flight is None:
                result.fail(Status.NO_FLIGHTS_FOUND, "No available flights with a price on the results page")
                return

            log.info(
                f"  Selecting: {flight.flight_number or ''} {flight.departs or ''}-{flight.arrives or ''} ({flight.price})"
            )
            item = await select_flight(stagehand, page, flight)
            item["price_basis"] = listing.price_basis
            result.items.append(item)
            if item["status"] != "added":
                result.fail(Status.ADD_TO_CART_FAILED, "The flight and fare could not be selected")
                return

            if not await walk_to_payment(stagehand, page, Passenger.from_settings(self.settings), result):
                result.fail(Status.NO_CHECKOUT, result.message)
                return
            checkout = await read_payment_page(stagehand, page, result)
            if checkout.blocked_reason:
                result.fail(Status.BLOCKED, checkout.blocked_reason)
            elif not checkout.is_checkout_page:
                result.fail(Status.NO_CHECKOUT, "Payment page not reached")
            else:
                result.status = Status.OK
        finally:  # keep whatever extras were seen, however the run ended
            self.conclude(result, lambda: compare_prices(result, checkout))
            with contextlib.suppress(*BROWSER_ERRORS):  # the browser may be gone; the URL is a nicety
                result.checkout_url = await page.url()

    def target_key(self, url: str, result: SiteResult) -> str:
        """Start page plus route, so that audits of different routes do not replace each other in summary.csv."""
        search = result.search or {}
        return f"{url} {search.get('origin', '?')}-{search.get('destination', '?')}"

    def sensitive_values(self) -> dict[str, str]:
        return Passenger.from_settings(self.settings).typed_details()

    def summarise(self, result: SiteResult) -> None:
        c = result.comparison
        log.info(
            f"  Advertised fare: {c['expected_subtotal']} | Checkout fare: {c['checkout_subtotal_display']} "
            f"| Total: {c['checkout_total_display']} | Fees: {c['other_fees_total']} | Extras offered: {c['extras_offered']}"
        )
        log.info(f"  Drip pricing detected: {drip_verdict(c)}")
