"""The analyst's review of one run, kept beside its evidence as ``review.json``.

There is one reviewer, so a review is a single record that a later save replaces.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REVIEW_FILE = "review.json"

# Verdict -> what it means for the final drip result (None: the analyst could not tell).
VERDICTS: dict[str, bool | None] = {"drip": True, "no_drip": False, "inconclusive": None}
VERDICT_LABELS = {"drip": "Drip confirmed", "no_drip": "No drip", "inconclusive": "Inconclusive"}


def read_review(run_path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((run_path / REVIEW_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("verdict") not in VERDICTS:
        return None
    return data


def save_review(run_path: Path, verdict: str, reasons: dict[str, bool], notes: str) -> dict[str, Any]:
    if verdict not in VERDICTS:
        raise ValueError(f"unknown verdict {verdict!r}")
    review = {
        "verdict": verdict,
        "reasons": reasons,
        "notes": notes.strip(),
        "reviewed_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    # Write to a temporary file and rename it, so a crash never leaves half a review.
    handle, tmp = tempfile.mkstemp(dir=run_path, prefix=".review-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(review, out, indent=2)
        Path(tmp).replace(run_path / REVIEW_FILE)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return review


def clear_review(run_path: Path) -> None:
    (run_path / REVIEW_FILE).unlink(missing_ok=True)
