"""Uber Eats token savings in a real browser: the steps the page can do itself, and extractions limited to one dialog."""

import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel

from audit.runner import RecordingStagehand, SessionLog
from audit.workflows.ubereats.models import MenuItem
from audit.workflows.ubereats.store import (
    JS_MARK_CART_DRAWER,
    JS_MARK_ITEM_MODAL,
    confirm_address_step,
    first_open_store_card,
    js,
    open_item_modal,
    scoped_extract,
)

from .browser_support import StubModel, find_chrome, launch_args, serve

FIXTURE = Path(__file__).parent / "fixtures" / "pages"
pytestmark = pytest.mark.browser
FILLER = "Filler dish number 42"  # a menu card far from the item that is opened


class Modal(BaseModel):
    button_price_display: str | None


class Cart(BaseModel):
    subtotal: str | None


class NoModel:
    """Stagehand stand-in for steps that must not need the model: every call is recorded and fails."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def observe(self, instruction: str, **_: object):
        self.calls.append(instruction)
        raise RuntimeError("no model in this test")

    async def extract(self, instruction: str, *_: object, **__: object):
        self.calls.append(instruction)
        raise RuntimeError("no model in this test")


async def run_steps(chrome: str, port: int) -> dict:
    from stagehand import Stagehand, local_browser

    seen: dict = {}
    stub = StubModel({"Modal": {"button_price_display": "$10.00"}, "Cart": {"subtotal": "$22.40"}})
    browser = await local_browser.launch(headless=True, executable_path=chrome, args=launch_args())
    client = await Stagehand.create(browser=browser, model=stub)
    base = f"http://127.0.0.1:{port}"
    try:
        page = (await browser.context.pages())[0]
        recording = RecordingStagehand(client, SessionLog(), page)
        await page.goto(f"{base}/uber-store.html")
        no_model = NoModel()
        seen["address_confirmed"] = await confirm_address_step(
            no_model,  # type: ignore[arg-type]
            page,
            "1 Jeffcott Place, Canberra ACT",
            await page.url(),
            1,
        )
        item = MenuItem(name="Super Supreme", category=None, price="$10.00", sold_out=False, is_food_or_drink=True)
        seen["modal_opened"] = await open_item_modal(no_model, page, item, 0)  # type: ignore[arg-type]
        seen["opened_item"] = await js(page, "() => window.openedItem")
        seen["quick_adds"] = await js(page, "() => window.quickAdds")
        seen["no_model_calls"] = no_model.calls

        calls = len(stub.prompts)
        await recording.extract("Read the Add button price.", Modal)
        seen["full_modal_prompt"] = stub.prompts[calls]
        calls = len(stub.prompts)
        data = await scoped_extract(
            recording, page, JS_MARK_ITEM_MODAL, "Read the Add button price.", Modal, "Only the modal is shown."
        )
        seen["modal_price"] = data.button_price_display
        seen["scoped_modal_prompt"] = stub.prompts[calls]  # the extraction (a metadata call follows)

        for case, query in (("drawer_only", ""), ("upsell_beside_drawer", "?upsell=1")):
            await page.goto(f"{base}/uber-store.html{query}")
            await js(page, "() => document.getElementById('cart').click()")
            seen[case] = await js(page, JS_MARK_CART_DRAWER, default=False)
            calls = len(stub.prompts)
            await scoped_extract(recording, page, JS_MARK_CART_DRAWER, "Read the cart.", Cart, "Only the cart.")
            seen[f"{case}_prompt"] = stub.prompts[calls]

        await page.goto(f"{base}/uber-search.html")
        seen["store_card"] = await first_open_store_card(page)
    finally:
        await client.close()
    return seen


@pytest.fixture(scope="module")
def seen() -> dict:
    chrome = find_chrome()
    if chrome is None:
        pytest.skip("no Chrome/Chromium found (set CHROME_PATH to run the browser tests)")
    with serve(FIXTURE) as port:
        return asyncio.run(run_steps(chrome, port))


def test_the_header_address_and_the_item_card_need_no_model(seen):
    assert seen["address_confirmed"] is True
    assert seen["modal_opened"] is True
    assert seen["opened_item"] == "Super Supreme"
    assert seen["quick_adds"] == 0  # the card was clicked, never its '+' quick-add button
    assert seen["no_model_calls"] == []


def test_the_item_modal_is_read_without_the_menu_behind_it(seen):
    assert seen["modal_price"] == "$10.00"
    assert "Add 1 to order" in seen["scoped_modal_prompt"]
    assert FILLER in seen["full_modal_prompt"] and FILLER not in seen["scoped_modal_prompt"]
    assert len(seen["scoped_modal_prompt"]) * 5 < len(seen["full_modal_prompt"])


def test_the_cart_drawer_is_read_alone_unless_a_popup_is_open_beside_it(seen):
    assert seen["drawer_only"] is True
    assert "Go to checkout" in seen["drawer_only_prompt"] and FILLER not in seen["drawer_only_prompt"]
    assert seen["upsell_beside_drawer"] is False  # the popup must be seen, so the whole page is read
    assert "Complete your meal" in seen["upsell_beside_drawer_prompt"] and FILLER in seen["upsell_beside_drawer_prompt"]


def test_the_first_open_restaurant_card_is_picked_from_the_page(seen):
    card = seen["store_card"]
    assert card["url"].endswith("/store/ooshman-belconnen/bbb")  # not Coles, which comes first
    assert card["deliveryFee"] == "$4.99 Delivery Fee^"
