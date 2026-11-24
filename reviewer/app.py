"""Evidence Reviewer: review what the drip-pricing audits captured, and record a verdict per run.

streamlit run reviewer/app.py                       # reads ./output
streamlit run reviewer/app.py -- --output /path/to/output
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# `streamlit run` puts reviewer/ on the path, not the repository root.
REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import streamlit as st  # noqa: E402

from reviewer import theme  # noqa: E402
from reviewer.views import common, history, overview, run_detail  # noqa: E402


def _output_arg() -> Path:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--output", default=os.environ.get("REVIEWER_OUTPUT", str(REPO / "output")))
    args, _ = parser.parse_known_args(sys.argv[1:])
    return Path(args.output).expanduser().resolve()


def main() -> None:
    st.set_page_config(page_title="Evidence Reviewer", page_icon=":material/fact_check:", layout="wide")
    output = _output_arg()
    st.session_state["output_dir"] = str(output)

    pages = {
        "overview": st.Page(overview.render, title="Overview", icon=":material/dashboard:", default=True),
        "run": st.Page(run_detail.render, title="Run detail", icon=":material/fact_check:", url_path="run"),
        "history": st.Page(history.render, title="Target history", icon=":material/timeline:", url_path="history"),
    }
    common.PAGES.update(pages)
    page = st.navigation(list(pages.values()))

    with st.sidebar:
        st.caption(f"Evidence folder: `{output}`")
        if not output.is_dir():
            st.error("That folder does not exist. Pass `-- --output <folder>`.")
            st.stop()
    theme.apply()
    theme.app_bar(page.title)
    page.run()


main()
