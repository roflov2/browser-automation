"""Shared fixtures. Nothing here opens a browser or calls an LLM."""

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
REPO = Path(__file__).parent.parent


@pytest.fixture(scope="session")
def saved_runs() -> list[dict]:
    """Inputs and expected outputs rebuilt from the seven runs that used to be committed under output/."""
    return json.loads((FIXTURES / "saved_runs.json").read_text(encoding="utf-8"))
