"""Uber Eats' 'building type' popup in a real browser: skipped or answered, never mistaken for a set address,
and never confused with an item modal or the cart."""

import asyncio
from pathlib import Path

import pytest

from audit import safety
from audit.workflows.ubereats.store import (
    JS_DISMISS_LOCATION_PROMPT,
    confirm_address_step,
    dismiss_location_prompt,
    js,
)

from .browser_support import StubModel, find_chrome, launch_args, serve

FIXTURE = Path(__file__).parent / "fixtures" / "pages"
PAGE = "ubereats_building_type.html"
ADDRESS = "1 Jeffcott Place, Canberra ACT 2615"
# base64 of {"address":"1 Jeffcott Place",...}: Uber's pl= parameter once the address is set
PL = "eyJhZGRyZXNzIjoiMSBKZWZmY290dCBQbGFjZSIsInJlZmVyZW5jZSI6IngifQ=="
pytestmark = pytest.mark.browser


class NoModel:
    """Stagehand stand-in: these steps must not need the model."""

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
    browser = await local_browser.launch(headless=True, executable_path=chrome, args=launch_args())
    client = await Stagehand.create(browser=browser, model=StubModel({}))
    base = f"http://127.0.0.1:{port}/{PAGE}"
    open_dialog = "() => Array.from(document.querySelectorAll('[role=\"dialog\"]')).map(d => d.id)"
    try:
        page = (await browser.context.pages())[0]
        await safety.install(browser.context, page)
        no_model = NoModel()

        async def confirm(page) -> bool:
            return await confirm_address_step(no_model, page, ADDRESS, await page.url(), 1)  # type: ignore[arg-type]

        with safety.active_guard():
            await page.goto(base)
            seen["skip_acted"] = await dismiss_location_prompt(page)
            seen["skip_clicks"] = await js(page, "() => window.clicks")
            seen["skip_dialogs"] = await js(page, open_dialog)

            await page.goto(f"{base}?noskip=1")
            seen["save_acted"] = await dismiss_location_prompt(page)
            seen["save_clicks"] = await js(page, "() => window.clicks")
            seen["saved"] = await js(page, "() => window.saved")
            seen["save_dialogs"] = await js(page, open_dialog)

            await page.goto(f"{base}?stuck=1&pl={PL}")
            seen["stuck_confirmed"] = await confirm(page)
            seen["stuck_dialogs"] = await js(page, open_dialog)

            await page.goto(f"{base}?pl={PL}")
            seen["open_confirmed"] = await confirm(page)
            seen["open_dialogs"] = await js(page, open_dialog)

            for case in ("item", "cart"):
                await page.goto(f"{base}?{case}=1")
                seen[f"{case}_step"] = await js(page, JS_DISMISS_LOCATION_PROMPT, {"addressConfirmed": True})
                seen[f"{case}_clicks"] = await js(page, "() => window.clicks")
                seen[f"{case}_dialogs"] = await js(page, open_dialog)

            await page.goto(f"{base}?addresses=1")
            seen["addresses_unconfirmed"] = await js(page, JS_DISMISS_LOCATION_PROMPT, {"addressConfirmed": False})
            seen["addresses_confirmed"] = await confirm(page)
            seen["addresses_dialogs"] = await js(page, open_dialog)
            seen["addresses_clicks"] = await js(page, "() => window.clicks")
        seen["model_calls"] = no_model.calls
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


def test_the_prompt_is_skipped(seen):
    assert seen["skip_acted"] is True
    assert seen["skip_clicks"] == ["Skip"]
    assert seen["skip_dialogs"] == []


def test_without_skip_a_building_type_is_chosen_then_saved(seen):
    assert seen["save_acted"] is True
    assert seen["save_clicks"][-1] == "Save"
    assert seen["saved"] == "House"
    assert seen["save_dialogs"] == []


def test_a_matching_pl_does_not_confirm_the_address_while_the_prompt_is_open(seen):
    assert seen["stuck_confirmed"] is False
    assert seen["stuck_dialogs"] == ["building"]
    assert seen["open_confirmed"] is True  # once the prompt is skipped, the same page is confirmed
    assert seen["open_dialogs"] == []
    assert seen["model_calls"] == []


def test_an_item_modal_or_the_cart_is_left_alone(seen):
    for case in ("item", "cart"):
        assert seen[f"{case}_step"] is None
        assert seen[f"{case}_clicks"] == []
        assert seen[f"{case}_dialogs"] == [case]


def test_a_leftover_addresses_dialog_is_closed_once_the_header_is_right(seen):
    assert seen["addresses_unconfirmed"] is None  # not before the address is confirmed
    assert seen["addresses_confirmed"] is True
    assert seen["addresses_dialogs"] == []
    assert seen["addresses_clicks"] == ["Close"]  # the X icon; nothing typed, nothing edited
