"""Read the audit output folder: the run list, one run's evidence, and the final drip verdict.

Nothing here imports Streamlit, so it can be tested on its own. Evidence files are only read; the one
file the reviewer writes, ``review.json``, is handled by ``reviews.py``.
"""

from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from reviewer import reviews

# Files shown in their own tab rather than as a generic table.
_KNOWN_JSON = {"price_comparison.json", "session.json", reviews.REVIEW_FILE}
_STEP_PNG = re.compile(r"^step-(\d+)-(.+)\.png$")
_RUN_FOLDER = re.compile(r"^\d{8}-\d{6}$")


class UnsafePath(ValueError):
    """A run id that would resolve outside the output folder."""


@dataclass
class Step:
    number: int
    step_type: str
    data: dict[str, Any]
    screenshot: Path | None


@dataclass
class Run:
    run_id: str
    path: Path
    comparison: dict[str, Any]
    session: dict[str, Any]
    extractions: list[dict[str, Any]]
    tables: dict[str, pd.DataFrame]
    steps: list[Step]
    screenshots: list[Path]  # every PNG that is not a numbered step screenshot
    review: dict[str, Any] | None
    files: list[Path] = field(default_factory=list)

    @property
    def automated_drip(self) -> bool | None:
        return parse_drip(self.comparison.get("drip_detected"))

    @property
    def final_drip(self) -> bool | None:
        return final_drip(self.automated_drip, self.review)

    @property
    def overridden(self) -> bool:
        return is_overridden(self.automated_drip, self.review)


# --- verdicts ---------------------------------------------------------------


def parse_drip(value: Any) -> bool | None:
    """``drip_detected`` as True / False / None (unknown), from JSON or from a CSV cell."""
    if isinstance(value, bool):
        return value
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def final_drip(automated: bool | None, review: dict[str, Any] | None) -> bool | None:
    """The analyst's verdict when there is one, else the automation's. 'Inconclusive' means unknown."""
    if not review or review.get("verdict") not in reviews.VERDICTS:
        return automated
    return reviews.VERDICTS[review["verdict"]]


def is_overridden(automated: bool | None, review: dict[str, Any] | None) -> bool:
    return bool(review) and review.get("verdict") in reviews.VERDICTS and final_drip(automated, review) != automated


def drip_label(value: bool | None) -> str:
    return {True: "Drip", False: "No drip", None: "Unknown"}[value]


# --- paths ------------------------------------------------------------------


def run_dir(output: Path, run_id: str) -> Path:
    """The folder for ``<domain>/<timestamp>``, refusing anything that escapes the output folder."""
    root = output.resolve()
    path = (root / run_id).resolve()
    if path == root or not path.is_relative_to(root) or len(path.relative_to(root).parts) != 2:
        raise UnsafePath(run_id)
    return path


def _run_id_from_output_dir(value: str) -> str:
    """runs.csv stores ``output/<domain>/<ts>``; keep the last two parts whatever the prefix."""
    parts = Path(str(value).replace("\\", "/")).parts
    return "/".join(parts[-2:])


def _timestamp_from_folder(name: str) -> datetime | None:
    try:
        return datetime.strptime(name, "%Y%m%d-%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None


# --- the run list -----------------------------------------------------------

RUN_COLUMNS = [
    "run_id",
    "run_at",
    "workflow",
    "site",
    "target",
    "status",
    "failure_reason",
    "items_added",
    "expected_subtotal",
    "checkout_subtotal",
    "checkout_total",
    "other_fees_total",
    "currency",
    "automated_drip",
    "review_verdict",
    "final_drip",
    "overridden",
    "reviewed",
    "total_vs_expected_diff",
]


def _row_from_folder(path: Path) -> dict[str, Any]:
    """A run-list row for a folder missing from runs.csv, built from its price_comparison.json."""
    data = _read_json(path / "price_comparison.json")
    session = _read_json(path / "session.json")
    return {
        "run_at": _timestamp_from_folder(path.name),
        "workflow": session.get("workflow", ""),
        "site": data.get("site", path.parent.name),
        "target": data.get("site", path.parent.name),
        "status": data.get("status", ""),
        "failure_reason": data.get("failure_reason"),
        "items_added": data.get("items_added"),
        "expected_subtotal": data.get("expected_subtotal"),
        "checkout_subtotal": data.get("checkout_subtotal"),
        "checkout_total": data.get("checkout_total"),
        "other_fees_total": data.get("other_fees_total"),
        "currency": data.get("currency"),
        "drip_detected": data.get("drip_detected"),
    }


def list_runs(output: Path) -> pd.DataFrame:
    """Every run: runs.csv first, plus any run folder it does not list. One row per run folder that exists."""
    rows: dict[str, dict[str, Any]] = {}
    runs_csv = output / "runs.csv"
    if runs_csv.is_file():
        with runs_csv.open(encoding="utf-8", newline="") as handle:
            for raw in csv.DictReader(handle):
                run_id = _run_id_from_output_dir(raw.get("output_dir", ""))
                rows[run_id] = dict(raw)
    for comparison in output.glob("*/*/price_comparison.json"):
        folder = comparison.parent
        if _RUN_FOLDER.match(folder.name):
            run_id = f"{folder.parent.name}/{folder.name}"
            rows.setdefault(run_id, _row_from_folder(folder))

    records = []
    for run_id, raw in rows.items():
        try:
            path = run_dir(output, run_id)
        except UnsafePath:
            continue
        if not path.is_dir():
            continue
        review = reviews.read_review(path)
        automated = parse_drip(raw.get("drip_detected"))
        expected, total = _num(raw.get("expected_subtotal")), _num(raw.get("checkout_total"))
        records.append(
            {
                **raw,
                "run_id": run_id,
                "run_at": pd.to_datetime(raw.get("run_at") or _timestamp_from_folder(path.name), utc=True),
                "target": raw.get("target") or raw.get("site", ""),
                "workflow": raw.get("workflow") or "",
                "failure_reason": raw.get("failure_reason") or None,
                "items_added": _num(raw.get("items_added")),
                "expected_subtotal": expected,
                "checkout_subtotal": _num(raw.get("checkout_subtotal")),
                "checkout_total": total,
                "other_fees_total": _num(raw.get("other_fees_total")),
                "automated_drip": automated,
                "review_verdict": (review or {}).get("verdict"),
                "final_drip": final_drip(automated, review),
                "overridden": is_overridden(automated, review),
                "reviewed": review is not None,
                "total_vs_expected_diff": None if expected is None or total is None else round(total - expected, 2),
            }
        )
    frame = pd.DataFrame(records, columns=RUN_COLUMNS)
    # Keep True / False / None as they are: pandas would otherwise turn an all-bool column into numpy bools.
    for column in ("automated_drip", "final_drip"):
        frame[column] = frame[column].astype(object)
    return frame.sort_values("run_at", ascending=False, ignore_index=True)


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(number) else number


# --- one run ----------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def _read_csv(path: Path) -> pd.DataFrame | None:
    try:
        return pd.read_csv(path, dtype=str, keep_default_na=False)
    except (OSError, ValueError, pd.errors.EmptyDataError):
        return None


def _pair_steps(steps: list[dict[str, Any]], pngs: list[Path]) -> list[Step]:
    """Match each step in price_comparison.json to its ``step-NN-<type>.png``, by number."""
    by_number: dict[int, Path] = {}
    for png in pngs:
        match = _STEP_PNG.match(png.name)
        if match:
            by_number.setdefault(int(match.group(1)), png)
    paired = []
    for index, data in enumerate(steps, start=1):
        number = int(data.get("step") or index)
        paired.append(Step(number, str(data.get("step_type") or ""), data, by_number.pop(number, None)))
    # Screenshots of steps the JSON does not list are kept, so no evidence is hidden.
    for number, png in sorted(by_number.items()):
        match = _STEP_PNG.match(png.name)
        step_type = match.group(2).replace("-", "_") if match else ""
        paired.append(Step(number, step_type, {}, png))
    return sorted(paired, key=lambda step: step.number)


def load_run(output: Path, run_id: str) -> Run:
    path = run_dir(output, run_id)
    files = sorted(p for p in path.iterdir() if p.is_file())
    pngs = [p for p in files if p.suffix.lower() == ".png"]
    comparison = _read_json(path / "price_comparison.json")
    tables = {}
    for csv_path in (p for p in files if p.suffix.lower() == ".csv"):
        table = _read_csv(csv_path)
        if table is not None:
            tables[csv_path.stem] = table
    for extra_json in (p for p in files if p.suffix.lower() == ".json" and p.name not in _KNOWN_JSON):
        tables.setdefault(extra_json.stem, pd.DataFrame([_read_json(extra_json)]))
    return Run(
        run_id=run_id,
        path=path,
        comparison=comparison,
        session=_read_json(path / "session.json"),
        extractions=_read_jsonl(path / "extractions.jsonl"),
        tables=tables,
        steps=_pair_steps(list(comparison.get("steps") or []), pngs),
        screenshots=[p for p in pngs if not _STEP_PNG.match(p.name)],
        review=reviews.read_review(path),
        files=files,
    )


def key_screenshots(run: Run) -> list[Path]:
    """The screenshots that bracket the flow: the start page first, cart, checkout, error last."""
    order = {"cart.png": 1, "checkout.png": 2, "error-before-cleanup.png": 3, "error.png": 4}
    return sorted(run.screenshots, key=lambda p: (order.get(p.name, 0), p.name))


def fee_lines(run: Run) -> list[dict[str, Any]]:
    """Fee lines in a common shape: from fee_breakdown when the workflow writes one, else other_fees."""
    data = run.comparison
    if data.get("fee_breakdown"):
        return [
            {"label": f.get("label"), "amount": f.get("amount"), "mandatory": f.get("mandatory")}
            for f in data["fee_breakdown"]
        ]
    lines = [
        {"label": f.get("label"), "amount": f.get("amount"), "mandatory": True} for f in data.get("other_fees") or []
    ]
    lines += [
        {"label": f"Card surcharge: {f.get('label', '')}", "amount": f.get("amount"), "mandatory": None}
        for f in data.get("payment_surcharges") or []
        if isinstance(f, dict)
    ]
    shipping = data.get("shipping")
    if isinstance(shipping, (int, float)) and shipping:
        lines.append({"label": "Shipping / delivery", "amount": shipping, "mandatory": True})
    return lines
