"""Uber Eats page steps in a real browser, against local copies of the cart drawer and the search results."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from audit import safety
from audit.page import js
from audit.workflows.ubereats.models import ItemModalState, MenuItem, StoreCard, UberResult
from audit.workflows.ubereats.store import (
    JS_ITEM_DIALOG_BLOCKER,
    add_menu_item_to_cart,
    clear_existing_cart,
    close_cart_drawer,
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
    "div_rows": "?divrows=1",
    "no_synthetic_escape": "?noesc=1",
}
ITEMS_BEFORE = {"div_rows": 2, "no_synthetic_escape": 2}  # every other case starts with 4
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


class ModalModel(NoModel):
    """Reads every item popup as having no required choice left, so the DOM steps click Add."""

    async def extract(self, instruction: str, schema: type, **_: object) -> SimpleNamespace:
        return SimpleNamespace(data=ItemModalState(
            has_unmet_required_choices=False, next_required_group=None, cheapest_option_in_required_group=None,
            button_price_display=None, paid_modifier_offers=[], reason="test",
        ))  # fmt: skip


async def clear_carts(chrome: str, port: int, out_dir: Path) -> dict:
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
                left = await clear_existing_cart(model, page, out_dir / case)  # type: ignore[arg-type]
                seen[case] = {
                    "before": before,
                    "after": await get_cart_count(page),
                    "left": left,
                    "plus_clicks": await js(page, "() => window.plusClicks"),
                    "llm": model.instructions,
                    "drawer_open": await js(page, "() => !!document.getElementById('drawer')"),
                }
            # A cart that cannot be cleared: the run folder says what was tried and what the drawer held
            await page.goto(f"http://127.0.0.1:{port}/uber-cart.html?divrows=1&stuck=1")
            seen["stuck"] = {"left": await clear_existing_cart(NoModel(), page, out_dir / "stuck")}  # type: ignore[arg-type]
            await page.goto(f"http://127.0.0.1:{port}/uber-cart.html?noesc=1")
            await js(page, "() => document.getElementById('cart').dispatchEvent(new Event('click'))")
            await js(page, "() => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))")
            seen["synthetic_escape_ignored"] = await js(page, "() => !!document.getElementById('drawer')")
            seen["closed_by_key"] = await close_cart_drawer(page)
            await page.goto(f"http://127.0.0.1:{port}/uber-search.html")
            for case, card in STORES.items():
                result = UberResult(site="ubereats", out_dir=Path())
                url = await store_url(page, card, result)
                seen[case] = (url.split("/store/")[-1] if url else None, result.advertised_delivery_fee_display)
            card = await first_open_store_card(page, frozenset({"/au/store/ooshman-belconnen/bbb"}))
            seen["skipping_ooshman"] = card["url"].split("/store/")[-1]
            await page.goto(f"http://127.0.0.1:{port}/uber-store.html?other=1")
            result = UberResult(site="ubereats", out_dir=out_dir)
            dish = MenuItem(name="Super Supreme", category=None, price="$10.00", sold_out=False, is_food_or_drink=True)
            seen["new_order"] = await add_menu_item_to_cart(ModalModel(), page, dish, result, 1)  # type: ignore[arg-type]
            seen["new_order_cart"] = await get_cart_count(page)
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
def out_dir(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("ubereats-run")


@pytest.fixture(scope="module")
def seen(out_dir) -> dict:
    chrome = find_chrome()
    if chrome is None:
        pytest.skip("no Chrome/Chromium found (set CHROME_PATH to run the browser tests)")
    with serve(FIXTURE) as port:
        return asyncio.run(clear_carts(chrome, port, out_dir))


@pytest.mark.parametrize("case", list(CASES))
def test_leftover_cart_items_are_all_removed_without_the_model(seen, out_dir, case):
    assert seen[case]["before"] == ITEMS_BEFORE.get(case, 4)
    assert seen[case]["after"] == 0
    assert seen[case]["left"] == (0, None)
    assert seen[case]["plus_clicks"] == 0
    assert seen[case]["llm"] == []
    assert not seen[case]["drawer_open"]  # the drawer is closed afterwards, so it cannot cover the menu
    assert not (out_dir / case / "cart_clear.json").exists()  # only a cart that could not be cleared is recorded


def test_a_drawer_that_ignores_a_synthetic_escape_is_closed_with_a_real_key_press(seen):
    assert seen["synthetic_escape_ignored"]
    assert seen["closed_by_key"]


def test_a_cart_that_cannot_be_cleared_is_named_and_recorded_in_the_run_folder(seen, out_dir):
    assert seen["stuck"]["left"] == (2, "OOSHMAN")  # the badge counts, even where the rows give nothing to click
    report = json.loads((out_dir / "stuck" / "cart_clear.json").read_text(encoding="utf-8"))
    assert report["items_left"] == 2
    assert report["store"] == "OOSHMAN"
    assert {"step": "stepper_button", "left": 2} in report["steps"]  # each step and the count it left
    assert "Super Supreme" in report["drawer_html"]
    assert "Super Supreme" in report["panels_text"]


def test_another_stores_cart_is_replaced_by_confirming_the_new_order_prompt(seen):
    assert seen["new_order"]["status"] == "added"
    assert seen["new_order_cart"] == 1  # this store's item, in place of the other store's


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
