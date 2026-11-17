"""Helpers for the tests that drive a real local browser against HTML fixtures (no site, no LLM)."""

import functools
import json
import os
import re
import shutil
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

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


class StubModel:
    """A stand-in for Gemini, plugged into Stagehand as its LLM callback. Counts calls and prompt size.

    observe()/act() prompts are answered by picking the accessibility-tree node whose name is quoted
    in the instruction; extract() prompts with the canned answer for the schema's title.
    """

    def __init__(self, extractions: dict[str, dict[str, Any]] | None = None) -> None:
        self.extractions = extractions or {}
        self.calls = 0
        self.prompt_chars = 0
        self.prompts: list[str] = []
        self.unknown: list[str] = []  # schemas the stub had no canned answer for

    async def __call__(self, params: Any) -> dict[str, Any]:
        request = params if isinstance(params, dict) else params.model_dump()
        text = json.dumps(request["messages"], default=str)
        self.calls += 1
        self.prompt_chars += len(text) + len(request.get("system_prompt") or "")
        self.prompts.append(text)
        schema = request["response_format"].get("schema_") or request["response_format"].get("schema") or {}
        properties = schema.get("properties", {})
        if "elements" in properties:
            content: dict[str, Any] = {"elements": [self._element(text)]}
        elif "action" in properties:
            content = {"action": self._element(text), "twoStep": False}
        elif schema.get("title") in self.extractions:
            content = self.extractions[schema["title"]]
        else:
            self.unknown.append(json.dumps(schema)[:600])
            content = {name: True if spec.get("type") == "boolean" else "done" for name, spec in properties.items()}
        usage = {"input_tokens": len(text) // 4, "output_tokens": 40, "total_tokens": len(text) // 4 + 40}
        return {"role": "assistant", "content": {"type": "text", "text": json.dumps(content)},
                "output_format": "json_schema", "structured_content": content, "usage": usage}  # fmt: skip

    @staticmethod
    def _element(text: str) -> dict[str, Any]:
        quoted = re.search(r"[Ii]nstruction: [^\\]*?'([^']+)'[^\\]*?(?:into the (\w+) field)?\.", text)
        typing = bool(quoted and quoted.group(2))
        wanted = (quoted.group(2) if typing else quoted.group(1)) if quoted else ""
        node = re.search(rf"\[(\d+-\d+)\] \w+: {re.escape(wanted)}", text)
        return {"elementId": node.group(1) if node else "0-0", "description": f"{wanted} control",
                "method": "fill" if typing else "click", "arguments": [quoted.group(1)] if typing and quoted else []}  # fmt: skip
