"""The coffee workflow's own add-to-cart click in a real browser, on a page laid out like Ona Coffee's theme.

A hidden sticky "quick add" bar comes first in the DOM, then a subscription button and the real product form,
then another product section. /cart.js is faked in the page and adds land after a delay. No model is involved.
"""

import asyncio
from pathlib import Path

import pytest

from audit.page import js
from audit.workflows import coffee

from .browser_support import find_chrome, launch_args, serve

FIXTURE = Path(__file__).parent / "fixtures" / "pages"
pytestmark = pytest.mark.browser

STICKY = "xpath=/html[1]/body[1]/main[1]/sticky-add-to-cart[1]/div[1]/button[1]"
MAIN = "xpath=//product-form-component//button[@name='add']"


async def drive(chrome: str, port: int) -> dict:
    from stagehand import Stagehand, local_browser

    seen: dict = {}
    browser = await local_browser.launch(headless=True, executable_path=chrome, args=launch_args())
    stagehand = await Stagehand.create(browser=browser)
    try:
        page = (await browser.context.pages())[0]
        await page.goto(f"http://127.0.0.1:{port}/quick-add.html")
        seen["visible_choice"] = await coffee.first_visible_choice(page, [STICKY, MAIN])
        seen["visible_none"] = await coffee.first_visible_choice(page, [STICKY, "xpath=//nothing"])

        before = await coffee.shopify_item_count(page)
        seen["direct"] = await coffee.click_shopify_add_button(page)
        seen["after"] = await coffee.wait_for_cart_growth(page, before)
        seen["before"] = before
        seen["clicked"] = await js(page, "() => window.clicked")
        seen["subscription"] = coffee.subscription_line(await coffee.fetch_json(page, "/cart.js"), "Maple")
        seen["selector_hits"] = await js(
            page, "sel => document.querySelector(sel).id", seen["direct"].selector.removeprefix("css=")
        )

        # With the main button disabled (sold out), nothing else on the page may be clicked instead
        await js(
            page,
            "() => { document.getElementById('BuyButtons-ProductSubmitButton-main__add-to-cart').disabled = true; }",
        )
        seen["disabled"] = await coffee.click_shopify_add_button(page)
        seen["clicked_after_disabled"] = await js(page, "() => window.clicked")
    finally:
        await stagehand.close()
    return seen


@pytest.fixture(scope="module")
def seen() -> dict:
    chrome = find_chrome()
    if chrome is None:
        pytest.skip("no Chrome/Chromium found (set CHROME_PATH to run the browser tests)")
    with serve(FIXTURE) as port:
        return asyncio.run(drive(chrome, port))


def test_the_product_form_button_is_clicked_not_the_sticky_bar(seen):
    assert seen["clicked"] == ["main"]
    assert seen["direct"].success
    assert "sticky" not in seen["direct"].selector
    assert seen["selector_hits"] == "BuyButtons-ProductSubmitButton-main__add-to-cart"


def test_a_slow_add_is_still_counted(seen):
    assert (seen["before"], seen["after"]) == (0, 1)
    assert seen["subscription"] is None


def test_no_other_button_stands_in_for_a_disabled_one(seen):
    assert seen["disabled"] is None
    assert seen["clicked_after_disabled"] == ["main"]  # no subscribe, buy-now, sticky or other-section click


def test_the_model_fallback_skips_the_hidden_sticky_button(seen):
    assert seen["visible_choice"] == 1
    assert seen["visible_none"] is None
