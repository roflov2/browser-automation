"""The Phase 5 savings in a real browser: Shopify's /cart.js, the page fingerprint, and trimmed extractions."""

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from audit.page import fetch_json, fingerprint
from audit.runner import EXTRACT_IGNORE, RecordingStagehand, SessionLog
from audit.workflows.coffee import cart_from_shopify, shopify_cart

from .browser_support import StubModel, find_chrome, launch_args, serve

FIXTURE = Path(__file__).parent / "fixtures" / "shopify"
pytestmark = pytest.mark.browser


class Total(BaseModel):
    total: str | None


async def observe(chrome: str, port: int) -> dict:
    from stagehand import Stagehand, local_browser

    seen: dict = {}
    stub = StubModel({"Total": {"total": "$44.00"}})
    browser = await local_browser.launch(headless=True, executable_path=chrome, args=launch_args())
    client = await Stagehand.create(browser=browser, model=stub)
    try:
        page = (await browser.context.pages())[0]
        await page.goto(f"http://127.0.0.1:{port}/index.html")
        seen["cart"] = await shopify_cart(page)
        seen["missing"] = await fetch_json(page, "/products/nope.js")
        seen["html_not_json"] = await fetch_json(page, "/index.html")

        first = await fingerprint(page)
        await page.locator("#nothing").click()
        seen["unchanged"] = first == await fingerprint(page)
        await page.locator("#open").click()
        seen["changed"] = first != await fingerprint(page)

        plain_calls = len(stub.prompts)
        await client.extract("Read the total", Total)
        seen["plain_prompt"] = stub.prompts[plain_calls]
        trimmed = RecordingStagehand(client, SessionLog(), page)
        calls = len(stub.prompts)
        seen["trimmed_result"] = (await trimmed.extract("Read the total", Total)).data.total
        seen["trimmed_prompt"] = stub.prompts[calls]
    finally:
        await client.close()
    return seen


@pytest.fixture(scope="module")
def seen() -> dict:
    chrome = find_chrome()
    if chrome is None:
        pytest.skip("no Chrome/Chromium found (set CHROME_PATH to run the browser tests)")
    with serve(FIXTURE) as port:
        return asyncio.run(observe(chrome, port))


def test_shopify_cart_is_read_without_the_model(seen):
    cart = seen["cart"]
    assert cart.subtotal == "$67.00"
    assert [(line.name, line.variant, line.quantity, line.price) for line in cart.items] == [
        ("Rogue", "225g / Whole Beans", 1, "$23.00"),
        ("Good Day", "225g / Whole Beans", 1, "$23.00"),
        ("Coffee", "225g", 1, "$21.00"),
    ]
    assert cart.has_checkout_button and cart.reason.endswith("/cart.js")


def test_only_json_counts(seen):
    assert seen["missing"] is None  # 404
    assert seen["html_not_json"] is None  # a store without the API answers with a page


def test_fingerprint_tells_a_no_op_from_a_change(seen):
    assert seen["unchanged"] is True
    assert seen["changed"] is True


def test_extractions_leave_out_the_footer(seen):
    assert seen["trimmed_result"] == "$44.00"
    assert "Copyright Fixture Roasters" in seen["plain_prompt"]
    assert "Copyright Fixture Roasters" not in seen["trimmed_prompt"]
    assert "Subtotal $44.00" in seen["trimmed_prompt"] and "Fixture Roasters" in seen["trimmed_prompt"]  # header kept
    assert len(seen["trimmed_prompt"]) < len(seen["plain_prompt"])


def test_cart_from_shopify_rejects_what_is_not_a_cart():
    assert cart_from_shopify(None) is None
    assert cart_from_shopify({"items": []}) is None
    assert cart_from_shopify("<html>") is None
    multi = cart_from_shopify(
        {"total_price": 4600, "items": [{"title": "Rogue", "quantity": 2, "final_line_price": 4600}]}
    )
    assert (multi.items[0].quantity, multi.items[0].price, multi.subtotal) == (2, "$46.00", "$46.00")
    assert "footer" in " ".join(EXTRACT_IGNORE) and not any("header" in s for s in EXTRACT_IGNORE)


def test_fixture_cart_is_valid_json():
    json.loads((FIXTURE / "cart.js").read_text(encoding="utf-8"))
