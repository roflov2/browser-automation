"""Uber Eats food-delivery drip-pricing POC.

Start on the Uber Eats AU homepage (or one or more direct `/store/...` URLs), set a delivery
address, capture the advertised delivery fee on the listing/banner, select a restaurant, add 1-5
menu items (handling required modifier groups with $0/cheapest options while declining paid
add-ons), open the cart, and walk to the checkout page.

Every optional extra, upsell and fee encountered along the way (modifier add-ons, 'Complete your
meal' upsells, Priority Delivery, Uber One membership prompts, Service Fee, Small Order Fee,
Delivery Fee changes, tips, ...) is recorded in `extras.csv`, and menu prices + advertised
delivery fees are compared against the final checkout breakdown.

The run always stops at the checkout/review screen (or login wall if unauthenticated in headless
mode): no payment details are entered and no order is placed.

Usage:
    # 1. Pre-authenticate the persistent local Chrome profile once:
    python uber-eats.py --login

    # 2. Run the drip-pricing audit:
    python uber-eats.py --address "240 City Walk, Canberra ACT 2601" --query "burgers" --items 2
    python uber-eats.py https://www.ubereats.com/au/store/... --items 3
"""

import os
import sys

os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

import argparse
import asyncio
import contextlib
import csv
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from urllib.parse import quote_plus, urljoin, urlparse

try:
    import psutil
except ImportError:
    psutil = None

from browserbase import Browserbase
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from stagehand import (
    Page,
    Stagehand,
    StagehandClientLoggingConfig,
    browserbase as stagehand_browserbase,
    local_browser,
)

load_dotenv()

SITE_URL = "https://www.ubereats.com/au"
DEFAULT_ADDRESS = os.getenv("UBEREATS_ADDRESS", "260 City Walk, Canberra ACT 2601")
SETTLE_MS = 2000  # fixed wait after navigation/clicks; Uber Eats is a heavy SPA
MAX_ADDRESS_STEPS = 10
MAX_MODIFIER_STEPS = 6
MAX_CHECKOUT_STEPS = 8
OUTPUT_ROOT = Path("output")
CHROME_PROFILE_DIR = Path(".chrome-profile")

NON_RESTAURANT_NAMES = (
    "coles", "woolworths", "aldi", "iga", "costco", "7-eleven", "7 eleven",
    "liquorland", "bws", "dan murphy", "first choice liquor", "vintage cellars",
    "chemist warehouse", "priceline", "ampol", "bp ", "caltex", "officeworks",
)

SAFETY_PROMPT = (
    "You are auditing a food-delivery ordering flow for price transparency. Never click a button that places, "
    "pays for, or submits an order (e.g. 'Place order', 'Pay', 'Submit order', 'Start Uber One trial'). "
    "Never enter payment card details, gift cards, or promo codes."
)

STEP_TYPES = (
    "address_setup, store_feed, store_menu, item_modal, upsell_modal, cart_drawer, "
    "checkout, login_wall, error, other"
)
EXTRA_CATEGORIES = (
    "delivery_fee, service_fee, small_order_fee, priority_delivery, express_delivery, "
    "uber_one_membership, item_modifier_paid, checkout_upsell, tip, bag_fee, "
    "payment_surcharge, other"
)


# ---------------------------------------------------------------------------
# Logging & Stream Noise Filter
# ---------------------------------------------------------------------------
_NOISY_CDP_PHRASES = (
    "CDP response failed",
    "Frame with the given id was not found",
    "Cannot find context with specified id",
    "Session with given id not found",
)


class _CdpNoiseFilter(logging.Filter):
    """Suppress harmless transient CDP frame/context race errors in Python logging."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not any(phrase in msg for phrase in _NOISY_CDP_PHRASES)


for _logger_name in ("", "stagehand", "stagehand.cdp_client", "websockets"):
    logging.getLogger(_logger_name).addFilter(_CdpNoiseFilter())


class _FilteredStream:
    """Suppress Stagehand's raw JSON CDP race errors written directly to stdout/stderr."""

    def __init__(self, stream):
        self._stream = stream

    def write(self, text: str) -> int:
        if any(phrase in text for phrase in _NOISY_CDP_PHRASES):
            return len(text)
        return self._stream.write(text)

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


sys.stdout = _FilteredStream(sys.stdout)
sys.stderr = _FilteredStream(sys.stderr)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class AddressAndFeedState(BaseModel):
    address_set: bool = Field(
        description="True if a specific street/suburb delivery address is already set in the header (not 'Enter delivery address')"
    )
    displayed_address: str | None = Field(description="Delivery address currently shown in the header, if any")
    address_suggestions_open: bool = Field(
        description="True if an autocomplete dropdown of address suggestions is currently visible"
    )
    blocked_reason: str | None = Field(
        description="If progress is blocked by a CAPTCHA, mandatory login wall, or bot check, describe it; otherwise null"
    )
    reason: str


class StoreCard(BaseModel):
    name: str = Field(description="Restaurant name as displayed (exclude supermarkets/grocery/liquor stores)")
    delivery_fee_display: str | None = Field(
        description="Advertised delivery fee on the card, e.g. '$2.99 Delivery Fee', '$0 Delivery Fee'; null if not shown"
    )
    service_fee_display: str | None = Field(description="Service fee if stated on the card; otherwise null")
    eta: str | None = Field(description="Estimated delivery time, e.g. '20-30 min'")
    available: bool = Field(description="True if the restaurant is currently open for delivery (not closed/unavailable)")
    is_restaurant: bool = Field(
        description="True for prepared-food restaurants/cafes/takeaway; False for supermarkets, grocery, convenience or liquor stores (e.g. Coles, Woolworths, 7-Eleven, BWS)"
    )
    url: str | None = Field(description="Link (href) to the `/store/...` page, if present")


class StoreFeed(BaseModel):
    stores: list[StoreCard]


class StoreHeaderInfo(BaseModel):
    store_name: str = Field(description="Restaurant name on the store page")
    advertised_delivery_fee: str | None = Field(
        description="Delivery fee shown in the store header/info banner, e.g. '$3.99 Delivery Fee'; null if not shown"
    )
    advertised_service_fee: str | None = Field(
        description="Service fee or minimum order requirement shown in the store header, if any"
    )
    store_open: bool = Field(description="True if the store is open and accepting orders now")


class MenuItem(BaseModel):
    name: str = Field(description="Clean menu item title without price, discount badge or calorie text")
    category: str | None = Field(description="Menu section heading, e.g. 'Burgers', 'Mains', 'Sides'")
    price: str = Field(description="Current price as displayed on the menu, e.g. '$14.70' or 'A$22.00'")
    sold_out: bool = Field(description="True if marked sold out or unavailable")
    is_food_or_drink: bool = Field(
        description="True for regular food, meals, sides or drinks; False for cutlery/utensils, napkins, bags, or donations"
    )


class StoreMenu(BaseModel):
    store_header: StoreHeaderInfo
    items: list[MenuItem]


class Offer(BaseModel):
    category: str = Field(description=f"One of: {EXTRA_CATEGORIES}")
    label: str = Field(
        description="Name of the extra, fee, upsell or delivery tier, e.g. 'Priority delivery', 'Service Fee', 'Add Bacon'"
    )
    price: str | None = Field(description="Price or rate as displayed, e.g. '$2.99', '+$3.00', '10%'; null if not shown")
    price_basis: str | None = Field(description="e.g. 'per item', 'per order', 'monthly subscription', if stated")
    selected: bool = Field(description="True if currently ticked/selected/included in the order total")
    mandatory: bool = Field(description="True if required to complete the item or order and cannot be removed/declined")


class ItemModalState(BaseModel):
    has_unmet_required_choices: bool = Field(
        description="True if ANY section marked 'Required' (or 'Choose 1') does NOT have a radio/checkbox selected yet. "
        "Ignore the 'Most popular' horizontal carousel at the top if actual 'Required' sections below it are still unselected."
    )
    next_required_group: str | None = Field(
        description="Exact heading of the first unselected 'Required' section (e.g. 'DOUBLE CHEESEBURGER', 'Choice of Side'); "
        "null only if every 'Required' section already has an active selection"
    )
    cheapest_option_in_required_group: str | None = Field(
        description="Exact label of the cheapest (ideally $0 / no '+$' price) option inside `next_required_group` (e.g. 'Single')"
    )
    button_price_display: str | None = Field(
        description="Price shown on the 'Add 1 to order • $XX.XX' button at the bottom of the modal"
    )
    paid_modifier_offers: list[Offer] = Field(
        description="Optional or required paid modifiers/add-ons visible in this modal (where price > $0), up to 10 items"
    )
    reason: str


class Fee(BaseModel):
    label: str
    amount: str


class LineItem(BaseModel):
    name: str
    variant: str | None = Field(description="Selected customizations/modifiers if shown")
    quantity: int
    price: str | None = Field(description="Line price as displayed, e.g. '$16.50'")


class CartAndCheckoutState(BaseModel):
    step_type: str = Field(
        description="One of: store_menu (if cart drawer is NOT open yet), cart_drawer (if the side cart drawer is open), "
        "upsell_modal (if an interstitial 'Complete your meal' / 'Uber One' popup is open), "
        "checkout (if on the final '/checkout' page with Delivery Fee & Service Fee), or login_wall (if sign-in is required)"
    )
    is_checkout_page: bool = Field(
        description="True ONLY if on the final checkout/review page showing the full fee breakdown (Subtotal, Delivery Fee, Service Fee, Total)"
    )
    is_cart_visible: bool = Field(
        description="True ONLY if the cart drawer/sidebar is open and listing line items with a 'Go to checkout' button (NOT just the small green header icon)"
    )
    is_upsell_modal: bool = Field(
        description="True if an interstitial upsell modal ('Complete your meal', 'Frequently bought together', 'Try Uber One') is blocking progress"
    )
    blocked_reason: str | None = Field(
        description="If progress is blocked by a mandatory login/sign-in screen, phone verification, CAPTCHA, or bot check, describe it; otherwise null"
    )
    items: list[LineItem]
    subtotal: str | None = Field(description="Items subtotal before delivery and service fees, if shown")
    delivery_fee: str | None = Field(description="Delivery fee currently shown, e.g. '$3.99', '$0.00'; null if not yet shown")
    service_fee: str | None = Field(description="Service fee currently shown, e.g. '$2.50'; null if not yet shown")
    small_order_fee: str | None = Field(description="Small order / minimum basket fee if shown; otherwise null")
    other_fees: list[Fee] = Field(
        description="Any other fees or charges shown (e.g. Priority fee, bag fee, delivery adjustment, tip) excluding Subtotal, Delivery Fee, Service Fee and Small Order Fee"
    )
    tax_amount: str | None = Field(description="Estimated taxes/GST if itemised separately")
    tax_included: bool = Field(description="True if GST/tax is included in prices")
    total: str | None = Field(description="Total amount due (or cart subtotal if full total not yet shown)")
    currency: str | None = Field(description="Currency code if shown, e.g. 'AUD'")
    offers: list[Offer] = Field(
        description="Every optional upsell, delivery speed tier (Priority vs Standard), Uber One prompt, tip preset, or fee visible on this screen"
    )
    summary: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def parse_price(value: str | None) -> float | None:
    """Parse '$1,234.56', 'AUD $21.00', '+$3.50', 'Free' ($0.0) into a float; None if no number."""
    if not value:
        return None
    if re.search(r"\bfree\b", value, re.IGNORECASE):
        return 0.0
    match = re.search(r"\d[\d.,\s]*", value)
    if not match:
        return None
    number = match.group(0).strip().replace(" ", "")
    if "," in number and "." in number:
        if number.rfind(",") > number.rfind("."):
            number = number.replace(".", "").replace(",", ".")
        else:
            number = number.replace(",", "")
    elif "," in number:
        head, _, tail = number.rpartition(",")
        number = f"{head.replace(',', '')}.{tail}" if len(tail) == 2 else number.replace(",", "")
    number = number.rstrip(".")
    try:
        return float(number)
    except ValueError:
        return None


def parse_amount(value: str | None) -> float | None:
    """Like parse_price, but None for percentages ('10%'), which are rates rather than dollar amounts."""
    return None if value and "%" in value else parse_price(value)


def money_sum(values: list[float | None]) -> float | None:
    known = [v for v in values if v is not None]
    return round(sum(known), 2) if known else None


def diff(a: float | None, b: float | None) -> float | None:
    return round(a - b, 2) if a is not None and b is not None else None


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "step"


def is_valid_restaurant_name(name: str | None) -> bool:
    if not name:
        return False
    low = name.lower().strip()
    return not any(bad in low for bad in NON_RESTAURANT_NAMES)


def is_login_url(url: str) -> bool:
    low = url.lower()
    return any(w in low for w in ("auth.uber.com", "login.uber.com", "/login-redirect", "/login"))


async def interactive_login_pause(page: Page, prompt_msg: str) -> bool:
    """Pause in headed mode so the user can sign in inside the persistent browser window."""
    if os.getenv("HEADLESS") == "true" or not sys.stdin.isatty():
        return False
    print(f"\n  [LOGIN REQUIRED] {prompt_msg}")
    print("  1. Complete the Uber Eats sign-in (and any SMS/email OTP) in the open browser window.")
    print("  2. Wait until the browser redirects back to Uber Eats.")
    await asyncio.to_thread(input, "  3. Press Enter here to continue... ")
    await settle(page, 2500)
    curr_url = await page.url()
    return not is_login_url(curr_url)


class LocalResourceMonitor:
    def __init__(self) -> None:
        self.cpu_usage: list[float] = []
        self.memory_usage: list[float] = []
        self._process = psutil.Process(os.getpid()) if psutil else None

    def start(self) -> asyncio.Task[None]:
        async def sample() -> None:
            if self._process is None:
                return
            self._process.cpu_percent(interval=None)
            while True:
                await asyncio.sleep(1)
                self.memory_usage.append(self._process.memory_info().rss / (1024 * 1024))
                self.cpu_usage.append(self._process.cpu_percent(interval=None))

        return asyncio.create_task(sample())

    def summary(self) -> dict[str, float]:
        if not self.memory_usage:
            return {"avg_memory_mb": 0.0, "peak_memory_mb": 0.0, "avg_cpu_percent": 0.0, "data_points": 0}
        return {
            "avg_memory_mb": round(sum(self.memory_usage) / len(self.memory_usage), 2),
            "peak_memory_mb": round(max(self.memory_usage), 2),
            "avg_cpu_percent": round(sum(self.cpu_usage) / len(self.cpu_usage), 2),
            "data_points": len(self.cpu_usage),
        }


@dataclass
class SiteResult:
    site: str
    out_dir: Path
    status: str = "error"
    message: str = ""
    store_name: str | None = None
    advertised_delivery_fee_display: str | None = None
    advertised_delivery_fee: float | None = None
    advertised_service_fee_display: str | None = None
    items: list[dict] = field(default_factory=list)
    extras: list[dict] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    comparison: dict = field(default_factory=dict)


class Blocked(RuntimeError):
    """The site presented a mandatory login wall, CAPTCHA, or bot check."""


# ---------------------------------------------------------------------------
# DOM & Modal Helpers (Strictly using page.evaluate — NO page.keyboard!)
# ---------------------------------------------------------------------------
async def settle(page: Page, ms: int = SETTLE_MS) -> None:
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("load")
    await page.wait_for_timeout(ms)


async def shot(page: Page, path: Path) -> None:
    try:
        await page.screenshot(path=str(path), full_page=True)
    except Exception:
        with contextlib.suppress(Exception):
            await page.screenshot(path=str(path))


async def _is_item_modal_open(page: Page) -> bool:
    """Fast DOM check for whether an Uber Eats item customization modal is currently visible."""
    with contextlib.suppress(Exception):
        return bool(
            await page.evaluate(
                """(() => {
                    const buttons = Array.from(document.querySelectorAll('button'));
                    for (const btn of buttons) {
                        const txt = (btn.innerText || '').trim();
                        if (/^(Add\\s+\\d*\\s*to\\s+(order|cart)|Update\\s+order)/i.test(txt)) {
                            const rect = btn.getBoundingClientRect();
                            if (rect.width > 80 && rect.height > 25) return true;
                        }
                    }
                    return false;
                })()"""
            )
        )
    return False


async def _get_cart_count(page: Page) -> int:
    """Read the integer item count from the green Uber Eats cart button in the header."""
    with contextlib.suppress(Exception):
        count = await page.evaluate(
            """(() => {
                const candidates = Array.from(document.querySelectorAll(
                    '[data-testid*="cart" i], button[aria-label*="cart" i], button[aria-label*="bag" i], header button'
                ));
                for (const el of candidates) {
                    const label = (el.getAttribute('aria-label') || '') + ' ' + (el.innerText || '');
                    const m = label.match(/(\\d+)\\s*item/i);
                    if (m) return parseInt(m[1], 10);
                }
                for (const el of candidates) {
                    const txt = (el.innerText || '').trim();
                    if (/^\\d+$/.test(txt)) return parseInt(txt, 10);
                }
                return 0;
            })()"""
        )
        if isinstance(count, int):
            return count
    return 0


async def _dom_auto_select_required_in_modal(page: Page) -> list[str]:
    """Find any unselected 'Required' radio/checkbox group inside the open item modal and click the cheapest option."""
    with contextlib.suppress(Exception):
        picked = await page.evaluate(
            """(() => {
                const chosen = [];
                const dialogs = Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"]'));
                const root = dialogs.find(d => d.getBoundingClientRect().height > 200) || document.body;

                const radios = Array.from(root.querySelectorAll('input[type="radio"]'));
                const groups = new Map();
                for (const r of radios) {
                    const key = r.name || r.closest('[role="radiogroup"]')?.id || '';
                    if (!key) continue;
                    if (!groups.has(key)) groups.set(key, []);
                    groups.get(key).push(r);
                }

                for (const [groupName, list] of groups.entries()) {
                    if (list.some(r => r.checked)) continue;
                    const containerText = (list[0].closest('div')?.parentElement?.innerText || '');
                    if (groups.size > 1 && /most popular|#1 ordered/i.test(containerText)) continue;

                    let best = list[0];
                    let bestLabel = '';
                    for (const r of list) {
                        const row = r.closest('label') || r.parentElement?.parentElement || r.parentElement;
                        const txt = (row ? row.innerText : '').trim();
                        if (!/\\+\\s*(A?\\$|AUD)/i.test(txt)) {
                            best = r;
                            bestLabel = txt.split('\\n')[0].trim();
                            break;
                        }
                        if (!bestLabel) bestLabel = txt.split('\\n')[0].trim();
                    }
                    const clickTarget = best.closest('label') || best;
                    clickTarget.scrollIntoView({ block: 'center', behavior: 'instant' });
                    clickTarget.click();
                    if (bestLabel) chosen.push(bestLabel);
                }
                return chosen;
            })()"""
        )
        if isinstance(picked, list):
            return [str(x) for x in picked if x]
    return []


async def _dom_click_add_to_order(page: Page) -> tuple[bool, str | None]:
    """Click the bottom 'Add X to order • $XX.XX' button inside the open modal via DOM and return (clicked, price_text)."""
    with contextlib.suppress(Exception):
        res = await page.evaluate(
            """(() => {
                const buttons = Array.from(document.querySelectorAll('button'));
                for (const btn of buttons) {
                    const txt = (btn.innerText || '').trim();
                    if (/^(Add\\s+\\d*\\s*to\\s+(order|cart)|Update\\s+order)/i.test(txt)) {
                        const rect = btn.getBoundingClientRect();
                        if (rect.width > 80 && rect.height > 25) {
                            const priceMatch = txt.match(/(A?\\$[\\d.,]+)/);
                            btn.click();
                            return { clicked: true, price: priceMatch ? priceMatch[1] : null };
                        }
                    }
                }
                return { clicked: false, price: null };
            })()"""
        )
        if isinstance(res, dict):
            return bool(res.get("clicked")), res.get("price")
    return False, None


async def _force_close_item_modal(stagehand: Stagehand, page: Page) -> None:
    """Ensure no lingering item modal blocks the rest of the page using DOM events (no page.keyboard)."""
    if not await _is_item_modal_open(page):
        return
    print("    Closing lingering item modal...")
    with contextlib.suppress(Exception):
        await page.evaluate(
            """(() => {
                document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', code: 'Escape', keyCode: 27, bubbles: true }));
                document.body.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', code: 'Escape', keyCode: 27, bubbles: true }));

                const dialogs = document.querySelectorAll('[role="dialog"], [aria-modal="true"]');
                for (const d of dialogs) {
                    const btn = d.querySelector(
                        'button[aria-label*="Close" i], button[aria-label*="close" i], button:first-of-type'
                    );
                    if (btn) {
                        const txt = (btn.innerText || '').trim();
                        if (!txt || txt === '×' || txt === '✕' || txt.length <= 2) {
                            btn.click();
                            return;
                        }
                    }
                }
            })()"""
        )
        await page.wait_for_timeout(1000)

    if await _is_item_modal_open(page):
        with contextlib.suppress(Exception):
            await stagehand.act("Click the 'X' close button in the top-left corner of the open item modal.")
            await page.wait_for_timeout(1000)


async def _dom_open_cart_drawer(page: Page) -> bool:
    """Click the header cart button via DOM to open the cart drawer."""
    with contextlib.suppress(Exception):
        clicked = await page.evaluate(
            """(() => {
                const selectors = [
                    '[data-testid*="cart" i]',
                    'button[aria-label*="cart" i]',
                    'button[aria-label*="bag" i]',
                    'a[href*="/checkout"]'
                ];
                for (const sel of selectors) {
                    for (const el of document.querySelectorAll(sel)) {
                        const rect = el.getBoundingClientRect();
                        if (rect.width > 20 && rect.height > 20) {
                            el.click();
                            return true;
                        }
                    }
                }
                for (const btn of document.querySelectorAll('header button, header a')) {
                    if (/\\d+\\s*item/i.test(btn.innerText || '') || /\\d+\\s*item/i.test(btn.getAttribute('aria-label') || '')) {
                        btn.click();
                        return true;
                    }
                }
                return false;
            })()"""
        )
        return bool(clicked)
    return False


async def resolve_store_url(page: Page, preferred_name: str | None = None) -> tuple[str | None, str | None]:
    """Find a valid `/store/...` restaurant URL and title directly from the DOM, skipping supermarkets/liquor."""
    payload = json.dumps({
        "preferred": preferred_name or "",
        "excluded": list(NON_RESTAURANT_NAMES),
    })
    with contextlib.suppress(Exception):
        found = await page.evaluate(
            f"""(({{ preferred, excluded }}) => {{
                const norm = s => (s || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
                const prefNorm = norm(preferred);
                const anchors = Array.from(document.querySelectorAll('a[href*="/store/"]'));
                const candidates = [];

                for (const a of anchors) {{
                    const href = a.href;
                    if (!href || !href.includes('/store/')) continue;
                    const heading = a.querySelector('h1, h2, h3, h4, [data-testid*="store" i]') || a;
                    const rawText = (heading.innerText || a.getAttribute('aria-label') || a.innerText || '')
                        .split('\\n')[0].trim();
                    const low = rawText.toLowerCase();
                    if (!rawText || excluded.some(bad => low.includes(bad) || href.toLowerCase().includes('/store/' + bad))) {{
                        continue;
                    }}
                    const rect = a.getBoundingClientRect();
                    if (rect.width < 40 || rect.height < 20) continue;
                    candidates.push({{ name: rawText, url: href, match: prefNorm && norm(rawText).includes(prefNorm) }});
                }}
                const exact = candidates.find(c => c.match);
                return exact || candidates[0] || null;
            }})({payload})"""
        )
        if isinstance(found, dict) and found.get("url"):
            return found.get("name"), found.get("url")
    return None, None


async def dismiss_popups(stagehand: Stagehand) -> None:
    """Close cookie banners, promo popups, Uber One trial modals, or app install banners."""
    with contextlib.suppress(Exception):
        found = await stagehand.observe(
            "Find the close, dismiss, 'No thanks', 'Maybe later', 'Skip', or accept-cookies button of any visible "
            "cookie banner, promotional popup, Uber One trial offer, or app-install banner. "
            "Do NOT click 'Sign in' or 'Continue' on an order. Return nothing if there is none."
        )
        for action in found.data[:2]:
            print(f"  Dismissing popup: {action.description}")
            await stagehand.act(action)


async def maybe_dismiss_popups(stagehand: Stagehand, page: Page) -> None:
    """Fast DOM check for visible modal/dialog overlays before invoking LLM observe()."""
    has_overlay = False
    with contextlib.suppress(Exception):
        has_overlay = bool(
            await page.evaluate(
                """(() => {
                    const selectors = [
                        '[role="dialog"]', '[aria-modal="true"]',
                        '[class*="cookie" i]', '[id*="cookie" i]',
                        '[class*="promo" i]', '[class*="banner" i]'
                    ];
                    for (const el of document.querySelectorAll(selectors.join(','))) {
                        const style = window.getComputedStyle(el);
                        const rect = el.getBoundingClientRect();
                        if (style.display !== 'none' && style.visibility !== 'hidden'
                            && parseFloat(style.opacity || '1') > 0.1
                            && rect.width > 120 && rect.height > 50) {
                            return true;
                        }
                    }
                    return false;
                })()"""
            )
        )
    if has_overlay:
        await dismiss_popups(stagehand)


def record_offers(result: SiteResult, step_num: int, step_type: str, heading: str | None, offers: list[Offer]) -> None:
    seen = {(e["step_type"], e["category"], e["label"].lower()) for e in result.extras}
    for offer in offers:
        key = (step_type, offer.category, offer.label.lower())
        if key in seen:
            continue
        seen.add(key)
        result.extras.append({
            "step": step_num,
            "step_type": step_type,
            "step_heading": heading,
            "category": offer.category,
            "label": offer.label,
            "price_display": offer.price,
            "price": parse_amount(offer.price),
            "price_basis": offer.price_basis,
            "selected_by_default": offer.selected,
            "mandatory": offer.mandatory,
        })


# ---------------------------------------------------------------------------
# Site steps
# ---------------------------------------------------------------------------
async def ensure_address_and_open_store(
    stagehand: Stagehand,
    page: Page,
    address: str,
    search_query: str | None,
    result: SiteResult,
) -> str:
    """Set the delivery address, navigate to `/au/search?q=...` if a query is provided, and open a `/store/...` page."""
    question = (
        f"We want delivery to '{address}'. Report whether a specific street/suburb delivery address is already set "
        "in the header (or if an autocomplete dropdown of address suggestions is currently open), and if anything is blocking progress."
    )
    query_submitted = False

    for step_idx in range(MAX_ADDRESS_STEPS):
        curr_url = await page.url()
        if "login.microsoftonline.com" in curr_url or "login.windows.net" in curr_url:
            print("  Waiting for corporate SSO redirect...")
            await page.wait_for_timeout(3000)
            continue

        if is_login_url(curr_url):
            if await interactive_login_pause(page, f"Uber login wall detected ({curr_url})."):
                curr_url = await page.url()
            else:
                raise Blocked(f"Redirected to Uber login wall ({curr_url})")

        state = (await stagehand.extract(question, AddressAndFeedState)).data
        if state.blocked_reason:
            raise Blocked(state.blocked_reason)

        # 1) Set delivery address if not yet set
        if not state.address_set and "pl=" not in curr_url:
            if state.address_suggestions_open:
                act_res = await stagehand.act(
                    f"Click the first autocomplete address suggestion matching '{address}' (or click 'Done' / 'Save')."
                )
            else:
                act_res = await stagehand.act(
                    f"Set the delivery address to '{address}': if an address input box is visible, type '{address}' into it; "
                    "otherwise click the 'Enter delivery address' / location selector button in the header."
                )
            print(f"  Address step {step_idx + 1}: {act_res.data.action_description}")
            await settle(page, 2200)
            continue

        if "/store/" in curr_url:
            return curr_url

        # 2) Navigate directly to Uber Eats' search results URL once the address session is set
        if search_query and not query_submitted and "q=" not in curr_url.lower():
            search_url = f"https://www.ubereats.com/au/search?q={quote_plus(search_query)}"
            print(f"  Navigating to search results: {search_url}")
            await page.goto(search_url)
            query_submitted = True
            await settle(page, 3000)
            curr_url = await page.url()

        # 3) Extract restaurants from the feed and resolve a real `/store/...` link
        feed = (
            await stagehand.extract(
                "Extract the prepared-food restaurants visible on this page with their advertised delivery fee, "
                "service fee, ETA, availability, and `/store/...` link. "
                "Exclude supermarkets, grocery stores, convenience stores, and liquor stores (e.g. Coles, Woolworths, 7-Eleven, BWS).",
                StoreFeed,
            )
        ).data

        valid_stores = [
            s for s in feed.stores
            if s.available and s.is_restaurant and is_valid_restaurant_name(s.name)
        ]
        if not valid_stores:
            valid_stores = [s for s in feed.stores if is_valid_restaurant_name(s.name)]

        chosen_store = valid_stores[0] if valid_stores else None
        if chosen_store:
            result.store_name = chosen_store.name
            result.advertised_delivery_fee_display = chosen_store.delivery_fee_display
            result.advertised_delivery_fee = parse_price(chosen_store.delivery_fee_display)
            result.advertised_service_fee_display = chosen_store.service_fee_display

        target_url: str | None = None
        if chosen_store and chosen_store.url and "/store/" in chosen_store.url:
            target_url = urljoin("https://www.ubereats.com", chosen_store.url)
        else:
            dom_name, dom_url = await resolve_store_url(page, chosen_store.name if chosen_store else None)
            if dom_url:
                target_url = dom_url
                if not result.store_name and dom_name:
                    result.store_name = dom_name

        if target_url:
            print(
                f"  Picking store: {result.store_name or target_url} "
                f"(Advertised delivery: {result.advertised_delivery_fee_display or 'not shown'})"
            )
            await page.goto(target_url)
            await settle(page, 2800)
            if "/store/" in await page.url():
                return await page.url()

        if chosen_store:
            await stagehand.act(f"Click on the restaurant card '{chosen_store.name}' to open its menu page.")
        else:
            await stagehand.act("Scroll down and click on the first open restaurant card (not a grocery store).")
        await settle(page, 2500)
        if "/store/" in await page.url():
            return await page.url()

    raise RuntimeError(f"Could not open a `/store/` restaurant page (ended on {await page.url()})")


def choose_menu_items(items: list[MenuItem], n: int) -> list[MenuItem]:
    available = [
        i for i in items
        if i.is_food_or_drink and not i.sold_out and (parse_price(i.price) or 0) > 3.0
    ]
    return available[:n]


async def add_menu_item_to_cart(
    stagehand: Stagehand,
    page: Page,
    menu_item: MenuItem,
    result: SiteResult,
    item_idx: int,
) -> dict:
    item: dict = {
        "name": menu_item.name,
        "shop_price_display": menu_item.price,
        "shop_price": parse_price(menu_item.price),
        "selected_variant": None,
        "selected_price_display": None,
        "selected_price": None,
        "status": "skipped",
    }

    await _force_close_item_modal(stagehand, page)
    before_cart_count = await _get_cart_count(page)

    try:
        step = await stagehand.act(
            f"Click on the menu item '{menu_item.name}' (priced around {menu_item.price}) to open its customization modal."
        )
        print(f"    Open item: {step.data.action_description}")
        await settle(page, 1800)

        if not await _is_item_modal_open(page):
            if await _get_cart_count(page) > before_cart_count:
                item["selected_price_display"] = menu_item.price
                item["selected_price"] = item["shop_price"]
                item["status"] = "added"
                return item

            retry = await stagehand.act(
                f"Scroll to and click directly on the menu item title '{menu_item.name}' to open its popup."
            )
            print(f"    Retry open item: {retry.data.action_description}")
            await settle(page, 1800)

        if not await _is_item_modal_open(page):
            print(f"    Modal did not open for '{menu_item.name}', skipping.")
            return item

        selected_modifiers: list[str] = []

        for _ in range(MAX_MODIFIER_STEPS):
            if not await _is_item_modal_open(page):
                item["status"] = "added"
                break

            # 1) Fast DOM pass: auto-select any unselected radio group (e.g. 'DOUBLE CHEESEBURGER -> Single')
            dom_picked = await _dom_auto_select_required_in_modal(page)
            if dom_picked:
                print(f"    Auto-selected required option(s): {', '.join(dom_picked)}")
                selected_modifiers.extend(dom_picked)
                await page.wait_for_timeout(800)

            # 2) Extract modal offers and check if any required groups remain unmet
            modal_state = (
                await stagehand.extract(
                    f"We are inside the item modal for '{menu_item.name}' (menu price {menu_item.price}). "
                    "Check if any section marked 'Required' (or 'Choose 1') still has NO option selected. "
                    "Also extract the price on the bottom 'Add to order' button and up to 10 paid modifier options.",
                    ItemModalState,
                )
            ).data

            if modal_state.paid_modifier_offers:
                record_offers(
                    result,
                    step_num=item_idx,
                    step_type="item_modal",
                    heading=menu_item.name,
                    offers=modal_state.paid_modifier_offers,
                )

            if modal_state.button_price_display:
                item["selected_price_display"] = modal_state.button_price_display
                item["selected_price"] = parse_price(modal_state.button_price_display) or item["shop_price"]

            if modal_state.has_unmet_required_choices and modal_state.next_required_group:
                choice_hint = modal_state.cheapest_option_in_required_group or "the first $0 / cheapest option"
                act_mod = await stagehand.act(
                    f"In the required section '{modal_state.next_required_group}', select '{choice_hint}'."
                )
                print(f"    Modifier ({modal_state.next_required_group}): {act_mod.data.action_description}")
                if modal_state.cheapest_option_in_required_group:
                    selected_modifiers.append(modal_state.cheapest_option_in_required_group)
                await page.wait_for_timeout(900)
                continue

            # 3) Click 'Add X to order' via DOM first (fallback to Stagehand)
            clicked, btn_price = await _dom_click_add_to_order(page)
            if btn_price and not item["selected_price_display"]:
                item["selected_price_display"] = btn_price
                item["selected_price"] = parse_price(btn_price) or item["shop_price"]

            if not clicked:
                add_act = await stagehand.act(
                    "Click the black 'Add 1 to order' / 'Add to order' button at the bottom of the item modal."
                )
                print(f"    Click Add: {add_act.data.action_description}")
            else:
                print(f"    Clicked Add to order button ({btn_price or item['selected_price_display'] or menu_item.price})")

            await settle(page, 1800)

            if not await _is_item_modal_open(page) or (await _get_cart_count(page) > before_cart_count):
                item["status"] = "added"
                break

            print("    Modal remained open after clicking Add; checking newly scrolled required section...")

        if item["selected_price"] is None:
            item["selected_price_display"] = menu_item.price
            item["selected_price"] = item["shop_price"]
        item["selected_variant"] = ", ".join(dict.fromkeys(selected_modifiers)) if selected_modifiers else None

        if item["status"] != "added":
            item["status"] = "add_failed"

        return item
    finally:
        await _force_close_item_modal(stagehand, page)


async def walk_to_checkout(
    stagehand: Stagehand,
    page: Page,
    result: SiteResult,
) -> tuple[CartAndCheckoutState | None, CartAndCheckoutState | None]:
    """Open the cart drawer, decline interstitial upsells, and proceed to the checkout screen."""
    out = result.out_dir
    cart_snapshot: CartAndCheckoutState | None = None
    checkout_snapshot: CartAndCheckoutState | None = None

    await _force_close_item_modal(stagehand, page)

    if await _dom_open_cart_drawer(page):
        print("  Opened cart drawer via header cart button")
        await settle(page, 2000)

    for step_idx in range(1, MAX_CHECKOUT_STEPS + 1):
        curr_url = await page.url()
        if is_login_url(curr_url):
            await shot(page, out / f"step-{step_idx:02d}-login-wall.png")
            if await interactive_login_pause(page, f"Uber login wall reached before checkout ({curr_url})."):
                curr_url = await page.url()
                if "/checkout" not in curr_url:
                    print("  Navigating to /au/checkout after login...")
                    await page.goto("https://www.ubereats.com/au/checkout")
                    await settle(page, 3000)
                    curr_url = await page.url()
            else:
                raise Blocked(f"Redirected to Uber login wall ({curr_url})")

        state = (
            await stagehand.extract(
                "Examine the current screen in the Uber Eats ordering flow. "
                "IMPORTANT: `is_cart_visible` is True ONLY if the side cart drawer/panel is open showing line items "
                "and a 'Go to checkout' button (NOT just the small green cart icon in the top header). "
                "Extract all visible line items, subtotal, delivery fee, service fee, small order fee, any other fees, "
                "total, and every optional upsell, Priority delivery option, Uber One offer, or tip.",
                CartAndCheckoutState,
            )
        ).data

        step_type = "checkout" if state.is_checkout_page else state.step_type
        print(
            f"  Step {step_idx}: {step_type} | subtotal={state.subtotal} | "
            f"delivery={state.delivery_fee} | service={state.service_fee} | total={state.total}"
        )
        await shot(page, out / f"step-{step_idx:02d}-{slug(step_type)}.png")
        record_offers(result, step_idx, step_type, step_type, state.offers)

        result.steps.append({
            "step": step_idx,
            "step_type": step_type,
            "url": curr_url,
            "subtotal_display": state.subtotal,
            "delivery_fee_display": state.delivery_fee,
            "service_fee_display": state.service_fee,
            "small_order_fee_display": state.small_order_fee,
            "total_display": state.total,
            "total": parse_price(state.total),
            "offers": len(state.offers),
            "summary": state.summary,
        })

        if await _is_item_modal_open(page):
            await _force_close_item_modal(stagehand, page)
            await _dom_open_cart_drawer(page)
            await settle(page, 1800)
            continue

        if state.is_cart_visible and cart_snapshot is None:
            cart_snapshot = state
            await shot(page, out / "cart.png")

        if state.is_checkout_page or "/checkout" in curr_url:
            checkout_snapshot = state
            await shot(page, out / "checkout.png")
            return cart_snapshot, checkout_snapshot

        if state.blocked_reason or step_type == "login_wall":
            if await interactive_login_pause(page, state.blocked_reason or "Mandatory sign-in screen before checkout."):
                if "/checkout" not in (await page.url()):
                    await page.goto("https://www.ubereats.com/au/checkout")
                    await settle(page, 3000)
                continue
            raise Blocked(state.blocked_reason or "Mandatory sign-in / login wall before checkout")

        if state.is_upsell_modal or step_type == "upsell_modal":
            act_res = await stagehand.act(
                "Decline or skip the upsell/addon modal (click 'Skip', 'No thanks', 'Continue to checkout', or close it) "
                "without adding any extra items or starting an Uber One trial."
            )
            print(f"    Declining upsell: {act_res.data.action_description}")
            await settle(page, 2000)
            continue

        if not state.is_cart_visible:
            if not await _dom_open_cart_drawer(page):
                act_res = await stagehand.act(
                    "Click the green cart button in the top-right header (or 'View cart') to open the cart drawer."
                )
                print(f"    Opening cart: {act_res.data.action_description}")
            else:
                print("    Opening cart via DOM click on header cart button")
            await settle(page, 2200)
            continue

        clicked_checkout = False
        with contextlib.suppress(Exception):
            clicked_checkout = bool(
                await page.evaluate(
                    """(() => {
                        const els = Array.from(document.querySelectorAll('a[href*="/checkout"], button'));
                        for (const el of els) {
                            const txt = (el.innerText || '').trim();
                            if (/^(Go to checkout|Checkout|Continue to checkout)/i.test(txt)) {
                                el.click();
                                return true;
                            }
                        }
                        return false;
                    })()"""
                )
            )
        if clicked_checkout:
            print("    Clicked 'Go to checkout' button")
        else:
            act_res = await stagehand.act(
                "Click the 'Go to checkout' / 'Checkout' button in the cart drawer. Never click 'Place order' or 'Pay'."
            )
            print(f"    Proceeding to checkout: {act_res.data.action_description}")
        await settle(page, 3000)

    return cart_snapshot, checkout_snapshot


def _match_checkout_item(item_name: str, candidates: list[LineItem]) -> LineItem | None:
    """Find and pop the best-matching checkout LineItem using token Jaccard similarity."""
    if not candidates:
        return None

    def norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()

    target_full = norm(item_name)
    target_tokens = set(target_full.split())

    best_idx: int | None = None
    best_score: float = 0.0

    for idx, li in enumerate(candidates):
        li_full = norm(li.name)
        li_tokens = set(li_full.split())
        if li_full == target_full:
            score = 100.0
        elif target_tokens and li_tokens:
            overlap = len(target_tokens & li_tokens)
            score = (overlap / len(target_tokens | li_tokens)) * 50.0
        else:
            score = 0.0
        if score > best_score:
            best_score = score
            best_idx = idx

    if best_idx is not None and best_score >= 20.0:
        return candidates.pop(best_idx)
    return None


def compare_prices(
    result: SiteResult,
    cart: CartAndCheckoutState | None,
    checkout: CartAndCheckoutState | None,
) -> dict:
    final_state = checkout or cart
    added = [i for i in result.items if i["status"] == "added"]
    remaining_checkout = list(final_state.items) if final_state else []

    for item in added:
        expected = item["selected_price"] if item["selected_price"] is not None else item["shop_price"]
        item["expected_price"] = expected
        item["selected_vs_shop_diff"] = diff(item["selected_price"], item["shop_price"])
        match = _match_checkout_item(item["name"], remaining_checkout)
        item["checkout_price_display"] = match.price if match else None
        checkout_unit = parse_price(match.price) if match else None
        if checkout_unit is not None and match and match.quantity > 1:
            checkout_unit = round(checkout_unit / match.quantity, 2)
        item["checkout_price"] = checkout_unit
        item["checkout_vs_expected_diff"] = diff(checkout_unit, expected)
        item["price_increased"] = bool(item["checkout_vs_expected_diff"] and item["checkout_vs_expected_diff"] > 0)

    expected_subtotal = money_sum([i["expected_price"] for i in added])
    subtotal = parse_price(final_state.subtotal) if final_state else None
    total = parse_price(final_state.total) if final_state else None

    checkout_delivery_fee = parse_price(final_state.delivery_fee) if final_state else None
    service_fee = parse_price(final_state.service_fee) if final_state else None
    small_order_fee = parse_price(final_state.small_order_fee) if final_state else None

    fees: list[dict] = []
    if service_fee is not None and service_fee > 0:
        fees.append({"label": "Service Fee", "amount": service_fee})
    if small_order_fee is not None and small_order_fee > 0:
        fees.append({"label": "Small Order Fee", "amount": small_order_fee})
    for f in (final_state.other_fees if final_state else []):
        amt = parse_amount(f.amount)
        fees.append({"label": f.label, "amount": amt})

    fees_total = money_sum([f["amount"] for f in fees]) or 0.0

    if service_fee is not None:
        result.extras.append({
            "step": len(result.steps),
            "step_type": "checkout",
            "step_heading": "Checkout Breakdown",
            "category": "service_fee",
            "label": "Service Fee",
            "price_display": final_state.service_fee if final_state else None,
            "price": service_fee,
            "price_basis": "per order",
            "selected_by_default": True,
            "mandatory": True,
        })
    if small_order_fee is not None:
        result.extras.append({
            "step": len(result.steps),
            "step_type": "checkout",
            "step_heading": "Checkout Breakdown",
            "category": "small_order_fee",
            "label": "Small Order Fee",
            "price_display": final_state.small_order_fee if final_state else None,
            "price": small_order_fee,
            "price_basis": "per order",
            "selected_by_default": True,
            "mandatory": True,
        })

    delivery_diff = diff(checkout_delivery_fee, result.advertised_delivery_fee)
    delivery_increased = bool(delivery_diff is not None and delivery_diff > 0)
    item_increase = any(i.get("price_increased") for i in added)
    subtotal_increase = (diff(subtotal, expected_subtotal) or 0) > 0
    preselected = [
        e for e in result.extras
        if e["selected_by_default"] and not e["mandatory"] and (e["price"] or 0) > 0
    ]

    reasons = [
        r for r, hit in (
            ("menu item price increased at checkout", item_increase),
            ("checkout item subtotal higher than menu prices", subtotal_increase),
            (f"service fee added ({final_state.service_fee if final_state else ''})", (service_fee or 0) > 0),
            (f"small order fee added ({final_state.small_order_fee if final_state else ''})", (small_order_fee or 0) > 0),
            (
                f"delivery fee higher than advertised ({result.advertised_delivery_fee_display} -> "
                f"{final_state.delivery_fee if final_state else ''})",
                delivery_increased,
            ),
            ("other non-tax fees added", fees_total > ((service_fee or 0) + (small_order_fee or 0))),
            ("optional extras pre-selected: " + ", ".join(e["label"] for e in preselected), bool(preselected)),
        )
        if hit
    ]

    by_category: dict[str, int] = {}
    for extra in result.extras:
        by_category[extra["category"]] = by_category.get(extra["category"], 0) + 1

    return {
        "store_name": result.store_name,
        "items_added": len(added),
        "expected_subtotal": expected_subtotal,
        "cart_subtotal_display": cart.subtotal if cart else None,
        "checkout_subtotal_display": final_state.subtotal if final_state else None,
        "checkout_subtotal": subtotal,
        "subtotal_vs_expected_diff": diff(subtotal, expected_subtotal),
        "advertised_delivery_fee_display": result.advertised_delivery_fee_display,
        "advertised_delivery_fee": result.advertised_delivery_fee,
        "shipping": (
            final_state.delivery_fee
            if final_state and final_state.delivery_fee
            else (result.advertised_delivery_fee_display or "not calculated (login/checkout required)")
        ),
        "checkout_delivery_fee": checkout_delivery_fee,
        "delivery_fee_diff": delivery_diff,
        "service_fee_display": final_state.service_fee if final_state else None,
        "service_fee": service_fee,
        "small_order_fee_display": final_state.small_order_fee if final_state else None,
        "small_order_fee": small_order_fee,
        "other_fees": fees,
        "other_fees_total": fees_total,
        "extras_offered": len(result.extras),
        "extras_offered_by_category": by_category,
        "extras_preselected": [e["label"] for e in preselected],
        "tax_amount_display": final_state.tax_amount if final_state else None,
        "tax_included": final_state.tax_included if final_state else None,
        "checkout_total_display": final_state.total if final_state else None,
        "checkout_total": total,
        "total_vs_expected_diff": diff(total, expected_subtotal),
        "currency": (final_state.currency if final_state and final_state.currency else "AUD"),
        "drip_detected": bool(reasons),
        "drip_reasons": reasons,
        "checkout_summary": final_state.summary if final_state else None,
    }


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


EXTRA_FIELDS = [
    "step", "step_type", "step_heading", "category", "label",
    "price_display", "price", "price_basis", "selected_by_default", "mandatory",
]
ITEM_FIELDS = [
    "name", "selected_variant", "shop_price_display", "selected_price_display",
    "checkout_price_display", "checkout_vs_expected_diff", "price_increased", "status",
]


def write_reports(
    result: SiteResult,
    address: str,
    query: str | None,
    store_url: str | None,
    checkout_url: str | None,
) -> None:
    """Written even when a run ends early (e.g. login wall), preserving all captured items and extras."""
    out = result.out_dir
    report = {
        "site": result.site,
        "shop_url": store_url,
        "checkout_url": checkout_url,
        "status": result.status,
        "message": result.message,
        "search": {"address": address, "query": query},
        "items": result.items,
        **result.comparison,
        "extras": result.extras,
        "steps": result.steps,
    }
    (out / "price_comparison.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    write_csv(out / "price_comparison.csv", ITEM_FIELDS, result.items)
    write_csv(out / "extras.csv", EXTRA_FIELDS, result.extras)


async def run_site(
    stagehand: Stagehand,
    page: Page,
    url: str,
    address: str,
    query: str | None,
    n_items: int,
    result: SiteResult,
) -> None:
    out = result.out_dir
    store_url: str | None = None
    cart_state: CartAndCheckoutState | None = None
    checkout_state: CartAndCheckoutState | None = None

    print(f"\n=== {url} | Address: {address} | Query: {query or 'any'} ===")
    try:
        await page.goto(url)
        await settle(page)
        await maybe_dismiss_popups(stagehand, page)

        store_url = await ensure_address_and_open_store(stagehand, page, address, query, result)
        print(f"  Store page: {store_url}")
        await shot(page, out / "store.png")

        menu = (
            await stagehand.extract(
                "Extract the restaurant name, advertised delivery fee and service fee shown in the store header/banner, "
                "and extract the menu items listed on this page with their category, price, availability, and whether "
                "each is food/drink.",
                StoreMenu,
            )
        ).data

        if menu.store_header.store_name:
            result.store_name = menu.store_header.store_name
        if menu.store_header.advertised_delivery_fee and not result.advertised_delivery_fee_display:
            result.advertised_delivery_fee_display = menu.store_header.advertised_delivery_fee
            result.advertised_delivery_fee = parse_price(menu.store_header.advertised_delivery_fee)
        if menu.store_header.advertised_service_fee and not result.advertised_service_fee_display:
            result.advertised_service_fee_display = menu.store_header.advertised_service_fee

        write_csv(out / "products.csv", list(MenuItem.model_fields), [m.model_dump() for m in menu.items])
        print(
            f"  Store: {result.store_name} | Extracted {len(menu.items)} menu items "
            f"| Advertised delivery: {result.advertised_delivery_fee_display or 'n/a'}"
        )

        chosen = choose_menu_items(menu.items, n_items)
        if not chosen:
            result.status, result.message = "no_items_found", "No available food/drink items found on the store menu"
            return

        for idx, menu_item in enumerate(chosen, start=1):
            print(f"  Adding ({idx}/{len(chosen)}): {menu_item.name} ({menu_item.price})")
            try:
                item = await add_menu_item_to_cart(stagehand, page, menu_item, result, idx)
            except Exception as error:
                item = {
                    "name": menu_item.name,
                    "shop_price_display": menu_item.price,
                    "shop_price": parse_price(menu_item.price),
                    "status": "skipped",
                    "error": str(error),
                }
                print(f"    Skipped: {error}")
            result.items.append(item)

        if not any(i["status"] == "added" for i in result.items):
            result.status, result.message = "add_to_cart_failed", "No menu items could be added to the order"
            return

        cart_state, checkout_state = await walk_to_checkout(stagehand, page, result)
        if checkout_state and checkout_state.is_checkout_page:
            result.status = "ok"
        elif cart_state and cart_state.is_cart_visible:
            result.status, result.message = "no_checkout", "Cart reached, but final checkout screen was not reached"
        else:
            result.status, result.message = "no_checkout", "Checkout screen not reached"

    except Blocked as error:
        result.status, result.message = "blocked", str(error)
        print(f"  Blocked: {result.message}")
    finally:
        result.comparison = compare_prices(result, cart_state, checkout_state)
        checkout_url = None
        with contextlib.suppress(Exception):
            checkout_url = await page.url()
        write_reports(result, address, query, store_url, checkout_url)

    c = result.comparison
    print(
        f"  Expected items: {c['expected_subtotal']} | Checkout subtotal: {c['checkout_subtotal_display']} "
        f"| Delivery: {c['shipping']} (adv: {c['advertised_delivery_fee_display']}) "
        f"| Service/Other fees: {c['other_fees_total']} | Total: {c['checkout_total_display']}"
    )
    print(f"  Drip pricing detected: {'YES - ' + '; '.join(c['drip_reasons']) if c['drip_detected'] else 'NO'}")


def update_summary(result: SiteResult) -> None:
    """Same columns as `airlines-virgin.py` and `coffee-shopify.py`, writing to `output/summary.csv`."""
    path = OUTPUT_ROOT / "summary.csv"
    c = result.comparison
    row = {
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "site": result.site,
        "status": result.status,
        "message": result.message,
        "items_added": c.get("items_added", 0),
        "expected_subtotal": c.get("expected_subtotal"),
        "checkout_subtotal": c.get("checkout_subtotal"),
        "checkout_total": c.get("checkout_total"),
        "currency": c.get("currency"),
        "other_fees_total": c.get("other_fees_total"),
        "shipping": c.get("shipping"),
        "drip_detected": c.get("drip_detected"),
        "output_dir": str(result.out_dir),
    }
    existing_rows: list[dict] = []
    if path.exists():
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for r in reader:
                if r.get("site") != result.site:
                    existing_rows.append(r)
    existing_rows.append(row)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(row))
        writer.writeheader()
        writer.writerows(existing_rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Uber Eats food-delivery drip-pricing POC")
    parser.add_argument(
        "urls",
        nargs="*",
        default=[SITE_URL],
        help=f"Uber Eats homepage or direct `/store/...` URL(s) (default: {SITE_URL})",
    )
    parser.add_argument(
        "--address",
        default=DEFAULT_ADDRESS,
        help=f"Delivery address to enter (default: '{DEFAULT_ADDRESS}')",
    )
    parser.add_argument(
        "--query",
        default=None,
        help="Optional search term when starting from the homepage (e.g. 'burgers', 'thai')",
    )
    parser.add_argument(
        "--items",
        "--products",
        dest="items",
        type=int,
        default=2,
        choices=range(1, 6),
        metavar="N",
        help="Number of menu items to add to the order (1-5, default 2)",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="Launch the persistent local Chrome profile on Uber Eats so you can log in manually, then exit",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Force local Chrome with `.chrome-profile` even if BROWSERBASE_API_KEY is set in .env",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    api_key = os.environ["GOOGLE_GENERATIVE_AI_API_KEY"]
    model = "google/" + os.getenv("GEMINI_MODEL", "gemini-3.8-flash").removeprefix("google/")

    history: list[dict] = []

    def record(log) -> None:
        msg = getattr(log, "message", "") or ""
        if any(phrase in msg for phrase in _NOISY_CDP_PHRASES):
            return
        entry = log.model_dump(mode="json")
        entry["timestamp"] = datetime.now(timezone.utc).isoformat()
        history.append(entry)

    bb_api_key, bb_project_id = os.getenv("BROWSERBASE_API_KEY"), os.getenv("BROWSERBASE_PROJECT_ID")
    bb_client: Browserbase | None = None
    bb_session_id: str | None = None
    use_browserbase = bool(bb_api_key and bb_project_id and not args.local and not args.login)

    if use_browserbase:
        bb_client = Browserbase(api_key=bb_api_key)
        bb_session_id = bb_client.sessions.create(project_id=bb_project_id).id
        browser = await stagehand_browserbase.connect(api_key=bb_api_key, session_id=bb_session_id)
    else:
        profile_dir = CHROME_PROFILE_DIR.resolve()
        profile_dir.mkdir(parents=True, exist_ok=True)
        headless = False if args.login else (os.getenv("HEADLESS") == "true")
        browser = await local_browser.launch(
            headless=headless,
            user_data_dir=str(profile_dir),
            viewport_width=1440,
            viewport_height=1000,
        )

    monitor = LocalResourceMonitor()
    monitor_task = monitor.start()
    try:
        stagehand = await Stagehand.create(
            browser=browser,
            model=model,
            model_api_key=api_key,
            system_prompt=SAFETY_PROMPT,
            logging=StagehandClientLoggingConfig(level="warn", format="json", on_log=record),
        )
        try:
            page = (await browser.context.pages())[0]

            if args.login:
                print(f"\n=== Preloading Uber Eats Login into {CHROME_PROFILE_DIR.resolve()} ===")
                await page.goto(SITE_URL)
                await settle(page, 2000)
                await interactive_login_pause(
                    page,
                    "Click 'Sign in' in the top-right corner of the browser window and log in to Uber Eats.",
                )
                print(f"  Login session saved to {CHROME_PROFILE_DIR.resolve()}")
                return

            for url in args.urls:
                url = url if url.startswith("http") else f"https://{url}"
                parsed = urlparse(url)
                domain = parsed.netloc.removeprefix("www.")
                store_slug = slug(parsed.path.split("/store/")[-1].split("/")[0]) if "/store/" in parsed.path else None
                folder_name = f"{domain}-{store_slug}" if store_slug else domain
                out_dir = OUTPUT_ROOT / folder_name / datetime.now().strftime("%Y%m%d-%H%M%S")
                out_dir.mkdir(parents=True, exist_ok=True)
                result = SiteResult(site=url, out_dir=out_dir)

                history.clear()
                start = perf_counter()
                before = await stagehand.metrics()

                try:
                    await run_site(stagehand, page, url, args.address, args.query, args.items, result)
                except Exception as error:
                    result.status, result.message = "error", f"{type(error).__name__}: {error}"
                    print(f"  ERROR: {result.message}")
                    with contextlib.suppress(Exception):
                        await page.screenshot(path=str(out_dir / "error.png"))

                after = before
                with contextlib.suppress(Exception):
                    after = await stagehand.metrics()

                session = {
                    "site": url,
                    "store_name": result.store_name,
                    "status": result.status,
                    "message": result.message,
                    "execution_time_ms": round((perf_counter() - start) * 1000),
                    "tokens_used": (after.total_prompt_tokens + after.total_completion_tokens)
                    - (before.total_prompt_tokens + before.total_completion_tokens),
                    "metrics_cumulative": after.model_dump(mode="json"),
                    "resource_usage": monitor.summary(),
                    "history": list(history),
                }
                if bb_client and bb_session_id:
                    info = bb_client.sessions.retrieve(bb_session_id)
                    session["browserbase_session"] = {"id": info.id, "status": info.status, "region": info.region}
                (out_dir / "session.json").write_text(json.dumps(session, indent=2), encoding="utf-8")
                update_summary(result)
                print(f"  Status: {result.status} {result.message} -> {out_dir}")
        finally:
            with contextlib.suppress(Exception):
                await stagehand.close()
    finally:
        monitor_task.cancel()
        with contextlib.suppress(Exception):
            await browser.close()

    if not args.login:
        print(f"\nSummary written to {OUTPUT_ROOT / 'summary.csv'}")


if __name__ == "__main__":
    asyncio.run(main())