"""The contract between the shared runner and a site-specific workflow.

A workflow owns everything that is particular to one kind of site: its CLI options, prompts, schemas,
navigation and price comparison. The runner owns everything that is not: the browser, the Stagehand
session, output folders, error handling and the report files.
"""

import argparse
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from typing import ClassVar, Literal
from urllib.parse import urlparse

from stagehand import Page, Stagehand

from .config import Settings
from .core import ITEM_FIELDS, Report, SiteResult, Status

log = logging.getLogger(__name__)


class Workflow(ABC):
    name: ClassVar[str]  # CLI subcommand
    help: ClassVar[str]  # one-line description shown in `main.py --help`

    system_prompt: ClassVar[str | None] = None  # safety rules given to the LLM for the whole session
    log_level: ClassVar[Literal["off", "error", "warn", "info", "debug"]] = (
        "warn"  # Stagehand log level recorded in session.json
    )
    viewport: ClassVar[tuple[int, int] | None] = None  # local browser window size; None for Chrome's default
    # "domain": before each site, clear that site's own cookies (and its subdomains'). Never everything: every
    # workflow shares the .chrome-profile, which also holds the corporate SSO login and other sites' sign-ins.
    clear_cookies: ClassVar[Literal["domain"] | None] = None
    result_class: ClassVar[type[SiteResult]] = SiteResult
    item_fields: ClassVar[Sequence[str]] = ITEM_FIELDS  # columns of price_comparison.csv

    def __init__(self, args: argparse.Namespace, settings: Settings) -> None:
        self.args = args
        self.settings = settings

    @classmethod  # noqa: B027 - an optional hook, deliberately not abstract
    def add_arguments(cls, parser: argparse.ArgumentParser, settings: Settings) -> None:
        """Add this workflow's own command-line options; `settings` supplies environment defaults."""

    @abstractmethod
    def targets(self) -> list[str]:
        """Start URLs to audit, one run each."""

    def login_url(self) -> str:
        """URL to open when priming a login session (`--login` / `--prelogin`)."""
        urls = self.targets()
        url = urls[0] if urls else ""
        return url if url.startswith("http") else f"https://{url}"

    def output_folder(self, url: str) -> str:
        """Folder under output/ that holds this URL's runs."""
        return urlparse(url).netloc.removeprefix("www.")

    @abstractmethod
    async def run(self, stagehand: Stagehand, page: Page, url: str, result: SiteResult) -> None:
        """Drive the site and fill in `result` (status, items, extras, steps, comparison).

        Raise `Blocked` for login walls and bot checks, or another `AuditError` for a specific
        failure; any other exception is recorded as an error.
        Use try/finally for work that must happen however the run ends.
        """

    @staticmethod
    def conclude(result: SiteResult, compare: Callable[[], Report]) -> None:
        """Fill in `result.comparison` from a `finally` block, however the run ended.

        A failure here is logged and recorded, never raised: raising from `finally` would replace
        the exception that actually ended the run.
        """
        try:
            result.comparison = compare()
        except Exception as error:  # noqa: BLE001 - see the docstring
            log.error(f"  Price comparison failed: {type(error).__name__}: {error}")
            if result.status == Status.OK:
                result.status, result.message = Status.ERROR, f"Price comparison failed: {error}"

    def target_key(self, url: str, result: SiteResult) -> str:
        """What this run audited, for runs.csv and summary.csv: the latest run per (workflow, target) is kept.

        Override when one start URL can lead to different things (two restaurants, two routes).
        """
        return url

    def sensitive_values(self) -> dict[str, str]:
        """Values (name -> value) that must not be written to session.json or extractions.jsonl."""
        return {}

    def summarise(self, result: SiteResult) -> None:  # noqa: B027 - optional hook
        """Print the headline numbers once `result.comparison` is filled in."""
