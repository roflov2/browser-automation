"""Uber Eats page steps in a real browser, against local copies of the cart drawer and the search results."""

import asyncio
from pathlib import Path

import pytest

from audit import safety
from audit.page import js
from audit.workflows.ubereats.models import StoreCard, UberResult
from audit.workflows.ubereats.store import (
    JS_ITEM_DIALOG_BLOCKER,
    clear_existing_cart,
    first_open_store_card,
    get_cart_count,
    is_item_modal_open,
    store_closed_reason,
    store_url,
)
from audit.workflows.ubereats.store import js as uber_js  # with the Uber Eats page helpers loaded

from .browser_support import find_chrome, launch_args, serve

FIXTURE = Path(__file__).parent / "fixtures" / "pages"
pytestmark = pytest.mark.browser
CASES = {
    "icons": "",
    "labels": "?labels=1",
    "confirm": "?confirm=1",
    "carts_by_store": "?carts=1",
    "more_menu": "?more=1",
}
STORES = {  # case: (the model's card, or None when its extraction failed)
    "model_failed": None,
    "model_gave_element_id": StoreCard(
        name="OOSHMAN", delivery_fee_display=None, service_fee_display=None, eta=None,
        available=True, is_restaurant=True, url="7-1809",
    ),
    "model_gave_link_but_no_fee": StoreCard(
        name="La Casetta", delivery_fee_display=None, service_fee_display=None, eta=None,
        available=True, is_restaurant=True, url="/au/store/la-casetta/ddd",
    ),
    "card_shows_no_fee": StoreCard(
        name="Pizza Stop", delivery_fee_display=None, service_fee_display=None, eta=None,
        available=True, is_restaurant=True, url=None,
    ),
}  # fmt: skip


class NoModel:
    """Stagehand stand-in: the DOM steps alone must clear the cart, so any LLM action is recorded and fails."""

    def __init__(self) -> None:
        self.instructions: list[str] = []

    async def observe(self, instruction: str, **_: object):
        self.instructions.append(instruction)
        raise RuntimeError("no model in this test")


async def clear_carts(chrome: str, port: int) -> dict:
    from stagehand import Stagehand, local_browser

    seen: dict = {}
    browser = await local_browser.launch(headless=True, executable_path=chrome, args=launch_args())
    stagehand = await Stagehand.create(browser=browser)
    try:
        page = (await browser.context.pages())[0]
        await safety.install(browser.context, page)
        with safety.active_guard():
            for case, query in CASES.items():
                await page.goto(f"http://127.0.0.1:{port}/uber-cart.html{query}")
                model = NoModel()
                before = await get_cart_count(page)
                await clear_existing_cart(model, page)  # type: ignore[arg-type]
                seen[case] = {
                    "before": before,
                    "after": await get_cart_count(page),
                    "plus_clicks": await js(page, "() => window.plusClicks"),
                    "llm": model.instructions,
                }
            await page.goto(f"http://127.0.0.1:{port}/uber-search.html")
            for case, card in STORES.items():
                result = UberResult(site="ubereats", out_dir=Path())
                url = await store_url(page, card, result)
                seen[case] = (url.split("/store/")[-1] if url else None, result.advertised_delivery_fee_display)
            card = await first_open_store_card(page, frozenset({"/au/store/ooshman-belconnen/bbb"}))
            seen["skipping_ooshman"] = card["url"].split("/store/")[-1]
            for case, query in (("open", ""), ("closed", "?closed=1")):
                await page.goto(f"http://127.0.0.1:{port}/uber-store.html{query}")
                why = await store_closed_reason(page)
                await js(page, "() => document.querySelector('#menu .card').click()")
                seen[f"store_{case}"] = (
                    why,
                    await is_item_modal_open(page),
                    await uber_js(page, JS_ITEM_DIALOG_BLOCKER),
                )
    finally:
        await stagehand.close()
    return seen


@pytest.fixture(scope="module")
def seen() -> dict:
    chrome = find_chrome()
    if chrome is None:
        pytest.skip("no Chrome/Chromium found (set CHROME_PATH to run the browser tests)")
    with serve(FIXTURE) as port:
        return asyncio.run(clear_carts(chrome, port))


@pytest.mark.parametrize("case", list(CASES))
def test_leftover_cart_items_are_all_removed_without_the_model(seen, case):
    assert seen[case]["before"] == 4
    assert seen[case]["after"] == 0
    assert seen[case]["plus_clicks"] == 0
    assert seen[case]["llm"] == []


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("model_failed", ("ooshman-belconnen/bbb", "$4.99 Delivery Fee^")),  # the first restaurant, not Coles
        ("model_gave_element_id", ("ooshman-belconnen/bbb", "$4.99 Delivery Fee^")),
        ("model_gave_link_but_no_fee", ("la-casetta/ddd", "Delivery Fee: $0.99")),
        ("card_shows_no_fee", ("pizza-stop/ccc", None)),
    ],
)
def test_the_store_link_and_fee_are_read_from_the_whole_search_card(seen, case, expected):
    assert seen[case] == expected


def test_a_closed_restaurant_card_is_passed_over(seen):
    # 3rd Space shows 'Available at 11:00 AM' instead of a fee and ETA, so OOSHMAN after it is picked
    assert seen["model_failed"][0] == "ooshman-belconnen/bbb"
    assert seen["skipping_ooshman"] == "pizza-stop/ccc"  # a store found closed once opened is not picked again


def test_a_closed_store_page_and_its_schedule_only_item_popup_are_recognised(seen):
    assert seen["store_open"] == (None, True, None)  # a menu item's own hours do not make the store closed
    assert seen["store_closed"] == ("Closed • Available at 11:00 AM", False, "Schedule order")
