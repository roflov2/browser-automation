"""Price parsing, money arithmetic and name matching in audit.core."""

from types import SimpleNamespace

import pytest

from audit.core import (
    Blocked,
    ElementNotFound,
    FailureReason,
    LineItem,
    SiteResult,
    best_match,
    classify_failure,
    compare_item_prices,
    diff,
    drip_outcome,
    money_sum,
    new_item,
    norm_name,
    parse_amount,
    parse_price,
    pct_diff,
    pct_of,
    pop_best_line,
    positive,
    record_offers,
    select_price,
    slug,
    summarise_extras,
    unit_price,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$1,234.56", 1234.56),
        ("1.234,56 €", 1234.56),
        ("From $15", 15.0),
        ("Free", 0.0),
        ("FREE", 0.0),
        ("+$3.50", 3.5),
        ("AUD $21.00", 21.0),
        ("A$22.00", 22.0),
        ("1,234", 1234.0),
        ("12,50", 12.5),
        ("1 234,50", 1234.5),
        ("1,234,567", 1234567.0),
        ("$0.00", 0.0),
        ("$25.00 $28.00", 25.0),
        ("$4.99 Delivery Fee^", 4.99),
        ("$67.00 AUD", 67.0),
        ("20", 20.0),
        ("1.3%", 1.3),
        ("", None),
        (None, None),
        ("garbage", None),
        ("Included", None),
        ("No fee", None),
    ],
)
def test_parse_price(text, expected):
    assert parse_price(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("-$11.85", -11.85),
        ("($5.00)", -5.0),
        ("Free on $50+", None),
        ("Gluten free base $3", 3.0),
        ("2 for $30", 30.0),
        ("3x $5", 5.0),
        ("$.50", 0.5),
        ("1,5", 1.5),
        ("$5 10 min", 5.0),
    ],
)
def test_parse_price_edge_cases(text, expected):
    assert parse_price(text) == expected


def test_parse_amount_rejects_rates():
    assert parse_amount("1.3%") is None
    assert parse_amount("10% of subtotal") is None
    assert parse_amount("$3.50") == 3.5
    assert parse_amount(None) is None


def test_money_sum():
    assert money_sum([0.1, 0.2]) == 0.3
    assert money_sum([21.0, None, 23.0]) == 44.0
    assert money_sum([None]) is None
    assert money_sum([]) is None
    assert money_sum([0.0]) == 0.0


def test_diff():
    assert diff(0.3, 0.1) == 0.2
    assert diff(46.23, 47.4) == -1.17
    assert diff(None, 1.0) is None
    assert diff(1.0, None) is None
    assert diff(0.0, 0.0) == 0.0


def test_pct_diff():
    assert pct_diff(47.4, 45.0) == 5.3
    assert pct_diff(25.0, 21.0) == 19.0
    assert pct_diff(5.0, 0.0) is None
    assert pct_diff(None, 5.0) is None


def test_pct_of():
    assert pct_of(5.69, 47.4) == 12.0
    assert pct_of(None, 47.4) is None
    assert pct_of(5.0, None) is None
    assert pct_of(5.0, 0.0) is None


def test_pct_of_zero_amount_is_zero_percent():
    assert pct_of(0.0, 47.4) == 0.0


def test_slug_and_norm_name():
    assert slug("Upsell Modal") == "upsell-modal"
    assert slug("!!!") == "step"
    assert norm_name("  Good-Day  COFFEE! ") == "good day coffee"


def test_best_match_exact_beats_containment():
    assert best_match("Chips", ["Fish and Chips", "Chips"]) == 1


def test_best_match_containment_switch():
    assert best_match("Coffee", ["Good Day Coffee"]) == 0
    assert best_match("Coffee", ["Good Day Coffee"], containment=False) is None


def test_best_match_threshold_and_empty():
    assert best_match("Pepperoni Burst", ["PEPPERONI BURST PIZZA", "Margarita"]) == 0
    assert best_match("Pepperoni Burst", ["Margarita"]) is None
    assert best_match("", ["x"]) is None
    assert best_match("x", []) is None


def test_best_match_prefers_closest_containing_candidate():
    assert best_match("Fries", ["OOSH Fries Large", "Fries"]) == 1
    assert best_match("Lebo Fries", ["OOSH Fries Large", "Lebo Fries Regular"]) == 1


def test_best_match_containment_prefers_most_token_overlap():
    assert best_match("Fries", ["OOSH Fries Large", "Lebo Fries"]) == 1


def _line(name: str, price: str | None, quantity: int = 1) -> LineItem:
    return LineItem(name=name, variant=None, quantity=quantity, price=price)


def test_pop_best_line_uses_each_line_once():
    find = pop_best_line([_line("Rogue Coffee", "$23.00"), _line("Rogue Coffee", "$24.00")])
    assert find({"name": "Rogue Coffee"}).price == "$23.00"
    assert find({"name": "Rogue Coffee"}).price == "$24.00"
    assert find({"name": "Rogue Coffee"}) is None


def test_compare_item_prices_unit_price_and_increase():
    items = [new_item("Rogue Coffee", "From $23.00"), new_item("Good Day Coffee", "$23.00")]
    items[0]["selected_price"] = 23.0
    lines = [_line("Rogue Coffee", "$50.00", quantity=2), _line("Good Day Coffee", "$23.00")]
    assert compare_item_prices(items, pop_best_line(lines)) == 46.0
    assert items[0]["checkout_price"] == 25.0
    assert items[0]["checkout_vs_expected_diff"] == 2.0
    assert items[0]["price_increased"] is True
    assert items[1]["expected_price"] == 23.0  # falls back to the shop price
    assert items[1]["price_increased"] is False


def test_compare_item_prices_without_a_checkout_line_is_unknown():
    items = [new_item("Rogue Coffee", "$23.00")]
    assert compare_item_prices(items, lambda item: None) == 23.0
    assert items[0]["checkout_price"] is None
    assert items[0]["price_increased"] is None  # not "False": nothing was compared


def test_zero_is_a_price():
    """$0.00 is a real amount everywhere; only None means unknown (REVIEW H5)."""
    item = new_item("Tap water", "$0.00")
    assert item["shop_price"] == 0.0
    select_price(item, "$0.00")
    assert item["selected_price"] == 0.0
    select_price(item, "see menu")
    assert item["selected_price"] == 0.0  # unreadable: falls back to the shop price, which is zero
    assert positive(0.0) is False and positive(None) is False and positive(0.01) is True
    assert (
        compare_item_prices([item], lambda _: LineItem(name="Tap water", variant=None, quantity=1, price="$1.00"))
        == 0.0
    )
    assert item["price_increased"] is True


def test_decimal_arithmetic_rounds_half_up():
    assert money_sum([0.1, 0.2, 0.3]) == 0.6
    assert money_sum([1.005, 1.005]) == 2.01
    assert diff(1.15, 1.0) == 0.15
    assert unit_price(10.0, 3) == 3.33
    assert unit_price(0.05, 2) == 0.03
    assert pct_of(1.0, 8.0) == 12.5


def test_drip_outcome_is_three_valued():
    assert drip_outcome(["fee added"], final_price_read=False)["drip_detected"] is True
    assert drip_outcome([], final_price_read=True)["drip_detected"] is False
    assert drip_outcome([], final_price_read=False)["drip_detected"] is None


def test_failures_are_classified():
    assert classify_failure(Blocked("captcha")) is FailureReason.BLOCKED
    assert classify_failure(ElementNotFound("no cart")) is FailureReason.ELEMENT_NOT_FOUND
    assert classify_failure(TimeoutError()) is FailureReason.TIMEOUT
    assert classify_failure(RuntimeError("observe() timed out after 90000ms")) is FailureReason.TIMEOUT
    assert classify_failure(KeyError("x")) is None
    assert [reason.value for reason in FailureReason] == [
        "blocked", "out_of_stock", "flow_changed", "element_not_found", "extract_failed", "timeout", "unsafe_action_refused",
        "cart_lost",
    ]  # fmt: skip


def test_record_offers_dedupes_per_step_type():
    result = SiteResult(site="s", out_dir=None)  # type: ignore[arg-type]
    offer = SimpleNamespace(
        category="seat", label="Economy X", price="$45", price_basis=None, selected=True, mandatory=False
    )
    rate = SimpleNamespace(
        category="payment_surcharge", label="Amex", price="1.3%", price_basis=None, selected=False, mandatory=False
    )
    record_offers(result, 1, "seats", "Seats", [offer, rate])
    record_offers(result, 2, "seats", "Seats", [offer])
    record_offers(result, 3, "extras", "Extras", [offer])
    assert [(e["step"], e["step_type"], e["label"]) for e in result.extras] == [
        (1, "seats", "Economy X"),
        (1, "seats", "Amex"),
        (3, "extras", "Economy X"),
    ]
    assert result.extras[0]["price"] == 45.0
    assert result.extras[0]["selected_by_default"] is True
    assert result.extras[1]["price"] is None  # a rate, not an amount
    summary = summarise_extras(result.extras)
    assert summary == {
        "extras_offered": 3,
        "extras_offered_by_category": {"seat": 2, "payment_surcharge": 1},
        "extras_preselected": ["Economy X", "Economy X"],
    }
