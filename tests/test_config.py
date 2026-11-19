"""One typed settings object replaces every os.getenv and the import-time side effects."""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from audit.config import ConfigError, Settings, keep_loopback_off_proxy, load_settings

REPO = Path(__file__).parent.parent
ENV_NAMES = [
    "GOOGLE_GENERATIVE_AI_API_KEY", "GEMINI_MODEL", "HEADLESS", "CHROME_PATH", "BROWSERBASE_API_KEY",
    "BROWSERBASE_PROJECT_ID", "UBEREATS_ADDRESS", "PAX_TITLE", "PAX_FIRST_NAME", "PAX_LAST_NAME", "PAX_GENDER",
    "PAX_DATE_OF_BIRTH", "PAX_EMAIL", "PAX_PHONE", "PAX_COUNTRY", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT",
    "AZURE_OPENAI_API_VERSION", "AZURE_OPENAI_MANAGED_IDENTITY",
]  # fmt: skip


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_defaults(clean_env):
    settings = load_settings()
    assert settings.model == "google/gemini-3.8-flash"
    assert settings.headless is False
    assert settings.chrome_path is None
    assert settings.use_browserbase is False
    assert settings.ubereats_address == "260 City Walk, Canberra ACT 2601"
    assert (settings.pax_first_name, settings.pax_email) == ("Alex", "alex.testbooking@example.com")
    with pytest.raises(ConfigError, match="GOOGLE_GENERATIVE_AI_API_KEY"):
        settings.require_api_key()


def test_environment_and_dotenv(clean_env, monkeypatch):
    chrome = clean_env / "chrome"
    chrome.write_text("")
    (clean_env / ".env").write_text(
        f"GOOGLE_GENERATIVE_AI_API_KEY=from-dotenv\nGEMINI_MODEL=google/gemini-x\nHEADLESS=true\nCHROME_PATH={chrome}\n"
        "BROWSERBASE_API_KEY=\nPAX_FIRST_NAME=Sam\nUBEREATS_ADDRESS=1 Test St, Sydney NSW 2000\nUNRELATED=ignored\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GOOGLE_GENERATIVE_AI_API_KEY", "from-env")  # the environment wins over .env
    settings = load_settings()
    assert settings.require_api_key() == "from-env"
    assert settings.model == "google/gemini-x"  # no doubled prefix
    assert settings.headless is True
    assert settings.chrome_path == chrome
    assert settings.browserbase_api_key is None  # `KEY=` means unset
    assert settings.pax_first_name == "Sam"
    assert settings.ubereats_address == "1 Test St, Sydney NSW 2000"


def test_invalid_values_exit_with_a_message(clean_env, monkeypatch):
    monkeypatch.setenv("CHROME_PATH", str(clean_env / "missing-chrome"))
    with pytest.raises(ConfigError, match="CHROME_PATH"):
        load_settings()
    monkeypatch.delenv("CHROME_PATH")
    monkeypatch.setenv("HEADLESS", "sometimes")
    with pytest.raises(ConfigError, match="HEADLESS"):
        load_settings()


def test_settings_are_immutable():
    with pytest.raises(Exception, match="frozen"):
        Settings().headless = True  # type: ignore[misc]


def test_no_proxy_is_extended_not_replaced(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "corp.internal, localhost")
    monkeypatch.delenv("no_proxy", raising=False)
    keep_loopback_off_proxy()
    assert os.environ["NO_PROXY"] == os.environ["no_proxy"] == "corp.internal,localhost,127.0.0.1"


def test_importing_the_package_has_no_side_effects(tmp_path):
    code = (
        "import os, sys; before = dict(os.environ); out, err = sys.stdout, sys.stderr\n"
        "import audit, audit.cli, audit.workflows\n"
        "assert dict(os.environ) == before, 'environment changed'\n"
        "assert (sys.stdout, sys.stderr) == (out, err), 'streams replaced'\n"
    )
    (tmp_path / ".env").write_text("GOOGLE_GENERATIVE_AI_API_KEY=should-not-be-read-on-import\n", encoding="utf-8")
    done = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(REPO)},
                          capture_output=True, text=True, timeout=120, check=False)  # fmt: skip
    assert done.returncode == 0, done.stderr


def test_only_config_reads_the_environment():
    offenders = [
        str(path.relative_to(REPO))
        for path in (REPO / "audit").rglob("*.py")
        if path.name != "config.py" and re.search(r"os\.(getenv|environ)|load_dotenv", path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_env_example_documents_every_setting():
    documented = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", (REPO / ".env.example").read_text(encoding="utf-8"), re.M))
    assert documented == {name.upper() for name in Settings.model_fields}
