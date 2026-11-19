"""Clearing leftover Uber Eats cart items in a real browser, against a local copy of the cart drawer."""

import asyncio
from pathlib import Path

import pytest

from audit import safety
from audit.page import js
from audit.workflows.ubereats.store import clear_existing_cart, get_cart_count

from .browser_support import find_chrome, launch_args, serve

FIXTURE = Path(__file__).parent / "fixtures" / "pages"
pytestmark = pytest.mark.browser
CASES = {"icons": "", "labels": "?labels=1", "confirm": "?confirm=1"}


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
