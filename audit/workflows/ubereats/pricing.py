"""Uber Eats price & fee comparison: menu vs checkout, advertised vs charged fees."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ...core import (
    Fee,
    Item,
    LineItem,
    Report,
    compare_item_prices,
    diff,
    drip_outcome,
    drip_verdict,
    final_price_was_read,
    money_sum,
    parse_amount,
    parse_price,
    pct_of,
    pop_best_line,
    positive,
    summarise_extras,
)
from .models import CartAndCheckoutState, UberResult

log = logging.getLogger(__name__)

FEE_BREAKDOWN_FIELDS = [
    "fee_type", "label", "advertised_display", "checkout_display",
    "amount", "diff_vs_advertised", "pct_of_subtotal", "mandatory",
]  # fmt: skip
UNDISCLOSED = "not disclosed upfront"
ASSUMED_CURRENCY = "AUD"  # ubereats.com/au; reported separately from a currency the page actually showed


@dataclass(frozen=True)
class Charges:
    """The amounts on the last screen that was read. None means "not shown", never zero."""

    subtotal: float | None = None
    delivery: float | None = None
    service: float | None = None
    small_order: float | None = None
    priority: float | None = None
    total: float | None = None
    other: tuple[tuple[Fee, float | None], ...] = ()

    @classmethod
    def read(cls, screen: CartAndCheckoutState | None) -> Charges:
        if screen is None:
            return cls()
        return cls(
            subtotal=parse_price(screen.subtotal),
            delivery=parse_price(screen.delivery_fee),
            service=parse_price(screen.service_fee),
            small_order=parse_price(screen.small_order_fee),
            priority=parse_price(screen.priority_delivery_fee),
            total=parse_price(screen.total),
            other=tuple((fee, parse_amount(fee.amount)) for fee in screen.other_fees),
        )

    @property
    def fees(self) -> list[dict[str, Any]]:
        """Every fee that was shown other than delivery, including a fee shown as $0.00."""
        named = (("Service Fee", self.service), ("Small Order Fee", self.small_order))
        shown = [{"label": label, "amount": amount} for label, amount in named if amount is not None]
        return shown + [{"label": fee.label, "amount": amount} for fee, amount in self.other]


def checkout_lines(
    cart: CartAndCheckoutState | None, checkout: CartAndCheckoutState | None
) -> tuple[list[LineItem], str | None]:
    """The line items to compare menu prices with, and where they came from.

    The checkout page collapses the cart ('Cart summary (2 items)'), so its item list is often
    empty; the cart drawer read just before it is the fallback.
    """
    for source, screen in (("checkout", checkout), ("cart", cart)):
        if screen and screen.items:
            return list(screen.items), source
    return [], None


def fee_row(
    fee_type: str, label: str, advertised: str, shown: str, amount: float | None,
    diff_vs_advertised: float | None, pct: float | None, mandatory: bool = True,
) -> dict[str, Any]:  # fmt: skip
    return {
        "fee_type": fee_type, "label": label, "advertised_display": advertised, "checkout_display": shown,
        "amount": amount, "diff_vs_advertised": diff_vs_advertised, "pct_of_subtotal": pct, "mandatory": mandatory,
    }  # fmt: skip


def fee_breakdown(
    result: UberResult, final: CartAndCheckoutState | None, c: Charges, base: float | None
) -> list[dict[str, Any]]:
    """One row per fee: what was advertised up front against what the last screen charged."""

    def shown(attr: str) -> str | None:
        return getattr(final, attr) if final else None

    return [
        fee_row("delivery_fee", "Standard Delivery Fee", result.advertised_delivery_fee_display or "not shown",
                shown("delivery_fee") or "not shown", c.delivery, diff(c.delivery, result.advertised_delivery_fee),
                pct_of(c.delivery, base)),
        fee_row("service_fee", "Service Fee", result.advertised_service_fee_display or UNDISCLOSED,
                shown("service_fee") or "$0.00 / not shown", c.service, c.service, pct_of(c.service, base)),
        fee_row("small_order_fee", "Small Order Fee", UNDISCLOSED, shown("small_order_fee") or "not shown",
                c.small_order, c.small_order, pct_of(c.small_order, base)),
        fee_row("priority_delivery", "Priority Delivery Upgrade (Optional)", "n/a",
                shown("priority_delivery_fee") or "not offered", c.priority, None, None, mandatory=False),
        *(fee_row("other_fee", fee.label, UNDISCLOSED, fee.amount or "not shown", amount, amount, pct_of(amount, base))
          for fee, amount in c.other),
    ]  # fmt: skip


def record_checkout_fees(
    result: UberResult, final: CartAndCheckoutState | None, c: Charges, service_pct: float | None
) -> None:
    """Add the mandatory fees of the fee breakdown to the run's extras, so extras.csv lists them too."""
    service_label = f"Service Fee ({service_pct}% of subtotal)" if service_pct is not None else "Service Fee"
    for category, label, amount, display in (
        ("service_fee", service_label, c.service, final.service_fee if final else None),
        ("small_order_fee", "Small Order Fee", c.small_order, final.small_order_fee if final else None),
    ):
        if amount is not None:
            result.extras.append({
                "step": len(result.steps), "step_type": "checkout", "step_heading": "Checkout Breakdown",
                "category": category, "label": label, "price_display": display, "price": amount,
                "price_basis": "per order", "selected_by_default": True, "mandatory": True,
            })  # fmt: skip


def drip_reasons(
    result: UberResult, final: CartAndCheckoutState | None, c: Charges, added: list[Item],
    expected_subtotal: float | None, service_pct: float | None, preselected: list[str],
) -> list[str]:  # fmt: skip
    service_display, small_display, delivery_display = (
        (final.service_fee, final.small_order_fee, final.delivery_fee) if final else (None, None, None)
    )
    service_note = f", {service_pct}% of subtotal)" if service_pct else ")"
    other_total = money_sum(amount for _, amount in c.other)
    checks = (
        ("menu item price increased at checkout", any(i["price_increased"] for i in added)),
        ("checkout item subtotal higher than menu prices", positive(diff(c.subtotal, expected_subtotal))),
        (f"service fee added at checkout ({service_display}{service_note}", positive(c.service)),
        (f"small order fee added ({small_display})", positive(c.small_order)),
        (f"delivery fee higher than advertised ({result.advertised_delivery_fee_display} -> {delivery_display})",
         positive(diff(c.delivery, result.advertised_delivery_fee))),
        ("other non-tax fees added", positive(other_total)),
        ("optional extras pre-selected: " + ", ".join(preselected), bool(preselected)),
    )  # fmt: skip
    return [reason for reason, hit in checks if hit]


def compare_prices(
    result: UberResult, cart: CartAndCheckoutState | None, checkout: CartAndCheckoutState | None
) -> Report:
    """Menu prices and advertised fees against the last screen read (checkout if reached, else the cart)."""
    final = checkout or cart
    added = result.added
    c = Charges.read(final)
    lines, lines_source = checkout_lines(cart, checkout)
    expected_subtotal = compare_item_prices(added, pop_best_line(lines))
    base = c.subtotal if c.subtotal is not None else expected_subtotal
    service_pct = pct_of(c.service, base)
    fees = c.fees
    fees_total = money_sum(f["amount"] for f in fees) or 0.0
    components = money_sum([c.subtotal, c.delivery, fees_total]) if c.subtotal is not None else None

    breakdown = fee_breakdown(result, final, c, base)
    record_checkout_fees(result, final, c, service_pct)
    extras = summarise_extras(result.extras)
    reasons = drip_reasons(result, final, c, added, expected_subtotal, service_pct, extras["extras_preselected"])

    def shown(attr: str) -> Any:
        return getattr(final, attr) if final else None

    return {
        "store_name": result.store_name,
        "items_added": len(added),
        "expected_subtotal": expected_subtotal,
        "cart_subtotal_display": cart.subtotal if cart else None,
        "checkout_subtotal_display": shown("subtotal"),
        "checkout_subtotal": c.subtotal,
        "subtotal_vs_expected_diff": diff(c.subtotal, expected_subtotal),
        "item_prices_source": lines_source,
        "advertised_delivery_fee_display": result.advertised_delivery_fee_display,
        "advertised_delivery_fee": result.advertised_delivery_fee,
        "shipping": shown("delivery_fee")
        or result.advertised_delivery_fee_display
        or "not calculated (login/checkout required)",
        "checkout_delivery_fee": c.delivery,
        "delivery_fee_diff": diff(c.delivery, result.advertised_delivery_fee),
        "priority_delivery_fee_display": shown("priority_delivery_fee"),
        "service_fee_display": shown("service_fee"),
        "service_fee": c.service,
        "service_fee_pct_of_subtotal": service_pct,
        "small_order_fee_display": shown("small_order_fee"),
        "small_order_fee": c.small_order,
        "other_fees": fees,
        "other_fees_total": fees_total,
        "fee_breakdown": breakdown,
        **extras,
        "tax_amount_display": shown("tax_amount"),
        "tax_included": shown("tax_included"),
        "checkout_total_display": shown("total"),
        "checkout_total": c.total,
        "total_vs_expected_diff": diff(c.total, expected_subtotal),
        # Subtotal + delivery + fees, and how far the total is from it. A negative gap is a discount or
        # promotion: it can make the total look lower than the menu prices while fees were still added.
        "fees_and_delivery_total": money_sum([c.delivery, fees_total]),
        "total_vs_components_diff": diff(c.total, components),
        "currency": shown("currency"),
        "currency_assumed": None if shown("currency") else ASSUMED_CURRENCY,
        **drip_outcome(reasons, final_price_was_read(checkout, c.total)),
        "checkout_summary": shown("summary"),
    }


def print_audit_tables(result: UberResult) -> None:
    """Log the fee breakdown of a finished run."""
    c = result.comparison
    log.info("\n  --- Extra Fees Breakdown (Uber Eats) ---")
    for row in c["fee_breakdown"]:
        pct_str = f" ({row['pct_of_subtotal']}% of subtotal)" if row.get("pct_of_subtotal") else ""
        log.info(
            f"    {row['label']:<34} | Adv: {row['advertised_display']!s:<24} | Checkout: {row['checkout_display']}{pct_str}"
        )

    log.info(
        f"\n  Summary: Uber Subtotal={c['expected_subtotal']} | Checkout Subtotal={c['checkout_subtotal_display']} "
        f"| Delivery={c['shipping']} | Service/Other Fees={c['other_fees_total']} | Total={c['checkout_total_display']}"
    )
    log.info(f"  Drip Detected: {drip_verdict(c)}")
