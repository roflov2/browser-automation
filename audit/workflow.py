"""The contract between the shared runner and a site-specific workflow.

A workflow owns everything that is particular to one kind of site: its CLI options, prompts, schemas,
navigation and price comparison. The runner owns everything that is not: the browser, the Stagehand
session, output folders, error handling and the report files.
"""

import argparse
from abc import ABC, abstractmethod
from typing import ClassVar
from urllib.parse import urlparse

from stagehand import Page, Stagehand

from .config import Settings
from .core import ITEM_FIELDS, SiteResult


class Workflow(ABC):
    name: ClassVar[str]  # CLI subcommand
    help: ClassVar[str]  # one-line description shown in `main.py --help`

    system_prompt: ClassVar[str | None] = None  # safety rules given to the LLM for the whole session
    log_level: ClassVar[str] = "warn"  # Stagehand log level recorded in session.json
    persistent_profile: ClassVar[bool] = True  # keep cookies/logins in .chrome-profile between local runs
    viewport: ClassVar[tuple[int, int] | None] = None  # local browser window size; None for Chrome's default
    clear_cookies: ClassVar[str | None] = None  # before each site: "domain" (that site's only), "all" or None
    result_class: ClassVar[type[SiteResult]] = SiteResult
    item_fields: ClassVar[list[str]] = ITEM_FIELDS  # columns of price_comparison.csv

    def __init__(self, args: argparse.Namespace, settings: Settings) -> None:
        self.args = args
        self.settings = settings

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser, settings: Settings) -> None:
        """Add this workflow's own command-line options; `settings` supplies environment defaults."""

    @abstractmethod
    def targets(self) -> list[str]:
        """Start URLs to audit, one run each."""

    def output_folder(self, url: str) -> str:
        """Folder under output/ that holds this URL's runs."""
        return urlparse(url).netloc.removeprefix("www.")

    @abstractmethod
    async def run(self, stagehand: Stagehand, page: Page, url: str, result: SiteResult) -> None:
        """Drive the site and fill in `result` (status, items, extras, steps, comparison).

        Raise `Blocked` for login walls and bot checks; any other exception is recorded as an error.
        Use try/finally for work that must happen however the run ends.
        """

    def summarise(self, result: SiteResult) -> None:  # noqa: B027 - optional hook
        """Print the headline numbers once `result.comparison` is filled in."""
