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
        return Acted(True, "clicked continue", "xpath=//button")

    async def nothing(*args, **kwargs):
        return None

    class Page:
        async def url(self):
            return "https://book.air.test/dx/#/extras"

    async def fake_fingerprint(page):
        return next(screens)

    monkeypatch.setattr(qantas, "fingerprint", fake_fingerprint)
    monkeypatch.setattr(qantas, "llm_act", fake_llm_act)
    for name in ("wait_for_render", "shot", "settle", "read_insurance", "read_accept"):
        monkeypatch.setattr(qantas, name, nothing)
    run = SiteResult(site="s", out_dir=tmp_path)
    reached = asyncio.run(qantas.walk_to_payment(Stagehand(), Page(), Passenger(), run))
    assert reached is False
    assert run.message.startswith("Stuck on 'Extras'")
    assert extracts == ["StepState"]  # one read; the three repeats cost no extraction (was four reads)
    assert len(actions) == 3
    assert len(run.steps) == 1
    assert run.steps[0]["action"] == "clicked continue | then: clicked continue | then: clicked continue"


# --- qantas: a custom dropdown that opens but is never chosen from does not eat the whole search ---------
class TripWidget:
    """The page's trip-type combobox, faked at the js()/click_at() boundary. None: the page has none."""

    def __init__(self, trip="return", *, ignores_mouse=False, results=False, route=None, points=None):
        self.trip, self.open, self.ignores_mouse = trip, False, ignores_mouse
        self.events = []
        # results: page_shows_results answers (a bool, or a list read in order, the last repeating);
        # route: the "<City> to <City>" subheading; points: the Rewards switch (None: no switch, else on/off)
        self.results, self.route, self.points = results, route, points

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
        if fn in (qantas.JS_RESULTS_PAGE, qantas.JS_RESULTS_ROUTE, qantas.JS_POINTS_SWITCH):
            return self.results_page(fn, default)
        if fn is qantas.JS_DOM_CLICK:
            self.events.append(("dom_click", arg))
            if arg == "data-audit-trip-option" and self.open:
                self.trip, self.open = "one_way", False
            return True
        raise AssertionError(f"unexpected script {fn[:40]!r}")

    def results_page(self, fn, default):
        if fn is qantas.JS_RESULTS_PAGE:
            self.events.append("results?")
            if isinstance(self.results, list):
                return self.results.pop(0) if len(self.results) > 1 else self.results[0]
            return self.results
        if fn is qantas.JS_RESULTS_ROUTE:
            return self.route or default
        if self.points is None:  # JS_POINTS_SWITCH
            return {"found": False, "on": False}
        was_on, self.points = self.points, False
        self.events.append(("points_off", was_on))
        return {"found": True, "on": was_on}

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


def _search(monkeypatch, *, states, selectors, widget=None, description="Trip Type combobox", page=None, waits=None):
    """Run qantas.search_flights with scripted SearchState reads and the controls the model acts on, in order."""
    reads, picks = iter(states), iter(selectors)
    calls = []
    waits = [] if waits is None else waits

    async def fake_wait_for_render(page, seconds=20):
        waits.append(seconds)
        return True

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
    monkeypatch.setattr(qantas, "wait_for_render", fake_wait_for_render)
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
    # The results page has no form: the model reads the cities from its heading ("Brisbane to Perth").
    cities = _form(on_results_page=True, origin="Brisbane", destination="Perth")
    assert qantas.wrong_results(cities, BNE_PER, normal) is None
    assert qantas.wrong_results(_form(origin="brisbane (BNE)"), BNE_PER, normal) is None
    assert qantas.wrong_results(_form(origin="Sydney"), BNE_PER, normal) == "origin shows 'Sydney', not BNE"
    assert qantas.wrong_results(_form(destination="Perth"), BNE_PER, normal) is None
    unknown = qantas.Trip("BNE", "MKY", qantas.date(2027, 2, 14))  # Mackay: not in the map, so no city is guessed
    assert qantas.wrong_results(_form(destination="Mackay"), unknown, normal) == "destination shows 'Mackay', not MKY"
    assert qantas.wrong_results(_form(destination="Mackay (MKY)"), unknown, normal) is None
    assert not qantas.shows_airport("Perthshire", "PER")  # whole words only


def test_qantas_search_takes_the_results_page_from_the_page_itself(monkeypatch):
    """The page shows flights: no model action, no restart, even though the model saw neither form nor results."""
    widget = TripWidget(None, results=True, route={"origin": "Brisbane", "destination": "Perth"})
    page = SearchPage()
    nothing = _form(form_visible=False, reason="nothing visible")
    calls, _ = _search(monkeypatch, states=[nothing], selectors=[], widget=widget, page=page)
    assert calls == [] and page.visited == []


def test_qantas_search_accepts_the_route_the_results_page_shows(monkeypatch):
    # The model reads "Brisbane" / "Perth" on the results page; that is our search, not a wrong page.
    page = SearchPage()
    states = [_form("one_way", on_results_page=True, form_visible=False, origin="Brisbane", destination="Perth")]
    calls, _ = _search(monkeypatch, states=states, selectors=[], page=page)
    assert calls == [] and page.visited == []


def test_qantas_search_waits_while_the_results_render(monkeypatch):
    """Nothing visible right after submit is a page still rendering: wait for it, never act on it."""
    widget = TripWidget(None, results=[False, True])  # loop check, then after the wait
    waits: list[int] = []
    nothing = _form("one_way", form_visible=False, reason="nothing visible")
    calls, _ = _search(monkeypatch, states=[nothing], selectors=[], widget=widget, waits=waits)
    assert calls == []
    assert waits == [qantas.RESULTS_RENDER_SECONDS]  # one wait in wait_for_results, then the results showed


def test_qantas_search_gives_the_results_a_last_chance_before_failing(monkeypatch):
    widget = TripWidget(None, results=[False] * 4 + [True])  # loop, three waits, the last look
    nothing = _form("one_way", form_visible=False, reason="nothing visible")
    calls, _ = _search(monkeypatch, states=[nothing] * 4, selectors=[], widget=widget)
    assert calls == []  # waited three times in wait_for_results, then once more before giving up: it showed


def test_qantas_search_turns_the_points_switch_off_in_code(monkeypatch):
    widget = TripWidget(None, points=True)
    states = [_form("one_way", using_points=True), _form("one_way", on_results_page=True)]
    calls, _ = _search(monkeypatch, states=states, selectors=["xpath=//button"], widget=widget)
    assert ("points_off", True) in widget.events
    assert "using points=False" in calls[0][0]
    assert "Never touch the Rewards" in calls[0][0]
    assert "turn it off now" not in calls[0][0]


def test_qantas_search_leaves_the_points_switch_to_the_model_when_it_cannot_be_found(monkeypatch):
    states = [_form("one_way", using_points=True), _form("one_way", on_results_page=True)]
    calls, _ = _search(monkeypatch, states=states, selectors=["xpath=//button"])
    assert "The Rewards / points switch is on: turn it off now, and do nothing else." in calls[0][0]


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
        # The sidebar still says "Change this flight" and "Subtotal", but the trip page's login button is gone.
        "options": {"url": "https://book.qantas.test/flights", "heading": "Options", "options": True},
    }  # fmt: skip
    NEXT: ClassVar[dict] = {"results": "fares", "fares": "trip", "trip": "passengers"}
    STEP: ClassVar[dict] = {"results": 1, "fares": 2, "trip": 3}

    def __init__(self, broken=(), offers=None, after=None, slow=0, fails=(), select=None, escape_closes=False):
        """`after`: the page Continue leads to; `slow`: page reads before a click shows; `fails`: steps whose
        click raises (the element re-rendered) although it takes effect. `select`: the fare panel's SELECT
        buttons are found in code, and these clicks on them add the fare ('mouse', 'dom'; None: no buttons
        found, so the model is asked). `escape_closes`: Escape closes the open fare panel."""
        self.screen, self.broken, self.offers = "results", set(broken), offers or {}
        self.next = {**self.NEXT, "trip": after or "passengers"}
        self.slow, self.fails, self.pending = slow, set(fails), None
        self.select, self.escape_closes = select, escape_closes
        self.observed, self.clicked, self.extracted, self.reads = [], [], [], 0
        self.events = []

    # The browser page select_flight talks to directly.
    async def key_press(self, key, delay=None):
        self.events.append(("key", key))
        if self.escape_closes and self.screen == "fares":
            self.screen = "results"

    async def url(self):
        return self.PAGES[self.screen]["url"]

    async def click_at(self, page, x, y):
        self.events.append(("click", x, y))
        if self.screen == "fares" and "mouse" in (self.select or ()):
            self.screen = "trip"

    def fare_buttons(self):
        """The fare panel's buttons as JS_FARE_BUTTONS lists them: a hidden duplicate, Red e-Deal's SELECT, Flex's
        SELECT, the points fare's LOG IN TO SELECT."""
        cell = {"visible": True, "box": [100, 500, 120, 40], "in_view": True, "hit": True}
        return [
            {**cell, "i": 0, "text": "SELECT", "column": "ECONOMY RED E-DEAL $529 SELECT", "visible": False,
             "box": [0, 0, 0, 0], "in_view": False, "hit": False},
            {**cell, "i": 1, "text": "SELECT", "column": "ECONOMY RED E-DEAL $529 SELECT 1 left at this price"},
            {**cell, "i": 2, "text": "SELECT", "column": "ECONOMY FLEX $639 SELECT", "box": [300, 500, 120, 40]},
            {**cell, "i": 3, "text": "LOG IN TO SELECT", "column": "ECONOMY CLASSIC PLUS 50,400 PTS + $77"},
        ]  # fmt: skip

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
            self.pending = [self.next[self.screen], self.slow]
            self.swap()
        if step in self.fails:
            raise RuntimeError("Could not find an element for the given xPath(s): /html/body/app/nav/button")
        return SimpleNamespace(data=SimpleNamespace(success=True, action_description=action.description))

    def swap(self):
        """The view changes once `slow` page reads have passed since the click."""
        if self.pending and self.pending[1] <= 0:
            self.screen, self.pending = self.pending[0], None
        elif self.pending:
            self.pending[1] -= 1

    async def extract(self, instruction, schema, **options):
        self.extracted.append(schema.__name__)
        if schema is qantas.FarePanel:
            return SimpleNamespace(data=qantas.FarePanel(fares=[
                qantas.FareOption(fare_name="ECONOMY FLEX", price="$639"),
                qantas.FareOption(fare_name="ECONOMY RED E-DEAL", price="$529"),
                qantas.FareOption(fare_name="ECONOMY CLASSIC PLUS", price="50,400 PTS + $77")]))  # fmt: skip
        return SimpleNamespace(data=qantas.SelectedFare(
            flight_selected=True, flight="QF2310", fare_name="Economy Red e-Deal", fare_price="$529",
            running_total="$529 AUD", subtotal="$529 AUD"))  # fmt: skip

    async def js(self, page, fn, arg=None, default=None, lib=""):
        if fn is qantas.JS_SELECTION_PAGE:
            self.reads += 1
            page = dict(self.PAGES[self.screen])
            self.swap()
            return page
        if fn is qantas.JS_FARE_BUTTONS:
            return self.fare_buttons() if self.select is not None and self.screen == "fares" else default
        if fn is qantas.JS_MARK_FARE_SELECT:
            self.events.append(("mark", arg))
            box = next(b["box"] for b in self.fare_buttons() if b["i"] == arg)
            return {"x": box[0] + box[2] / 2, "y": box[1] + box[3] / 2}
        if fn is qantas.JS_DOM_CLICK:
            self.events.append(("dom_click", arg))
            if self.screen == "fares" and "dom" in (self.select or ()):
                self.screen = "trip"
            return True
        if fn is qantas.JS_DIAGNOSTICS:
            return {"url": self.PAGES[self.screen]["url"], "messages": []}
        return default

    async def fingerprint(self, page):
        return self.screen


def select(monkeypatch, tmp_path, site):
    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(qantas, "js", site.js)
    monkeypatch.setattr(qantas, "fingerprint", site.fingerprint)
    for name in ("settle", "shot", "try_act", "pause"):
        monkeypatch.setattr(qantas, name, nothing)
    monkeypatch.setattr(qantas, "click_at", site.click_at)
    flight = qantas.Flight(flight_number="QF2310", departs="12:35", arrives="16:00", stops=0, duration=None,
                           price="$529", sold_out=False, fares=[])  # fmt: skip
    return asyncio.run(qantas.select_flight(site, site, flight, tmp_path))


def test_qantas_selects_the_flight_in_three_checked_steps(monkeypatch, tmp_path):
    site = SelectionSite()
    item = select(monkeypatch, tmp_path, site)
    assert site.screen == "passengers"
    assert site.observed == [1, 2, 3]
    assert site.clicked == ["step 1 target", "step 2 target", "step 3 target"]
    assert site.extracted == ["FarePanel", "SelectedFare"]
    assert item["status"] == "added"
    assert (item["selected_variant"], item["selected_price"], item["cart_total_display"]) == (
        "Economy Red e-Deal", 529, "$529 AUD")  # fmt: skip
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


# --- qantas: Continue to the Options page, and the travel insurance question -----------------------------
def test_qantas_continue_waits_for_the_options_page_and_clicks_once(monkeypatch, tmp_path):
    site = SelectionSite(after="options", slow=3)  # the trip page still shows for the first reads after the click
    item = select(monkeypatch, tmp_path, site)
    assert site.screen == "options"
    assert site.observed == [1, 2, 3]  # CONTINUE observed (and clicked) once
    assert [(s["step"], s["attempt"], s["passed"]) for s in item["selection_steps"]][-1] == (3, 1, True)


def test_qantas_a_click_that_failed_as_the_page_re_rendered_is_judged_by_the_page(monkeypatch, tmp_path):
    site = SelectionSite(after="options", fails={3})
    item = select(monkeypatch, tmp_path, site)
    assert site.observed == [1, 2, 3]
    last = item["selection_steps"][-1]
    assert (last["step"], last["attempt"], last["passed"], last["acted"]) == (3, 1, True, False)
    assert last["description"].startswith("click failed")


def test_qantas_a_retry_rereads_the_page_before_clicking_again(monkeypatch, tmp_path):
    # Slower than the whole check: the page shows Options only once the first attempt has given up.
    monkeypatch.setattr(qantas, "STEP_CHECK_MS", qantas.STEP_POLL_MS * 2)
    site = SelectionSite(after="options", slow=3)
    item = select(monkeypatch, tmp_path, site)
    assert site.observed == [1, 2, 3]  # no second CONTINUE
    assert [(s["attempt"], s["passed"]) for s in item["selection_steps"] if s["step"] == 3] == [(1, False), (2, True)]


@pytest.mark.parametrize("label", ["USE CASH button", "USE POINTS button", "UPGRADE MY FLIGHTS button"])
def test_qantas_click_one_refuses_the_options_page_s_offers(label):
    offer = SimpleNamespace(description=label, selector="xpath=/offer", method="click", arguments=[])

    class Stagehand:
        acted = False

        async def observe(self, instruction, **options):
            return SimpleNamespace(data=[offer])

        async def act(self, action, **options):
            Stagehand.acted = True

    acted = asyncio.run(qantas.click_one(Stagehand(), "Click the red CONTINUE button"))
    assert not acted.success and acted.description.startswith("refused") and not Stagehand.acted


class InsuranceBox:
    """The 'No, I don't want travel insurance' checkbox at the js()/click_at() boundary."""

    def __init__(self, checked=False, takes=("mouse",), found=True, warning=False):
        self.checked, self.takes, self.found, self.warning = checked, set(takes), found, warning
        self.events = []

    async def js(self, page, fn, arg=None, default=None, lib=""):
        if fn is qantas.JS_INSURANCE:
            seen = {"question": True, "warning": self.warning and not self.checked, "found": self.found,
                    "checked": self.checked}  # fmt: skip
            return {**seen, "x": 40, "y": 400} if arg and self.found else seen
        if fn is qantas.JS_DOM_CLICK:
            self.events.append("dom_click")
            self.checked = self.checked or "dom" in self.takes
            return True
        if fn is qantas.JS_ACCEPT:
            return default  # no fare rules on this page
        raise AssertionError(f"unexpected script {fn[:40]!r}")

    async def click_at(self, page, x, y):
        self.events.append(("click", x, y))
        self.checked = self.checked or "mouse" in self.takes


def _decline(monkeypatch, box):
    async def nothing(*args, **kwargs):
        return None

    async def fake_llm_act(stagehand, instruction, **options):
        box.events.append(("model", instruction))
        box.checked = box.checked or "model" in box.takes
        return Acted(True, "ticked the box")

    monkeypatch.setattr(qantas, "js", box.js)
    monkeypatch.setattr(qantas, "click_at", box.click_at)
    monkeypatch.setattr(qantas, "llm_act", fake_llm_act)
    monkeypatch.setattr(qantas, "settle", nothing)
    return asyncio.run(qantas.decline_travel_insurance(object(), object()))


def test_qantas_declines_travel_insurance_with_a_mouse_click(monkeypatch):
    box = InsuranceBox(warning=True)
    assert _decline(monkeypatch, box) is True
    assert box.events == [("click", 40, 400)]  # no DOM click, no model


def test_qantas_declining_insurance_falls_back_to_a_dom_click_then_the_model(monkeypatch):
    box = InsuranceBox(takes=())
    assert _decline(monkeypatch, box) is False
    assert box.events[:2] == [("click", 40, 400), "dom_click"]
    assert box.events[2] == ("model", qantas.DECLINE_INSURANCE)
    assert "Use cash" in qantas.DECLINE_INSURANCE and "Use points" in qantas.DECLINE_INSURANCE
    assert _decline(monkeypatch, InsuranceBox(takes=("dom",))) is True


def test_qantas_never_unticks_insurance_that_is_already_declined(monkeypatch):
    box = InsuranceBox(checked=True)
    assert _decline(monkeypatch, box) is True
    assert box.events == []


def test_qantas_asks_the_model_when_the_insurance_box_cannot_be_found(monkeypatch):
    box = InsuranceBox(found=False, takes=("model",))
    assert _decline(monkeypatch, box) is True
    assert box.events == [("model", qantas.DECLINE_INSURANCE)]


def _options_step(monkeypatch, tmp_path, *, box, screens, steps):
    """walk_to_payment over scripted steps (StepState per read), with the insurance box on the page."""
    events, reads = box.events, iter(steps)

    class Stagehand:
        async def extract(self, instruction, schema, **options):
            events.append("extract")
            return SimpleNamespace(data=next(reads))

    async def fake_llm_act(stagehand, instruction, **options):
        events.append(("act", instruction))
        return Acted(True, "clicked continue")

    async def nothing(*args, **kwargs):
        return None

    class Page:
        async def url(self):
            return "https://book.qantas.test/options"

    screens = iter(screens)

    async def fake_fingerprint(page):
        return next(screens)

    monkeypatch.setattr(qantas, "fingerprint", fake_fingerprint)
    monkeypatch.setattr(qantas, "llm_act", fake_llm_act)
    monkeypatch.setattr(qantas, "js", box.js)
    monkeypatch.setattr(qantas, "click_at", box.click_at)
    for name in ("wait_for_render", "shot", "settle"):
        monkeypatch.setattr(qantas, name, nothing)
    run = SiteResult(site="s", out_dir=tmp_path)
    return run, asyncio.run(qantas.walk_to_payment(Stagehand(), Page(), Passenger(), run))


def _step(step_type, heading, offers=(), payment=False):
    return qantas.StepState(step_type=step_type, heading=heading, is_payment_page=payment, blocked_reason=None,
                            offers=list(offers), price_lines=[], total="$529.00", validation_errors=[],
                            summary=heading)  # fmt: skip


OPTIONS_OFFERS = [
    qantas.Offer(category="fare_upgrade", label="Upgrade to Business", price="from $720", price_basis=None,
                 selected=False, mandatory=False),
    qantas.Offer(category="travel_insurance", label="Australian Comprehensive", price="$40",
                 price_basis="per adult", selected=False, mandatory=False),
]  # fmt: skip


def test_qantas_walk_declines_insurance_before_continuing(monkeypatch, tmp_path):
    box = InsuranceBox()
    steps = [_step("insurance", "Options", OPTIONS_OFFERS), _step("payment", "Payment", payment=True)]
    run, reached = _options_step(monkeypatch, tmp_path, box=box, screens=["options", "payment"], steps=steps)
    assert reached is True
    click = box.events.index(("click", 40, 400))
    continue_ = next(i for i, e in enumerate(box.events) if isinstance(e, tuple) and e[0] == "act")
    assert box.events.index("extract") < click < continue_  # read and recorded first, declined before Continue
    assert qantas.INSURANCE_DONE.strip() in box.events[continue_][1]
    assert {e["category"] for e in run.extras} == {"fare_upgrade", "travel_insurance"}  # still offered
    assert not any(e["selected_by_default"] for e in run.extras)


def test_qantas_walk_gives_up_when_the_insurance_answer_is_not_accepted(monkeypatch, tmp_path):
    box = InsuranceBox(takes=(), warning=True)  # the box never ticks; Continue keeps showing the warning
    steps = [_step("insurance", "Options", OPTIONS_OFFERS)] * 3
    run, reached = _options_step(monkeypatch, tmp_path, box=box, screens=["a", "b", "c"], steps=steps)
    assert reached is False
    assert run.message == "Stuck on 'Options': insurance selection not accepted"
    assert sum(1 for e in box.events if isinstance(e, tuple) and e[0] == "act") == 2  # one model tick, no Continue


# --- qantas: the Options page moves on with the fare rules' ACCEPT -------------------------------------
class FareRulesPage:
    """The bottom of the Options page: 'Fare rules' and its ACCEPT button, at the js()/click_at() boundary.

    `takes` lists the clicks that move the page on ('mouse', 'dom'); `has` is False for a page without them.
    """

    def __init__(self, takes=("mouse",), has=True):
        self.takes, self.has, self.screen = set(takes), has, "options"
        self.events = []

    async def js(self, page, fn, arg=None, default=None, lib=""):
        if fn is qantas.JS_ACCEPT:
            if not self.has or self.screen != "options":
                return default
            return {"text": "ACCEPT", **({"x": 1300, "y": 900} if arg else {})}
        if fn is qantas.JS_SELECTION_PAGE:
            return {"heading": "Options" if self.screen == "options" else "Passenger details"}
        if fn is qantas.JS_DOM_CLICK:
            self.events.append(("dom_click", arg))
            if "dom" in self.takes:
                self.screen = "passengers"
            return True
        if fn is qantas.JS_INSURANCE:  # the question is answered: the box is ticked, no warning
            return {"question": self.screen == "options", "warning": False, "found": True, "checked": True}
        raise AssertionError(f"unexpected script {fn[:40]!r}")

    async def click_at(self, page, x, y):
        self.events.append(("click", x, y))
        if "mouse" in self.takes:
            self.screen = "passengers"

    async def fingerprint(self, page):
        return self.screen


def _accept(monkeypatch, rules):
    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(qantas, "js", rules.js)
    monkeypatch.setattr(qantas, "click_at", rules.click_at)
    monkeypatch.setattr(qantas, "fingerprint", rules.fingerprint)
    monkeypatch.setattr(qantas, "pause", nothing)
    return asyncio.run(qantas.accept_fare_rules(object()))


def test_qantas_accepts_the_fare_rules_with_a_mouse_click(monkeypatch):
    rules = FareRulesPage()
    assert _accept(monkeypatch, rules) is True
    assert rules.events == [("click", 1300, 900)]  # no DOM click


def test_qantas_accepting_the_fare_rules_falls_back_to_a_dom_click(monkeypatch):
    rules = FareRulesPage(takes=("dom",))
    assert _accept(monkeypatch, rules) is True
    assert rules.events == [("click", 1300, 900), ("dom_click", "data-audit-accept")]


def test_qantas_accepting_the_fare_rules_gives_up_when_the_page_stays(monkeypatch):
    rules = FareRulesPage(takes=())
    assert _accept(monkeypatch, rules) is False
    assert rules.events == [("click", 1300, 900), ("dom_click", "data-audit-accept")]


def test_qantas_does_not_click_accept_without_fare_rules(monkeypatch):
    rules = FareRulesPage(has=False)
    assert _accept(monkeypatch, rules) is False
    assert rules.events == []


def test_qantas_accept_is_never_refused_or_forbidden():
    assert not qantas.NEVER_CLICK.search("ACCEPT button under Fare rules")
    assert "Accepting fare rules" in qantas.BOOKING_GOAL and "not a purchase" in qantas.BOOKING_GOAL
    assert "ACCEPT" in qantas.INSURANCE_DONE and "does not pay" in qantas.INSURANCE_DONE


def _walk(monkeypatch, tmp_path, rules, steps):
    events, reads = rules.events, iter(steps)

    class Stagehand:
        async def extract(self, instruction, schema, **options):
            events.append("extract")
            return SimpleNamespace(data=next(reads))

    async def fake_llm_act(stagehand, instruction, **options):
        events.append(("act", instruction))
        return Acted(True, "clicked the Continue button", "xpath=/html/body/nav/button")

    async def nothing(*args, **kwargs):
        return None

    class Page:
        async def url(self):
            return "https://book.qantas.test/qf-booking/dyn/air/booking/FDRF"

    monkeypatch.setattr(qantas, "llm_act", fake_llm_act)
    monkeypatch.setattr(qantas, "js", rules.js)
    monkeypatch.setattr(qantas, "click_at", rules.click_at)
    monkeypatch.setattr(qantas, "fingerprint", rules.fingerprint)
    for name in ("wait_for_render", "shot", "settle", "pause"):
        monkeypatch.setattr(qantas, name, nothing)
    run = SiteResult(site="s", out_dir=tmp_path)
    return run, asyncio.run(qantas.walk_to_payment(Stagehand(), Page(), Passenger(), run))


def test_qantas_walk_accepts_the_fare_rules_in_code_instead_of_asking_the_model(monkeypatch, tmp_path):
    rules = FareRulesPage()
    steps = [_step("review", "Options", OPTIONS_OFFERS), _step("payment", "Payment", payment=True)]
    run, reached = _walk(monkeypatch, tmp_path, rules, steps)
    assert reached is True
    assert not any(isinstance(e, tuple) and e[0] == "act" for e in rules.events)  # the model never acted
    assert rules.events.index(("click", 1300, 900)) > rules.events.index("extract")  # after the step was read
    assert (run.steps[0]["action"], run.steps[0]["selector"]) == (
        "Fare rules: accepted (ACCEPT clicked in code)", "[data-audit-accept]")  # fmt: skip
    assert {e["category"] for e in run.extras} == {"fare_upgrade", "travel_insurance"}


def test_qantas_walk_tells_the_model_to_click_accept_when_code_could_not(monkeypatch, tmp_path):
    rules = FareRulesPage(takes=())
    steps = [_step("review", "Options", OPTIONS_OFFERS)] * 2
    run, reached = _walk(monkeypatch, tmp_path, rules, steps)
    assert reached is False
    acts = [e[1] for e in rules.events if isinstance(e, tuple) and e[0] == "act"]
    assert acts and all(qantas.ACCEPT_FARE_RULES.strip() in a for a in acts)
    assert run.steps[0]["action"].startswith("clicked the Continue button")  # what the model clicked is kept
    assert " | then: " in run.steps[0]["action"]  # each repeat on the same page appended
    assert run.steps[0]["selector"] == "xpath=/html/body/nav/button"


# --- qantas: SELECT on the fare is clicked in code ----------------------------------------------------
def test_qantas_picks_the_visible_select_in_the_chosen_fare_s_column():
    buttons = SelectionSite().fare_buttons()
    assert qantas.pick_fare_select(buttons, "ECONOMY RED E-DEAL")["i"] == 1  # not the hidden copy (i=0)
    assert qantas.pick_fare_select(buttons, "Economy Flex")["i"] == 2
    assert qantas.pick_fare_select(buttons, "ECONOMY CLASSIC PLUS") is None  # LOG IN TO SELECT is never taken
    covered = [{**b, "hit": False} if b["i"] == 1 else b for b in buttons]  # under the sticky footer
    assert qantas.pick_fare_select(covered, "RED E-DEAL") is None
    off_screen = [{**b, "in_view": False} if b["i"] == 1 else b for b in buttons]
    assert qantas.pick_fare_select(off_screen, "red e-deal") is None
    assert qantas.fare_brand("ECONOMY RED E-DEAL").search("Economy Red e-Deal $529")
    assert not qantas.fare_brand("ECONOMY RED E-DEAL").search("ECONOMY FLEX $639")


def test_qantas_step_2_clicks_select_in_code_without_the_model(monkeypatch, tmp_path):
    site = SelectionSite(select=("mouse",))
    item = select(monkeypatch, tmp_path, site)
    assert item["status"] == "added"
    assert site.observed == [1, 3]  # the model picked no element for step 2
    assert ("mark", 1) in site.events and ("click", 160.0, 520.0) in site.events
    assert not any(e[0] == "dom_click" for e in site.events)
    step2 = next(s for s in item["selection_steps"] if s["step"] == 2)
    assert (step2["attempt"], step2["passed"], step2["code_click"], step2["button_text"]) == (1, True, True, "SELECT")
    assert step2["box"] == [100, 500, 120, 40] and step2["element_from_point"] is True
    assert step2["selector"] == qantas.FARE_SELECT_MARK


def test_qantas_step_2_falls_back_to_a_dom_click_on_the_same_button(monkeypatch, tmp_path):
    site = SelectionSite(select=("dom",))
    item = select(monkeypatch, tmp_path, site)
    assert item["status"] == "added"
    assert ("dom_click", "data-audit-fare-select") in site.events
    step2 = next(s for s in item["selection_steps"] if s["step"] == 2)
    assert (step2["attempt"], step2["passed"], step2["dom_click"]) == (1, True, True)


def test_qantas_step_2_asks_the_model_only_when_no_button_is_found(monkeypatch, tmp_path):
    site = SelectionSite()  # JS_FARE_BUTTONS finds nothing
    item = select(monkeypatch, tmp_path, site)
    assert site.observed == [1, 2, 3]
    assert next(s for s in item["selection_steps"] if s["step"] == 2)["code_click"] is False


def test_qantas_step_2_retry_starts_clean_and_reports_why(monkeypatch, tmp_path):
    site = SelectionSite(select=(), escape_closes=True)  # the site ignores every click on SELECT
    with pytest.raises(qantas.SelectionFailed, match="Fare not added to trip") as failed:
        select(monkeypatch, tmp_path, site)
    assert ("key", "Escape") in site.events
    assert site.observed == [1, 1]  # Escape closed the panel, so step 1 ran again to reopen it; no model SELECT
    marks = [e for e in site.events if e[0] == "mark"]
    assert marks == [("mark", 1), ("mark", 1)]  # the retry clicked the same button, found again
    attempts = [s for s in failed.value.item["selection_steps"] if s["step"] == 2]
    assert attempts[1]["escape"] is True and attempts[1]["reopened"] is True
    diagnostics = failed.value.item["step2_diagnostics"]
    assert diagnostics["url"] == "https://book.qantas.test/flights" and diagnostics["console"] == []


def test_qantas_console_messages_are_read_from_stagehand_events():
    event = SimpleNamespace(params=SimpleNamespace(root={"type": "error", "args": [
        {"type": "string", "value": "Failed to add fare"}, {"type": "object", "description": "Error: 409"}]}))  # fmt: skip
    assert qantas.console_text(event) == "error: Failed to add fare Error: 409"
    assert qantas.console_text(object()) == "log: "


def test_qantas_tries_the_next_cheapest_flight_when_a_fare_cannot_be_added(monkeypatch, tmp_path):
    tried = []

    async def fake_select_flight(stagehand, page, flight, out):
        tried.append(flight.departs)
        item = qantas.new_item(flight.departs, flight.price)
        if flight.departs == "12:35":
            item["status"] = "add_failed"
            raise qantas.SelectionFailed(2, "Fare not added to trip: no 'Your trip' summary", item)
        item["status"] = "added"
        return item

    def flight(departs, price):
        return qantas.Flight(flight_number=None, departs=departs, arrives=None, stops=0, duration=None, price=price,
                             sold_out=False, fares=[])  # fmt: skip

    monkeypatch.setattr(qantas, "select_flight", fake_select_flight)
    flights = qantas.rank_flights([flight("15:00", "$569"), flight("12:35", "$529"), flight("18:25", "$529"),
                                   flight("19:00", "16,600 PTS + $58")])  # fmt: skip
    assert [f.departs for f in flights] == ["12:35", "18:25", "15:00"]
    run = SiteResult(site="s", out_dir=tmp_path)
    item = asyncio.run(qantas.select_a_flight(object(), object(), flights, run, "per adult"))
    assert tried == ["12:35", "18:25"] and item["name"] == "18:25"
    assert [(i["name"], i["status"]) for i in run.items] == [("12:35", "add_failed"), ("18:25", "added")]
    assert [i["name"] for i in run.added] == ["18:25"]


def test_qantas_gives_up_after_the_second_flight_and_on_other_steps(monkeypatch, tmp_path):
    async def always_fails(stagehand, page, flight, out):
        raise qantas.SelectionFailed(2 if flight.departs != "07:00" else 3, "nope", qantas.new_item("x", None))

    def flight(departs, price):
        return qantas.Flight(flight_number=None, departs=departs, arrives=None, stops=0, duration=None, price=price,
                             sold_out=False, fares=[])  # fmt: skip

    monkeypatch.setattr(qantas, "select_flight", always_fails)
    run = SiteResult(site="s", out_dir=tmp_path)
    flights = [flight("12:35", "$529"), flight("18:25", "$529"), flight("15:00", "$569")]
    with pytest.raises(qantas.SelectionFailed):
        asyncio.run(qantas.select_a_flight(object(), object(), flights, run, None))
    assert len(run.items) == 2  # the cheapest and one more, never a third
    run = SiteResult(site="s", out_dir=tmp_path)
    with pytest.raises(qantas.SelectionFailed):  # step 3 (Continue) failing is not the fare's fault: no next flight
        asyncio.run(qantas.select_a_flight(object(), object(), [flight("07:00", "$1"), *flights], run, None))
    assert len(run.items) == 1
