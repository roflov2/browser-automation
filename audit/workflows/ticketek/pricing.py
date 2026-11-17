"""Ticket price as advertised against the payment page."""

import logging

from ...core import (
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
from .models import CheckoutDetails, Surcharge, TicketekResult

log = logging.getLogger(__name__)


def fee_rows(fees: list) -> list[dict]:
    return [{"label": f.label, "amount": parse_amount(f.amount)} for f in fees]


def record_surcharges(result: TicketekResult, surcharges: list[Surcharge]) -> None:
    """Replace the payment step's own surcharge offers with the per-method list read from the payment page."""
    if not surcharges:
        return
    result.extras = [
        e for e in result.extras if not (e["step_type"] == "payment" and e["category"] == "payment_surcharge")
    ]
    for s in surcharges:
        result.extras.append(
            {
                "step": len(result.steps),
                "step_type": "payment",
                "step_heading": "Payment",
                "category": "payment_surcharge",
                "label": s.payment_method,
                "price_display": s.fee,
                "price": parse_amount(s.fee),
                "price_basis": "per transaction",
                "selected_by_default": False,
                "mandatory": False,
            }
        )


def compare_prices(result: TicketekResult, checkout: CheckoutDetails | None) -> Report:
    """Advertised ticket price against the payment page. `checkout` is None if the payment page was never read."""
    added = result.added
    lines = checkout.items if checkout else []
    compare_item_prices(added, pop_best_line(lines))  # per-ticket prices; the order is that times the quantity
    expected = money_sum(
        i["expected_price"] * (i.get("quantity") or 1) for i in added if i["expected_price"] is not None
    )
    subtotal = parse_price(checkout.ticket_subtotal) if checkout else None
    total = parse_price(checkout.total) if checkout else None
    final_read = final_price_was_read(checkout, total)
    if total is None:  # payment page not reached: fall back to the last running total seen
        total = next((s["total"] for s in reversed(result.steps) if s["total"] is not None), None)

    booking = fee_rows(checkout.booking_fees) if checkout else []
    handling = fee_rows(checkout.service_and_handling_fees) if checkout else []
    delivery = parse_amount(checkout.delivery_fee.amount) if checkout and checkout.delivery_fee else None
    fees_total = money_sum(f["amount"] for f in booking + handling) or 0.0
    included = fee_rows(checkout.extras) if checkout else []
    surcharges = [s.model_dump() for s in (checkout.payment_surcharges if checkout else [])]
    chargeable = [s for s in surcharges if positive(parse_price(s["fee"]))]
    extras = summarise_extras(result.extras)
    preselected = extras["extras_preselected"]
    advertised_from = parse_price(result.advertised_from_price_display)

    reasons = [
        reason
        for reason, hit in (
            ("ticket price increased at checkout", any(i["price_increased"] for i in added)),
            ("checkout ticket subtotal higher than the selected tickets", positive(diff(subtotal, expected))),
            ("total at checkout higher than the ticket price with no extras chosen", positive(diff(total, expected))),
            ("booking, service or handling fees added", fees_total > 0),
            (
                f"delivery fee charged ({checkout.delivery_fee.label if checkout and checkout.delivery_fee else ''})",
                positive(delivery),
            ),
            ("optional extras pre-selected: " + ", ".join(preselected), bool(preselected)),
            (
                "payment surcharge applies: " + ", ".join(f"{s['payment_method']} {s['fee']}" for s in chargeable),
                bool(chargeable),
            ),
            (
                f"advertised 'from' price {result.advertised_from_price_display} is below every ticket offered "
                f"for the session (cheapest {result.cheapest_offered})",
                positive(diff(result.cheapest_offered, advertised_from)),
            ),
        )
        if hit
    ]
    return {
        "event_name": result.event_name,
        "session": result.session,
        "venue": result.venue,
        "advertised_from_price_display": result.advertised_from_price_display,
        "advertised_from_price": advertised_from,
        "advertised_price_note": result.advertised_price_note,
        "cheapest_ticket_offered": result.cheapest_offered,
        "items_added": len(added),
        "tickets": sum(i.get("quantity") or 1 for i in added),
        "expected_subtotal": expected,
        "cart_subtotal_display": added[0].get("cart_total_display") if added else None,
        "checkout_subtotal_display": checkout.ticket_subtotal if checkout else None,
        "checkout_subtotal": subtotal,
        "subtotal_vs_expected_diff": diff(subtotal, expected),
        "total_vs_expected_diff": diff(total, expected),
        "shipping": delivery,
        "delivery_method": checkout.delivery_fee.label if checkout and checkout.delivery_fee else None,
        "booking_fees": booking,
        "service_and_handling_fees": handling,
        "other_fees": booking + handling,
        "other_fees_total": fees_total,
        "fees_pct_of_tickets": pct_of(money_sum([fees_total, delivery]), expected),
        "extras_in_total": included,
        "payment_surcharges": surcharges,
        **extras,
        "checkout_total_display": checkout.total if checkout else None,
        "checkout_total": total,
        "currency": checkout.currency if checkout else None,
        **drip_outcome(reasons, final_read),
        "checkout_summary": checkout.summary if checkout else None,
    }


def summarise(result: TicketekResult) -> None:
    c = result.comparison
    log.info(
        f"  {c['event_name'] or 'Event'} | {c['session'] or ''} | Advertised from: {c['advertised_from_price_display']} "
        f"| Tickets: {c['expected_subtotal']} | Fees: {c['other_fees_total']} (+ delivery {c['shipping']}) "
        f"| Total: {c['checkout_total_display']}"
    )
    log.info(f"  Drip pricing detected: {drip_verdict(c)}")
