"""Coffee roaster / retailer stores (Shopify-style storefronts).

For each store URL: let Stagehand find the shop page, read one page of products, add 1-5 coffee
products (one-time purchase, smallest size) to the cart, go to checkout, screenshot it, and compare
shop-page prices against checkout prices to flag drip pricing.
"""

import argparse
import contextlib
import re
from urllib.parse import urljoin

from pydantic import BaseModel, Field
from stagehand import Page, Stagehand

from ..core import (
    Fee,
    LineItem,
    SiteResult,
    compare_item_prices,
    diff,
    drip_verdict,
    money_sum,
    new_item,
    norm_name,
    parse_price,
    pop_best_line,
    write_csv,
)
from ..page import dismiss_popups, is_sso_redirect, js, maybe_dismiss_popups, settle, shot, wait_for_render
from ..workflow import Workflow

SETTLE_MS = 1500  # fixed wait after navigation/clicks; increase for slow sites
MAX_SHOP_STEPS = 4
POPUPS = "cookie banner, newsletter/discount popup, region or currency selector modal, or age-verification modal"
COFFEE_AFFIX = re.compile(r"(?i)^coffee\s+|\s+coffee$")  # 'Coffee X' / 'X Coffee' -> 'X'


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class PageCheck(BaseModel):
    is_shop_page: bool = Field(
        description="True only on the store's shop/catalogue page listing coffee products for sale with prices, not the homepage"
    )
    reason: str


class Product(BaseModel):
    name: str = Field(
        description="Clean product title without duplicated collection/category prefixes (e.g. 'Ethiopia Worka Sakaro', not 'Coffee Ethiopia Worka Sakaro')"
    )
    price: str = Field(description="Price as displayed, e.g. 'From $15.00'")
    sold_out: bool
    is_coffee: bool = Field(
        description="True for coffee for drinking: whole bean or ground coffee, pods, capsules, drip bags or steep bags. "
        "False for equipment, mugs, merch, gift cards, courses and subscription-only products."
    )
    product_type: str = Field(
        description="One of: whole_bean_or_ground, pods_or_capsules, drip_or_steep_bags, equipment, merch, gift_card, subscription, course, other"
    )
    url: str | None = Field(description="Link (href) to the product page, if present")


class Products(BaseModel):
    products: list[Product]


class ProductPageState(BaseModel):
    is_product_page: bool = Field(
        description="True if on an individual product page with an Add to Cart / Buy button (not a collection or catalogue page)"
    )
    has_purchase_options: bool = Field(
        description="True if the product page offers a choice between one-time purchase and subscription"
    )
    one_time_selected: bool = Field(
        description="True if there is no subscription choice, or if one-time purchase is currently selected"
    )
    has_size_options: bool = Field(
        description="True if the product page has a package size, weight or quantity-pack selector (e.g. 225g, 250g, 500g, 1kg, 10 pack)"
    )
    lowest_size_selected: bool = Field(
        description="True only if there are no size options, or the smallest/cheapest size is currently selected"
    )
    selected_size: str | None = Field(description="Currently selected size option, e.g. '250g'")
    lowest_available_size: str | None = Field(description="Smallest available size option, e.g. '250g'")
    current_price: str | None = Field(
        description="Price currently displayed for the selected one-time purchase option, e.g. '$21.00'"
    )
    reason: str


class CartLines(BaseModel):
    is_cart_visible: bool = Field(description="True if a cart page or cart drawer is open showing items")
    items: list[LineItem]
    subtotal: str | None
    has_checkout_button: bool
    reason: str


class CheckoutDetails(BaseModel):
    is_checkout_page: bool = Field(description="True if on a checkout page showing an order summary")
    blocked_reason: str | None = Field(
        description="If progress is blocked by a required login, CAPTCHA, 'verify you are human' or bot check, describe it; otherwise null"
    )
    items: list[LineItem]
    subtotal: str | None
    shipping: str | None = Field(
        description="Shipping amount if a number is shown; null if not yet calculated (e.g. 'Enter shipping address')"
    )
    other_fees: list[Fee] = Field(
        description="Any other added charges that are NOT tax and NOT shipping, e.g. service fee, handling fee, card surcharge"
    )
    tax_amount: str | None
    tax_included: bool = Field(description="True if tax is included in item prices (e.g. 'Including $1.91 in taxes')")
    total: str | None
    currency: str | None = Field(description="Currency code if shown, e.g. 'AUD'")
    summary: str


# ---------------------------------------------------------------------------
# Site steps
# ---------------------------------------------------------------------------
async def find_shop_page(stagehand: Stagehand, page: Page) -> str:
    for _ in range(MAX_SHOP_STEPS + 2):
        curr_url = await page.url()
        if is_sso_redirect(curr_url):
            print("  Waiting for corporate SSO redirect...")
            await page.wait_for_timeout(3000)
            continue
        if curr_url.startswith("chrome-error://"):
            raise RuntimeError(f"Page failed to load ({curr_url})")

        check = await stagehand.extract("Is the current page the store's coffee shop page?", PageCheck)
        if check.data.is_shop_page:
            return curr_url
        if "ERR_" in check.data.reason or "failed to load" in check.data.reason.lower():
            raise RuntimeError(check.data.reason)

        step = await stagehand.act(
            "Take the next step to get to this store's shop page where its coffee is listed for sale "
            "(e.g. a 'Shop', 'Coffee' or 'Shop Coffee' link). If navigation links are hidden behind a menu button, open the menu first."
        )
        await settle(page, SETTLE_MS)
        await maybe_dismiss_popups(stagehand, page, POPUPS)
        print(f"  {step.data.action_description} -> {check.data.reason}")
    raise RuntimeError("Couldn't find the shop page")


def choose_coffees(products: list[Product], n: int) -> list[Product]:
    available = [p for p in products if p.is_coffee and not p.sold_out]
    preferred = [p for p in available if p.product_type == "whole_bean_or_ground"]
    others = [p for p in available if p.product_type != "whole_bean_or_ground"]
    return (preferred + others)[:n]


JS_FIND_PRODUCT_LINK = r"""({ names, currentUrl }) => {
    const norm = s => (s || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
    const targets = new Set(names.map(norm).filter(Boolean));
    const base = currentUrl.split('?')[0].replace(/\/+$/, '');

    const anchors = Array.from(document.querySelectorAll('a[href]'));
    // Prefer links containing /products/ or /product/ when multiple links match
    anchors.sort((a, b) => (b.href.includes('/product') ? 1 : 0) - (a.href.includes('/product') ? 1 : 0));

    for (const a of anchors) {
        const rawHref = a.getAttribute('href') || '';
        if (!rawHref || rawHref.startsWith('#') || rawHref.startsWith('javascript:')) continue;

        const resolved = a.href.split('?')[0].replace(/\/+$/, '');
        if (!resolved || resolved === base) continue;

        const heading = a.querySelector('h1, h2, h3, h4, h5, [class*="title" i], [class*="name" i]');
        const candidates = [
            heading ? norm(heading.innerText) : '',
            norm(a.getAttribute('aria-label')),
            norm(a.title),
            norm(a.innerText)
        ].filter(Boolean);

        if (candidates.some(c => targets.has(c))) return a.href;
    }
    return null;
}"""


async def resolve_product_url(page: Page, product: Product, shop_url: str) -> str | None:
    """Return a validated product URL from extraction or exact DOM match; None if clicking is required."""
    if product.url and product.url.startswith(("http://", "https://", "/")):
        full = urljoin(shop_url, product.url)
        if full.split("?")[0].rstrip("/") != shop_url.split("?")[0].rstrip("/"):
            return full

    names = [product.name, COFFEE_AFFIX.sub("", product.name).strip()]
    href = await js(page, JS_FIND_PRODUCT_LINK, {"names": names, "currentUrl": shop_url})
    return href if isinstance(href, str) and href else None


async def add_product_to_cart(stagehand: Stagehand, page: Page, product: Product, shop_url: str) -> dict:
    item = new_item(product.name, product.price)

    # Open the product page: navigate directly via resolved URL, otherwise fall back to Stagehand click.
    target_url = await resolve_product_url(page, product, shop_url)
    if target_url:
        await page.goto(target_url)
    else:
        await stagehand.act(f"Click on the product '{product.name}' to open its product page.")
    await settle(page, 1000)
    await maybe_dismiss_popups(stagehand, page, POPUPS)

    # Combined check for product page validity, one-time purchase option, and smallest size (allows multi-step dropdowns).
    state: ProductPageState | None = None
    for attempt in range(3):
        state = (
            await stagehand.extract(
                f"Check this page for product '{product.name}' (shop price: {product.price}): "
                "1) Is this an individual product page with an Add to Cart / Buy button? "
                "2) Does it offer one-time purchase vs subscription, and is one-time purchase selected? "
                "3) Does it have package size or weight options, and is the smallest/cheapest one selected? "
                "4) Report the price currently displayed for the selected one-time purchase option.",
                ProductPageState,
            )
        ).data

        if not state.is_product_page:
            if attempt == 0 and not target_url:
                # If the initial click didn't open the product page, try once more
                await stagehand.act(f"Click on the product link or title for '{product.name}' to open its product page.")
                await settle(page, 1000)
                continue
            print(f"    Not on a valid product page for '{product.name}' ({state.reason}), skipping.")
            return item

        acted = False
        if state.has_purchase_options and not state.one_time_selected:
            step = await stagehand.act("Select the one-time purchase option instead of subscription.")
            print(f"    Purchase option: {step.data.action_description}")
            await page.wait_for_timeout(800)
            acted = True

        print(f"    Size: selected={state.selected_size}, lowest={state.lowest_available_size}, price={state.current_price}")
        if state.has_size_options and not state.lowest_size_selected:
            target = state.lowest_available_size or "the smallest/cheapest option"
            step = await stagehand.act(
                f"Change the package size or weight selector (currently '{state.selected_size}') to {target}."
            )
            print(f"    Size selection: {step.data.action_description}")
            await page.wait_for_timeout(800)
            acted = True

        if not acted:
            break

    if state is not None:
        item["selected_variant"] = state.selected_size
        item["selected_price_display"] = state.current_price or product.price
        item["selected_price"] = parse_price(state.current_price) or item["shop_price"]

    add = await stagehand.act("Click the 'Add to cart', 'ADD TO BAG', 'Add to bag' or 'Buy' button for this product.")
    print(f"    Added: {add.data.action_description}")
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("networkidle", timeout=3000)
    await page.wait_for_timeout(1200)
    item["status"] = "added" if add.data.success else "add_failed"
    return item


async def open_cart(stagehand: Stagehand, page: Page) -> CartLines:
    for _ in range(3):
        cart = (
            await stagehand.extract(
                "Is a cart page or cart drawer open? List its line items, subtotal and whether there is a checkout button.",
                CartLines,
            )
        ).data
        if cart.is_cart_visible and cart.items:
            return cart
        step = await stagehand.act("Open the shopping cart (click the cart/bag icon or link in the header, or 'View cart').")
        print(f"  Cart navigation: {step.data.action_description}")
        await settle(page, SETTLE_MS)
    raise RuntimeError("Couldn't open the shopping cart")


def match_name(name: str) -> str:
    """Normalised product name without a leading/trailing 'coffee', which carts add or drop freely."""
    return norm_name(COFFEE_AFFIX.sub("", name)) or norm_name(name)


def compare_prices(added: list[dict], cart: CartLines, checkout: CheckoutDetails | None) -> dict:
    # No containment matching: 'Coffee' must not match 'Good Day Coffee'
    find_line = pop_best_line(checkout.items if checkout else [], threshold=0.5, containment=False, norm=match_name)
    expected_total = compare_item_prices(added, find_line)
    subtotal = parse_price(checkout.subtotal) if checkout else None
    fees = [{"label": f.label, "amount": parse_price(f.amount)} for f in (checkout.other_fees if checkout else [])]
    fees_total = money_sum([f["amount"] for f in fees]) or 0.0
    reasons = [
        reason
        for reason, hit in (
            ("item price increased at checkout", any(i["price_increased"] for i in added)),
            ("checkout subtotal higher than selected prices", (diff(subtotal, expected_total) or 0) > 0),
            ("non-tax, non-shipping fees added", fees_total > 0),
        )
        if hit
    ]
    return {
        "items_added": len(added),
        "expected_subtotal": expected_total,
        "cart_subtotal_display": cart.subtotal,
        "checkout_subtotal_display": checkout.subtotal if checkout else None,
        "checkout_subtotal": subtotal,
        "subtotal_vs_expected_diff": diff(subtotal, expected_total),
        "shipping": (checkout.shipping if checkout and checkout.shipping else "not calculated (address required)"),
        "other_fees": fees,
        "other_fees_total": fees_total,
        "tax_amount_display": checkout.tax_amount if checkout else None,
        "tax_included": checkout.tax_included if checkout else None,
        "checkout_total_display": checkout.total if checkout else None,
        "checkout_total": parse_price(checkout.total) if checkout else None,
        "currency": checkout.currency if checkout else None,
        "drip_detected": bool(reasons),
        "drip_reasons": reasons,
        "checkout_summary": checkout.summary if checkout else None,
    }


async def go_to_checkout(stagehand: Stagehand, page: Page, result: SiteResult) -> CheckoutDetails:
    step = await stagehand.act("Click the 'Check out' / 'CHECKOUT' / 'Checkout' button to proceed to the checkout page.")
    print(f"  Checkout: {step.data.action_description}")
    await page.wait_for_timeout(3000)

    # If the page hasn't left /cart (e.g. the button was a decoy link or needed a form submit), navigating
    # straight to /checkout activates the session's checkout on Shopify and other platforms.
    curr_url = await page.url()
    if "checkouts" not in curr_url and "checkout" not in curr_url.split("?")[0].rstrip("/").split("/")[-1]:
        print("  Still on cart; navigating to /checkout directly...")
        await page.goto(urljoin(result.shop_url, "/checkout"))
        await page.wait_for_timeout(3000)

    print("  Waiting for checkout page to render...")
    await wait_for_render(page, 15)
    await page.wait_for_timeout(2000)
    await shot(page, result.out_dir / "checkout.png", full_page=False)
    return (
        await stagehand.extract(
            "Extract the checkout order summary: line items, subtotal, shipping, any other fees, tax, total and currency. "
            "Also report if a login, CAPTCHA or bot check is blocking checkout.",
            CheckoutDetails,
        )
    ).data


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------
class CoffeeWorkflow(Workflow):
    name = "coffee"
    help = "Coffee stores: add 1-5 coffees to the cart and compare shop prices with checkout"
    clear_cookies = "domain"

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("urls", nargs="+", help="Coffee website URL(s)")
        parser.add_argument("--products", type=int, default=3, choices=range(1, 6), metavar="N",
                            help="Number of coffee products to add to the cart (1-5, default 3)")

    def targets(self) -> list[str]:
        return self.args.urls

    async def run(self, stagehand: Stagehand, page: Page, url: str, result: SiteResult) -> None:
        out = result.out_dir
        print(f"\n=== {url} ===")
        await page.goto(url)
        await settle(page, SETTLE_MS)
        await dismiss_popups(stagehand, POPUPS)

        shop_url = result.shop_url = await find_shop_page(stagehand, page)
        print(f"  Shop page: {shop_url}")

        products = (
            await stagehand.extract(
                "Extract every product listed on this page with its price, availability, whether it is coffee, its type and link.",
                Products,
            )
        ).data.products
        write_csv(out / "products.csv", list(Product.model_fields), [p.model_dump() for p in products])
        print(f"  Extracted {len(products)} products ({sum(p.is_coffee for p in products)} coffee)")

        chosen = choose_coffees(products, self.args.products)
        if not chosen:
            result.status, result.message = "no_coffee_found", "No in-stock coffee products on the shop page"
            return

        for idx, product in enumerate(chosen):
            print(f"  Adding ({idx + 1}/{len(chosen)}): {product.name} ({product.price})")
            try:
                item = await add_product_to_cart(stagehand, page, product, shop_url)
            except Exception as error:  # keep going with the remaining products
                item = new_item(product.name, product.price, error=str(error))
                print(f"    Skipped: {error}")
            result.items.append(item)
            # Only go back to the shop if there are more products to add; staying on the final
            # product page preserves any auto-opened cart drawer.
            if idx < len(chosen) - 1:
                await page.goto(shop_url)
                await settle(page, 1000)

        if not result.added:
            result.status, result.message = "add_to_cart_failed", "No products could be added to the cart"
            return

        cart = await open_cart(stagehand, page)
        print(f"  Cart: {len(cart.items)} line(s), subtotal {cart.subtotal}")
        await shot(page, out / "cart.png", full_page=False)

        checkout = await go_to_checkout(stagehand, page, result) if cart.has_checkout_button else None
        result.checkout_url = await page.url()
        result.comparison = compare_prices(result.added, cart, checkout)
        if checkout is None:
            result.status, result.message = "no_checkout", "Cart has no checkout button"
        elif checkout.blocked_reason:
            result.status, result.message = "blocked", checkout.blocked_reason
        elif not checkout.is_checkout_page:
            result.status, result.message = "no_checkout", "Checkout page not reached"
        else:
            result.status = "ok"

    def summarise(self, result: SiteResult) -> None:
        c = result.comparison
        print(
            f"  Expected subtotal: {c['expected_subtotal']} | Checkout subtotal: {c['checkout_subtotal_display']} "
            f"| Total: {c['checkout_total_display']} | Fees: {c['other_fees_total']} | Shipping: {c['shipping']}"
        )
        print(f"  Drip pricing detected: {drip_verdict(c)}")
