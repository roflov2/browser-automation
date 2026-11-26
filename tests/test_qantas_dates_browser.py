"""set_depart_date against a stand-in for qantas.com's travel-dates modal: pick the day, Continue, check the form."""

import asyncio
from datetime import date
from pathlib import Path

import pytest

from audit.workflows import qantas

from .browser_support import find_chrome, launch_args, serve

FIXTURES = Path(__file__).parent / "fixtures" / "qantas"


async def _session(chrome: str, port: int, steps):
    from stagehand import Stagehand, local_browser

    browser = await local_browser.launch(
        headless=True, executable_path=chrome, args=launch_args(), viewport_width=1280, viewport_height=800
    )
    stagehand = await Stagehand.create(browser=browser)
    try:
        page = (await browser.context.pages())[0]
        await page.goto(f"http://127.0.0.1:{port}/dates.html")
        return await steps(page)
    finally:
        await stagehand.close()
        await browser.close()


def _run(steps):
    chrome = find_chrome()
    if chrome is None:
        pytest.skip("no Chrome/Chromium found (set CHROME_PATH to run the browser tests)")
    with serve(FIXTURES) as port:
        return asyncio.run(_session(chrome, port, steps))


@pytest.mark.browser
def test_the_date_is_picked_and_confirmed_with_continue():
    async def steps(page):
        before = await qantas.page_depart_date(page)
        done = await qantas.set_depart_date(page, date(2027, 2, 14))
        return before, done, await qantas.page_depart_date(page), await qantas.date_dialog(page)

    before, done, after, dialog = _run(steps)
    assert before == "29 Nov 2026"
    assert done is True
    assert after == "14 Feb 2027"
    assert dialog is None  # Continue closed the picker


@pytest.mark.browser
def test_an_open_picker_is_finished_and_a_set_date_is_left_alone():
    async def steps(page):
        field = await qantas.read_date_field(page)
        await qantas.open_date_dialog(page, field)
        opened = await qantas.date_dialog(page)
        read_while_open = await qantas.page_depart_date(page)  # the form's date, not a button of the picker
        done = await qantas.set_depart_date(page, date(2027, 3, 2))  # carries on in the open picker
        again = await qantas.set_depart_date(page, date(2027, 3, 2))
        return opened, read_while_open, done, again, await qantas.page_depart_date(page)

    opened, read_while_open, done, again, after = _run(steps)
    assert opened == {"picked": "Depart BNE Select a date"}
    assert read_while_open == "29 Nov 2026"
    assert (done, again, after) == (True, True, "2 Mar 2027")


@pytest.mark.browser
def test_a_disabled_or_missing_day_is_left_to_the_model():
    async def steps(page):
        return await qantas.set_depart_date(page, date(2027, 1, 5)), await qantas.set_depart_date(
            page, date(2027, 6, 1)
        )

    assert _run(steps) == (False, False)
