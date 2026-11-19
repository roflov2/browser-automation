"""Characterisation tests: each workflow's compare_prices reproduces the reports of the saved runs.

The inputs are rebuilt from the display fields of the seven runs that used to be committed under
output/ (see tests/fixtures/saved_runs.json; URLs and the delivery address are left out).
"""

import copy
from pathlib import Path

import pytest

from audit.core import Fee, LineItem, SiteResult, drip_verdict
from audit.workflows import coffee, virgin
from audit.workflows.ubereats import models as uber_models
from audit.workflows.ubereats import pricing as uber_pricing

# Values that were changed on purpose after the saved runs were made (see MIGRATION.md). Everything else
# in a saved report must still come out exactly as it was.
NO_CHECKOUT_LINE = {"price_increased": None}  # was False: no line to compare with is "unknown", not "no" (REVIEW H1)
SMALL_ORDER_FEE_NOT_SHOWN = {
    "checkout_display": "not shown",
    "amount": None,
    "pct_of_subtotal": None,
}  # was $0.00 / 0.0


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
    report = uber_pricing.compare_prices(result, cart, checkout)

    expected = run["expected"]
    small_order_row = next(row for row in expected["fee_breakdown"] if row["fee_type"] == "small_order_fee")
    small_order_row.update(SMALL_ORDER_FEE_NOT_SHOWN)  # the fee was not on the page; it is no longer reported as $0.00
    assert_report(report, expected)
    # The checkout collapsed the cart, so no item had a checkout line: unknown, where the saved run said False.
    assert_items(result.items, [{**want, **NO_CHECKOUT_LINE} for want in run["items_expected"]])
    assert result.extras == run["extras_expected"]

    # New fields. The promotion of -$11.85 is now visible instead of hiding $10.68 of added fees.
    assert report["final_price_read"] is True
    assert report["item_prices_source"] is None
    assert report["fees_and_delivery_total"] == 10.68
    assert report["total_vs_components_diff"] == -11.85
    assert report["currency_assumed"] is None  # the page showed AUD


def test_ubereats_reads_item_prices_from_the_cart_when_checkout_hides_them(saved_runs):
    (run,) = runs_for(saved_runs, "ubereats")
    c = run["checkout"]
    result = uber_models.UberResult(site="https://example.test", out_dir=Path(), items=run["items"], **run["result"])
    cart = uber_state(c, checkout_page=False, subtotal=run["cart_subtotal"])
    cart.items = [
        LineItem(name="Pepperoni Burst", variant=None, quantity=1, price="$26.00"),
        LineItem(name="Super Supreme", variant=None, quantity=2, price="$44.80"),
    ]
    report = uber_pricing.compare_prices(result, cart, uber_state(c, checkout_page=True, subtotal=c["subtotal"]))
    assert report["item_prices_source"] == "cart"
    assert [(i["checkout_price"], i["price_increased"]) for i in result.items] == [(26.0, True), (22.4, False)]
    assert "menu item price increased at checkout" in report["drip_reasons"]


def test_ubereats_zero_fees_and_missing_currency(saved_runs):
    """A fee shown as $0.00 is a fee of zero, a fee that is not shown is unknown, and currency is not invented."""
    (run,) = runs_for(saved_runs, "ubereats")
    c = {**run["checkout"], "service_fee": "$0.00", "small_order_fee": None, "currency": None,
         "other_fees": [{"label": "Promotion", "amount": "-$11.85"}]}  # fmt: skip
    result = uber_models.UberResult(site="https://example.test", out_dir=Path(), items=run["items"], **run["result"])
    report = uber_pricing.compare_prices(result, None, uber_state(c, checkout_page=True, subtotal=c["subtotal"]))
    assert report["service_fee"] == 0.0
    assert report["service_fee_pct_of_subtotal"] == 0.0
    assert {"label": "Service Fee", "amount": 0.0} in report["other_fees"]
    assert report["small_order_fee"] is None
    assert report["other_fees_total"] == -11.85  # a discount filed under fees lowers the total, it is not a fee added
    assert "other non-tax fees added" not in report["drip_reasons"]
    assert not any(reason.startswith("service fee added") for reason in report["drip_reasons"])
    assert (report["currency"], report["currency_assumed"]) == (None, "AUD")


def test_ubereats_fee_without_amount(saved_runs):
    """A fee row shown with no amount (an info-only 'Fees' line) is kept but adds nothing to the fee total."""
    (run,) = runs_for(saved_runs, "ubereats")
    c = {**run["checkout"], "other_fees": [{"label": "Fees", "amount": None}]}
    result = uber_models.UberResult(site="https://example.test", out_dir=Path(), items=run["items"], **run["result"])
    baseline = uber_pricing.compare_prices(
        result, None, uber_state({**c, "other_fees": []}, checkout_page=True, subtotal=c["subtotal"])
    )
    report = uber_pricing.compare_prices(result, None, uber_state(c, checkout_page=True, subtotal=c["subtotal"]))
    assert {"label": "Fees", "amount": None} in report["other_fees"]
    assert report["other_fees_total"] == baseline["other_fees_total"]


def coffee_inputs(run: dict, **checkout_overrides):
    c = {**run["checkout"], **checkout_overrides}
    cart = coffee.CartLines(
        is_cart_visible=True, items=[], subtotal=run["cart_subtotal"], has_checkout_button=True, reason=""
    )
    checkout = coffee.CheckoutDetails(
        is_checkout_page=c.get("is_checkout_page", True), blocked_reason=c.get("blocked_reason"),
        items=[LineItem(**line) for line in c["items"]], subtotal=c["subtotal"], shipping=c["shipping"],
        other_fees=fees(c["other_fees"]), tax_amount=c["tax_amount"], tax_included=c["tax_included"], total=c["total"],
        currency=c["currency"], summary=c["summary"],
    )  # fmt: skip
    return cart, checkout


def test_no_checkout_is_unknown_not_no_drip(saved_runs):
    """A run that never read the final price screen must not report 'no drip' (REVIEW H1)."""
    run = runs_for(saved_runs, "coffee")[0]
    cart, _ = coffee_inputs(run)
    report = coffee.compare_prices(run["items"], cart, None)
    assert report["drip_detected"] is None
    assert report["final_price_read"] is False
    assert [i["price_increased"] for i in run["items"]] == [None, None, None]
    assert report["checkout_total"] is None
    assert drip_verdict(report).startswith("UNKNOWN")


@pytest.mark.parametrize(
    "overrides", [{"is_checkout_page": False}, {"blocked_reason": "Verify you are human"}, {"total": None}]
)
def test_checkout_not_really_read_is_unknown(saved_runs, overrides):
    run = runs_for(saved_runs, "coffee")[0]
    report = coffee.compare_prices(run["items"], *coffee_inputs(run, **overrides))
    assert report["drip_detected"] is None
    assert report["final_price_read"] is False


def test_evidence_from_a_partial_run_still_counts(saved_runs):
    """A fee seen before the run was blocked is still a finding."""
    run = runs_for(saved_runs, "coffee")[0]
    fee = [{"label": "Handling fee", "amount": "$2.50"}]
    report = coffee.compare_prices(run["items"], *coffee_inputs(run, is_checkout_page=False, other_fees=fee))
    assert report["drip_detected"] is True
    assert report["drip_reasons"] == ["non-tax, non-shipping fees added"]
    assert report["final_price_read"] is False


def test_coffee_reports_whether_shipping_was_calculated(saved_runs):
    run = runs_for(saved_runs, "coffee")[0]
    assert coffee.compare_prices(run["items"], *coffee_inputs(run))["shipping_calculated"] is False
    assert coffee.compare_prices(run["items"], *coffee_inputs(run, shipping="$9.95"))["shipping_calculated"] is True


def test_virgin_without_payment_page_is_unknown(saved_runs):
    run = runs_for(saved_runs, "virgin")[0]
    result = SiteResult(
        site="https://example.test", out_dir=Path(), items=run["items"], extras=run["extras"], steps=run["steps"]
    )
    report = virgin.compare_prices(result, None)
    assert report["drip_detected"] is None
    assert report["checkout_total"] == 385.0  # the last running total seen is still reported
    assert report["final_price_read"] is False
