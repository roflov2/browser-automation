"""Workflow logic that needs neither a browser nor a model."""

import argparse
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

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


# --- coffee: an add-to-cart failure says why in price_comparison.json ---------------------------------
def _add_to_cart(monkeypatch, *, observed, counts):
    """Run coffee.add_product_to_cart on a ready product page, with a fake model and a scripted /cart.js."""

    class Stagehand:
        async def extract(self, instruction, schema, **options):
            return SimpleNamespace(data=schema(
                is_product_page=True, has_purchase_options=False, one_time_selected=True, has_size_options=False,
                lowest_size_selected=True, selected_size="200g", lowest_available_size="200g",
                current_price="$22.00", reason=None,
            ))  # fmt: skip

        async def observe(self, instruction, **options):
            return SimpleNamespace(data=observed)

        async def act(self, action, **options):
            return SimpleNamespace(data=SimpleNamespace(success=True, action_description="clicked Add to cart"))

    async def nothing(*args, **kwargs):
        return None

    reads = iter(counts)

    async def item_count(page):
        return next(reads)

    for name in ("resolve_product_url", "settle", "maybe_dismiss_popups", "wait_until"):
        monkeypatch.setattr(coffee, name, nothing)
    monkeypatch.setattr(coffee, "shopify_item_count", item_count)
    product = coffee.Product(name="Maple", price="$22.00", sold_out=False, is_coffee=True, url=None)
    return asyncio.run(coffee.add_product_to_cart(Stagehand(), SimpleNamespace(), product, "https://shop.test/all"))


def test_coffee_add_failure_when_nothing_is_found(monkeypatch):
    item = _add_to_cart(monkeypatch, observed=[], counts=[0])
    assert item["status"] == "add_failed"
    assert item["add_detail"] == "no matching element found"
    assert item["add_selector"] == ""
    assert item["cart_count"] == [0, None]  # no click, so no second read


def test_coffee_add_failure_when_the_cart_does_not_grow(monkeypatch):
    button = SimpleNamespace(description="Add to cart : Maple", selector="xpath=//form/button", method="click")
    item = _add_to_cart(monkeypatch, observed=[button], counts=[2, 2])
    assert item["status"] == "add_failed"
    assert item["cart_count"] == [2, 2]
    assert item["add_selector"] == "xpath=//form/button"
    assert item["add_detail"] == "clicked Add to cart"


def test_coffee_add_records_an_unreadable_cart(monkeypatch):
    button = SimpleNamespace(description="Add to cart", selector="xpath=//button", method="click")
    item = _add_to_cart(monkeypatch, observed=[button], counts=[None, None])
    assert item["status"] == "added"  # the cart check is skipped, as before
    assert item["cart_count"] == [None, None]
