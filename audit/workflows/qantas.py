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
import asyncio
import contextlib
import itertools
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from stagehand import Page, Stagehand

from ..actions import Acted, act_on, click_at, llm_act, try_act
from ..config import Settings
from ..core import (
    BROWSER_ERRORS,
    STATUS_REASONS,
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
    is_transient,
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
ALTERNATING_STEPS = 4  # A, B, A, B with nothing changed counts as stuck
# Paging the date picker on with the same arrow is progress the search state cannot show (the date stays empty
# until a day is picked), so it gets a year of months before it counts as stuck.
MAX_MONTH_REPEATS = 12
MONTH_NAVIGATION = re.compile(
    r"\b(next|previous|prev|forward|back|later|earlier)\b.{0,20}\bmonths?\b|\bmonths?\b.{0,20}\b(next|previous|forward|back)\b",
    re.IGNORECASE,
)
TRIP_SETTLE_MS = 800  # the trip-type list opens and closes in place; no navigation to wait for
AIRPORT_OPTION_WAIT_MS = (
    8000  # the type-ahead fetches its suggestions after the code is typed; they take a second or two
)
AIRPORT_OPTION_POLL_MS = 250
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
    fare_name: str = Field(
        description="Fare brand as displayed, e.g. 'Economy Saver', 'Economy Flex', 'Business'. 'Classic Plus' and "
        "'Classic Reward' are reward-seat badges, not fare names. If no fare name is shown (the fare panel is not "
        "open yet), use the cabin name, e.g. 'Economy' or 'Business'"
    )
    price: str | None = Field(
        description="Cash price as displayed, e.g. '$129'; null if unavailable, sold out or only shown in Qantas "
        "Points (PTS)"
    )


class FarePanel(BaseModel):
    fares: list[FareOption] = Field(
        description="One entry per fare column of the open 'Select your fare' panel (e.g. 'Economy Saver', "
        "'Economy Flex'), left to right, with its cash price. Empty if no such panel is open"
    )


class Flight(BaseModel):
    flight_number: str | None = Field(
        description="Flight number, e.g. 'QF2310', shown under the times; null if not shown"
    )
    departs: str | None = Field(
        default=None, description="Departure time as displayed, e.g. '6:00am'; null if not shown"
    )
    arrives: str | None = Field(default=None, description="Arrival time as displayed; null if not shown")
    stops: int | None = Field(default=None, description="Number of stops; 0 for a direct flight; null if not shown")
    duration: str | None
    price: str | None = Field(
        description="The lowest CASH price in this flight's Economy Cabin column, e.g. '$529'; null if only a "
        "Qantas Points price (e.g. '16,600 PTS + $58') or 'No seats' is shown"
    )
    award_only: bool = Field(
        default=False,
        description="True if the only economy price shown for this flight is in Qantas Points / PTS, "
        "e.g. '16,600 PTS + $58'",
    )
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
        description="True only if the page shows a 'Your trip' summary (or a cart) with this flight and a flight "
        "subtotal above $0"
    )
    flight: str | None = Field(description="Selected flight, e.g. 'QF 401 06:00 SYD - 07:35 MEL'")
    fare_name: str | None = Field(description="Selected fare brand, e.g. 'Economy Sale'")
    fare_price: str | None = Field(description="Price shown for the selected fare, e.g. '$129'")
    running_total: str | None = Field(description="Trip total / cart total currently displayed, if any")
    subtotal: str | None = Field(
        default=None, description="Flight subtotal as displayed, e.g. '$995 AUD'; null if not shown"
    )
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
# The city a results page names for an airport ("Brisbane to Perth  Change search"). An airport not listed here
# has to show its code: a city is never guessed.
AIRPORT_CITIES = {
    "BNE": "Brisbane",
    "PER": "Perth",
    "SYD": "Sydney",
    "MEL": "Melbourne",
    "CBR": "Canberra",
    "ADL": "Adelaide",
    "OOL": "Gold Coast",
    "CNS": "Cairns",
    "HBA": "Hobart",
    "DRW": "Darwin",
}


def shows_airport(shown: str, code: str) -> bool:
    """Whether a shown airport ('Brisbane (BNE)', 'BNE, Brisbane, Australia', 'Brisbane') is `code`."""
    names = [code, AIRPORT_CITIES.get(code.upper(), "")]
    return any(name and re.search(rf"\b{re.escape(name)}\b", shown, re.IGNORECASE) for name in names)


def wrong_results(state: SearchState, trip: Trip, url: str) -> str | None:
    """Why a page the extraction calls 'results' is not our search's results; None if it may be.

    A real results page may hide the search form, so only an airport that is shown and names another code
    or city counts against it: an empty (null) field never does.
    """
    if DEALS_PAGE.search(url):
        return f"a flight-deals page ({url}), not search results"
    for field, code in (("origin", trip.origin), ("destination", trip.destination)):
        shown = getattr(state, field)
        if shown and not shows_airport(shown, code):
            return f"{field} shows '{shown}', not {code}"
    return None


# The results page, told from the page text alone (its class names are unknown): the "Economy Cabin" column
# header and at least one flight card with a time and a dollar price.
JS_RESULTS_PAGE = r"""() => {
    const text = document.body ? document.body.innerText : '';
    if (!/economy cabin/i.test(text)) return false;
    const time = /\b\d{2}:\d{2}\b/, price = /\$\s?\d/;
    return Array.from(document.querySelectorAll('li, article, section, div, [role="row"]')).some(el => {
        const t = txt(el);
        return t.length < 800 && time.test(t) && price.test(t) && vis(el, 200, 30);
    });
}"""
# The route in the results page's subheading: "Brisbane to Perth  Change search".
JS_RESULTS_ROUTE = r"""() => {
    const all = Array.from(document.querySelectorAll('h1, h2, h3, p, div, section, header'))
        .filter(el => vis(el, 20, 8) && /change search/i.test(txt(el)) && txt(el).length < 200);
    all.sort((a, b) => txt(a).length - txt(b).length);
    for (const el of all) {
        const m = clean(txt(el)).match(/([A-Z][A-Za-z .'()-]*?)\s+to\s+([A-Z][A-Za-z .'()-]*?)\s*change search/i);
        if (m) return { origin: m[1].trim(), destination: m[2].trim() };
    }
    return null;
}"""


async def page_shows_results(page: Page) -> bool:
    return bool(await js(page, JS_RESULTS_PAGE, default=False))


async def results_state(page: Page, state: SearchState) -> SearchState:
    """`state` as a results page, with the route the page's own subheading shows in place of the model's."""
    route = await js(page, JS_RESULTS_ROUTE, default=None)
    update: dict = {"on_results_page": True, "loading": False}
    if isinstance(route, dict):
        update.update({k: route[k] for k in ("origin", "destination") if route.get(k)})
    return state.model_copy(update=update)


def nothing_shown(state: SearchState) -> bool:
    """Neither the form nor results nor a spinner: a submitted search still rendering, so wait, don't act."""
    return not (state.form_visible or state.on_results_page or state.loading or state.blocked_reason)


async def wait_for_results(
    stagehand: Stagehand, page: Page, question: str, trip: Trip, state: SearchState | None = None
) -> SearchState:
    """Wait (without clicking anything in the page) for a loading flight list to appear."""
    for attempt in range(3):
        await wait_for_render(page, RESULTS_RENDER_SECONDS)
        if await page_shows_results(page):
            base = state or (await stagehand.extract(question, SearchState)).data
            return await results_state(page, base)
        state = with_airports((await stagehand.extract(question, SearchState)).data, await page_airports(page))
        if state.on_results_page and (wrong := wrong_results(state, trip, await page.url())):
            log.info(f"  The loaded page is not our search's results: {wrong}")
            break  # search_flights goes back to the search form
        if state.blocked_reason or state.on_results_page or not (state.loading or nothing_shown(state)):
            break
        log.info(f"  Waiting for the flight results to load ({attempt + 1}/3): {state.reason}")
        await settle(page, SETTLE_MS)
    assert state is not None
    return state


# The search form's Rewards / "use points" switch, if it is on: a role="switch" or checkbox labelled with points
# or Rewards. Clicked off in code so the model never has to.
JS_POINTS_SWITCH = r"""() => {
    const label = el => clean([el.getAttribute('aria-label'), el.closest('label') && txt(el.closest('label')),
        el.id && document.querySelector(`label[for="${CSS.escape(el.id)}"]`)
            && txt(document.querySelector(`label[for="${CSS.escape(el.id)}"]`)),
        el.parentElement && txt(el.parentElement)].filter(Boolean).join(' '));
    for (const el of document.querySelectorAll('[role="switch"], input[type="checkbox"]')) {
        const l = label(el);
        if (!/points|rewards?\b|classic reward/i.test(l) || l.length > 200) continue;
        const on = el.getAttribute('aria-checked') === 'true' || el.checked === true;
        if (!on) return { found: true, on: false };
        clickEl(el);
        return { found: true, on: true };
    }
    return { found: false, on: false };
}"""
POINTS_RULE = " Never touch the Rewards / 'use points' / 'Classic Reward' switch: we pay with money."


async def keep_cash_search(page: Page, state: SearchState) -> tuple[SearchState, str]:
    """Turn the points switch off in code if it is on; else the one instruction the model gets about it."""
    if not state.using_points:
        return state, ""
    if await switch_off_points(page):
        return state.model_copy(update={"using_points": False}), ""
    return state, " The Rewards / points switch is on: turn it off now, and do nothing else."


async def last_look_for_results(page: Page, state: SearchState) -> SearchState:
    """Before giving up: the last action may have been the submit, so give the results time to render."""
    if state.on_results_page:
        return state
    await wait_for_render(page, RESULTS_RENDER_SECONDS)
    return await results_state(page, state) if await page_shows_results(page) else state


async def switch_off_points(page: Page) -> bool:
    """Turn the form's points switch off in code; True if it was found (whether or not it needed turning off)."""
    found = await js(page, JS_POINTS_SWITCH, default=None)
    if isinstance(found, dict) and found.get("on"):
        log.info("  Turned the Rewards / points switch off")
        await settle(page, TRIP_SETTLE_MS)
    return bool(isinstance(found, dict) and found.get("found"))


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


def alternating(recent: list[tuple[str, tuple, str]]) -> tuple[str, str] | None:
    """The two controls' descriptions when the last ALTERNATING_STEPS actions took turns between just those two
    controls (A, B, A, B) and the search held the same throughout; None otherwise.

    The repeat count only sees the same control twice in a row, so a model clicking a calendar day and then the
    search button hidden behind the calendar, over and over, would otherwise use up every step.
    """
    if len(recent) < ALTERNATING_STEPS:
        return None
    steps = recent[-ALTERNATING_STEPS:]
    controls = [selector for selector, _, _ in steps]
    if len(set(controls)) != 2 or len({held for _, held, _ in steps}) != 1:
        return None
    if any(a == b for a, b in itertools.pairwise(controls)):
        return None  # not taking turns
    return steps[-2][2], steps[-1][2]


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
# The visible suggestion for `code`: in the list the field controls, else anywhere. Qantas shows the code as a
# badge after the city ('Brisbane, Australia  BNE'), so the code is matched as an upper-case word anywhere in the
# text (case-sensitive, so typing 'bne' does not match the 'bne' inside 'Brisbane').
JS_AIRPORT_OPTION = r"""({ which, code }) => {
    const names = new RegExp(`(^|[^A-Za-z])${code.toUpperCase()}([^A-Za-z]|$)`);
    const pick = root => Array.from(root.querySelectorAll('[role="option"], li'))
        .find(el => vis(el, 4, 4) && names.test(clean(el.innerText)));
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


async def wait_for_airport_option(page: Page, which: str, code: str) -> dict | None:
    """The type-ahead suggestion for `code` once it shows ({text, x, y}); None if it never does.

    The suggestions are fetched after typing and take a second or two to appear, and the page can look settled
    before they arrive, so this polls for the option itself rather than waiting for the page to go quiet.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + AIRPORT_OPTION_WAIT_MS / 1000
    while True:
        option = await js(page, JS_AIRPORT_OPTION, {"which": which, "code": code})
        if isinstance(option, dict):
            await asyncio.sleep(AIRPORT_OPTION_POLL_MS / 1000)  # let the list finish rendering before it is clicked
            again = await js(page, JS_AIRPORT_OPTION, {"which": which, "code": code})
            return again if isinstance(again, dict) else option
        if loop.time() >= deadline:
            return None
        await asyncio.sleep(AIRPORT_OPTION_POLL_MS / 1000)


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
        option = await wait_for_airport_option(page, which, code)
        if option is None:
            log.info(
                f"  {label}: typed '{code}' but no '{code}' option appeared within "
                f"{AIRPORT_OPTION_WAIT_MS / 1000:.0f}s; leaving it to the model"
            )
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


# The departure date. On qantas.com the search form's date control is a button ("Button to select travel dates,
# currently set to 29 Nov 2026") that opens a full-screen "Select travel dates" modal, rendered as a portal under
# <body> (outside the search form). The modal's subheading shows the day picked so far ("Depart BNE Sun, 14 Feb
# 2027"); its months are stacked vertically and scroll, each a heading ("February 2027") followed by a grid of day
# <button>s that show the day number and a price; a sticky footer holds a red "Continue" button. Picking a day does
# NOT set the form's date: only Continue does, and closes the modal.
# These selectors follow that description (from the run log and screenshots of 2026-11-26); the modal's HTML was
# not captured. Elements are found by role and text, never by position, and marked with data-audit-* attributes.
MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)  # fmt: skip  - not strftime('%B'): that follows the machine's locale
DATE_DIALOG_WAIT_MS = 6000  # the modal fetches its fares before it renders the months
DATE_POLL_MS = 250
MAX_DATE_SCROLLS = 12  # the months scroll; a year ahead is about this many screens
# The form's travel-dates control: a button, input or combobox outside any open dialog whose own label names dates.
JS_DATE_FIELD = r"""() => {
    const named = el => clean([el.getAttribute('aria-label'), el.getAttribute('placeholder'), el.getAttribute('name'),
        ...(el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
            .map(id => (document.getElementById(id) || {}).innerText), el.innerText].filter(Boolean).join(' '));
    const inDialog = el => !!el.closest('[role="dialog"], [aria-modal="true"], [data-audit-date-dialog]');
    const found = Array.from(document.querySelectorAll('button, [role="button"], input, [role="combobox"]'))
        .filter(el => vis(el) && !inDialog(el) && !el.closest('header, nav, footer'))
        .filter(el => /travel dates|depart(ure|ing)? date|\bdates?\b/i.test(named(el))
                      && !/return date/i.test(named(el)));
    const inForm = el => Number(!!el.closest('form, [role="search"], [class*="search" i], [id*="search" i]'));
    found.sort((a, b) => inForm(b) - inForm(a));
    const el = found[0];
    if (!el) return null;
    document.querySelectorAll('[data-audit-date]').forEach(e => e.removeAttribute('data-audit-date'));
    el.setAttribute('data-audit-date', '1');
    el.scrollIntoView({ block: 'center' });
    const r = el.getBoundingClientRect();
    return { text: named(el), value: el.value || null, x: r.left + r.width / 2, y: r.top + r.height / 2 };
}"""
# The open date modal: a dialog (or, failing a role, the outermost visible block holding a "Select travel dates"
# title and a Continue button). Returns its subheading line that names the leg ('Depart BNE Sun, 14 Feb 2027').
JS_DATE_DIALOG = r"""() => {
    const titled = el => /select (your )?travel dates|select (a )?(departure )?date/i.test(clean(el.innerText).slice(0, 400));
    let el = dialogs().find(d => vis(d) && titled(d));
    if (!el) {
        const title = Array.from(document.querySelectorAll('h1, h2, h3, h4, [role="heading"], [class*="title" i]'))
            .find(h => vis(h, 4, 4) && /^select (your )?travel dates$/i.test(clean(h.innerText)));
        for (let up = title; up && up !== document.body; up = up.parentElement) {
            const hasContinue = Array.from(up.querySelectorAll('button')).some(b => /^continue$/i.test(clean(b.innerText)));
            if (hasContinue) el = up;
            if (up.parentElement === document.body) break;
        }
    }
    if (!el) return null;
    document.querySelectorAll('[data-audit-date-dialog]').forEach(e => e.removeAttribute('data-audit-date-dialog'));
    el.setAttribute('data-audit-date-dialog', '1');
    const lines = (el.innerText || '').split('\n').map(clean).filter(Boolean);
    return { picked: lines.find(l => /^depart(ing|ure)?\b/i.test(l)) || null };
}"""
# The day's button in the open modal. A cell that carries the date itself (an ISO date in any attribute, or an
# aria-label naming the day) is taken first; otherwise the cell is found under its month's heading ('February
# 2027'), among the buttons before the next month's heading, by the first line of its text that is a day number.
JS_DAY_CELL = r"""({ iso, day, month, label }) => {
    const dialog = document.querySelector('[data-audit-date-dialog]');
    if (!dialog) return null;
    const labelled = new RegExp(label, 'i');
    const usable = b => vis(b, 4, 4) && !b.disabled && b.getAttribute('aria-disabled') !== 'true';
    const cells = Array.from(dialog.querySelectorAll('button, [role="gridcell"], [role="button"], td'));
    let el = cells.find(b => usable(b) && (Array.from(b.attributes).some(a => a.value.startsWith(iso))
        || labelled.test(b.getAttribute('aria-label') || '')));
    if (!el) {
        const heads = Array.from(dialog.querySelectorAll('*')).filter(h => h.children.length <= 1
            && /^[A-Za-z]+ \d{4}$/.test(clean(h.innerText)));
        const head = heads.find(h => clean(h.innerText).toLowerCase() === month.toLowerCase());
        if (!head) return null;
        const next = heads.find(h => clean(h.innerText) !== clean(head.innerText)
            && (head.compareDocumentPosition(h) & Node.DOCUMENT_POSITION_FOLLOWING));
        const after = (a, b) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
        el = Array.from(dialog.querySelectorAll('button')).filter(b => after(head, b) && (!next || after(b, next)))
            .find(b => (b.innerText || '').split('\n').map(clean).find(l => /^\d{1,2}$/.test(l)) === String(day));
        if (el && !usable(el)) { el.scrollIntoView({ block: 'center' }); if (!usable(el)) return { disabled: true }; }
    }
    if (!el) return null;
    document.querySelectorAll('[data-audit-day]').forEach(e => e.removeAttribute('data-audit-day'));
    el.setAttribute('data-audit-day', '1');
    el.scrollIntoView({ block: 'center' });
    const r = el.getBoundingClientRect();
    return { text: clean(el.innerText), x: r.left + r.width / 2, y: r.top + r.height / 2 };
}"""
# Scroll the modal's month list one screen on (its first scrollable block); false once it cannot scroll further.
JS_SCROLL_DATES = r"""() => {
    const dialog = document.querySelector('[data-audit-date-dialog]');
    if (!dialog) return false;
    const box = [dialog, ...dialog.querySelectorAll('*')].find(e => e.scrollHeight > e.clientHeight + 20
        && /auto|scroll/.test(getComputedStyle(e).overflowY));
    if (!box) return false;
    const before = box.scrollTop;
    box.scrollTop = before + box.clientHeight * 0.8;
    return box.scrollTop > before;
}"""
# The modal's Continue button (never its close ✕).
JS_DATE_CONTINUE = r"""() => {
    const dialog = document.querySelector('[data-audit-date-dialog]');
    const el = dialog && Array.from(dialog.querySelectorAll('button, [role="button"]'))
        .find(b => vis(b, 4, 4) && /^continue$/i.test(clean(b.innerText) || b.getAttribute('aria-label') || ''));
    if (!el) return null;
    document.querySelectorAll('[data-audit-date-continue]').forEach(e => e.removeAttribute('data-audit-date-continue'));
    el.setAttribute('data-audit-date-continue', '1');
    el.scrollIntoView({ block: 'nearest' });
    const r = el.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
}"""
SHOWN_DATE = re.compile(r"\b\d{1,2}\s+[A-Za-z]{3,9}\.?,?\s+\d{4}\b|\b[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}\b")


def date_text(day: date) -> str:
    """The date as Qantas writes it, e.g. '14 Feb 2027'."""
    return f"{day.day} {MONTHS[day.month - 1][:3]} {day.year}"


def date_label_pattern(day: date) -> str:
    """A regex (valid in Python and JavaScript) that matches `day` written out: '14 Feb 2027', 'Sun, 14 February
    2027', 'February 14, 2027', '14/02/2027' or '2027-02-14'."""
    month = MONTHS[day.month - 1]
    name = f"{month[:3]}(?:{month[3:]})?\\.?"
    return (
        rf"(?:^|[^0-9]){day.day}(?:st|nd|rd|th)?\s+{name},?\s+{day.year}(?:[^0-9]|$)"
        rf"|(?:^|[^A-Za-z]){name}\s+{day.day}(?:st|nd|rd|th)?,?\s+{day.year}(?:[^0-9]|$)"
        rf"|(?:^|[^0-9])0?{day.day}/0?{day.month}/{day.year}(?:[^0-9]|$)|{day.isoformat()}"
    )


def shows_date(text: str | None, day: date) -> bool:
    """True if `text` names `day` ('Sun, 14 Feb 2027' or '... currently set to 14 Feb 2027' for 14 Feb 2027)."""
    return bool(text and re.search(date_label_pattern(day), text, re.IGNORECASE))


def shown_date(text: str | None) -> str | None:
    """The date a control's text names ('29 Nov 2026' from 'Button to select travel dates, currently set to 29 Nov
    2026'), else the text itself; None for no text."""
    if not text:
        return None
    found = SHOWN_DATE.search(text)
    return found.group(0) if found else text


async def poll(page: Page, fn: str, arg: object = None, *, gone: bool = False) -> dict | None:
    """Run `fn` until it finds something (or, with `gone`, until it finds nothing) for up to DATE_DIALOG_WAIT_MS.

    Returns what it last found (None if nothing, or once gone).
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + DATE_DIALOG_WAIT_MS / 1000
    while True:
        found = await js(page, fn, arg)
        found = found if isinstance(found, dict) else None
        if (found is None) == gone or loop.time() >= deadline:
            return found
        await asyncio.sleep(DATE_POLL_MS / 1000)


async def read_date_field(page: Page) -> dict | None:
    """The form's travel-dates control as the page shows it ({text, x, y}); None if not found."""
    found = await js(page, JS_DATE_FIELD)
    return found if isinstance(found, dict) else None


async def date_dialog(page: Page) -> dict | None:
    """The open 'Select travel dates' modal ({picked}); None if it is not open."""
    found = await js(page, JS_DATE_DIALOG)
    return found if isinstance(found, dict) else None


async def open_date_dialog(page: Page, field: dict) -> dict | None:
    """Open the modal from the form's date control (a real click, a DOM click if nothing opened); the modal."""
    await click_at(page, field["x"], field["y"])
    dialog = await poll(page, JS_DATE_DIALOG)
    if dialog is None:
        await js(page, JS_DOM_CLICK, "data-audit-date", default=False)
        dialog = await poll(page, JS_DATE_DIALOG)
    return dialog


async def find_day_cell(page: Page, day: date) -> dict | None:
    """The day's button in the open modal, scrolling the months on until its month is rendered; None if not found."""
    arg = {"iso": day.isoformat(), "day": day.day, "month": f"{MONTHS[day.month - 1]} {day.year}",
           "label": date_label_pattern(day)}  # fmt: skip
    for _ in range(MAX_DATE_SCROLLS + 1):
        cell = await js(page, JS_DAY_CELL, arg)
        if isinstance(cell, dict):
            return cell
        if not await js(page, JS_SCROLL_DATES, default=False):
            return None
        await settle(page, TRIP_SETTLE_MS)
    return None


async def pick_day(page: Page, day: date) -> str | None:
    """Click `day` in the open modal; None once it is clicked, else why it could not be (no cell, or disabled).

    The modal's header is only a hint: on the live site it is two elements ('Depart BNE' / 'Sun, 14 Feb 2027')
    and reads as its first line. A header that does not show the day is logged, not a failure: Continue, and the
    form's date field after it, decide whether the date was set.
    """
    cell = await find_day_cell(page, day)
    if cell is None:
        return f"no {date_text(day)} button in the date picker"
    if cell.get("disabled"):
        return f"{date_text(day)} cannot be picked (the day is disabled)"
    await click_at(page, cell["x"], cell["y"])
    await settle(page, TRIP_SETTLE_MS)
    dialog = await date_dialog(page)
    if dialog and not shows_date(dialog.get("picked"), day):  # React ignored the mouse click: try a DOM click
        await js(page, JS_DOM_CLICK, "data-audit-day", default=False)
        await settle(page, TRIP_SETTLE_MS)
        dialog = await date_dialog(page)
    if dialog and not shows_date(dialog.get("picked"), day):
        log.debug("Clicked %s but the picker's header reads %r", date_text(day), dialog.get("picked"))
    return None


async def confirm_dates(page: Page) -> str | None:
    """Click the modal's Continue and wait for it to close; None once closed, else why it did not."""
    button = await js(page, JS_DATE_CONTINUE)
    if not isinstance(button, dict):
        return "no Continue button in the date picker"
    await click_at(page, button["x"], button["y"])
    if await poll(page, JS_DATE_DIALOG, gone=True) is not None:
        await js(page, JS_DOM_CLICK, "data-audit-date-continue", default=False)
        if await poll(page, JS_DATE_DIALOG, gone=True) is not None:
            return "clicked Continue but the date picker stayed open"
    await settle(page, TRIP_SETTLE_MS)
    return None


async def set_depart_date(page: Page, day: date) -> bool:
    """Set the search form's departure date to `day` in code, without the model. True if the form now shows it.

    Opens the date picker (or works in the one already open), clicks the day, then Continue, which is what sets
    the date. The model cannot be relied on here: it clicked the day and then the form's search button hidden
    behind the still-open picker, over and over, and never chose Continue.
    """
    wanted = date_text(day)
    try:
        dialog = await date_dialog(page)
        field = await read_date_field(page)
        if dialog is None:
            if field is None:
                log.info("  Departure date: no travel-dates control found in the page; leaving it to the model")
                return False
            if shows_date(field["text"], day):
                log.info(f"  Departure date: already '{wanted}'")
                return True
            dialog = await open_date_dialog(page, field)
            if dialog is None:
                log.info("  Departure date: the date picker did not open; leaving it to the model")
                return False
        problem = await pick_day(page, day) or await confirm_dates(page)
        if problem:
            log.info(f"  Departure date: {problem}; leaving it to the model")
            return False
        after = await read_date_field(page)
    except BROWSER_ERRORS as error:  # a failed helper leaves the date to the model, as before
        log.info(f"  Departure date: could not be set in the page ({error}); leaving it to the model")
        return False
    done = bool(after and shows_date(after["text"], day))
    log.info(
        f"  Departure date: set to '{wanted}'"
        if done
        else f"  Departure date: the form shows '{shown_date((after or field or {}).get('text'))}' after choosing '{wanted}'"
    )
    return done


async def page_depart_date(page: Page) -> str | None:
    """The date the form's own travel-dates control shows ('29 Nov 2026'); None if it can't be read.

    Read from the form, not the picker: the picker's subheading shows a day that is not set until Continue.
    """
    try:
        await date_dialog(page)  # marks an open picker, so that none of its buttons is taken for the form's
        field = await read_date_field(page)
    except BROWSER_ERRORS as error:  # a failed read falls back to the extraction's date
        log.debug("Date read failed: %s", error)
        return None
    return shown_date(field["text"]) if field else None


def with_depart_date(state: SearchState, shown: str | None) -> SearchState:
    """`state` with the date the form's control shows in place of the extraction's reading, where the DOM has one."""
    return state.model_copy(update={"depart_date": shown}) if shown else state


def airports_set(airports: dict[str, str | None], trip: Trip) -> bool:
    return shows_code(airports.get("origin"), trip.origin) and shows_code(airports.get("destination"), trip.destination)


def form_rules(trip_type: str | None, airports: dict[str, str | None], trip: Trip, depart: str | None = None) -> str:
    """What the model is told to leave alone: the parts of the form already set in code."""
    rules = ""
    if trip_type == "one_way":
        rules += " The trip type is already set to One way: do not touch the trip-type control."
    if airports_set(airports, trip):
        rules += (
            f" The departure and arrival airports are already set to {trip.origin} and {trip.destination}: "
            "do not touch, clear or swap them."
        )
    if shows_date(depart, trip.depart):
        rules += f" The departure date is already set to {date_text(trip.depart)}: do not open the date picker."
    return rules + POINTS_RULE


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
    """The parts of the search form set and read in code, without the model: trip type, airports and date."""

    stagehand: Stagehand
    page: Page
    trip: Trip
    question: str
    helper_works: bool = True  # False once set_airports failed: the airports are then the model's, not fought over
    date_works: bool = True  # False once set_depart_date failed: likewise for the date

    async def prepare(self) -> None:
        """Set the trip type, then both airports, then the departure date."""
        await set_one_way(self.page)
        self.helper_works = await set_airports(self.page, self.trip)
        self.date_works = await set_depart_date(self.page, self.trip.depart)

    async def keep_date(self, depart: str | None) -> str | None:
        """Set the date again if it is not set or the picker is open (while the helper works); what it shows now.

        The model is never handed a page with the picker open: the day it shows there is not set until Continue.
        """
        if not self.date_works:
            if await date_dialog(self.page) is not None:  # never hand the model the open picker
                problem = await confirm_dates(self.page)
                if problem:
                    log.info(f"  Departure date: {problem}")
                return await page_depart_date(self.page)
            return depart
        if shows_date(depart, self.trip.depart) and await date_dialog(self.page) is None:
            return depart
        self.date_works = await set_depart_date(self.page, self.trip.depart)
        return await page_depart_date(self.page)

    async def keep_airports(self, airports: dict[str, str | None]) -> dict[str, str | None]:
        """Set the airports back if the last action changed one (while the helper works); what they show now."""
        if not self.helper_works or airports_set(airports, self.trip):
            return airports
        self.helper_works = await set_airports(self.page, self.trip)
        return await page_airports(self.page)

    async def read(self) -> tuple[SearchState, str | None, dict[str, str | None]]:
        """The search state, with the trip type, airports and date the DOM shows in place of the extraction's."""
        state = (await self.stagehand.extract(self.question, SearchState)).data
        trip_type, airports = await page_trip_type(self.page), await page_airports(self.page)
        state = with_depart_date(state, await page_depart_date(self.page))
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
    recent: list[tuple[str, tuple, str]] = []  # the last few (control, what the search held, description)
    repeats = 0
    wrong_pages = 0
    hint = ""
    for _ in range(MAX_SEARCH_STEPS):
        if state.blocked_reason:
            raise Blocked(state.blocked_reason)
        if state.on_results_page or await page_shows_results(page):
            state = await results_state(page, state)
            wrong = wrong_results(state, trip, await page.url())
            if wrong is None:
                return
            wrong_pages = await start_again(stagehand, page, wrong, wrong_pages)
            await form.prepare()
            state, trip_type, airports = await form.read()
            held, last, repeats, hint, recent = None, None, 0, "", []
            continue
        if state.loading or nothing_shown(state):
            # A submitted search renders slowly, and a half-rendered page reads as nothing at all; acting on it
            # is how a search gets restarted. Only the visible form is acted on.
            state = await wait_for_results(stagehand, page, question, trip, state)
            if state.loading or nothing_shown(state):
                break
            continue
        state, points = await keep_cash_search(page, state)
        if (trip_type or state.trip_type) != "one_way" and await set_one_way(page):
            trip_type = "one_way"
        airports = await form.keep_airports(airports)
        state = with_depart_date(state, await form.keep_date(state.depart_date))
        state = with_airports(with_trip_type(state, trip_type), airports)
        step = await llm_act(
            stagehand,
            f"Goal: show the list of flights for {goal}. Take the single next action that moves towards that goal. "
            f"The search currently holds: trip type={state.trip_type}, from={state.origin}, to={state.destination}, "
            f"date={state.depart_date}, guests={state.guests}, using points={state.using_points}. Correct whatever "
            "does not match the goal (opening the search form first if it is hidden, and choosing airports and dates "
            f"from the site's own lists or calendar), then submit the search once everything matches."
            f"{form_rules(trip_type, airports, trip, state.depart_date)}{hint}{points}",
            follow_up=True,  # a custom dropdown needs opening before its option can be picked
            page=page,
        )
        await settle(page, SETTLE_MS)
        state, trip_type, airports = await form.read()
        log.info(f"  {step.description} -> {state.reason}")
        held = search_held(state, held)
        now = (step.selector, held)
        repeats = repeats + 1 if now == last else 0
        last = now
        paging = is_month_navigation(step.description)
        recent = [*recent[-(ALTERNATING_STEPS - 1) :], (step.selector, held, step.description)]
        if repeats >= (MAX_MONTH_REPEATS if paging else MAX_STUCK_REPEATS):
            raise FlowChanged(
                f"Stuck on the flight search: '{step.description}' was acted on {repeats + 1} times in a row "
                f"and the search did not change (trip type={state.trip_type}, from={state.origin}, to={state.destination})"
            )
        if (pair := alternating(recent)) and not any(is_month_navigation(d) for d in pair):
            raise FlowChanged(
                f"Stuck on the flight search: alternating between '{pair[0]}' and '{pair[1]}' for "
                f"{ALTERNATING_STEPS} steps and the search did not change (date={state.depart_date})"
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
    state = await last_look_for_results(page, state)
    wrong = wrong_results(state, trip, await page.url()) if state.on_results_page else None
    if not state.on_results_page or wrong:
        raise FlowChanged(f"Couldn't reach the flight results page: {wrong or state.reason or 'no reason given'}")


# A Qantas Points (award) price, e.g. '16,600 PTS + $58' or '16,600PTS+ $58': never bookable as a guest.
# No \b before 'pts': the page often runs it into the number ('600PTS'); the lookbehind keeps 'receipts' out.
POINTS_PRICE = re.compile(r"(?<![a-z])pts\b|points", re.IGNORECASE)


def cash_price(price: str | None) -> float | None:
    """The amount of a cash price; None for a points price or no price at all."""
    if not price or POINTS_PRICE.search(price):
        return None
    return parse_price(price)


def rank_flights(flights: list[Flight]) -> list[Flight]:
    """Available flights on a cash fare, cheapest first: the direct ones if there are any. Award-only never count."""
    available = [f for f in flights if not f.sold_out and not f.award_only and cash_price(f.price) is not None]
    direct = [f for f in available if f.stops == 0]
    return sorted(direct or available, key=lambda f: cash_price(f.price) or 0.0)


def choose_flight(flights: list[Flight]) -> Flight | None:
    """Cheapest available flight on a cash fare, preferring direct services."""
    return next(iter(rank_flights(flights)), None)


def choose_fare(fares: list[FareOption]) -> FareOption | None:
    """Cheapest cash fare in the open 'Select your fare' panel."""
    priced = [f for f in fares if cash_price(f.price) is not None]
    return min(priced, key=lambda f: cash_price(f.price) or 0.0, default=None)


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


# Controls the selection steps must never act on, whatever the model returns for them.
NEVER_CLICK = re.compile(
    r"log ?in|sign ?in|start again|change (this flight|search)|rewards?\b|promo|(?<![a-z])pts\b|points"
    r"|use cash|upgrade",
    re.IGNORECASE,
)
# After a click, how long a step's check keeps re-reading the page before it counts as failed: the Angular app
# can take several seconds to swap views (Continue to the Options page did, in the 14:08 run).
STEP_CHECK_MS = 15_000
STEP_POLL_MS = 500
NOT_ECONOMY = re.compile(r"business|premium economy|first class", re.IGNORECASE)

JS_BANNER = r"""() => /about our prices/i.test(document.body ? document.body.innerText : '')"""
# Scroll the card of the chosen flight into view: the smallest block showing both its times (and number).
JS_SCROLL_TO_FLIGHT = r"""words => {
    const all = Array.from(document.querySelectorAll('li, article, section, div, [role="row"]'))
        .filter(el => vis(el, 200, 40) && words.every(w => txt(el).includes(w)));
    if (!all.length) return false;
    all.sort((a, b) => txt(a).length - txt(b).length);
    all[0].scrollIntoView({ block: 'center' });
    return true;
}"""
JS_SELECTION_PAGE = r"""() => {
    const text = document.body ? document.body.innerText : '';
    const heading = document.querySelector('h1, [role="heading"][aria-level="1"], h2');
    const selects = Array.from(document.querySelectorAll('button, a, [role="button"]'))
        .filter(el => vis(el) && /^select$/i.test(clean(el.innerText)));
    return {
        url: location.href,
        heading: heading ? clean(heading.innerText) : '',
        fare_panel: /select your fare/i.test(text) && selects.length > 0,
        trip_summary: /your trip/i.test(text) && /flight subtotal/i.test(text),
        summary_page: /flight subtotal/i.test(text) && /change this flight/i.test(text)
            && /login to skip to payment/i.test(text),
        results: /select your fare|economy cabin/i.test(text),
        options: /need travel insurance\?/i.test(text) || /^options$/i.test(heading ? clean(heading.innerText) : ''),
    };
}"""


class SelectionFailed(FlowChanged):
    """One of the three selection steps did not do what it should, even after a retry."""

    def __init__(self, step: int, message: str, item: dict) -> None:
        super().__init__(message)
        self.step, self.item = step, item
        # Steps 1 and 2 never got a fare into the trip; step 3 has one but could not leave the trip summary.
        self.status = Status.NO_CHECKOUT if step == 3 else Status.ADD_TO_CART_FAILED
        self.reason = STATUS_REASONS[self.status]


async def selection_page(page: Page) -> dict:
    return await js(page, JS_SELECTION_PAGE, default={}) or {}


async def pause(ms: int) -> None:
    await asyncio.sleep(ms / 1000)


async def wait_for_check(page: Page, check: Callable[[dict], bool], ms: int) -> dict:
    """Re-read the page every STEP_POLL_MS until `check` passes or `ms` runs out; the last reading."""
    state = await selection_page(page)
    for _ in range(ms // STEP_POLL_MS):
        if check(state):
            break
        await pause(STEP_POLL_MS)
        state = await selection_page(page)
    return state


async def click_one(
    stagehand: Stagehand,
    instruction: str,
    refuse: re.Pattern[str] = NEVER_CLICK,
    target: Callable[[object], None] | None = None,
) -> Acted:
    """Observe and click the element the instruction names, unless the model returned a forbidden control.

    The step itself is fixed in code: the model only picks which element. A result that looks like a login,
    'Start again', 'Change this flight', Rewards or points control is never acted on and counts as a miss.
    """
    found = await stagehand.observe(instruction + " Return nothing if there is no such element.")
    candidates = list(found.data or [])
    if not candidates:
        return Acted(False, "no matching element found")
    first = candidates[0]
    if target is not None:
        target(first)
    description = str(first.description or "")
    if refuse.search(description):
        log.info(f"    Refused to click {description!r} ({first.selector})")
        return Acted(False, f"refused: {description}", str(first.selector or ""))
    try:
        return await act_on(stagehand, first)
    except BROWSER_ERRORS as error:  # e.g. "Could not find an element for the given xPath": the page re-rendered
        return Acted(False, f"click failed ({description}): {error}", str(first.selector or ""))


# The fare columns' SELECT buttons, for the code to choose from (pick_fare_select). Every button whose text says
# "select" (so "LOG IN TO SELECT" too) is listed with the text of its fare column: the largest ancestor holding no
# other visible such button. The first visible SELECT is scrolled to the middle of the screen first, so each
# button's box, whether its centre is in view, and whether elementFromPoint there lands on it (not on a sticky
# footer, a banner or a hidden duplicate) are read as the mouse click will meet them. Each is marked with
# data-audit-fare-candidate.
JS_FARE_BUTTONS = r"""() => {
    const selectish = el => /select/i.test(clean(el.innerText));
    const all = () => Array.from(document.querySelectorAll('button, a, [role="button"]')).filter(selectish);
    const buttons = all();
    document.querySelectorAll('[data-audit-fare-candidate]').forEach(e => e.removeAttribute('data-audit-fare-candidate'));
    const first = buttons.find(b => vis(b, 4, 4) && /^select$/i.test(clean(b.innerText)));
    if (first) first.scrollIntoView({ block: 'center' });
    return buttons.map((b, i) => {
        b.setAttribute('data-audit-fare-candidate', String(i));
        let column = b;
        for (let up = b.parentElement; up && up !== document.body; up = up.parentElement) {
            const others = Array.from(up.querySelectorAll('button, a, [role="button"]'))
                .filter(el => selectish(el) && vis(el, 4, 4));
            if (others.length > 1) break;
            column = up;
        }
        const r = b.getBoundingClientRect();
        const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
        const inView = r.width > 0 && r.height > 0 && cx >= 0 && cy >= 0 && cx <= innerWidth && cy <= innerHeight;
        const hit = inView ? document.elementFromPoint(cx, cy) : null;
        return { i, text: clean(b.innerText), column: clean(txt(column)).slice(0, 300), visible: vis(b, 4, 4),
                 box: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
                 in_view: inView, hit: !!hit && (hit === b || b.contains(hit)) };
    });
}"""
# Mark the chosen candidate as the SELECT to click (data-audit-fare-select) and return its centre.
JS_MARK_FARE_SELECT = r"""i => {
    const el = document.querySelector(`[data-audit-fare-candidate="${i}"]`);
    if (!el) return null;
    document.querySelectorAll('[data-audit-fare-select]').forEach(e => e.removeAttribute('data-audit-fare-select'));
    el.setAttribute('data-audit-fare-select', '1');
    const r = el.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
}"""
FARE_SELECT_MARK = "[data-audit-fare-select]"
STEP2_CHECK_MS = 20_000  # adding the fare to the trip is a server round trip
# What the page says when it will not add a fare, for step2_diagnostics: alerts and live regions, and short texts
# that read like an error.
JS_DIAGNOSTICS = r"""() => {
    const says = /sorry|unavailable|error|try again|no longer/i;
    const own = '[role="alert"], [aria-live], [role="status"]';
    const seen = new Set(), messages = [];
    for (const el of document.querySelectorAll(`${own}, p, div, span, li`)) {
        if (messages.length >= 15) break;
        if (!vis(el, 2, 2)) continue;
        const t = clean(txt(el));
        if (!t || t.length > 300 || seen.has(t)) continue;
        if (!el.matches(own) && (!says.test(t) || Array.from(el.children).some(c => clean(txt(c)) === t))) continue;
        seen.add(t);
        messages.push(t);
    }
    return { url: location.href, messages };
}"""


def fare_brand(fare_name: str) -> re.Pattern[str]:
    """The fare's brand words as a pattern: 'ECONOMY RED E-DEAL' -> red, e, deal (the cabin word is dropped)."""
    words = [w for w in re.findall(r"[a-z0-9]+", fare_name.lower()) if w != "economy"] or ["economy"]
    return re.compile(r"\W*".join(map(re.escape, words)), re.IGNORECASE)


def pick_fare_select(candidates: list[dict], fare_name: str) -> dict | None:
    """The SELECT button to click for `fare_name`: text exactly 'SELECT' (never 'LOG IN TO SELECT'), in the column
    naming that fare, visible, in view, and the element a click at its centre would land on."""
    brand = fare_brand(fare_name)
    for candidate in candidates:
        box = candidate.get("box") or [0, 0, 0, 0]
        if (
            re.fullmatch(r"select", str(candidate.get("text") or "").strip(), re.IGNORECASE)
            and candidate.get("visible")
            and box[2] > 0
            and box[3] > 0
            and candidate.get("in_view")
            and candidate.get("hit")
            and brand.search(str(candidate.get("column") or ""))
        ):
            return candidate
    return None


async def find_fare_select(page: Page, fare_name: str) -> dict | None:
    """Find and mark the SELECT button of `fare_name` in the open fare panel; its candidate entry and centre."""
    candidates = await js(page, JS_FARE_BUTTONS, default=[]) or []
    chosen = pick_fare_select(candidates if isinstance(candidates, list) else [], fare_name)
    if chosen is None:
        return None
    centre = await js(page, JS_MARK_FARE_SELECT, chosen["i"], default=None)
    return {**chosen, **centre} if isinstance(centre, dict) else None


def console_text(event: object) -> str:
    """One console message from Stagehand's Runtime.consoleAPICalled event, e.g. 'error: Failed to add fare'."""
    params = getattr(event, "params", None)
    raw = getattr(params, "root", params)
    root = raw if isinstance(raw, dict) else {}
    args = root.get("args")
    parts = [
        str(a.get("value", a.get("description", "")))
        for a in (args if isinstance(args, list) else [])
        if isinstance(a, dict)
    ]
    return f"{root.get('type', 'log')}: {' '.join(parts)}"[:300]


@contextlib.asynccontextmanager
async def console_messages(page: Page) -> AsyncIterator[list[str]]:
    """Collect the page's console messages while the block runs; empty if the page cannot report them."""
    messages: list[str] = []
    subscription = None
    on = getattr(page, "on", None)
    if on is not None:
        try:
            subscription = await on("console", lambda event: messages.append(console_text(event)))
        except BROWSER_ERRORS as error:  # diagnostics only
            log.debug("Could not listen to the console: %s", error)
    try:
        yield messages
    finally:
        if subscription is not None:
            with contextlib.suppress(*BROWSER_ERRORS):
                await subscription.unsubscribe()


def fare_panel_open(state: dict) -> bool:
    return bool(state.get("fare_panel"))


def fare_added(state: dict) -> bool:
    return bool(state.get("trip_summary"))


async def step2_diagnostics(page: Page, console: list[str]) -> dict:
    """Why the site may have ignored SELECT: the URL, any alert or error text shown, the console messages."""
    seen = await js(page, JS_DIAGNOSTICS, default={}) or {}
    return {
        "url": seen.get("url") or await page.url(),
        "messages": seen.get("messages") or [],
        "console": console[-30:],
    }


@dataclass
class FareSelectClick:
    """Step 2's click: the chosen fare's SELECT, found and clicked in code (a mouse click, then a DOM click); the
    model's choice of element only if no such button is found. What it did goes into `details`.

    A retry starts clean: Escape, the fare panel reopened if that closed it, then the same column's SELECT found
    again (never a different element).
    """

    stagehand: Stagehand
    page: Page
    fare_name: str | None
    open_fares: str
    economy_only: re.Pattern[str]
    select_instruction: str
    details: dict

    async def __call__(self, attempt: int) -> Acted:
        if attempt == 2:
            await self.start_clean()
        found = await find_fare_select(self.page, self.fare_name) if self.fare_name else None
        if found is None:
            self.details["code_click"] = False
            log.info("    No SELECT button found in code for the chosen fare; asking the model")
            return await click_one(self.stagehand, self.select_instruction)
        self.details.update(
            code_click=True, button_text=found["text"], box=found["box"], element_from_point=found["hit"]
        )
        log.info(
            f"    Clicking SELECT of '{self.fare_name}' in code: '{found['text']}' at box {found['box']} "
            f"(elementFromPoint matches: {found['hit']})"
        )
        await click_at(self.page, found["x"], found["y"])
        await settle(self.page, SETTLE_MS)
        if fare_added(await wait_for_check(self.page, fare_added, STEP2_CHECK_MS)):
            return Acted(True, "code click on SELECT (mouse)", FARE_SELECT_MARK)
        log.info("    The mouse click on SELECT did not add the fare; trying a DOM click")
        self.details["dom_click"] = bool(await js(self.page, JS_DOM_CLICK, "data-audit-fare-select", default=False))
        return Acted(True, "code click on SELECT (mouse, then DOM)", FARE_SELECT_MARK)

    async def start_clean(self) -> None:
        if fare_panel_open(await selection_page(self.page)):
            with contextlib.suppress(*BROWSER_ERRORS):
                await self.page.key_press("Escape")
            await settle(self.page, TRIP_SETTLE_MS)
            self.details["escape"] = True
        if not fare_panel_open(await selection_page(self.page)):
            log.info("    The fare panel is closed; opening it again")
            reopened = await click_one(self.stagehand, self.open_fares, self.economy_only)
            await settle(self.page, SETTLE_MS)
            self.details["reopened"] = fare_panel_open(await wait_for_check(self.page, fare_panel_open, STEP_CHECK_MS))
            self.details["reopen_click"] = reopened.description


async def bring_flight_into_view(stagehand: Stagehand, page: Page, flight: Flight) -> None:
    """Before step 1: get the info banner off the first rows, and bring the flight's card on screen."""
    if await js(page, JS_BANNER, default=False):
        await try_act(stagehand, "Click the close 'x' button of the teal 'About our prices' banner.")
    words = [w for w in (flight.departs, flight.arrives) if w]
    if words:
        await js(page, JS_SCROLL_TO_FLIGHT, words, default=False)


async def select_flight(stagehand: Stagehand, page: Page, flight: Flight, out: Path) -> dict:
    """Open the flight's fares, select the cheapest, then click Continue: three fixed steps, each checked.

    Each step gets one retry. A step that still fails raises SelectionFailed naming it. The item is
    'added' once the fare shows in the 'Your trip' summary (step 2).
    """
    name, described = describe_flight(flight)
    item = new_item(name, flight.price)
    item["selection_steps"] = []

    async def run_step(
        step: int,
        label: str,
        instruction: str,
        check: Callable[[dict], bool],
        failure: str,
        refuse: re.Pattern[str] = NEVER_CLICK,
        click: Callable[[int], Awaitable[Acted]] | None = None,
        wait_ms: int = STEP_CHECK_MS,
        details: dict | None = None,
    ) -> dict:
        """`click(attempt)` replaces the model's click; whatever it puts in `details` is saved with the attempt."""
        for attempt in (1, 2):
            before = await fingerprint(page)
            if attempt == 2 and check(state := await selection_page(page)):
                # The first click worked after all, only later than the check waited: never click it twice.
                acted, ok = Acted(False, "no second click: the first one had taken effect"), True
            else:
                acted = await (click(attempt) if click else click_one(stagehand, instruction, refuse))
                await settle(page, SETTLE_MS)
                # A refused or missing element changed nothing; a click (even one that failed because the page
                # re-rendered under it) may still be taking effect, so its check waits for the page.
                tried = acted.success or acted.description.startswith("click failed")
                state = await wait_for_check(page, check, wait_ms if tried else 0)
                ok = check(state)
            after = await fingerprint(page)
            record = {
                "step": step,
                "name": label,
                "attempt": attempt,
                "description": acted.description,
                "selector": acted.selector,
                "acted": acted.success,
                "page_changed": before != after,
                "passed": ok,
                **(details or {}),
            }
            if details:
                details.clear()
            item["selection_steps"].append(record)
            log.info(
                f"    Selection step {step} ({label}), attempt {attempt}: {acted.description} [{acted.selector}] "
                f"-> {'ok' if ok else 'check failed'}{'' if ok or before != after else ' (page unchanged)'}"
            )
            await shot(page, out / f"select-{step}-{label}.png")
            if ok:
                return state
        if step < 3:  # after step 2 the fare is in the trip, whatever Continue does
            item["status"] = "add_failed"
        raise SelectionFailed(step, failure, item)

    await bring_flight_into_view(stagehand, page, flight)

    # Step 1: open the flight's fares from its Economy price.
    price = f" (e.g. '{flight.price}')" if flight.price else ""
    open_fares = (
        f"Click the Economy Cabin price link{price} in the card of {described}. Not the Business Cabin price, "
        "not the date strip at the top, not the Rewards toggle and not a points (PTS) price."
    )
    economy_only = re.compile(f"{NEVER_CLICK.pattern}|{NOT_ECONOMY.pattern}", re.IGNORECASE)
    await run_step(
        1,
        "fares",
        open_fares,
        fare_panel_open,
        "Fare panel never opened: no 'Select your fare' panel with a SELECT button appeared after clicking the "
        f"Economy price of {name}",
        economy_only,
    )

    # Step 2: read the fare columns, pick the cheapest cash fare in code, click its SELECT.
    panel = (
        await stagehand.extract(
            "List every fare column of the open 'Select your fare' panel (e.g. 'Economy Saver', 'Economy Flex') "
            "with its cash price. Ignore Qantas Points (PTS) prices and the comparison rows below.",
            FarePanel,
        )
    ).data
    fare = choose_fare(panel.fares)
    log.info(f"    Fares shown: {'; '.join(f'{f.fare_name} {f.price}' for f in panel.fares) or 'none read'}")
    target = (
        f"the SELECT button under the '{fare.fare_name}' fare priced {fare.price}"
        if fare
        else "the SELECT button under the cheapest cash fare"
    )
    select_instruction = f"Click {target} in the open 'Select your fare' panel."
    details: dict = {}
    click_fare_select = FareSelectClick(
        stagehand, page, fare.fare_name if fare else None, open_fares, economy_only, select_instruction, details
    )

    async with console_messages(page) as console:
        try:
            await run_step(
                2,
                "trip",
                select_instruction,
                fare_added,
                "Fare not added to trip: no 'Your trip' summary with a flight subtotal appeared after clicking SELECT",
                click=click_fare_select,
                wait_ms=STEP2_CHECK_MS,
                details=details,
            )
        except SelectionFailed:
            item["step2_diagnostics"] = await step2_diagnostics(page, console)
            log.info(f"    Step 2 diagnostics: {item['step2_diagnostics']}")
            raise
    selected = (
        await stagehand.extract(
            f"We selected {described}. Read the 'Your trip' summary: the flight, fare brand, fare price, the flight "
            "subtotal and any cart total.",
            SelectedFare,
        )
    ).data
    item["selected_variant"] = selected.fare_name or (fare.fare_name if fare else None)
    item["selected_price_display"] = selected.fare_price or (fare.price if fare else None)
    item["selected_price"] = parse_price(item["selected_price_display"])
    item["cart_total_display"] = selected.subtotal or selected.running_total
    item["status"] = "added"
    log.info(
        f"    Fare added: {item['selected_variant']} {item['selected_price_display']} "
        f"(subtotal {item['cart_total_display']})"
    )

    # Step 3: Continue, in the sticky footer, never the login shortcut.
    trip_page = await selection_page(page)

    def left_trip_summary(state: dict) -> bool:
        """The 'Your trip' page is gone. Not 'Change this flight' or 'Subtotal' alone: the Options sidebar has both."""
        if not state:
            return False
        if state.get("options") or not state.get("summary_page"):
            return True
        return bool(state.get("heading")) and state.get("heading") != trip_page.get("heading")

    await run_step(
        3,
        "continue",
        "Click the red CONTINUE button in the sticky footer bar at the bottom of the page. Do not click "
        "'Login to skip to payment', 'Start again' or 'Change this flight'.",
        left_trip_summary,
        "Continue did not advance: the 'Your trip' summary was still showing after clicking CONTINUE",
    )
    return item


MAX_FLIGHTS_TRIED = 2  # the cheapest, and once the next-cheapest if the first one's fare cannot be added


async def select_a_flight(
    stagehand: Stagehand, page: Page, flights: list[Flight], result: SiteResult, price_basis: str | None
) -> dict:
    """Select the cheapest flight's fare; if the site will not add it (step 2), the next-cheapest once.

    Every attempt is kept in result.items, so the report shows a fare that could not be selected.
    """
    for index, flight in enumerate(flights[:MAX_FLIGHTS_TRIED]):
        log.info(
            f"  Selecting: {flight.flight_number or ''} {flight.departs or ''}-{flight.arrives or ''} ({flight.price})"
        )
        try:
            item = await select_flight(stagehand, page, flight, result.out_dir)
        except SelectionFailed as failed:  # keep what was clicked; the runner records the step's message
            failed.item["price_basis"] = price_basis
            result.items.append(failed.item)
            if failed.step == 2 and index + 1 < min(len(flights), MAX_FLIGHTS_TRIED):
                log.info(f"  {failed}; trying the next-cheapest flight")
                continue
            raise
        item["price_basis"] = price_basis
        result.items.append(item)
        return item
    raise AssertionError("unreachable: the loop returns or raises")  # pragma: no cover


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


async def fill_passenger_details(stagehand: Stagehand, page: Page, passenger: Passenger) -> bool:
    """Fill the required guest fields, two passes at most (a first pass can reveal more fields).

    True once a reading finds no required field left empty; False if the second pass still found some.
    """
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
            return True
        for entry in form.fields[:20]:
            planned = passenger_field_instruction(entry, passenger)
            if planned is None:
                log.debug("No passenger detail for field %r (%s)", entry.label, entry.detail)
                continue
            instruction, variables = planned
            typed: dict[str, Any] = {"variables": variables} if variables else {}
            try:
                step = await llm_act(
                    stagehand,
                    instruction,
                    follow_up=not variables,
                    page=page,  # a custom dropdown needs opening before the option can be picked
                    **typed,
                )
                log.info(f"    Guest details: {step.description}")
            except BROWSER_ERRORS as error:  # one field failing must not stop the rest; the step check reports it
                log.warning(f"    Guest details: could not fill '{entry.label}' ({type(error).__name__}: {error})")
            await settle(page, 400)
    return False


# The sticky footer's red "CONTINUE >" (in the navigation-ribbon bar at the bottom of each booking page). Only a
# button inside that bar (or a bottom nav / footer) counts, so no "Continue" elsewhere on the page is taken. With
# `mark`, it is marked with data-audit-continue and its centre returned. Null if there is none.
JS_CONTINUE = r"""mark => {
    const bar = 'navigation-ribbon, nav, footer, [class*="ribbon" i], [class*="footer" i]';
    const found = Array.from(document.querySelectorAll('button, a, [role="button"]'))
        .filter(el => vis(el) && /^continue\b/i.test(clean(el.innerText)) && el.closest(bar));
    if (!found.length) return null;
    const el = found[found.length - 1];
    const out = { text: clean(el.innerText) };
    if (mark) {
        document.querySelectorAll('[data-audit-continue]').forEach(e => e.removeAttribute('data-audit-continue'));
        el.setAttribute('data-audit-continue', '1');
        const r = el.getBoundingClientRect();
        out.x = r.left + r.width / 2;
        out.y = r.top + r.height / 2;
    }
    return out;
}"""
CONTINUE_MARK = "[data-audit-continue]"
# What the model may never click on the passenger details page: the loyalty sign-up, the travel arranger and
# phone-number extras, and the "Red Email" marketing box, on top of the controls never clicked anywhere.
PASSENGER_NEVER_CLICK = re.compile(
    rf"{NEVER_CLICK.pattern}|\bjoin\b|sign ?up|travel arranger|red email|multiple phone", re.IGNORECASE
)


async def left_page(page: Page, before: dict) -> bool:
    """Poll up to STEP_CHECK_MS for the URL or the main heading to differ from `before` (a selection_page)."""
    for poll_index in range(STEP_CHECK_MS // STEP_POLL_MS + 1):
        if poll_index:
            await pause(STEP_POLL_MS)
        now = await selection_page(page)
        if now and (now.get("url") != before.get("url") or (now.get("heading") or "") != (before.get("heading") or "")):
            return True
    return False


async def click_continue(page: Page) -> bool:
    """Click the sticky footer's CONTINUE in code: a mouse click, then a DOM click. True once the page moved on."""
    found = await js(page, JS_CONTINUE, True, default=None)
    if not isinstance(found, dict) or "x" not in found:
        return False
    before = await selection_page(page)
    await click_at(page, found["x"], found["y"])
    if await left_page(page, before):
        return True
    log.info("  CONTINUE: the page did not move on after the click; trying a DOM click")
    await js(page, JS_DOM_CLICK, "data-audit-continue")
    return await left_page(page, before)


# The travel insurance question ("Need Travel Insurance?" on the Options page): an insurance card (SELECT, or
# USE CASH / USE POINTS) and the checkbox "No, I don't want travel insurance". Continue is refused, with the warning
# "You need to make a travel insurance selection before you can proceed", until one is chosen. Read by the
# checkbox's text, not the URL: the same question can come on a later step. With `mark`, the control to click
# (its label if it has one: a mouse click on the label is what an Angular checkbox most reliably takes) is marked
# with data-audit-insurance, scrolled into view and its centre returned.
JS_INSURANCE = r"""mark => {
    const want = /no,?\s*i\s*don.?t\s*want\s*travel\s*insurance/i;
    const text = document.body ? document.body.innerText : '';
    const out = { question: /need travel insurance\?/i.test(text),
                  warning: /you need to make a travel insurance selection/i.test(text),
                  found: false, checked: false };
    const labelFor = el => el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
    const labelOf = el => {
        const parts = [el.getAttribute('aria-label'), el.closest('label') && txt(el.closest('label'))];
        const own = labelFor(el);
        if (own) parts.push(txt(own));
        for (const id of (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)) {
            const l = document.getElementById(id);
            if (l) parts.push(txt(l));
        }
        if (el.parentElement && txt(el.parentElement).length < 200) parts.push(txt(el.parentElement));
        if (el.matches('label, [role="checkbox"]')) parts.push(txt(el));
        return clean(parts.filter(Boolean).join(' '));
    };
    const boxOf = el => el.matches('input[type="checkbox"], [role="checkbox"]') ? el
        : (el.control || el.querySelector('input[type="checkbox"], [role="checkbox"]'));
    for (const el of document.querySelectorAll('input[type="checkbox"], [role="checkbox"], label')) {
        if (!want.test(labelOf(el))) continue;
        const box = boxOf(el);
        if (!box) continue;
        const target = [box.closest('label') || labelFor(box), box].find(t => t && vis(t, 4, 4));
        if (!target) continue;
        out.found = true;
        out.checked = box.checked === true || box.getAttribute('aria-checked') === 'true';
        if (mark) {
            document.querySelectorAll('[data-audit-insurance]').forEach(e => e.removeAttribute('data-audit-insurance'));
            target.setAttribute('data-audit-insurance', '1');
            target.scrollIntoView({ block: 'center' });
            const r = target.getBoundingClientRect();
            out.x = r.left + r.width / 2;
            out.y = r.top + r.height / 2;
        }
        return out;
    }
    return out;
}"""
DECLINE_INSURANCE = (
    "Tick the checkbox 'No, I don't want travel insurance'. Do not click Select, Use cash, Use points or any upgrade."
)
ACCEPT_FARE_RULES = (
    " Now click the red ACCEPT button at the very bottom of the page, under 'Fare rules': it accepts the fare "
    "conditions and does not pay."
)
INSURANCE_DONE = f" Travel insurance is already declined.{ACCEPT_FARE_RULES}"
MAX_INSURANCE_WARNINGS = 2  # the warning after Continue: decline once more, then the run ends


async def read_insurance(page: Page, mark: bool = False) -> dict | None:
    found = await js(page, JS_INSURANCE, mark, default=None)
    return found if isinstance(found, dict) else None


def insurance_declined(seen: dict | None) -> bool:
    return bool(seen and seen.get("checked") and not seen.get("warning"))


async def decline_travel_insurance(stagehand: Stagehand, page: Page) -> bool:
    """Tick "No, I don't want travel insurance": a mouse click, then a DOM click, then the model. True once ticked.

    Never clicks the insurance card (Select, Use cash, Use points). A box that is already ticked is not clicked
    again, since a second click would untick it; nor is any fallback run on a ticked box.
    """
    seen = await read_insurance(page, mark=True)
    if seen and seen.get("checked"):
        return insurance_declined(seen)
    if seen and seen.get("found") and "x" in seen:
        await click_at(page, seen["x"], seen["y"])
        await settle(page, TRIP_SETTLE_MS)
        seen = await read_insurance(page)
        if not (seen and seen.get("checked")):
            await js(page, JS_DOM_CLICK, "data-audit-insurance")
            await settle(page, TRIP_SETTLE_MS)
            seen = await read_insurance(page)
    if not (seen and seen.get("checked")):
        log.info("  Travel insurance: the checkbox did not take a click in code; asking the model")
        try:
            step = await llm_act(stagehand, DECLINE_INSURANCE, page=page)
            log.info(f"    {step.description}")
        except BROWSER_ERRORS as error:  # judged by the re-read below
            log.info(f"  Travel insurance: the model's click failed ({type(error).__name__}: {error})")
        await settle(page, TRIP_SETTLE_MS)
        seen = await read_insurance(page)
    return insurance_declined(seen)


@dataclass
class InsuranceAnswer:
    extra: str = ""  # what to tell the booking action about it
    warning: bool = False  # the "You need to make a travel insurance selection" warning showed
    settled: bool = True  # no question on this page, or "No, I don't want travel insurance" is ticked


async def answer_insurance(stagehand: Stagehand, page: Page, warnings: int = 0) -> InsuranceAnswer:
    """Decline travel insurance if this page asks.

    `warnings` is how many times the warning has shown already: on the one that ends the run nothing is clicked.
    """
    seen = await read_insurance(page)
    if not seen or not (seen.get("question") or seen.get("warning")):
        return InsuranceAnswer()
    warning = bool(seen.get("warning"))
    if warning and warnings + 1 >= MAX_INSURANCE_WARNINGS:
        return InsuranceAnswer(warning=True, settled=False)
    if not seen.get("checked") or warning:
        declined = await decline_travel_insurance(stagehand, page)
        log.info(
            "  Travel insurance: declined"
            if declined
            else "  Travel insurance: 'No, I don't want travel insurance' is not ticked; leaving it to the model"
        )
        seen = await read_insurance(page) or seen
    if insurance_declined(seen):
        return InsuranceAnswer(INSURANCE_DONE, warning)
    # Ticked but the warning still showing: say so, and never ask for a tick that would untick it.
    extra = INSURANCE_DONE if seen.get("checked") else f" {DECLINE_INSURANCE}"
    return InsuranceAnswer(extra, warning, settled=False)


# The fare rules' ACCEPT button at the very bottom of the Options / Review page ("Fare rules", "Red e-Deal
# conditions", then "< Back" and a red "ACCEPT >"). It accepts the fare conditions, which moves the booking on; it
# does not pay. The page must show fare rules or conditions, and buttons inside a cookie / consent banner or a
# dialog are skipped, so no other "Accept" is taken. With `mark`, the button is marked with data-audit-accept,
# scrolled into view and its centre returned. Null if there is no such button.
JS_ACCEPT = r"""mark => {
    const text = document.body ? document.body.innerText : '';
    if (!/fare rules|conditions/i.test(text)) return null;
    const banner = '[id*="cookie" i], [class*="cookie" i], [id*="consent" i], [class*="consent" i], [role="dialog"]';
    const buttons = Array.from(document.querySelectorAll('button, a, [role="button"]'))
        .filter(el => vis(el) && /^accept\b/i.test(clean(el.innerText)) && !el.closest(banner));
    if (!buttons.length) return null;
    const el = buttons[buttons.length - 1];  // the one at the bottom of the page
    const out = { text: clean(el.innerText) };
    if (mark) {
        document.querySelectorAll('[data-audit-accept]').forEach(e => e.removeAttribute('data-audit-accept'));
        el.setAttribute('data-audit-accept', '1');
        el.scrollIntoView({ block: 'center' });
        const r = el.getBoundingClientRect();
        out.x = r.left + r.width / 2;
        out.y = r.top + r.height / 2;
    }
    return out;
}"""


async def read_accept(page: Page, mark: bool = False) -> dict | None:
    found = await js(page, JS_ACCEPT, mark, default=None)
    return found if isinstance(found, dict) else None


async def moved_on(page: Page, before: str | None, heading: str) -> bool:
    """Poll up to STEP_CHECK_MS: has the page left the fare rules? A new main heading, or a changed screen
    without the fare rules' ACCEPT."""
    for poll_index in range(STEP_CHECK_MS // STEP_POLL_MS + 1):
        if poll_index:
            await pause(STEP_POLL_MS)
        now = (await selection_page(page)).get("heading") or ""
        if now and now != heading:
            return True
        if await fingerprint(page) != before and await read_accept(page) is None:
            return True
    return False


async def accept_fare_rules(page: Page) -> bool:
    """Click the fare rules' ACCEPT: a mouse click, then a DOM click. True once the page has moved on."""
    found = await read_accept(page, mark=True)
    if not found or "x" not in found:
        return False
    before, heading = await fingerprint(page), (await selection_page(page)).get("heading") or ""
    await click_at(page, found["x"], found["y"])
    if await moved_on(page, before, heading):
        return True
    log.info("  Fare rules: the page did not move on after clicking ACCEPT; trying a DOM click")
    await js(page, JS_DOM_CLICK, "data-audit-accept")
    return await moved_on(page, before, heading)


BOOKING_GOAL = (
    "Goal: reach the payment page of this booking with the cheapest fare and NO optional extras (no paid bags, "
    "seats, insurance, carbon offset, upgrades, car hire, hotels or donations), continuing as a guest without "
    "logging in or joining a loyalty program. Take the single next action that moves the booking towards the "
    "payment page. Where the page insists on a choice, choose the free or decline option. Accepting fare rules or "
    "fare conditions (an 'Accept' button) is allowed and needed: it is not a purchase. Never click a button that "
    "pays or completes the purchase (Pay, Purchase, Book now, Confirm booking)."
)


async def next_booking_action(
    stagehand: Stagehand,
    page: Page,
    passenger: Passenger,
    state: StepState,
    step_type: str,
    extra: str = "",
    target: dict | None = None,
) -> Acted:
    """Take the one action that moves the booking on from `step_type`, declining every extra.

    A completed passenger form moves on with the footer's CONTINUE, clicked in code. `target` is filled with the
    element the model chose before it is acted on, so a caller can record it even if the act raises.
    """
    target = {} if target is None else target
    passengers = step_type == "passenger_details"
    if passengers and await fill_passenger_details(stagehand, page, passenger) and await click_continue(page):
        log.info("  Passenger details: CONTINUE clicked in code")
        target.update(description="CONTINUE in the sticky footer", selector=CONTINUE_MARK)
        return Acted(True, "code click on CONTINUE", CONTINUE_MARK)
    instruction = f"{BOOKING_GOAL} The current screen looks like: {state.heading or step_type}."
    if state.validation_errors:
        instruction += (
            f" The page shows these errors: {'; '.join(state.validation_errors[:3])}. Fix the first one if it is "
            "a missing choice or field (never by adding a paid extra)."
        )
    instruction = f"{instruction}{extra} {NO_RESEARCH}"

    def chosen(action: object) -> None:
        target.update(description=getattr(action, "description", ""), selector=getattr(action, "selector", ""))

    if passengers:  # the sign-up and marketing controls are refused, not just discouraged
        step = await click_one(stagehand, instruction, PASSENGER_NEVER_CLICK, target=chosen)
    else:
        step = await llm_act(stagehand, instruction, page=page, target=chosen)
    log.info(f"    {step.description}")
    await settle(page, SETTLE_MS)
    return step


def note_action(step: dict, acted: Acted) -> None:
    """Record on a step's entry what was clicked on it (a repeat on the same page is appended)."""
    step["action"] = f"{step['action']} | then: {acted.description}" if step.get("action") else acted.description
    step["selector"] = acted.selector


class InsuranceRefused(Exception):
    """Continue was refused twice for want of an insurance answer."""


async def move_booking_on(
    stagehand: Stagehand,
    page: Page,
    passenger: Passenger,
    state: StepState,
    step_type: str,
    warnings: list[int],
    target: dict | None = None,
) -> Acted:
    """One turn on a booking step: answer the insurance question in code, accept the fare rules in code once it is
    settled, and only then (if neither moved the page on) ask the model. `warnings` counts the insurance warnings.
    """
    answer = await answer_insurance(stagehand, page, warnings[0])
    warnings[0] += answer.warning
    if warnings[0] >= MAX_INSURANCE_WARNINGS:
        raise InsuranceRefused
    extra = answer.extra
    if answer.settled:
        if await accept_fare_rules(page):
            log.info("  Fare rules: accepted")
            return Acted(True, "Fare rules: accepted (ACCEPT clicked in code)", "[data-audit-accept]")
        if not extra and await read_accept(page) is not None:
            extra = ACCEPT_FARE_RULES
    return await next_booking_action(stagehand, page, passenger, state, step_type, extra, target)


async def take_turn(
    stagehand: Stagehand,
    page: Page,
    passenger: Passenger,
    state: StepState,
    step_type: str,
    warnings: list[int],
    entry: dict,
) -> bool:
    """move_booking_on, recording on the step's `entry` what was clicked (or being clicked when an error struck).

    A browser session hiccup that survives the retries is logged and the turn ends: the loop reads the page
    again, and a step that keeps failing ends the run through the stuck check. False if the insurance answer was
    refused for good.
    """
    target: dict = {}
    try:
        acted = await move_booking_on(stagehand, page, passenger, state, step_type, warnings, target)
    except InsuranceRefused:
        return False
    except BROWSER_ERRORS as error:
        what = "browser session hiccup" if is_transient(error) else f"{type(error).__name__}"
        clicked = target.get("description") or "the page"
        note_action(
            entry, Acted(False, f"{what} while acting on {clicked}: {str(error)[:150]}", target.get("selector") or "")
        )
        if not is_transient(error):
            raise
        log.warning(f"  {what} while acting on {clicked} ({str(error)[:120]}); reading the page again")
        return True
    note_action(entry, acted)
    return True


async def walk_to_payment(stagehand: Stagehand, page: Page, passenger: Passenger, result: SiteResult) -> bool:
    """Step through the booking flow, recording every extra offered, until the payment page. True if reached."""
    out = result.out_dir
    last_signature, repeats = None, 0
    insurance_warnings = [0]
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
            step_type = "payment" if last_state.is_payment_page else last_state.step_type
            if not await take_turn(
                stagehand, page, passenger, last_state, step_type, insurance_warnings, result.steps[-1]
            ):
                result.message = (
                    f"Stuck on '{last_state.heading or last_state.step_type}': insurance selection not accepted"
                )
                return False
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
        # After the step's offers are recorded: insurance and fare rules are answered in code, never by the model.
        if not await take_turn(stagehand, page, passenger, state, step_type, insurance_warnings, result.steps[-1]):
            # Continue refused twice for want of an insurance answer
            result.message = f"Stuck on '{state.heading or step_type}': insurance selection not accepted"
            return False

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
                    "Extract every flight listed on this results page with its times, flight number (e.g. 'QF2310', "
                    "under the times), stops, duration and availability. For the price, read only the Economy Cabin "
                    "column and only CASH prices (e.g. '$529'): ignore Qantas Points prices such as '16,600 PTS + "
                    "$58' (mark such flights award_only), ignore the 'Classic Plus' / 'Classic Reward' badges (they "
                    "are reward-seat badges, not fare names) and ignore the Rewards toggle. Also report what the "
                    "prices cover.",
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

            flights = rank_flights(listing.flights)
            if not flights:
                result.fail(Status.NO_FLIGHTS_FOUND, "No available flights with a price on the results page")
                return
            await select_a_flight(stagehand, page, flights, result, listing.price_basis)

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
