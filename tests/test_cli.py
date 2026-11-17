"""The command line works without a browser, an API key or a .env file."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from audit.cli import build_parser
from audit.config import Settings

REPO = Path(__file__).parent.parent
WORKFLOWS = ["coffee", "virgin", "ubereats", "ticketek"]


def run_main(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GOOGLE_", "GEMINI_", "BROWSERBASE_"))}
    env["PYTHONPATH"] = str(REPO)
    return subprocess.run(
        [sys.executable, str(REPO / "main.py"), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_main_help(tmp_path):
    done = run_main("--help", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    for name in WORKFLOWS:
        assert name in done.stdout


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_subcommand_help(tmp_path, workflow):
    done = run_main(workflow, "--help", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert "--local" in done.stdout
    assert "--login" in done.stdout


def test_missing_api_key_is_a_clear_error(tmp_path):
    done = run_main("coffee", "https://shop.test", cwd=tmp_path)
    assert done.returncode != 0
    assert "GOOGLE_GENERATIVE_AI_API_KEY" in done.stderr
    assert "Traceback" not in done.stderr


def test_options_are_stable():
    parser = build_parser(Settings(ubereats_address="9 Default St"))
    coffee = parser.parse_args(["coffee", "a.test", "b.test", "--products", "5"])
    assert (coffee.urls, coffee.products, coffee.local, coffee.login) == (["a.test", "b.test"], 5, False, False)
    virgin = parser.parse_args(["virgin", "--origin", "BNE", "--destination", "PER", "--date", "2099-01-01"])
    assert (virgin.origin, virgin.destination, virgin.date.isoformat()) == ("BNE", "PER", "2099-01-01")
    uber = parser.parse_args(
        [
            "ubereats",
            "--address",
            "1 Test St",
            "--query",
            "pizza",
            "--items",
            "2",
            "--restaurant-url",
            "https://r.test",
            "--skip-direct-site",
            "--local",
        ]
    )
    assert (uber.address, uber.query, uber.items, uber.restaurant_url, uber.skip_direct_site, uber.local) == (
        "1 Test St",
        "pizza",
        2,
        "https://r.test",
        True,
        True,
    )
    defaults = parser.parse_args(["ubereats", "--products", "3"])
    assert defaults.items == 3  # alias kept
    assert defaults.address == "9 Default St"  # UBEREATS_ADDRESS is the default for --address


def test_virgin_rejects_past_dates():
    with pytest.raises(SystemExit):
        build_parser(Settings()).parse_args(["virgin", "--date", "2020-01-01"])
