"""move_booking_on against stand-ins for the Qantas Options, Review and Passenger details pages: insurance
declined, the fare rules' ACCEPT and the footer CONTINUE / CONFIRM DETAILS clicked in code (also inside the
<page-bottom-modal>), never a skip link, a cookie banner, the cart panel, an upsell or the model."""

import asyncio
from pathlib import Path

import pytest

from audit.workflows import qantas
from audit.workflows.qantas import Passenger

from .browser_support import find_chrome, launch_args, serve
from .test_workflows import OPTIONS_OFFERS, _step

FIXTURES = Path(__file__).parent / "fixtures" / "qantas"


async def _session(chrome: str, port: int, steps, fixture: str = "options.html"):
    from stagehand import Stagehand, local_browser

    browser = await local_browser.launch(
        headless=True, executable_path=chrome, args=launch_args(), viewport_width=1280, viewport_height=800
    )
    stagehand = await Stagehand.create(browser=browser)
    try:
        page = (await browser.context.pages())[0]
        await page.goto(f"http://127.0.0.1:{port}/{fixture}")
        return await steps(stagehand, page)
    finally:
        await stagehand.close()
        await browser.close()


def _run(steps, fixture: str = "options.html"):
    chrome = find_chrome()
    if chrome is None:
        pytest.skip("no Chrome/Chromium found (set CHROME_PATH to run the browser tests)")
    with serve(FIXTURES) as port:
        return asyncio.run(_session(chrome, port, steps, fixture))


@pytest.mark.browser
def test_the_options_page_moves_on_with_the_footer_continue_clicked_in_code(monkeypatch):
    async def no_model(*args, **kwargs):
        raise AssertionError("the model was asked")

    monkeypatch.setattr(qantas, "click_one", no_model)
    monkeypatch.setattr(qantas, "llm_act", no_model)

    async def steps(stagehand, page):
        footer = await qantas.find_footer_forward(page)
        accept = await qantas.read_accept(page)
        target: dict = {}
        state = _step("extras", "Options", OPTIONS_OFFERS)
        acted = await qantas.move_booking_on(stagehand, page, Passenger(), state, "extras", [0], target)
        marked = await page.evaluate("document.querySelector('[data-audit-continue]')?.id || null")
        heading = await page.evaluate("document.getElementById('heading').textContent")
        ticked = await page.evaluate("document.getElementById('no-insurance').checked")
        return footer, accept, acted, target, marked, heading, ticked

    footer, accept, acted, target, marked, heading, ticked = _run(steps)
    assert footer == {"text": "CONTINUE >"}  # the sticky footer's, not the skip link's "Continue"
    assert accept is None  # the skip link's "Accept" is no fare rules ACCEPT
    assert (acted.success, acted.description, acted.selector) == (True, "code click on CONTINUE >",
                                                                  qantas.CONTINUE_MARK)  # fmt: skip
    assert target == {"description": "CONTINUE > in the sticky footer", "selector": qantas.CONTINUE_MARK}
    assert marked == "continue"
    assert ticked is True  # insurance declined in code first
    assert heading == "Passenger details"  # no upsell was clicked


def _no_model(monkeypatch):
    async def no_model(*args, **kwargs):
        raise AssertionError("the model was asked")

    monkeypatch.setattr(qantas, "click_one", no_model)
    monkeypatch.setattr(qantas, "llm_act", no_model)


@pytest.mark.browser
def test_the_fare_rules_accept_inside_the_bottom_modal_is_clicked_in_code_not_the_cookie_accept(monkeypatch):
    _no_model(monkeypatch)

    async def steps(stagehand, page):
        accept = await qantas.read_accept(page)
        state = _step("review", "Review your trip")
        acted = await qantas.move_booking_on(stagehand, page, Passenger(), state, "review", [0], {})
        heading = await page.evaluate("document.getElementById('heading').textContent")
        return accept, acted, heading

    accept, acted, heading = _run(steps, "review.html")
    assert accept == {"text": "ACCEPT >"}  # the modal's, though it has role="dialog"; not the cookie banner's
    assert (acted.success, acted.selector) == (True, "[data-audit-accept]")
    assert heading == "Options"  # the modal's ACCEPT took the click, not the cookie Accept


@pytest.mark.browser
def test_confirm_details_inside_the_bottom_modal_is_clicked_in_code(monkeypatch):
    _no_model(monkeypatch)

    async def steps(stagehand, page):
        found = await qantas.find_footer_forward(page)
        clicked = await qantas.click_footer_forward(page)
        heading = await page.evaluate("document.getElementById('heading').textContent")
        return found, clicked, heading

    found, clicked, heading = _run(steps, "passenger.html")
    assert found == {"text": "CONFIRM DETAILS"}  # the open modal's, over the footer's CONTINUE under it
    assert clicked == "CONFIRM DETAILS"
    assert heading == "Seats"  # neither the footer, the cart panel nor EDIT was clicked
