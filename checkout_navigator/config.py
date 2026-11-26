"""Every setting the navigator uses, with deterministic defaults.

Values come from the dataclass defaults below, overridden by environment variables (or a `.env` file in the
working directory). Nothing here is site-specific: per-domain timing quirks live in `site_hints.yaml`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).resolve().parent
SITE_HINTS_PATH = PACKAGE_DIR / "site_hints.yaml"

DEFAULT_MODEL = "google/gemini-3.8-flash"
LLM_TIMEOUT_MS = 90_000  # Stagehand timeouts are in milliseconds

# Placeholder shopper typed into contact / passenger / guest forms. Typed values reach the page as Stagehand
# `%name%` variables, so they are substituted after the model answers and never appear in a prompt.
# The mobile number is in ACMA's reserved range for fictional use (0491 570 xxx).
DEFAULT_PERSONA: dict[str, str] = {
    "title": "Mr",
    "first_name": "Alex",
    "last_name": "Testbooking",
    "full_name": "Alex Testbooking",
    "gender": "Male",
    "date_of_birth": "15/06/1988",
    "email": "alex.testbooking@example.com",
    "phone": "0491 570 156",
    "country": "Australia",
}
# Chosen from a list rather than typed, so the model has to be told the value. None of these identifies anyone.
CHOSEN_PERSONA_FIELDS = frozenset({"title", "gender", "country"})

DEFAULT_SEARCH: dict[str, Any] = {"origin": "CBR", "destination": "SYD", "city": "Sydney", "guests": 1}

# Merged from the legacy workflows' safety prompts (coffee, qantas, ubereats, ticketek).
SYSTEM_PROMPT = (
    "You are navigating an online purchase or booking flow on behalf of a tester. The goal is only to REACH the "
    "payment step, never to complete it. Never enter payment card, gift card, voucher, promo code, travel credit or "
    "loyalty points details. Never click a button that pays for, purchases, places, submits or confirms an order or "
    "booking (e.g. 'Pay now', 'Place order', 'Complete purchase', 'Confirm and pay', 'Submit payment', 'Buy now' on "
    "a payment page). Never create an account, join a membership or loyalty program, or start a free trial. Prefer "
    "guest checkout. Decline every optional extra, add-on, upgrade and insurance; where a choice is mandatory, pick "
    "the free, default or cheapest option. Buttons such as 'Continue', 'Next', 'Checkout', 'Confirm details' and "
    "'Accept' (terms or fare rules) are allowed: they are not purchases."
)


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal `.env` reader (KEY=VALUE lines); variables already set in the environment win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    return default if value is None else value.lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class LLMConfig:
    model: str = DEFAULT_MODEL
    api_key: str | None = None
    timeout_ms: int = LLM_TIMEOUT_MS

    @property
    def stagehand_kwargs(self) -> dict[str, Any]:
        options: dict[str, Any] = {"model": self.model}
        if self.api_key:
            options["model_api_key"] = self.api_key
        return options


@dataclass(frozen=True)
class BrowserConfig:
    user_data_dir: Path = Path(".chrome-profile")
    headless: bool = False
    chrome_path: Path | None = None
    proxy_server: str | None = None
    proxy_bypass: str | None = None
    viewport: tuple[int, int] = (1440, 1000)


@dataclass(frozen=True)
class NavigatorConfig:
    max_steps: int = 15
    default_address: str = "240 City Walk, Canberra ACT 2601"
    search_defaults: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_SEARCH))
    persona: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PERSONA))
    depart_offset_days: int = 14
    return_offset_days: int = 17
    llm: LLMConfig = field(default_factory=LLMConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    system_prompt: str = SYSTEM_PROMPT
    interactive: bool = True  # may pause a headed run for a manual sign-in when no guest path exists

    def dates(self, today: date | None = None) -> dict[str, date]:
        """Target travel/stay dates: depart / check-in and return / check-out."""
        today = today or datetime.now().astimezone().date()
        return {
            "depart": today + timedelta(days=self.depart_offset_days),
            "return": today + timedelta(days=self.return_offset_days),
        }

    def context(self, hints: dict[str, Any] | None = None) -> dict[str, Any]:
        """The plain dict the control loop and helpers read."""
        return {
            "max_steps": self.max_steps,
            "default_address": self.default_address,
            "search_defaults": dict(self.search_defaults),
            "persona": dict(self.persona),
            "dates": self.dates(),
            "hints": hints or {},
            "llm_timeout_ms": self.llm.timeout_ms,
            "interactive": self.interactive and not self.browser.headless,
        }


def load_config(**overrides: Any) -> NavigatorConfig:
    """Defaults, then environment / `.env`, then explicit keyword overrides (from the CLI)."""
    _load_dotenv()
    persona = dict(DEFAULT_PERSONA)
    for key, default in DEFAULT_PERSONA.items():
        persona[key] = _env(f"PERSONA_{key.upper()}", default) or default

    search = dict(DEFAULT_SEARCH)
    for key in ("origin", "destination", "city"):
        search[key] = _env(f"SEARCH_{key.upper()}", search[key])
    search["guests"] = int(_env("SEARCH_GUESTS", str(search["guests"])) or 1)

    api_key = _env("MODEL_API_KEY") or _env("GOOGLE_GENERATIVE_AI_API_KEY") or _env("ANTHROPIC_API_KEY")
    model = _env("NAVIGATOR_MODEL") or _env("GEMINI_MODEL")
    if model and "/" not in model:
        model = f"google/{model}"
    llm = LLMConfig(model=model or DEFAULT_MODEL, api_key=api_key)

    chrome = _env("CHROME_PATH")
    browser = BrowserConfig(
        user_data_dir=Path(_env("CHROME_PROFILE_DIR", ".chrome-profile") or ".chrome-profile"),
        headless=_env_bool("HEADLESS", False),
        chrome_path=Path(chrome).expanduser() if chrome else None,
        proxy_server=_env("BROWSER_PROXY_SERVER"),
        proxy_bypass=_env("BROWSER_PROXY_BYPASS"),
    )

    values: dict[str, Any] = {
        "max_steps": int(_env("MAX_STEPS", "15") or 15),
        "default_address": _env("DEFAULT_ADDRESS", NavigatorConfig.default_address),
        "search_defaults": search,
        "persona": persona,
        "llm": llm,
        "browser": browser,
    }
    browser_overrides = {
        k: v for k in list(overrides) if k in BrowserConfig.__dataclass_fields__ and (v := overrides.pop(k)) is not None
    }
    if browser_overrides:
        values["browser"] = BrowserConfig(**{**browser.__dict__, **browser_overrides})
    values.update({k: v for k, v in overrides.items() if v is not None})
    return NavigatorConfig(**values)
