"""Ticketek's ticket quantity and 'Next' steps in a real browser, against a local copy of the selection page."""

import asyncio
from pathlib import Path

import pytest

from audit import safety
from audit.page import js
from audit.workflows.ticketek.flow import JS_CONTINUE_SELECTION, set_quantity

from .browser_support import find_chrome, launch_args, serve

FIXTURE = Path(__file__).parent / "fixtures" / "pages"
pytestmark = pytest.mark.browser
CASES = {"stepper": "", "child_row_first": "?child=1", "dropdown": "?select=1"}


async def select_tickets(chrome: str, port: int) -> dict:
    from stagehand import Stagehand, local_browser

    seen: dict = {}
    browser = await local_browser.launch(headless=True, executable_path=chrome, args=launch_args())
    stagehand = await Stagehand.create(browser=browser)
    try:
        page = (await browser.context.pages())[0]
        await safety.install(browser.context, page)
        with safety.active_guard():
            for case, query in CASES.items():
                await page.goto(f"http://127.0.0.1:{port}/ticketek-selection.html{query}")
                no_next = await js(page, JS_CONTINUE_SELECTION)  # quantity 0: only the resale 'Find tickets'
                read_before = await set_quantity(page, "Admit", None)
                state = await set_quantity(page, "Admit", 2)
                await asyncio.sleep(0.2)
                clicked = await js(page, JS_CONTINUE_SELECTION)
                seen[case] = {
                    "no_next": no_next,
                    "read_before": read_before,
                    "state": state,
                    "read_after": await set_quantity(page, "Admit", None),
                    "clicked": clicked,
                    "clicks": await js(page, "() => window.clicks", default=[]),
                }
    finally:
        await stagehand.close()
        await browser.close()
    return seen


@pytest.fixture(scope="module")
def seen() -> dict:
    chrome = find_chrome()
    if not chrome:
        pytest.skip("no Chrome/Chromium found")
    with serve(FIXTURE) as port:
        return asyncio.run(select_tickets(chrome, port))


@pytest.mark.parametrize("case", CASES)
def test_the_admit_quantity_is_set_and_next_clicked_without_the_model(seen, case):
    got = seen[case]
    assert got["no_next"] is None  # never the resale 'Find tickets' while nothing is chosen
    assert got["read_before"]["after"] == 0 and got["read_before"]["row"].startswith("Admit")
    assert (got["state"]["before"], got["state"]["after"]) == (0, 2)
    assert got["read_after"]["after"] == 2
    assert got["clicked"] == "Next"
    banned = {"resale-link", "resale-button", "afterpay", "change", "companion", "back"}
    assert not banned & set(got["clicks"])
    assert got["clicks"][-1] == "next"
