"""Browser-free helpers shared by every workflow: price parsing, name matching, results and report files."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

OUTPUT_ROOT = Path("output")

Item = dict[str, Any]  # one thing we tried to buy; see new_item()
Report = dict[str, Any]  # the price comparison of one run

ITEM_FIELDS = [
    "name", "selected_variant", "shop_price_display", "selected_price_display",
    "checkout_price_display", "checkout_vs_expected_diff", "price_increased", "status",
]  # fmt: skip
EXTRA_FIELDS = [
    "step", "step_type", "step_heading", "category", "label",
    "price_display", "price", "price_basis", "selected_by_default", "mandatory",
]  # fmt: skip
SUMMARY_FIELDS = [
    "run_at", "site", "status", "message", "items_added", "expected_subtotal", "checkout_subtotal",
    "checkout_total", "currency", "other_fees_total", "shipping", "drip_detected", "output_dir",
]  # fmt: skip
RUN_FIELDS = [*SUMMARY_FIELDS, "workflow", "target", "failure_reason"]  # runs.csv: the summary columns plus the key


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
_CURRENCY = r"(?:[$€£]|\b(?:AUD|USD|NZD|EUR|GBP)\b)"
# 1,234.56 | 1.234,56 | 1 234,50 (a space only as a thousands separator) | .50
_NUMBER = re.compile(r"(?:\d{1,3}(?:[  ]\d{3})+|\d+)(?:[.,]\d+)*|(?<![\d.,])\.\d+")
_MARKED_BEFORE = re.compile(_CURRENCY + r"\s?$", re.IGNORECASE)
_MARKED_AFTER = re.compile(r"^\s?" + _CURRENCY, re.IGNORECASE)
_NEGATIVE_BEFORE = re.compile(r"[-−–(]\s*(?:[A-Za-z]{1,3}\s?)?[$€£]?\s*$")
_CENT = Decimal("0.01")
_TENTH = Decimal("0.1")


def _to_decimal(number: str) -> Decimal | None:
    """Read one numeric token, working out which of ',' and '.' is the decimal separator."""
    number = re.sub(r"[  ]", "", number)
    if "," in number and "." in number:
        # Whichever separator comes last is the decimal separator.
        if number.rfind(",") > number.rfind("."):
            number = number.replace(".", "").replace(",", ".")
        else:
            number = number.replace(",", "")
    elif "," in number:
        head, *groups = number.split(",")
        decimal_comma = len(groups) == 1 and len(groups[0]) in (1, 2)  # '12,50', '1,5'; '1,234' is a thousand
        number = f"{head}.{groups[0]}" if decimal_comma else number.replace(",", "")
    elif number.count(".") > 1:
        number = number.replace(".", "")  # '1.234.567'
    try:
        return Decimal(number)
    except InvalidOperation:
        return None


def parse_price(value: str | None) -> float | None:
    """Read the amount out of a displayed price; None if there is no usable number.

    '$1,234.56', '1.234,56 €', 'AUD $21.00', 'From $15', '+$3.50' and '12,50' are read as written.
    A number attached to a currency marker wins over a bare one ('2 for $30' is 30). A leading
    minus or brackets make it negative ('-$11.85'). 'Free' is 0.0, but a conditional offer such
    as 'Free on $50+' is not a price at all.
    """
    if not value:
        return None
    text = value.strip()
    tokens = list(_NUMBER.finditer(text))
    if re.match(r"free\b", text, re.IGNORECASE):
        return None if tokens else 0.0
    if not tokens:
        return 0.0 if re.search(r"\bfree\b", text, re.IGNORECASE) else None
    marked = (t for t in tokens if _MARKED_BEFORE.search(text[: t.start()]) or _MARKED_AFTER.search(text[t.end() :]))
    token = next(marked, tokens[0])
    amount = _to_decimal(token.group(0))
    if amount is None:
        return None
    negative = bool(_NEGATIVE_BEFORE.search(text[: token.start()]))
    return float(-amount if negative else amount)


def parse_amount(value: str | None) -> float | None:
    """Like parse_price, but None for percentages ('1.3%'), which are rates rather than amounts."""
    return None if value and "%" in value else parse_price(value)


def _dec(value: float) -> Decimal:
    return Decimal(str(value))


def _rounded(value: Decimal, step: Decimal = _CENT) -> float:
    return float(value.quantize(step, rounding=ROUND_HALF_UP))


def money_sum(values: Iterable[float | None]) -> float | None:
    """Sum of the known amounts to the cent; None if none is known."""
    known = [_dec(v) for v in values if v is not None]
    return _rounded(sum(known, Decimal(0))) if known else None


def diff(a: float | None, b: float | None) -> float | None:
    """a - b to the cent; None if either is unknown."""
    return _rounded(_dec(a) - _dec(b)) if a is not None and b is not None else None


def unit_price(line_total: float, quantity: int) -> float:
    return _rounded(_dec(line_total) / quantity) if quantity > 1 else line_total


def pct_diff(higher: float | None, base: float | None) -> float | None:
    """How much `higher` exceeds `base`, in percent to one decimal; None without a positive base."""
    if higher is None or base is None or base <= 0:
        return None
    return _rounded((_dec(higher) - _dec(base)) / _dec(base) * 100, _TENTH)


def pct_of(amount: float | None, base: float | None) -> float | None:
    """`amount` as a percentage of `base` to one decimal; None if either is unknown or the base is zero."""
    if amount is None or not base:
        return None
    return _rounded(_dec(amount) / _dec(base) * 100, _TENTH)


def positive(value: float | None) -> bool:
    """True only for a known amount above zero (unknown is not 'more')."""
    return value is not None and value > 0


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------
def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "step"


def norm_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def best_match(
    name: str,
    candidates: Sequence[str],
    threshold: float = 0.4,
    containment: bool = True,
    norm: Callable[[str], str] = norm_name,
) -> int | None:
    """Index of the candidate most similar to `name` (exact > containment > token Jaccard), or None.

    Containment means every word of one name appears in the other; among such candidates the one
    sharing the largest fraction of words wins. Turn `containment` off where a generic word would
    collide, e.g. 'Coffee' inside 'Good Day Coffee'.
    """
    target = norm(name)
    target_tokens = set(target.split())
    best_idx, best_score = None, 0.0
    for idx, cand in enumerate(candidates):
        c = norm(cand)
        c_tokens = set(c.split())
        if not target or not c:
            score = 0.0
        elif c == target:
            score = 1.0
        else:
            score = len(target_tokens & c_tokens) / len(target_tokens | c_tokens)
            if containment and (target_tokens <= c_tokens or c_tokens <= target_tokens):
                score = 0.8 + score / 10
        if score > best_score:
            best_idx, best_score = idx, score
    return best_idx if best_score >= threshold else None


# ---------------------------------------------------------------------------
# Outcomes
# ---------------------------------------------------------------------------
class Status(StrEnum):
    """How a run ended; the `status` value in every report."""

    OK = "ok"
    ERROR = "error"
    BLOCKED = "blocked"
    NO_CHECKOUT = "no_checkout"
    ADD_TO_CART_FAILED = "add_to_cart_failed"
    NO_COFFEE_FOUND = "no_coffee_found"
    NO_FLIGHTS_FOUND = "no_flights_found"
    NO_ITEMS_FOUND = "no_items_found"


class FailureReason(StrEnum):
    """Why a run did not finish; written next to `status` as `failure_reason`."""

    BLOCKED = "blocked"
    OUT_OF_STOCK = "out_of_stock"
    FLOW_CHANGED = "flow_changed"
    ELEMENT_NOT_FOUND = "element_not_found"
    EXTRACT_FAILED = "extract_failed"
    TIMEOUT = "timeout"
    UNSAFE_ACTION_REFUSED = "unsafe_action_refused"


# The reason that goes with each status a workflow sets without raising.
STATUS_REASONS: dict[str, FailureReason] = {
    Status.BLOCKED: FailureReason.BLOCKED,
    Status.NO_CHECKOUT: FailureReason.FLOW_CHANGED,
    Status.ADD_TO_CART_FAILED: FailureReason.ELEMENT_NOT_FOUND,
    Status.NO_COFFEE_FOUND: FailureReason.OUT_OF_STOCK,
    Status.NO_FLIGHTS_FOUND: FailureReason.OUT_OF_STOCK,
    Status.NO_ITEMS_FOUND: FailureReason.OUT_OF_STOCK,
}


# What a browser step can fail with and a workflow may recover from: Stagehand's RPC and CDP errors
# (and our own AuditError) are RuntimeErrors, waits raise TimeoutError, sockets raise OSError. Programming
# errors (TypeError, KeyError, AttributeError, ...) are deliberately not in here, so they are never hidden.
BROWSER_ERRORS: tuple[type[Exception], ...] = (RuntimeError, TimeoutError, OSError)


class AuditError(RuntimeError):
    """A run ended for a known reason. The runner records `status` and `reason`."""

    reason: FailureReason = FailureReason.FLOW_CHANGED
    status: Status = Status.ERROR


class Blocked(AuditError):
    """The site put a mandatory login wall, CAPTCHA or bot check in the way. Never worked around."""

    reason = FailureReason.BLOCKED
    status = Status.BLOCKED


class FlowChanged(AuditError):
    """The site no longer behaves the way the workflow expects (a step led somewhere unexpected)."""


class ElementNotFound(AuditError):
    """Something the workflow needs to click or read is not on the page."""

    reason = FailureReason.ELEMENT_NOT_FOUND


class ExtractFailed(AuditError):
    """The model's extraction failed or did not fit the schema."""

    reason = FailureReason.EXTRACT_FAILED


class StepTimeout(AuditError):
    """An LLM call, a navigation or a wait ran out of time."""

    reason = FailureReason.TIMEOUT


class PageScriptError(AuditError):
    """A JavaScript snippet threw in the page: a bug in the snippet or a page it was not written for."""


class UnsafeActionRefused(BaseException):
    """The purchase guard refused an action. Nothing further is done on that site.

    Deliberately not an `Exception`: no `except Exception` or `suppress(Exception)` anywhere in a
    workflow, present or future, can swallow a refusal and carry on. Only the runner catches it.
    """

    reason = FailureReason.UNSAFE_ACTION_REFUSED
    status = Status.ERROR


_TIMEOUT_WORDS = re.compile(r"timed? ?out|timeout|deadline exceeded", re.IGNORECASE)


def is_timeout(error: BaseException) -> bool:
    return isinstance(error, TimeoutError) or bool(_TIMEOUT_WORDS.search(str(error)))


def classify_failure(error: BaseException) -> FailureReason | None:
    """The failure reason for an exception the workflows did not raise themselves, if it is recognisable."""
    if isinstance(error, AuditError | UnsafeActionRefused):
        return error.reason
    return FailureReason.TIMEOUT if is_timeout(error) else None


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
@dataclass
class SiteResult:
    """Everything one audit run learns about one site. Workflows fill it in; the runner writes it out."""

    site: str
    out_dir: Path
    status: str = Status.ERROR
    message: str = ""
    failure_reason: FailureReason | None = None
    shop_url: str | None = None
    checkout_url: str | None = None
    search: dict[str, Any] | None = None  # what was searched for, if the workflow searches
    items: list[Item] = field(default_factory=list)
    extras: list[dict[str, Any]] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    comparison: Report = field(default_factory=dict)

    @property
    def added(self) -> list[Item]:
        return [i for i in self.items if i.get("status") == "added"]

    def fail(self, status: Status, message: str) -> None:
        """Record an early exit, with the failure reason that goes with the status."""
        self.status, self.message = status, message
        self.failure_reason = STATUS_REASONS.get(status)


def new_item(name: str, price: str | None, **extra: Any) -> Item:
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


def select_price(item: Item, display: str | None) -> None:
    """Record the price shown for the option we selected; fall back to the shop price only if it is unreadable."""
    parsed = parse_price(display)
    item["selected_price_display"] = display
    item["selected_price"] = parsed if parsed is not None else item["shop_price"]


class Offer(Protocol):
    """An optional extra or fee as the model extracted it (each workflow has its own pydantic model)."""

    category: str
    label: str
    price: str | None
    price_basis: str | None
    selected: bool
    mandatory: bool


def record_offers(result: SiteResult, step: int, step_type: str, heading: str | None, offers: Iterable[Offer]) -> None:
    """Append each offered extra/fee to `result.extras` once per (step type, category, label)."""
    seen = {(e["step_type"], e["category"], e["label"].lower()) for e in result.extras}
    for offer in offers:
        key = (step_type, offer.category, offer.label.lower())
        if key in seen:
            continue
        seen.add(key)
        result.extras.append(
            {
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
            }
        )


def summarise_extras(extras: Sequence[dict[str, Any]]) -> dict[str, Any]:
    preselected = [
        e["label"] for e in extras if e["selected_by_default"] and not e["mandatory"] and positive(e["price"])
    ]
    return {
        "extras_offered": len(extras),
        "extras_offered_by_category": dict(Counter(e["category"] for e in extras)),
        "extras_preselected": preselected,
    }


def pop_best_line(lines: Sequence[LineItem], **match_options: Any) -> Callable[[Item], LineItem | None]:
    """A finder that hands each item its best-matching checkout line, using every line at most once."""
    remaining = list(lines)

    def find(item: Item) -> LineItem | None:
        idx = best_match(item["name"], [line.name for line in remaining], **match_options)
        return remaining.pop(idx) if idx is not None else None

    return find


def compare_item_prices(added: Sequence[Item], find_line: Callable[[Item], Any]) -> float | None:
    """Fill in each added item's expected vs checkout unit price; return the expected subtotal.

    `price_increased` is None when the item has no checkout line to compare with: not read is not "no".
    """
    for item in added:
        expected = item["selected_price"] if item["selected_price"] is not None else item["shop_price"]
        line = find_line(item)
        total = parse_price(line.price) if line else None
        unit = unit_price(total, line.quantity) if line and total is not None else None
        change = diff(unit, expected)
        item["expected_price"] = expected
        item["selected_vs_shop_diff"] = diff(item["selected_price"], item["shop_price"])
        item["checkout_price_display"] = line.price if line else None
        item["checkout_price"] = unit
        item["checkout_vs_expected_diff"] = change
        item["price_increased"] = change > 0 if change is not None else None
    return money_sum(i["expected_price"] for i in added)


def drip_outcome(reasons: Sequence[str], final_price_read: bool) -> dict[str, Any]:
    """The verdict fields of a report.

    `drip_detected` is True if any reason was found (evidence from a partial run still counts),
    False only if the final price screen was read and nothing was found, and None (unknown) otherwise.
    """
    detected = True if reasons else (False if final_price_read else None)
    return {"drip_detected": detected, "drip_reasons": list(reasons), "final_price_read": final_price_read}


def final_price_was_read(checkout: Any, total: float | None) -> bool:
    """True if the model was on the final checkout/payment page, unblocked, and read a total from it."""
    return bool(checkout and checkout.is_checkout_page and not checkout.blocked_reason and total is not None)


def drip_verdict(comparison: Report) -> str:
    if comparison["drip_detected"]:
        return "YES - " + "; ".join(comparison["drip_reasons"])
    return "NO" if comparison["drip_detected"] is False else "UNKNOWN (the final price screen was not read)"


# ---------------------------------------------------------------------------
# Report files
# ---------------------------------------------------------------------------
def atomic_write(path: Path, text: str) -> None:
    """Write `text` to `path` so that a crash leaves either the old file or the new one, never half of it."""
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(temp, path)  # noqa: PTH105 - os.replace is the atomic primitive this function exists for
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def write_csv(path: Path, fields: Sequence[str], rows: Iterable[dict[str, Any]]) -> None:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(fields), extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    atomic_write(path, buffer.getvalue())


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_json(path: Path, data: Any) -> None:
    atomic_write(path, json.dumps(data, indent=2))


def write_reports(result: SiteResult, item_fields: Sequence[str] = ITEM_FIELDS) -> None:
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
    write_json(out / "price_comparison.json", report)
    write_csv(out / "price_comparison.csv", item_fields, result.items)
    if result.extras:
        write_csv(out / "extras.csv", EXTRA_FIELDS, result.extras)


_SEED_WORKFLOWS = {"ubereats.com": "ubereats", "virginaustralia.com": "virgin"}


def _seed_runs(summary_rows: Iterable[dict[str, str]]) -> list[dict[str, Any]]:
    """Carry a summary.csv written before runs.csv existed over as the start of the run history."""
    seeded = []
    for row in summary_rows:
        workflow = next((name for domain, name in _SEED_WORKFLOWS.items() if domain in row.get("site", "")), "coffee")
        seeded.append({**row, "workflow": workflow, "target": row.get("site", ""), "failure_reason": ""})
    return seeded


def latest_per_target(runs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The most recent row of each (workflow, target), least recently run first."""
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in runs:
        key = (str(row.get("workflow", "")), str(row.get("target") or row.get("site", "")))
        latest.pop(key, None)  # re-insert so the order follows the latest run
        latest[key] = row
    return list(latest.values())


def update_summary(result: SiteResult, workflow: str = "", target: str | None = None) -> None:
    """Append this run to output/runs.csv (the full history) and rebuild output/summary.csv from it.

    summary.csv keeps its columns and holds the latest run of each (workflow, target). `target`
    identifies what was audited more precisely than the start URL, so that two restaurants or two
    routes audited from the same start page do not replace each other.
    """
    runs_path, summary_path = OUTPUT_ROOT / "runs.csv", OUTPUT_ROOT / "summary.csv"
    c = result.comparison
    row = {
        "run_at": datetime.now(UTC).isoformat(timespec="seconds"),
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
        "workflow": workflow,
        "target": target or result.site,
        "failure_reason": result.failure_reason or "",
    }
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    history: list[dict[str, Any]] = read_csv(runs_path) if runs_path.exists() else _seed_runs(read_csv(summary_path))
    history.append(row)
    write_csv(runs_path, RUN_FIELDS, history)
    write_csv(summary_path, SUMMARY_FIELDS, latest_per_target(history))
