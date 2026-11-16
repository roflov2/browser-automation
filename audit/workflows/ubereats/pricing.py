"""Uber Eats price & fee comparison: menu vs checkout, advertised vs charged fees, Uber vs restaurant direct."""

from ...core import (
    compare_item_prices,
    diff,
    drip_verdict,
    money_sum,
    parse_amount,
    parse_price,
    pct_diff,
    pct_of,
    pop_best_line,
    summarise_extras,
)
from .models import CartAndCheckoutState, UberResult

FEE_BREAKDOWN_FIELDS = [
    "fee_type", "label", "advertised_display", "checkout_display",
    "amount", "diff_vs_advertised", "pct_of_subtotal", "mandatory",
]


def compare_prices(
    result: UberResult,
    cart: CartAndCheckoutState | None,
    checkout: CartAndCheckoutState | None,
) -> dict:
    final = checkout or cart
    added = result.added

    def shown(attr: str):
        return getattr(final, attr) if final else None

    expected_subtotal = compare_item_prices(added, pop_best_line(final.items if final else []))
    subtotal = parse_price(shown("subtotal"))
    delivery = parse_price(shown("delivery_fee"))
    service = parse_price(shown("service_fee"))
    small_order = parse_price(shown("small_order_fee"))
    base = subtotal or expected_subtotal
    service_pct = pct_of(service, base)
    delivery_diff = diff(delivery, result.advertised_delivery_fee)
    other = [(f, parse_amount(f.amount)) for f in (final.other_fees if final else [])]

    fees = [{"label": label, "amount": amt} for label, amt in (("Service Fee", service), ("Small Order Fee", small_order)) if amt]
    fees += [{"label": f.label, "amount": amt} for f, amt in other]
    fees_total = money_sum([f["amount"] for f in fees]) or 0.0

    def fee_row(fee_type, label, advertised, checkout_display, amount, diff_adv, pct, mandatory=True) -> dict:
        return {
            "fee_type": fee_type, "label": label, "advertised_display": advertised,
            "checkout_display": checkout_display, "amount": amount, "diff_vs_advertised": diff_adv,
            "pct_of_subtotal": pct, "mandatory": mandatory,
        }

    undisclosed = "not disclosed upfront"
    fee_breakdown = [
        fee_row("delivery_fee", "Standard Delivery Fee", result.advertised_delivery_fee_display or "not shown",
                shown("delivery_fee") or "not shown", delivery, delivery_diff,
                round((delivery / base) * 100.0, 1) if delivery is not None and base else None),
        fee_row("service_fee", "Service Fee", result.advertised_service_fee_display or undisclosed,
                shown("service_fee") or "$0.00 / not shown", service, service, service_pct),
        fee_row("small_order_fee", "Small Order Fee", undisclosed,
                shown("small_order_fee") or "$0.00", small_order or 0.0, small_order, pct_of(small_order, base) or 0.0),
        fee_row("priority_delivery", "Priority Delivery Upgrade (Optional)", "n/a",
                shown("priority_delivery_fee") or "not offered", parse_price(shown("priority_delivery_fee")),
                None, None, mandatory=False),
        *(fee_row("other_fee", f.label, undisclosed, f.amount, amt, amt, pct_of(amt, base)) for f, amt in other),
    ]

    for category, label, amount, attr in (
        ("service_fee", f"Service Fee ({service_pct}% of subtotal)" if service_pct else "Service Fee", service, "service_fee"),
        ("small_order_fee", "Small Order Fee", small_order, "small_order_fee"),
    ):
        if amount is not None:
            result.extras.append({
                "step": len(result.steps), "step_type": "checkout", "step_heading": "Checkout Breakdown",
                "category": category, "label": label, "price_display": shown(attr), "price": amount,
                "price_basis": "per order", "selected_by_default": True, "mandatory": True,
            })

    matched_direct = [i for i in added if i.get("direct_price") is not None and i.get("shop_price") is not None]
    direct_subtotal = money_sum([i["direct_price"] for i in matched_direct])
    uber_matched_subtotal = money_sum([i["shop_price"] for i in matched_direct])
    markup_dollars = diff(uber_matched_subtotal, direct_subtotal)
    markup_pct = pct_diff(uber_matched_subtotal, direct_subtotal)
    extras = summarise_extras(result.extras)
    preselected = extras["extras_preselected"]

    reasons = [
        reason for reason, hit in (
            ("menu item price increased at checkout", any(i["price_increased"] for i in added)),
            ("checkout item subtotal higher than menu prices", (diff(subtotal, expected_subtotal) or 0) > 0),
            (f"service fee added at checkout ({shown('service_fee')}"
             + (f", {service_pct}% of subtotal)" if service_pct else ")"), (service or 0) > 0),
            (f"small order fee added ({shown('small_order_fee')})", (small_order or 0) > 0),
            (f"delivery fee higher than advertised ({result.advertised_delivery_fee_display} -> {shown('delivery_fee')})",
             (delivery_diff or 0) > 0),
            ("other non-tax fees added", fees_total > (service or 0) + (small_order or 0)),
            (f"Uber Eats menu prices marked up +{markup_pct}% (+${markup_dollars or 0:.2f}) vs restaurant direct website",
             (markup_dollars or 0) > 0),
            ("optional extras pre-selected: " + ", ".join(preselected), bool(preselected)),
        )
        if hit
    ]

    total = parse_price(shown("total"))
    return {
        "store_name": result.store_name,
        "direct_website_url": result.direct_website_url,
        "items_added": len(added),
        "expected_subtotal": expected_subtotal,
        "cart_subtotal_display": cart.subtotal if cart else None,
        "checkout_subtotal_display": shown("subtotal"),
        "checkout_subtotal": subtotal,
        "subtotal_vs_expected_diff": diff(subtotal, expected_subtotal),
        "advertised_delivery_fee_display": result.advertised_delivery_fee_display,
        "advertised_delivery_fee": result.advertised_delivery_fee,
        "shipping": shown("delivery_fee") or result.advertised_delivery_fee_display
        or "not calculated (login/checkout required)",
        "checkout_delivery_fee": delivery,
        "delivery_fee_diff": delivery_diff,
        "priority_delivery_fee_display": shown("priority_delivery_fee"),
        "service_fee_display": shown("service_fee"),
        "service_fee": service,
        "service_fee_pct_of_subtotal": service_pct,
        "small_order_fee_display": shown("small_order_fee"),
        "small_order_fee": small_order,
        "other_fees": fees,
        "other_fees_total": fees_total,
        "direct_items_matched": len(matched_direct),
        "direct_matched_subtotal": direct_subtotal,
        "uber_matched_subtotal": uber_matched_subtotal,
        "uber_vs_direct_subtotal_diff": markup_dollars,
        "uber_vs_direct_markup_pct": markup_pct,
        "fee_breakdown": fee_breakdown,
        **extras,
        "tax_amount_display": shown("tax_amount"),
        "tax_included": shown("tax_included"),
        "checkout_total_display": shown("total"),
        "checkout_total": total,
        "total_vs_expected_diff": diff(total, expected_subtotal),
        "currency": shown("currency") or "AUD",
        "drip_detected": bool(reasons),
        "drip_reasons": reasons,
        "checkout_summary": shown("summary"),
    }


def print_audit_tables(result: UberResult) -> None:
    c = result.comparison
    print("\n  --- Extra Fees Breakdown (Uber Eats) ---")
    for row in c["fee_breakdown"]:
        pct_str = f" ({row['pct_of_subtotal']}% of subtotal)" if row.get("pct_of_subtotal") else ""
        print(
            f"    {row['label']:<34} | Adv: {str(row['advertised_display']):<24} "
            f"| Checkout: {row['checkout_display']}{pct_str}"
        )

    print("\n  --- Menu Item Unit Price Comparison (Uber Eats vs Restaurant Direct) ---")
    for item in result.added:
        uber_p = item.get("shop_price_display") or item.get("selected_price_display")
        delta, pct = item.get("uber_vs_direct_diff"), item.get("uber_vs_direct_markup_pct")
        markup = f"{delta:+.2f} ({pct:+}%)" if delta is not None else "n/a"
        print(
            f"    {item['name']:<34} | Uber Eats: {str(uber_p):<10} "
            f"| Direct: {item.get('direct_price_display') or 'not found':<10} | Diff: {markup}"
        )

    print(
        f"\n  Summary: Uber Subtotal={c['expected_subtotal']} | Checkout Subtotal={c['checkout_subtotal_display']} "
        f"| Delivery={c['shipping']} | Service/Other Fees={c['other_fees_total']} | Total={c['checkout_total_display']}"
    )
    print(f"  Drip / Markup Detected: {drip_verdict(c)}")
