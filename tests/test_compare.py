"""Characterisation tests: each workflow's compare_prices reproduces the reports of the saved runs.

The inputs are rebuilt from the display fields of the seven runs that used to be committed under
output/ (see tests/fixtures/saved_runs.json; URLs and the delivery address are left out).
"""

import copy
from pathlib import Path

import pytest

from audit.core import Fee, LineItem, SiteResult
from audit.workflows import coffee, virgin
from audit.workflows.ubereats import models as uber_models
from audit.workflows.ubereats import pricing as uber_pricing

# Report keys whose saved value was wrong on purpose-to-be-fixed grounds. Phase 4 changes them; until
# then the tests pin today's behaviour so that nothing else moves by accident.
CHANGES_IN_PHASE_4 = {
    "drip_detected": "None unless the final price screen was read (REVIEW H1)",
    "price_increased": "None when the item has no checkout line (REVIEW H1)",
}


def runs_for(saved_runs: list[dict], workflow: str) -> list[dict]:
    return [copy.deepcopy(r) for r in saved_runs if r["workflow"] == workflow]


def assert_report(actual: dict, expected: dict) -> None:
    """Every key of the saved report is still there with the same value; new keys are allowed."""
    mismatched = {k: (actual.get(k, "<missing>"), v) for k, v in expected.items() if actual.get(k, "<missing>") != v}
    assert not mismatched, f"(actual, saved) differs for: {mismatched}"


def assert_items(items: list[dict], expected: list[dict]) -> None:
    for item, want in zip(items, expected, strict=True):
        assert {k: item[k] for k in want} == want, item["name"]


def fees(rows: list[dict]) -> list[Fee]:
    return [Fee(**row) for row in rows]


@pytest.mark.parametrize("index", range(4))
def test_coffee_matches_saved_report(saved_runs, index):
    run = runs_for(saved_runs, "coffee")[index]
    c = run["checkout"]
    cart = coffee.CartLines(
        is_cart_visible=True, items=[], subtotal=run["cart_subtotal"], has_checkout_button=True, reason=""
    )
    checkout = coffee.CheckoutDetails(
        is_checkout_page=True,
        blocked_reason=None,
        items=[LineItem(**line) for line in c["items"]],
        subtotal=c["subtotal"],
        shipping=c["shipping"],
        other_fees=fees(c["other_fees"]),
        tax_amount=c["tax_amount"],
        tax_included=c["tax_included"],
        total=c["total"],
        currency=c["currency"],
        summary=c["summary"],
    )
    assert_report(coffee.compare_prices(run["items"], cart, checkout), run["expected"])
    assert_items(run["items"], run["items_expected"])


@pytest.mark.parametrize("index", range(2))
def test_virgin_matches_saved_report(saved_runs, index):
    run = runs_for(saved_runs, "virgin")[index]
    c = run["checkout"]
    result = SiteResult(
        site="https://example.test", out_dir=Path(), items=run["items"], extras=run["extras"], steps=run["steps"]
    )
    checkout = virgin.CheckoutDetails(
        is_checkout_page=True,
        blocked_reason=None,
        items=[virgin.LineItem(**line) for line in c["items"]],
        subtotal=c["subtotal"],
        extras=fees(c["extras"]),
        other_fees=fees(c["other_fees"]),
        payment_surcharges=[virgin.Surcharge(**s) for s in c["payment_surcharges"]],
        tax_amount=c["tax_amount"],
        tax_included=c["tax_included"],
        total=c["total"],
        currency=c["currency"],
        summary=c["summary"],
    )
    assert_report(virgin.compare_prices(result, checkout), run["expected"])
    assert_items(result.items, run["items_expected"])
    assert result.extras == run["extras_expected"]


def uber_state(c: dict, *, checkout_page: bool, subtotal: str | None) -> uber_models.CartAndCheckoutState:
    return uber_models.CartAndCheckoutState(
        step_type="checkout" if checkout_page else "cart_drawer",
        is_checkout_page=checkout_page,
        is_cart_visible=not checkout_page,
        is_upsell_modal=False,
        blocked_reason=None,
        items=[LineItem(**line) for line in c["items"]],
        subtotal=subtotal,
        delivery_fee=c["delivery_fee"],
        priority_delivery_fee=c["priority_delivery_fee"],
        service_fee=c["service_fee"],
        small_order_fee=c["small_order_fee"],
        other_fees=fees(c["other_fees"]),
        tax_amount=c["tax_amount"],
        tax_included=c["tax_included"],
        total=c["total"],
        currency=c["currency"],
        offers=[],
        summary=c["summary"],
    )


def test_ubereats_matches_saved_report(saved_runs):
    (run,) = runs_for(saved_runs, "ubereats")
    c = run["checkout"]
    result = uber_models.UberResult(
        site="https://example.test",
        out_dir=Path(),
        items=run["items"],
        extras=run["extras"],
        steps=run["steps"],
        **run["result"],
    )
    cart = uber_state(c, checkout_page=False, subtotal=run["cart_subtotal"])
    checkout = uber_state(c, checkout_page=True, subtotal=c["subtotal"])
    assert_report(uber_pricing.compare_prices(result, cart, checkout), run["expected"])
    assert_items(result.items, run["items_expected"])
    assert result.extras == run["extras_expected"]


def test_no_checkout_still_reports_no_drip(saved_runs):
    """Pins the false 'no drip' for a run that never saw a checkout. Phase 4 turns this into None."""
    run = runs_for(saved_runs, "coffee")[0]
    cart = coffee.CartLines(
        is_cart_visible=True, items=[], subtotal=run["cart_subtotal"], has_checkout_button=False, reason=""
    )
    report = coffee.compare_prices(run["items"], cart, None)
    assert report["drip_detected"] is False
    assert [i["price_increased"] for i in run["items"]] == [False, False, False]
    assert report["checkout_total"] is None
