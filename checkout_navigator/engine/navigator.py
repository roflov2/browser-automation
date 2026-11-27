"""The control loop: "Are we at checkout?" -> (no) "What must we do to get there?" -> "Do" -> repeat.

Every website goes through this same loop and the same UI-capability helpers. There are no domain branches:
the only per-site input is the optional timing / never-click data in `site_hints.yaml`.

The loop ends, and never goes further, as soon as:
* `is_at_checkout` sees the payment wall                      -> status DONE
* the safety brake refuses a final-purchase action            -> status DONE
* a CAPTCHA / bot check or mandatory sign-in blocks the way   -> status BLOCKED
* several steps in a row bring no new URL or page state       -> status STUCK
* the step budget runs out                                    -> status MAX_STEPS_EXCEEDED
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
from checkout_navigator.engine.progress import Progress, action_key
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


async def recover_from_stagnation(
    stagehand: Any, page: Any, hints: dict[str, Any] | None = None, *, skip_overlay: bool = False
) -> None:
    """Nothing changed since the last step: scroll to reveal off-screen Continue/Checkout buttons and sticky footers,
    and clear any overlay that may be swallowing clicks (unless this state's overlay is a known false alarm)."""
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
    overlay = not skip_overlay and bool((await scan_dom(page)).get("overlay"))
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


def _overlay_diagnostics(observed: Observation, false_alarm: bool) -> dict[str, Any]:
    return {
        "flagged": bool(observed.dom.get("overlay")),
        "matches": list(observed.dom.get("overlay_matches") or []),
        "rejected_candidates": int(observed.dom.get("overlay_rejected") or 0),
        "known_false_alarm": false_alarm,
    }


def screenshot_dir(start_url: str, root: Path = Path("output")) -> Path:
    """output/<host>/<timestamp>/ for this run's per-step screenshots."""
    host = urlparse(start_url).hostname or "site"
    return root / host / datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")


SCREENSHOT_ERRORS: tuple[type[Exception], ...] = (*BROWSER_ERRORS, ValueError)


async def _screenshot(page: Any, folder: Path | None, step: int) -> None:
    if folder is None:
        return
    try:
        folder.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=folder / f"step-{step:02d}.jpg", type="jpeg", quality=60, timeout=10_000)
    except SCREENSHOT_ERRORS as error:
        log.debug("Screenshot failed: %s", error)


async def _execute(
    stagehand: Any, page: Any, decision: NextAction, context: dict[str, Any], url_before: str
) -> dict[str, Any] | tuple[bool, str]:
    """Run one decision. A terminal status dict (DONE by the brake, BLOCKED), or (did something, failure note)."""
    hints: dict[str, Any] = context.get("hints") or {}
    try:
        done = await do_step(stagehand, page, decision, context)
        if await _url(page) != url_before:
            await _arrive(page, hints)  # a navigation: queue, hydration
    except CheckoutReachedInterrupt as brake:
        log.info("Safety brake: %s", brake)
        return {"status": "DONE", "url": await _url(page), "stopped_by": "safety_brake", "refused": brake.label[:200]}
    except LoginWallBlocked as blocked:
        return {"status": "BLOCKED", "url": await _url(page), "reason": str(blocked)}
    except ActionRefused as refused:
        log.info("  Refused: %s", refused)
        return False, f"{decision.route} refused ({refused}); choose a different element"
    except BROWSER_ERRORS as error:
        log.warning("  Step failed: %s", str(error)[:200])
        return False, f"{decision.route} {decision.instruction or ''} failed: {str(error)[:120]}"
    return done, ""


async def reach_checkout(stagehand: Any, page: Any, start_url: str, context: dict[str, Any]) -> dict[str, Any]:
    """Navigate from `start_url` to the payment wall and stop there. Returns the terminal status dictionary."""
    hints: dict[str, Any] = context.get("hints") or {}
    timeout = int(context.get("llm_timeout_ms") or 90_000)
    history: list[str] = []
    decisions: list[str] = []
    progress = Progress(stuck_limit=int(context.get("stuck_steps") or 4))
    shots = screenshot_dir(start_url) if context.get("screenshots") else None
    extra: dict[str, Any] = {"screenshots": str(shots)} if shots else {}

    await page.goto(start_url)
    try:
        await _arrive(page, hints)
    except LoginWallBlocked as blocked:
        return _done("BLOCKED", 0, await _url(page), reason=str(blocked), **extra)
    prev_state: tuple[str, str] | None = None

    for step in range(int(context["max_steps"])):
        elements = await cdp_observe(stagehand, page, timeout=timeout)
        await _screenshot(page, shots, step)

        # 1. Are we at checkout? If yes: done.
        if await is_at_checkout(page, elements):
            return _done("DONE", step, elements.url or await _url(page), history=history, **extra)

        current_state = (elements.url, compute_dom_hash(elements))
        progress.settled(current_state)  # an unchanged page counts the last decision as "no effect"
        matches = [str(m) for m in elements.dom.get("overlay_matches") or []]
        false_overlay = progress.overlay_is_false_alarm(current_state, matches)

        # Stuck: several steps in a row with no new URL and no new page state.
        if progress.observe(current_state):
            log.info("Stuck: %d steps without a new URL or page state", progress.stuck_steps)
            return _done("STUCK", step, elements.url, history=history, last_decisions=decisions[-4:],
                         overlay=_overlay_diagnostics(elements, false_overlay), **extra)  # fmt: skip

        # Stagnation guard: only when BOTH the URL and the interactive-DOM hash are unchanged since the last step.
        if current_state == prev_state:
            history.append(f"step {step}: no visible change after the last action; scrolled to recover")
            await recover_from_stagnation(stagehand, page, hints, skip_overlay=false_overlay)
            prev_state = None
            continue
        prev_state = current_state

        # 2. If no, what must we do to get there?
        avoid = progress.banned(current_state)
        decision = await determine_next_step(
            stagehand, elements, history, timeout, avoid=avoid, overlay_false_alarm=false_overlay
        )
        log.info("Step %d: %s %s | %s", step + 1, decision.route, decision.instruction or "", decision.reason)
        decisions.append(f"step {step}: {decision.route} {decision.instruction or ''} | {decision.reason}"[:240])
        if decision.blocked_reason:
            return _done("BLOCKED", step, elements.url, reason=decision.blocked_reason, history=history, **extra)

        # 3. Do (behind the hard payment safety brake).
        progress.decided(current_state, action_key(decision.route, decision.instruction))
        outcome = await _execute(stagehand, page, decision, context, elements.url)
        if isinstance(outcome, dict):
            return {**outcome, "steps": step, "history": history, **extra}
        done, failure = outcome
        progress.outcome(done)
        if failure:
            history.append(f"step {step}: {failure}")
        elif decision.route == "DO_DISMISS_OVERLAY" and not done:
            # Nothing to click and still "visible" after Escape: the overlay flag is wrong for this page state.
            progress.mark_false_overlay(current_state, matches)
            history.append(f"step {step}: overlay dismissal found nothing to close; treat the page as unobstructed")
        else:
            history.append(
                f"step {step}: {decision.route} {decision.instruction or ''} -> {'ok' if done else 'no effect'}"
            )

    # The last action may itself have landed on the payment wall: one model-free look before giving up.
    final = Observation(dom=await scan_dom(page), url=await _url(page))
    if await is_at_checkout(page, final):
        return _done("DONE", int(context["max_steps"]), final.url, history=history, **extra)
    return _done("MAX_STEPS_EXCEEDED", int(context["max_steps"]), final.url, history=history, **extra)
