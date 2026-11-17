"""Workflow logic that needs neither a browser nor a model."""

import argparse
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from audit.config import Settings
from audit.core import SiteResult, Status
from audit.workflows import WORKFLOWS, coffee, virgin
from audit.workflows.coffee import CoffeeWorkflow
from audit.workflows.ubereats import UberEatsWorkflow
from audit.workflows.ubereats import models as uber_models
from audit.workflows.ubereats import store as uber_store
from audit.workflows.virgin import FieldToFill, Passenger, VirginWorkflow, passenger_field_instruction

REAL = Settings(pax_first_name="Samantha", pax_last_name="Okafor", pax_email="sam@real.example",
                pax_phone="0412 345 678", pax_date_of_birth="3 March 1991")  # fmt: skip


def test_every_workflow_has_a_safety_prompt():
    for workflow in WORKFLOWS:
        assert workflow.system_prompt, workflow.name
        assert "never" in workflow.system_prompt.lower()


# --- virgin: passenger details stay out of the model's input (REVIEW H7) -----------------------------
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
    assert VirginWorkflow(argparse.Namespace(), REAL).sensitive_values() == typed

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
            if "auditRefused" in expression:  # a page.js() snippet
                return {"value": None, "error": None, "refused": []}
            if "auditChain" in expression:  # safe_act asking what the selector points at
                return {"found": True, "labelled": True, "click": None, "field": None}
            return []  # draining the in-page blocker

        async def wait_for_load_state(self, *args, **kwargs):
            return None

        async def wait_for_function(self, *args, **kwargs):
            return True

    stagehand = Recorder()
    asyncio.run(virgin.fill_passenger_details(stagehand, Page(), passenger))
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

    virgin_flow = VirginWorkflow(argparse.Namespace(), settings)
    trip = result(search={"origin": "BNE", "destination": "PER"})
    assert (
        virgin_flow.target_key("https://www.virginaustralia.com/au/en/", trip)
        == "https://www.virginaustralia.com/au/en/ BNE-PER"
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


def test_virgin_picks_the_cheapest_direct_flight():
    def flight(price, stops=0, sold_out=False):
        return virgin.Flight(flight_number=None, departs="6:00am", arrives="7:35am", stops=stops, duration=None,
                             price=price, sold_out=sold_out, fares=[])  # fmt: skip

    assert (
        virgin.choose_flight(
            [flight("$99", stops=1), flight("$199"), flight("$149"), flight("$1", sold_out=True)]
        ).price
        == "$149"
    )
    assert virgin.choose_flight([flight(None), flight("$10", sold_out=True)]) is None


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
    assert workflow.clear_cookies in (None, "domain", "all")
    assert workflow.log_level in ("warn", "info")


# --- Phase 5: no extraction is paid for when an action changed nothing ----------------------------------
def test_virgin_reuses_the_step_when_the_screen_did_not_change(monkeypatch, tmp_path):
    screens = iter(["extras|A", "extras|A", "extras|A", "extras|A"])  # every action leaves the screen as it was
    extracts, actions = [], []

    def state():
        return virgin.StepState(step_type="extras", heading="Extras", is_payment_page=False, blocked_reason=None,
                                offers=[], price_lines=[], total="$199.00", validation_errors=[], summary="extras page")  # fmt: skip

    class Stagehand:
        async def extract(self, instruction, schema, **options):
            extracts.append(schema.__name__)
            return SimpleNamespace(data=state())

    async def fake_safe_act(stagehand, page, instruction, **options):
        actions.append(instruction)
        return SimpleNamespace(description="clicked continue", success=True)

    async def nothing(*args, **kwargs):
        return None

    class Page:
        async def url(self):
            return "https://book.air.test/dx/#/extras"

    async def fake_fingerprint(page):
        return next(screens)

    monkeypatch.setattr(virgin, "fingerprint", fake_fingerprint)
    monkeypatch.setattr(virgin, "safe_act", fake_safe_act)
    for name in ("wait_for_render", "shot", "settle"):
        monkeypatch.setattr(virgin, name, nothing)
    run = SiteResult(site="s", out_dir=tmp_path)
    reached = asyncio.run(virgin.walk_to_payment(Stagehand(), Page(), Passenger(), run))
    assert reached is False
    assert run.message.startswith("Stuck on 'Extras'")
    assert extracts == ["StepState"]  # one read; the three repeats cost no extraction (was four reads)
    assert len(actions) == 3
    assert len(run.steps) == 1
