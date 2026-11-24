"""Target history: one store, route or event over time, and two of its runs side by side."""

from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from reviewer import loader, theme
from reviewer.views import common

_SERIES = {"expected_subtotal": "Advertised", "checkout_total": "Checkout total", "other_fees_total": "Fees"}


def _chart(frame: pd.DataFrame) -> None:
    long = frame.melt(
        id_vars=["run_at", "run_id", "final"], value_vars=list(_SERIES), var_name="series", value_name="amount"
    ).dropna(subset=["amount"])
    long["series"] = long["series"].map(_SERIES)
    base = alt.Chart(long).encode(
        x=alt.X("run_at:T", title="Run"),
        y=alt.Y("amount:Q", title="Amount ($)"),
        color=alt.Color(
            "series:N",
            title=None,
            sort=list(_SERIES.values()),
            # Fees are the drip signal, so they take the alert colour.
            scale=alt.Scale(domain=list(_SERIES.values()), range=[theme.ROYAL_BLUE, theme.NAVY, theme.ALERT_ORANGE]),
        ),
    )
    points = base.mark_point(size=90, filled=True).encode(
        shape=alt.Shape("final:N", title="Final result", scale=alt.Scale(domain=["Drip", "No drip", "Unknown"])),
        tooltip=["run_id", "series", alt.Tooltip("amount:Q", format="$.2f"), "final"],
    )
    st.altair_chart(base.mark_line() + points, width="stretch")


def _compare(output_runs: pd.DataFrame) -> None:
    st.subheader("Compare two runs")
    ids = list(output_runs["run_id"])
    labels = dict(zip(output_runs["run_id"], output_runs["run_at"].dt.strftime("%d %b %Y %H:%M"), strict=True))
    cols = st.columns(2)
    first = cols[0].selectbox("Earlier run", ids, index=min(1, len(ids) - 1), format_func=labels.get)
    second = cols[1].selectbox("Later run", ids, index=0, format_func=labels.get)
    output = common.output_dir()
    a, b = loader.load_run(output, first), loader.load_run(output, second)

    def figures(run: loader.Run) -> dict[str, object]:
        d = run.comparison
        return {
            "Status": d.get("status"),
            "Advertised / expected": d.get("expected_subtotal"),
            "Checkout subtotal": d.get("checkout_subtotal"),
            "Fees": d.get("other_fees_total"),
            "Checkout total": d.get("checkout_total"),
            "Extras offered": d.get("extras_offered"),
            "Pre-selected extras": len(d.get("extras_preselected") or []),
            "Automation": loader.drip_label(run.automated_drip),
            "Final result": loader.drip_label(run.final_drip),
        }

    table = pd.DataFrame({labels[first]: figures(a), labels[second]: figures(b)})
    table = table.astype(str).replace({"None": "—", "nan": "—"})
    st.dataframe(table, width="stretch")

    fee_cols = st.columns(2)
    for col, run in zip(fee_cols, (a, b), strict=True):
        with col:
            st.markdown(f"**Fees, {labels[run.run_id]}**")
            lines = loader.fee_lines(run)
            if lines:
                st.dataframe(pd.DataFrame(lines), hide_index=True, width="stretch", column_config=common.FEE_COLUMNS)
            else:
                st.caption("None")
            for reason in run.comparison.get("drip_reasons") or []:
                st.markdown(f"- {common.esc(reason)}")

    st.markdown("#### Screenshots, step by step")
    st.caption("Steps are paired by type, so the same page of the flow sits side by side.")
    a_steps = {s.step_type: s for s in a.steps}
    b_steps = {s.step_type: s for s in b.steps}
    order = list(dict.fromkeys([*a_steps, *b_steps]))
    shots_a = {p.name: p for p in loader.key_screenshots(a)}
    shots_b = {p.name: p for p in loader.key_screenshots(b)}
    pairs = [(t.replace("_", " ").capitalize(), a_steps.get(t), b_steps.get(t)) for t in order]
    pairs += [(name, shots_a.get(name), shots_b.get(name)) for name in dict.fromkeys([*shots_a, *shots_b])]
    for title, left, right in pairs:
        st.markdown(f"**{common.esc(title)}**")
        cols = st.columns(2)
        for col, item, side in ((cols[0], left, "a"), (cols[1], right, "b")):
            path = getattr(item, "screenshot", item)
            with col:
                if path:
                    st.image(str(path), width="stretch")
                    common.enlarge_button(path, key=f"cmp-{side}-{title}")
                else:
                    st.caption("Not in this run")


def render() -> None:
    st.title("Target history")
    all_runs = common.runs()
    if all_runs.empty:
        st.info("No runs in this folder yet.")
        return
    targets = list(dict.fromkeys(all_runs["target"]))
    wanted = st.query_params.get("target")
    target = st.selectbox("Target", targets, index=targets.index(wanted) if wanted in targets else 0)
    st.query_params["target"] = target

    frame = all_runs[all_runs["target"] == target].copy()
    frame["final"] = frame["final_drip"].map(common.drip_text)
    frame["automated"] = frame["automated_drip"].map(common.drip_text)
    st.caption(f"{common.esc(frame.iloc[0]['workflow'])} · {len(frame)} run(s)")

    if len(frame) > 1:
        _chart(frame)
    else:
        st.info("Only one run of this target so far. Run the audit again to build up its history.")

    picked = st.dataframe(
        frame,
        column_order=[
            "run_at",
            "status",
            "expected_subtotal",
            "checkout_total",
            "other_fees_total",
            "automated",
            "final",
            "overridden",
        ],
        column_config={
            "run_at": st.column_config.DatetimeColumn("Run at", format="D MMM YYYY, HH:mm"),
            "status": "Status",
            "expected_subtotal": st.column_config.NumberColumn("Advertised", format="$%.2f"),
            "checkout_total": st.column_config.NumberColumn("Checkout total", format="$%.2f"),
            "other_fees_total": st.column_config.NumberColumn("Fees", format="$%.2f"),
            "automated": "Automation",
            "final": "Final result",
            "overridden": st.column_config.CheckboxColumn("Overridden"),
        },
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="history-runs",
    )
    if picked.selection.rows:
        common.open_run(frame.iloc[picked.selection.rows[0]]["run_id"])

    if len(frame) > 1:
        _compare(frame)
