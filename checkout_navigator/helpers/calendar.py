"""DO_CALENDAR: date, departure/return and check-in/check-out pickers.

Targets come from the config (`today + 14` to depart / check in, `today + 17` to return / check out). Native
`<input type="date">` fields are filled directly. A custom picker is opened, its visible month header read, and it is
paged forward month by month until the target day is enabled; the day is clicked, then the return / check-out day
when the form asks for one, then the picker is confirmed or closed. The day-cell finder (ISO attribute, aria-label
naming the date, or a day number under its month heading) handles both paged and vertically scrolling month layouts;
every element is located in the DOM by role and text, never by position. The model is
only used to open the picker, or as a last resort when the DOM cannot find the day or the next-month control.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

from checkout_navigator.engine.cdp_guard import (
    guarded_click_point,
    safe_evaluate,
    settle,
    try_cdp_act,
)

log = logging.getLogger(__name__)

MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)  # fmt: skip  - not strftime('%B'): that follows the machine's locale
MAX_MONTH_STEPS = 14
STEP_SETTLE_MS = 600

# Shared by every snippet: the open picker's root (a dialog holding a day grid, or a known calendar container).
PICKER_LIB = r"""
const clean = s => (s || '').replace(/\s+/g, ' ').trim();
const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return r.width > 3 && r.height > 3 && s.visibility !== 'hidden' && s.display !== 'none'; };
const MONTH_RE = /^(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}$/i;
const picker = () => {
    const sel = '[role="dialog"], [aria-modal="true"], [role="grid"], [class*="calendar" i], [class*="datepicker" i], '
        + '[class*="date-picker" i], [class*="DayPicker" i], [class*="flatpickr-calendar" i], [class*="daterange" i]';
    const found = Array.from(document.querySelectorAll(sel)).filter(el => vis(el)
        && el.querySelectorAll('button, td, [role="gridcell"]').length >= 20);
    if (!found.length) return null;
    // The outermost match, so the month header and the navigation arrows are inside it.
    return found.find(el => !found.some(o => o !== el && o.contains(el))) || found[0];
};
const box = el => { el.scrollIntoView({ block: 'center' }); const r = el.getBoundingClientRect();
    return { text: clean(el.innerText || el.getAttribute('aria-label') || '').slice(0, 80),
             x: r.left + r.width / 2, y: r.top + r.height / 2 }; };
"""
JS_PICKER_STATE = (
    PICKER_LIB
    + r"""
return (() => {
    const root = picker();
    if (!root) return { open: false, months: [] };
    const months = Array.from(root.querySelectorAll('*')).filter(h => h.children.length <= 2 && vis(h))
        .map(h => clean(h.innerText)).filter(t => MONTH_RE.test(t));
    return { open: true, months: Array.from(new Set(months)) };
})();
"""
)
JS_DAY_CELL = (
    PICKER_LIB
    + r"""
return (({ iso, day, month, label }) => {
    const root = picker();
    if (!root) return null;
    const labelled = new RegExp(label, 'i');
    const usable = b => vis(b) && !b.disabled && b.getAttribute('aria-disabled') !== 'true'
        && !/disabled|unavailable|blocked/i.test(b.className || '');
    const cells = Array.from(root.querySelectorAll('button, [role="gridcell"], [role="button"], td, [data-date], [data-day]'));
    let el = cells.find(b => Array.from(b.attributes).some(a => a.value.startsWith(iso))
        || labelled.test(b.getAttribute('aria-label') || '') || labelled.test(b.getAttribute('title') || ''));
    if (!el) {
        const heads = Array.from(root.querySelectorAll('*')).filter(h => h.children.length <= 2 && MONTH_RE.test(clean(h.innerText)));
        const head = heads.find(h => clean(h.innerText).toLowerCase() === month.toLowerCase());
        if (!head) return null;
        const after = (a, b) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
        const next = heads.find(h => clean(h.innerText) !== clean(head.innerText) && after(head, h));
        el = cells.filter(b => after(head, b) && (!next || after(b, next)))
            .find(b => (b.innerText || '').split('\n').map(clean).find(l => /^\d{1,2}$/.test(l)) === String(day));
    }
    if (!el) return null;
    const target = el.matches('button, [role="button"]') ? el : (el.querySelector('button, [role="button"]') || el);
    if (!usable(target) && !usable(el)) return { disabled: true };
    return box(target);
})(ARG);
"""
)
JS_NAV = (
    PICKER_LIB
    + r"""
return ((direction) => {
    const root = picker();
    if (!root) return null;
    const want = direction === 'next' ? /next|forward|later|›|»|→|>/i : /prev|back|earlier|‹|«|←|</i;
    const el = Array.from(root.querySelectorAll('button, [role="button"], a'))
        .filter(b => vis(b) && !b.disabled && b.getAttribute('aria-disabled') !== 'true')
        .find(b => want.test([b.getAttribute('aria-label'), b.title, clean(b.innerText)].filter(Boolean).join(' '))
                   && clean(b.innerText).length <= 12);
    return el ? box(el) : null;
})(ARG);
"""
)
JS_SCROLL_MONTHS = (
    PICKER_LIB
    + r"""
return (() => {
    const root = picker();
    if (!root) return false;
    const scroller = [root, ...root.querySelectorAll('*')].find(e => e.scrollHeight > e.clientHeight + 20
        && /auto|scroll/.test(getComputedStyle(e).overflowY));
    if (!scroller) return false;
    const before = scroller.scrollTop;
    scroller.scrollTop = before + scroller.clientHeight * 0.8;
    return scroller.scrollTop > before;
})();
"""
)
JS_CONFIRM = (
    PICKER_LIB
    + r"""
return (() => {
    const root = picker();
    if (!root) return null;
    const scope = root.closest('[role="dialog"], [aria-modal="true"]') || root.parentElement || root;
    const el = Array.from(scope.querySelectorAll('button, [role="button"]')).filter(vis)
        .find(b => /^(done|apply|ok|confirm( dates)?|continue|select( dates)?|save|close)$/i.test(clean(b.innerText)
                    || b.getAttribute('aria-label') || ''));
    return el ? box(el) : null;
})();
"""
)
JS_NATIVE_DATES = r"""(dates => {
    const inputs = Array.from(document.querySelectorAll('input[type="date"]')).filter(el => {
        const r = el.getBoundingClientRect(); return r.width > 3 && r.height > 3 && !el.disabled;
    });
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
    const filled = [];
    inputs.slice(0, 2).forEach((el, i) => {
        if (el.value) return;
        setter.call(el, dates[i] || dates[0]);
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        filled.push(el.name || el.id || `date ${i + 1}`);
    });
    return filled;
})"""
JS_WANTS_RETURN = r"""(() => Array.from(document.querySelectorAll('input, button, [role="button"], [role="combobox"], label'))
    .some(el => { const r = el.getBoundingClientRect(); if (r.width < 3 || r.height < 3) return false;
        const t = [el.getAttribute('aria-label'), el.getAttribute('placeholder'), el.innerText].filter(Boolean).join(' ');
        return /\breturn( date)?\b|check.?out|end date|\bto date\b|departure.*return/i.test(t)
            && !/one.?way/i.test(t); }))()"""


def _wrap(lib_snippet: str, arg: Any = None) -> str:
    """Turn a PICKER_LIB snippet (ending in `return ...(ARG)`) into an evaluate expression."""
    return "(() => {" + lib_snippet.replace("ARG", json.dumps(arg)) + "})()"


def date_label_pattern(day: date) -> str:
    """A regex (valid in Python and JS) matching `day` written out: '14 Feb 2027', 'February 14, 2027',
    'Sunday, 14 February 2027', '14/02/2027' or '2027-02-14'."""
    month = MONTHS[day.month - 1]
    name = f"{month[:3]}(?:{month[3:]})?\\.?"
    return (
        rf"(?:^|[^0-9]){day.day}(?:st|nd|rd|th)?\s+{name},?\s+{day.year}(?:[^0-9]|$)"
        rf"|(?:^|[^A-Za-z]){name}\s+{day.day}(?:st|nd|rd|th)?,?\s+{day.year}(?:[^0-9]|$)"
        rf"|(?:^|[^0-9])0?{day.day}/0?{day.month}/{day.year}(?:[^0-9]|$)|{day.isoformat()}"
    )


def _month_index(text: str) -> int | None:
    """'February 2027' -> a sortable month number."""
    parts = text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        return None
    names = [m.lower() for m in MONTHS]
    if parts[0].lower() not in names:
        return None
    return int(parts[1]) * 12 + names.index(parts[0].lower())


async def _click(page: Any, target: dict[str, Any], hints: dict[str, Any], settle_ms: int) -> None:
    """Mouse-click a DOM-located picker control behind the payment brake, then let the picker re-render."""
    await guarded_click_point(page, target, hints)
    await settle(page, settle_ms)


async def picker_state(page: Any) -> dict[str, Any]:
    state = await safe_evaluate(page, _wrap(JS_PICKER_STATE), default={})
    return state if isinstance(state, dict) else {"open": False, "months": []}


async def pick_day(stagehand: Any, page: Any, day: date, context: dict[str, Any]) -> bool:
    """Page the open picker forward to `day`'s month and click the day. True if it was clicked."""
    hints = context.get("hints") or {}
    arg = {"iso": day.isoformat(), "day": day.day, "month": f"{MONTHS[day.month - 1]} {day.year}",
           "label": date_label_pattern(day)}  # fmt: skip
    target_month = day.year * 12 + day.month - 1
    for _ in range(MAX_MONTH_STEPS + 1):
        cell = await safe_evaluate(page, _wrap(JS_DAY_CELL, arg), default=None)
        if isinstance(cell, dict) and "x" in cell:
            await _click(page, cell, hints, STEP_SETTLE_MS)
            log.info("  Calendar: clicked %s (%s)", day.isoformat(), cell.get("text"))
            return True
        state = await picker_state(page)
        shown = [i for m in state.get("months") or [] if (i := _month_index(m)) is not None]
        direction = "prev" if shown and min(shown) > target_month else "next"
        nav = await safe_evaluate(page, _wrap(JS_NAV, direction), default=None)
        if isinstance(nav, dict) and "x" in nav:
            await _click(page, nav, hints, STEP_SETTLE_MS)
            continue
        if await safe_evaluate(page, _wrap(JS_SCROLL_MONTHS), default=False):  # stacked, scrolling months
            await settle(page, STEP_SETTLE_MS)
            continue
        moved = await try_cdp_act(
            stagehand, page, f"Click the open calendar's {'next' if direction == 'next' else 'previous'}-month arrow",
            hints=hints, timeout_ms=context.get("llm_timeout_ms", 90_000),
        )  # fmt: skip
        if not (moved and moved.success):
            break
    outcome = await try_cdp_act(
        stagehand, page,
        f"In the open calendar, click the day {day.day} {MONTHS[day.month - 1]} {day.year}",
        hints=hints, timeout_ms=context.get("llm_timeout_ms", 90_000),
    )  # fmt: skip
    return bool(outcome and outcome.success)


async def handle_calendar(
    stagehand: Any, page: Any, dates: dict[str, date], context: dict[str, Any] | None = None
) -> bool:
    """Fill the depart (and, if asked, return) dates. True if any date was set."""
    context = context or {}
    hints = context.get("hints") or {}
    depart, back = dates["depart"], dates["return"]

    native = await safe_evaluate(page, f"({JS_NATIVE_DATES})({json.dumps([depart.isoformat(), back.isoformat()])})", [])
    if native:
        log.info("  Calendar: filled native date input(s) %s", native)
        await settle(page, int(hints.get("settle_ms") or 1500))
        return True

    if not (await picker_state(page)).get("open"):
        await try_cdp_act(
            stagehand, page,
            "Click the date field (departure date, travel dates, check-in date or event date) to open its calendar",
            hints=hints, timeout_ms=context.get("llm_timeout_ms", 90_000),
        )  # fmt: skip
        if not (await picker_state(page)).get("open"):
            log.info("  Calendar: no date picker opened")
            return False

    picked = await pick_day(stagehand, page, depart, context)
    if picked and await safe_evaluate(page, JS_WANTS_RETURN, default=False):
        if (await picker_state(page)).get("open"):
            await pick_day(stagehand, page, back, context)
        else:
            await try_cdp_act(stagehand, page, "Click the return date / check-out date field to open its calendar",
                              hints=hints, timeout_ms=context.get("llm_timeout_ms", 90_000))  # fmt: skip
            if (await picker_state(page)).get("open"):
                await pick_day(stagehand, page, back, context)

    if (await picker_state(page)).get("open"):
        confirm = await safe_evaluate(page, _wrap(JS_CONFIRM), default=None)
        if isinstance(confirm, dict) and "x" in confirm:
            await _click(page, confirm, hints, int(hints.get("settle_ms") or 1500))
            log.info("  Calendar: confirmed with %r", confirm.get("text"))
    return picked
