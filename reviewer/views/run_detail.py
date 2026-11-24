"""One run: the verdict, the step-by-step evidence, every table and extraction, and the review form."""

from __future__ import annotations

import contextlib
import json
from datetime import datetime
from typing import Any

import pandas as pd
import streamlit as st

from reviewer import loader, reviews
from reviewer.views import common

_FEE_TABLES = {"extras", "fees_breakdown"}


def _pick_run() -> loader.Run | None:
    all_runs = common.runs()
    if all_runs.empty:
        st.info("No runs in this folder yet.")
        return None
    ids = list(all_runs["run_id"])
    wanted = st.query_params.get("run")
    index = ids.index(wanted) if wanted in ids else 0
    labels = {row.run_id: f"{row.target} · {row.run_at:%d %b %Y %H:%M}" for row in all_runs.itertuples()}
    run_id = st.selectbox("Run", ids, index=index, format_func=lambda r: labels.get(r, r))
    st.query_params["run"] = run_id
    try:
        return loader.load_run(common.output_dir(), run_id)
    except loader.UnsafePath:
        st.error("That run is not in the evidence folder.")
        return None


# --- review form (sidebar) -----------------------------------------------------


def _review_panel(run: loader.Run) -> None:
    reasons = list(run.comparison.get("drip_reasons") or [])
    current = run.review or {}
    with st.sidebar:
        st.subheader("Analyst review")
        if run.review:
            st.caption(f"Saved {common.esc(current.get('reviewed_at', ''))}")
        with st.form(f"review-{run.run_id}"):
            options = list(reviews.VERDICT_LABELS)
            default = options.index(current["verdict"]) if current.get("verdict") in options else None
            verdict = st.radio("Verdict", options, index=default, format_func=reviews.VERDICT_LABELS.__getitem__)
            accepted = {}
            if reasons:
                st.caption("Reasons the automation gave: tick the ones the evidence supports.")
                for i, reason in enumerate(reasons):
                    accepted[reason] = st.checkbox(
                        reason, value=current.get("reasons", {}).get(reason, True), key=f"reason-{run.run_id}-{i}"
                    )
            notes = st.text_area("Notes", value=current.get("notes", ""), height=120)
            if st.form_submit_button("Save review", type="primary", icon=":material/save:"):
                if verdict is None:
                    st.error("Choose a verdict first.")
                else:
                    reviews.save_review(run.path, verdict, accepted, notes)
                    st.toast("Review saved")
                    st.rerun()
        if run.review and st.button("Clear review", icon=":material/delete:", type="tertiary"):
            reviews.clear_review(run.path)
            st.rerun()


# --- tabs -------------------------------------------------------------------------


def _verdict_tab(run: loader.Run) -> None:
    data = run.comparison
    currency = data.get("currency")
    badges = st.columns([1, 1, 3])
    with badges[0]:
        common.drip_badge("Automation", run.automated_drip)
    with badges[1]:
        if run.review:
            common.drip_badge("Analyst", run.final_drip)
        else:
            st.badge("Analyst: not reviewed", color="gray", icon=":material/pending:")
    if run.overridden:
        with badges[2]:
            st.badge("Overridden: the analyst's verdict is used", color="orange", icon=":material/gavel:")

    if data.get("status") not in (None, "ok") or data.get("failure_reason"):
        st.warning(
            f"Run status **{common.esc(data.get('status'))}**"
            + (f", failure: {common.esc(data.get('failure_reason'))}" if data.get("failure_reason") else "")
            + (f". {common.esc(data.get('message'))}" if data.get("message") else "")
        )
    if not data.get("final_price_read", True):
        st.info("The final price screen was never read, so the automation cannot say whether there was drip pricing.")

    cols = st.columns(4)
    cols[0].metric("Advertised / expected", common.money(data.get("expected_subtotal"), currency))
    cols[1].metric("Checkout subtotal", common.money(data.get("checkout_subtotal"), currency))
    cols[2].metric("Checkout total", common.money(data.get("checkout_total"), currency))
    diff = data.get("total_vs_expected_diff")
    if diff is None and data.get("checkout_total") is not None and data.get("expected_subtotal") is not None:
        diff = round(data["checkout_total"] - data["expected_subtotal"], 2)
    cols[3].metric(
        "Total vs advertised",
        common.money(diff, currency),
        delta=None if not diff else f"{diff:+.2f}",
        delta_color="inverse",
    )

    st.markdown("#### Why the automation flagged it")
    reasons = data.get("drip_reasons") or []
    if not reasons:
        st.caption("No drip-pricing reasons were recorded.")
    for reason in reasons:
        decision = (run.review or {}).get("reasons", {}).get(reason)
        mark = {True: ":green[accepted]", False: ":red[rejected]", None: ":gray[not reviewed]"}[decision]
        st.markdown(f"- {common.esc(reason)} — {mark}")

    if data.get("checkout_summary"):
        st.markdown("#### Checkout as the model read it")
        st.text(data["checkout_summary"])

    shots = loader.key_screenshots(run)
    if shots:
        st.markdown("#### Key screenshots")
        cols = st.columns(min(len(shots), 4))
        for i, shot in enumerate(shots):
            with cols[i % len(cols)]:
                st.image(str(shot), caption=shot.name, width="stretch")
                common.enlarge_button(shot, key=f"key-{shot.name}")


def _timeline_tab(run: loader.Run) -> None:
    if not run.steps:
        st.info("This workflow records no numbered steps. The cart and checkout screenshots are on the Verdict tab.")
        return
    currency = run.comparison.get("currency")
    previous: float | None = None
    for step in run.steps:
        data = step.data
        with st.container(border=True):
            image_col, info_col = st.columns([3, 2])
            with image_col:
                if step.screenshot:
                    st.image(str(step.screenshot), width="stretch")
                    common.enlarge_button(step.screenshot, key=f"step-{step.number}")
                else:
                    st.caption("No screenshot for this step.")
            with info_col:
                title = data.get("heading") or data.get("step_heading") or step.step_type.replace("_", " ").title()
                st.markdown(f"**Step {step.number}: {common.esc(title)}**")
                st.caption(common.esc(step.step_type))
                total = data.get("total")
                if isinstance(total, (int, float)):
                    delta = None if previous is None or total == previous else round(total - previous, 2)
                    st.metric(
                        "Total at this step",
                        common.money(total, currency),
                        delta=None if delta is None else f"{delta:+.2f}",
                        delta_color="inverse",
                    )
                    previous = total
                elif data.get("total_display"):
                    st.metric("Total at this step", data["total_display"])
                lines = data.get("price_lines")
                if lines:
                    st.dataframe(
                        pd.DataFrame(lines),
                        hide_index=True,
                        width="stretch",
                        column_config={"label": "Line", "amount": "Amount"},
                    )
                for error in data.get("validation_errors") or []:
                    st.warning(common.esc(error))
                if data.get("summary"):
                    st.text(data["summary"])
                if data.get("url"):
                    with st.expander("Page URL"):
                        st.code(data["url"], language=None, wrap_lines=True)


def _highlight_extras(row: pd.Series) -> list[str]:
    preselected = str(row.get("selected_by_default", "")).lower() == "true"
    mandatory = str(row.get("mandatory", "")).lower() == "true"
    paid = str(row.get("price", "")) not in {"", "0", "0.0"}
    style = "background-color: rgba(230, 70, 70, 0.18)" if preselected and paid and not mandatory else ""
    return [style] * len(row)


def _fees_tab(run: loader.Run) -> None:
    lines = loader.fee_lines(run)
    st.markdown("#### Fees at checkout")
    if lines:
        st.dataframe(pd.DataFrame(lines), hide_index=True, width="stretch", column_config=common.FEE_COLUMNS)
    else:
        st.caption("No fees recorded.")
    if "fees_breakdown" in run.tables:
        st.markdown("#### Advertised vs charged")
        st.dataframe(run.tables["fees_breakdown"], hide_index=True, width="stretch")
    preselected = run.comparison.get("extras_preselected") or []
    if preselected:
        st.warning("Pre-selected extras: " + ", ".join(common.esc(p) for p in preselected))
    if "extras" in run.tables:
        st.markdown("#### Every extra, add-on and fee offered")
        st.caption("Rows in red were paid, optional and pre-selected.")
        st.dataframe(run.tables["extras"].style.apply(_highlight_extras, axis=1), hide_index=True, width="stretch")


def _items_tab(run: loader.Run) -> None:
    shown = False
    for name, table in run.tables.items():
        if name in _FEE_TABLES:
            continue
        st.markdown(f"#### {common.esc(name.replace('_', ' ').capitalize())}")
        st.dataframe(table, hide_index=True, width="stretch")
        shown = True
    if not shown:
        st.caption("No item tables in this run.")


def _extraction_title(item: dict[str, Any]) -> str:
    stamp = item.get("timestamp", "")
    with contextlib.suppress(TypeError, ValueError):
        stamp = datetime.fromisoformat(stamp).strftime("%H:%M:%S")
    usage = (item.get("metadata") or {}).get("usage") or {}
    tokens = f" · {usage.get('input_tokens', 0):,} tokens in" if usage else ""
    return f"{stamp} · {item.get('schema', 'extraction')}{tokens}"


def _extractions_tab(run: loader.Run) -> None:
    items = run.extractions
    if not items:
        st.caption("No extractions recorded.")
        return
    cols = st.columns([2, 1])
    query = cols[0].text_input("Search instructions and results", placeholder="e.g. service fee")
    schemas = cols[1].multiselect("Schema", sorted({str(i.get("schema", "")) for i in items}))
    shown = 0
    for item in items:
        if schemas and str(item.get("schema", "")) not in schemas:
            continue
        if query and query.lower() not in json.dumps(item, ensure_ascii=False).lower():
            continue
        shown += 1
        with st.expander(_extraction_title(item)):
            st.markdown("**Instruction**")
            st.text(item.get("instruction", ""))
            st.markdown("**What the model read**")
            st.json(item.get("data"), expanded=2)
    st.caption(f"{shown} of {len(items)} extractions")


def _session_tab(run: loader.Run) -> None:
    session = run.session
    if not session:
        st.caption("No session.json in this run.")
        return
    metrics = session.get("metrics") or {}
    cols = st.columns(4)
    ms = session.get("execution_time_ms")
    cols[0].metric("Run time", f"{ms / 1000:,.0f} s" if ms else "—")
    llm_ms = metrics.get("total_inference_time_ms")
    cols[1].metric("LLM time", f"{llm_ms / 1000:,.0f} s" if llm_ms else "—")
    tokens = session.get("tokens_used")
    cols[2].metric("Tokens", f"{tokens:,.0f}" if tokens else "—")
    cols[3].metric("Extractions", len(run.extractions))
    history = session.get("history") or []
    with st.expander(f"Stagehand action log ({len(history)} entries)"):
        st.json(history, expanded=1)
    with st.expander("Resource usage and raw metrics"):
        st.json({"resource_usage": session.get("resource_usage"), "metrics": metrics}, expanded=1)


def _files_tab(run: loader.Run) -> None:
    for path in run.files:
        cols = st.columns([4, 1, 1])
        cols[0].text(path.name)
        cols[1].caption(f"{path.stat().st_size / 1024:,.0f} KB")
        cols[2].download_button("Download", path.read_bytes(), file_name=path.name, key=f"dl-{path.name}")


# --- page ---------------------------------------------------------------------------


def render() -> None:
    run = _pick_run()
    if run is None:
        return
    data = run.comparison
    st.title(common.esc(data.get("event_name") or run.run_id.split("/")[0]))
    details = [run.run_id, data.get("session"), data.get("venue")]
    st.caption(" · ".join(common.esc(d) for d in details if d))
    links = {"Started at": data.get("shop_url"), "Checkout": data.get("checkout_url")}
    if any(links.values()):
        with st.expander("Page links"):
            for label, url in links.items():
                if url:
                    st.caption(label)
                    st.code(url, language=None, wrap_lines=True)
    if st.button("Target history", icon=":material/timeline:", type="tertiary"):
        row = common.runs().set_index("run_id").loc[run.run_id]
        common.open_target(row["target"])
    _review_panel(run)

    tabs = st.tabs(["Verdict", "Timeline", "Fees & extras", "Items", "Extractions", "Session", "Files"])
    with tabs[0]:
        _verdict_tab(run)
    with tabs[1]:
        _timeline_tab(run)
    with tabs[2]:
        _fees_tab(run)
    with tabs[3]:
        _items_tab(run)
    with tabs[4]:
        _extractions_tab(run)
    with tabs[5]:
        _session_tab(run)
    with tabs[6]:
        _files_tab(run)
