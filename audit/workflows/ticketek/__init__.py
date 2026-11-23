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
from ...core import BROWSER_ERRORS, ITEM_FIELDS, SiteResult, Status, slug, write_csv
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
    find_event,
    hold_tickets,
    open_session,
    read_event,
    walk_to_payment,
)
from .models import CheckoutDetails, PriceOption, Session, TicketekResult

log = logging.getLogger(__name__)

SAFETY_PROMPT = (
    "You are auditing a ticket-buying flow for price transparency. Never enter payment card, gift card, voucher or "
    "promo code details, and never click a button that pays for, purchases or completes an order (e.g. 'Pay now', "
    "'Complete purchase', 'Place order', 'Buy now'). Never sign in, sign up, create an account or join a membership."
)
ANY_SESSION = Session(date="the next available date", venue=None, status="on_sale", from_price=None)


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
            result.session, result.venue = session.date, session.venue
            log.info(f"  Session: {session.date} {session.venue or ''} ({session.status})")

            options = await open_session(stagehand, page, session)
            await shot(page, out / "tickets.png")
            write_csv(out / "ticket_options.csv", list(PriceOption.model_fields),
                      [o.model_dump() for o in options.options])  # fmt: skip
            result.cheapest_offered = cheapest_offered(options.options)
            option = choose_option(options.options)
            if option is None:
                result.fail(Status.NO_ITEMS_FOUND, "No tickets available for the session")
                return

            log.info(f"  Holding {args.tickets} x {option.area or ''} {option.price_type or ''} ({option.price})")
            item = await hold_tickets(stagehand, page, option, args.tickets)
            item["price_basis"] = options.price_basis
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

    def target_key(self, url: str, result: SiteResult) -> str:
        """The event and session audited, so different events started from the homepage keep separate rows."""
        event = getattr(result, "event_name", None)
        session = getattr(result, "session", None)
        return " | ".join(filter(None, [event, session])) or url

    def summarise(self, result: SiteResult) -> None:
        assert isinstance(result, TicketekResult)
        pricing.summarise(result)
