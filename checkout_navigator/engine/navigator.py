"""The control loop: "Are we at checkout?" -> (no) "What must we do to get there?" -> "Do" -> repeat.

Every website goes through this same loop and the same UI-capability helpers. There are no domain branches:
the only per-site input is the optional timing / never-click data in `site_hints.yaml`.

The loop ends, and never goes further, as soon as:
* `is_at_checkout` sees the payment wall                      -> status DONE
* the safety brake refuses a final-purchase action            -> status DONE
* a CAPTCHA / bot check or mandatory sign-in blocks the way   -> status BLOCKED
* the step budget runs out                                    -> status MAX_STEPS_EXCEEDED
"""

from __future__ import annotations

import logging
from typing import Any

from checkout_navigator.engine.cdp_guard import (
    BROWSER_ERRORS,
    ActionRefused,
    CheckoutReachedInterrupt,
    LoginWallBlocked,
    Observation,
    cdp_act,
    cdp_observe,
    compute_dom_hash,
    safe_evaluate,
    scan_dom,
    settle,
    wait_for_hydration,
    wait_out_queue,
)
from checkout_navigator.engine.classifier import NextAction, determine_next_step, is_at_checkout
from checkout_navigator.helpers.auth import handle_auth_or_details
from checkout_navigator.helpers.calendar import handle_calendar
from checkout_navigator.helpers.location import handle_location
from checkout_navigator.helpers.options import handle_options
from checkout_navigator.helpers.overlay import dismiss_overlay

log = logging.getLogger(__name__)


async def _url(page: Any) -> str:
    try:
        return str(await page.url())
    except BROWSER_ERRORS:
        return ""


async def _arrive(page: Any, hints: dict[str, Any]) -> None:
    """After a navigation: load, sit out any virtual queue, and give the SPA time to hydrate."""
    await settle(page, int(hints.get("settle_ms") or 1500))
    if not await wait_out_queue(page, float(hints.get("queue_wait_s") or 300)):
        raise LoginWallBlocked("still in the virtual waiting room after the queue limit")
    await wait_for_hydration(page, float(hints.get("hydration_wait_s") or 10))


async def recover_from_stagnation(stagehand: Any, page: Any, hints: dict[str, Any] | None = None) -> None:
    """Nothing changed since the last step: scroll to reveal off-screen Continue/Checkout buttons and sticky footers,
    and clear any overlay that may be swallowing clicks."""
    hints = hints or {}
    size = await safe_evaluate(page, "({ w: window.innerWidth, h: window.innerHeight })", default={})
    width = float((size or {}).get("w") or 1280)
    height = float((size or {}).get("h") or 900)
    try:
        await page.scroll(width / 2, height / 2, 0, height * 0.8)
    except BROWSER_ERRORS as error:
        log.debug("Wheel scroll failed (%s); scrolling with script", error)
        await safe_evaluate(page, f"window.scrollBy(0, {int(height * 0.8)})")
    await settle(page, int(hints.get("settle_ms") or 1500))
    overlay = bool((await scan_dom(page)).get("overlay"))
    if overlay:
        await dismiss_overlay(stagehand, page, hints)
    log.info("  Stagnation: scrolled down%s", " and tried to dismiss an overlay" if overlay else "")


async def do_step(stagehand: Any, page: Any, decision: NextAction, context: dict[str, Any]) -> bool:
    """Run the helper the decision routes to, or the single safety-checked act(). True if it did something."""
    hints: dict[str, Any] = context.get("hints") or {}
    if decision.route == "DO_DISMISS_OVERLAY":
        return await dismiss_overlay(stagehand, page, hints)
    if decision.route == "DO_LOCATION":
        return await handle_location(stagehand, page, context)
    if decision.route == "DO_CALENDAR":
        return await handle_calendar(stagehand, page, context["dates"], context)
    if decision.route == "DO_OPTIONS":
        return await handle_options(stagehand, page, context)
    if decision.route == "DO_AUTH_OR_DETAILS":
        return await handle_auth_or_details(stagehand, page, context["persona"], context)
    outcome = await cdp_act(
        stagehand, page, decision.instruction or "Click the primary button that continues to the next step",
        action=decision.target, hints=hints, timeout_ms=int(context.get("llm_timeout_ms") or 90_000),
    )  # fmt: skip
    return outcome.success


def _done(status: str, step: int, url: str, **extra: Any) -> dict[str, Any]:
    return {"status": status, "steps": step, "url": url, **extra}


async def reach_checkout(stagehand: Any, page: Any, start_url: str, context: dict[str, Any]) -> dict[str, Any]:
    """Navigate from `start_url` to the payment wall and stop there. Returns the terminal status dictionary."""
    hints: dict[str, Any] = context.get("hints") or {}
    timeout = int(context.get("llm_timeout_ms") or 90_000)
    history: list[str] = []

    await page.goto(start_url)
    try:
        await _arrive(page, hints)
    except LoginWallBlocked as blocked:
        return _done("BLOCKED", 0, await _url(page), reason=str(blocked))
    prev_state: tuple[str, str] | None = None

    for step in range(int(context["max_steps"])):
        elements = await cdp_observe(stagehand, page, timeout=timeout)

        # 1. Are we at checkout? If yes: done.
        if await is_at_checkout(page, elements):
            return _done("DONE", step, elements.url or await _url(page), history=history)

        # Stagnation guard: only when BOTH the URL and the interactive-DOM hash are unchanged since the last step.
        current_state = (elements.url, compute_dom_hash(elements))
        if current_state == prev_state:
            history.append(f"step {step}: no visible change after the last action; scrolled to recover")
            await recover_from_stagnation(stagehand, page, hints)
            prev_state = None
            continue
        prev_state = current_state

        # 2. If no, what must we do to get there?
        decision = await determine_next_step(stagehand, elements, history, timeout)
        log.info("Step %d: %s %s | %s", step + 1, decision.route, decision.instruction or "", decision.reason)
        if decision.blocked_reason:
            return _done("BLOCKED", step, elements.url, reason=decision.blocked_reason, history=history)

        # 3. Do (behind the hard payment safety brake).
        url_before = elements.url
        try:
            done = await do_step(stagehand, page, decision, context)
            if await _url(page) != url_before:
                await _arrive(page, hints)  # a navigation: queue, hydration
        except CheckoutReachedInterrupt as brake:
            log.info("Safety brake: %s", brake)
            return _done("DONE", step, await _url(page), stopped_by="safety_brake", refused=brake.label[:200],
                         history=history)  # fmt: skip
        except LoginWallBlocked as blocked:
            return _done("BLOCKED", step, await _url(page), reason=str(blocked), history=history)
        except ActionRefused as refused:
            log.info("  Refused: %s", refused)
            history.append(f"step {step}: {decision.route} refused ({refused}); choose a different element")
            continue
        except BROWSER_ERRORS as error:
            log.warning("  Step failed: %s", str(error)[:200])
            history.append(f"step {step}: {decision.route} {decision.instruction or ''} failed: {str(error)[:120]}")
            continue

        history.append(f"step {step}: {decision.route} {decision.instruction or ''} -> {'ok' if done else 'no effect'}")

    # The last action may itself have landed on the payment wall: one model-free look before giving up.
    final = Observation(dom=await scan_dom(page), url=await _url(page))
    if await is_at_checkout(page, final):
        return _done("DONE", int(context["max_steps"]), final.url, history=history)
    return _done("MAX_STEPS_EXCEEDED", int(context["max_steps"]), final.url, history=history)
