"""Ticketek: find the event, pick a session and tickets, and walk the purchase to the payment page."""

import logging
import re
import time

from stagehand import Page, Stagehand

from ...core import (
    BROWSER_ERRORS,
    Blocked,
    FlowChanged,
    Item,
    SiteResult,
    new_item,
    parse_price,
    record_offers,
    select_price,
    slug,
)
from ...page import dismiss_popups, fingerprint, js, pause_for_login, settle, shot, wait_for_render
from ...safety import current_guard, safe_act, try_act
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
LOGIN_URL = "https://premier.ticketek.com.au/membership/login.aspx"
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
JS_SEARCH_EVENT = r"""query => {
    for (const inp of document.querySelectorAll('input[type="search"], input[placeholder*="search" i], input[aria-label*="search" i], header input[type="text"]')) {
        if (!vis(inp, 80, 20)) continue;
        if (!safeFill(inp, query)) continue;
        return 'typed';
    }
    for (const btn of document.querySelectorAll('header button, header a, [aria-label*="search" i]')) {
        const label = ((btn.getAttribute('aria-label') || '') + ' ' + txt(btn)).trim();
        if (vis(btn, 18, 18) && /search/i.test(label) && safeClick(btn)) return 'opened';
    }
    return null;
}"""


JS_DISMISS_ALERT = r"""() => {
    for (const d of dialogs()) {
        if (!vis(d, 150, 80)) continue;
        const okBtn = Array.from(d.querySelectorAll('button')).find(
            b => vis(b, 30, 20) && /^(ok|okay|got it|close|dismiss|i understand)$/i.test(clean(txt(b))) && guardAllows(b));
        if (okBtn && safeClick(okBtn)) return clean(txt(okBtn));
    }
    for (const b of document.querySelectorAll('button')) {
        if (vis(b, 40, 24) && /^ok$/i.test(clean(txt(b))) && guardAllows(b) && safeClick(b)) return 'OK';
    }
    return null;
}"""


async def dismiss_ticketek_alert(page: Page) -> None:
    clicked = await js(page, JS_DISMISS_ALERT)
    if clicked:
        log.info(f"  Dismissed informational alert dialog ('{clicked}')")
        await settle(page, 1000)


async def find_event(
    stagehand: Stagehand, page: Page, query: str | None, session_hint: str | None = None
) -> None:
    """From the homepage or a search page, reach the event page of an on-sale event matching `query`."""
    venue_clause = f" at '{session_hint}'" if session_hint else ""
    wanted = f"an on-sale event matching '{query}'{venue_clause}" if query else "the first on-sale event featured on the page"
    question = (
        f"We are looking for {wanted}. What kind of page is this, and which matching on-sale events are listed? "
        "Report anything blocking progress."
    )
    for _ in range(MAX_FIND_STEPS):
        await dismiss_ticketek_alert(page)
        state = (await stagehand.extract(question, FindState)).data
        if state.blocked_reason:
            raise Blocked(state.blocked_reason)
        if state.page_type in ("event", "ticket_selection", "seat_map"):
            log.info(f"  Event page: {state.event_name or '?'}")
            return
        if state.page_type == "search_results" and state.matching_events:
            instruction = (
                f"Open the event page of '{state.matching_events[0]}'{venue_clause} by clicking its 'Find tickets' / "
                f"'Buy tickets' / 'More info' button or title{(' for ' + session_hint) if session_hint else ''}."
            )
        elif query:
            dom_search = await js(page, JS_SEARCH_EVENT, query)
            if dom_search == "typed":
                await page.key_press("Enter")
                log.info(f"  Searched for '{query}' via search box -> {state.reason}")
                await settle(page, SETTLE_MS)
                await wait_for_render(page)
                continue
            if dom_search == "opened":
                await settle(page, 1000)
                if await js(page, JS_SEARCH_EVENT, query) == "typed":
                    await page.key_press("Enter")
                    log.info(f"  Opened search and searched for '{query}' -> {state.reason}")
                    await settle(page, SETTLE_MS)
                    await wait_for_render(page)
                    continue
            instruction = (
                f"Search the site for '{query}': type it into the search box (open the search first if only a "
                "search icon is visible) and press Enter."
            )
        else:
            instruction = "Open the event page of the first on-sale event featured on the page."
        before_url = await page.url()
        try:
            step = await safe_act(stagehand, page, instruction)
            log.info(f"  {step.description} -> {state.reason}")
        except BROWSER_ERRORS as error:
            await settle(page, SETTLE_MS)
            if await page.url() == before_url:
                raise
            log.info(f"  Navigated to {await page.url()} ({error}) -> {state.reason}")
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


JS_SWITCH_BEST_AVAILABLE = r"""() => {
    for (const el of document.querySelectorAll('button, a, [role="tab"], [role="button"]')) {
        if (!vis(el, 50, 20)) continue;
        const label = clean(txt(el));
        if (/^(find\s+)?best\s+available$/i.test(label) && guardAllows(el)) {
            if (el.getAttribute('aria-selected') === 'true' || /\bactive\b|\bselected\b/i.test(el.className || '')) return null;
            if (safeClick(el)) return label;
        }
    }
    return null;
}"""


async def open_session(stagehand: Stagehand, page: Page, session: Session) -> TicketOptions:
    """Click through to the ticket selection page of `session` and read its prices."""
    described = f"the session on {session.date}" + (f" at {session.venue}" if session.venue else "")
    question = (
        "List the distinct ticket areas/tiers and price types with their price per ticket and availability "
        "(summarise per tier, not per seat on the map), what the prices cover, and anything blocking progress. "
        "Do not interact with the page."
    )
    for _ in range(MAX_SELECT_STEPS):
        await dismiss_ticketek_alert(page)
        switched = await js(page, JS_SWITCH_BEST_AVAILABLE)
        if switched:
            log.info(f"    Switched to '{switched}' view")
            await settle(page, SETTLE_MS)
            await wait_for_render(page)
        options = (await stagehand.extract(question, TicketOptions)).data
        if options.blocked_reason:
            raise Blocked(options.blocked_reason)
        if options.is_ticket_selection and options.options:
            return options
        before_url = await page.url()
        try:
            step = await safe_act(
                stagehand,
                page,
                f"Take the single next action to see ticket prices and quantity controls for {described}: if an "
                "informational alert popup with an 'OK' button is covering the page, click 'OK'; if a "
                "'Find Best available' or 'Best available' tab/button is visible on a seat map, click it; otherwise "
                "click the session's 'Find tickets', 'Buy tickets' or 'Get tickets' button (expand the session list "
                "or choose the date first if needed). Do not choose resale tickets.",
            )
            log.info(f"    Session: {step.description}")
            if not step.success and options.options:
                return options
        except BROWSER_ERRORS as error:
            await settle(page, SETTLE_MS)
            if await page.url() == before_url:
                raise
            log.info(f"    Session navigated to {await page.url()} ({error})")
        await settle(page, SETTLE_MS)
        await wait_for_render(page)
    raise FlowChanged(f"Couldn't reach the ticket selection page for {described}")


def option_name(option: PriceOption) -> str:
    return " - ".join(filter(None, [option.area, option.price_type]))


def choose_option(options: list[PriceOption]) -> PriceOption | None:
    """The cheapest available ticket an ordinary adult can buy; any cheapest ticket if none is plainly adult."""
    priced = [o for o in options if o.available and parse_price(o.price) is not None]
    standard = [o for o in priced if not RESTRICTED_TYPES.search(o.price_type or "")]
    candidates = standard or priced
    specific = [o for o in candidates if not re.search(r"any\s+price", o.area or "", re.IGNORECASE)]
    return min(specific or candidates, key=lambda o: parse_price(o.price) or 0.0, default=None)


def cheapest_offered(options: list[PriceOption]) -> float | None:
    prices = [p for o in options if o.available and (p := parse_price(o.price)) is not None]
    return min(prices, default=None)


async def hold_tickets(stagehand: Stagehand, page: Page, option: PriceOption, quantity: int) -> Item:
    """Choose `quantity` tickets of `option` (best available seats) and get them held in the cart."""
    item = new_item(option_name(option), option.price, quantity=quantity)
    kind = " in ".join(f"'{part}'" for part in [option.price_type, option.area] if part) or "standard"
    described = f"{quantity} x {kind}"
    ticket_type = f"'{option.price_type}' (or Adult / Standard)" if option.price_type else "'Adult / Standard'"
    question = (
        f"We want {described} tickets. Are tickets now held/reserved (a cart, basket, order summary or countdown "
        "timer)? Report the area, seats, ticket type, quantity, price per ticket and total."
    )
    held = None
    for _ in range(MAX_SELECT_STEPS + 2):
        await dismiss_ticketek_alert(page)
        step = await safe_act(
            stagehand,
            page,
            f"Take the single next action to get {described} tickets (priced around {option.price}). "
            f"In order: 1) If '{option.area or 'Best available'}' is not yet selected/expanded (look at the price/area "
            f"pills above the seat map or in the category list), click the '{option.area or 'Best available'}' "
            f"({option.price}) button/option (scroll the price bar or click its arrow if needed), or click 'Find Best "
            "available' to show the category list; "
            f"2) Once quantity +/- buttons or a quantity dropdown are visible for {ticket_type}, "
            f"increase/set the quantity to {quantity} (leave every other ticket type at 0); "
            "3) Once the quantity is set, click the 'Next' / 'Get tickets' / 'Find tickets' / 'Continue' button "
            "to reserve the tickets. Do NOT click 'Choose your own' or 'Find Best available' if ticket categories or "
            "quantity controls are already open. Never add parking, merchandise, packages or upgrades.",
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
                    "; ".join(last_state.validation_errors) or last_state.summary or "no summary"
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


JS_SELECT_VENUE = r"""hint => {
    const words = (hint || '').toLowerCase().split(/\s+/).filter(Boolean);
    for (const sel of document.querySelectorAll('select')) {
        if (!vis(sel, 80, 20)) continue;
        const cur = sel.options[sel.selectedIndex];
        if (!cur || /select\s+(venue|city|state|location)/i.test(cur.text || '')) {
            const opts = Array.from(sel.options).filter(o => o.value && !/select\s+(venue|city|state|location)/i.test(o.text || ''));
            const match = words.length ? opts.find(o => words.some(w => (o.text || '').toLowerCase().includes(w))) : null;
            const opt = match || opts[0];
            if (opt && safeSelect(sel, opt.value)) return clean(opt.text);
        }
    }
    return null;
}"""


async def read_event(
    stagehand: Stagehand, page: Page, result: TicketekResult, session_hint: str | None = None
) -> EventPage:
    await dismiss_ticketek_alert(page)
    venue_picked = await js(page, JS_SELECT_VENUE, session_hint)
    if venue_picked:
        log.info(f"  Selected venue from dropdown: {venue_picked}")
        await settle(page, SETTLE_MS)
        await wait_for_render(page)
    event = (
        await stagehand.extract(
            "Extract the event name, the headline 'from' price advertised for the event, what the advertised prices "
            "are said to cover (quote any note about booking, service or transaction fees), and every session/"
            "performance listed with its date, venue, availability and lowest price. Do not interact with the page.",
            EventPage,
        )
    ).data
    if not event.sessions and not event.is_ticket_selection:
        wanted = f"matching '{session_hint}'" if session_hint else "the first venue"
        step = await try_act(
            stagehand,
            page,
            f"If a 'Select venue' or city/venue dropdown is shown with no venue selected, open it and select {wanted}.",
            follow_up=True,
        )
        if step and step.success:
            log.info(f"  Venue dropdown: {step.description}")
            await settle(page, SETTLE_MS)
            await wait_for_render(page)
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
