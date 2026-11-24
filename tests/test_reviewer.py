"""The evidence reviewer's loader and review file. No Streamlit server is started."""

import csv
import json

import pytest

pytest.importorskip("pandas")

from reviewer import loader, reviews

RUN_FIELDS = [
    "run_at", "site", "status", "message", "items_added", "expected_subtotal", "checkout_subtotal",
    "checkout_total", "currency", "other_fees_total", "shipping", "drip_detected", "output_dir",
    "workflow", "target", "failure_reason",
]  # fmt: skip


def _make_run(output, domain, stamp, *, drip=True, steps=(), pngs=(), csv_row=True, total=110.0):
    path = output / domain / stamp
    path.mkdir(parents=True)
    comparison = {
        "site": f"https://{domain}/",
        "status": "ok",
        "expected_subtotal": 100.0,
        "checkout_total": total,
        "other_fees": [{"label": "Service fee", "amount": 10.0}],
        "drip_detected": drip,
        "drip_reasons": ["service fee added"] if drip else [],
        "steps": list(steps),
    }
    (path / "price_comparison.json").write_text(json.dumps(comparison), encoding="utf-8")
    (path / "extractions.jsonl").write_text('{"schema": "Cart", "data": {"total": 110}}\nnot json\n', encoding="utf-8")
    (path / "extras.csv").write_text("label,price\nInsurance,5\n", encoding="utf-8")
    for name in pngs:
        (path / name).write_bytes(b"\x89PNG")
    if csv_row:
        runs_csv = output / "runs.csv"
        new = not runs_csv.exists()
        with runs_csv.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, RUN_FIELDS)
            if new:
                writer.writeheader()
            writer.writerow(
                {
                    "run_at": f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}T00:00:00+00:00",
                    "site": comparison["site"],
                    "status": "ok",
                    "expected_subtotal": "100.0",
                    "checkout_total": str(total),
                    "drip_detected": str(drip) if drip is not None else "",
                    "output_dir": f"output/{domain}/{stamp}",
                    "workflow": "coffee",
                    "target": f"target-{domain}",
                }
            )
    return path


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, True), ("True", True), ("False", False), (False, False), ("", None), (None, None), (float("nan"), None)],
)
def test_parse_drip(value, expected):
    assert loader.parse_drip(value) is expected


@pytest.mark.parametrize(
    ("automated", "verdict", "final", "overridden"),
    [
        (True, None, True, False),
        (True, "drip", True, False),
        (True, "no_drip", False, True),
        (False, "drip", True, True),
        (None, "drip", True, True),
        (True, "inconclusive", None, True),
        (None, "inconclusive", None, False),
    ],
)
def test_final_drip_uses_the_analyst_verdict(automated, verdict, final, overridden):
    review = {"verdict": verdict} if verdict else None
    assert loader.final_drip(automated, review) is final
    assert loader.is_overridden(automated, review) is overridden


def test_list_runs_joins_reviews_and_folders_missing_from_runs_csv(tmp_path):
    first = _make_run(tmp_path, "shop.example", "20261101-100000")
    _make_run(tmp_path, "shop.example", "20261102-100000", total=100.0, drip=False)
    _make_run(tmp_path, "other.example", "20261103-100000", csv_row=False)
    reviews.save_review(first, "no_drip", {"service fee added": False}, " shown upfront ")

    frame = loader.list_runs(tmp_path).set_index("run_id")

    assert list(frame.index) == [
        "other.example/20261103-100000",
        "shop.example/20261102-100000",
        "shop.example/20261101-100000",
    ]
    reviewed = frame.loc["shop.example/20261101-100000"]
    assert reviewed["automated_drip"] is True
    assert reviewed["final_drip"] is False
    assert reviewed["overridden"]
    assert reviewed["review_verdict"] == "no_drip"
    assert reviewed["total_vs_expected_diff"] == pytest.approx(10.0)
    unlisted = frame.loc["other.example/20261103-100000"]
    assert unlisted["target"] == "https://other.example/"
    assert unlisted["final_drip"] is True
    assert not unlisted["reviewed"]


def test_list_runs_skips_csv_rows_whose_folder_is_gone(tmp_path):
    path = _make_run(tmp_path, "shop.example", "20261101-100000")
    for child in path.iterdir():
        child.unlink()
    path.rmdir()
    assert loader.list_runs(tmp_path).empty


def test_load_run_pairs_step_screenshots_and_keeps_unlisted_ones(tmp_path):
    steps = [
        {"step": 1, "step_type": "cart_drawer", "total": 100.0},
        {"step": 2, "step_type": "checkout", "total": 110.0},
    ]
    pngs = [
        "step-01-cart-drawer.png",
        "step-02-checkout.png",
        "step-03-payment.png",
        "checkout.png",
        "cart.png",
        "store.png",
    ]
    _make_run(tmp_path, "shop.example", "20261101-100000", steps=steps, pngs=pngs)

    run = loader.load_run(tmp_path, "shop.example/20261101-100000")

    assert [(s.number, s.step_type, s.screenshot.name) for s in run.steps] == [
        (1, "cart_drawer", "step-01-cart-drawer.png"),
        (2, "checkout", "step-02-checkout.png"),
        (3, "payment", "step-03-payment.png"),
    ]
    assert [p.name for p in loader.key_screenshots(run)] == ["store.png", "cart.png", "checkout.png"]
    assert len(run.extractions) == 1  # the malformed line is skipped
    assert list(run.tables) == ["extras"]
    assert run.automated_drip is True and run.final_drip is True and not run.overridden
    assert loader.fee_lines(run) == [{"label": "Service fee", "amount": 10.0, "mandatory": True}]


@pytest.mark.parametrize("run_id", ["../etc", "shop.example/../../x", "shop.example", "/tmp/x/y", "a/b/c"])
def test_run_dir_refuses_paths_outside_the_output_folder(tmp_path, run_id):
    with pytest.raises(loader.UnsafePath):
        loader.run_dir(tmp_path / "output", run_id)


def test_review_round_trip_and_clear(tmp_path):
    saved = reviews.save_review(tmp_path, "drip", {"fee": True}, "  note  ")
    assert saved["notes"] == "note"
    assert reviews.read_review(tmp_path) == saved
    assert not list(tmp_path.glob(".review-*"))  # no temporary file left behind
    reviews.clear_review(tmp_path)
    assert reviews.read_review(tmp_path) is None


def test_review_rejects_unknown_verdicts_and_ignores_bad_files(tmp_path):
    with pytest.raises(ValueError):
        reviews.save_review(tmp_path, "maybe", {}, "")
    (tmp_path / reviews.REVIEW_FILE).write_text('{"verdict": "maybe"}', encoding="utf-8")
    assert reviews.read_review(tmp_path) is None
    (tmp_path / reviews.REVIEW_FILE).write_text("{", encoding="utf-8")
    assert reviews.read_review(tmp_path) is None


def test_list_runs_reads_the_committed_output_folder():
    """The sample runs in output/ load, and each has the evidence the app shows."""
    output = loader.Path(__file__).parent.parent / "output"
    if not (output / "runs.csv").exists():
        pytest.skip("no output folder")
    frame = loader.list_runs(output)
    assert not frame.empty
    for run_id in frame["run_id"]:
        run = loader.load_run(output, run_id)
        assert run.comparison
        assert all(step.screenshot is not None for step in run.steps)
