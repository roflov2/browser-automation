"""Ticketek workflow logic that needs neither a browser nor a model."""

import argparse
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from audit.config import Settings
from audit.core import Blocked, Fee, drip_verdict, new_item
from audit.workflows.ticketek import TicketekWorkflow, flow, pricing
from audit.workflows.ticketek.models import (
    CheckoutDetails,
    PriceOption,
    Session,
    StepState,
    Surcharge,
    TicketekResult,
    TicketLine,
)


def session(date, status="on_sale", venue="Her Majesty's Theatre, Melbourne"):
    return Session(date=date, venue=venue, status=status, from_price=None)


def option(price_type, price, area="A Reserve", available=True):
    return PriceOption(area=area, price_type=price_type, price=price, available=available, price_note=None)


# --- choosing what to buy --------------------------------------------------------------------------------
def test_choose_session_takes_the_first_bookable_one_or_the_one_asked_for():
    sessions = [session("Fri 12 Mar 7:30pm", "sold_out"), session("Sat 13 Mar 2:00pm"),
                session("Sat 13 Mar 7:30pm", "selling_fast"), session("Sun 14 Mar 1:00pm", "not_on_sale_yet")]  # fmt: skip
    assert flow.choose_session(sessions, None).date == "Sat 13 Mar 2:00pm"
    assert flow.choose_session(sessions, "sat 7:30pm").date == "Sat 13 Mar 7:30pm"
    assert flow.choose_session(sessions, "Sun 14") is None  # not on sale yet
    assert flow.choose_session(sessions, "Sydney") is None
    assert flow.choose_session([session("x", "sold_out")], None) is None


def test_choose_option_prefers_the_cheapest_standard_adult_ticket():
    options = [option("Child", "$49.90"), option("Concession", "$59.90"), option("Adult", "$129.90"),
               option("Adult", "$89.90", area="C Reserve"), option("Adult", "$69.90", area="D Reserve", available=False),
               option("Companion Card", "$0.00"), option("Adult", None, area="Premium")]  # fmt: skip
    chosen = flow.choose_option(options)
    assert (chosen.area, chosen.price) == ("C Reserve", "$89.90")
    assert flow.cheapest_offered(options) == 0.0  # any available type counts for the 'from' price check
    assert flow.choose_option([option("Student", "$40")]).price_type == "Student"  # nothing standard: cheapest
    assert flow.choose_option([option("Adult", "$40", available=False)]) is None


def test_a_tier_with_no_ticket_type_reads_and_counts_as_standard():
    shown = {"area": "A Reserve", "price": "$129", "available": True, "price_note": None}
    assert PriceOption.model_validate({**shown, "price_type": None}).price_type is None
    assert PriceOption.model_validate(shown).price_type is None  # key left out entirely
    untyped = option(None, "$129")
    assert flow.choose_option([untyped, option("Student", "$40")]) is untyped  # untyped beats a restricted type
    assert flow.choose_option([untyped]) is untyped
    assert flow.option_name(untyped) == "A Reserve"


def test_a_session_or_surcharge_may_leave_out_what_the_page_does_not_show():
    no_status = Session.model_validate({"date": "Fri 5 Mar 2027", "venue": None, "from_price": None})
    assert no_status.status is None
    assert flow.choose_session([no_status], None) is None  # unknown status is not bookable
    assert Surcharge.model_validate({"payment_method": "Amex", "fee": None}).fee is None


def test_a_surcharge_with_no_fee_shown_is_recorded_but_not_charged(tmp_path):
    run = result(tmp_path)
    pricing.record_surcharges(run, [Surcharge(payment_method="Amex", fee=None)])
    assert [(e["label"], e["price_display"], e["price"]) for e in run.extras] == [("Amex", None, None)]
    report = pricing.compare_prices(run, checkout(payment_surcharges=[Surcharge(payment_method="Amex")]))
    assert not any("payment surcharge" in r for r in report["drip_reasons"])


# --- price comparison --------------------------------------------------------------------------------------
def held(price="$89.90", quantity=2):
    item = new_item("C Reserve - Adult", price, quantity=quantity)
    item.update(selected_price=89.90, selected_price_display=price, status="added")
    return item


def checkout(**overrides):
    fields = {
        "is_checkout_page": True,
        "blocked_reason": None,
        "items": [TicketLine(name="C Reserve - Adult", variant="Adult", quantity=2, price="$179.80")],
        "ticket_subtotal": "$179.80",
        "booking_fees": [],
        "service_and_handling_fees": [Fee(label="Service & handling fee", amount="$8.95")],
        "delivery_fee": Fee(label="Mobile ticket", amount="Free"),
        "extras": [],
        "payment_surcharges": [Surcharge(payment_method="Visa/Mastercard", fee="No fee")],
        "total": "$188.75",
        "currency": "AUD",
        "summary": "payment page",
    }
    return CheckoutDetails(**(fields | overrides))


def result(tmp_path, **fields) -> TicketekResult:
    defaults = {"event_name": "Hamilton", "session": "Sat 13 Mar 2:00pm", "advertised_from_price_display": "From $89.90",
                "cheapest_offered": 49.90}  # fmt: skip
    run = TicketekResult(site="https://premier.ticketek.com.au/", out_dir=tmp_path, **(defaults | fields))
    run.items.append(held())
    return run


def test_handling_fee_is_drip_pricing(tmp_path):
    run = result(tmp_path)
    report = pricing.compare_prices(run, checkout())
    assert report["expected_subtotal"] == 179.80  # 2 x $89.90
    assert report["checkout_subtotal"] == 179.80
    assert run.items[0]["price_increased"] is False
    assert report["other_fees_total"] == 8.95
    assert report["shipping"] == 0.0
    assert report["fees_pct_of_tickets"] == 5.0
    assert report["total_vs_expected_diff"] == 8.95
    assert report["drip_detected"] is True and report["final_price_read"] is True
    assert report["drip_reasons"] == [
        "total at checkout higher than the ticket price with no extras chosen",
        "booking, service or handling fees added",
    ]
    assert drip_verdict(report).startswith("YES")


def test_no_fees_is_no_drip(tmp_path):
    report = pricing.compare_prices(result(tmp_path), checkout(service_and_handling_fees=[], total="$179.80"))
    assert report["drip_detected"] is False
    assert report["fees_pct_of_tickets"] == 0.0


def test_delivery_fee_surcharge_and_bait_from_price(tmp_path):
    run = result(tmp_path, advertised_from_price_display="From $39.90")  # every ticket costs more
    report = pricing.compare_prices(
        run,
        checkout(
            service_and_handling_fees=[],
            delivery_fee=Fee(label="Express post", amount="$6.50"),
            payment_surcharges=[Surcharge(payment_method="Amex", fee="1.9%")],
            total="$186.30",
        ),
    )
    reasons = " | ".join(report["drip_reasons"])
    assert "delivery fee charged (Express post)" in reasons
    assert "payment surcharge applies: Amex 1.9%" in reasons
    assert "advertised 'from' price From $39.90 is below every ticket offered" in reasons
    assert report["other_fees_total"] == 0.0 and report["shipping"] == 6.5


def test_without_the_payment_page_the_verdict_is_unknown(tmp_path):
    run = result(tmp_path)
    run.steps.append({"total": 179.80})
    report = pricing.compare_prices(run, None)
    assert report["drip_detected"] is None
    assert report["checkout_total"] == 179.80  # last running total seen
    assert run.items[0]["price_increased"] is None


def test_payment_page_surcharges_replace_the_steps_own(tmp_path):
    run = result(tmp_path)
    run.extras.append({"step_type": "payment", "category": "payment_surcharge", "label": "Card fee"})
    pricing.record_surcharges(run, [Surcharge(payment_method="Amex", fee="1.9%")])
    assert [(e["label"], e["price_display"], e["price"]) for e in run.extras] == [
        ("Amex", "1.9%", None)
    ]  # a % is no amount


# --- keys and folders --------------------------------------------------------------------------------------
def test_target_key_and_output_folder(tmp_path):
    workflow = TicketekWorkflow(argparse.Namespace(), Settings())
    assert workflow.target_key("https://premier.ticketek.com.au/", result(tmp_path)) == "Hamilton | Sat 13 Mar 2:00pm"
    empty = TicketekResult(site="s", out_dir=Path())
    assert workflow.target_key("https://premier.ticketek.com.au/", empty) == "https://premier.ticketek.com.au/"
    assert workflow.output_folder("https://premier.ticketek.com.au/") == "premier.ticketek.com.au"
    assert (
        workflow.output_folder("https://premier.ticketek.com.au/shows/show.aspx?sh=HAMILTON23")
        == "premier.ticketek.com.au-hamilton23"
    )


# --- walking to payment ------------------------------------------------------------------------------------
def step(**fields):
    defaults = {"page_type": "cart", "heading": "Your cart", "is_payment_page": False, "login_required": False,
                "in_queue": False, "blocked_reason": None, "offers": [], "price_lines": [], "total": "$188.75",
                "validation_errors": [], "summary": "s"}  # fmt: skip
    return StepState(**(defaults | fields))


@pytest.fixture
def walk(monkeypatch):
    """walk_to_payment over a scripted sequence of steps; returns (run, reached, actions)."""

    def go(states, tmp_path, interactive=False):
        states = iter(states)
        actions: list[str] = []

        class Stagehand:
            async def extract(self, instruction, schema, **options):
                return SimpleNamespace(data=next(states))

        class Page:
            async def url(self):
                return "https://premier.ticketek.com.au/cart"

        async def fake_safe_act(stagehand, page, instruction, **options):
            actions.append(instruction)
            return SimpleNamespace(description="clicked continue", success=True)

        async def nothing(*args, **kwargs):
            return None

        screens = iter(range(100))

        async def fake_fingerprint(page):
            return str(next(screens))  # every action changes the screen

        async def no_pause(page, message, interactive):
            return interactive

        frozen = []

        class Guard:
            async def freeze(self, page):
                frozen.append(True)

        monkeypatch.setattr(flow, "safe_act", fake_safe_act)
        monkeypatch.setattr(flow, "fingerprint", fake_fingerprint)
        monkeypatch.setattr(flow, "pause_for_login", no_pause)
        monkeypatch.setattr(flow, "current_guard", Guard)
        for name in ("wait_for_render", "shot", "settle"):
            monkeypatch.setattr(flow, name, nothing)
        run = TicketekResult(site="s", out_dir=tmp_path)
        reached = asyncio.run(flow.walk_to_payment(Stagehand(), Page(), run, interactive))
        return run, reached, actions, frozen

    return go


def test_walk_records_fees_and_freezes_on_payment(walk, tmp_path):
    offers = [
        {"category": "delivery_fee", "label": "Mobile ticket", "price": "Free", "price_basis": None,
         "selected": False, "mandatory": False},
        {"category": "refund_protection", "label": "Refund protection", "price": "$12.60", "price_basis": None,
         "selected": True, "mandatory": False},
    ]  # fmt: skip
    run, reached, actions, frozen = walk(
        [step(), step(page_type="delivery", heading="Delivery", offers=offers), step(is_payment_page=True)], tmp_path
    )
    assert reached is True and frozen == [True]
    assert [s["step_type"] for s in run.steps] == ["cart", "delivery", "payment"]
    assert len(actions) == 2 and "free delivery method" in actions[1]
    assert {e["label"]: e["selected_by_default"] for e in run.extras} == {
        "Mobile ticket": False,
        "Refund protection": True,
    }


def test_walk_stops_at_a_login_wall_when_nobody_can_sign_in(walk, tmp_path):
    with pytest.raises(Blocked, match="--login"):
        walk([step(page_type="login_prompt", login_required=True)], tmp_path)


def test_walk_lets_a_person_sign_in_once(walk, tmp_path):
    login = step(page_type="login_prompt", login_required=True)
    _, reached, *_ = walk([login, step(is_payment_page=True)], tmp_path, interactive=True)
    assert reached is True
    with pytest.raises(Blocked):  # still asking after the pause: give up rather than loop
        walk([login, login], tmp_path, interactive=True)


def test_walk_waits_out_a_queue(walk, tmp_path, monkeypatch):
    waited = []

    async def fake_wait(stagehand, page):
        waited.append(True)

    monkeypatch.setattr(flow, "wait_in_queue", fake_wait)
    _, reached, actions, _ = walk([step(page_type="waiting_room", in_queue=True), step(is_payment_page=True)], tmp_path)
    assert reached is True and waited == [True] and actions == []
