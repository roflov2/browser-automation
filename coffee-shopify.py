"""Generic coffee-site drip-pricing POC.

For each coffee website URL: let Stagehand find the shop page, read one page of products,
add 1-5 coffee products (one-time purchase, smallest size) to the cart, go to checkout,
screenshot it, and compare shop-page prices against checkout prices to flag drip pricing.

Usage:
    python coffee-shopify.py https://redbrick.coffee https://another-roaster.com --products 3
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
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from urllib.parse import urljoin, urlparse

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

SETTLE_MS = 1500  # fixed wait after navigation/clicks; increase for slow sites
MAX_SHOP_STEPS = 4
OUTPUT_ROOT = Path("output")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class PageCheck(BaseModel):
    is_shop_page: bool = Field(
        description="True only on the store's shop/catalogue page listing coffee products for sale with prices, not the homepage"
    )
    reason: str


class Product(BaseModel):
    name: str
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


class PurchaseOptionCheck(BaseModel):
    has_purchase_options: bool = Field(
        description="True if the product page offers a choice between one-time purchase and subscription"
    )
    one_time_selected: bool = Field(
        description="True if there is no such choice, or if one-time purchase is currently selected"
    )
    reason: str


class SizeOptionCheck(BaseModel):
    has_size_options: bool = Field(
        description="True if the product page has a package size, weight or quantity-pack selector (e.g. 250g, 500g, 1kg, 10 pack)"
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


class LineItem(BaseModel):
    name: str
    variant: str | None = Field(description="Selected options, e.g. '250g / Whole Beans'")
    quantity: int
    price: str | None = Field(description="Line price as displayed, e.g. '$21.00'")


class CartLines(BaseModel):
    is_cart_visible: bool = Field(description="True if a cart page or cart drawer is open showing items")
    items: list[LineItem]
    subtotal: str | None
    has_checkout_button: bool
    reason: str


class Fee(BaseModel):
    label: str
    amount: str


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


def money_sum(values: list[float | None]) -> float | None:
    known = [v for v in values if v is not None]
    return round(sum(known), 2) if known else None


def diff(a: float | None, b: float | None) -> float | None:
    return round(a - b, 2) if a is not None and b is not None else None


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
    items: list[dict] = field(default_factory=list)
    comparison: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Site steps
# ---------------------------------------------------------------------------
async def settle(page: Page, ms: int = SETTLE_MS) -> None:
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("load")
    await page.wait_for_timeout(ms)


async def dismiss_popups(stagehand: Stagehand) -> None:
    """Close cookie banners, newsletter popups, region selectors or age gates if any are visible."""
    with contextlib.suppress(Exception):
        found = await stagehand.observe(
            "Find the close, accept, dismiss or 'no thanks' button of any visible cookie banner, newsletter/discount "
            "popup, region or currency selector modal, or age-verification modal. Return nothing if there is none."
        )
        for action in found.data[:2]:
            print(f"  Dismissing popup: {action.description}")
            await stagehand.act(action)


async def find_shop_page(stagehand: Stagehand, page: Page) -> str:
    check = await stagehand.extract("Is the current page the store's coffee shop page?", PageCheck)
    for _ in range(MAX_SHOP_STEPS):
        if check.data.is_shop_page:
            return await page.url()
        step = await stagehand.act(
            "Take the next step to get to this store's shop page where its coffee is listed for sale "
            "(e.g. a 'Shop', 'Coffee' or 'Shop Coffee' link). If navigation links are hidden behind a menu button, open the menu first."
        )
        await settle(page)
        await dismiss_popups(stagehand)
        check = await stagehand.extract("Is the current page the store's coffee shop page?", PageCheck)
        print(f"  {step.data.action_description} -> {check.data.reason}")
    if check.data.is_shop_page:
        return await page.url()
    raise RuntimeError("Couldn't find the shop page")


def choose_coffees(products: list[Product], n: int) -> list[Product]:
    available = [p for p in products if p.is_coffee and not p.sold_out]
    preferred = [p for p in available if p.product_type == "whole_bean_or_ground"]
    others = [p for p in available if p.product_type != "whole_bean_or_ground"]
    return (preferred + others)[:n]


async def add_product_to_cart(stagehand: Stagehand, page: Page, product: Product, shop_url: str) -> dict:
    item: dict = {
        "name": product.name,
        "shop_price_display": product.price,
        "shop_price": parse_price(product.price),
        "selected_variant": None,
        "selected_price_display": None,
        "selected_price": None,
        "status": "skipped",
    }

    # Open the product page: navigate directly if we have a real link, otherwise click it.
    href = product.url or ""
    if href.startswith(("http://", "https://", "/")):
        await page.goto(urljoin(shop_url, href))
    else:
        await stagehand.act(f"Click on the product '{product.name}' to open its product page.")
    await settle(page)
    await dismiss_popups(stagehand)

    # One-time purchase rather than subscription.
    purchase = await stagehand.extract(
        "Does this product offer one-time purchase vs subscription, and is one-time purchase selected?",
        PurchaseOptionCheck,
    )
    if purchase.data.has_purchase_options and not purchase.data.one_time_selected:
        step = await stagehand.act("Select the one-time purchase option instead of subscription.")
        print(f"    Purchase option: {step.data.action_description}")
        await page.wait_for_timeout(1000)

    # Smallest size / weight.
    size = None
    for _ in range(3):
        size = (
            await stagehand.extract(
                f"Does this product (shop price: {product.price}) have package size or weight options, and is the "
                "smallest/cheapest one selected? Report the price shown for the selected one-time purchase option.",
                SizeOptionCheck,
            )
        ).data
        print(f"    Size: selected={size.selected_size}, lowest={size.lowest_available_size}, price={size.current_price}")
        if not size.has_size_options or size.lowest_size_selected:
            break
        target = size.lowest_available_size or "the smallest/cheapest option"
        step = await stagehand.act(
            f"Change the package size or weight selector (currently '{size.selected_size}') to {target}."
        )
        print(f"    Size selection: {step.data.action_description}")
        await page.wait_for_timeout(1000)

    if size is not None:
        item["selected_variant"] = size.selected_size
        item["selected_price_display"] = size.current_price
        item["selected_price"] = parse_price(size.current_price)

    add = await stagehand.act("Click the 'Add to cart', 'ADD TO BAG', 'Add to bag' or 'Buy' button for this product.")
    print(f"    Added: {add.data.action_description}")
    await page.wait_for_timeout(2000)
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
        await settle(page)
    raise RuntimeError("Couldn't open the shopping cart")


def compare_prices(items: list[dict], cart: CartLines, checkout: CheckoutDetails | None) -> dict:
    added = [i for i in items if i["status"] == "added"]
    for item in added:
        expected = item["selected_price"] if item["selected_price"] is not None else item["shop_price"]
        item["expected_price"] = expected
        item["selected_vs_shop_diff"] = diff(item["selected_price"], item["shop_price"])
        match = None
        if checkout:
            match = next((li for li in checkout.items if item["name"].lower() in li.name.lower()
                          or li.name.lower() in item["name"].lower()), None)
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
    fees = [{"label": f.label, "amount": parse_price(f.amount)} for f in (checkout.other_fees if checkout else [])]
    fees_total = money_sum([f["amount"] for f in fees]) or 0.0
    item_increase = any(i.get("price_increased") for i in added)
    subtotal_increase = (diff(subtotal, expected_total) or 0) > 0

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
        "checkout_total": total,
        "currency": checkout.currency if checkout else None,
        "drip_detected": bool(item_increase or subtotal_increase or fees_total > 0),
        "drip_reasons": [
            r for r, hit in (
                ("item price increased at checkout", item_increase),
                ("checkout subtotal higher than selected prices", subtotal_increase),
                ("non-tax, non-shipping fees added", fees_total > 0),
            ) if hit
        ],
        "checkout_summary": checkout.summary if checkout else None,
    }


async def run_site(stagehand: Stagehand, page: Page, url: str, n_products: int, result: SiteResult) -> None:
    out = result.out_dir
    print(f"\n=== {url} ===")
    await page.goto(url)
    await settle(page)
    await dismiss_popups(stagehand)

    shop_url = await find_shop_page(stagehand, page)
    print(f"  Shop page: {shop_url}")

    products = (
        await stagehand.extract(
            "Extract every product listed on this page with its price, availability, whether it is coffee, its type and link.",
            Products,
        )
    ).data.products
    with open(out / "products.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(Product.model_fields))
        writer.writeheader()
        writer.writerows(p.model_dump() for p in products)
    print(f"  Extracted {len(products)} products ({sum(p.is_coffee for p in products)} coffee)")

    chosen = choose_coffees(products, n_products)
    if not chosen:
        result.status = "no_coffee_found"
        result.message = "No in-stock coffee products on the shop page"
        return

    for product in chosen:
        print(f"  Adding: {product.name} ({product.price})")
        try:
            item = await add_product_to_cart(stagehand, page, product, shop_url)
        except Exception as error:  # keep going with the remaining products
            item = {"name": product.name, "shop_price_display": product.price,
                    "shop_price": parse_price(product.price), "status": "skipped", "error": str(error)}
            print(f"    Skipped: {error}")
        result.items.append(item)
        await page.goto(shop_url)
        await settle(page, 1000)

    if not any(i["status"] == "added" for i in result.items):
        result.status = "add_to_cart_failed"
        result.message = "No products could be added to the cart"
        return

    cart = await open_cart(stagehand, page)
    print(f"  Cart: {len(cart.items)} line(s), subtotal {cart.subtotal}")
    await page.screenshot(path=str(out / "cart.png"))

    checkout: CheckoutDetails | None = None
    if cart.has_checkout_button:
        step = await stagehand.act("Click the 'Check out' / 'CHECKOUT' / 'Checkout' button to proceed to the checkout page.")
        print(f"  Checkout: {step.data.action_description}")
        await page.wait_for_timeout(3000)
        curr_url = await page.url()

        # If the page hasn't navigated away from /cart (e.g. button was a decoy link or required form submit),
        # direct navigation to /checkout activates the session's checkout on Shopify and other platforms:
        if "checkouts" not in curr_url and "checkout" not in curr_url.split("?")[0].rstrip("/").split("/")[-1]:
            print("  Still on cart; navigating to /checkout directly...")
            await page.goto(urljoin(shop_url, "/checkout"))
            await page.wait_for_timeout(3000)
            curr_url = await page.url()

        # Wait for checkout to render (hydration of client-side checkout apps)
        print("  Waiting for checkout page to render...")
        for _ in range(15):
            await page.wait_for_timeout(1000)
            with contextlib.suppress(Exception):
                ready = await page.evaluate("() => document.body && document.body.innerText.trim().length > 100")
                if ready:
                    break

        await page.wait_for_timeout(2000)
        await page.screenshot(path=str(out / "checkout.png"))
        checkout = (
            await stagehand.extract(
                "Extract the checkout order summary: line items, subtotal, shipping, any other fees, tax, total and currency. "
                "Also report if a login, CAPTCHA or bot check is blocking checkout.",
                CheckoutDetails,
            )
        ).data

    result.comparison = compare_prices(result.items, cart, checkout)
    if checkout is None:
        result.status, result.message = "no_checkout", "Cart has no checkout button"
    elif checkout.blocked_reason:
        result.status, result.message = "blocked", checkout.blocked_reason
    elif not checkout.is_checkout_page:
        result.status, result.message = "no_checkout", "Checkout page not reached"
    else:
        result.status = "ok"

    report = {"site": url, "shop_url": shop_url, "checkout_url": await page.url(),
              "status": result.status, "message": result.message,
              "items": result.items, **result.comparison}
    (out / "price_comparison.json").write_text(json.dumps(report, indent=2))
    with open(out / "price_comparison.csv", "w", newline="") as f:
        fields = ["name", "selected_variant", "shop_price_display", "selected_price_display",
                  "checkout_price_display", "checkout_vs_expected_diff", "price_increased", "status"]
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(result.items)

    c = result.comparison
    print(f"  Expected subtotal: {c['expected_subtotal']} | Checkout subtotal: {c['checkout_subtotal_display']} "
          f"| Total: {c['checkout_total_display']} | Fees: {c['other_fees_total']} | Shipping: {c['shipping']}")
    print(f"  Drip pricing detected: {'YES - ' + '; '.join(c['drip_reasons']) if c['drip_detected'] else 'NO'}")


def update_summary(result: SiteResult) -> None:
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
async def main() -> None:
    parser = argparse.ArgumentParser(description="Coffee-site drip-pricing POC")
    parser.add_argument("urls", nargs="+", help="Coffee website URL(s)")
    parser.add_argument("--products", type=int, default=3, choices=range(1, 6), metavar="N",
                        help="Number of coffee products to add to the cart (1-5, default 3)")
    args = parser.parse_args()

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
        browser = await local_browser.launch(headless=os.getenv("HEADLESS") == "true")

    monitor = LocalResourceMonitor()
    monitor_task = monitor.start()
    try:
        stagehand = await Stagehand.create(
            browser=browser,
            model=model,
            model_api_key=api_key,
            logging=StagehandClientLoggingConfig(level="info", format="json", on_log=record),
        )
        try:
            page = (await browser.context.pages())[0]
            for url in args.urls:
                url = url if url.startswith("http") else f"https://{url}"
                domain = urlparse(url).netloc.removeprefix("www.")
                out_dir = OUTPUT_ROOT / domain / datetime.now().strftime("%Y%m%d-%H%M%S")
                out_dir.mkdir(parents=True, exist_ok=True)
                result = SiteResult(site=url, out_dir=out_dir)

                history.clear()
                start = perf_counter()
                before = await stagehand.metrics()
                with contextlib.suppress(Exception):
                    await browser.context.clear_cookies()  # fresh cart per site

                try:
                    await run_site(stagehand, page, url, args.products, result)
                except Exception as error:
                    result.status, result.message = "error", f"{type(error).__name__}: {error}"
                    print(f"  ERROR: {result.message}")
                    with contextlib.suppress(Exception):
                        await page.screenshot(path=str(out_dir / "error.png"))

                after = await stagehand.metrics()
                session = {
                    "site": url,
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
