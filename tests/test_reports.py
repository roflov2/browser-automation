"""The output contract: file names, JSON keys and CSV columns written by audit.core."""

import csv
import json
from pathlib import Path

import pytest

from audit import core
from audit.core import SiteResult, new_item, update_summary, write_reports

SUMMARY_COLUMNS = [
    "run_at",
    "site",
    "status",
    "message",
    "items_added",
    "expected_subtotal",
    "checkout_subtotal",
    "checkout_total",
    "currency",
    "other_fees_total",
    "shipping",
    "drip_detected",
    "output_dir",
]
ITEM_COLUMNS = [
    "name",
    "selected_variant",
    "shop_price_display",
    "selected_price_display",
    "checkout_price_display",
    "checkout_vs_expected_diff",
    "price_increased",
    "status",
]
EXTRA_COLUMNS = [
    "step",
    "step_type",
    "step_heading",
    "category",
    "label",
    "price_display",
    "price",
    "price_basis",
    "selected_by_default",
    "mandatory",
]


@pytest.fixture
def output_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(core, "OUTPUT_ROOT", tmp_path)
    return tmp_path


def make_result(root: Path, site: str = "https://shop.test", folder: str = "shop.test/20260101-000000") -> SiteResult:
    out = root / folder
    out.mkdir(parents=True)
    result = SiteResult(site=site, out_dir=out, status="ok", shop_url=f"{site}/collections/all")
    result.items.append(new_item("Rogue Coffee", "$23.00"))
    result.comparison = {
        "items_added": 1,
        "expected_subtotal": 23.0,
        "checkout_subtotal": 23.0,
        "checkout_total": 23.0,
        "currency": "AUD",
        "other_fees_total": 0.0,
        "shipping": "$5.00",
        "drip_detected": False,
    }
    return result


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_column_contract():
    assert core.ITEM_FIELDS == ITEM_COLUMNS
    assert core.EXTRA_FIELDS == EXTRA_COLUMNS


def test_write_reports_files_and_keys(output_root):
    result = make_result(output_root)
    write_reports(result)
    report = json.loads((result.out_dir / "price_comparison.json").read_text(encoding="utf-8"))
    assert list(report)[:5] == ["site", "shop_url", "checkout_url", "status", "message"]
    assert {"items", "extras", "steps", "drip_detected", "expected_subtotal"} <= set(report)
    assert "search" not in report  # only present for workflows that search
    assert list(read_csv(result.out_dir / "price_comparison.csv")[0]) == ITEM_COLUMNS


def test_write_reports_extras_csv(output_root):
    result = make_result(output_root)
    result.search = {"query": "pizza"}
    result.extras.append(dict.fromkeys(EXTRA_COLUMNS, "x"))
    write_reports(result)
    assert list(read_csv(result.out_dir / "extras.csv")[0]) == EXTRA_COLUMNS
    assert json.loads((result.out_dir / "price_comparison.json").read_text(encoding="utf-8"))["search"] == {
        "query": "pizza"
    }


def test_summary_columns_and_one_row_per_site(output_root):
    update_summary(make_result(output_root, "https://a.test", "a.test/1"))
    update_summary(make_result(output_root, "https://b.test", "b.test/1"))
    update_summary(make_result(output_root, "https://a.test", "a.test/2"))
    rows = read_csv(output_root / "summary.csv")
    assert list(rows[0]) == SUMMARY_COLUMNS
    assert [(r["site"], r["output_dir"]) for r in rows] == [
        ("https://b.test", str(output_root / "b.test/1")),
        ("https://a.test", str(output_root / "a.test/2")),
    ]
    assert rows[0]["drip_detected"] == "False"
    assert rows[0]["expected_subtotal"] == "23.0"
