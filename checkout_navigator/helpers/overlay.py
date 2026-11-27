"""DO_DISMISS_OVERLAY: cookie banners, promo/newsletter popups, app banners, region selectors and age gates.

Order of attempts (cheapest first): known consent-vendor buttons from `site_hints.yaml` (no model call), then one
safety-checked act() with a generic dismiss prompt, then the Escape key.
"""

from __future__ import annotations

import logging
from typing import Any

from checkout_navigator.engine.cdp_guard import dom_click, press_key, scan_dom, settle, try_cdp_act

log = logging.getLogger(__name__)

DISMISS_INSTRUCTION = (
    "Close the visible cookie banner, promotional / newsletter / discount popup, app-install banner, region or "
    "currency selector, or age-verification gate that covers the page. Click its 'Accept', 'Accept all', 'Close', "
    "'X', 'No thanks', 'Maybe later', 'Skip' or 'Continue to site' control (for an age gate, the 'Yes, I am over 18' "
    "/ 'Enter' button). Prefer an 'X' or 'Close' icon over a sign-up form. Never click Subscribe, Sign up, Join, "
    "Reveal code, a 'Skip to content' link, or an email field."
)
JS_CLICK_SELECTOR = r"""selectors => {
    for (const sel of selectors) {
        let el = null;
        try { el = document.querySelector(sel); } catch (e) { continue; }
        if (!el) continue;
        const r = el.getBoundingClientRect();
        if (r.width < 4 || r.height < 4) continue;
        el.scrollIntoView({ block: 'center' });
        const b = el.getBoundingClientRect();
        return { text: (el.innerText || el.getAttribute('aria-label') || sel).trim().slice(0, 80),
                 x: b.left + b.width / 2, y: b.top + b.height / 2 };
    }
    return null;
}"""


async def dismiss_overlay(stagehand: Any, page: Any, hints: dict[str, Any] | None = None) -> bool:
    """Try to clear whatever overlay blocks the page. True if something was clicked or the overlay is gone."""
    hints = hints or {}
    selectors = list(hints.get("dismiss_selectors") or [])
    if selectors and (clicked := await dom_click(page, JS_CLICK_SELECTOR, selectors, hints)):
        log.info("  Overlay: clicked %r (known consent control)", clicked.get("text"))
        return True

    instruction = DISMISS_INSTRUCTION
    if hints.get("overlay_hint"):
        instruction += f" {hints['overlay_hint']}"
    outcome = await try_cdp_act(stagehand, page, instruction, hints=hints)
    if outcome and outcome.success:
        log.info("  Overlay: %s", outcome.description)
        return True

    await press_key(page, "Escape")
    await settle(page, 800)
    still = (await scan_dom(page)).get("overlay")
    log.info("  Overlay: pressed Escape (%s)", "still visible" if still else "gone")
    return not still
