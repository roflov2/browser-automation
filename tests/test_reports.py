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


def test_summary_keeps_its_columns_and_the_latest_run_per_target(output_root):
    update_summary(make_result(output_root, "https://a.test", "a.test/1"), "coffee")
    update_summary(make_result(output_root, "https://b.test", "b.test/1"), "coffee")
    update_summary(make_result(output_root, "https://a.test", "a.test/2"), "coffee")
    rows = read_csv(output_root / "summary.csv")
    assert list(rows[0]) == SUMMARY_COLUMNS
    assert [(r["site"], r["output_dir"]) for r in rows] == [
        ("https://b.test", str(output_root / "b.test/1")),
        ("https://a.test", str(output_root / "a.test/2")),
    ]
    assert rows[0]["drip_detected"] == "False"
    assert rows[0]["expected_subtotal"] == "23.0"


def test_runs_csv_keeps_every_run(output_root):
    for folder in ("a.test/1", "a.test/2", "a.test/3"):
        update_summary(make_result(output_root, "https://a.test", folder), "coffee")
    runs = read_csv(output_root / "runs.csv")
    assert list(runs[0]) == [*SUMMARY_COLUMNS, "workflow", "target", "failure_reason"]
    assert [r["output_dir"] for r in runs] == [str(output_root / f"a.test/{n}") for n in (1, 2, 3)]
    assert len(read_csv(output_root / "summary.csv")) == 1


def test_different_targets_from_one_start_url_do_not_replace_each_other(output_root):
    """Every Uber Eats run starts from the same URL; the store is the key (REVIEW H4)."""
    site = "https://www.ubereats.com/au"
    update_summary(make_result(output_root, site, "ubereats.com/1"), "ubereats", "/store/ooshman-belconnen/abc")
    update_summary(make_result(output_root, site, "ubereats.com/2"), "ubereats", "/store/grease-monkey/def")
    update_summary(make_result(output_root, site, "ubereats.com/3"), "ubereats", "/store/ooshman-belconnen/abc")
    rows = read_csv(output_root / "summary.csv")
    assert [r["output_dir"].rsplit("/", 1)[-1] for r in rows] == ["2", "3"]
    assert {r["site"] for r in rows} == {site}  # the site value itself is unchanged


def test_unknown_verdict_and_failure_reason_are_written(output_root):
    result = make_result(output_root)
    result.comparison["drip_detected"] = None
    result.fail(core.Status.NO_CHECKOUT, "Checkout page not reached")
    update_summary(result, "coffee")
    write_reports(result)
    (run,) = read_csv(output_root / "runs.csv")
    assert (run["drip_detected"], run["status"], run["failure_reason"]) == ("", "no_checkout", "flow_changed")
    report = json.loads((result.out_dir / "price_comparison.json").read_text(encoding="utf-8"))
    assert (report["drip_detected"], report["failure_reason"]) == (None, "flow_changed")


def test_existing_summary_seeds_the_run_history(output_root):
    """A summary.csv written before runs.csv existed is carried over, not lost."""
    old = dict.fromkeys(SUMMARY_COLUMNS, "")
    rows = [
        {**old, "site": "https://redbrick.coffee", "status": "ok", "output_dir": "output/redbrick.coffee/1"},
        {**old, "site": "https://www.ubereats.com/au", "status": "ok", "output_dir": "output/ubereats.com/1"},
    ]
    core.write_csv(output_root / "summary.csv", SUMMARY_COLUMNS, rows)
    update_summary(make_result(output_root, "https://b.test", "b.test/1"), "coffee")
    runs = read_csv(output_root / "runs.csv")
    assert [(r["workflow"], r["site"]) for r in runs] == [
        ("coffee", "https://redbrick.coffee"), ("ubereats", "https://www.ubereats.com/au"), ("coffee", "https://b.test"),
    ]  # fmt: skip
    assert len(read_csv(output_root / "summary.csv")) == 3


def test_writes_are_atomic(output_root, monkeypatch):
    """A crash while writing leaves the previous file intact and no temp file behind."""
    update_summary(make_result(output_root, "https://a.test", "a.test/1"), "coffee")
    before = (output_root / "summary.csv").read_text(encoding="utf-8")

    def crash(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(core.os, "replace", crash)
    with pytest.raises(OSError, match="disk full"):
        update_summary(make_result(output_root, "https://b.test", "b.test/1"), "coffee")
    assert (output_root / "summary.csv").read_text(encoding="utf-8") == before
    assert [p.name for p in output_root.iterdir() if p.name.endswith(".tmp")] == []
