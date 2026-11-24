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
    HeldTickets,
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


def test_a_session_with_no_status_read_may_still_be_bookable():
    unknown = [session("Fri 5 Mar 2027", None, venue="RAC Arena, Perth, WA"),
               session("Sat 6 Mar 2027", None, venue="Qudos Bank Arena, Sydney, NSW")]  # fmt: skip
    assert flow.choose_session(unknown, None) is unknown[0]
    on_sale = session("Sat 6 Mar 2027", "on_sale")
    assert flow.choose_session([unknown[0], on_sale], None) is on_sale  # an explicit status wins
    assert flow.choose_session([session("a", "sold_out"), session("b", "cancelled")], None) is None
    assert flow.choose_session(unknown, "Sydney") is unknown[1]
    no_status = Session.model_validate({"date": "Fri 5 Mar 2027", "venue": "X", "from_price": None})
    assert no_status.status is None


def test_after_a_session_runs_out_every_other_date_and_venue_is_tried_the_one_asked_for_first():
    sessions = [session("Fri 04 Jun 2027 6:30PM"), session("Sun 06 Jun 2027 4:00PM", "sold_out"),
                session("Sun 06 Jun 2027 6:30PM", None), session("Sat 05 Jun 2027 6:30PM", venue="Llewellyn Hall, Canberra"),
                session("Mon 07 Jun 2027 7:00PM", "selling_fast")]  # fmt: skip
    tried = [sessions[0]]
    assert [s.date for s in flow.other_sessions(sessions, tried, None)] == [
        "Sun 06 Jun 2027 6:30PM", "Sat 05 Jun 2027 6:30PM", "Mon 07 Jun 2027 7:00PM"]  # fmt: skip
    assert flow.other_sessions(sessions, tried, "Canberra")[0].venue == "Llewellyn Hall, Canberra"
    assert flow.other_sessions(sessions, sessions, None) == []


def test_fewer_tickets_halves_down_to_one():
    seen, quantity = [], 8
    while quantity > 1:
        quantity = flow.fewer_tickets(quantity)
        seen.append(quantity)
    assert seen == [4, 2, 1]
    assert [flow.fewer_tickets(n) for n in (3, 2, 1)] == [2, 1, 1]


def test_the_same_performance_read_two_ways_is_one_session():
    dropdown = Session(date="Fri 04 Jun 2027 6:30PM", venue=None, status=None, from_price=None)
    assert flow.same_session(session("Fri 4 Jun 2027, 6:30 pm"), dropdown)
    assert not flow.same_session(session("Sun 06 Jun 2027 4:00PM"), session("Sun 06 Jun 2027 6:30PM"))
    assert not flow.same_session(session("Fri 04 Jun 2027 6:30PM"), session("Fri 04 Jun 2027 6:30PM", venue="Perth"))
    assert flow.other_sessions([dropdown], [session("Fri 04 Jun 2027 6:30pm")], None) == []


def test_choose_option_prefers_the_cheapest_standard_adult_ticket():
    options = [option("Child", "$49.90"), option("Concession", "$59.90"), option("Adult", "$129.90"),
               option("Adult", "$89.90", area="C Reserve"), option("Adult", "$69.90", area="D Reserve", available=False),
               option("Companion Card", "$0.00"), option("Adult", None, area="Premium")]  # fmt: skip
    chosen = flow.choose_option(options)
    assert (chosen.area, chosen.price) == ("C Reserve", "$89.90")
    assert flow.cheapest_offered(options) == 0.0  # any available type counts for the 'from' price check
    assert flow.choose_option([option("Student", "$40")]).price_type == "Student"  # nothing standard: cheapest
    assert flow.choose_option([option("Adult", "$40", available=False)]) is None


def test_choose_option_skips_the_best_available_any_price_row():
    # Evanescence, RAC Arena Perth: the catch-all row's range starts at the cheapest real tier's price.
    options = [option("Any price", "$145.65 - $431.10", area="Best available"),
               option(None, "$349.55", area="Premium Reserve Seating"),
               option(None, "$206.85", area="General Admission Standing"),
               option("Limited availability", "$227.20", area="A Reserve Aisle Seating"),
               option(None, "$206.85", area="A Reserve Seating"),
               option(None, "$196.65", area="B Reserve Aisle Seating"),
               option(None, "$176.25", area="B Reserve Seating"),
               option("Limited availability", "$166.05", area="C Reserve Aisle Seating"),
               option(None, "$145.65", area="C Reserve Seating"),
               option(None, None, area="D Reserve Aisle Seating", available=False),
               option(None, None, area="D Reserve Seating", available=False),
               option(None, None, area="Evanescence Premium Ticket Package - Seated", available=False),
               option(None, None, area="Evanescence Premium Ticket Package - GA", available=False),
               option("Single seats only", "$431.10", area="Evanescence Tour Package - Seated"),
               option("Limited availability", "$369.95", area="Evanescence Tour Package - GA")]  # fmt: skip
    assert flow.choose_option(options).area == "C Reserve Seating"
    c_reserve = option(None, "$145.65", area="C Reserve Seating")
    for catch_all in [option("Any price", "$145.65 - $431.10", area="Best available"),
                      option("Standard", "$145.65 - $431.10", area="Best available (Any price)")]:  # fmt: skip
        assert flow.is_catch_all(catch_all)
        assert flow.choose_option([catch_all, c_reserve]) is c_reserve
        assert flow.choose_option([catch_all]) is catch_all  # nothing else: still something to try
    assert flow.is_catch_all(option(None, "$145.65 - $431.10", area="Stalls"))  # a price range alone
    assert not flow.is_catch_all(option(None, "$1,234.56", area="Stalls"))


def test_restricted_words_in_the_area_count_and_availability_notes_are_not_ticket_types():
    package, plain = option(None, "$100", area="Evanescence Tour Package - GA"), option(None, "$150", area="B Reserve")
    assert flow.choose_option([package, plain]) is plain
    assert flow.ticket_type_of(option("Limited availability", "$166.05")) is None
    assert flow.ticket_type_of(option("Single seats only", "$431.10")) is None
    assert flow.ticket_type_of(option("Adult", "$89.90")) == "Adult"


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
    assert Surcharge.model_validate({"payment_method": "Amex", "fee": None}).fee is None


def test_a_surcharge_with_no_fee_shown_is_recorded_but_not_charged(tmp_path):
    run = result(tmp_path)
    pricing.record_surcharges(run, [Surcharge(payment_method="Amex", fee=None)])
    assert [(e["label"], e["price_display"], e["price"]) for e in run.extras] == [("Amex", None, None)]
    report = pricing.compare_prices(run, checkout(payment_surcharges=[Surcharge(payment_method="Amex")]))
    assert not any("payment surcharge" in r for r in report["drip_reasons"])


# --- holding the tickets ---------------------------------------------------------------------------------
def reading(tickets_held=True, quantity=2, unit_price="$145.65"):
    return HeldTickets(tickets_held=tickets_held, area="C Reserve Seating", seats=None, price_type="Admit",
                       quantity=quantity, unit_price=unit_price, total=None)  # fmt: skip


SELECTION = "https://premier.ticketek.com.au/shows/show.aspx?sh=EVANESCE26"


def test_a_hold_counts_only_when_the_page_shows_it():
    assert flow.hold_confirmed(reading(), 2, SELECTION, 2)
    assert flow.hold_confirmed(reading(), 2, SELECTION, None)  # moved on past the quantity controls
    assert not flow.hold_confirmed(reading(quantity=None), 2, SELECTION, None)  # a countdown timer, nothing listed
    assert not flow.hold_confirmed(reading(), 2, "https://marketplace.ticketek.com.au/listing/1", None)
    assert not flow.hold_confirmed(reading(), 2, SELECTION, 0)  # the stepper still reads 0
    assert not flow.hold_confirmed(reading(quantity=1), 2, SELECTION, None)
    assert not flow.hold_confirmed(reading(tickets_held=False), 2, SELECTION, None)


def test_a_held_price_far_from_the_one_chosen_is_not_our_ticket():
    assert flow.price_matches("$150.00", "$145.65")  # resale was only 3% dearer: the other checks must catch it
    assert not flow.price_matches("$166.40", "$145.65")
    assert flow.price_matches(None, "$145.65") and flow.price_matches("$150.00", None)


def test_the_hold_instructions_never_lead_to_resale_or_afterpay():
    text = flow.hold_instruction("2 x 'Admit'", option("Admit", "$145.65"), "'Admit'", 2)
    assert "'Find tickets'" not in text
    for words in ("Fan-to-Fan", "Afterpay", "'Change'", "Companion Card"):
        assert words in text and words in flow.CONTINUE_INSTRUCTION
    assert "Afterpay" in flow.DEFAULT_NEXT_ACTION and "Fan-to-Fan" in flow.DEFAULT_NEXT_ACTION
    assert "Afterpay" in flow.NEXT_ACTIONS["ticket_selection"]


def test_still_selecting_only_on_the_first_step_or_twice_in_a_row():
    assert flow.still_selecting("ticket_selection", 1, None)
    assert flow.still_selecting("seat_map", 4, "ticket_selection")
    assert not flow.still_selecting("ticket_selection", 3, "delivery")  # one later selection step is acted on
    assert not flow.still_selecting("cart", 1, None)


def test_went_backwards_from_a_page_past_the_hold_to_ticket_selection():
    assert flow.went_backwards("extras", "ticket_selection")
    assert flow.went_backwards("extras", "seat_map")
    assert flow.went_backwards("login_prompt", "event")
    assert not flow.went_backwards("review", "extras")
    assert not flow.went_backwards(None, "ticket_selection")  # step 1: the still_selecting rule covers it
    assert flow.still_selecting("ticket_selection", 1, None)


def test_extras_instruction_names_the_forward_button_and_every_instruction_forbids_going_back():
    assert "Go to payment" in flow.NEXT_ACTIONS["extras"]
    assert "Go to payment" in flow.NEXT_ACTIONS["review"]
    for instruction in [*flow.NEXT_ACTIONS.values(), flow.DEFAULT_NEXT_ACTION, flow.DELIVERY_CHOICE]:
        assert flow.NEVER_BACK in instruction


def test_the_guard_allows_go_to_payment_but_not_paying():
    from audit.safety import click_verdict

    assert click_verdict("Go to payment") is None
    assert click_verdict("Pay now") is not None


def test_note_action_keeps_every_action_taken_on_a_step():
    step = {"step": 2}
    flow.note_action(step, "js: Go to payment")
    flow.note_action(step, "clicked 'Next' [xpath=/a]")
    assert step["action"] == "js: Go to payment | then: clicked 'Next' [xpath=/a]"


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

    def go(states, tmp_path, interactive=False, continues=()):
        """`continues`: what each JS forward click returns in turn (a label, or None: no button found)."""
        states = iter(states)
        continues = iter(continues)
        actions: list[str] = []

        class Stagehand:
            async def extract(self, instruction, schema, **options):
                return SimpleNamespace(data=next(states))

        class Page:
            async def url(self):
                return "https://premier.ticketek.com.au/cart"

        async def fake_safe_act(stagehand, page, instruction, **options):
            actions.append(instruction)
            return SimpleNamespace(description="clicked continue", success=True, selector="xpath=/a")

        async def fake_js(page, fn, arg=None, **options):
            assert fn is flow.JS_CONTINUE_CHECKOUT
            label = next(continues, None)
            if label:
                actions.append(f"js: {label}")
            return label

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
        monkeypatch.setattr(flow, "js", fake_js)
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
    # no forward button found by script: the delivery choice, then the model's own continue
    assert len(actions) == 3 and "Do not click any continue" in actions[1] and "Then click" in actions[2]
    assert run.steps[0]["action"] == "clicked continue [xpath=/a]"
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


def test_walk_refuses_to_act_on_the_ticket_selection_page(walk, tmp_path):
    selecting = step(page_type="ticket_selection", heading="Choose your tickets")
    run, reached, actions, _ = walk([selecting], tmp_path)
    assert reached is False and actions == []
    assert run.message == "Still on the ticket selection page: tickets were not held"


def test_walk_clicks_go_to_payment_on_the_extras_page_without_the_model(walk, tmp_path):
    review = step(page_type="review", heading="Review", total="$300.20")
    extras = step(page_type="extras", heading="Add to order", total="$300.20")
    run, reached, actions, _ = walk(
        [review, extras, step(is_payment_page=True)], tmp_path, continues=["Continue", "Go to payment"]
    )
    assert reached is True and actions == ["js: Continue", "js: Go to payment"]
    assert [s.get("action") for s in run.steps] == ["js: Continue", "js: Go to payment", None]
    assert "extra_added" not in run.steps[1]


def test_walk_stops_and_names_the_action_that_sent_it_back_to_ticket_selection(walk, tmp_path):
    extras = step(page_type="extras", heading="Add to order", total="$300.20")
    selecting = step(page_type="ticket_selection", heading="Choose your tickets")
    run, reached, actions, _ = walk([extras, selecting, step(is_payment_page=True)], tmp_path)
    assert reached is False and len(actions) == 1  # nothing is clicked on the selection page
    assert run.message == "Sent back to ticket selection from 'Add to order' by: clicked continue [xpath=/a]"


# --- not enough tickets: fewer tickets, then another session ---------------------------------------------------
def hold_with(tmp_path, monkeypatch, sessions, held_at, tickets=4, dropdown=None):
    """Run hold_any_session where `held_at(date, tickets)` says whether a hold works; returns (result, item, tries)."""
    import audit.workflows.ticketek as ticketek

    tries = []

    async def try_session(self, stagehand, page, result, session, quantity):
        tries.append((session.date, quantity))
        item = new_item("A Reserve - Adult", "$89.90", quantity=quantity)
        if held_at(session.date, quantity):
            item["status"] = "added"
            return item, None
        item["status"], item["unavailable_message"] = "unavailable", "There aren't enough tickets"
        return item, f"Not enough tickets for {quantity} on {session.date}"

    async def nothing(*args, **kwargs):
        return None

    async def listed(page):
        return (dropdown or [], None)

    monkeypatch.setattr(TicketekWorkflow, "try_session", try_session)
    monkeypatch.setattr(ticketek, "return_to_event", nothing)
    monkeypatch.setattr(ticketek, "dropdown_sessions", listed)
    workflow = TicketekWorkflow(argparse.Namespace(tickets=tickets, session=None), Settings())
    run = TicketekResult(site="s", out_dir=tmp_path, shop_url="https://premier.ticketek.com.au/shows/x")
    event = SimpleNamespace(sessions=sessions)
    item = asyncio.run(workflow.hold_any_session(None, None, run, event, sessions[0]))
    return run, item, tries


def test_not_enough_tickets_retries_the_same_session_with_fewer(tmp_path, monkeypatch):
    sessions = [session("Fri 04 Jun 2027 6:30PM"), session("Sun 06 Jun 2027 4:00PM")]
    run, item, tries = hold_with(tmp_path, monkeypatch, sessions, lambda date, n: n <= 2)
    assert tries == [("Fri 04 Jun 2027 6:30PM", 4), ("Fri 04 Jun 2027 6:30PM", 2)]
    assert item["quantity"] == 2 and run.session == "Fri 04 Jun 2027 6:30PM"
    assert [t["tickets"] for t in run.sessions_tried] == [4]


def test_when_not_even_one_ticket_is_left_another_session_is_tried_for_the_full_quantity(tmp_path, monkeypatch):
    sessions = [session("Fri 04 Jun 2027 6:30PM"), session("Sun 06 Jun 2027 4:00PM")]
    run, item, tries = hold_with(tmp_path, monkeypatch, sessions, lambda date, n: date.startswith("Sun"))
    assert tries == [("Fri 04 Jun 2027 6:30PM", 4), ("Fri 04 Jun 2027 6:30PM", 2), ("Fri 04 Jun 2027 6:30PM", 1),
                     ("Sun 06 Jun 2027 4:00PM", 4)]  # fmt: skip
    assert item["quantity"] == 4 and run.session == "Sun 06 Jun 2027 4:00PM"


def test_a_dropdown_date_is_tried_and_the_run_gives_up_when_nothing_is_left(tmp_path, monkeypatch):
    dropdown = [Session(date="Sun 06 Jun 2027 6:30PM", venue=None, status=None, from_price=None)]
    run, item, tries = hold_with(tmp_path, monkeypatch, [session("Fri 04 Jun 2027 6:30PM")], lambda d, n: False,
                                 tickets=1, dropdown=dropdown)  # fmt: skip
    assert tries == [("Fri 04 Jun 2027 6:30PM", 1), ("Sun 06 Jun 2027 6:30PM", 1)]
    assert item is None and run.status == "no_items_found" and "no other session to try" in run.message
    assert len(run.items) == 1 and len(run.sessions_tried) == 2


def test_the_tries_are_capped(tmp_path, monkeypatch):
    sessions = [session(f"Day {d} Jun 2027 6:30PM") for d in range(1, 9)]
    run, item, tries = hold_with(tmp_path, monkeypatch, sessions, lambda d, n: False, tickets=8)
    assert len(tries) == 8 and item is None and run.status == "no_items_found"
