"""Workflow logic that needs neither a browser nor a model."""

import argparse
import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from audit.actions import Acted
from audit.config import Settings
from audit.core import SiteResult, Status
from audit.workflows import WORKFLOWS, coffee, qantas
from audit.workflows.coffee import CoffeeWorkflow
from audit.workflows.qantas import FieldToFill, Passenger, QantasWorkflow, passenger_field_instruction
from audit.workflows.ubereats import UberEatsWorkflow
from audit.workflows.ubereats import models as uber_models
from audit.workflows.ubereats import store as uber_store

REAL = Settings(pax_first_name="Samantha", pax_last_name="Okafor", pax_email="sam@real.example",
                pax_phone="0412 345 678", pax_date_of_birth="3 March 1991")  # fmt: skip


def test_every_workflow_has_a_safety_prompt():
    for workflow in WORKFLOWS:
        assert workflow.system_prompt, workflow.name
        assert "never" in workflow.system_prompt.lower()


# --- qantas: passenger details stay out of the model's input (REVIEW H7) -----------------------------
def test_typed_details_become_variables_not_prompt_text():
    passenger = Passenger.from_settings(REAL)
    instruction, variables = passenger_field_instruction(
        FieldToFill(label="First name", kind="text", detail="first_name"), passenger
    )
    assert instruction == "Type %first_name% into the 'First name' field of the passenger details form."
    assert variables == {"first_name": "Samantha"}
    assert "Samantha" not in instruction


def test_listed_choices_are_named_because_the_model_must_find_the_option():
    passenger = Passenger.from_settings(REAL)
    instruction, variables = passenger_field_instruction(
        FieldToFill(label="Title", kind="dropdown", detail="title"), passenger
    )
    assert (instruction, variables) == ("Select 'Mr' for the 'Title' field of the passenger details form.", {})
    assert (
        passenger_field_instruction(FieldToFill(label="Velocity number", kind="text", detail="other"), passenger)
        is None
    )


def test_no_typed_detail_reaches_a_prompt_or_a_saved_file():
    passenger = Passenger.from_settings(REAL)
    typed = passenger.typed_details()
    assert set(typed) == {"first_name", "last_name", "date_of_birth", "email", "phone"}
    assert QantasWorkflow(argparse.Namespace(), REAL).sensitive_values() == typed

    class Recorder:
        def __init__(self):
            self.prompts: list[str] = []
            self.variables: list[dict] = []

        async def extract(self, instruction, schema, **options):
            self.prompts.append(instruction)
            fields = [FieldToFill(label=name.replace("_", " ").title(), kind="text", detail=name) for name in typed]
            return SimpleNamespace(
                data=schema(fields=fields if len(self.prompts) == 1 else [], complete=len(self.prompts) > 1)
            )

        async def observe(self, instruction, **options):
            self.prompts.append(instruction)
            self.variables.append(options.get("variables", {}))
            action = SimpleNamespace(description="field", selector="xpath=//input", method="fill", arguments=["%x%"])
            return SimpleNamespace(data=[action])

        async def act(self, action, **options):
            return SimpleNamespace(data=SimpleNamespace(success=True, action_description="typed"))

    class Page:
        async def url(self):
            return "https://book.air.test/dx/#/passengers"

        async def evaluate(self, expression):
            return {"value": None, "error": None}  # a page.js() snippet

        async def wait_for_load_state(self, *args, **kwargs):
            return None

        async def wait_for_function(self, *args, **kwargs):
            return True

    stagehand = Recorder()
    asyncio.run(qantas.fill_passenger_details(stagehand, Page(), passenger))
    everything_the_model_saw = " ".join(stagehand.prompts)
    for value in typed.values():
        assert value not in everything_the_model_saw
    assert {name for v in stagehand.variables for name in v} == set(typed)  # the values travel as variables


# --- keys for runs.csv / summary.csv (REVIEW H4) -------------------------------------------------------
def result(**fields) -> SiteResult:
    return SiteResult(site="https://start.test", out_dir=Path(), **fields)


def test_target_keys():
    settings = Settings()
    coffee_flow = CoffeeWorkflow(argparse.Namespace(), settings)
    assert coffee_flow.target_key("https://redbrick.coffee", result()) == "https://redbrick.coffee"

    qantas_flow = QantasWorkflow(argparse.Namespace(), settings)
    trip = result(search={"origin": "BNE", "destination": "PER"})
    assert (
        qantas_flow.target_key("https://www.qantas.com/au/en.html", trip) == "https://www.qantas.com/au/en.html BNE-PER"
    )

    uber = UberEatsWorkflow(argparse.Namespace(), settings)
    home = "https://www.ubereats.com/au"
    store = uber_models.UberResult(
        site=home, out_dir=Path(), shop_url=f"{home}/store/ooshman-belconnen/ARt4Fx0q?diningMode=DELIVERY&pl=abc"
    )
    assert uber.target_key(home, store) == "/store/ooshman-belconnen/ARt4Fx0q"  # no address-bearing query string
    named = uber_models.UberResult(site=home, out_dir=Path(), store_name="OOSHMAN")
    assert uber.target_key(home, named) == "OOSHMAN"
    assert uber.target_key(home, uber_models.UberResult(site=home, out_dir=Path())) == home


def test_ubereats_empties_the_cart_after_the_run(monkeypatch):
    import audit.workflows.ubereats as uber_package

    cleared: list[str] = []

    async def clear(stagehand, page, out_dir=None, report_name="cart_clear.json"):
        cleared.append(report_name)
        return uber_store.CartLeft(0, None)

    class FakePage:
        def __init__(self) -> None:
            self.visited: list[str] = []

        async def goto(self, url: str) -> None:
            self.visited.append(url)

        async def evaluate(self, expression: str) -> object:
            return True

        async def url(self) -> str:
            return shop

    async def settled(page, ms=0):
        return None

    monkeypatch.setattr(uber_package, "clear_existing_cart", clear)
    monkeypatch.setattr(uber_package, "settle", settled)
    shop = "https://www.ubereats.com/au/store/ooshman-belconnen/abc"
    uber = UberEatsWorkflow(argparse.Namespace(), Settings())
    run = uber_models.UberResult(site="ubereats", out_dir=Path(), shop_url=shop)
    page = FakePage()
    asyncio.run(uber.empty_cart_after_run(None, page, run))  # type: ignore[arg-type]
    assert page.visited == [shop]
    assert cleared == ["cart_clear-after.json"]


def test_ubereats_output_folder():
    uber = UberEatsWorkflow(argparse.Namespace(), Settings())
    assert uber.output_folder("https://www.ubereats.com/au") == "ubereats.com"
    assert (
        uber.output_folder("https://www.ubereats.com/au/store/ooshman-belconnen/abc")
        == "ubereats.com-ooshman-belconnen"
    )


# --- pure helpers ----------------------------------------------------------------------------------------
def test_coffee_selection_and_name_matching():
    def product(name, coffee_kind=True, sold_out=False, kind="whole_bean_or_ground"):
        return coffee.Product(
            name=name, price="$20", sold_out=sold_out, is_coffee=coffee_kind, product_type=kind, url=None
        )

    products = [product("Mug", coffee_kind=False, kind="merch"), product("Pods", kind="pods_or_capsules"),
                product("Sold", sold_out=True), product("Rogue"), product("Good Day")]  # fmt: skip
    assert [p.name for p in coffee.choose_coffees(products, 3)] == ["Rogue", "Good Day", "Pods"]
    assert coffee.match_name("Coffee Coffee") == "coffee"
    assert coffee.match_name("Good Day Coffee") == "good day"


def test_qantas_picks_the_cheapest_direct_flight():
    def flight(price, stops=0, sold_out=False):
        return qantas.Flight(flight_number=None, departs="6:00am", arrives="7:35am", stops=stops, duration=None,
                             price=price, sold_out=sold_out, fares=[])  # fmt: skip

    assert (
        qantas.choose_flight(
            [flight("$99", stops=1), flight("$199"), flight("$149"), flight("$1", sold_out=True)]
        ).price
        == "$149"
    )
    assert qantas.choose_flight([flight(None), flight("$10", sold_out=True)]) is None


def test_qantas_never_chooses_a_points_fare():
    def flight(price, departs="19:00", award_only=False):
        return qantas.Flight(flight_number=None, departs=departs, arrives="22:20", stops=0, duration=None,
                             price=price, award_only=award_only, sold_out=False, fares=[])  # fmt: skip

    # The model put the award price in `price` and did not flag it: the code still skips it.
    cash = flight("From $529", departs="12:35")
    assert qantas.choose_flight([flight("Classic 16,600PTS+ $58"), cash]) is cash
    assert qantas.choose_flight([flight("16,600 PTS + $58"), flight("10,000 points"), cash]) is cash
    flagged = flight("$10", award_only=True)
    assert qantas.choose_flight([flagged, cash]) is cash
    assert qantas.choose_flight([flagged, flight("16,600 PTS + $58")]) is None
    assert qantas.cash_price("$835 or Classic Plus") == 835  # a reward badge next to a cash price is fine
    fares = [qantas.FareOption(fare_name="Economy Flex", price="$995"),
             qantas.FareOption(fare_name="Economy Saver", price="$529"),
             qantas.FareOption(fare_name="Economy", price="16,600 PTS + $58")]  # fmt: skip
    assert qantas.choose_fare(fares).fare_name == "Economy Saver"


def test_fields_left_null_are_handled_where_they_are_read():
    """Each field the model may leave null (see test_compare.NULLABLE_READS) has readers that cope with it."""
    untyped = coffee.Product(name="Mystery", sold_out=False, is_coffee=True, url=None)  # no price, no type
    bean = coffee.Product(name="Rogue", price="$20", sold_out=False, is_coffee=True,
                          product_type="whole_bean_or_ground", url=None)  # fmt: skip
    assert coffee.choose_coffees([untyped, bean], 2) == [bean, untyped]  # an unknown type is not preferred

    def flight(**shown):
        return qantas.Flight(
            **{"flight_number": None, "duration": None, "price": "$129", "sold_out": False, "fares": [], **shown}
        )

    full = flight(flight_number="QF 401", departs="6:00am", arrives="7:35am", stops=0)
    assert qantas.describe_flight(full) == (
        "QF 401 6:00am - 7:35am",
        "the flight departing 6:00am and arriving 7:35am (QF 401)",
    )
    assert qantas.describe_flight(flight(departs="6:00am")) == ("6:00am", "the flight departing 6:00am")
    assert qantas.describe_flight(flight()) == ("Flight at $129", "the flight priced $129")
    assert "None" not in " ".join(qantas.describe_flight(flight(arrives="7:35am")))
    direct, unknown = flight(departs="9:00am", stops=0), flight(departs="6:00am")
    unknown.price = "$99"
    assert qantas.choose_flight([unknown, direct]) is direct  # unknown stops is not taken as direct

    passenger = Passenger.from_settings(REAL)
    assert passenger_field_instruction(FieldToFill(label="Mystery", kind="text"), passenger) is None
    assert passenger_field_instruction(FieldToFill(label="Email", detail="email"), passenger) == (
        "Type %email% into the 'Email' field of the passenger details form.",
        {"email": "sam@real.example"},
    )

    def dish(price):
        return uber_models.MenuItem(name="Dish", category=None, price=price, sold_out=False, is_food_or_drink=True)

    assert uber_store.choose_menu_items([dish(None), dish("$14.70")], 2) == [dish("$14.70")]


def test_ubereats_address_helpers():
    assert uber_store.street_tokens("1 Jeffcott Place, Canberra ACT 2615") == ["1", "jeffcott"]
    assert uber_store.addresses_match("1 Jeffcott Pl", "1 Jeffcott Place, Canberra ACT 2615")
    assert not uber_store.addresses_match("240 City Walk", "1 Jeffcott Place, Canberra")
    assert not uber_store.addresses_match("Enter delivery address", "1 Jeffcott Place")
    assert uber_store.extract_pl_address_from_url("https://x.test/store/a?pl=not-base64-json") is None
    assert uber_store.extract_pl_address_from_url("https://x.test/store/a") is None
    encoded = "JTdCJTIyYWRkcmVzcyUyMiUzQSUyMjElMjBUZXN0JTIwU3QlMjIlN0Q="  # {"address":"1 Test St"}
    assert uber_store.extract_pl_address_from_url(f"https://x.test/store/a?pl={encoded}") == "1 Test St"
    assert uber_store.is_valid_restaurant_name("OOSHMAN") and not uber_store.is_valid_restaurant_name("Coles Belconnen")


def test_fail_sets_the_matching_reason():
    run = result()
    run.fail(Status.ADD_TO_CART_FAILED, "nothing added")
    assert (run.status, run.failure_reason, run.message) == ("add_to_cart_failed", "element_not_found", "nothing added")


def test_conclude_never_masks_the_run_s_own_failure():
    run = result(status=Status.OK)

    def broken():
        raise KeyError("fee_breakdown")

    CoffeeWorkflow.conclude(run, broken)  # must not raise from a `finally`
    assert run.status == Status.ERROR and "Price comparison failed" in run.message
    blocked = result(status=Status.BLOCKED, message="captcha")
    CoffeeWorkflow.conclude(blocked, broken)
    assert (blocked.status, blocked.message) == ("blocked", "captcha")  # the real outcome is kept


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda w: w.name)
def test_workflows_declare_their_policy(workflow):
    assert workflow.clear_cookies in (None, "domain")
    assert workflow.log_level in ("warn", "info")


# --- Phase 5: no extraction is paid for when an action changed nothing ----------------------------------
def test_qantas_reuses_the_step_when_the_screen_did_not_change(monkeypatch, tmp_path):
    screens = iter(["extras|A", "extras|A", "extras|A", "extras|A"])  # every action leaves the screen as it was
    extracts, actions = [], []

    def state():
        return qantas.StepState(step_type="extras", heading="Extras", is_payment_page=False, blocked_reason=None,
                                offers=[], price_lines=[], total="$199.00", validation_errors=[], summary="extras page")  # fmt: skip

    class Stagehand:
        async def extract(self, instruction, schema, **options):
            extracts.append(schema.__name__)
            return SimpleNamespace(data=state())

    async def fake_llm_act(stagehand, instruction, **options):
        actions.append(instruction)
        return SimpleNamespace(description="clicked continue", success=True)

    async def nothing(*args, **kwargs):
        return None

    class Page:
        async def url(self):
            return "https://book.air.test/dx/#/extras"

    async def fake_fingerprint(page):
        return next(screens)

    monkeypatch.setattr(qantas, "fingerprint", fake_fingerprint)
    monkeypatch.setattr(qantas, "llm_act", fake_llm_act)
    for name in ("wait_for_render", "shot", "settle"):
        monkeypatch.setattr(qantas, name, nothing)
    run = SiteResult(site="s", out_dir=tmp_path)
    reached = asyncio.run(qantas.walk_to_payment(Stagehand(), Page(), Passenger(), run))
    assert reached is False
    assert run.message.startswith("Stuck on 'Extras'")
    assert extracts == ["StepState"]  # one read; the three repeats cost no extraction (was four reads)
    assert len(actions) == 3
    assert len(run.steps) == 1


# --- qantas: a custom dropdown that opens but is never chosen from does not eat the whole search ---------
class TripWidget:
    """The page's trip-type combobox, faked at the js()/click_at() boundary. None: the page has none."""

    def __init__(self, trip="return", *, ignores_mouse=False):
        self.trip, self.open, self.ignores_mouse = trip, False, ignores_mouse
        self.events = []

    async def js(self, page, fn, arg=None, default=None, lib=""):
        if fn is qantas.JS_TRIP_TYPE:
            if self.trip is None:
                return default
            text = {"one_way": "One way", "return": "Return"}[self.trip]
            return {"trip": self.trip, "text": text, "tag": "div", "x": 10, "y": 20, "controls": None}
        if fn is qantas.JS_ONE_WAY_OPTION:
            return {"x": 10, "y": 60} if self.open else default
        if fn in (qantas.JS_AIRPORT_FIELD, qantas.JS_FOCUS_AIRPORT, qantas.JS_AIRPORT_OPTION):
            return default  # no airport fields unless a test fakes them
        if fn in (
            qantas.JS_DATE_FIELD,
            qantas.JS_DATE_DIALOG,
            qantas.JS_DAY_CELL,
            qantas.JS_DATE_CONTINUE,
            qantas.JS_SCROLL_DATES,
        ):
            return default  # no date control unless a test fakes it
        if fn is qantas.JS_DOM_CLICK:
            self.events.append(("dom_click", arg))
            if arg == "data-audit-trip-option" and self.open:
                self.trip, self.open = "one_way", False
            return True
        raise AssertionError(f"unexpected script {fn[:40]!r}")

    async def click_at(self, page, x, y):
        self.events.append(("click", x, y))
        if (x, y) == (10, 20):
            self.open = not self.open
        elif (x, y) == (10, 60) and self.open and not self.ignores_mouse:
            self.trip, self.open = "one_way", False


class SearchPage:
    """The browser page search_flights talks to directly: its URL, navigation and the keyboard."""

    def __init__(self, urls=None):
        self.urls = list(urls or [])
        self.visited: list[str] = []
        self.keys: list[str] = []

    async def url(self):
        return self.urls.pop(0) if len(self.urls) > 1 else (self.urls[0] if self.urls else "https://www.qantas.com/x")

    async def goto(self, url):
        self.visited.append(url)

    async def key_press(self, key, delay=None):
        self.keys.append(key)

    async def type(self, text, delay=None):
        self.keys.append(f"type:{text}")


def _search(monkeypatch, *, states, selectors, widget=None, description="Trip Type combobox", page=None):
    """Run qantas.search_flights with scripted SearchState reads and the controls the model acts on, in order."""
    reads, picks = iter(states), iter(selectors)
    calls = []
    widget = widget or TripWidget(None)

    class Stagehand:
        async def extract(self, instruction, schema, **options):
            widget.events.append("extract")
            return SimpleNamespace(data=next(reads))

    async def fake_llm_act(stagehand, instruction, **options):
        calls.append((instruction, options))
        return Acted(True, description, next(picks))

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(qantas, "llm_act", fake_llm_act)
    monkeypatch.setattr(qantas, "settle", nothing)
    monkeypatch.setattr(qantas, "js", widget.js)
    monkeypatch.setattr(qantas, "click_at", widget.click_at)
    monkeypatch.setattr(qantas, "dismiss_popups", nothing)
    trip = qantas.Trip("BNE", "PER", qantas.date(2027, 2, 14))
    return calls, asyncio.run(qantas.search_flights(Stagehand(), page or SearchPage(), trip))


def _form(trip_type="return", **fields):
    values = {"on_results_page": False, "form_visible": True, "trip_type": trip_type, "origin": None,
              "destination": None, "depart_date": None, "guests": "1 Adult", "using_points": False,
              "blocked_reason": None, "reason": "search form"}  # fmt: skip
    return qantas.SearchState(**{**values, **fields})


def test_qantas_search_opens_a_dropdown_and_picks_from_it(monkeypatch):
    calls, _ = _search(monkeypatch, states=[_form(), _form(on_results_page=True)], selectors=["xpath=//option[2]"])
    assert calls[0][1].get("follow_up") is True


def test_qantas_search_stops_when_the_same_control_changes_nothing(monkeypatch):
    combobox = "xpath=//div[@role='combobox']"
    # 'Return' and '' read the same as 'return' and None: the model's spelling varies from read to read
    states = [_form(), _form(), _form("Return", origin=""), _form(), _form()]
    with pytest.raises(qantas.FlowChanged, match="acted on 4 times in a row"):
        _search(monkeypatch, states=states, selectors=[combobox] * 5)  # stops after 4 of the 14 steps


def test_qantas_search_tells_the_model_its_last_action_changed_nothing(monkeypatch):
    combobox = "xpath=//div[@role='combobox']"
    states = [_form(), _form(), _form(), _form("one_way", on_results_page=True)]
    calls, _ = _search(monkeypatch, states=states, selectors=[combobox, combobox, "xpath=//li[text()='One way']"])
    assert len(calls) == 3
    assert "changed nothing" not in calls[0][0]
    assert "changed nothing" not in calls[1][0]  # the first click on a control is not a repeat
    assert "changed nothing" in calls[2][0] and "Trip Type combobox" in calls[2][0]  # the second one is


def test_qantas_search_sets_one_way_in_the_page_before_asking_the_model(monkeypatch):
    widget = TripWidget("return")
    # The extraction keeps saying 'return'; the page's own combobox is what counts.
    states = [_form("return"), _form("return", on_results_page=True)]
    calls, _ = _search(monkeypatch, states=states, selectors=["xpath=//input[1]"], widget=widget)
    assert widget.events[:3] == [("click", 10, 20), ("click", 10, 60), "extract"]  # set before any model call
    assert widget.trip == "one_way"
    instruction = calls[0][0]
    assert "trip type=one_way" in instruction
    assert "already set to One way: do not touch the trip-type control" in instruction


def test_qantas_trip_type_falls_back_to_a_dom_click_when_the_mouse_click_is_ignored(monkeypatch):
    widget = TripWidget("return", ignores_mouse=True)
    states = [_form(), _form(on_results_page=True)]
    calls, _ = _search(monkeypatch, states=states, selectors=["xpath=//input[1]"], widget=widget)
    assert ("dom_click", "data-audit-trip-option") in widget.events
    assert "already set to One way" in calls[0][0]


def test_qantas_search_leaves_the_trip_type_to_the_model_without_a_combobox(monkeypatch):
    calls, _ = _search(monkeypatch, states=[_form(), _form(on_results_page=True)], selectors=["xpath=//x"])
    assert "trip type=return" in calls[0][0]
    assert "do not touch the trip-type control" not in calls[0][0]


def test_qantas_stuck_guard_ignores_guests_dropping_in_and_out(monkeypatch):
    combobox = "xpath=//div[@role='combobox']"
    # The extraction reads '1 Adult' one time and nothing the next, on the same screen.
    states = [_form(), _form(), _form(guests=None), _form(), _form(guests=None, origin=None)]
    with pytest.raises(qantas.FlowChanged, match="acted on 4 times in a row"):
        _search(monkeypatch, states=states, selectors=[combobox] * 5)


def test_qantas_stuck_guard_counts_an_empty_read_as_unchanged(monkeypatch):
    combobox = "xpath=//div[@role='combobox']"
    filled = {"origin": "Brisbane (BNE)"}
    states = [_form(**filled), _form(**filled), _form(), _form(**filled), _form()]  # origin drops out, comes back
    with pytest.raises(qantas.FlowChanged, match="acted on 4 times in a row"):
        _search(monkeypatch, states=states, selectors=[combobox] * 5)


def test_qantas_paging_the_calendar_on_is_not_stuck(monkeypatch):
    arrow = "xpath=//button[@aria-label='Next month']"
    states = [_form("one_way")] * 6 + [_form("one_way", on_results_page=True)]
    calls, _ = _search(monkeypatch, states=states, selectors=[arrow] * 6, description="Next month arrow")
    assert len(calls) == 6
    assert not any("changed nothing" in instruction for instruction, _ in calls)


# --- qantas: a flight-deals page or other airports are not our search's results ---------------------------
SYD = "SYD, Sydney (Kingsford Smith), Australia"
DEALS_URL = "https://www.qantas.com/en-au/book/flight-deals/from-sydney-to-ballina?cabin=economy"
BNE_PER = qantas.Trip("BNE", "PER", qantas.date(2027, 2, 14))


def test_wrong_results():
    normal = "https://www.qantas.com/au/en/book-a-trip/flights.html"
    assert qantas.wrong_results(_form(on_results_page=True), BNE_PER, normal) is None  # null fields never count
    shown = _form(on_results_page=True, origin="BNE, Brisbane, Australia", destination="PER, Perth, Australia")
    assert qantas.wrong_results(shown, BNE_PER, normal) is None
    assert qantas.wrong_results(_form(origin=SYD), BNE_PER, normal) == f"origin shows '{SYD}', not BNE"
    assert "flight-deals page" in qantas.wrong_results(shown, BNE_PER, DEALS_URL)


def test_qantas_search_goes_back_when_results_show_other_airports(monkeypatch):
    page = SearchPage()
    states = [_form("one_way", on_results_page=True, origin=SYD), _form("one_way"),
              _form("one_way", on_results_page=True)]  # fmt: skip
    calls, _ = _search(monkeypatch, states=states, selectors=["xpath=//button"], page=page)
    assert page.visited == [qantas.SITE_URL]  # back to the search form, not on to the "results"
    assert len(calls) == 1


def test_qantas_search_gives_up_after_the_second_wrong_results_page(monkeypatch):
    states = [_form("one_way", on_results_page=True, origin=SYD)] * 3
    with pytest.raises(qantas.FlowChanged, match="wrong page twice: origin shows"):
        _search(monkeypatch, states=states, selectors=[])


def test_qantas_search_does_not_take_a_deals_page_for_results(monkeypatch):
    page = SearchPage([DEALS_URL])
    with pytest.raises(qantas.FlowChanged, match="flight-deals page"):
        _search(monkeypatch, states=[_form("one_way", on_results_page=True)] * 3, selectors=[], page=page)
    assert page.visited == [qantas.SITE_URL]


class AirportWidget(TripWidget):
    """The departure/arrival type-ahead fields, pre-filled with SYD as geolocation does, plus no trip type."""

    def __init__(self, *, has_option=True):
        super().__init__(None)
        self.values = {"departure": SYD, "arrival": None}
        self.has_option = has_option
        self.typing = None  # which field has the focus
        self.typed = ""

    async def js(self, page, fn, arg=None, default=None, lib=""):
        if fn is qantas.JS_AIRPORT_FIELD:
            x = {"departure": 100, "arrival": 200}[arg]
            return {"value": self.values[arg], "tag": "input", "x": x, "y": 50, "clear": None, "controls": None}
        if fn is qantas.JS_FOCUS_AIRPORT:
            return self.typing == arg
        if fn is qantas.JS_AIRPORT_OPTION:
            code = arg["code"]
            if not self.has_option or self.typed != code:
                return default
            return {"text": f"{code}, Somewhere, Australia", "x": 300, "y": 300}
        return await super().js(page, fn, arg, default, lib)

    async def click_at(self, page, x, y):
        self.events.append(("click", x, y))
        if (x, y) == (300, 300):
            self.values[self.typing] = f"{self.typed}, Somewhere, Australia"
        else:
            self.typing = {100: "departure", 200: "arrival"}.get(x, self.typing)


def _typing_page(widget):
    page = SearchPage()

    async def key_press(key, delay=None):
        page.keys.append(key)
        widget.typed = ""

    async def type_text(text, delay=None):
        page.keys.append(f"type:{text}")
        widget.typed += text

    page.key_press, page.type = key_press, type_text
    return page


def test_qantas_search_sets_both_airports_before_asking_the_model(monkeypatch):
    widget = AirportWidget()
    page = _typing_page(widget)
    calls, _ = _search(monkeypatch, states=[_form("one_way", origin=SYD), _form("one_way", on_results_page=True)],
                       selectors=["xpath=//input[3]"], widget=widget, page=page)  # fmt: skip
    assert page.keys == ["Backspace", "type:BNE", "Backspace", "type:PER"]
    assert widget.values == {"departure": "BNE, Somewhere, Australia", "arrival": "PER, Somewhere, Australia"}
    instruction = calls[0][0]
    assert "from=BNE, Somewhere, Australia" in instruction  # the DOM's reading, not the extraction's SYD
    assert "already set to BNE and PER: do not touch, clear or swap them" in instruction


def test_qantas_airports_are_left_to_the_model_when_no_option_matches(monkeypatch):
    widget = AirportWidget(has_option=False)
    widget.values["departure"] = None  # an empty field: the DOM has nothing to correct the extraction with
    page = _typing_page(widget)
    states = [_form("one_way"), _form("one_way"), _form("one_way", on_results_page=True)]
    calls, _ = _search(monkeypatch, states=states, selectors=["xpath=//input[3]", "xpath=//input[4]"],
                       widget=widget, page=page)  # fmt: skip
    assert len(calls) == 2
    assert not any("do not touch, clear or swap" in instruction for instruction, _ in calls)
    assert page.keys.count("type:BNE") == page.keys.count("type:PER") == 1  # not typed again on every step


def test_qantas_a_listing_without_times_or_flight_numbers_is_not_an_add_to_cart_failure(monkeypatch, tmp_path):
    deals = qantas.Flights(route="Flights from Sydney to Ballina", price_basis=None, flights=[
        qantas.Flight(flight_number=None, duration=None, price=f"${p}", sold_out=False, fares=[]) for p in (99, 129)
    ])  # fmt: skip

    class Stagehand:
        async def extract(self, instruction, schema, **options):
            return SimpleNamespace(data=deals)

    async def nothing(*args, **kwargs):
        return None

    async def selected(*args, **kwargs):
        raise AssertionError("no flight should be selected from a deals page")

    for name in ("settle", "dismiss_popups", "search_flights", "wait_for_render", "shot"):
        monkeypatch.setattr(qantas, name, nothing)
    monkeypatch.setattr(qantas, "select_flight", selected)
    monkeypatch.setattr(QantasWorkflow, "conclude", staticmethod(lambda result, compare: None))
    args = argparse.Namespace(origin="BNE", destination="PER", date=qantas.date(2027, 2, 14))
    run = SiteResult(site="qantas", out_dir=tmp_path)
    with pytest.raises(qantas.FlowChanged, match=r"no flights with times or flight numbers.*Sydney to Ballina"):
        asyncio.run(QantasWorkflow(args, Settings()).run(Stagehand(), SearchPage(), qantas.SITE_URL, run))
    assert run.status != Status.ADD_TO_CART_FAILED


def test_month_navigation():
    assert qantas.is_month_navigation("Click the 'Next month' arrow in the calendar")
    assert qantas.is_month_navigation("Go to the next month")
    assert qantas.is_month_navigation("Month forward button")
    assert not qantas.is_month_navigation("Trip Type combobox")
    assert not qantas.is_month_navigation("Select 14 February 2027")


# --- qantas: the departure date is set in code, and the picker's pending day is not taken for it --------------
FEB_14 = qantas.date(2027, 2, 14)


@pytest.mark.parametrize(
    "text",
    ["Sun, 14 Feb 2027", "14 Feb 2027", "Depart BNE Sun, 14 Feb 2027", "14 February 2027", "February 14, 2027",
     "Button to select travel dates, currently set to 14 Feb 2027", "14/02/2027", "2027-02-14"],
)  # fmt: skip
def test_dates_that_name_the_day(text):
    assert qantas.shows_date(text, FEB_14)


@pytest.mark.parametrize("text", ["29 Nov 2026", "14 Feb 2026", "4 Feb 2027", "114 Feb 2027", "14 Mar 2027", "", None])
def test_dates_that_do_not(text):
    assert not qantas.shows_date(text, FEB_14)


def test_date_text_and_the_date_a_control_shows():
    assert qantas.date_text(FEB_14) == "14 Feb 2027"
    assert qantas.date_text(qantas.date(2027, 3, 2)) == "2 Mar 2027"
    assert qantas.shown_date("Button to select travel dates, currently set to 29 Nov 2026") == "29 Nov 2026"
    assert qantas.shown_date("Travel dates") == "Travel dates"
    assert qantas.shown_date(None) is None


class DateWidget(TripWidget):
    """The form's travel-dates button, already showing `shown`; no picker opens."""

    def __init__(self, shown):
        super().__init__(None)
        self.shown = shown

    async def js(self, page, fn, arg=None, default=None, lib=""):
        if fn is qantas.JS_DATE_FIELD:
            return {"text": f"Button to select travel dates, currently set to {self.shown}", "x": 5, "y": 5}
        return await super().js(page, fn, arg, default, lib)


def test_qantas_search_tells_the_model_the_date_is_set(monkeypatch):
    widget = DateWidget("14 Feb 2027")
    calls, _ = _search(monkeypatch, states=[_form(depart_date="1 Jan 2027"), _form(on_results_page=True)],
                       selectors=["xpath=//button[1]"], widget=widget)  # fmt: skip
    instruction = calls[0][0]
    assert "date=14 Feb 2027" in instruction  # the form's date, not the extraction's
    assert "departure date is already set to 14 Feb 2027: do not open the date picker" in instruction


def test_qantas_search_shows_the_model_the_form_s_date_not_the_picker_s(monkeypatch):
    widget = DateWidget("29 Nov 2026")  # the picker said 14 Feb; the form still has its default
    calls, _ = _search(monkeypatch, states=[_form(depart_date="14 Feb 2027"), _form(on_results_page=True)],
                       selectors=["xpath=//button[1]"], widget=widget)  # fmt: skip
    assert "date=29 Nov 2026" in calls[0][0]
    assert "do not open the date picker" not in calls[0][0]


# --- qantas: taking turns between two controls that change nothing is stuck too ------------------------------
def test_alternating():
    held = ("one_way", "bne", "per", "14 feb 2027", False)
    day, submit = ("xpath=//button[7]", held, "14 Feb cell"), ("xpath=//form/button", held, "Search flights")
    assert qantas.alternating([day, submit, day, submit]) == ("14 Feb cell", "Search flights")
    assert qantas.alternating([day, submit, day]) is None  # too few
    assert qantas.alternating([day, day, submit, submit]) is None  # repeats, not turns (the repeat count's job)
    moved = ("xpath=//button[7]", (*held[:3], "15 feb 2027", False), "14 Feb cell")
    assert qantas.alternating([day, submit, moved, submit]) is None  # the search changed
    third = ("xpath=//x", held, "Guests")
    assert qantas.alternating([day, submit, third, submit]) is None


def test_qantas_search_stops_when_two_controls_take_turns(monkeypatch):
    day, submit = "xpath=/html[1]/body[1]/div[7]//button[7]", "xpath=//form//button[@type='submit']"
    states = [_form("one_way", depart_date="14 Feb 2027")] * 6
    with pytest.raises(qantas.FlowChanged, match="alternating between"):
        _search(monkeypatch, states=states, selectors=[day, submit, day, submit, day])  # stops after 4 of the 14


# --- coffee: an add-to-cart failure says why in price_comparison.json ---------------------------------
def _add_to_cart(monkeypatch, *, observed, counts, direct=None, visible=0, cart=None):
    """Run coffee.add_product_to_cart on a ready product page, with a fake model and a scripted /cart.js.

    `counts` are the item_count reads in order (the last one repeats); `direct` is what the product form's own
    button click returns (None: no such button); `visible` is the index the visibility check picks.
    """
    calls = {"observe": 0, "acted": [], "direct": 0, "visible": []}

    class Stagehand:
        async def extract(self, instruction, schema, **options):
            return SimpleNamespace(data=schema(
                is_product_page=True, has_purchase_options=False, one_time_selected=True, has_size_options=False,
                lowest_size_selected=True, selected_size="200g", lowest_available_size="200g",
                current_price="$22.00", reason=None,
            ))  # fmt: skip

        async def observe(self, instruction, **options):
            if "Add to cart" not in instruction:
                return SimpleNamespace(data=[])  # the click that opens the product page: already open
            calls["observe"] += 1
            return SimpleNamespace(data=observed)

        async def act(self, action, **options):
            calls["acted"].append(action.selector)
            return SimpleNamespace(data=SimpleNamespace(success=True, action_description="clicked Add to cart"))

    async def nothing(*args, **kwargs):
        return None

    reads = list(counts)

    async def item_count(page):
        return reads.pop(0) if len(reads) > 1 else reads[0]

    async def click_direct(page):
        calls["direct"] += 1
        return direct

    async def first_visible(page, selectors):
        calls["visible"].append(selectors)
        return visible

    async def fetch(page, path):
        return cart

    for name in ("resolve_product_url", "settle", "maybe_dismiss_popups", "wait_until"):
        monkeypatch.setattr(coffee, name, nothing)
    monkeypatch.setattr(coffee, "shopify_item_count", item_count)
    monkeypatch.setattr(coffee, "click_shopify_add_button", click_direct)
    monkeypatch.setattr(coffee, "first_visible_choice", first_visible)
    monkeypatch.setattr(coffee, "fetch_json", fetch)
    monkeypatch.setattr(coffee, "CART_POLL_INTERVAL_MS", 1)
    monkeypatch.setattr(coffee, "CART_POLL_TIMEOUT_MS", 5)
    product = coffee.Product(name="Maple", price="$22.00", sold_out=False, is_coffee=True, url=None)
    item = asyncio.run(coffee.add_product_to_cart(Stagehand(), SimpleNamespace(), product, "https://shop.test/all"))
    return item, calls


def test_coffee_add_failure_when_nothing_is_found(monkeypatch):
    item, _ = _add_to_cart(monkeypatch, observed=[], counts=[0])
    assert item["status"] == "add_failed"
    assert item["add_detail"] == "no matching element found"
    assert item["add_selector"] == ""
    assert item["cart_count"] == [0, None]  # no click, so no second read


def test_coffee_add_failure_when_the_cart_does_not_grow(monkeypatch):
    button = SimpleNamespace(description="Add to cart : Maple", selector="xpath=//form/button", method="click")
    item, _ = _add_to_cart(monkeypatch, observed=[button], counts=[2, 2])
    assert item["status"] == "add_failed"
    assert item["cart_count"] == [2, 2]
    assert item["add_selector"] == "xpath=//form/button"
    assert item["add_detail"] == "clicked Add to cart"


def test_coffee_add_records_an_unreadable_cart(monkeypatch):
    button = SimpleNamespace(description="Add to cart", selector="xpath=//button", method="click")
    item, calls = _add_to_cart(monkeypatch, observed=[button], counts=[None, None])
    assert item["status"] == "added"  # the cart check is skipped, as before
    assert item["cart_count"] == [None, None]
    assert calls["direct"] == 0  # no /cart.js, so not Shopify: no direct click


FORM_BUTTON = coffee.Acted(True, "Clicked the product form's 'Add to cart' button", "css=button#BuyButtons-add")
MODEL_BUTTON = SimpleNamespace(description="Add to cart : Maple", selector="xpath=//form/button", method="click")


def test_coffee_add_clicks_the_product_form_button_without_the_model(monkeypatch):
    item, calls = _add_to_cart(monkeypatch, observed=[MODEL_BUTTON], counts=[0, 0, 1], direct=FORM_BUTTON)
    assert item["status"] == "added"
    assert calls["observe"] == 0 and calls["acted"] == []
    assert item["add_selector"] == "css=button#BuyButtons-add"
    assert item["add_detail"] == FORM_BUTTON.description
    assert item["cart_count"] == [0, 1]
    assert "subscription_added" not in item


def test_coffee_add_asks_the_model_when_there_is_no_form_button(monkeypatch):
    item, calls = _add_to_cart(monkeypatch, observed=[MODEL_BUTTON], counts=[0, 1], direct=None)
    assert item["status"] == "added"
    assert calls["direct"] == 1 and calls["observe"] == 1
    assert calls["acted"] == ["xpath=//form/button"]
    assert item["cart_count"] == [0, 1]


def test_coffee_add_asks_the_model_when_the_form_button_did_not_add(monkeypatch):
    # 0 before; the direct click's poll sees 0 five times; 0 again just before the model's click; then 1
    counts = [0, 0, 0, 0, 0, 0, 0, 1]
    item, calls = _add_to_cart(monkeypatch, observed=[MODEL_BUTTON], counts=counts, direct=FORM_BUTTON)
    assert item["status"] == "added"
    assert calls["observe"] == 1 and calls["acted"] == ["xpath=//form/button"]
    assert item["add_selector"] == "xpath=//form/button"  # what actually added it
    assert item["cart_count"] == [0, 1]


def test_coffee_add_skips_a_hidden_first_match(monkeypatch):
    sticky = SimpleNamespace(
        description="Quick add to cart bar button", selector="xpath=//sticky-add-to-cart/div/button", method="click"
    )
    item, calls = _add_to_cart(monkeypatch, observed=[sticky, MODEL_BUTTON], counts=[0, 1], visible=1)
    assert calls["visible"] == [["xpath=//sticky-add-to-cart/div/button", "xpath=//form/button"]]
    assert calls["acted"] == ["xpath=//form/button"]
    assert item["add_selector"] == "xpath=//form/button"


def test_coffee_add_acts_on_the_first_match_when_none_looks_visible(monkeypatch):
    other = SimpleNamespace(description="Add", selector="xpath=//div/button", method="click")
    _, calls = _add_to_cart(monkeypatch, observed=[MODEL_BUTTON, other], counts=[0, 1], visible=None)
    assert calls["acted"] == ["xpath=//form/button"]  # as before the filter


def test_coffee_add_flags_a_subscription_line(monkeypatch):
    cart = {
        "item_count": 1,
        "items": [{"product_title": "Maple", "selling_plan_allocation": {"selling_plan": {"name": "Every 2 weeks"}}}],
    }
    item, _ = _add_to_cart(monkeypatch, observed=[], counts=[0, 1], direct=FORM_BUTTON, cart=cart)
    assert item["status"] == "added"  # recorded, not undone
    assert item["subscription_added"] is True


def test_subscription_line():
    one_time = {"items": [{"product_title": "Maple", "selling_plan_allocation": None}]}
    assert coffee.subscription_line(one_time, "Maple") is None
    assert coffee.subscription_line(None, "Maple") is None
    older = {"items": [{"product_title": "Aspen"}, {"product_title": "Maple", "selling_plan_allocation": {}}]}
    assert coffee.subscription_line(older, "Aspen") is None  # an empty allocation is no plan
    plan = {
        "items": [
            {"product_title": "Aspen"},
            {"product_title": "Maple", "selling_plan_allocation": {"selling_plan": {}}},
        ]
    }
    assert coffee.subscription_line(plan, "Maple Coffee") == "Maple"


def _poll(monkeypatch, counts, before):
    reads = list(counts)
    seen = []

    async def item_count(page):
        seen.append(reads[0])
        return reads.pop(0) if len(reads) > 1 else reads[0]

    monkeypatch.setattr(coffee, "shopify_item_count", item_count)
    monkeypatch.setattr(coffee, "CART_POLL_INTERVAL_MS", 1)
    monkeypatch.setattr(coffee, "CART_POLL_TIMEOUT_MS", 10)
    return asyncio.run(coffee.wait_for_cart_growth(SimpleNamespace(), before)), seen


def test_cart_poll_stops_as_soon_as_the_count_goes_up(monkeypatch):
    count, seen = _poll(monkeypatch, [2, 2, 3, 9], before=2)
    assert count == 3 and seen == [2, 2, 3]


def test_cart_poll_gives_up_after_the_time_limit(monkeypatch):
    count, seen = _poll(monkeypatch, [2], before=2)
    assert count == 2 and len(seen) == 10  # 10 ms at one read per ms


def test_cart_poll_reads_once_when_the_cart_is_unreadable(monkeypatch):
    assert _poll(monkeypatch, [None], before=None) == (None, [None])


def test_cart_poll_reads_through_a_navigation(monkeypatch):
    count, seen = _poll(monkeypatch, [2, None, 3], before=2)  # e.g. the theme goes to /cart after adding
    assert count == 3 and seen == [2, None, 3]


# --- qantas: flight selection is three fixed steps, each checked -----------------------------------------
class SelectionSite:
    """The results page, fare panel and trip summary as select_flight sees them, driven by what is clicked.

    `broken` names steps whose click does nothing on screen; `offers` maps a step to the observe results
    returned for it (default: one element that does what the step asks).
    """

    PAGES: ClassVar[dict] = {
        "results": {"url": "https://book.qantas.test/flights", "heading": "Flights", "results": True},
        "fares": {"url": "https://book.qantas.test/flights", "heading": "Flights", "results": True, "fare_panel": True},
        "trip": {"url": "https://book.qantas.test/flights", "heading": "Flights", "trip_summary": True,
                 "summary_page": True},
        "passengers": {"url": "https://book.qantas.test/passengers", "heading": "Passenger details"},
    }  # fmt: skip
    NEXT: ClassVar[dict] = {"results": "fares", "fares": "trip", "trip": "passengers"}
    STEP: ClassVar[dict] = {"results": 1, "fares": 2, "trip": 3}

    def __init__(self, broken=(), offers=None):
        self.screen, self.broken, self.offers = "results", set(broken), offers or {}
        self.observed, self.clicked, self.extracted = [], [], []

    async def observe(self, instruction, **options):
        step = self.STEP.get(self.screen)
        self.observed.append(step)
        default = [SimpleNamespace(description=f"step {step} target", selector=f"xpath=/step{step}", method="click",
                                   arguments=[])]  # fmt: skip
        return SimpleNamespace(data=self.offers.get(step, default))

    async def act(self, action, **options):
        self.clicked.append(action.description)
        step = self.STEP.get(self.screen)
        if step not in self.broken:
            self.screen = self.NEXT[self.screen]
        return SimpleNamespace(data=SimpleNamespace(success=True, action_description=action.description))

    async def extract(self, instruction, schema, **options):
        self.extracted.append(schema.__name__)
        if schema is qantas.FarePanel:
            return SimpleNamespace(data=qantas.FarePanel(fares=[
                qantas.FareOption(fare_name="Economy Flex", price="$995"),
                qantas.FareOption(fare_name="Economy Saver", price="$529")]))  # fmt: skip
        return SimpleNamespace(data=qantas.SelectedFare(
            flight_selected=True, flight="QF2310", fare_name="Economy Saver", fare_price="$529",
            running_total="$529 AUD", subtotal="$529 AUD"))  # fmt: skip

    async def js(self, page, fn, arg=None, default=None, lib=""):
        if fn is qantas.JS_SELECTION_PAGE:
            return dict(self.PAGES[self.screen])
        return default

    async def fingerprint(self, page):
        return self.screen


def select(monkeypatch, tmp_path, site):
    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(qantas, "js", site.js)
    monkeypatch.setattr(qantas, "fingerprint", site.fingerprint)
    for name in ("settle", "shot", "try_act"):
        monkeypatch.setattr(qantas, name, nothing)
    flight = qantas.Flight(flight_number="QF2310", departs="12:35", arrives="16:00", stops=0, duration=None,
                           price="$529", sold_out=False, fares=[])  # fmt: skip
    return asyncio.run(qantas.select_flight(site, object(), flight, tmp_path))


def test_qantas_selects_the_flight_in_three_checked_steps(monkeypatch, tmp_path):
    site = SelectionSite()
    item = select(monkeypatch, tmp_path, site)
    assert site.screen == "passengers"
    assert site.observed == [1, 2, 3]
    assert site.clicked == ["step 1 target", "step 2 target", "step 3 target"]
    assert site.extracted == ["FarePanel", "SelectedFare"]
    assert item["status"] == "added"
    assert (item["selected_variant"], item["selected_price"], item["cart_total_display"]) == (
        "Economy Saver", 529, "$529 AUD")  # fmt: skip
    assert [(s["step"], s["passed"], s["selector"]) for s in item["selection_steps"]] == [
        (1, True, "xpath=/step1"), (2, True, "xpath=/step2"), (3, True, "xpath=/step3")]  # fmt: skip


@pytest.mark.parametrize(
    ("step", "message", "status", "item_status"),
    [
        (1, "Fare panel never opened", Status.ADD_TO_CART_FAILED, "add_failed"),
        (2, "Fare not added to trip", Status.ADD_TO_CART_FAILED, "add_failed"),
        (3, "Continue did not advance", Status.NO_CHECKOUT, "added"),
    ],
)
def test_qantas_retries_a_failed_selection_step_once(monkeypatch, tmp_path, step, message, status, item_status):
    site = SelectionSite(broken={step})
    with pytest.raises(qantas.SelectionFailed, match=message) as failed:
        select(monkeypatch, tmp_path, site)
    assert site.observed == [*range(1, step), step, step]  # the failing step ran twice, nothing after it
    assert failed.value.status == status and failed.value.step == step
    assert failed.value.item["status"] == item_status
    attempts = [s for s in failed.value.item["selection_steps"] if s["step"] == step]
    assert [(s["attempt"], s["passed"], s["page_changed"]) for s in attempts] == [(1, False, False), (2, False, False)]


def test_qantas_never_clicks_login_to_skip_to_payment(monkeypatch, tmp_path):
    login = SimpleNamespace(description="LOGIN TO SKIP TO PAYMENT button", selector="xpath=/login", method="click",
                            arguments=[])  # fmt: skip
    site = SelectionSite(offers={3: [login]})
    with pytest.raises(qantas.SelectionFailed, match="Continue did not advance"):
        select(monkeypatch, tmp_path, site)
    assert "LOGIN TO SKIP TO PAYMENT button" not in site.clicked
    assert site.screen == "trip"
