"""Uber Eats food delivery: drip pricing at checkout, and menu markup against the restaurant's own site.

1. Start on Uber Eats AU (or a direct `/store/...` URL), set the delivery address, and record the
   advertised delivery fee and service fee shown on the feed card and store banner.
2. Clear leftover cart items (confirming Uber's 'new order' prompt if another store's cart remains),
   then add 1-5 menu items (auto-selecting $0/cheapest required modifiers and declining paid add-ons).
3. Walk through the cart drawer to the final checkout screen and extract every fee.
4. Visit the restaurant's own website (found by search, or `--restaurant-url`), walk it until item
   prices are on screen, and compare its unit prices against the Uber Eats prices.

The run always stops on the checkout screen: nothing is ordered or paid for. When the checkout screen was not
reached, the cart is emptied again on the way out, so the next run starts with an empty cart.
"""

import argparse
import contextlib
import logging
from urllib.parse import urlparse

from stagehand import Page, Stagehand

from ...config import Settings
from ...core import BROWSER_ERRORS, ITEM_FIELDS, SiteResult, Status, UnsafeActionRefused, parse_price, slug, write_csv
from ...page import settle, shot
from ...safety import Mode, current_guard
from ...workflow import Workflow
from .direct_site import audit_direct_restaurant_site
from .models import CartAndCheckoutState, MenuItem, StoreMenu, UberResult
from .pricing import FEE_BREAKDOWN_FIELDS, compare_prices, print_audit_tables
from .store import (
    SITE_URL,
    CartLeft,
    add_menu_item_to_cart,
    choose_menu_items,
    clear_existing_cart,
    dismiss_location_prompt,
    dismiss_uber_popups,
    ensure_address_and_open_store,
    order_item,
    walk_to_checkout,
)

log = logging.getLogger(__name__)

SETTLE_MS = 2000

SAFETY_PROMPT = (
    "You are auditing a food-delivery ordering flow for price transparency. Never click a button that places, "
    "pays for, or submits an order (e.g. 'Place order', 'Pay', 'Submit order', 'Start Uber One trial'). "
    "Never enter payment card details, gift cards, or promo codes."
)


class UberEatsWorkflow(Workflow):
    name = "ubereats"
    help = "Uber Eats: order 1-5 menu items up to checkout, record every fee, and compare prices with the restaurant's own site"
    system_prompt = SAFETY_PROMPT
    viewport = (1440, 1000)
    result_class = UberResult
    item_fields = (
        *ITEM_FIELDS[:-1],
        "direct_item_name",
        "direct_price_display",
        "uber_vs_direct_diff",
        "uber_vs_direct_markup_pct",
        ITEM_FIELDS[-1],
    )

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser, settings: Settings) -> None:
        parser.add_argument(
            "urls",
            nargs="*",
            default=[SITE_URL],
            help=f"Uber Eats homepage or direct `/store/...` URL(s) (default: {SITE_URL})",
        )
        parser.add_argument(
            "--address",
            default=settings.ubereats_address,
            help=f"Delivery address to enter (default: '{settings.ubereats_address}')",
        )
        parser.add_argument(
            "--query",
            default=None,
            help="Optional search term when starting from the homepage (e.g. 'burgers', 'thai')",
        )
        parser.add_argument(
            "--items",
            "--products",
            dest="items",
            type=int,
            default=2,
            choices=range(1, 6),
            metavar="N",
            help="Number of menu items to add to the order (1-5, default 2)",
        )
        parser.add_argument(
            "--restaurant-url",
            default=None,
            help="Optional direct URL to the restaurant's own website/ordering page for unit-price comparison",
        )
        parser.add_argument(
            "--skip-direct-site",
            action="store_true",
            help="Skip visiting the restaurant's own website at the end of the Uber Eats run",
        )

    def targets(self) -> list[str]:
        return self.args.urls

    def output_folder(self, url: str) -> str:
        """One folder per store when started from a `/store/...` URL."""
        folder = super().output_folder(url)
        path = urlparse(url).path
        return f"{folder}-{slug(path.split('/store/')[-1].split('/')[0])}" if "/store/" in path else folder

    async def run(self, stagehand: Stagehand, page: Page, url: str, result: SiteResult) -> None:
        assert isinstance(result, UberResult)
        args, out = self.args, result.out_dir
        result.search = {"address": args.address, "query": args.query}
        leftover = CartLeft(0, None)
        cart_state: CartAndCheckoutState | None = None
        checkout_state: CartAndCheckoutState | None = None
        refused = False

        log.info(f"\n=== {url} | Address: {args.address} | Query: {args.query or 'any'} ===")
        try:
            await page.goto(url)
            await settle(page, SETTLE_MS)
            await dismiss_uber_popups(stagehand, page)
            await dismiss_location_prompt(page, out)

            result.shop_url = await ensure_address_and_open_store(
                stagehand, page, args.address, args.query, result, interactive=not self.settings.headless
            )
            log.info(f"  Store page: {result.shop_url}")
            leftover = await clear_existing_cart(stagehand, page, out)
            await shot(page, out / "store.png")

            menu = (
                await stagehand.extract(
                    "Extract the restaurant name, street/suburb location (if shown), advertised delivery fee and service fee "
                    "shown in the store header/banner, and extract up to 25 menu items listed on this page with their category, "
                    "price, availability, and whether each is food/drink.",
                    StoreMenu,
                )
            ).data
            header = menu.store_header
            result.store_name = header.store_name or result.store_name
            result.store_location = header.store_address_or_suburb or result.store_location
            if header.advertised_delivery_fee and not result.advertised_delivery_fee_display:
                log.info(
                    f"  Delivery fee not read from the search card; using the store page's: {header.advertised_delivery_fee}"
                )
                result.advertised_delivery_fee_display = header.advertised_delivery_fee
                result.advertised_delivery_fee = parse_price(header.advertised_delivery_fee)
            result.advertised_service_fee_display = (
                result.advertised_service_fee_display or header.advertised_service_fee
            )

            write_csv(out / "products.csv", list(MenuItem.model_fields), [m.model_dump() for m in menu.items])
            log.info(
                f"  Store: {result.store_name} | Extracted {len(menu.items)} menu items "
                f"| Advertised delivery: {result.advertised_delivery_fee_display or 'n/a'}"
            )

            chosen = choose_menu_items(menu.items, args.items)
            if not chosen:
                result.fail(Status.NO_ITEMS_FOUND, "No available food/drink items found on the store menu")
                return

            for idx, menu_item in enumerate(chosen, start=1):
                log.info(f"  Adding ({idx}/{len(chosen)}): {menu_item.name} ({menu_item.price})")
                try:
                    item = await add_menu_item_to_cart(stagehand, page, menu_item, result, idx)
                except BROWSER_ERRORS as error:  # keep going with the remaining items
                    item = order_item(menu_item, error=str(error))
                    log.warning(f"    Skipped: {type(error).__name__}: {error}")
                result.items.append(item)

            if not result.added:
                first_error = next((i["error"] for i in result.items if i.get("error")), None)
                result.fail(
                    Status.ADD_TO_CART_FAILED,
                    f"A cart from another store ({leftover.store or 'store not shown'}) could not be cleared"
                    if leftover.items
                    else "No menu items could be added to the order"
                    + (f" (first item: {first_error})" if first_error else ""),
                )
                return

            cart_state, checkout_state = await walk_to_checkout(
                stagehand, page, result, interactive=not self.settings.headless
            )
            if checkout_state and (
                checkout_state.is_checkout_page or checkout_state.delivery_fee or checkout_state.service_fee
            ):
                result.status = Status.OK
            elif cart_state or checkout_state:
                result.fail(Status.NO_CHECKOUT, "Cart reached, but final checkout screen was not reached")
            else:
                result.fail(Status.NO_CHECKOUT, "Checkout screen not reached")
        except UnsafeActionRefused:
            refused = True  # the guard stopped the run: touch nothing else, on this site or the restaurant's
            raise
        finally:
            with contextlib.suppress(*BROWSER_ERRORS):  # the browser may be gone; the URL is a nicety
                result.checkout_url = await page.url()
            if result.added and not refused:
                await self.empty_cart_after_run(stagehand, page, result)
            # However the Uber Eats side ended (even blocked by a login wall), the items that made it into
            # the cart can still be compared with the restaurant's own prices. Not after a guard refusal.
            if not args.skip_direct_site and result.added and not refused:
                try:
                    await audit_direct_restaurant_site(stagehand, page, result, args.address, args.restaurant_url)
                except BROWSER_ERRORS as error:  # the direct-site comparison is an extra; the audit stands without it
                    log.warning(f"  Direct website check skipped/failed: {type(error).__name__}: {error}")
            self.conclude(result, lambda: compare_prices(result, cart_state, checkout_state))
            write_csv(out / "fees_breakdown.csv", FEE_BREAKDOWN_FIELDS, result.comparison.get("fee_breakdown", []))

    async def empty_cart_after_run(self, stagehand: Stagehand, page: Page, result: UberResult) -> None:
        """Go back to the store page and empty the cart, so the next run does not meet this run's items.

        Not once the checkout page has been identified: the guard then allows no clicks on Uber Eats at all
        (it only ever tightens), and the next run's clear step and 'new order' prompt deal with the cart.
        Errors are swallowed; a guard refusal still ends the run.
        """
        if not result.shop_url:
            return
        if current_guard().mode_for(result.shop_url) is not Mode.NORMAL:
            log.info("  Leaving the cart as it is: the checkout page was reached and Uber Eats takes no more clicks.")
            return
        try:
            await page.goto(result.shop_url)
            await settle(page, SETTLE_MS)
            await clear_existing_cart(stagehand, page, result.out_dir, "cart_clear-after.json")
        except BROWSER_ERRORS as error:
            log.warning(f"  Could not empty the cart after the run: {type(error).__name__}: {error}")

    def target_key(self, url: str, result: SiteResult) -> str:
        """The store that was audited (`/store/<slug>/<id>`), so different restaurants keep separate summary rows."""
        path = urlparse(result.shop_url or "").path
        if "/store/" in path:
            return "/store/" + "/".join(path.split("/store/")[-1].split("/")[:2])
        return getattr(result, "store_name", None) or url

    def summarise(self, result: SiteResult) -> None:
        assert isinstance(result, UberResult)
        print_audit_tables(result)
