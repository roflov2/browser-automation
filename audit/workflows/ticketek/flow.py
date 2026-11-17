"""Ticketek: find the event, pick a session and tickets, and walk the purchase to the payment page."""

import logging
import re
import time

from stagehand import Page, Stagehand

from ...core import Blocked, FlowChanged, Item, SiteResult, new_item, parse_price, record_offers, select_price, slug
from ...page import dismiss_popups, fingerprint, pause_for_login, settle, shot, wait_for_render
from ...safety import current_guard, safe_act
from .models import (
    EventPage,
    FindState,
    HeldTickets,
    PriceOption,
    Session,
    StepState,
    TicketekResult,
    TicketOptions,
)

log = logging.getLogger(__name__)

SITE_URL = "https://premier.ticketek.com.au/"
SETTLE_MS = 2500
MAX_FIND_STEPS = 6
MAX_SELECT_STEPS = 5
MAX_BOOKING_STEPS = 15
MAX_STUCK_REPEATS = 3
QUEUE_POLL_S = 20
QUEUE_LIMIT_S = 15 * 60  # an on-sale queue can be long; give up rather than wait indefinitely

BOOKABLE = ("on_sale", "selling_fast", "limited")
# Discounted or restricted ticket types; the audit buys the ticket an ordinary adult would buy.
RESTRICTED_TYPES = re.compile(
    r"child|kid|junior|youth|concession|pension|senior|student|family|member|companion|carer|"
    r"group|school|infant|under\s*\d+|wheelchair|access|hospitality|vip|package",
    re.IGNORECASE,
)


async def dismiss_ticketek_popups(stagehand: Stagehand, page: Page) -> None:
    await dismiss_popups(
        stagehand,
        page,
        "cookie banner, newsletter/sign-up popup, app-install banner or location selector",
        caution="Do NOT click 'Sign in', 'Find tickets', 'Buy tickets' or anything that adds tickets. ",
    )


# ---------------------------------------------------------------------------
# 1. Event
# ---------------------------------------------------------------------------
async def find_event(stagehand: Stagehand, page: Page, query: str | None) -> None:
    """From the homepage or a search page, reach the event page of an on-sale event matching `query`."""
    wanted = f"an on-sale event matching '{query}'" if query else "the first on-sale event featured on the page"
    question = (
        f"We are looking for {wanted}. What kind of page is this, and which matching on-sale events are listed? "
        "Report anything blocking progress."
    )
    for _ in range(MAX_FIND_STEPS):
        state = (await stagehand.extract(question, FindState)).data
        if state.blocked_reason:
            raise Blocked(state.blocked_reason)
        if state.page_type in ("event", "ticket_selection"):
            log.info(f"  Event page: {state.event_name or '?'}")
            return
        if state.page_type == "search_results" and state.matching_events:
            instruction = (
                f"Open the event page of '{state.matching_events[0]}' by clicking its title or its 'Find tickets' / "
                "'More info' link."
            )
        elif query:
            instruction = (
                f"Search the site for '{query}': type it into the search box (open the search first if only a "
                "search icon is visible) and press Enter."
            )
        else:
            instruction = "Open the event page of the first on-sale event featured on the page."
        step = await safe_act(stagehand, page, instruction)
        log.info(f"  {step.description} -> {state.reason}")
        await settle(page, SETTLE_MS)
        await wait_for_render(page)
    raise FlowChanged(f"Couldn't reach the event page for {wanted}")


def choose_session(sessions: list[Session], hint: str | None) -> Session | None:
    """The first bookable session, or the first bookable one whose date/venue contains `hint`."""
    bookable = [s for s in sessions if s.status in BOOKABLE]
    if hint:
        words = hint.lower().split()
        matching = [s for s in bookable if all(w in f"{s.date} {s.venue or ''}".lower() for w in words)]
        return matching[0] if matching else None
    return bookable[0] if bookable else None


async def open_session(stagehand: Stagehand, page: Page, session: Session) -> TicketOptions:
    """Click through to the ticket selection page of `session` and read its prices."""
    described = f"the session on {session.date}" + (f" at {session.venue}" if session.venue else "")
    question = (
        "List every ticket area and price type with its price per ticket and availability, what the prices cover, "
        "and anything blocking progress. Do not interact with the page."
    )
    for _ in range(MAX_SELECT_STEPS):
        options = (await stagehand.extract(question, TicketOptions)).data
        if options.blocked_reason:
            raise Blocked(options.blocked_reason)
        if options.is_ticket_selection and options.options:
            return options
        step = await safe_act(
            stagehand,
            page,
            f"Take the single next action to see ticket prices for {described}: click its 'Find tickets', "
            "'Buy tickets' or 'Get tickets' button (expand the session list or choose the date first if needed). "
            "Do not choose resale tickets.",
        )
        log.info(f"    Session: {step.description}")
        await settle(page, SETTLE_MS)
        await wait_for_render(page)
    raise FlowChanged(f"Couldn't reach the ticket selection page for {described}")


def option_name(option: PriceOption) -> str:
    return " - ".join(filter(None, [option.area, option.price_type]))


def choose_option(options: list[PriceOption]) -> PriceOption | None:
    """The cheapest available ticket an ordinary adult can buy; any cheapest ticket if none is plainly adult."""
    priced = [o for o in options if o.available and parse_price(o.price) is not None]
    standard = [o for o in priced if not RESTRICTED_TYPES.search(o.price_type)]
    return min(standard or priced, key=lambda o: parse_price(o.price) or 0.0, default=None)


def cheapest_offered(options: list[PriceOption]) -> float | None:
    prices = [p for o in options if o.available and (p := parse_price(o.price)) is not None]
    return min(prices, default=None)


async def hold_tickets(stagehand: Stagehand, page: Page, option: PriceOption, quantity: int) -> Item:
    """Choose `quantity` tickets of `option` (best available seats) and get them held in the cart."""
    item = new_item(option_name(option), option.price, quantity=quantity)
    described = f"{quantity} x '{option.price_type}'" + (f" in '{option.area}'" if option.area else "")
    question = (
        f"We want {described} tickets. Are tickets now held/reserved (a cart, basket, order summary or countdown "
        "timer)? Report the area, seats, ticket type, quantity, price per ticket and total."
    )
    held = None
    for _ in range(MAX_SELECT_STEPS):
        step = await safe_act(
            stagehand,
            page,
            f"Take the single next action to get {described} tickets. In order: choose the '{option.area or 'cheapest'}' "
            f"area or price band if a choice is needed; set the quantity of '{option.price_type}' to {quantity} "
            "(leave every other ticket type at 0); choose 'Best available' rather than picking seats on a map; then "
            "click the button that finds or reserves the tickets (e.g. 'Find tickets', 'Get tickets', 'Add to cart', "
            "'Continue'). Never add parking, merchandise, packages or upgrades.",
            follow_up=True,  # a quantity dropdown is opened, then the number is picked
        )
        log.info(f"    Tickets: {step.description}")
        await settle(page, SETTLE_MS)
        held = (await stagehand.extract(question, HeldTickets)).data
        if held.tickets_held:
            break
    if held is not None:
        item["selected_variant"] = " - ".join(filter(None, [held.area, held.price_type, held.seats]))
        select_price(item, held.unit_price)
        item["cart_total_display"] = held.total
        item["held_quantity"] = held.quantity
        item["status"] = "added" if held.tickets_held else "add_failed"
    return item


# ---------------------------------------------------------------------------
# 2. Cart to payment
# ---------------------------------------------------------------------------
NEXT_ACTIONS = {
    "cart": "Click the button that continues from the cart towards checkout (e.g. 'Checkout', 'Continue', 'Next').",
    "delivery": "Choose the free delivery method (e.g. 'Mobile ticket', 'eTicket', 'Print at home'); if none is free, "
    "choose the cheapest one. Then click the button that continues to the next step.",
    "extras": "Do not add any extra (no refund protection, insurance, parking, merchandise or donation). If an "
    "explicit choice is required, choose the decline option (e.g. 'No thanks', 'I don't want ...'), then continue.",
    "upsell_modal": "Decline the offer in the open popup (e.g. 'No thanks', 'Skip', 'Continue without') without adding "
    "anything.",
    "seat_map": "Do not pick seats by hand. Accept the seats offered (e.g. 'Accept seats', 'Continue') or choose "
    "'Best available'.",
}
DEFAULT_NEXT_ACTION = (
    "Do not add any optional extra (no refund protection, insurance, parking, merchandise, upgrades or donations). "
    "If the page requires an explicit choice, choose the decline option. Otherwise click the button that continues "
    "towards payment (e.g. 'Continue', 'Next', 'Proceed to payment'). Never click a button that pays or completes "
    "the purchase, and never sign up for anything."
)


async def wait_in_queue(stagehand: Stagehand, page: Page) -> None:
    """Sit in an on-sale queue / waiting room until it lets us through; Blocked if it takes too long."""
    started = time.monotonic()
    log.info("  In the Ticketek queue; waiting (this can take a while)...")
    while time.monotonic() - started < QUEUE_LIMIT_S:
        await settle(page, QUEUE_POLL_S * 1000)
        state = (
            await stagehand.extract("Is this still a queue / waiting room page? Report anything blocking.", StepState)
        ).data
        if state.blocked_reason:
            raise Blocked(state.blocked_reason)
        if not state.in_queue:
            return
    raise Blocked(f"Still in the Ticketek queue after {QUEUE_LIMIT_S // 60} minutes")


async def next_action(stagehand: Stagehand, page: Page, state: StepState, page_type: str) -> None:
    instruction = NEXT_ACTIONS.get(page_type, DEFAULT_NEXT_ACTION)
    if state.validation_errors:
        instruction = (
            f"The page shows these errors: {'; '.join(state.validation_errors[:3])}. Fix the first one if it is a "
            f"missing choice (never by adding a paid extra or entering payment details). Otherwise: {instruction}"
        )
    step = await safe_act(stagehand, page, instruction)
    log.info(f"    {step.description}")
    await settle(page, SETTLE_MS)


async def walk_to_payment(stagehand: Stagehand, page: Page, result: SiteResult, interactive: bool) -> bool:
    """Step from the held tickets to the payment page, recording every fee and extra offered. True if reached."""
    out = result.out_dir
    last_signature, repeats = None, 0
    last_state: StepState | None = None
    last_seen: str | None = None
    login_paused = False
    for index in range(1, MAX_BOOKING_STEPS + 1):
        await wait_for_render(page, 10)
        seen = await fingerprint(page)
        if last_state is not None and seen is not None and seen == last_seen:
            repeats += 1  # the last action changed nothing on screen: same step, no new extraction needed
            log.info(f"  Step {index}: unchanged after the last action (repeat {repeats})")
            if repeats >= MAX_STUCK_REPEATS:
                result.message = f"Stuck on '{last_state.heading or last_state.page_type}': " + (
                    "; ".join(last_state.validation_errors) or last_state.summary
                )
                return False
            await next_action(stagehand, page, last_state, last_state.page_type)
            continue

        state = (
            await stagehand.extract(
                "We hold tickets and want to reach the payment page without buying any optional extras. Identify the "
                "current step, list every fee, delivery option, add-on, upsell or surcharge offered on it with prices "
                "(including in any open popup), and report the order summary lines, total, validation errors, whether "
                "a sign-in or queue is in the way, and anything else blocking progress.",
                StepState,
            )
        ).data
        page_type = "payment" if state.is_payment_page else state.page_type
        log.info(f"  Step {index}: {page_type} ({state.heading}) | total {state.total} | {len(state.offers)} offer(s)")
        await shot(page, out / f"step-{index:02d}-{slug(page_type)}.png")
        record_offers(result, index, page_type, state.heading, state.offers)
        result.steps.append(
            {
                "step": index,
                "step_type": page_type,
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

        if state.blocked_reason:
            raise Blocked(state.blocked_reason)
        if state.is_payment_page or page_type == "payment":
            await current_guard().freeze(page)  # hard stop: the payment page is only read from here on
            return True
        if page_type == "confirmation":
            raise FlowChanged("Reached a confirmation page unexpectedly; stopping")
        if state.in_queue or page_type == "waiting_room":
            await wait_in_queue(stagehand, page)
            last_state = None
            continue
        if state.login_required or page_type == "login_prompt":
            # Ticketek sells only to signed-in accounts. Never sign in or sign up on the user's behalf.
            if login_paused or not await pause_for_login(
                page,
                "Ticketek requires a signed-in account to buy. Sign in (an account with no saved card), "
                "or run `python main.py ticketek --login` once beforehand.",
                interactive,
            ):
                raise Blocked("Ticketek requires signing in to an account (run with --login first)")
            login_paused = True
            last_state = None
            continue

        signature = (page_type, state.heading, await page.url())
        repeats = repeats + 1 if signature == last_signature else 0
        last_signature = signature
        if repeats >= MAX_STUCK_REPEATS:
            result.message = (
                f"Stuck on '{state.heading or page_type}': {'; '.join(state.validation_errors) or state.summary}"
            )
            return False
        last_state, last_seen = state, seen
        await next_action(stagehand, page, state, page_type)

    result.message = f"Payment page not reached within {MAX_BOOKING_STEPS} steps"
    return False


async def read_event(stagehand: Stagehand, page: Page, result: TicketekResult) -> EventPage:
    event = (
        await stagehand.extract(
            "Extract the event name, the headline 'from' price advertised for the event, what the advertised prices "
            "are said to cover (quote any note about booking, service or transaction fees), and every session/"
            "performance listed with its date, venue, availability and lowest price. Do not interact with the page.",
            EventPage,
        )
    ).data
    result.event_name = event.event_name
    result.advertised_from_price_display = event.advertised_from_price
    result.advertised_price_note = event.price_note
    return event
