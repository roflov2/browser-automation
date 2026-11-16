"""Browser-free helpers shared by every workflow: price parsing, name matching, results and report files."""

import csv
import json
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

OUTPUT_ROOT = Path("output")

ITEM_FIELDS = [
    "name", "selected_variant", "shop_price_display", "selected_price_display",
    "checkout_price_display", "checkout_vs_expected_diff", "price_increased", "status",
]
EXTRA_FIELDS = [
    "step", "step_type", "step_heading", "category", "label",
    "price_display", "price", "price_basis", "selected_by_default", "mandatory",
]


# ---------------------------------------------------------------------------
# Schemas shared by more than one workflow
# ---------------------------------------------------------------------------
class Fee(BaseModel):
    label: str
    amount: str


class LineItem(BaseModel):
    name: str
    variant: str | None = Field(description="Selected options or customisations if shown, e.g. '250g / Whole Beans'")
    quantity: int
    price: str | None = Field(description="Line price as displayed, e.g. '$21.00'")


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------
def parse_price(value: str | None) -> float | None:
    """Parse '$1,234.56', '1.234,56 €', 'AUD $21.00', 'From $15', '+$3.50' or 'Free' (0.0); None if no number."""
    if not value:
        return None
    if re.search(r"\bfree\b", value, re.IGNORECASE):
        return 0.0
    match = re.search(r"\d[\d.,\s]*", value)
    if not match:
        return None
    number = match.group(0).strip().replace(" ", "")
    if "," in number and "." in number:
        # Whichever separator comes last is the decimal separator.
        if number.rfind(",") > number.rfind("."):
            number = number.replace(".", "").replace(",", ".")
        else:
            number = number.replace(",", "")
    elif "," in number:
        head, _, tail = number.rpartition(",")
        number = f"{head.replace(',', '')}.{tail}" if len(tail) == 2 else number.replace(",", "")
    try:
        return float(number.rstrip("."))
    except ValueError:
        return None


def parse_amount(value: str | None) -> float | None:
    """Like parse_price, but None for percentages ('1.3%'), which are rates rather than amounts."""
    return None if value and "%" in value else parse_price(value)


def money_sum(values: list[float | None]) -> float | None:
    known = [v for v in values if v is not None]
    return round(sum(known), 2) if known else None


def diff(a: float | None, b: float | None) -> float | None:
    return round(a - b, 2) if a is not None and b is not None else None


def pct_diff(higher: float | None, base: float | None) -> float | None:
    if higher is None or base is None or base <= 0:
        return None
    return round(((higher - base) / base) * 100.0, 1)


def pct_of(amount: float | None, base: float | None) -> float | None:
    return round((amount / base) * 100.0, 1) if amount and base else None


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------
def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "step"


def norm_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def best_match(
    name: str,
    candidates: list[str],
    threshold: float = 0.4,
    containment: bool = True,
    norm: Callable[[str], str] = norm_name,
) -> int | None:
    """Index of the candidate most similar to `name` (exact > containment > token Jaccard), or None.

    Turn `containment` off where a generic word would collide, e.g. 'Coffee' inside 'Good Day Coffee'.
    """
    target = norm(name)
    target_tokens = set(target.split())
    best_idx, best_score = None, 0.0
    for idx, cand in enumerate(candidates):
        c = norm(cand)
        if not target or not c:
            score = 0.0
        elif c == target:
            score = 1.0
        elif containment and (target in c or c in target):
            score = 0.8
        else:
            c_tokens = set(c.split())
            score = len(target_tokens & c_tokens) / len(target_tokens | c_tokens)
        if score > best_score:
            best_idx, best_score = idx, score
    return best_idx if best_score >= threshold else None


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
class FailureReason(StrEnum):
    """Why a run did not finish; written next to `status` as `failure_reason`."""

    BLOCKED = "blocked"
    OUT_OF_STOCK = "out_of_stock"
    FLOW_CHANGED = "flow_changed"
    ELEMENT_NOT_FOUND = "element_not_found"
    EXTRACT_FAILED = "extract_failed"
    TIMEOUT = "timeout"
    UNSAFE_ACTION_REFUSED = "unsafe_action_refused"


class AuditError(RuntimeError):
    """A run ended for a known reason. The runner records `reason` and `status`."""

    reason: FailureReason = FailureReason.FLOW_CHANGED
    status: str = "error"


class Blocked(AuditError):
    """The site put a mandatory login wall, CAPTCHA or bot check in the way."""

    reason = FailureReason.BLOCKED
    status = "blocked"


class UnsafeActionRefused(BaseException):
    """The purchase guard refused an action. Nothing further is done on that site.

    Deliberately not an `Exception`: no `except Exception` or `suppress(Exception)` anywhere in a
    workflow, present or future, can swallow a refusal and carry on. Only the runner catches it.
    """

    reason = FailureReason.UNSAFE_ACTION_REFUSED
    status = "error"


@dataclass
class SiteResult:
    """Everything one audit run learns about one site. Workflows fill it in; the runner writes it out."""

    site: str
    out_dir: Path
    status: str = "error"
    message: str = ""
    failure_reason: FailureReason | None = None
    shop_url: str | None = None
    checkout_url: str | None = None
    search: dict | None = None  # what was searched for, if the workflow searches
    items: list[dict] = field(default_factory=list)
    extras: list[dict] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    comparison: dict = field(default_factory=dict)

    @property
    def added(self) -> list[dict]:
        return [i for i in self.items if i.get("status") == "added"]


def new_item(name: str, price: str | None, **extra) -> dict:
    """An item we intend to buy; `status` moves from 'skipped' to 'added' or 'add_failed'."""
    return {
        "name": name,
        "shop_price_display": price,
        "shop_price": parse_price(price),
        "selected_variant": None,
        "selected_price_display": None,
        "selected_price": None,
        "status": "skipped",
        **extra,
    }


def record_offers(result: SiteResult, step: int, step_type: str, heading: str | None, offers: list) -> None:
    """Append each offered extra/fee to `result.extras` once per (step type, category, label)."""
    seen = {(e["step_type"], e["category"], e["label"].lower()) for e in result.extras}
    for offer in offers:
        key = (step_type, offer.category, offer.label.lower())
        if key in seen:
            continue
        seen.add(key)
        result.extras.append({
            "step": step,
            "step_type": step_type,
            "step_heading": heading,
            "category": offer.category,
            "label": offer.label,
            "price_display": offer.price,
            "price": parse_amount(offer.price),
            "price_basis": offer.price_basis,
            "selected_by_default": offer.selected,  # we never add extras, so anything selected was pre-selected
            "mandatory": offer.mandatory,
        })


def summarise_extras(extras: list[dict]) -> dict:
    preselected = [e["label"] for e in extras if e["selected_by_default"] and not e["mandatory"] and (e["price"] or 0) > 0]
    return {
        "extras_offered": len(extras),
        "extras_offered_by_category": dict(Counter(e["category"] for e in extras)),
        "extras_preselected": preselected,
    }


def pop_best_line(lines: list[LineItem], **match_options) -> Callable[[dict], LineItem | None]:
    """A finder that hands each item its best-matching checkout line, using every line at most once."""
    remaining = list(lines)

    def find(item: dict) -> LineItem | None:
        idx = best_match(item["name"], [line.name for line in remaining], **match_options)
        return remaining.pop(idx) if idx is not None else None

    return find


def compare_item_prices(added: list[dict], find_line: Callable[[dict], LineItem | None]) -> float | None:
    """Fill in each added item's expected vs checkout unit price; return the expected subtotal."""
    for item in added:
        expected = item["selected_price"] if item["selected_price"] is not None else item["shop_price"]
        line = find_line(item)
        unit = parse_price(line.price) if line else None
        if unit is not None and line.quantity > 1:
            unit = round(unit / line.quantity, 2)
        item["expected_price"] = expected
        item["selected_vs_shop_diff"] = diff(item["selected_price"], item["shop_price"])
        item["checkout_price_display"] = line.price if line else None
        item["checkout_price"] = unit
        item["checkout_vs_expected_diff"] = diff(unit, expected)
        item["price_increased"] = (item["checkout_vs_expected_diff"] or 0) > 0
    return money_sum([i["expected_price"] for i in added])


def drip_verdict(comparison: dict) -> str:
    return "YES - " + "; ".join(comparison["drip_reasons"]) if comparison["drip_detected"] else "NO"


# ---------------------------------------------------------------------------
# Report files
# ---------------------------------------------------------------------------
def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_reports(result: SiteResult, item_fields: list[str] = ITEM_FIELDS) -> None:
    """Written for every run, however it ended, so whatever was captured is kept."""
    report = {
        "site": result.site,
        "shop_url": result.shop_url,
        "checkout_url": result.checkout_url,
        "status": result.status,
        "message": result.message,
        "failure_reason": result.failure_reason,
        **({"search": result.search} if result.search else {}),
        "items": result.items,
        **result.comparison,
        "extras": result.extras,
        "steps": result.steps,
    }
    out = result.out_dir
    (out / "price_comparison.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    write_csv(out / "price_comparison.csv", item_fields, result.items)
    if result.extras:
        write_csv(out / "extras.csv", EXTRA_FIELDS, result.extras)


def update_summary(result: SiteResult) -> None:
    """Keep one row per site (its latest run) in output/summary.csv, whichever workflow produced it."""
    path = OUTPUT_ROOT / "summary.csv"
    c = result.comparison
    row = {
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "site": result.site,
        "status": result.status,
        "message": result.message,
        "items_added": c.get("items_added", 0),
        "expected_subtotal": c.get("expected_subtotal"),
        "checkout_subtotal": c.get("checkout_subtotal"),
        "checkout_total": c.get("checkout_total"),
        "currency": c.get("currency"),
        "other_fees_total": c.get("other_fees_total"),
        "shipping": c.get("shipping"),
        "drip_detected": c.get("drip_detected"),
        "output_dir": str(result.out_dir),
    }
    rows: list[dict] = []
    if path.exists():
        with open(path, newline="", encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r.get("site") != result.site]
    write_csv(path, list(row), rows + [row])
