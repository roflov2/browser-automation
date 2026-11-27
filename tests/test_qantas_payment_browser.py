"""open_card_payment against a stand-in for the Qantas Payment page: only the footer's PAY WITH CARD is clicked, and
only once every guard holds; where it goes (a popup, the same tab, an overlay, or a refusal on the page) is
detected; the card window is read by the model and nothing on it, or on the payment page, is typed into."""

import asyncio
from pathlib import Path

import pytest

from audit.core import SiteResult
from audit.runner import SessionLog, workflow_records
from audit.workflows import qantas

from .browser_support import StubModel, find_chrome, launch_args, serve

FIXTURES = Path(__file__).parent / "fixtures" / "qantas"
pytestmark = pytest.mark.browser

SCREEN = {
    "heading": "Secure card payment",
    "amount_due": "$529.00",
    "currency": "AUD",
    "card_fees": [{"label": "Card fee", "amount": "1.2%"}],
    "fields_required": ["Card number", "Expiry (MM/YY)", "Security code (CVV)"],
    "messages": [],
    "blocked_reason": None,
}
# Anything that could put text into a field or submit a form.
TYPING = {"type", "key_press", "fill", "set_input_files", "select_option", "drag_and_drop", "goto", "reload"}


def checkout(total: str | None = "$529.00") -> qantas.CheckoutDetails:
    return qantas.CheckoutDetails(
        is_checkout_page=True, blocked_reason=None, items=[], subtotal=total, extras=[], other_fees=[],
        payment_surcharges=[], tax_amount=None, total=total, currency="AUD",
    )  # fmt: skip


class Spy:
    """Records every method called on the wrapped object (a Stagehand, page or locator), and passes it through.
    Locators and pages it hands back are spied on too."""

    def __init__(self, inner, calls: list[str], prefix: str = "") -> None:
        self._inner, self._calls, self._prefix = inner, calls, prefix

    def __getattr__(self, name):
        attribute = getattr(self._inner, name)
        if not callable(attribute):
            return attribute
        calls, prefix = self._calls, self._prefix

        def wrap(value, label):
            return Spy(value, calls, f"{label}.") if type(value).__name__ in ("Locator",) else value

        if asyncio.iscoroutinefunction(attribute):

            async def spied_async(*args, **kwargs):
                calls.append(prefix + name)
                return wrap(await attribute(*args, **kwargs), name)

            return spied_async

        def spied(*args, **kwargs):
            calls.append(prefix + name)
            return wrap(attribute(*args, **kwargs), name)

        return spied


async def _session(chrome, port, steps, query, stub):
    from stagehand import Stagehand, local_browser

    browser = await local_browser.launch(
        headless=True, executable_path=chrome, args=launch_args(), viewport_width=1280, viewport_height=800
    )
    stagehand = await Stagehand.create(browser=browser, model=stub)
    try:
        page = (await browser.context.pages())[0]
        await page.goto(f"http://127.0.0.1:{port}/payment.html{query}")
        return await steps(stagehand, page)
    finally:
        await stagehand.close()
        await browser.close()


def _run(monkeypatch, steps, query: str = "", stub: StubModel | None = None):
    chrome = find_chrome()
    if chrome is None:
        pytest.skip("no Chrome/Chromium found (set CHROME_PATH to run the browser tests)")
    monkeypatch.setattr(qantas, "PAY_WINDOW_WAIT_MS", 5000)
    monkeypatch.setattr(qantas, "PAY_WINDOW_SETTLE_MS", 1000)
    stub = stub or StubModel({"CardWindowScreen": SCREEN})
    with serve(FIXTURES) as port:
        monkeypatch.setattr(qantas, "PAYMENT_HOST", f"127.0.0.1:{port}")
        return asyncio.run(_session(chrome, port, steps, query, stub))


async def _values(page) -> list[str]:
    return await page.evaluate(
        "Array.from(document.querySelectorAll('input:not([type=radio]):not([type=range])')).map(i => i.value)"
    )


def _open(monkeypatch, tmp_path, query: str = "", total: str | None = "$529.00"):
    """open_card_payment on payment.html{query}, through spies. Returns what the test checks."""
    stub = StubModel({"CardWindowScreen": SCREEN})
    calls: list[str] = []

    async def steps(stagehand, page):
        result = SiteResult(site="s", out_dir=tmp_path)
        card = await qantas.open_card_payment(Spy(stagehand, calls, "stagehand."), Spy(page, calls, "page."),
                                              result, checkout(total))  # fmt: skip
        tabs = await stagehand.browser.context.pages()
        values = {}
        for tab in tabs:
            values[await tab.url()] = await _values(tab)
        clicks = await page.evaluate("window.payClicks === undefined ? null : window.payClicks")
        heading = await page.evaluate("document.querySelector('h1').textContent")
        titles = [await tab.evaluate("document.title") for tab in tabs]
        return {"card": card, "result": result, "values": values, "clicks": clicks, "heading": heading,
                "titles": titles, "tabs": len(tabs)}  # fmt: skip

    session = SessionLog()
    with workflow_records(session):
        seen = _run(monkeypatch, steps, query, stub)
    seen["calls"], seen["stub"] = calls, stub
    seen["records"] = {r["data"]["stage"]: r["data"] for r in session.history
                       if r["data"].get("click") == "pay_with_card"}  # fmt: skip
    return seen


def _nothing_typed(seen):
    typed = [c for c in seen["calls"] if c.split(".")[-1] in TYPING]
    assert typed == []
    assert not any(c.endswith((".act", ".observe")) for c in seen["calls"])
    assert all(value == "" for values in seen["values"].values() for value in values)  # no card field holds a value
    assert "SUBMITTED" not in seen["titles"]  # the card window's own Pay button was never pressed


def test_the_finder_marks_only_the_footer_pay_with_card(monkeypatch):
    async def steps(stagehand, page):
        found = await qantas.find_pay_with_card(page)
        marked = await page.evaluate(
            "Array.from(document.querySelectorAll('[data-audit-pay-card]')).map(e => e.id || e.textContent)"
        )
        reading = await qantas.read_card_state(page)
        return found, marked, reading

    found, marked, reading = _run(monkeypatch, steps)
    assert found["text"] == "PAY WITH CARD >" and found["hit_is_target"] is True and found["pinned"] is True
    assert found["matches"] == 1  # the skip links' "Pay with card" never counts
    assert marked == ["pay"]
    assert reading["heading"] == "Payment"
    assert reading["card_method"] == {"text": "Card", "selected": True}
    assert {i["kind"] for i in reading["card_inputs"]} == {"card_number", "name_on_card", "expiry"}
    assert qantas.card_fields_state(reading) == "empty"


@pytest.mark.parametrize(
    ("mode", "kind"), [("popup", "new_tab"), ("navigate", "same_tab"), ("overlay", "overlay")]
)  # fmt: skip
def test_pay_with_card_is_clicked_once_and_where_it_went_is_detected(monkeypatch, tmp_path, mode, kind):
    seen = _open(monkeypatch, tmp_path, f"?mode={mode}")
    card = seen["card"]
    assert (card.status, card.kind) == ("reached", kind), card.reason
    assert card.card_fields == "empty"
    assert (tmp_path / "pay-window.png").is_file()
    if kind == "overlay":
        assert seen["records"]["detected"]["overlays"][0]["tag"] in ("div", "iframe")
        assert card.url.endswith("payment.html?mode=overlay")
    else:
        assert card.url.endswith("/pay-cart.html")
    # The model read the card window (the popup's or the navigated tab's own page), and only read it.
    prompts = seen["stub"].prompts
    assert prompts and all("Read this card payment screen" in p for p in prompts)  # one extraction, no observe/act
    assert "Amount due $529.00" in prompts[0] and "Card fee 1.2%" in prompts[0]  # an overlay's iframe is read too
    assert (card.amount_due, [f.amount for f in card.card_fees]) == ("$529.00", ["1.2%"])
    assert seen["tabs"] == (2 if kind == "new_tab" else 1)  # the popup is left open
    if kind != "same_tab":
        assert seen["clicks"] == 1 and seen["heading"] == "Payment"  # no other control was clicked
    _nothing_typed(seen)
    # The session record shows the guards, the click and what was detected.
    records = seen["records"]
    assert all(g["ok"] for g in records["guards"]["guards"])
    assert records["click"]["how"] == "mouse"
    assert records["detected"]["kind"] == kind
    assert records["outcome"]["card_window"]["status"] == "reached"
    assert seen["result"].card_window["kind"] == kind and seen["result"].warnings == []
    # A card fee on the card window is drip-pricing evidence.
    report = qantas.compare_prices(seen["result"], checkout())
    assert "card payment window shows a card fee: Card fee 1.2%" in report["drip_reasons"]
    assert report["card_window_amount_due"] == "$529.00" and report["card_window_vs_checkout_diff"] == 0


def test_a_card_number_already_entered_stops_the_click(monkeypatch, tmp_path):
    seen = _open(monkeypatch, tmp_path, "?mode=popup&prefill=1")
    card = seen["card"]
    assert (card.status, card.card_fields, card.reason) == ("skipped", "filled", "card details already entered")
    assert seen["clicks"] == 0 and seen["tabs"] == 1
    assert "found" not in seen["records"] and seen["stub"].prompts == []
    assert seen["result"].warnings == ["PAY WITH CARD: skipped – card details already entered"]


def test_card_is_selected_in_code_when_another_method_is(monkeypatch, tmp_path):
    seen = _open(monkeypatch, tmp_path, "?mode=popup&method=applepay")
    assert seen["records"]["select_card"]["method"]["text"] == "Card"
    assert seen["card"].status == "reached" and seen["card"].kind == "new_tab"
    _nothing_typed(seen)


def test_field_errors_after_the_click_are_recorded_as_not_reached(monkeypatch, tmp_path):
    seen = _open(monkeypatch, tmp_path, "?mode=refuse")
    card = seen["card"]
    assert (card.status, card.kind, card.reason) == ("not_reached", "none", "card details required")
    assert card.messages == ["Card number is required"]
    assert seen["clicks"] == 1  # clicked once, never again
    assert (tmp_path / "pay-refused.png").is_file() and not (tmp_path / "pay-window.png").exists()
    assert seen["result"].warnings == ["PAY WITH CARD: not reached – card details required"]
    _nothing_typed(seen)


def test_off_the_payment_host_nothing_is_clicked(monkeypatch, tmp_path):
    chrome = find_chrome()
    if chrome is None:
        pytest.skip("no Chrome/Chromium found (set CHROME_PATH to run the browser tests)")
    monkeypatch.setattr(qantas, "PAY_WINDOW_WAIT_MS", 1000)  # the fixture page is not on book.qantas.com

    async def steps(stagehand, page):
        result = SiteResult(site="s", out_dir=tmp_path)
        card = await qantas.open_card_payment(stagehand, page, result, checkout())
        return card, await page.evaluate("window.payClicks")

    with serve(FIXTURES) as port:
        card, clicks = asyncio.run(_session(chrome, port, steps, "?mode=popup", StubModel()))
    assert card.status == "skipped" and card.reason.startswith("not on the payment page")
    assert clicks == 0


def test_a_card_window_that_cannot_be_read_is_still_recorded_as_reached(monkeypatch, tmp_path):
    async def unreadable(*args, **kwargs):
        raise TimeoutError("extract timed out")

    monkeypatch.setattr(qantas, "read_card_window", unreadable)
    seen = _open(monkeypatch, tmp_path, "?mode=popup")
    card = seen["card"]
    assert (card.status, card.kind) == ("reached", "new_tab")
    assert card.url.endswith("/pay-cart.html")
    assert card.reason == "card window not read: TimeoutError: extract timed out"
    assert seen["result"].warnings == []  # reached: the window was found, so no "not reached" warning
    _nothing_typed(seen)
