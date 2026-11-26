"""LLM actions and in-page click/type helpers.

There is no code-level purchase guard. What keeps a run from paying is the workflows' own flow (each one stops
once it has read the checkout/payment page) and the instructions given to the model: every workflow's
`system_prompt` and the wording of each `act` instruction tell it never to pay, place an order, enter card
details or start a trial or membership.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from .core import BROWSER_ERRORS

log = logging.getLogger(__name__)

LLM_TIMEOUT_MS = 90_000  # Stagehand timeouts are in milliseconds

# Prepended to every snippet run through page.js(): small helpers for clicking, selecting and typing in the page.
PAGE_ACTIONS_JS = r"""
const clickEl = el => { el.click(); return true; };
const selectOption = (el, value) => {
    el.value = value;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
};
const fillInput = (el, text) => {
    el.focus();
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value');
    if (setter && setter.set && el instanceof HTMLInputElement) setter.set.call(el, text); else el.value = text;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    return true;
};
"""


@dataclass(frozen=True)
class Acted:
    """What llm_act did. `success` is False when the model found nothing to act on."""

    success: bool
    description: str
    selector: str = field(default="", compare=False)  # what was acted on, for the run's step log


# The Stagehand client, the runner's recording wrapper around it, or a test double: anything with observe() and act().
_Actions = Any


class _Clicks(Protocol):
    async def click(self, x: float, y: float) -> None: ...


async def act_on(stagehand: _Actions, action: Any, **options: Any) -> Acted:
    """Run one action returned by observe()."""
    options.setdefault("timeout", LLM_TIMEOUT_MS)
    result = await stagehand.act(action, **options)
    return Acted(
        bool(result.data.success), result.data.action_description or action.description, str(action.selector or "")
    )


async def click_at(page: _Clicks, x: float, y: float) -> None:
    """A real mouse click at viewport point (x, y), for controls that ignore a DOM click."""
    await page.click(x, y)


# The tag name at the start of a selector step: "li[3]" -> "li".
_STEP = re.compile(r"^([a-zA-Z][\w-]*)")
# Wording the model uses for an entry of an open list, as opposed to the control that opens it.
_OPTION_WORDS = re.compile(r"\b(option|list ?item|menu ?item|menuitem)\b", re.IGNORECASE)
_OPENER_WORDS = re.compile(
    r"\b(combobox|combo box|dropdown|drop-down|select box|button|input|field|textbox)\b", re.IGNORECASE
)
# A click described like this commits or submits something (a form, a day in a calendar) rather than opening a list.
_NOT_OPENER_WORDS = re.compile(
    r"\b(submit|search|continue|next|confirm|done|apply|save|proceed|close)\b"
    r"|\b\d{1,2}(st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b"
    r"|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}(st|nd|rd|th)?\b",
    re.IGNORECASE,
)
# aria-haspopup values that mean "opens a list to choose from" (a "dialog" popup has no options to pick).
_LIST_POPUPS = {"true", "listbox", "menu", "tree", "grid"}
_LIST_ROLES = {"combobox", "listbox"}
# The ARIA hints of the element an xpath selector names, or null if it cannot be found in the main frame.
JS_POPUP_HINTS = r"""xpath => {
    let el = null;
    try {
        el = document.evaluate(xpath, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
    } catch (e) { return null; }
    if (!el || el.nodeType !== 1) return null;
    const hinted = el.closest('[aria-haspopup], [role="combobox"], [aria-expanded]') || el;
    return { haspopup: hinted.getAttribute('aria-haspopup'), role: hinted.getAttribute('role'),
             expanded: hinted.getAttribute('aria-expanded') };
}"""
CONTAINER_LEVELS = 3  # an option counts as "in the opened list" if it shares the opener's ancestor this many steps up


def xpath_steps(selector: str) -> list[str]:
    """The steps of an `xpath=` selector (empty for anything else), e.g. ['html[1]', 'body[1]', 'div[2]']."""
    if not selector.startswith(("xpath=", "/")):
        return []
    return [step for step in selector.removeprefix("xpath=").split("/") if step]


def is_native_select(selector: str) -> bool:
    """True when the selector ends on a `<select>`: the only element Stagehand's selectOptionFromDropdown works on."""
    steps = xpath_steps(selector)
    last = steps[-1] if steps else selector.rsplit(" ", 1)[-1].rsplit(">", 1)[-1].strip()
    match = _STEP.match(last)
    return bool(match and match.group(1).lower() == "select")


def _as_click(action: Any) -> Any:
    """A copy of an observed action that clicks its element instead."""
    if hasattr(action, "model_copy"):
        return action.model_copy(update={"method": "click", "arguments": []})
    clicked = copy.copy(action)
    clicked.method, clicked.arguments = "click", []
    return clicked


def is_option_of(candidate: Any, opener: Any) -> bool:
    """Whether `candidate` (a follow-up observe result) is an entry of the list that clicking `opener` opened.

    Never the opener itself. Accepted: an `li`/`option` element; one the model describes as an option or list
    item (and clicks, rather than calling selectOptionFromDropdown on it as it does on a dropdown control); or,
    failing those, one inside the opener's container (it shares the opener's ancestor CONTAINER_LEVELS steps
    up, as `<combobox parent>/ul[1]/li[1]` does) that is not described as a control. Any other control on the
    page (a combobox, button, field) is refused: acting on it would undo progress made elsewhere in the form.
    """
    selector, opened = str(candidate.selector or ""), str(opener.selector or "")
    if not selector or selector == opened:
        return False
    description = str(candidate.description or "")
    steps, opener_steps = xpath_steps(selector), xpath_steps(opened)
    last = _STEP.match(steps[-1]) if steps else None
    says_option = (
        bool(_OPTION_WORDS.search(description)) and getattr(candidate, "method", None) != "selectOptionFromDropdown"
    )
    if (last and last.group(1).lower() in ("li", "option")) or says_option:
        return True
    if _OPENER_WORDS.search(description):
        return False
    container = opener_steps[: max(len(opener_steps) - CONTAINER_LEVELS, 1)]
    return bool(opener_steps) and steps[: len(container)] == container and len(steps) > len(container)


def _follow_up_instruction(opener: Any, wanted: str | None, instruction: str) -> str:
    target = f"is '{wanted}'" if wanted else f"moves towards this goal: {instruction}"
    return (
        f"A list or menu has just been opened by '{opener.description}'. Return the option in that open list that "
        f"{target}. Return the option itself (a list item), never the control that opened it, and never any other "
        "control on the page. Return nothing if no list is open."
    )


async def _pick_from_opened_list(
    stagehand: _Actions, opener: Any, wanted: str | None, instruction: str, **options: Any
) -> Acted | None:
    """After `opener` opened a list, choose the wanted option from it; None if no acceptable option was found."""
    again = await stagehand.observe(_follow_up_instruction(opener, wanted, instruction), **options)
    for candidate in again.data or []:
        if not is_option_of(candidate, opener):
            log.debug(
                "Follow-up skipped %r (%s): not an option of the opened list", candidate.description, candidate.selector
            )
            continue
        selects = getattr(candidate, "method", None) == "selectOptionFromDropdown"
        option = _as_click(candidate) if selects and not is_native_select(str(candidate.selector or "")) else candidate
        return await act_on(stagehand, option, **options)
    return None


class _Evaluates(Protocol):
    async def evaluate(self, expression: str) -> Any: ...


async def popup_hints(page: _Evaluates | None, selector: str) -> dict[str, Any] | None:
    """The ARIA popup hints (haspopup, role, expanded) of the element `selector` names; None if unreadable."""
    steps = xpath_steps(selector)
    if page is None or not steps:
        return None
    xpath = "/" + "/".join(steps)
    try:
        found = await page.evaluate(f"({JS_POPUP_HINTS})({json.dumps(xpath)})")
    except Exception as error:  # noqa: BLE001 - a failed read falls back to the description
        log.debug("Could not read the popup hints of %s: %s", selector, error)
        return None
    return found if isinstance(found, dict) else None


def opened_a_list(description: str, before: dict[str, Any] | None, after: dict[str, Any] | None) -> bool:
    """Whether a click (on an element with ARIA hints `before`, `after` it) may have opened a list to choose from.

    The page's own markup decides when it has any say: a combobox or list popup, or aria-expanded turning true,
    is an opener; an element with no such hint, or only a dialog popup, is not. Without markup (the element was
    not found, or it carries no ARIA hints) the description decides: a submit, search, continue or a specific
    date ("14 Feb 2027") is no opener, anything else may be.
    """
    hints = [h for h in (before, after) if h]
    if any(str(h.get("haspopup") or "").lower() in _LIST_POPUPS or h.get("role") in _LIST_ROLES for h in hints):
        return True
    if after and after.get("expanded") == "true":
        return True
    if any(h.get("haspopup") or h.get("expanded") is not None for h in hints):
        return False  # it says what it opens, and that is no list (a dialog), or it stayed closed
    return not _NOT_OPENER_WORDS.search(description)


async def llm_act(
    stagehand: _Actions, instruction: str, *, follow_up: bool = False, page: _Evaluates | None = None, **options: Any
) -> Acted:
    """Run one LLM action: observe, then act on the first element found.

    `options` are passed to both observe() and act() (e.g. `variables`, `timeout`, `locator`).

    With `follow_up`, an action that opens a custom dropdown is followed by choosing from it. That covers a
    `click` on the control, and a `selectOptionFromDropdown` on anything but a native `<select>`: Stagehand runs
    that as `locator.selectOption()`, which changes nothing on a `div` combobox yet reports success, so it is
    turned into a click that opens the list. The follow-up observe asks only for an option of the opened list
    (see is_option_of) and takes the first acceptable result, not just the first result.

    A plain click is followed up only if it looks like it opened a list (see opened_a_list): read from the
    element's ARIA attributes in `page` when one is given, else from its description. A click on a submit
    button or a calendar day costs no second observe.
    """
    options.setdefault("timeout", LLM_TIMEOUT_MS)
    found = await stagehand.observe(instruction, **options)
    if not found.data:
        log.debug("Nothing to act on for %r", instruction)
        return Acted(False, "no matching element found")
    first = found.data[0]
    if not follow_up:
        return await act_on(stagehand, first, **options)
    arguments = list(getattr(first, "arguments", None) or [])
    wanted = str(arguments[0]) if arguments and first.method == "selectOptionFromDropdown" else None
    if wanted is not None and not is_native_select(str(first.selector or "")):
        log.debug("selectOptionFromDropdown(%r) on a non-<select> %s: clicking it open instead", wanted, first.selector)
        first = _as_click(first)
    before = await popup_hints(page, str(first.selector or "")) if wanted is None else None
    done = await act_on(stagehand, first, **options)
    if done.success and first.method == "click" and wanted is None:
        after = await popup_hints(page, str(first.selector or ""))
        if not opened_a_list(str(first.description or ""), before, after):
            log.debug("No follow-up after %r: it did not open a list", first.description)
            return done
    if done.success and first.method == "click":
        picked = await _pick_from_opened_list(stagehand, first, wanted, instruction, **options)
        if picked is not None:
            done = picked
    return done


async def try_act(stagehand: _Actions, instruction: str, **options: Any) -> Acted | None:
    """llm_act() for an optional step: an ordinary failure is logged at debug level and returns None."""
    try:
        return await llm_act(stagehand, instruction, **options)
    except BROWSER_ERRORS as error:  # the caller treats this step as best-effort
        log.debug("Optional action failed (%r): %s", instruction, error)
        return None
