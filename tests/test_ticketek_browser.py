"""Ticketek's ticket quantity and 'Next' steps in a real browser, against a local copy of the selection page."""

import asyncio
from pathlib import Path

import pytest

from audit.page import js
from audit.workflows.ticketek.flow import (
    JS_CONTINUE_SELECTION,
    acknowledge_view_notice,
    continue_checkout,
    read_quantity,
    set_quantity,
)

from .browser_support import find_chrome, launch_args, serve

FIXTURE = Path(__file__).parent / "fixtures" / "pages"
pytestmark = pytest.mark.browser
CASES = {"stepper": "", "icon_divs": "?divs=1", "child_row_first": "?child=1", "dropdown": "?select=1"}


async def select_tickets(chrome: str, port: int) -> dict:
    from stagehand import Stagehand, local_browser

    seen: dict = {}
    browser = await local_browser.launch(headless=True, executable_path=chrome, args=launch_args())
    stagehand = await Stagehand.create(browser=browser)
    try:
        page = (await browser.context.pages())[0]
        for case, query in CASES.items():
            await page.goto(f"http://127.0.0.1:{port}/ticketek-selection.html{query}")
            no_next = await js(page, JS_CONTINUE_SELECTION)  # quantity 0: only the resale 'Find tickets'
            read_before = await read_quantity(page, "Admit")
            state = await set_quantity(page, "Admit", 2)
            clicked = await js(page, JS_CONTINUE_SELECTION)
            seen[case] = {
                "no_next": no_next,
                "read_before": read_before,
                "state": state,
                "read_after": await read_quantity(page, "Admit"),
                "trusted": await js(page, "() => window.trusted", default=0),
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
    assert got["read_before"]["value"] == 0 and got["read_before"]["row"].startswith("Admit")
    assert (got["state"]["before"], got["state"]["after"]) == (0, 2)
    assert got["read_after"]["value"] == 2 and not got["read_after"]["busy"]
    assert got["clicked"] == "Next"
    banned = {"resale-link", "resale-button", "afterpay", "change", "companion", "back"}
    assert not banned & set(got["clicks"])
    assert got["clicks"][-1] == "next"


def test_each_plus_is_one_real_mouse_click_after_the_page_has_caught_up(seen):
    for case in ("stepper", "icon_divs", "child_row_first"):
        pluses = [c for c in seen[case]["clicks"] if c.startswith("plus-")]
        assert pluses == ["plus-Admit", "plus-Admit"]  # never a second click while the first is processing
        assert seen[case]["trusted"] >= 2  # real mouse input, not el.click()


async def pick_dates(chrome: str, port: int) -> dict:
    from stagehand import Stagehand, local_browser

    from audit.workflows.ticketek.flow import choose_date, dropdown_sessions, not_enough_tickets
    from audit.workflows.ticketek.models import Session

    seen: dict = {}
    browser = await local_browser.launch(headless=True, executable_path=chrome, args=launch_args())
    stagehand = await Stagehand.create(browser=browser)
    try:
        page = (await browser.context.pages())[0]
        for wanted in ["Wed 02 Dec 2026, 7:00 pm", "Sat 14 Mar 2027"]:
            await page.goto(f"http://127.0.0.1:{port}/ticketek-event.html")
            session = Session(date=wanted, venue=None, status="on_sale", from_price=None)
            seen[wanted] = {
                "clicked": await choose_date(page, session),
                "again": await choose_date(page, session),  # already chosen: the dropdown is left alone
                "chosen": await js(page, "() => window.chosen", default=[]),
                "found": await js(page, "() => window.found"),
                "resale": await js(page, "() => window.resale"),
                "listed": await dropdown_sessions(page),
                "not_enough": await not_enough_tickets(page),
            }
        await page.goto(f"http://127.0.0.1:{port}/ticketek-event.html?soldout=1")
        seen["sold_out"] = {"not_enough": await not_enough_tickets(page), "listed": await dropdown_sessions(page)}
    finally:
        await stagehand.close()
        await browser.close()
    return seen


@pytest.fixture(scope="module")
def dates() -> dict:
    chrome = find_chrome()
    if not chrome:
        pytest.skip("no Chrome/Chromium found")
    with serve(FIXTURE) as port:
        return asyncio.run(pick_dates(chrome, port))


def test_the_session_date_is_chosen_from_the_dropdown_and_find_tickets_clicked(dates):
    got = dates["Wed 02 Dec 2026, 7:00 pm"]
    assert got["clicked"] and got["again"]
    assert got["chosen"] == ["s2"]
    assert got["found"] == "s2"
    assert got["resale"] is None


def test_an_unmatched_date_falls_back_to_the_first_date(dates):
    got = dates["Sat 14 Mar 2027"]
    assert got["clicked"] and got["chosen"] == ["s1"] and got["found"] == "s1"
    assert got["resale"] is None


def test_the_dropdown_dates_are_listed_with_the_one_chosen(dates):
    listed, shown = dates["Wed 02 Dec 2026, 7:00 pm"]["listed"]
    assert [s.date for s in listed] == ["Tue 01 Dec 2026 7:00PM", "Wed 02 Dec 2026 7:00PM"]
    assert shown.date == "Wed 02 Dec 2026 7:00PM"
    assert dates["sold_out"]["listed"][1] is None  # nothing chosen yet: '---- Select date ----'


def test_the_not_enough_tickets_message_is_read_only_when_shown(dates):
    assert dates["Wed 02 Dec 2026, 7:00 pm"]["not_enough"] is None
    assert dates["sold_out"]["not_enough"] == ("There aren't enough tickets to meet your request right now.")


async def restricted_view(chrome: str, port: int) -> dict:
    from stagehand import Stagehand, local_browser

    browser = await local_browser.launch(headless=True, executable_path=chrome, args=launch_args())
    stagehand = await Stagehand.create(browser=browser)
    try:
        page = (await browser.context.pages())[0]
        await page.goto(f"http://127.0.0.1:{port}/ticketek-restricted-view.html")
        before = await continue_checkout(page)  # 'Go to checkout' is disabled until the box is ticked
        ticked = await acknowledge_view_notice(page)
        again = await acknowledge_view_notice(page)  # already ticked: left alone
        after = await continue_checkout(page)
        return {"before": before, "ticked": ticked, "again": again, "after": after,
                "clicks": await js(page, "() => window.clicks", default=[])}  # fmt: skip
    finally:
        await stagehand.close()
        await browser.close()


def test_the_restricted_view_notice_is_ticked_and_checkout_continues():
    chrome = find_chrome()
    if not chrome:
        pytest.skip("no Chrome/Chromium found")
    with serve(FIXTURE) as port:
        got = asyncio.run(restricted_view(chrome, port))
    assert got["before"] is None
    assert len(got["ticked"]) == 1 and "restricted view" in got["ticked"][0]
    assert got["again"] == []
    assert got["after"] == "Go to checkout"
    assert got["clicks"] == ["ack:true", "go"]  # never the refund protection box
