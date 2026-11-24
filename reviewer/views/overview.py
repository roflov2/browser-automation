"""Overview: headline numbers, one row per audited target, and every run."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from reviewer.views import common

_RUN_COLUMNS = {
    "run_at": st.column_config.DatetimeColumn("Run at", format="D MMM YYYY, HH:mm"),
    "workflow": "Workflow",
    "target": "Target",
    "status": "Status",
    "expected_subtotal": st.column_config.NumberColumn("Advertised", format="$%.2f"),
    "checkout_total": st.column_config.NumberColumn("Checkout total", format="$%.2f"),
    "total_vs_expected_diff": st.column_config.NumberColumn("Difference", format="$%.2f"),
    "automated": "Automation",
    "final": "Final result",
    "review": "Review",
}


def _with_labels(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out["automated"] = out["automated_drip"].map(common.drip_text)
    out["final"] = out["final_drip"].map(common.drip_text)
    out["review"] = [
        "Overridden" if overridden else ("Reviewed" if reviewed else "Not reviewed")
        for overridden, reviewed in zip(out["overridden"], out["reviewed"], strict=True)
    ]
    return out


def _filters(frame: pd.DataFrame) -> pd.DataFrame:
    with st.sidebar:
        st.subheader("Filters")
        workflows = st.multiselect("Workflow", sorted(w for w in frame["workflow"].unique() if w))
        finals = st.multiselect("Final result", ["Drip", "No drip", "Unknown"])
        review = st.multiselect("Review", ["Not reviewed", "Reviewed", "Overridden"])
    if workflows:
        frame = frame[frame["workflow"].isin(workflows)]
    if finals:
        frame = frame[frame["final"].isin(finals)]
    if review:
        frame = frame[frame["review"].isin(review)]
    return frame


def render() -> None:
    st.title("Overview")
    st.caption("Drip-pricing audits: what the automation found, the evidence behind it, and the analyst's verdict.")
    all_runs = common.runs()
    if all_runs.empty:
        st.info("No runs in this folder yet. Run an audit (`python main.py <workflow> ...`) and refresh.")
        return
    frame = _filters(_with_labels(all_runs))

    cols = st.columns(5)
    cols[0].metric("Runs", len(frame))
    cols[1].metric("Targets", frame["target"].nunique())
    cols[2].metric("Drip (final)", int((frame["final_drip"] == True).sum()))  # noqa: E712 - column holds None too
    cols[3].metric("Overridden by analyst", int(frame["overridden"].sum()))
    cols[4].metric("Not reviewed", int((~frame["reviewed"]).sum()))

    st.subheader("Targets")
    st.caption("The latest run of each store, route or event. Select a row to see its history.")
    latest = frame.sort_values("run_at", ascending=False).drop_duplicates("target")
    counts = frame.groupby("target").size().rename("runs")
    latest = latest.join(counts, on="target")
    target_cols = {**_RUN_COLUMNS, "runs": st.column_config.NumberColumn("Runs")}
    picked = st.dataframe(
        latest,
        column_order=[
            "target",
            "workflow",
            "runs",
            "run_at",
            "status",
            "total_vs_expected_diff",
            "automated",
            "final",
            "review",
        ],
        column_config=target_cols,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="targets",
    )
    if picked.selection.rows:
        common.open_target(latest.iloc[picked.selection.rows[0]]["target"])

    st.subheader("All runs")
    st.caption("Select a run to review its evidence.")
    picked = st.dataframe(
        frame,
        column_order=list(_RUN_COLUMNS),
        column_config=_RUN_COLUMNS,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="runs",
    )
    if picked.selection.rows:
        common.open_run(frame.iloc[picked.selection.rows[0]]["run_id"])

    export = frame.drop(columns=["automated", "final", "review"])
    st.download_button(
        "Download runs and reviews (CSV)",
        export.to_csv(index=False).encode("utf-8"),
        file_name="evidence_review.csv",
        mime="text/csv",
        icon=":material/download:",
    )
