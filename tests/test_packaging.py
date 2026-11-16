"""requirements.txt and pyproject.toml list the same pinned runtime dependencies."""

import tomllib
from pathlib import Path

REPO = Path(__file__).parent.parent


def test_requirements_match_pyproject():
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    lines = (REPO / "requirements.txt").read_text(encoding="utf-8").splitlines()
    pinned = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
    assert pinned == project["dependencies"]
    assert project["requires-python"] == ">=3.11"
    assert project["scripts"]["drip-audit"] == "audit.cli:main"
