"""Ticketek Australia event tickets: drip pricing between the advertised ticket price and the payment page.

1. Start on Ticketek (homepage plus `--query`, or a direct event URL), open the event, and record the
   advertised 'from' price, what the prices are said to include, and every session.
2. Pick a session (the first on sale, or the one matching `--session`), read every ticket area and
   price type, and hold 1-8 tickets of the cheapest standard adult ticket on 'Best available'.
3. Walk the cart, delivery and extras steps to the payment page, choosing free delivery and declining
   every extra, and record every booking, service, handling, delivery and card fee offered on the way.

Ticketek only sells to signed-in accounts: sign in once with `--login` (use an account with no saved
card). The run always stops on the payment page: no payment details are entered and nothing is bought.
"""

import argparse
import contextlib
import logging
from urllib.parse import parse_qs, urlparse

from stagehand import Page, Stagehand

from ...config import Settings
from ...core import BROWSER_ERRORS, ITEM_FIELDS, Item, SiteResult, Status, slug, write_csv
from ...page import settle, shot, wait_for_render
from ...workflow import Workflow
from . import pricing
from .flow import (
    LOGIN_URL,
    SETTLE_MS,
    SITE_URL,
    cheapest_offered,
    choose_option,
    choose_session,
    dismiss_ticketek_popups,
    dropdown_sessions,
    fewer_tickets,
    find_event,
    hold_tickets,
    open_session,
    other_sessions,
    read_event,
    return_to_event,
    walk_to_payment,
)
from .models import CheckoutDetails, EventPage, PriceOption, Session, TicketekResult

log = logging.getLogger(__name__)

SAFETY_PROMPT = (
    "You are auditing a ticket-buying flow for price transparency. Never enter payment card, gift card, voucher or "
    "promo code details, and never click a button that pays for, purchases or completes an order (e.g. 'Pay now', "
    "'Complete purchase', 'Place order', 'Buy now'). Never sign in, sign up, create an account or join a membership."
)
ANY_SESSION = Session(date="the next available date", venue=None, status="on_sale", from_price=None)
# When the one chosen has no tickets left ("There aren't enough tickets ..."), fewer tickets and then other sessions
# are tried. Each try costs a few model calls, so a long run of sold out dates is cut short: at most this many
# sessions, and this many holds in all.
MAX_SESSION_TRIES = 4
MAX_HOLD_TRIES = 8


async def read_payment_page(stagehand: Stagehand, page: Page, result: TicketekResult) -> CheckoutDetails:
    await shot(page, result.out_dir / "checkout.png")
    checkout = (
        await stagehand.extract(
            "Extract the order's price summary from this payment page: each ticket line with its quantity and line "
            "total, the ticket subtotal, any per-ticket booking fees and per-transaction service/handling fees, the "
            "delivery method and its charge, optional extras included, the total due and currency. Also list every "
            "card or payment-method surcharge advertised, and report if a login, CAPTCHA or bot check is blocking "
            "the page. Do not interact with the page.",
            CheckoutDetails,
        )
    ).data
    pricing.record_surcharges(result, checkout.payment_surcharges)
    return checkout


class TicketekWorkflow(Workflow):
    name = "ticketek"
    help = "Ticketek: hold 1-8 tickets up to the payment page and record every booking, handling and delivery fee"
    system_prompt = SAFETY_PROMPT
    log_level = "info"
    viewport = (1440, 1000)
    result_class = TicketekResult
    item_fields = (*ITEM_FIELDS[:-1], "quantity", ITEM_FIELDS[-1])

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser, settings: Settings) -> None:
        parser.add_argument(
            "urls",
            nargs="*",
            default=[SITE_URL],
            help=f"Ticketek homepage or event page URL(s) (default: {SITE_URL})",
        )
        parser.add_argument(
            "--query",
            default=None,
            help="Event to search for when starting from the homepage (e.g. 'Hamilton'); default: first featured event",
        )
        parser.add_argument(
            "--session",
            default=None,
            help="Words that pick the session, matched against its date and venue (e.g. 'Sat 14 Mar', 'Melbourne')",
        )
        parser.add_argument(
            "--tickets",
            type=int,
            default=2,
            choices=range(1, 9),
            metavar="N",
            help="Number of tickets to hold (1-8, default 2)",
        )

    def targets(self) -> list[str]:
        return self.args.urls

    def login_url(self) -> str:
        """Open My Ticketek sign-in directly when priming the browser profile."""
        return LOGIN_URL if self.args.urls == [SITE_URL] else super().login_url()

    def output_folder(self, url: str) -> str:
        """One folder per event when started from an event page."""
        folder = super().output_folder(url)
        parts = urlparse(url)
        show = parse_qs(parts.query).get("sh", [""])[0]  # show.aspx?sh=HAMILTON23
        name = show or parts.path.strip("/").split("/")[-1].removesuffix(".aspx")
        return f"{folder}-{slug(name)}" if name else folder

    async def run(self, stagehand: Stagehand, page: Page, url: str, result: SiteResult) -> None:
        assert isinstance(result, TicketekResult)
        args, out = self.args, result.out_dir
        result.search = {"query": args.query, "session": args.session, "tickets": args.tickets}
        checkout: CheckoutDetails | None = None
        log.info(f"\n=== {url} | Query: {args.query or 'any'} | {args.tickets} ticket(s) ===")
        try:
            await page.goto(url)
            await settle(page, SETTLE_MS)
            await dismiss_ticketek_popups(stagehand, page)

            await find_event(stagehand, page, args.query, args.session)
            await wait_for_render(page)
            result.shop_url = await page.url()
            event = await read_event(stagehand, page, result, args.session)
            write_csv(out / "products.csv", ["date", "venue", "status", "from_price"],
                      [s.model_dump() for s in event.sessions])  # fmt: skip
            await shot(page, out / "event.png")
            log.info(
                f"  {event.event_name}: {len(event.sessions)} session(s) | from {event.advertised_from_price} "
                f"| {event.price_note or 'price basis not stated'}"
            )

            session = choose_session(event.sessions, args.session) if event.sessions else ANY_SESSION
            if session is None:
                wanted = f" matching '{args.session}'" if args.session else ""
                result.fail(Status.NO_ITEMS_FOUND, f"No session on sale{wanted}")
                return

            item = await self.hold_any_session(stagehand, page, result, event, session)
            if item is None:
                return
            result.items.append(item)
            if item["status"] != "added":
                result.fail(Status.ADD_TO_CART_FAILED, "The tickets could not be held")
                return
            await shot(page, out / "cart.png")

            if not await walk_to_payment(stagehand, page, result, interactive=not self.settings.headless):
                result.fail(Status.NO_CHECKOUT, result.message)
                return
            checkout = await read_payment_page(stagehand, page, result)
            if checkout.blocked_reason:
                result.fail(Status.BLOCKED, checkout.blocked_reason)
            elif not checkout.is_checkout_page:
                result.fail(Status.NO_CHECKOUT, "Payment page not reached")
            else:
                result.status = Status.OK
        finally:  # keep whatever fees were seen, however the run ended
            self.conclude(result, lambda: pricing.compare_prices(result, checkout))
            with contextlib.suppress(*BROWSER_ERRORS):  # the browser may be gone; the URL is a nicety
                result.checkout_url = await page.url()

    async def hold_any_session(
        self, stagehand: Stagehand, page: Page, result: TicketekResult, event: EventPage, session: Session
    ) -> Item | None:
        """Hold the tickets for `session`; when there aren't enough, for fewer tickets and then for other sessions.
        Returns the item to audit, or None (the result marked failed) when no session had a ticket left."""
        args = self.args
        tried: list[Session] = []  # the sessions given up on, as the event page and the date dropdown name them
        event_url, tickets, sessions_used = result.shop_url, args.tickets, 1
        while True:
            if result.sessions_tried:  # the last try had no tickets: go back for fewer, or another session
                await return_to_event(page, event_url, session)
            result.session, result.venue = session.date, session.venue
            log.info(f"  Session: {session.date} {session.venue or ''} ({session.status}), {tickets} ticket(s)")
            item, no_tickets = await self.try_session(stagehand, page, result, session, tickets)
            if no_tickets is None:
                break
            result.sessions_tried.append(
                {"date": session.date, "venue": session.venue, "tickets": tickets, "reason": no_tickets}
            )
            out_of_tries = len(result.sessions_tried) >= MAX_HOLD_TRIES
            # Ticketek's own advice: a smaller quantity may still be held for the same session
            if item is not None and tickets > 1 and not out_of_tries:
                tickets = fewer_tickets(tickets)
                log.warning(f"  {no_tickets}; trying {tickets} ticket(s) for the same session")
                continue
            # Even one ticket is not to be had: try another date or venue, for the full quantity again.
            # The date dropdown can list performances the event page's list left out; the date it shows is
            # the one just tried, however differently the event page wrote it
            tried.append(session)
            listed, shown = await dropdown_sessions(page)
            if shown:
                tried.append(shown)
            remaining = other_sessions(event.sessions + listed, tried, args.session)
            if not remaining or out_of_tries or sessions_used >= MAX_SESSION_TRIES:
                if item is not None:
                    result.items.append(item)
                stopped = "no other session to try" if not remaining else f"{len(result.sessions_tried)} tries"
                result.fail(Status.NO_ITEMS_FOUND, f"{no_tickets} ({stopped})")
                return None
            session, tickets, sessions_used = remaining[0], args.tickets, sessions_used + 1
            log.warning(f"  {no_tickets}; trying {session.date} {session.venue or ''} instead")
        if tickets < args.tickets:
            log.warning(f"  Holding {tickets} ticket(s) instead of the {args.tickets} asked for")
        return item

    async def try_session(
        self, stagehand: Stagehand, page: Page, result: TicketekResult, session: Session, tickets: int
    ) -> tuple[Item | None, str | None]:
        """Open `session` and hold `tickets` tickets. Returns the item, and why the session has no tickets for us
        (None when it had some, held or not), so the caller can try another date or venue."""
        out = result.out_dir
        options = await open_session(stagehand, page, session)
        await shot(page, out / "tickets.png")
        write_csv(out / "ticket_options.csv", list(PriceOption.model_fields),
                  [o.model_dump() for o in options.options])  # fmt: skip
        result.cheapest_offered = cheapest_offered(options.options)
        option = choose_option(options.options)
        if option is None:
            return None, "No tickets available for the session"

        log.info(f"  Holding {tickets} x {option.area or ''} {option.price_type or ''} ({option.price})")
        item = await hold_tickets(stagehand, page, option, tickets)
        item["price_basis"] = options.price_basis
        if item["status"] == "unavailable":
            await shot(page, out / f"unavailable-{len(result.sessions_tried) + 1}.png")
            return item, f"Not enough tickets for {tickets} on {session.date}: {item['unavailable_message']}"
        return item, None

    def target_key(self, url: str, result: SiteResult) -> str:
        """The event and session audited, so different events started from the homepage keep separate rows."""
        event = getattr(result, "event_name", None)
        session = getattr(result, "session", None)
        return " | ".join(filter(None, [event, session])) or url

    def summarise(self, result: SiteResult) -> None:
        assert isinstance(result, TicketekResult)
        pricing.summarise(result)
