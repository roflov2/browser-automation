"""Helpers for the tests that drive a real local browser against HTML fixtures (no site, no LLM)."""

import functools
import os
import shutil
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CANDIDATES = (
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome",
    "/opt/pw-browsers/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)  # fmt: skip


def find_chrome() -> str | None:
    """CHROME_PATH if set, else the first Chrome/Chromium found on this machine."""
    for candidate in filter(None, (os.environ.get("CHROME_PATH"), *CANDIDATES)):
        found = shutil.which(candidate)
        if found:
            return found
    return None


def launch_args() -> list[str]:
    """Chrome refuses to sandbox when run as root (CI containers)."""
    return ["--no-sandbox"] if hasattr(os, "geteuid") and os.geteuid() == 0 else []


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass


@contextmanager
def serve(directory: Path) -> Iterator[int]:
    """Serve `directory` on a free port of every local interface; yields the port."""
    server = ThreadingHTTPServer(("", 0), functools.partial(_QuietHandler, directory=str(directory)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
