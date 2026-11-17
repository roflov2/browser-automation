"""Uber Eats food delivery: drip pricing at checkout, and menu markup against the restaurant's own site.

1. Start on Uber Eats AU (or a direct `/store/...` URL), set the delivery address, and record the
   advertised delivery fee and service fee shown on the feed card and store banner.
2. Clear leftover cart items, then add 1-5 menu items (auto-selecting $0/cheapest required
   modifiers and declining paid add-ons).
3. Walk through the cart drawer to the final checkout screen and extract every fee.
4. Visit the restaurant's own website (found by search, or `--restaurant-url`), walk it until item
   prices are on screen, and compare its unit prices against the Uber Eats prices.

The run always stops on the checkout screen: nothing is ordered or paid for.
"""

import argparse
import contextlib
from urllib.parse import urlparse

from stagehand import Page, Stagehand

from ...config import Settings
from ...core import ITEM_FIELDS, UnsafeActionRefused, parse_price, slug, write_csv
from ...page import settle, shot
from ...workflow import Workflow
from .direct_site import audit_direct_restaurant_site
from .models import CartAndCheckoutState, MenuItem, StoreMenu, UberResult
from .pricing import FEE_BREAKDOWN_FIELDS, compare_prices, print_audit_tables
from .store import (
    SITE_URL,
    add_menu_item_to_cart,
    choose_menu_items,
    clear_existing_cart,
    dismiss_uber_popups,
    ensure_address_and_open_store,
    order_item,
    walk_to_checkout,
)

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
    item_fields = [
        *ITEM_FIELDS[:-1],
        "direct_item_name",
        "direct_price_display",
        "uber_vs_direct_diff",
        "uber_vs_direct_markup_pct",
        ITEM_FIELDS[-1],
    ]

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

    async def run(self, stagehand: Stagehand, page: Page, url: str, result: UberResult) -> None:
        args, out = self.args, result.out_dir
        result.search = {"address": args.address, "query": args.query}
        cart_state: CartAndCheckoutState | None = None
        checkout_state: CartAndCheckoutState | None = None
        refused = False

        print(f"\n=== {url} | Address: {args.address} | Query: {args.query or 'any'} ===")
        try:
            await page.goto(url)
            await settle(page, SETTLE_MS)
            await dismiss_uber_popups(stagehand, page)

            result.shop_url = await ensure_address_and_open_store(
                stagehand, page, args.address, args.query, result, interactive=not self.settings.headless
            )
            print(f"  Store page: {result.shop_url}")
            await clear_existing_cart(stagehand, page)
            await shot(page, out / "store.png")

            menu = (
                await stagehand.extract(
                    "Extract the restaurant name, street/suburb location (if shown), advertised delivery fee and service fee "
                    "shown in the store header/banner, and extract the menu items listed on this page with their category, "
                    "price, availability, and whether each is food/drink.",
                    StoreMenu,
                )
            ).data
            header = menu.store_header
            result.store_name = header.store_name or result.store_name
            result.store_location = header.store_address_or_suburb or result.store_location
            if header.advertised_delivery_fee and not result.advertised_delivery_fee_display:
                result.advertised_delivery_fee_display = header.advertised_delivery_fee
                result.advertised_delivery_fee = parse_price(header.advertised_delivery_fee)
            result.advertised_service_fee_display = (
                result.advertised_service_fee_display or header.advertised_service_fee
            )

            write_csv(out / "products.csv", list(MenuItem.model_fields), [m.model_dump() for m in menu.items])
            print(
                f"  Store: {result.store_name} | Extracted {len(menu.items)} menu items "
                f"| Advertised delivery: {result.advertised_delivery_fee_display or 'n/a'}"
            )

            chosen = choose_menu_items(menu.items, args.items)
            if not chosen:
                result.status, result.message = (
                    "no_items_found",
                    "No available food/drink items found on the store menu",
                )
                return

            for idx, menu_item in enumerate(chosen, start=1):
                print(f"  Adding ({idx}/{len(chosen)}): {menu_item.name} ({menu_item.price})")
                try:
                    item = await add_menu_item_to_cart(stagehand, page, menu_item, result, idx)
                except Exception as error:
                    item = order_item(menu_item, error=str(error))
                    print(f"    Skipped: {error}")
                result.items.append(item)

            if not result.added:
                result.status, result.message = "add_to_cart_failed", "No menu items could be added to the order"
                return

            cart_state, checkout_state = await walk_to_checkout(
                stagehand, page, result, interactive=not self.settings.headless
            )
            if checkout_state and (
                checkout_state.is_checkout_page or checkout_state.delivery_fee or checkout_state.service_fee
            ):
                result.status = "ok"
            elif cart_state or checkout_state:
                result.status, result.message = "no_checkout", "Cart reached, but final checkout screen was not reached"
            else:
                result.status, result.message = "no_checkout", "Checkout screen not reached"
        except UnsafeActionRefused:
            refused = True  # the guard stopped the run: touch nothing else, on this site or the restaurant's
            raise
        finally:
            # However the Uber Eats side ended (even blocked by a login wall), the items that made it
            # into the cart can still be compared with the restaurant's own prices.
            with contextlib.suppress(Exception):
                result.checkout_url = await page.url()
            if not args.skip_direct_site and result.added and not refused:
                try:
                    await audit_direct_restaurant_site(stagehand, page, result, args.address, args.restaurant_url)
                except Exception as error:
                    print(f"  Direct website check skipped/failed: {error}")
            result.comparison = compare_prices(result, cart_state, checkout_state)
            write_csv(out / "fees_breakdown.csv", FEE_BREAKDOWN_FIELDS, result.comparison["fee_breakdown"])

    def summarise(self, result: UberResult) -> None:
        print_audit_tables(result)
