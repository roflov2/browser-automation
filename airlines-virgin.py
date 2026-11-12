"""Virgin Australia flight-booking drip-pricing POC.

Start on the Virgin Australia homepage, search for a one-way flight, pick the cheapest flight and fare,
then walk the whole booking chain (guest details, seats, bags, extras, review) to the payment page.
Every optional extra and fee offered along the way (baggage, seats, insurance, carbon offset, service
and card fees, ...) is recorded, and the advertised fare is compared with the total at checkout.

The run always stops on the payment page: no payment details are entered and nothing is purchased.

Usage:
    python airlines-virgin.py
    python airlines-virgin.py --origin BNE --destination PER --date 2027-02-14
"""

import argparse
import asyncio
import contextlib
import csv
import json
import os
import re
import resource
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from urllib.parse import urlparse

from browserbase import Browserbase
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from stagehand import (
    Page,
    Stagehand,
    StagehandClientLoggingConfig,
    browserbase as stagehand_browserbase,
    local_browser,
)

load_dotenv()

SITE_URL = "https://www.virginaustralia.com/au/en/"
SETTLE_MS = 2500  # fixed wait after navigation/clicks; the booking app is a slow single-page app
MAX_SEARCH_STEPS = 14
MAX_BOOKING_STEPS = 25
MAX_STUCK_REPEATS = 3
OUTPUT_ROOT = Path("output")

SAFETY_PROMPT = (
    "You are auditing an airline booking flow for price transparency. Never enter payment card, voucher or "
    "Travel Bank details, and never click a button that pays for, purchases or confirms a booking "
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
    form_visible: bool = Field(description="True if the flight search form/widget is visible")
    trip_type: str | None = Field(description="'one_way', 'return' or 'multi_city' as currently set, if shown")
    origin: str | None = Field(description="Currently chosen departure airport, e.g. 'Sydney (SYD)'; null if empty")
    destination: str | None = Field(description="Currently chosen arrival airport; null if empty")
    depart_date: str | None = Field(description="Currently chosen departure date as displayed; null if empty")
    guests: str | None = Field(description="Currently chosen passengers, e.g. '1 Adult'")
    using_points: bool = Field(description="True if 'Use Velocity Points' / pay with points is switched on")
    blocked_reason: str | None = Field(
        description="If progress is blocked by a CAPTCHA, 'verify you are human', access-denied or bot check, describe it; otherwise null"
    )
    reason: str


class FareOption(BaseModel):
    fare_name: str = Field(description="Fare brand, e.g. 'Lite', 'Choice', 'Flex', 'Business'")
    price: str | None = Field(description="Price as displayed, e.g. '$129'; null if unavailable/sold out")


class Flight(BaseModel):
    flight_number: str | None = Field(description="e.g. 'VA 815'; null if not shown")
    departs: str = Field(description="Departure time as displayed, e.g. '6:00am'")
    arrives: str = Field(description="Arrival time as displayed")
    stops: int = Field(description="Number of stops; 0 for a direct flight")
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
    flight_selected: bool = Field(description="True if a specific flight and fare is now selected or in the trip summary/cart")
    flight: str | None = Field(description="Selected flight, e.g. 'VA 815 06:00 SYD - 07:35 MEL'")
    fare_name: str | None = Field(description="Selected fare brand, e.g. 'Lite'")
    fare_price: str | None = Field(description="Price shown for the selected fare, e.g. '$129'")
    running_total: str | None = Field(description="Trip total / cart total currently displayed, if any")
    reason: str


class Fee(BaseModel):
    label: str
    amount: str


class Offer(BaseModel):
    category: str = Field(description=f"One of: {EXTRA_CATEGORIES}")
    label: str = Field(description="Name of the extra or fee as displayed, e.g. '23kg checked bag', 'Economy X seat'")
    price: str | None = Field(description="Price as displayed, e.g. '$45', 'From $9', 'Included', '1.3%'; null if not shown")
    price_basis: str | None = Field(description="e.g. 'per passenger per flight', 'per booking', if stated")
    selected: bool = Field(
        description="True if this extra is currently added/ticked/selected and so included in the running total"
    )
    mandatory: bool = Field(description="True if it is a compulsory charge that cannot be declined")


class StepState(BaseModel):
    step_type: str = Field(description=f"Which booking step this is. One of: {STEP_TYPES}")
    heading: str | None = Field(description="Main heading or step name shown, e.g. 'Guest details', 'Choose your seats'")
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
    price_lines: list[Fee] = Field(description="Lines of the price summary / trip total panel currently displayed, if any")
    total: str | None = Field(description="Running trip total currently displayed, e.g. '$129.00'")
    validation_errors: list[str] = Field(description="Any form validation or error messages currently displayed")
    summary: str


class FieldToFill(BaseModel):
    label: str = Field(description="Label of the form field exactly as displayed")
    kind: str = Field(description="One of: text, dropdown, radio, checkbox, date")
    value: str = Field(description="The value to enter or the option to choose, taken from the supplied passenger details")


class PassengerForm(BaseModel):
    fields: list[FieldToFill] = Field(
        description="Required passenger/contact fields that are still empty or unselected, in page order. "
        "Exclude optional fields, loyalty/Velocity numbers, marketing opt-ins and any payment fields."
    )
    complete: bool = Field(description="True if every required field is already filled in")


class LineItem(BaseModel):
    name: str = Field(description="Flight as displayed, e.g. 'Sydney to Melbourne VA 815'")
    variant: str | None = Field(description="Fare brand, e.g. 'Lite'")
    quantity: int = Field(description="Number of passengers")
    price: str | None = Field(description="Fare amount as displayed for this flight")


class Surcharge(BaseModel):
    payment_method: str = Field(description="e.g. 'Visa/Mastercard credit', 'Amex', 'PayPal', 'Debit card'")
    fee: str = Field(description="Fee as displayed, e.g. '1.3%', '$3.50', 'No fee'")


class CheckoutDetails(BaseModel):
    is_checkout_page: bool = Field(description="True if on the payment / review-and-pay page showing the booking's price summary")
    blocked_reason: str | None = Field(
        description="If progress is blocked by a required login, CAPTCHA, 'verify you are human' or bot check, describe it; otherwise null"
    )
    items: list[LineItem]
    subtotal: str | None = Field(description="Flight/fare total before optional extras and payment fees, as displayed")
    extras: list[Fee] = Field(description="Optional extras currently included in the total (bags, seats, insurance, carbon offset, ...)")
    other_fees: list[Fee] = Field(
        description="Any compulsory charge added on top of the fare that is NOT a government tax and NOT an optional extra, "
        "e.g. booking fee, service fee, card payment fee already applied"
    )
    payment_surcharges: list[Surcharge] = Field(
        description="Card/payment-method fees advertised on the page, whether or not applied to the total yet"
    )
    tax_amount: str | None = Field(description="Taxes/airport charges if itemised")
    tax_included: bool = Field(description="True if taxes and charges are included in the fare shown")
    total: str | None = Field(description="Total amount due as displayed")
    currency: str | None = Field(description="Currency code if shown, e.g. 'AUD'")
    summary: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def parse_price(value: str | None) -> float | None:
    """Parse '$1,234.56', '1.234,56 €', 'AUD $21.00', 'From $15' into a float; None if no number."""
    if not value:
        return None
    match = re.search(r"\d[\d.,\s]*", value)
    if not match:
        return None
    number = match.group(0).strip().replace(" ", "")
    if "," in number and "." in number:
        # Whichever separator comes last is the decimal separator.
        if number.rfind(",") > number.rfind("."):
            number = number.replace(".", "").replace(",", ".")
        else:
            number = number.replace(",", "")
    elif "," in number:
        head, _, tail = number.rpartition(",")
        number = f"{head.replace(',', '')}.{tail}" if len(tail) == 2 else number.replace(",", "")
    number = number.rstrip(".")
    try:
        return float(number)
    except ValueError:
        return None


def parse_amount(value: str | None) -> float | None:
    """Like parse_price, but None for percentages ('1.3%'), which are rates rather than amounts."""
    return None if value and "%" in value else parse_price(value)


def money_sum(values: list[float | None]) -> float | None:
    known = [v for v in values if v is not None]
    return round(sum(known), 2) if known else None


def diff(a: float | None, b: float | None) -> float | None:
    return round(a - b, 2) if a is not None and b is not None else None


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "step"


class LocalResourceMonitor:
    def __init__(self) -> None:
        self.cpu_usage: list[float] = []
        self.memory_usage: list[float] = []

    def start(self) -> asyncio.Task[None]:
        async def sample() -> None:
            while True:
                self.memory_usage.append(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024)
                self.cpu_usage.append(os.getloadavg()[0])
                await asyncio.sleep(1)

        return asyncio.create_task(sample())

    def summary(self) -> dict[str, float]:
        if not self.memory_usage:
            return {"avg_memory_mb": 0.0, "peak_memory_mb": 0.0, "avg_cpu_load": 0.0, "data_points": 0}
        return {
            "avg_memory_mb": round(sum(self.memory_usage) / len(self.memory_usage), 2),
            "peak_memory_mb": round(max(self.memory_usage), 2),
            "avg_cpu_load": round(sum(self.cpu_usage) / len(self.cpu_usage), 2),
            "data_points": len(self.cpu_usage),
        }


@dataclass
class Trip:
    origin: str
    destination: str
    depart: date

    @property
    def depart_text(self) -> str:
        return f"{self.depart.strftime('%A')} {self.depart.day} {self.depart.strftime('%B %Y')}"

    @property
    def deep_link(self) -> str:
        """Search-results link for the booking engine, used only if the homepage form can't be driven."""
        day = self.depart.strftime("%m-%d-%Y")
        return (
            "https://book.virginaustralia.com/dx/VADX/#/flight-selection?journeyType=one-way"
            f"&activeMonth={day}&awardBooking=false&searchType=BRANDED&class=Economy&ADT=1&CHD=0&INF=0"
            f"&origin={self.origin}&destination={self.destination}&date={day}&direction=0"
        )


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
    def from_env(cls) -> "Passenger":
        defaults = cls()
        return cls(**{name: os.getenv(f"PAX_{name.upper()}", getattr(defaults, name)) for name in cls.__dataclass_fields__})

    def describe(self) -> str:
        return (
            f"title {self.title}; first name {self.first_name}; last name {self.last_name}; gender {self.gender}; "
            f"date of birth {self.date_of_birth}; email {self.email}; mobile phone {self.phone} ({self.country}, +61); "
            f"country of residence {self.country}"
        )


@dataclass
class SiteResult:
    site: str
    out_dir: Path
    status: str = "error"
    message: str = ""
    items: list[dict] = field(default_factory=list)
    extras: list[dict] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    comparison: dict = field(default_factory=dict)


class Blocked(RuntimeError):
    """The site put a login wall, CAPTCHA or bot check in the way."""


# ---------------------------------------------------------------------------
# Site steps
# ---------------------------------------------------------------------------
async def settle(page: Page, ms: int = SETTLE_MS) -> None:
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("load")
    await page.wait_for_timeout(ms)


async def wait_for_render(page: Page, seconds: int = 20) -> None:
    """Wait for the client-side booking app to hydrate and its loading spinner to go away."""
    for _ in range(seconds):
        await page.wait_for_timeout(1000)
        with contextlib.suppress(Exception):
            ready = await page.evaluate(
                "() => document.readyState === 'complete' && document.body && document.body.innerText.trim().length > 200"
                " && ![...document.querySelectorAll('[class*=\"spinner\" i], [class*=\"loader\" i], [aria-busy=\"true\"]')]"
                ".some(el => el.offsetParent !== null)"
            )
            if ready:
                return


async def shot(page: Page, path: Path) -> None:
    try:
        await page.screenshot(path=str(path), full_page=True)
    except Exception:
        with contextlib.suppress(Exception):
            await page.screenshot(path=str(path))


async def dismiss_popups(stagehand: Stagehand) -> None:
    """Close cookie banners, promo popups or region selectors if any are visible."""
    with contextlib.suppress(Exception):
        found = await stagehand.observe(
            "Find the close, accept, dismiss or 'no thanks' button of any visible cookie banner, promotional/newsletter "
            "popup, app-install banner or region selector modal. Return nothing if there is none."
        )
        for action in found.data[:2]:
            print(f"  Dismissing popup: {action.description}")
            await stagehand.act(action)


async def search_flights(stagehand: Stagehand, page: Page, trip: Trip) -> None:
    """Drive the homepage flight search form one action at a time until the results page shows."""
    goal = (
        f"a ONE-WAY flight for 1 adult from {trip.origin} to {trip.destination} departing {trip.depart_text}, "
        "paying with money (not Velocity Points)"
    )
    question = (
        f"We are searching for {goal}. Report the current state of the flight search form, "
        "or whether the flight results page is already showing."
    )
    state = (await stagehand.extract(question, SearchState)).data
    for _ in range(MAX_SEARCH_STEPS):
        if state.blocked_reason:
            raise Blocked(state.blocked_reason)
        if state.on_results_page:
            return
        step = await stagehand.act(
            f"Take the single next action needed to search for {goal} using the flight search form. "
            f"Current form state: trip type={state.trip_type}, from={state.origin}, to={state.destination}, "
            f"date={state.depart_date}, guests={state.guests}, using points={state.using_points}. "
            "Order: open the flight booking form if it is hidden; set 'Fly from'; set 'Fly to' (pick the airport from "
            "the list); switch the trip type to one way; pick the departure date in the calendar (move to the right "
            "month first if needed) and confirm it; confirm 1 adult guest; then click the search button "
            "(e.g. 'Find flights' / 'Search flights' / 'Let's fly')."
        )
        await settle(page)
        state = (await stagehand.extract(question, SearchState)).data
        print(f"  {step.data.action_description} -> {state.reason}")
    if state.on_results_page:
        return

    print("  Search form did not reach the results page; opening the booking engine's search link directly...")
    await page.goto(trip.deep_link)
    await settle(page)
    await wait_for_render(page)
    state = (await stagehand.extract(question, SearchState)).data
    if state.blocked_reason:
        raise Blocked(state.blocked_reason)
    if not state.on_results_page:
        raise RuntimeError(f"Couldn't reach the flight results page: {state.reason}")


def choose_flight(flights: list[Flight]) -> Flight | None:
    """Cheapest available flight, preferring direct services."""
    available = [f for f in flights if not f.sold_out and parse_price(f.price) is not None]
    direct = [f for f in available if f.stops == 0]
    return min(direct or available, key=lambda f: parse_price(f.price), default=None)


async def select_flight(stagehand: Stagehand, page: Page, flight: Flight) -> dict:
    name = " ".join(filter(None, [flight.flight_number, f"{flight.departs} - {flight.arrives}"]))
    item: dict = {
        "name": name,
        "shop_price_display": flight.price,
        "shop_price": parse_price(flight.price),
        "selected_variant": None,
        "selected_price_display": None,
        "selected_price": None,
        "status": "skipped",
    }
    described = f"the flight departing {flight.departs} and arriving {flight.arrives}" + (
        f" ({flight.flight_number})" if flight.flight_number else ""
    )
    question = (
        f"We want {described} on its cheapest fare. Is a flight and fare now selected (shown as selected, or in the "
        "trip summary / cart)? Report the selected flight, fare brand, fare price and any running total."
    )
    selected = None
    for _ in range(4):
        step = await stagehand.act(
            f"Take the single next action to select {described} with its CHEAPEST fare (usually the first/lowest fare "
            "brand such as 'Lite' in Economy). If the fare options for that flight are not visible yet, click the "
            "flight's lowest price to reveal them; otherwise click the select button of the cheapest fare. "
            "If a popup offers a more expensive fare, decline it and keep the cheapest fare."
        )
        print(f"    Flight selection: {step.data.action_description}")
        await settle(page)
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


async def fill_passenger_details(stagehand: Stagehand, page: Page, passenger: Passenger) -> None:
    details = passenger.describe()
    for _ in range(2):
        form = (
            await stagehand.extract(
                f"Passenger details to use: {details}. List the required passenger and contact fields on this page that "
                "are still empty or unselected, and for each give the value to enter from those details.",
                PassengerForm,
            )
        ).data
        if form.complete or not form.fields:
            return
        for entry in form.fields[:20]:
            verb = "Select" if entry.kind in ("dropdown", "radio", "checkbox") else "Type"
            where = "for" if verb == "Select" else "into"
            with contextlib.suppress(Exception):
                step = await stagehand.act(f"{verb} '{entry.value}' {where} the '{entry.label}' field of the passenger details form.")
                print(f"    Guest details: {step.data.action_description}")
            await page.wait_for_timeout(400)


NEXT_ACTIONS = {
    "fare_selection": "Choose the cheapest fare and continue. Decline any offer to upgrade to a more expensive fare.",
    "login_prompt": "Continue as a guest without logging in or joining Velocity (e.g. 'Continue as guest', 'Skip', 'Not now').",
    "passenger_details": "Click the button that continues from guest details to the next booking step.",
    "seats": "Do not choose a paid seat. Skip seat selection (e.g. 'Skip seats', 'Skip seat selection', 'No thanks', "
    "'Continue without seats') or click continue without selecting any seat.",
    "upsell_modal": "Decline the offer in the open popup (e.g. 'No thanks', 'Continue without', 'Skip') without adding anything.",
}
DEFAULT_NEXT_ACTION = (
    "Do not add any optional extra (no bags, seats, insurance, carbon offset, upgrades or donations). If the page "
    "requires an explicit choice, choose the decline option (e.g. 'No thanks', 'No, I don't want ...'). Otherwise "
    "click the button that continues to the next booking step towards payment (e.g. 'Continue', 'Next', "
    "'Continue to payment'). Never click a button that pays or completes the purchase."
)


def record_offers(result: SiteResult, index: int, state: StepState) -> None:
    seen = {(e["step_type"], e["category"], e["label"].lower()) for e in result.extras}
    for offer in state.offers:
        key = (state.step_type, offer.category, offer.label.lower())
        if key in seen:
            continue
        seen.add(key)
        result.extras.append({
            "step": index,
            "step_type": state.step_type,
            "step_heading": state.heading,
            "category": offer.category,
            "label": offer.label,
            "price_display": offer.price,
            "price": parse_amount(offer.price),
            "price_basis": offer.price_basis,
            "selected_by_default": offer.selected,  # we never add extras, so anything selected was pre-selected
            "mandatory": offer.mandatory,
        })


async def walk_to_payment(stagehand: Stagehand, page: Page, passenger: Passenger, result: SiteResult) -> bool:
    """Step through the booking flow, recording every extra offered, until the payment page. True if reached."""
    out = result.out_dir
    last_signature, repeats = None, 0
    for index in range(1, MAX_BOOKING_STEPS + 1):
        await wait_for_render(page, 10)
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
        print(f"  Step {index}: {step_type} ({state.heading}) | total {state.total} | {len(state.offers)} offer(s)")
        await shot(page, out / f"step-{index:02d}-{slug(step_type)}.png")
        record_offers(result, index, state)
        result.steps.append({
            "step": index, "step_type": step_type, "heading": state.heading, "url": await page.url(),
            "total_display": state.total, "total": parse_price(state.total),
            "price_lines": [line.model_dump() for line in state.price_lines],
            "offers": len(state.offers), "validation_errors": state.validation_errors, "summary": state.summary,
        })
        if index == 1:
            await shot(page, out / "cart.png")  # first view of the selected trip and its running total

        if state.blocked_reason:
            raise Blocked(state.blocked_reason)
        if state.is_payment_page or step_type == "payment":
            return True
        if step_type == "confirmation":
            raise RuntimeError("Reached a confirmation page unexpectedly; stopping")

        signature = (step_type, state.heading, await page.url())
        repeats = repeats + 1 if signature == last_signature else 0
        last_signature = signature
        if repeats >= MAX_STUCK_REPEATS:
            result.message = f"Stuck on '{state.heading or step_type}': {'; '.join(state.validation_errors) or state.summary}"
            return False

        if step_type == "passenger_details":
            await fill_passenger_details(stagehand, page, passenger)
        instruction = NEXT_ACTIONS.get(step_type, DEFAULT_NEXT_ACTION)
        if state.validation_errors:
            instruction = (
                f"The page shows these errors: {'; '.join(state.validation_errors[:3])}. Fix the first one if it is a "
                f"missing choice or field (never by adding a paid extra). Otherwise: {instruction}"
            )
        step = await stagehand.act(instruction)
        print(f"    {step.data.action_description}")
        await settle(page)

    result.message = f"Payment page not reached within {MAX_BOOKING_STEPS} steps"
    return False


def compare_prices(items: list[dict], extras: list[dict], steps: list[dict], checkout: CheckoutDetails | None) -> dict:
    added = [i for i in items if i["status"] == "added"]
    for item in added:
        expected = item["selected_price"] if item["selected_price"] is not None else item["shop_price"]
        item["expected_price"] = expected
        item["selected_vs_shop_diff"] = diff(item["selected_price"], item["shop_price"])
        match = checkout.items[0] if checkout and checkout.items else None
        item["checkout_price_display"] = match.price if match else None
        checkout_unit = parse_price(match.price) if match else None
        if checkout_unit is not None and match and match.quantity > 1:
            checkout_unit = round(checkout_unit / match.quantity, 2)
        item["checkout_price"] = checkout_unit
        item["checkout_vs_expected_diff"] = diff(checkout_unit, expected)
        item["price_increased"] = bool(item["checkout_vs_expected_diff"] and item["checkout_vs_expected_diff"] > 0)

    expected_total = money_sum([i["expected_price"] for i in added])
    subtotal = parse_price(checkout.subtotal) if checkout else None
    total = parse_price(checkout.total) if checkout else None
    if total is None:  # payment page not reached: fall back to the last running total seen
        total = next((s["total"] for s in reversed(steps) if s["total"] is not None), None)
    fees = [{"label": f.label, "amount": parse_amount(f.amount)} for f in (checkout.other_fees if checkout else [])]
    fees_total = money_sum([f["amount"] for f in fees]) or 0.0
    included = [{"label": f.label, "amount": parse_amount(f.amount)} for f in (checkout.extras if checkout else [])]
    surcharges = [s.model_dump() for s in (checkout.payment_surcharges if checkout else [])]
    preselected = [e for e in extras if e["selected_by_default"] and not e["mandatory"] and (e["price"] or 0) > 0]
    chargeable = [s for s in surcharges if (parse_price(s["fee"]) or 0) > 0]

    item_increase = any(i.get("price_increased") for i in added)
    subtotal_increase = (diff(subtotal, expected_total) or 0) > 0
    total_increase = (diff(total, expected_total) or 0) > 0
    reasons = [
        r for r, hit in (
            ("fare increased at checkout", item_increase),
            ("checkout fare subtotal higher than selected fare", subtotal_increase),
            ("total at checkout higher than advertised fare with no extras chosen", total_increase),
            ("non-tax fees added", fees_total > 0),
            ("optional extras pre-selected: " + ", ".join(e["label"] for e in preselected), bool(preselected)),
            ("payment surcharge applies: " + ", ".join(f"{s['payment_method']} {s['fee']}" for s in chargeable), bool(chargeable)),
        ) if hit
    ]
    by_category: dict[str, int] = {}
    for extra in extras:
        by_category[extra["category"]] = by_category.get(extra["category"], 0) + 1

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
        "extras_offered": len(extras),
        "extras_offered_by_category": by_category,
        "extras_preselected": [e["label"] for e in preselected],
        "tax_amount_display": checkout.tax_amount if checkout else None,
        "tax_included": checkout.tax_included if checkout else None,
        "checkout_total_display": checkout.total if checkout else None,
        "checkout_total": total,
        "currency": checkout.currency if checkout else None,
        "drip_detected": bool(reasons),
        "drip_reasons": reasons,
        "checkout_summary": checkout.summary if checkout else None,
    }


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


EXTRA_FIELDS = ["step", "step_type", "step_heading", "category", "label", "price_display", "price",
                "price_basis", "selected_by_default", "mandatory"]
ITEM_FIELDS = ["name", "selected_variant", "shop_price_display", "selected_price_display",
               "checkout_price_display", "checkout_vs_expected_diff", "price_increased", "status"]


def write_reports(result: SiteResult, trip: Trip, search_url: str | None, checkout_url: str | None) -> None:
    """Written even when a run ends early, so whatever extras were seen are kept."""
    out = result.out_dir
    report = {"site": result.site, "shop_url": search_url, "checkout_url": checkout_url,
              "status": result.status, "message": result.message,
              "search": {"origin": trip.origin, "destination": trip.destination,
                         "depart_date": trip.depart.isoformat(), "passengers": "1 adult", "trip_type": "one_way"},
              "items": result.items, **result.comparison, "extras": result.extras, "steps": result.steps}
    (out / "price_comparison.json").write_text(json.dumps(report, indent=2))
    write_csv(out / "price_comparison.csv", ITEM_FIELDS, result.items)
    write_csv(out / "extras.csv", EXTRA_FIELDS, result.extras)


async def run_site(stagehand: Stagehand, page: Page, trip: Trip, passenger: Passenger, result: SiteResult) -> None:
    out = result.out_dir
    search_url: str | None = None
    checkout: CheckoutDetails | None = None
    print(f"\n=== {result.site} | {trip.origin} -> {trip.destination} on {trip.depart_text} ===")
    try:
        await page.goto(result.site)
        await settle(page)
        await dismiss_popups(stagehand)

        await search_flights(stagehand, page, trip)
        await wait_for_render(page)
        search_url = await page.url()
        print(f"  Results page: {search_url}")

        listing = (
            await stagehand.extract(
                "Extract every flight listed on this results page with its times, flight number, stops, duration, lowest "
                "displayed price, availability and per-fare prices where visible. Also report what the prices cover.",
                Flights,
            )
        ).data
        rows = [{**f.model_dump(exclude={"fares"}), "fares": "; ".join(f"{o.fare_name} {o.price}" for o in f.fares)}
                for f in listing.flights]
        write_csv(out / "products.csv", [n for n in Flight.model_fields], rows)
        await shot(page, out / "flights.png")
        print(f"  Extracted {len(listing.flights)} flights ({listing.price_basis or 'price basis not stated'})")

        flight = choose_flight(listing.flights)
        if flight is None:
            result.status, result.message = "no_flights_found", "No available flights with a price on the results page"
            return

        print(f"  Selecting: {flight.flight_number or ''} {flight.departs}-{flight.arrives} ({flight.price})")
        item = await select_flight(stagehand, page, flight)
        item["price_basis"] = listing.price_basis
        result.items.append(item)
        if item["status"] != "added":
            result.status, result.message = "add_to_cart_failed", "The flight and fare could not be selected"
            return

        reached = await walk_to_payment(stagehand, page, passenger, result)
        if reached:
            await shot(page, out / "checkout.png")
            checkout = (
                await stagehand.extract(
                    "Extract the booking's price summary from this payment page: flights and fare, fare subtotal, optional "
                    "extras included, any other fees, taxes, total due and currency. Also list every card or payment-method "
                    "surcharge/fee advertised (e.g. credit card fee percentages), and report if a login, CAPTCHA or bot "
                    "check is blocking the page. Do not interact with the page.",
                    CheckoutDetails,
                )
            ).data
            if checkout.blocked_reason:
                result.status, result.message = "blocked", checkout.blocked_reason
            elif not checkout.is_checkout_page:
                result.status, result.message = "no_checkout", "Payment page not reached"
            else:
                result.status = "ok"
            if checkout.payment_surcharges:  # replace the payment step's own offers with the per-method list
                result.extras = [e for e in result.extras
                                 if not (e["step_type"] == "payment" and e["category"] == "payment_surcharge")]
            for s in checkout.payment_surcharges:
                result.extras.append({"step": len(result.steps), "step_type": "payment", "step_heading": "Payment",
                                      "category": "payment_surcharge", "label": s.payment_method, "price_display": s.fee,
                                      "price": parse_amount(s.fee), "price_basis": "per booking",
                                      "selected_by_default": False, "mandatory": False})
        else:
            result.status = "no_checkout"
    except Blocked as error:
        result.status, result.message = "blocked", str(error)
    finally:
        result.comparison = compare_prices(result.items, result.extras, result.steps, checkout)
        checkout_url = None
        with contextlib.suppress(Exception):
            checkout_url = await page.url()
        write_reports(result, trip, search_url, checkout_url)

    c = result.comparison
    print(f"  Advertised fare: {c['expected_subtotal']} | Checkout fare: {c['checkout_subtotal_display']} "
          f"| Total: {c['checkout_total_display']} | Fees: {c['other_fees_total']} | Extras offered: {c['extras_offered']}")
    print(f"  Drip pricing detected: {'YES - ' + '; '.join(c['drip_reasons']) if c['drip_detected'] else 'NO'}")


def update_summary(result: SiteResult) -> None:
    """Same columns as the coffee workflow, so both write to one output/summary.csv."""
    path = OUTPUT_ROOT / "summary.csv"
    c = result.comparison
    row = {
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "site": result.site,
        "status": result.status,
        "message": result.message,
        "items_added": c.get("items_added", 0),
        "expected_subtotal": c.get("expected_subtotal"),
        "checkout_subtotal": c.get("checkout_subtotal"),
        "checkout_total": c.get("checkout_total"),
        "currency": c.get("currency"),
        "other_fees_total": c.get("other_fees_total"),
        "shipping": c.get("shipping"),
        "drip_detected": c.get("drip_detected"),
        "output_dir": str(result.out_dir),
    }
    existing_rows: list[dict] = []
    if path.exists():
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            for r in reader:
                if r.get("site") != result.site:
                    existing_rows.append(r)
    existing_rows.append(row)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(row))
        writer.writeheader()
        writer.writerows(existing_rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Virgin Australia flight-booking drip-pricing POC")
    parser.add_argument("--origin", default="SYD", help="Departure airport code (default SYD)")
    parser.add_argument("--destination", default="MEL", help="Arrival airport code (default MEL)")
    parser.add_argument("--date", type=date.fromisoformat, default=date.today() + timedelta(days=30),
                        help="Departure date as YYYY-MM-DD (default: 30 days from today)")
    parser.add_argument("--url", default=SITE_URL, help=f"Start page (default {SITE_URL})")
    args = parser.parse_args()
    if args.date <= date.today():
        parser.error("--date must be in the future")
    return args


async def main() -> None:
    args = parse_args()
    trip = Trip(args.origin.upper(), args.destination.upper(), args.date)
    passenger = Passenger.from_env()

    api_key = os.environ["GOOGLE_GENERATIVE_AI_API_KEY"]
    model = "google/" + os.getenv("GEMINI_MODEL", "gemini-3.8-flash").removeprefix("google/")

    history: list[dict] = []

    def record(log) -> None:
        entry = log.model_dump(mode="json")
        entry["timestamp"] = datetime.now(timezone.utc).isoformat()
        history.append(entry)

    bb_api_key, bb_project_id = os.getenv("BROWSERBASE_API_KEY"), os.getenv("BROWSERBASE_PROJECT_ID")
    bb_client: Browserbase | None = None
    bb_session_id: str | None = None
    if bb_api_key and bb_project_id:
        bb_client = Browserbase(api_key=bb_api_key)
        bb_session_id = bb_client.sessions.create(project_id=bb_project_id).id
        browser = await stagehand_browserbase.connect(api_key=bb_api_key, session_id=bb_session_id)
    else:
        browser = await local_browser.launch(headless=os.getenv("HEADLESS") == "true",
                                             viewport_width=1440, viewport_height=1000)

    monitor = LocalResourceMonitor()
    monitor_task = monitor.start()
    try:
        stagehand = await Stagehand.create(
            browser=browser,
            model=model,
            model_api_key=api_key,
            system_prompt=SAFETY_PROMPT,
            logging=StagehandClientLoggingConfig(level="info", format="json", on_log=record),
        )
        try:
            page = (await browser.context.pages())[0]
            domain = urlparse(args.url).netloc.removeprefix("www.")
            out_dir = OUTPUT_ROOT / domain / datetime.now().strftime("%Y%m%d-%H%M%S")
            out_dir.mkdir(parents=True, exist_ok=True)
            result = SiteResult(site=args.url, out_dir=out_dir)

            start = perf_counter()
            before = await stagehand.metrics()
            with contextlib.suppress(Exception):
                await browser.context.clear_cookies()  # fresh booking session

            try:
                await run_site(stagehand, page, trip, passenger, result)
            except Exception as error:
                result.status, result.message = "error", f"{type(error).__name__}: {error}"
                print(f"  ERROR: {result.message}")
                with contextlib.suppress(Exception):
                    await page.screenshot(path=str(out_dir / "error.png"))

            after = await stagehand.metrics()
            session = {
                "site": args.url,
                "status": result.status,
                "message": result.message,
                "execution_time_ms": round((perf_counter() - start) * 1000),
                "tokens_used": (after.total_prompt_tokens + after.total_completion_tokens)
                - (before.total_prompt_tokens + before.total_completion_tokens),
                "metrics_cumulative": after.model_dump(mode="json"),
                "resource_usage": monitor.summary(),
                "history": list(history),
            }
            if bb_client and bb_session_id:
                info = bb_client.sessions.retrieve(bb_session_id)
                session["browserbase_session"] = {"id": info.id, "status": info.status, "region": info.region}
            (out_dir / "session.json").write_text(json.dumps(session, indent=2))
            update_summary(result)
            print(f"  Status: {result.status} {result.message} -> {out_dir}")
        finally:
            with contextlib.suppress(Exception):
                await stagehand.close()
    finally:
        monitor_task.cancel()
        with contextlib.suppress(Exception):
            await browser.close()

    print(f"\nSummary written to {OUTPUT_ROOT / 'summary.csv'}")


if __name__ == "__main__":
    asyncio.run(main())
