"""Ticketek: find the event, pick a session and tickets, and walk the purchase to the payment page."""

import logging
import re
import time
from urllib.parse import urlparse

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
from ...page import dismiss_popups, fingerprint, js, pause_for_login, settle, shot, try_goto, wait_for_render
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
# A "Best available / Any price" row spans every tier; it is a way into the seat map, not a ticket to hold.
CATCH_ALL = re.compile(r"any\s+price|best\s+available", re.IGNORECASE)
# The host the purchase must stay on; the resale "Verified Fan-to-Fan" link leads to Ticketek Marketplace.
TICKETEK_HOST = "premier.ticketek.com.au"
# A held ticket priced this far from the price chosen is not the ticket we picked (a resale listing, another tier).
PRICE_TOLERANCE = 0.10
# Pages that are still choosing tickets: walk_to_payment must not act on them (nothing would be held).
SELECTION_PAGES = ("ticket_selection", "seat_map", "event")
# Availability notes the extraction sometimes puts where the ticket type belongs.
AVAILABILITY_NOTE = re.compile(
    r"limited|single seats|selling fast|few left|available|exhausted|sold out", re.IGNORECASE
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
    """The first bookable session, or the first bookable one whose date/venue contains `hint`.

    Sessions marked bookable come first. If there are none, sessions with no status read are tried
    instead, since a buy button with no label can leave the status unread. A session with a known
    non-bookable status (sold out, cancelled, ...) is never picked.
    """
    bookable = [s for s in sessions if s.status in BOOKABLE] or [s for s in sessions if s.status is None]
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


def is_catch_all(option: PriceOption) -> bool:
    """A 'Best available' / 'Any price' row, however the extraction split it, or one priced as a range."""
    if CATCH_ALL.search(f"{option.area or ''} {option.price_type or ''}"):
        return True
    return len(re.findall(r"\d[\d,]*(?:\.\d+)?", option.price or "")) > 1


def ticket_type_of(option: PriceOption) -> str | None:
    """The option's ticket type, or None when the field only holds an availability note."""
    if option.price_type and AVAILABILITY_NOTE.search(option.price_type):
        return None
    return option.price_type


def choose_option(options: list[PriceOption]) -> PriceOption | None:
    """The cheapest available ticket an ordinary adult can buy; any cheapest ticket if none is plainly adult."""
    priced = [o for o in options if o.available and parse_price(o.price) is not None]
    standard = [o for o in priced if not RESTRICTED_TYPES.search(f"{o.price_type or ''} {o.area or ''}")]
    candidates = standard or priced
    specific = [o for o in candidates if not is_catch_all(o)]
    return min(specific or candidates, key=lambda o: parse_price(o.price) or 0.0, default=None)


def cheapest_offered(options: list[PriceOption]) -> float | None:
    prices = [p for o in options if o.available and (p := parse_price(o.price)) is not None]
    return min(prices, default=None)


# The ticket quantity controls, set without the model. `row` is a ticket type's row: the smallest element around a
# quantity (a number leaf next to the -/+ buttons, a number input or a <select>) that also shows a price.
QUANTITY_JS = r"""
const INCREASE = /increase|increment|\bplus\b|\badd\b|^\s*\+\s*$/i;
const ctrlLabel = b => [b.getAttribute('aria-label'), b.getAttribute('title'), txt(b),
    ...Array.from(b.querySelectorAll('svg title, [aria-label]'), n => n.textContent || n.getAttribute('aria-label'))]
    .filter(Boolean).join(' ');
const enabled = b => !b.disabled && b.getAttribute('aria-disabled') !== 'true';
// The quantity control inside `el`: a <select>, a number input, or a number leaf next to a button
const qtyControl = el => el.querySelector('select')
    || Array.from(el.querySelectorAll('input')).find(i => /^\d{1,2}$/.test(i.value || '') && i.type !== 'hidden')
    || Array.from(el.querySelectorAll('span, div, p, output, strong')).find(
        n => n.children.length === 0 && /^\d{1,2}$/.test(txt(n)) && !!n.parentElement
            && !!n.parentElement.querySelector('button, [role="button"]'));
const qtyOf = c => parseInt(c.tagName === 'SELECT' || c.tagName === 'INPUT' ? c.value : txt(c), 10);
const quantityRows = () => {
    const rows = [];
    for (const c of document.querySelectorAll('select, input, span, div, p, output, strong')) {
        if (!vis(c, 4, 4) || qtyControl(c.parentElement || c) !== c) continue;
        let row = c.parentElement;
        for (let i = 0; row && i < 6 && !/\$\s*\d/.test(row.innerText || ''); i++) row = row.parentElement;
        if (!row || !/\$\s*\d/.test(row.innerText || '') || (row.innerText || '').length > 400) continue;
        if (c.tagName !== 'SELECT' && !row.querySelector('button, [role="button"]')) continue;
        if (!rows.some(r => r.row === row)) rows.push({ row, control: c });
    }
    return rows;
};
"""
# {label, quantity, restricted}: set the chosen ticket type's quantity to `quantity` with ONE + click (or a <select>
# choice) per call, so the page can re-render between clicks; `quantity: null` only reads. Prefers the row that
# names `label` or reads 'Admit' / 'Adult' / 'Standard' / 'General'; never a restricted (companion, child, ...)
# row. Returns {row, before, after}, or null when no quantity control is on the page.
JS_SET_QUANTITY = r"""({ label, quantity, restricted }) => {
    const banned = new RegExp(restricted, 'i');
    const rows = quantityRows().filter(r => !banned.test(clean(txt(r.row)).split('$')[0]));
    if (!rows.length) return null;
    const head = r => clean(txt(r.row)).toLowerCase();
    const wanted = (label || '').toLowerCase();
    const pick = rows.find(r => wanted && head(r).startsWith(wanted))
        || rows.find(r => /^(admit|adult|standard|general)\b/.test(head(r)))
        || rows.find(r => wanted && head(r).includes(wanted))
        || rows[0];
    const name = clean(txt(pick.row)).slice(0, 60);
    const before = qtyOf(pick.control);
    if (quantity === null || before === quantity) return { row: name, before, after: before };
    if (pick.control.tagName === 'SELECT') {
        const opt = Array.from(pick.control.options).find(o => parseInt(o.value || o.text, 10) === quantity);
        if (opt && guardAllows(pick.control)) safeSelect(pick.control, opt.value);
    } else if (before < quantity) {
        const buttons = Array.from(pick.row.querySelectorAll('button, [role="button"]')).filter(b => vis(b, 6, 6));
        const plus = buttons.find(b => INCREASE.test(ctrlLabel(b))) || buttons[buttons.length - 1];
        if (plus && enabled(plus) && guardAllows(plus)) safeClick(plus);
    }
    const now = qtyControl(pick.row);
    return { row: name, before, after: now ? qtyOf(now) : before };
}"""
# Click the button that reserves the chosen tickets. Only real buttons (never links, which is how the resale
# 'Find tickets' leaves the site), never inside the Fan-to-Fan / resale / Marketplace box, never external links.
JS_CONTINUE_SELECTION = r"""() => {
    const LABEL = /^(next|continue|get tickets|add to (cart|basket)|find tickets|proceed)$/i;
    const RESALE = /fan-to-fan|resale|marketplace/i;
    const inResale = el => {
        for (let n = el; n && n !== document.body; n = n.parentElement) {
            if ((n.innerText || '').length > 600) break;
            if (RESALE.test(n.innerText || '')) return true;
        }
        return false;
    };
    const external = el => !!el.closest('a') || !!el.querySelector('[class*="external" i], [aria-label*="external" i]')
        || /↗/.test(txt(el));
    for (const b of document.querySelectorAll('button, [role="button"], input[type="submit"]')) {
        const label = clean(txt(b) || b.value || '');
        if (!LABEL.test(label) || !vis(b, 40, 20) || b.disabled || b.getAttribute('aria-disabled') === 'true') continue;
        if (b.tagName === 'A' || external(b) || inResale(b) || !guardAllows(b)) continue;
        if (safeClick(b)) return label;
    }
    return null;
}"""


def on_ticketek(url: str) -> bool:
    return urlparse(url).hostname == TICKETEK_HOST


def hold_confirmed(held: HeldTickets, quantity: int, url: str, stepper_value: int | None) -> bool:
    """Whether the page really holds `quantity` tickets, not just a timer, a resale listing or a stepper at 0."""
    if not held.tickets_held or held.quantity != quantity:
        return False
    if not on_ticketek(url):
        return False
    return stepper_value != 0


def price_matches(held_price: str | None, chosen_price: str | None) -> bool:
    """False when the held ticket's price is more than PRICE_TOLERANCE away from the price chosen."""
    held_value, chosen_value = parse_price(held_price), parse_price(chosen_price)
    if held_value is None or not chosen_value:
        return True
    return abs(held_value - chosen_value) / chosen_value <= PRICE_TOLERANCE


async def set_quantity(page: Page, label: str | None, quantity: int | None) -> dict | None:
    """Set (or with `quantity=None`, read) the chosen ticket type's quantity. None if no quantity control is shown."""
    arg = {"label": label, "quantity": quantity, "restricted": RESTRICTED_TYPES.pattern}
    state = await js(page, JS_SET_QUANTITY, arg, lib=QUANTITY_JS)
    if quantity is None or not state:
        return state
    first = state["before"]
    for _ in range(quantity + 2):  # one click per call; stop once it holds or stops moving
        if state["after"] == quantity:
            break
        await settle(page, 400)
        moved = await js(page, JS_SET_QUANTITY, arg, lib=QUANTITY_JS)
        if not moved or moved["after"] == state["after"]:
            state = moved or state
            break
        state = moved
    return {**state, "before": first}


HOLD_RULES = (
    "Never click anything in the 'Verified Fan-to-Fan' / resale / Ticketek Marketplace box, never Afterpay, never "
    "'Change', 'Back to event details' or the Companion Card question. Never add parking, merchandise, packages or "
    "upgrades."
)


def hold_instruction(described: str, option: PriceOption, ticket_type: str, quantity: int) -> str:
    """The fallback instruction for the model when no quantity control could be set without it."""
    area = option.area or "Best available"
    return (
        f"Take the single next action to get {described} tickets (priced around {option.price}). "
        f"In order: 1) If '{area}' is not yet selected/expanded (look at the price/area pills above the seat map or "
        f"in the category list), click the '{area}' ({option.price}) button/option (scroll the price bar or click its "
        "arrow if needed), or click 'Find Best available' to show the category list; "
        f"2) Once quantity +/- buttons or a quantity dropdown are visible for {ticket_type}, "
        f"increase/set the quantity to {quantity} (leave every other ticket type at 0); "
        "3) Once the quantity is set, click the 'Next' / 'Get tickets' / 'Continue' button below the ticket "
        "quantities to reserve the tickets. Do NOT click 'Choose your own' or 'Find Best available' if ticket "
        f"categories or quantity controls are already open. {HOLD_RULES}"
    )


CONTINUE_INSTRUCTION = (
    "The ticket quantity is set. Click the 'Next' / 'Continue' / 'Get tickets' button below the ticket quantities to "
    f"reserve the tickets; do not change anything else. {HOLD_RULES}"
)


async def hold_tickets(stagehand: Stagehand, page: Page, option: PriceOption, quantity: int) -> Item:
    """Choose `quantity` tickets of `option` (best available seats) and get them held in the cart."""
    item = new_item(option_name(option), option.price, quantity=quantity)
    steps: list[str] = item.setdefault("hold_steps", [])
    price_type = ticket_type_of(option)
    kind = " in ".join(f"'{part}'" for part in [price_type, option.area] if part) or "standard"
    described = f"{quantity} x {kind}"
    ticket_type = f"'{price_type}' (or Adult / Standard)" if price_type else "'Adult / Standard'"
    question = (
        f"We want {described} tickets. Are tickets now held/reserved: does a cart, basket or order summary list them "
        "with a quantity above 0? A countdown timer alone does not count, and resale / Fan-to-Fan / Marketplace "
        "listings never count. Report the area, seats, ticket type, quantity, price per ticket and total."
    )
    session_url = await page.url()
    held, confirmed = None, False

    async def act(instruction: str) -> None:
        step = await safe_act(stagehand, page, instruction)
        log.info(f"    Tickets: {step.description}")
        steps.append(step.description)

    for _ in range(MAX_SELECT_STEPS + 2):
        await dismiss_ticketek_alert(page)
        stepper = await set_quantity(page, price_type, quantity)
        if stepper and stepper["after"] == quantity:
            log.info(f"    Tickets: quantity {stepper['before']} -> {stepper['after']} on '{stepper['row']}'")
            steps.append(f"set quantity {stepper['before']} -> {stepper['after']} on '{stepper['row']}'")
            await settle(page, 1000)
            clicked = await js(page, JS_CONTINUE_SELECTION)
            if clicked:
                log.info(f"    Tickets: clicked '{clicked}'")
                steps.append(f"clicked '{clicked}'")
            else:
                await act(CONTINUE_INSTRUCTION)
        else:
            if stepper:
                log.info(f"    Tickets: quantity stuck at {stepper['after']} on '{stepper['row']}'")
            await act(hold_instruction(described, option, ticket_type, quantity))
        await settle(page, SETTLE_MS)

        url = await page.url()
        if not on_ticketek(url):
            log.warning(f"    Tickets: left Ticketek for {url}; going back to the session")
            steps.append(f"left Ticketek for {url}; went back")
            await try_goto(page, session_url)
            await settle(page, SETTLE_MS)
            await wait_for_render(page)
            continue
        held = (await stagehand.extract(question, HeldTickets)).data
        shown = await set_quantity(page, price_type, None)
        confirmed = hold_confirmed(held, quantity, url, shown["after"] if shown else None)
        if confirmed and not price_matches(held.unit_price, option.price):
            log.warning(f"    Tickets: held at {held.unit_price}, not the {option.price} chosen; not our tickets")
            confirmed = False
        if confirmed:
            break
        if held.tickets_held:
            log.info(f"    Tickets: the page does not show the hold ({held.reason or 'no reason given'})")
    if held is not None:
        item["selected_variant"] = " - ".join(filter(None, [held.area, held.price_type, held.seats]))
        select_price(item, held.unit_price)
        item["cart_total_display"] = held.total
        item["held_quantity"] = held.quantity
    item["status"] = "added" if confirmed else "add_failed"
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
    "ticket_selection": "Click the 'Next' / 'Continue' button below the ticket quantities; do not change anything "
    "else, never click 'Change', Afterpay, resale or 'Back to event details'.",
    "seat_map": "Do not pick seats by hand. Accept the seats offered (e.g. 'Accept seats', 'Continue') or choose "
    "'Best available'.",
}
DEFAULT_NEXT_ACTION = (
    "Do not add any optional extra (no refund protection, insurance, parking, merchandise, upgrades or donations). "
    "If the page requires an explicit choice, choose the decline option. Otherwise click the button that continues "
    "towards payment (e.g. 'Continue', 'Next', 'Proceed to payment'). Never click a button that pays or completes "
    "the purchase, and never sign up for anything. Never click Afterpay, or anything in a resale / 'Verified "
    "Fan-to-Fan' / Ticketek Marketplace box."
)


def still_selecting(page_type: str, step_index: int, last_type: str | None) -> bool:
    """True when the walk is still on a ticket choosing page on its first step, or twice in a row: nothing is held."""
    return page_type in SELECTION_PAGES and (step_index == 1 or last_type in SELECTION_PAGES)


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
    last_type: str | None = None
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

        if still_selecting(page_type, index, last_type):
            result.message = "Still on the ticket selection page: tickets were not held"
            return False
        last_type = page_type

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
