"""Every setting the package reads from the environment or `.env`, loaded and validated once.

`cli.main` builds one `Settings` and hands it to the runner and the workflow; nothing else in the
package reads `os.environ`, and importing the package has no side effects.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_MODEL = "gemini-3.8-flash"
DEFAULT_AZURE_API_VERSION = "2024-12-01-preview"
LOOPBACK = ("127.0.0.1", "localhost")


class ConfigError(SystemExit):
    """A setting is missing or invalid; exits with a message instead of a traceback."""


class Settings(BaseSettings):
    """Field names are the environment variable names, lower-cased (see `.env.example`)."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", frozen=True)

    google_generative_ai_api_key: str | None = None
    gemini_model: str = DEFAULT_MODEL
    headless: bool = False
    chrome_path: Path | None = None
    browserbase_api_key: str | None = None
    browserbase_project_id: str | None = None

    # Azure OpenAI, signed in with Microsoft Entra ID (see audit/azure.py). Setting the endpoint replaces Gemini.
    azure_openai_endpoint: str | None = None
    azure_openai_deployment: str | None = None
    azure_openai_api_version: str = DEFAULT_AZURE_API_VERSION
    azure_openai_managed_identity: bool = False

    # Guest details typed into the airline booking form by `virgin`. Placeholders, never a real person by default.
    pax_title: str = "Mr"
    pax_first_name: str = "Alex"
    pax_last_name: str = "Testbooking"
    pax_gender: str = "Male"
    pax_date_of_birth: str = "15 June 1988"
    pax_email: str = "alex.testbooking@example.com"
    pax_phone: str = "0400 000 000"
    pax_country: str = "Australia"

    ubereats_address: str = Field(default="260 City Walk, Canberra ACT 2601", min_length=5)

    @field_validator(
        "google_generative_ai_api_key",
        "browserbase_api_key",
        "browserbase_project_id",
        "chrome_path",
        "azure_openai_endpoint",
        "azure_openai_deployment",
        mode="before",
    )
    @classmethod
    def _blank_is_unset(cls, value: object) -> object:
        """`KEY=` in .env means "not set", as it did before."""
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("azure_openai_endpoint")
    @classmethod
    def _azure_endpoint_is_https(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("https://"):
            raise ValueError(f"AZURE_OPENAI_ENDPOINT must be an https:// URL: {value}")
        return value

    @model_validator(mode="after")
    def _azure_needs_a_deployment(self) -> Settings:
        if self.azure_openai_endpoint and not self.azure_openai_deployment:
            raise ValueError("AZURE_OPENAI_DEPLOYMENT is required when AZURE_OPENAI_ENDPOINT is set (e.g. gpt-4o)")
        return self

    @field_validator("chrome_path")
    @classmethod
    def _chrome_exists(cls, value: Path | None) -> Path | None:
        if value is not None and not value.expanduser().is_file():
            raise ValueError(f"CHROME_PATH does not point to a file: {value}")
        return value.expanduser() if value is not None else None

    @property
    def model(self) -> str:
        """The Stagehand model name, e.g. 'google/gemini-3.8-flash'."""
        return "google/" + self.gemini_model.removeprefix("google/")

    @property
    def use_azure(self) -> bool:
        """Azure OpenAI replaces Gemini when its endpoint is set."""
        return self.azure_openai_endpoint is not None

    @property
    def use_browserbase(self) -> bool:
        return bool(self.browserbase_api_key and self.browserbase_project_id)

    def require_api_key(self) -> str:
        if not self.google_generative_ai_api_key:
            raise ConfigError("GOOGLE_GENERATIVE_AI_API_KEY is not set (copy .env.example to .env and fill it in)")
        return self.google_generative_ai_api_key

    def require_azure(self) -> tuple[str, str]:
        """The Azure OpenAI endpoint and deployment name."""
        if not (self.azure_openai_endpoint and self.azure_openai_deployment):
            raise ConfigError("AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_DEPLOYMENT must both be set to use Azure OpenAI")
        return self.azure_openai_endpoint, self.azure_openai_deployment

    def require_model(self) -> None:
        """Stop before anything starts if the chosen model's settings are missing."""
        if self.use_azure:
            self.require_azure()
        else:
            self.require_api_key()


def load_settings() -> Settings:
    """Read the environment and `.env` in the current directory; exit with a readable message if invalid."""
    try:
        return Settings()
    except ValidationError as error:
        problems = "; ".join(f"{'.'.join(map(str, e['loc'])).upper()}: {e['msg']}" for e in error.errors())
        raise ConfigError(f"Invalid configuration: {problems}") from error


def keep_loopback_off_proxy() -> None:
    """The local browser is driven over a loopback CDP connection: add loopback to NO_PROXY, keeping what is there."""
    current = [
        h.strip() for h in (os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or "").split(",") if h.strip()
    ]
    merged = ",".join(dict.fromkeys([*current, *LOOPBACK]))
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = merged
