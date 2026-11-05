"""Let Stagehand find the Redbrick Coffee shop page, select the cheapest size of a product, proceed to checkout, screenshot checkout, compare prices for drip pricing, and record the session."""

import asyncio
import contextlib
import csv
import json
import os
import re
import resource
from datetime import datetime, timezone
from time import perf_counter

from browserbase import Browserbase
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from stagehand import (
    Stagehand,
    StagehandClientLoggingConfig,
    browserbase as stagehand_browserbase,
    local_browser,
)

load_dotenv()


class PageCheck(BaseModel):
    is_shop_page: bool = Field(
        description="True only on the store's shop page listing its products for sale, not the homepage"
    )
    reason: str


class CartCheck(BaseModel):
    is_cart_visible: bool = Field(
        description="True if the shopping cart page or cart drawer is open and showing the added product"
    )
    has_checkout_option: bool = Field(
        description="True if a 'Check out' or 'Checkout' button or link is available in the cart"
    )
    cart_item_price: str | None = Field(
        description="The unit or line price shown for the coffee in the cart, e.g. '$21.00'"
    )
    reason: str


class SizeOptionCheck(BaseModel):
    has_size_options: bool = Field(
        description="True if the product page has a package size or weight selector/options (e.g. 'Weight' or 'Size' such as 225g, 250g, 500g, 1kg)"
    )
    lowest_size_selected: bool = Field(
        description="True only if there are no size/weight options, or if the smallest/lowest size or weight (cheapest option) is currently selected"
    )
    selected_size: str | None = Field(
        description="Currently selected size/weight option if applicable, e.g. '225g' or '1kg'"
    )
    lowest_available_size: str | None = Field(
        description="Smallest/lowest available size/weight option if applicable, e.g. '225g'"
    )
    current_price: str | None = Field(
        description="Currently displayed price on the product page for the selected option, e.g. '$21.00'"
    )
    reason: str


class CheckoutDetails(BaseModel):
    is_checkout_page: bool = Field(
        description="True if currently on the checkout page showing order summary / payment / shipping details"
    )
    item_price: str | None = Field(
        description="Price of the selected coffee item shown on the checkout page, e.g. '$21.00'"
    )
    subtotal: str | None = Field(
        description="Subtotal shown at checkout before shipping, e.g. '$21.00'"
    )
    shipping_or_fees: str | None = Field(
        description="Any shipping cost, surcharge, or extra fee added at checkout if a numeric amount is shown, else null"
    )
    total_price: str | None = Field(
        description="Total price shown at checkout, e.g. '$21.00'"
    )
    summary: str = Field(
        description="Short explanation of the prices shown at checkout vs the item price"
    )


class Product(BaseModel):
    name: str
    price: str = Field(description="Price as displayed, e.g. 'From $15.00'")
    original_price: str | None = Field(description="Crossed-out price if visibly on sale, else null")
    sold_out: bool
    url: str | None


class Products(BaseModel):
    products: list[Product]


def parse_price(value: str | None) -> float | None:
    if not value:
        return None
    match = re.search(r"\d+(?:,\d{3})*(?:\.\d+)?", value)
    return float(match.group(0).replace(",", "")) if match else None


class LocalResourceMonitor:
    def __init__(self) -> None:
        self.cpu_usage: list[float] = []
        self.memory_usage: list[float] = []
        self._task: asyncio.Task[None] | None = None

    def start_monitoring(self) -> asyncio.Task[None]:
        async def sample() -> None:
            while True:
                peak_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                self.memory_usage.append(peak_kb / 1024)  # MB
                self.cpu_usage.append(os.getloadavg()[0])
                await asyncio.sleep(1)

        self._task = asyncio.create_task(sample())
        return self._task

    def get_resource_summary(self) -> dict[str, float]:
        if not self.memory_usage:
            return {
                "avg_memory_usage": 0.0,
                "peak_memory_usage": 0.0,
                "avg_cpu_load": 0.0,
                "total_data_points": 0,
            }
        return {
            "avg_memory_usage": round(sum(self.memory_usage) / len(self.memory_usage), 2),
            "peak_memory_usage": round(max(self.memory_usage), 2),
            "avg_cpu_load": round(sum(self.cpu_usage) / len(self.cpu_usage), 2),
            "total_data_points": len(self.cpu_usage),
        }


async def main() -> None:
    api_key = os.environ["GOOGLE_GENERATIVE_AI_API_KEY"]
    model = "google/" + os.getenv("GEMINI_MODEL", "gemini-3.8-flash").removeprefix("google/")

    history: list[dict] = []

    def record(log) -> None:
        entry = log.model_dump(mode="json")
        entry["timestamp"] = datetime.now(timezone.utc).isoformat()
        history.append(entry)

    bb_api_key = os.getenv("BROWSERBASE_API_KEY")
    bb_project_id = os.getenv("BROWSERBASE_PROJECT_ID")
    bb_client: Browserbase | None = None
    bb_session_id: str | None = None

    monitor = LocalResourceMonitor()
    monitor_task = monitor.start_monitoring()

    if bb_api_key and bb_project_id:
        bb_client = Browserbase(api_key=bb_api_key)
        session = bb_client.sessions.create(project_id=bb_project_id)
        bb_session_id = session.id
        browser = await stagehand_browserbase.connect(
            api_key=bb_api_key,
            session_id=session.id,
        )
    else:
        browser = await local_browser.launch(headless=os.getenv("HEADLESS") == "true")

    try:
        stagehand = await Stagehand.create(
            browser=browser,
            model=model,
            model_api_key=api_key,
            logging=StagehandClientLoggingConfig(level="info", format="json", on_log=record),
        )
        start_time = perf_counter()
        initial_metrics = await stagehand.metrics()

        try:
            page = (await browser.context.pages())[0]
            await page.goto("https://redbrick.coffee/")

            # 1. Stagehand finds the shop page itself: take a step, then check whether we've arrived.
            for _ in range(4):
                step = await stagehand.act(
                    "Take the next step to get to this store's shop page, where all its products are listed "
                    "for sale. If the navigation links are hidden behind a menu button, open the menu first."
                )
                check = await stagehand.extract("Is the current page the store's shop page?", PageCheck)
                print(f"{step.data.action_description} -> {check.data.reason}")
                if check.data.is_shop_page:
                    break
            else:
                raise RuntimeError("Couldn't find the shop page")

            # 2. Extract products on the shop page.
            result = await stagehand.extract("Extract every product on this page with its price.", Products)
            products = result.data.products

            os.makedirs("output", exist_ok=True)
            with open("output/redbrick_products.csv", "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=Product.model_fields)
                writer.writeheader()
                writer.writerows(p.model_dump() for p in products)

            for p in products:
                print(f"{p.name}: {p.price}")
            print(f"\nSaved {len(products)} products from {await page.url()} to output/redbrick_products.csv")

            # 3. Click on an available product and select the lowest size/weight (cheapest option).
            target_product = next((p for p in products if not p.sold_out), products[0])
            print(f"\nOpening product: {target_product.name} (shop price: {target_product.price})")
            open_step = await stagehand.act(
                f"Click on the product '{target_product.name}' to open its product page."
            )
            print(f"Opened product: {open_step.data.action_description}")
            await page.wait_for_load_state("load")
            await page.wait_for_timeout(1500)

            selected_size: str | None = None
            selected_product_price: str | None = None
            for _ in range(3):
                size_check = await stagehand.extract(
                    f"Check if this product (catalog price: {target_product.price}) has package size or weight "
                    "options (such as a 'Weight' or 'Size' dropdown/selector) and whether the smallest/lowest "
                    "weight or package size (the cheapest option) is currently selected.",
                    SizeOptionCheck,
                )
                selected_size = size_check.data.selected_size
                selected_product_price = size_check.data.current_price
                print(
                    f"Size check: {size_check.data.reason} "
                    f"(selected={selected_size}, lowest={size_check.data.lowest_available_size}, "
                    f"price={selected_product_price})"
                )
                if not size_check.data.has_size_options or size_check.data.lowest_size_selected:
                    break
                target_size = size_check.data.lowest_available_size or "the smallest/cheapest size or weight"
                size_step = await stagehand.act(
                    f"Change the package size or weight selector (currently '{selected_size}') "
                    f"to the smallest/lowest option ({target_size}) so the cheapest option is selected."
                )
                print(f"Size selection: {size_step.data.action_description}")
                await page.wait_for_timeout(1000)

            # 4. Add to cart and ensure the cart is open.
            add_step = await stagehand.act(
                "Click the 'Add to cart' or 'Buy' button to add this product to the cart."
            )
            print(f"Added to cart: {add_step.data.action_description}")
            await page.wait_for_timeout(2000)

            cart_info: CartCheck | None = None
            for _ in range(3):
                cart_check = await stagehand.extract(
                    "Is the shopping cart page or cart drawer currently open and showing the added item, "
                    "and is there a checkout button?",
                    CartCheck,
                )
                cart_info = cart_check.data
                if cart_info.is_cart_visible:
                    print(f"Cart visible: {cart_info.reason} (cart item price: {cart_info.cart_item_price})")
                    break
                cart_step = await stagehand.act(
                    "Open or navigate to the shopping cart (click 'View cart' or the cart icon/link in the header)."
                )
                print(f"Cart navigation: {cart_step.data.action_description}")
                await page.wait_for_timeout(1500)
            else:
                raise RuntimeError("Couldn't open the shopping cart")

            # 5. Select the checkout button if available and screenshot the checkout page.
            if cart_info and cart_info.has_checkout_option:
                checkout_step = await stagehand.act(
                    "Click the 'Check out' or 'Checkout' button to proceed to checkout."
                )
                print(f"Proceeded to checkout: {checkout_step.data.action_description}")
                await page.wait_for_load_state("load")
                await page.wait_for_timeout(2500)

            checkout_screenshot_path = "output/checkout.png"
            await page.screenshot(path=checkout_screenshot_path)
            checkout_url = await page.url()
            print(f"Saved checkout screenshot from {checkout_url} to {checkout_screenshot_path}")

            checkout_details = (
                await stagehand.extract(
                    f"Extract the checkout pricing details for '{target_product.name}', including the item price, "
                    "subtotal, any shipping or additional fees, and the total price.",
                    CheckoutDetails,
                )
            ).data

            # 6. Compare shop page price vs selected product price vs checkout price to detect drip pricing.
            shop_amount = parse_price(target_product.price)
            selected_amount = parse_price(selected_product_price)
            cart_amount = parse_price(cart_info.cart_item_price if cart_info else None)
            checkout_item_amount = parse_price(checkout_details.item_price)
            checkout_subtotal_amount = parse_price(checkout_details.subtotal)
            checkout_total_amount = parse_price(checkout_details.total_price)
            fees_amount = parse_price(checkout_details.shipping_or_fees) or 0.0

            effective_final_amount = (
                checkout_total_amount
                or checkout_subtotal_amount
                or checkout_item_amount
                or cart_amount
                or selected_amount
            )
            price_delta = (
                round(effective_final_amount - shop_amount, 2)
                if (effective_final_amount is not None and shop_amount is not None)
                else None
            )
            item_price_delta = (
                round((selected_amount or checkout_item_amount) - shop_amount, 2)
                if ((selected_amount or checkout_item_amount) is not None and shop_amount is not None)
                else None
            )
            drip_pricing_detected = bool(
                (price_delta is not None and price_delta > 0)
                or (item_price_delta is not None and item_price_delta > 0)
                or fees_amount > 0
            )

            price_comparison = {
                "product_name": target_product.name,
                "selected_size": selected_size,
                "shop_page_price_display": target_product.price,
                "shop_page_price_amount": shop_amount,
                "product_page_selected_price_display": selected_product_price,
                "product_page_selected_price_amount": selected_amount,
                "cart_item_price_display": cart_info.cart_item_price if cart_info else None,
                "checkout_item_price_display": checkout_details.item_price,
                "checkout_subtotal_display": checkout_details.subtotal,
                "checkout_shipping_or_fees_display": checkout_details.shipping_or_fees,
                "checkout_total_display": checkout_details.total_price,
                "checkout_total_amount": effective_final_amount,
                "item_price_difference": item_price_delta,
                "total_price_difference": price_delta,
                "drip_pricing_detected": drip_pricing_detected,
                "checkout_summary": checkout_details.summary,
                "checkout_url": checkout_url,
                "checkout_screenshot": checkout_screenshot_path,
            }

            with open("output/price_comparison.json", "w") as f:
                json.dump(price_comparison, f, indent=2)

            with open("output/price_comparison.csv", "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(price_comparison.keys()))
                writer.writeheader()
                writer.writerow(price_comparison)

            print("\n=== Price Comparison (Drip Pricing Check) ===")
            print(f"Product:                  {target_product.name} ({selected_size or 'default'})")
            print(f"Shop Page Price:          {target_product.price} (${shop_amount:.2f})" if shop_amount is not None else f"Shop Page Price:          {target_product.price}")
            print(f"Selected Product Price:   {selected_product_price}")
            print(f"Cart Item Price:          {cart_info.cart_item_price if cart_info else None}")
            print(f"Checkout Item Price:      {checkout_details.item_price}")
            print(f"Checkout Subtotal:        {checkout_details.subtotal}")
            print(f"Checkout Shipping/Fees:   {checkout_details.shipping_or_fees or 'None ($0.00)'}")
            print(f"Checkout Total:           {checkout_details.total_price}")
            print(f"Total Price Difference:   ${price_delta:+.2f}" if price_delta is not None else "Total Price Difference:   N/A")
            print(f"Drip Pricing Increase:    {'YES' if drip_pricing_detected else 'NO'}")
            print(f"Summary:                  {checkout_details.summary}")
            print("Saved price comparison to output/price_comparison.json and output/price_comparison.csv")

            # 7. Record session metrics, resource usage, and operation history.
            final_metrics = await stagehand.metrics()
            execution_time = (perf_counter() - start_time) * 1000
            tokens_used = (
                final_metrics.total_prompt_tokens
                + final_metrics.total_completion_tokens
                - initial_metrics.total_prompt_tokens
                - initial_metrics.total_completion_tokens
            )
            performance_summary = {
                "execution_time": f"{execution_time:.0f}ms",
                "tokens_used": tokens_used,
                "total_inference_time": f"{final_metrics.total_inference_time_ms:.0f}ms",
                "tokens_per_second": f"{tokens_used / (execution_time / 1000):.2f}",
            }
            print("\nLocal Performance Summary:", performance_summary)

            monitor_task.cancel()
            resource_summary = monitor.get_resource_summary()
            print("Resource Usage:", resource_summary)

            errors = [entry for entry in history if entry.get("level") == "error"]
            session_record: dict[str, object] = {
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "product_added": target_product.model_dump(),
                "price_comparison": price_comparison,
                "checkout_url": checkout_url,
                "screenshot_path": checkout_screenshot_path,
                "performance": performance_summary,
                "metrics": final_metrics.model_dump(mode="json"),
                "resource_usage": resource_summary,
                "error_count": len(errors),
                "history": history,
            }

            if bb_client is not None and bb_session_id is not None:
                session_info = bb_client.sessions.retrieve(bb_session_id)
                bb_summary = {
                    "id": session_info.id,
                    "status": session_info.status,
                    "region": session_info.region,
                    "avg_cpu_usage": session_info.avg_cpu_usage,
                    "memory_usage": session_info.memory_usage,
                    "proxy_bytes": session_info.proxy_bytes,
                }
                print("Browserbase Session:", bb_summary)
                session_record["browserbase_session"] = bb_summary

            with open("output/session.json", "w") as f:
                json.dump(session_record, f, indent=2)
            print(f"Saved session recording ({len(history)} log events) to output/session.json")
        finally:
            with contextlib.suppress(Exception):
                await stagehand.close()
    finally:
        monitor_task.cancel()
        with contextlib.suppress(Exception):
            await browser.close()


asyncio.run(main())
