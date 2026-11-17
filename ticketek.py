"""Ticketek event-ticketing drip-pricing POC.

1. Start on Ticketek AU (or a direct event URL), search for an event, and record the advertised
   "from" price, what that price is said to include, and every fee disclosure on the event page.
2. Pick the first on-sale show, wait out any Queue-it waiting room, and select the cheapest standard
   adult ticket (1-4 tickets, best available).
3. Walk the purchase chain (login, delivery method, ticket protection, donations, review) to the
   payment page, declining every optional extra and choosing the cheapest delivery method, and log
   every fee and add-on shown along the way.
4. Compare the advertised "from" price and the price chosen on the ticket-selection page with the
   ticket subtotal, booking fees, handling/transaction fee, delivery fee and total at checkout.

The run always stops on the payment page: no payment details are entered and nothing is purchased.
Tickets held in the cart are released by Ticketek when the hold timer runs out.

Usage:
    python ticketek.py --login                       # preload a My Ticketek login into `.chrome-profile` once
    python ticketek.py --query "comedy" --tickets 2
    python ticketek.py https://premier.ticketek.com.au/shows/show.aspx?sh=EXAMPLE
"""

import os
import sys

os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

import argparse
import asyncio
import contextlib
import csv
import json
import re
import resource
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from urllib.parse import quote_plus, urlparse

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

SITE_URL = "https://premier.ticketek.com.au/"
SEARCH_URL = "https://premier.ticketek.com.au/search/SearchResults.aspx?k={query}"
DEFAULT_QUERY = os.getenv("TICKETEK_QUERY", "comedy")
SETTLE_MS = 2500
MAX_SEARCH_STEPS = 6
MAX_SELECTION_STEPS = 5
MAX_PURCHASE_STEPS = 20
MAX_STUCK_REPEATS = 3
QUEUE_MAX_MINUTES = 15
OUTPUT_ROOT = Path("output")
CHROME_PROFILE_DIR = Path(".chrome-profile")

SAFETY_PROMPT = (
    "You are auditing a ticket-purchase flow for price transparency. Never enter payment card, gift card, voucher "
    "or promo code details, and never click a button that pays for, purchases or confirms an order (e.g. 'Pay now', "
    "'Buy now', 'Complete purchase', 'Place order' on a payment page). Never create an account, never join a "
    "resale/waitlist, and never add ticket insurance, protection, donations, parking, merchandise or upgrades."
)

STEP_TYPES = (
    "event_page, show_selection, ticket_selection, seat_map, queue, best_available_result, login_prompt, "
    "delivery_method, ticket_protection, donation, upsell_modal, cart, review, payment, confirmation, error, other"
)
EXTRA_CATEGORIES = (
    "booking_fee, handling_fee, delivery_fee, ticket_protection, refund_guarantee, donation, parking, merchandise, "
    "seat_upgrade, vip_package, membership, service_fee, payment_surcharge, other"
)
# Ticket types that are not a standard adult ticket; only chosen if nothing else is on sale.
RESTRICTED_TICKET_WORDS = (
    "concession", "child", "student", "pensioner", "senior", "companion", "wheelchair", "accessib", "carer",
    "family", "group", "member", "vip", "package", "hospitality", "meet", "infant", "under", "youth", "restricted view",
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class SearchCard(BaseModel):
    name: str = Field(description="Event name as displayed")
    venue: str | None = Field(description="Venue and city if shown")
    dates: str | None = Field(description="Date or date range as displayed")
    price_from: str | None = Field(description="Lowest price shown on the card, e.g. 'From $59.90'; null if not shown")
    on_sale: bool = Field(description="False if marked sold out, coming soon, cancelled or not yet on sale")
    is_event: bool = Field(description="True for a ticketed event; false for gift cards, news, venue or tour pages")


class SearchResults(BaseModel):
    on_results_page: bool = Field(description="True if the page lists events matching a search")
    on_event_page: bool = Field(
        description="True if this is already a single event's page showing its performances/dates and a buy/find tickets button"
    )
    blocked_reason: str | None = Field(
        description="If an 'Access denied', 'Request unsuccessful', CAPTCHA or bot check is shown, describe it; otherwise null"
    )
    events: list[SearchCard]


class Show(BaseModel):
    date_time: str = Field(description="Performance date and time as displayed")
    venue: str | None
    price_from: str | None = Field(description="Lowest price shown for this performance, if any")
    status: str = Field(description="One of: on_sale, selling_fast, sold_out, coming_soon, cancelled, unknown")
    button_label: str | None = Field(description="Label of this performance's buy button, e.g. 'Find tickets', 'Buy'")


class EventPage(BaseModel):
    event_name: str | None
    is_event_page: bool = Field(description="True if this is a single event page listing performances or ticket prices")
    price_from: str | None = Field(description="Headline 'tickets from' price for the event, e.g. 'From $59.90'")
    price_basis: str | None = Field(
        description="What the displayed prices are said to include, quoted, e.g. 'Prices include booking fee', "
        "'plus booking fee', 'per ticket'; null if not stated"
    )
    fee_disclosures: list[str] = Field(
        description="Every sentence on the page that mentions a booking fee, handling fee, transaction fee, delivery "
        "fee, card surcharge or 'fees apply', quoted exactly. Empty if none."
    )
    price_table: list[str] = Field(
        description="Rows of any ticket price table on the page, e.g. 'A Reserve Adult $89.90 (incl. $5.20 booking fee)'"
    )
    shows: list[Show]
    blocked_reason: str | None = Field(description="CAPTCHA / access-denied / bot check, if shown; otherwise null")


class TicketOption(BaseModel):
    section: str | None = Field(description="Price reserve, section or area, e.g. 'A Reserve', 'General Admission'")
    ticket_type: str = Field(description="Ticket type, e.g. 'Adult', 'Concession', 'Child', 'VIP Package'")
    price: str | None = Field(description="Price per ticket as displayed, e.g. '$89.90'")
    booking_fee: str | None = Field(description="Per-ticket booking fee shown next to this price, if any")
    price_includes_fees: bool | None = Field(
        description="True if the page says this price includes booking fees; False if it says fees are extra; null if unclear"
    )
    available: bool = Field(description="False if this option is sold out or unavailable")


class TicketSelection(BaseModel):
    on_ticket_selection: bool = Field(
        description="True if the page lets you choose ticket types/price reserves and quantities for one performance"
    )
    in_queue: bool = Field(description="True if this is a waiting room / virtual queue page")
    login_required: bool = Field(description="True if the page asks to sign in before tickets can be chosen")
    blocked_reason: str | None = Field(description="CAPTCHA / access-denied / bot check, if shown; otherwise null")
    performance: str | None = Field(description="Performance date/time and venue shown, if any")
    options: list[TicketOption]
    fee_disclosures: list[str] = Field(description="Sentences about booking, handling, delivery or card fees, quoted")
    summary: str


class HeldTickets(BaseModel):
    tickets_held: bool = Field(
        description="True if tickets are now reserved/held for us (seats allocated, cart shows tickets, or a hold timer runs)"
    )
    seats: str | None = Field(description="Allocated section/row/seats or 'General Admission', as displayed")
    ticket_type: str | None
    quantity: int | None
    unit_price: str | None = Field(description="Price per ticket shown now")
    booking_fee_per_ticket: str | None = Field(description="Booking/service fee per ticket shown now, if any")
    running_total: str | None = Field(description="Cart/order total shown now, if any")
    hold_timer: str | None = Field(description="Time remaining to complete the purchase, if shown, e.g. '09:45'")
    reason: str


class Fee(BaseModel):
    label: str
    amount: str


class Offer(BaseModel):
    category: str = Field(description=f"One of: {EXTRA_CATEGORIES}")
    label: str = Field(description="Name of the extra or fee as displayed, e.g. 'Ticket protection', 'Mobile ticket'")
    price: str | None = Field(description="Price as displayed, e.g. '$8.95', 'Free', '1.5%'; null if not shown")
    price_basis: str | None = Field(description="e.g. 'per ticket', 'per transaction', 'per order', if stated")
    selected: bool = Field(description="True if this option is currently ticked/selected and in the running total")
    mandatory: bool = Field(description="True if it is a compulsory charge that cannot be declined")


class StepState(BaseModel):
    step_type: str = Field(description=f"Which purchase step this is. One of: {STEP_TYPES}")
    heading: str | None = Field(description="Main heading or step name, e.g. 'Delivery options', 'Review your order'")
    is_payment_page: bool = Field(
        description="True only on the final step where a payment method / card details are requested to complete the purchase"
    )
    login_required: bool = Field(description="True if the page requires signing in to My Ticketek to continue")
    guest_checkout_available: bool = Field(description="True if a 'continue as guest' option is offered")
    in_queue: bool = Field(description="True if this is a waiting room / virtual queue page")
    blocked_reason: str | None = Field(
        description="If progress is blocked by a CAPTCHA, 'verify you are human', access-denied, session-expired or "
        "bot check (NOT a normal sign-in form), describe it; otherwise null"
    )
    offers: list[Offer] = Field(
        description="Every delivery option, add-on, upsell, fee or surcharge shown on this step (in the page or an open "
        "modal), with its price. Include free delivery options. Empty if none."
    )
    price_lines: list[Fee] = Field(description="Lines of the order summary / price panel currently displayed, if any")
    total: str | None = Field(description="Running order total currently displayed, e.g. '$97.85'")
    hold_timer: str | None = Field(description="Ticket hold time remaining, if shown")
    validation_errors: list[str] = Field(description="Any form validation or error messages currently displayed")
    summary: str


class LineItem(BaseModel):
    name: str = Field(description="Ticket line as displayed, e.g. 'A Reserve Adult'")
    quantity: int
    unit_price: str | None = Field(description="Price per ticket as displayed")
    line_price: str | None = Field(description="Line total as displayed")


class Surcharge(BaseModel):
    payment_method: str = Field(description="e.g. 'Visa/Mastercard', 'Amex', 'PayPal', 'Debit card'")
    fee: str = Field(description="Fee as displayed, e.g. '1.2%', '$2.50', 'No fee'")


class CheckoutDetails(BaseModel):
    is_checkout_page: bool = Field(description="True if on the payment / review-and-pay page with the order's price summary")
    blocked_reason: str | None = Field(description="Login wall, CAPTCHA or bot check, if any; otherwise null")
    items: list[LineItem]
    ticket_subtotal: str | None = Field(description="Ticket face value total before any fees, as displayed")
    booking_fees: str | None = Field(description="Total per-ticket booking/service fees, if itemised")
    handling_fee: str | None = Field(description="Per-transaction handling / transaction / order processing fee, if any")
    delivery_fee: str | None = Field(description="Fee for the chosen delivery method, e.g. '$0.00', '$6.50'")
    delivery_method: str | None = Field(description="Chosen delivery method, e.g. 'Mobile ticket', 'eTicket', 'Post'")
    extras: list[Fee] = Field(description="Optional extras included in the total (ticket protection, donation, parking, ...)")
    other_fees: list[Fee] = Field(
        description="Any other compulsory charge on top of the tickets that is not listed above and is not a tax"
    )
    payment_surcharges: list[Surcharge] = Field(
        description="Card/payment-method fees advertised on the page, whether or not applied to the total yet"
    )
    tax_included: bool = Field(description="True if GST is included in the prices shown")
    total: str | None = Field(description="Total amount due as displayed")
    currency: str | None = Field(description="Currency code if shown, e.g. 'AUD'")
    hold_timer: str | None
    summary: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def parse_price(value: str | None) -> float | None:
    """Parse '$1,234.56', 'AUD $21.00', 'From $15' into a float; None if no number. 'Free' -> 0.0."""
    if not value:
        return None
    if re.fullmatch(r"\s*(free|no (fee|charge)|included)\s*", value, re.I):
        return 0.0
    match = re.search(r"\d[\d.,\s]*", value)
    if not match:
        return None
    number = match.group(0).strip().replace(" ", "")
    if "," in number and "." in number:
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
    """Like parse_price, but None for percentages ('1.2%'), which are rates rather than amounts."""
    return None if value and "%" in value else parse_price(value)


def money_sum(values: list[float | None]) -> float | None:
    known = [v for v in values if v is not None]
    return round(sum(known), 2) if known else None


def diff(a: float | None, b: float | None) -> float | None:
    return round(a - b, 2) if a is not None and b is not None else None


def pct_of(amount: float | None, base: float | None) -> float | None:
    return round(amount / base * 100.0, 1) if amount and base else None


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "step"


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def is_standard_ticket(option: TicketOption) -> bool:
    text = f"{option.section or ''} {option.ticket_type}".lower()
    return not any(word in text for word in RESTRICTED_TICKET_WORDS)


def choose_ticket(options: list[TicketOption]) -> TicketOption | None:
    """Cheapest available standard adult ticket; any cheapest available ticket if there is no standard one."""
    priced = [o for o in options if o.available and parse_price(o.price) is not None]
    standard = [o for o in priced if is_standard_ticket(o)]
    return min(standard or priced, key=lambda o: parse_price(o.price), default=None)


def choose_show(shows: list[Show]) -> Show | None:
    return next((s for s in shows if s.status in ("on_sale", "selling_fast")), None) or next(
        (s for s in shows if s.status == "unknown"), None
    )


def mentions_fee(texts: list[str], *words: str) -> list[str]:
    return [t for t in texts if any(w in t.lower() for w in words)]


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
class SiteResult:
    site: str
    out_dir: Path
    status: str = "error"
    message: str = ""
    event_name: str | None = None
    event_url: str | None = None
    performance: str | None = None
    advertised_from_display: str | None = None
    price_basis: str | None = None
    fee_disclosures: list[str] = field(default_factory=list)
    ticket_options: list[dict] = field(default_factory=list)
    held: dict = field(default_factory=dict)
    items: list[dict] = field(default_factory=list)
    extras: list[dict] = field(default_factory=list)
    fee_breakdown_rows: list[dict] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    comparison: dict = field(default_factory=dict)


class Blocked(RuntimeError):
    """The site put a login wall, CAPTCHA or bot check in the way."""


# ---------------------------------------------------------------------------
# Page helpers
# ---------------------------------------------------------------------------
async def settle(page: Page, ms: int = SETTLE_MS) -> None:
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("load")
    await page.wait_for_timeout(ms)


async def shot(page: Page, path: Path) -> None:
    try:
        await page.screenshot(path=str(path), full_page=True)
    except Exception:
        with contextlib.suppress(Exception):
            await page.screenshot(path=str(path))


JS_IN_QUEUE = r"""() => {
    const host = location.hostname.toLowerCase();
    const text = (document.body && document.body.innerText || '').toLowerCase();
    return host.includes('queue-it') || host.startsWith('queue.')
        || /you are now in line|you're in the queue|you are in the queue|waiting room|your estimated wait|number in line/.test(text);
}"""


async def in_queue(page: Page) -> bool:
    with contextlib.suppress(Exception):
        return bool(await page.evaluate(f"({JS_IN_QUEUE})()"))
    return False


async def wait_out_queue(page: Page, out: Path) -> None:
    """Sit in a Queue-it waiting room (if any) until it lets us through."""
    if not await in_queue(page):
        return
    print("  In the Ticketek waiting room / queue; waiting...")
    await shot(page, out / "queue.png")
    for minute in range(QUEUE_MAX_MINUTES * 6):
        await page.wait_for_timeout(10_000)
        if not await in_queue(page):
            print(f"  Through the queue after ~{(minute + 1) * 10}s")
            await settle(page)
            return
    raise Blocked(f"Still in the waiting room after {QUEUE_MAX_MINUTES} minutes")


async def interactive_login_pause(page: Page, prompt_msg: str) -> bool:
    """Pause in headed mode so the user can sign in inside the persistent browser window."""
    if os.getenv("HEADLESS") == "true" or not sys.stdin.isatty():
        return False
    print(f"\n  [LOGIN REQUIRED] {prompt_msg}")
    print("  1. Sign in to My Ticketek in the open browser window (complete any email/SMS code).")
    print("  2. Wait until Ticketek returns you to your order.")
    await asyncio.to_thread(input, "  3. Press Enter here to continue... ")
    await settle(page)
    return True


async def dismiss_popups(stagehand: Stagehand) -> None:
    """Close cookie banners, newsletter or app prompts if any are visible."""
    with contextlib.suppress(Exception):
        found = await stagehand.observe(
            "Find the close, accept, dismiss or 'no thanks' button of any visible cookie banner, newsletter/promotional "
            "popup, app-install banner or location prompt. Do not pick anything that buys tickets or signs in. "
            "Return nothing if there is none."
        )
        for action in found.data[:2]:
            print(f"  Dismissing popup: {action.description}")
            await stagehand.act(action)


# ---------------------------------------------------------------------------
# Ticketek steps
# ---------------------------------------------------------------------------
async def find_event(stagehand: Stagehand, page: Page, query: str, result: SiteResult) -> None:
    """From the homepage, search for `query` and open the first on-sale event. Leaves the browser on its event page."""
    question = (
        f"We searched Ticketek for '{query}'. Is this a search results page or already a single event page? "
        "List the events shown with venue, dates, lowest 'from' price and whether they are on sale."
    )
    state = (await stagehand.extract(question, SearchResults)).data
    for _ in range(MAX_SEARCH_STEPS):
        if state.blocked_reason:
            raise Blocked(state.blocked_reason)
        if state.on_results_page or state.on_event_page:
            break
        step = await stagehand.act(
            f"Take the single next action to search for '{query}': open the site search if hidden, type '{query}' "
            "into the search box, then press the search button or Enter."
        )
        print(f"  {step.data.action_description}")
        await settle(page)
        state = (await stagehand.extract(question, SearchResults)).data
    if not (state.on_results_page or state.on_event_page):
        print("  Search box could not be driven; opening the search results URL directly...")
        await page.goto(SEARCH_URL.format(query=quote_plus(query)))
        await settle(page)
        state = (await stagehand.extract(question, SearchResults)).data
        if state.blocked_reason:
            raise Blocked(state.blocked_reason)
    if state.on_event_page:
        return

    events = [e for e in state.events if e.is_event and e.on_sale]
    write_csv(result.out_dir / "search_results.csv", list(SearchCard.model_fields), [e.model_dump() for e in state.events])
    await shot(page, result.out_dir / "search.png")
    if not events:
        raise LookupError(f"No on-sale events found for '{query}'")
    chosen = next((e for e in events if e.price_from), events[0])
    result.advertised_from_display = chosen.price_from
    print(f"  Opening event: {chosen.name} ({chosen.venue}, {chosen.dates}) {chosen.price_from or ''}")
    step = await stagehand.act(
        f"Click the search result for the event '{chosen.name}'"
        + (f" at {chosen.venue}" if chosen.venue else "")
        + " to open its event page (click its title or 'More info'/'Find tickets' link)."
    )
    print(f"    {step.data.action_description}")
    await settle(page)


async def read_event_page(stagehand: Stagehand, page: Page, result: SiteResult) -> EventPage:
    event = (
        await stagehand.extract(
            "This is a Ticketek event page. Extract the event name, the headline 'from' price, exactly what the page "
            "says the prices include, every sentence mentioning booking/handling/transaction/delivery/card fees, any "
            "ticket price table rows, and every performance (date/time, venue, from price, status, buy button label).",
            EventPage,
        )
    ).data
    if event.blocked_reason:
        raise Blocked(event.blocked_reason)
    result.event_name = event.event_name
    result.event_url = await page.url()
    result.advertised_from_display = event.price_from or result.advertised_from_display
    result.price_basis = event.price_basis
    result.fee_disclosures = list(dict.fromkeys(event.fee_disclosures))
    write_csv(result.out_dir / "shows.csv", list(Show.model_fields), [s.model_dump() for s in event.shows])
    await shot(page, result.out_dir / "event.png")
    print(f"  Event: {event.event_name} | from {event.price_from} | basis: {event.price_basis or 'not stated'} "
          f"| {len(event.shows)} show(s) | {len(event.fee_disclosures)} fee disclosure(s)")
    return event


async def open_ticket_selection(stagehand: Stagehand, page: Page, event: EventPage, result: SiteResult) -> TicketSelection:
    show = choose_show(event.shows)
    if show:
        result.performance = f"{show.date_time} {show.venue or ''}".strip()
        if show.price_from:
            result.advertised_from_display = result.advertised_from_display or show.price_from
    target = f"the performance on {show.date_time}" + (f" at {show.venue}" if show and show.venue else "") if show else (
        "the first performance that is on sale"
    )
    question = (
        "Are we on the ticket selection step for one performance (choose price reserve / ticket type and quantity)? "
        "List every ticket option with its section, ticket type, price, any per-ticket booking fee and availability, "
        "and quote every sentence about booking, handling, delivery or card fees."
    )
    selection = None
    for _ in range(MAX_SELECTION_STEPS):
        await wait_out_queue(page, result.out_dir)
        selection = (await stagehand.extract(question, TicketSelection)).data
        if selection.blocked_reason:
            raise Blocked(selection.blocked_reason)
        if selection.on_ticket_selection and selection.options:
            break
        if selection.login_required:
            await require_login(page, "Ticketek asks you to sign in before choosing tickets.")
            continue
        if selection.in_queue:
            continue
        step = await stagehand.act(
            f"Take the single next action to reach ticket selection for {target}: click its 'Find tickets' / "
            "'Buy tickets' / 'Buy' button (choose the performance or date first if a picker is shown). "
            "Do not click resale, waitlist, VIP or package links."
        )
        print(f"    {step.data.action_description}")
        await settle(page)
    if selection is None or not selection.on_ticket_selection:
        raise LookupError(f"Couldn't reach ticket selection: {selection.summary if selection else 'no page state'}")
    result.performance = selection.performance or result.performance
    result.fee_disclosures = list(dict.fromkeys(result.fee_disclosures + selection.fee_disclosures))
    result.ticket_options = [o.model_dump() for o in selection.options]
    write_csv(result.out_dir / "products.csv", list(TicketOption.model_fields), result.ticket_options)
    await shot(page, result.out_dir / "tickets.png")
    print(f"  Ticket selection: {len(selection.options)} option(s) for {result.performance}")
    return selection


async def hold_tickets(stagehand: Stagehand, page: Page, option: TicketOption, quantity: int, result: SiteResult) -> dict:
    name = " ".join(filter(None, [option.section, option.ticket_type]))
    item: dict = {
        "name": f"{result.event_name or 'Event'} - {name}",
        "selected_variant": name,
        "quantity": quantity,
        "shop_price_display": result.advertised_from_display,
        "shop_price": parse_price(result.advertised_from_display),
        "selected_price_display": option.price,
        "selected_price": parse_price(option.price),
        "selection_booking_fee_display": option.booking_fee,
        "price_includes_fees": option.price_includes_fees,
        "status": "skipped",
    }
    question = (
        f"We asked for {quantity} x '{name}' ticket(s). Are tickets now held for us (seats allocated, in the cart, or "
        "a hold timer running)? Report seats, ticket type, quantity, price and booking fee per ticket, running total "
        "and the hold timer."
    )
    held = None
    for attempt in range(4):
        step = await stagehand.act(
            f"Take the single next action to buy {quantity} '{name}' ticket(s) at {option.price} each using best "
            f"available seating: set the quantity for '{name}' to {quantity} (leave every other ticket type at 0), "
            "then click 'Find tickets' / 'Search' / 'Add to cart' / 'Continue'. If a seat map is shown, use "
            "'Best available' rather than picking seats. If a popup offers better seats or an upgrade, decline it."
        )
        print(f"    Ticket selection: {step.data.action_description}")
        await settle(page)
        await wait_out_queue(page, result.out_dir)
        held = (await stagehand.extract(question, HeldTickets)).data
        if held.tickets_held:
            break
    if held is not None:
        result.held = held.model_dump()
        item["held_seats"] = held.seats
        item["held_unit_price_display"] = held.unit_price
        item["held_booking_fee_display"] = held.booking_fee_per_ticket
        item["cart_total_display"] = held.running_total
        item["status"] = "added" if held.tickets_held else "add_failed"
        print(f"  Held: {held.quantity} x {held.ticket_type} {held.unit_price} ({held.seats}) | "
              f"fee/ticket {held.booking_fee_per_ticket} | total {held.running_total} | timer {held.hold_timer}")
    return item


async def require_login(page: Page, reason: str) -> None:
    if not await interactive_login_pause(page, reason):
        raise Blocked(f"{reason} Run `python ticketek.py --login` once to save a My Ticketek login, then re-run.")


NEXT_ACTIONS = {
    "best_available_result": "Accept the seats offered and continue (e.g. 'Continue', 'Accept seats', 'Add to cart'). "
    "Do not search again for better seats.",
    "login_prompt": "Continue as a guest if offered (e.g. 'Continue as guest'); otherwise click 'Continue'. "
    "Never create an account.",
    "delivery_method": "Choose the cheapest delivery method, preferring a free one (e.g. 'Mobile ticket', "
    "'Ticketek app', 'eTicket', 'Print at home' or 'Collect from box office' when free) and then continue.",
    "ticket_protection": "Decline ticket protection / refund insurance (e.g. 'No thanks', 'No, I don't want to protect "
    "my tickets', 'I'll risk it') and continue.",
    "donation": "Do not donate. Choose 'No thanks' / '$0' / 'No donation' and continue.",
    "upsell_modal": "Decline the offer in the open popup (e.g. 'No thanks', 'Skip', 'Continue without') without adding anything.",
    "cart": "Click the button that continues to checkout (e.g. 'Checkout', 'Continue'). Do not add anything.",
}
DEFAULT_NEXT_ACTION = (
    "Do not add any optional extra (no ticket protection, donations, parking, merchandise, upgrades or packages). If "
    "the page requires an explicit choice, choose the free or decline option. Otherwise click the button that "
    "continues towards payment (e.g. 'Continue', 'Next', 'Checkout', 'Continue to payment'). Never click a button "
    "that pays or completes the purchase."
)


def record_offers(result: SiteResult, index: int, step_type: str, heading: str | None, offers: list[Offer]) -> None:
    seen = {(e["step_type"], e["category"], e["label"].lower()) for e in result.extras}
    for offer in offers:
        key = (step_type, offer.category, offer.label.lower())
        if key in seen:
            continue
        seen.add(key)
        result.extras.append({
            "step": index,
            "step_type": step_type,
            "step_heading": heading,
            "category": offer.category,
            "label": offer.label,
            "price_display": offer.price,
            "price": parse_amount(offer.price),
            "price_basis": offer.price_basis,
            "selected_by_default": offer.selected,  # we never add extras, so anything selected was pre-selected
            "mandatory": offer.mandatory,
        })


async def walk_to_payment(stagehand: Stagehand, page: Page, result: SiteResult) -> bool:
    """Step through the purchase flow, recording every fee and extra offered, until the payment page."""
    out = result.out_dir
    last_signature, repeats = None, 0
    for index in range(1, MAX_PURCHASE_STEPS + 1):
        await wait_out_queue(page, out)
        state = (
            await stagehand.extract(
                "We are part-way through buying event tickets and want to reach the payment page without buying any "
                "optional extras. Identify the current step, list every delivery option, add-on, upsell, fee or "
                "surcharge offered on it with prices (including in any open popup), and report the order summary "
                "lines, running total, hold timer, validation errors, whether sign-in is required and anything blocking.",
                StepState,
            )
        ).data
        step_type = "payment" if state.is_payment_page else state.step_type
        print(f"  Step {index}: {step_type} ({state.heading}) | total {state.total} | {len(state.offers)} offer(s)")
        await shot(page, out / f"step-{index:02d}-{slug(step_type)}.png")
        record_offers(result, index, step_type, state.heading, state.offers)
        result.steps.append({
            "step": index, "step_type": step_type, "heading": state.heading, "url": await page.url(),
            "total_display": state.total, "total": parse_price(state.total), "hold_timer": state.hold_timer,
            "price_lines": [line.model_dump() for line in state.price_lines],
            "offers": len(state.offers), "validation_errors": state.validation_errors, "summary": state.summary,
        })
        if index == 1:
            await shot(page, out / "cart.png")

        if state.blocked_reason:
            raise Blocked(state.blocked_reason)
        if state.is_payment_page or step_type == "payment":
            return True
        if step_type == "confirmation":
            raise RuntimeError("Reached a confirmation page unexpectedly; stopping")
        if state.login_required and not state.guest_checkout_available:
            await require_login(page, "Ticketek requires a My Ticketek sign-in to continue the purchase.")
            continue

        signature = (step_type, state.heading, await page.url())
        repeats = repeats + 1 if signature == last_signature else 0
        last_signature = signature
        if repeats >= MAX_STUCK_REPEATS:
            result.message = f"Stuck on '{state.heading or step_type}': {'; '.join(state.validation_errors) or state.summary}"
            return False

        instruction = NEXT_ACTIONS.get(step_type, DEFAULT_NEXT_ACTION)
        if state.validation_errors:
            instruction = (
                f"The page shows these errors: {'; '.join(state.validation_errors[:3])}. Fix the first one if it is a "
                f"missing choice (choose the free or decline option, never a paid extra). Otherwise: {instruction}"
            )
        step = await stagehand.act(instruction)
        print(f"    {step.data.action_description}")
        await settle(page)

    result.message = f"Payment page not reached within {MAX_PURCHASE_STEPS} steps"
    return False


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
def compare_prices(result: SiteResult, checkout: CheckoutDetails | None) -> dict:
    added = [i for i in result.items if i["status"] == "added"]
    quantity = added[0]["quantity"] if added else 0

    line = checkout.items[0] if checkout and checkout.items else None
    for item in added:
        expected = item["selected_price"] if item["selected_price"] is not None else item["shop_price"]
        unit = parse_price(line.unit_price) if line else None
        if unit is None and line and parse_price(line.line_price) is not None and line.quantity:
            unit = round(parse_price(line.line_price) / line.quantity, 2)
        item["expected_price"] = expected
        item["selected_vs_shop_diff"] = diff(item["selected_price"], item["shop_price"])
        item["checkout_price_display"] = (line.unit_price or line.line_price) if line else None
        item["checkout_price"] = unit
        item["checkout_vs_expected_diff"] = diff(unit, expected)
        item["price_increased"] = (item["checkout_vs_expected_diff"] or 0) > 0

    selected_unit = added[0]["expected_price"] if added else None
    expected_subtotal = round(selected_unit * quantity, 2) if selected_unit is not None else None
    advertised_from = parse_price(result.advertised_from_display)

    def shown(attr: str):
        return getattr(checkout, attr) if checkout else None

    subtotal = parse_price(shown("ticket_subtotal"))
    booking = parse_amount(shown("booking_fees"))
    handling = parse_amount(shown("handling_fee"))
    delivery = parse_amount(shown("delivery_fee"))
    other = [(f.label, parse_amount(f.amount)) for f in (checkout.other_fees if checkout else [])]
    included_extras = [{"label": f.label, "amount": parse_amount(f.amount)} for f in (checkout.extras if checkout else [])]
    surcharges = [s.model_dump() for s in (checkout.payment_surcharges if checkout else [])]
    chargeable = [s for s in surcharges if (parse_price(s["fee"]) or 0) > 0]

    total = parse_price(shown("total"))
    if total is None:  # payment page not reached: fall back to the last running total seen
        total = next((s["total"] for s in reversed(result.steps) if s["total"] is not None), None)

    # A booking fee shown only at checkout counts as added unless the selection page said prices include fees.
    includes_fees = added[0].get("price_includes_fees") if added else None
    fees = [{"label": label, "amount": amt} for label, amt in (
        ("Booking fee", booking if not includes_fees else None),
        ("Handling / transaction fee", handling),
        ("Delivery fee", delivery),
    ) if amt]
    fees += [{"label": label, "amount": amt} for label, amt in other if amt]
    fees_total = money_sum([f["amount"] for f in fees]) or 0.0

    disclosures = result.fee_disclosures
    def disclosed(*words: str) -> str:
        hits = mentions_fee(disclosures, *words)
        return hits[0] if hits else "not disclosed upfront"

    base = subtotal or expected_subtotal

    def fee_row(fee_type, label, advertised, checkout_display, amount, mandatory=True) -> dict:
        return {"fee_type": fee_type, "label": label, "advertised_display": advertised,
                "checkout_display": checkout_display, "amount": amount, "pct_of_subtotal": pct_of(amount, base),
                "mandatory": mandatory}

    result.fee_breakdown_rows = [
        fee_row("booking_fee", "Booking fees (all tickets)",
                added[0].get("selection_booking_fee_display") or disclosed("booking fee", "service fee") if added else None,
                shown("booking_fees") or "not shown", booking),
        fee_row("handling_fee", "Handling / transaction fee", disclosed("handling", "transaction", "processing"),
                shown("handling_fee") or "not shown", handling),
        fee_row("delivery_fee", f"Delivery ({shown('delivery_method') or 'method not shown'})",
                disclosed("delivery", "postage", "mobile ticket", "eticket"), shown("delivery_fee") or "not shown",
                delivery),
        *(fee_row("other_fee", label, "not disclosed upfront", amount_display, amt)
          for (label, amt), amount_display in zip(other, [f.amount for f in (checkout.other_fees if checkout else [])])),
        *(fee_row("extra", e["label"], "n/a (optional)", e["amount"], e["amount"], mandatory=False) for e in included_extras),
        *(fee_row("payment_surcharge", s["payment_method"], disclosed("card", "surcharge", "payment"), s["fee"],
                  parse_amount(s["fee"]), mandatory=False) for s in surcharges),
    ]

    preselected = [e["label"] for e in result.extras
                   if e["selected_by_default"] and not e["mandatory"] and (e["price"] or 0) > 0
                   and e["category"] not in ("booking_fee", "handling_fee", "service_fee", "payment_surcharge")]
    delivery_options = [e for e in result.extras if e["category"] == "delivery_fee" and e["price"] is not None]
    cheapest_delivery = min((e["price"] for e in delivery_options), default=None)

    reasons = [
        reason for reason, hit in (
            ("ticket price increased at checkout", any(i["price_increased"] for i in added)),
            ("checkout ticket subtotal higher than selected price x quantity", (diff(subtotal, expected_subtotal) or 0) > 0),
            (f"booking fee added at checkout ({shown('booking_fees')}) not included in the selected ticket price",
             (booking or 0) > 0 and not includes_fees),
            (f"handling/transaction fee added ({shown('handling_fee')})", (handling or 0) > 0),
            (f"no free delivery method; cheapest is ${cheapest_delivery:.2f}" if cheapest_delivery else "",
             (cheapest_delivery or 0) > 0),
            ("other non-tax fees added: " + ", ".join(l for l, a in other if a), any(a for _, a in other)),
            ("total at checkout higher than selected ticket price x quantity with no extras chosen",
             (diff(total, expected_subtotal) or 0) > 0),
            ("optional extras pre-selected: " + ", ".join(preselected), bool(preselected)),
            ("payment surcharge applies: " + ", ".join(f"{s['payment_method']} {s['fee']}" for s in chargeable),
             bool(chargeable)),
        ) if hit
    ]

    by_category: dict[str, int] = {}
    for extra in result.extras:
        by_category[extra["category"]] = by_category.get(extra["category"], 0) + 1

    return {
        "event_name": result.event_name,
        "event_url": result.event_url,
        "performance": result.performance,
        "items_added": quantity,
        "advertised_from_display": result.advertised_from_display,
        "advertised_from": advertised_from,
        "advertised_price_basis": result.price_basis,
        "fee_disclosures": disclosures,
        "selected_unit_price": selected_unit,
        "selected_vs_advertised_from_diff": diff(selected_unit, advertised_from),
        "from_price_not_available": (diff(selected_unit, advertised_from) or 0) > 0,
        "expected_subtotal": expected_subtotal,
        "cart_subtotal_display": added[0].get("cart_total_display") if added else None,
        "checkout_subtotal_display": shown("ticket_subtotal"),
        "checkout_subtotal": subtotal,
        "subtotal_vs_expected_diff": diff(subtotal, expected_subtotal),
        "booking_fees_display": shown("booking_fees"),
        "booking_fees": booking,
        "handling_fee_display": shown("handling_fee"),
        "handling_fee": handling,
        "delivery_method": shown("delivery_method"),
        "shipping": shown("delivery_fee") or "not shown",
        "delivery_fee": delivery,
        "other_fees": fees,
        "other_fees_total": fees_total,
        "fees_pct_of_subtotal": pct_of(fees_total, base),
        "extras_in_total": included_extras,
        "payment_surcharges": surcharges,
        "extras_offered": len(result.extras),
        "extras_offered_by_category": by_category,
        "extras_preselected": preselected,
        "tax_included": shown("tax_included"),
        "checkout_total_display": shown("total"),
        "checkout_total": total,
        "total_vs_expected_diff": diff(total, expected_subtotal),
        "total_vs_advertised_from_diff": diff(total, round(advertised_from * quantity, 2) if advertised_from and quantity else None),
        "currency": shown("currency") or "AUD",
        "drip_detected": bool(reasons),
        "drip_reasons": reasons,
        "checkout_summary": shown("summary"),
    }


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
EXTRA_FIELDS = ["step", "step_type", "step_heading", "category", "label", "price_display", "price",
                "price_basis", "selected_by_default", "mandatory"]
ITEM_FIELDS = ["name", "selected_variant", "quantity", "shop_price_display", "selected_price_display",
               "selection_booking_fee_display", "held_seats", "checkout_price_display", "checkout_vs_expected_diff",
               "price_increased", "status"]
FEE_BREAKDOWN_FIELDS = ["fee_type", "label", "advertised_display", "checkout_display", "amount",
                        "pct_of_subtotal", "mandatory"]


def write_reports(result: SiteResult, query: str | None, checkout_url: str | None) -> None:
    """Written even when a run ends early, so whatever fees and extras were seen are kept."""
    out = result.out_dir
    report = {
        "site": result.site, "shop_url": result.event_url, "checkout_url": checkout_url,
        "status": result.status, "message": result.message,
        "search": {"query": query, "tickets": result.items[0]["quantity"] if result.items else None},
        "ticket_options": result.ticket_options, "held": result.held,
        "items": result.items, "fee_breakdown": result.fee_breakdown_rows,
        **result.comparison, "extras": result.extras, "steps": result.steps,
    }
    (out / "price_comparison.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    write_csv(out / "price_comparison.csv", ITEM_FIELDS, result.items)
    write_csv(out / "extras.csv", EXTRA_FIELDS, result.extras)
    write_csv(out / "fees_breakdown.csv", FEE_BREAKDOWN_FIELDS, result.fee_breakdown_rows)


def print_audit(result: SiteResult) -> None:
    c = result.comparison
    print("\n  --- Fees Breakdown (Ticketek) ---")
    for row in result.fee_breakdown_rows:
        pct = f" ({row['pct_of_subtotal']}% of tickets)" if row.get("pct_of_subtotal") else ""
        print(f"    {row['label']:<34} | Upfront: {str(row['advertised_display'])[:40]:<40} "
              f"| Checkout: {row['checkout_display']}{pct}")
    print(f"\n  Advertised from: {c['advertised_from_display']} | Selected: {c['selected_unit_price']} x {c['items_added']} "
          f"= {c['expected_subtotal']} | Tickets at checkout: {c['checkout_subtotal_display']} "
          f"| Fees: {c['other_fees_total']} | Total: {c['checkout_total_display']}")
    print(f"  Drip pricing detected: {'YES - ' + '; '.join(c['drip_reasons']) if c['drip_detected'] else 'NO'}")


def update_summary(result: SiteResult) -> None:
    """Same columns as the other workflows, so all of them write to one output/summary.csv."""
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
    rows: list[dict] = []
    if path.exists():
        with open(path, newline="", encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r.get("site") != result.site]
    write_csv(path, list(row), rows + [row])


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
async def run_site(stagehand: Stagehand, page: Page, url: str, query: str, tickets: int, result: SiteResult) -> None:
    out = result.out_dir
    checkout: CheckoutDetails | None = None
    print(f"\n=== {url} | {tickets} ticket(s){'' if '/shows/' in url else f' | search: {query}'} ===")
    try:
        await page.goto(url)
        await settle(page)
        await wait_out_queue(page, out)
        await dismiss_popups(stagehand)

        if urlparse(url).path in ("", "/"):
            await find_event(stagehand, page, query, result)
            await wait_out_queue(page, out)
            await dismiss_popups(stagehand)

        event = await read_event_page(stagehand, page, result)
        result.site = result.event_url or url  # one summary row per event
        if event.shows and choose_show(event.shows) is None:
            result.status, result.message = "no_tickets_found", "No performance of this event is on sale"
            return

        selection = await open_ticket_selection(stagehand, page, event, result)
        option = choose_ticket(selection.options)
        if option is None:
            result.status, result.message = "no_tickets_found", "No available ticket option with a price"
            return
        print(f"  Choosing: {tickets} x {option.section or ''} {option.ticket_type} {option.price} "
              f"(booking fee {option.booking_fee or 'not shown'})")
        item = await hold_tickets(stagehand, page, option, tickets, result)
        result.items.append(item)
        if item["status"] != "added":
            result.status, result.message = "add_to_cart_failed", "The tickets could not be held"
            return

        if await walk_to_payment(stagehand, page, result):
            await shot(page, out / "checkout.png")
            checkout = (
                await stagehand.extract(
                    "Extract the order's price summary from this payment page: ticket lines (name, quantity, unit and "
                    "line price), ticket subtotal, booking fees, handling/transaction fee, delivery method and fee, "
                    "optional extras included, any other fees, total due and currency. Also list every card or "
                    "payment-method surcharge advertised, and report any login wall, CAPTCHA or bot check. "
                    "Do not interact with the page.",
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
                                      "price": parse_amount(s.fee), "price_basis": "per order",
                                      "selected_by_default": False, "mandatory": False})
        else:
            result.status = "no_checkout"
    except Blocked as error:
        result.status, result.message = "blocked", str(error)
    except LookupError as error:
        result.status, result.message = "no_tickets_found", str(error)
    finally:
        result.comparison = compare_prices(result, checkout)
        checkout_url = None
        with contextlib.suppress(Exception):
            checkout_url = await page.url()
        write_reports(result, query, checkout_url)
    print_audit(result)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ticketek event-ticketing drip-pricing POC")
    parser.add_argument("urls", nargs="*", default=[SITE_URL],
                        help=f"Ticketek homepage or event page URL(s) (default: {SITE_URL})")
    parser.add_argument("--query", default=DEFAULT_QUERY,
                        help=f"Search term when starting from the homepage (default: '{DEFAULT_QUERY}')")
    parser.add_argument("--tickets", type=int, default=1, choices=range(1, 5), metavar="N",
                        help="Number of tickets to hold (1-4, default 1)")
    parser.add_argument("--login", action="store_true",
                        help="Open the persistent local Chrome profile on Ticketek so you can sign in, then exit")
    parser.add_argument("--local", action="store_true",
                        help="Use local Chrome with `.chrome-profile` even if BROWSERBASE_API_KEY is set")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

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
    if bb_api_key and bb_project_id and not args.local and not args.login:
        bb_client = Browserbase(api_key=bb_api_key)
        bb_session_id = bb_client.sessions.create(project_id=bb_project_id).id
        browser = await stagehand_browserbase.connect(api_key=bb_api_key, session_id=bb_session_id)
    else:
        profile_dir = CHROME_PROFILE_DIR.resolve()
        profile_dir.mkdir(parents=True, exist_ok=True)
        browser = await local_browser.launch(
            headless=False if args.login else os.getenv("HEADLESS") == "true",
            user_data_dir=str(profile_dir),
            viewport_width=1440,
            viewport_height=1000,
        )

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

            if args.login:
                print(f"\n=== Preloading a My Ticketek login into {CHROME_PROFILE_DIR.resolve()} ===")
                await page.goto(SITE_URL)
                await settle(page)
                await interactive_login_pause(page, "Click 'Sign in' / 'My Ticketek' in the browser window and log in.")
                print(f"  Login session saved to {CHROME_PROFILE_DIR.resolve()}")
                return

            for url in args.urls:
                url = url if url.startswith("http") else f"https://{url}"
                domain = urlparse(url).netloc.removeprefix("www.").removeprefix("premier.")
                out_dir = OUTPUT_ROOT / domain / datetime.now().strftime("%Y%m%d-%H%M%S")
                out_dir.mkdir(parents=True, exist_ok=True)
                result = SiteResult(site=url, out_dir=out_dir)

                history.clear()
                start = perf_counter()
                before = await stagehand.metrics()
                try:
                    await run_site(stagehand, page, url, args.query, args.tickets, result)
                except Exception as error:
                    result.status, result.message = "error", f"{type(error).__name__}: {error}"
                    print(f"  ERROR: {result.message}")
                    with contextlib.suppress(Exception):
                        await page.screenshot(path=str(out_dir / "error.png"))

                after = before
                with contextlib.suppress(Exception):
                    after = await stagehand.metrics()
                session = {
                    "site": result.site,
                    "event_name": result.event_name,
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
                (out_dir / "session.json").write_text(json.dumps(session, indent=2), encoding="utf-8")
                update_summary(result)
                print(f"  Status: {result.status} {result.message} -> {out_dir}")
        finally:
            with contextlib.suppress(Exception):
                await stagehand.close()
    finally:
        monitor_task.cancel()
        with contextlib.suppress(Exception):
            await browser.close()

    if not args.login:
        print(f"\nSummary written to {OUTPUT_ROOT / 'summary.csv'}")


if __name__ == "__main__":
    asyncio.run(main())
