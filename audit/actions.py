"""LLM actions and in-page click/type helpers.

There is no code-level purchase guard. What keeps a run from paying is the workflows' own flow (each one stops
once it has read the checkout/payment page) and the instructions given to the model: every workflow's
`system_prompt` and the wording of each `act` instruction tell it never to pay, place an order, enter card
details or start a trial or membership.
"""

from __future__ import annotations

import logging
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


async def llm_act(stagehand: _Actions, instruction: str, *, follow_up: bool = False, **options: Any) -> Acted:
    """Run one LLM action: observe, then act on the first element found.

    `options` are passed to both observe() and act() (e.g. `variables`, `timeout`, `locator`).

    With `follow_up`, the instruction is observed once more after a click and, if the model now points at a
    different element (a custom dropdown that had to be opened first), that action is run too.
    """
    options.setdefault("timeout", LLM_TIMEOUT_MS)
    found = await stagehand.observe(instruction, **options)
    if not found.data:
        log.debug("Nothing to act on for %r", instruction)
        return Acted(False, "no matching element found")
    first = found.data[0]
    done = await act_on(stagehand, first, **options)
    if follow_up and done.success and first.method == "click":
        again = await stagehand.observe(instruction, **options)
        if again.data and again.data[0].selector != first.selector:
            done = await act_on(stagehand, again.data[0], **options)
    return done


async def try_act(stagehand: _Actions, instruction: str, **options: Any) -> Acted | None:
    """llm_act() for an optional step: an ordinary failure is logged at debug level and returns None."""
    try:
        return await llm_act(stagehand, instruction, **options)
    except BROWSER_ERRORS as error:  # the caller treats this step as best-effort
        log.debug("Optional action failed (%r): %s", instruction, error)
        return None
