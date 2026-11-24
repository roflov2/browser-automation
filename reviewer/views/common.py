"""Shared bits of the Streamlit pages: the output folder, page links, badges and safe text."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from reviewer import loader

# Filled in by app.py so pages can link to each other without importing it.
PAGES: dict[str, Any] = {}

# Only the characters that change how text renders; escaping more shows stray backslashes in URLs.
_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>#|~$])")

# Drip is an alert (Safety Alert Orange), no drip a positive confirmation (Emerald Green).
DRIP_COLOURS = {True: "orange", False: "green", None: "gray"}


def output_dir() -> Path:
    return Path(st.session_state["output_dir"])


def runs() -> pd.DataFrame:
    """The run list, read fresh on every rerun: it is small, and a saved review must show at once."""
    return loader.list_runs(output_dir())


def esc(text: Any) -> str:
    """Escape page or model text before it goes into markdown, so it is shown and never rendered."""
    return _MD_SPECIAL.sub(r"\\\1", "" if text is None else str(text))


def money(value: Any, currency: str | None = None) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    prefix = f"{currency} " if currency and currency not in {"AUD", "USD"} else "$"
    return f"{'-' if number < 0 else ''}{prefix}{abs(number):,.2f}"


def drip_badge(label: str, value: bool | None) -> None:
    icon = {True: ":material/warning:", False: ":material/check_circle:", None: ":material/help:"}[value]
    st.badge(f"{label}: {loader.drip_label(value)}", icon=icon, color=DRIP_COLOURS[value])


def open_run(run_id: str) -> None:
    st.switch_page(PAGES["run"], query_params={"run": run_id})


def open_target(target: str) -> None:
    st.switch_page(PAGES["history"], query_params={"target": target})


def drip_text(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "Unknown"
    return loader.drip_label(bool(value))


def enlarge_button(path: Path, key: str) -> None:
    if st.button("Enlarge", key=key, icon=":material/open_in_full:", type="tertiary"):
        _show_image(str(path))


@st.dialog("Screenshot", width="large")
def _show_image(path: str) -> None:
    st.caption(Path(path).name)
    st.image(path, width="stretch")


FEE_COLUMNS = {
    "label": "Fee",
    "amount": st.column_config.NumberColumn("Amount", format="$%.2f"),
    "mandatory": st.column_config.CheckboxColumn("Mandatory"),
}
