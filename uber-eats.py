"""Uber Eats food-delivery drip-pricing & direct-menu markup POC.

1. Start on Uber Eats AU (or a direct `/store/...` URL), set the delivery address, and record the
   advertised delivery fee and service fee shown on the feed card and store banner.
2. Clear leftover cart items, then add 1-5 menu items (auto-selecting $0/cheapest required
   modifiers and declining paid add-ons).
3. Walk through the cart drawer to the final checkout screen and extract every fee.
4. Visit the restaurant's own website (found by search, or `--restaurant-url`) and walk it until
   item prices are on screen, in this order:
       Menu  ->  Order online  ->  Pickup  ->  select the nearest store  ->  read prices
   then compare the restaurant's direct unit prices against the Uber Eats prices.

Usage:
    python uber-eats.py --login            # preload a login into `.chrome-profile` once
    python uber-eats.py --address "1 Jeffcott Place, Canberra ACT 2615" --query "pizza" --items 2
    python uber-eats.py https://www.ubereats.com/au/store/... --restaurant-url https://...
"""

import os
import sys

os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

import argparse
import asyncio
import base64
import contextlib
import csv
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

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
CHECKOUT_URL = f"{SITE_URL}/checkout"
DEFAULT_ADDRESS = os.getenv("UBEREATS_ADDRESS", "260 City Walk, Canberra ACT 2601")
SETTLE_MS = 2000
MAX_ADDRESS_STEPS = 12
MAX_MODIFIER_STEPS = 6
MAX_CHECKOUT_STEPS = 10
OUTPUT_ROOT = Path("output")
CHROME_PROFILE_DIR = Path(".chrome-profile")

# Direct restaurant website walk
MAX_DIRECT_SITES = 2          # search results to try before giving up
MAX_DIRECT_SITE_STEPS = 8     # navigation steps per site
MAX_DIRECT_LLM_NAV = 3        # LLM-driven clicks per site once DOM navigation runs dry
MAX_CATEGORY_HOPS = 2         # "open the category containing X" attempts per site
MIN_PRICE_SIGNALS = 4         # price-looking strings on a page before it is worth an LLM extract
DIRECT_KIND_ORDER = ("menu", "order", "pickup", "store", "frame")
DIRECT_KIND_BUDGET = {"menu": 2, "order": 2, "pickup": 2, "store": 2, "frame": 1}

NON_RESTAURANT_NAMES = (
    "coles", "woolworths", "aldi", "iga", "costco", "7-eleven", "7 eleven",
    "liquorland", "bws", "dan murphy", "first choice liquor", "vintage cellars",
    "chemist warehouse", "priceline", "ampol", "bp ", "caltex", "officeworks",
)

# Never treated as "the restaurant's own site": delivery platforms, search engines, social, review/listing sites.
EXCLUDED_SEARCH_DOMAINS = (
    "ubereats.com", "uber.com", "doordash.com", "menulog.com", "deliveroo.", "hungrypanda.", "easi.",
    "google.", "bing.com", "duckduckgo.com", "yahoo.com", "facebook.com", "instagram.com", "tiktok.com",
    "x.com", "twitter.com", "pinterest.", "youtube.com", "linkedin.com", "reddit.com", "wikipedia.org",
    "apple.com", "tripadvisor.", "yelp.", "zomato.com", "restaurantguru.", "foursquare.com",
    "opentable.", "thefork.", "quandoo.", "agfg.com.au", "broadsheet.com.au", "timeout.com",
    "yellowpages.", "whitepages.", "truelocal.", "localsearch.", "eatability.",
)

STREET_TYPES = {
    "st", "street", "pl", "place", "rd", "road", "ave", "avenue", "dr", "drive", "ct", "court",
    "cct", "circuit", "ln", "lane", "way", "blvd", "boulevard", "pde", "parade", "tce", "terrace",
    "cl", "close", "cr", "cres", "crescent", "hwy", "highway", "walk", "grove", "gr",
}
NON_SUBURB_WORDS = STREET_TYPES | {
    "act", "nsw", "vic", "qld", "sa", "wa", "tas", "nt", "australia", "shop", "unit", "level", "the", "and",
}

SAFETY_PROMPT = (
    "You are auditing a food-delivery ordering flow for price transparency. Never click a button that places, "
    "pays for, or submits an order (e.g. 'Place order', 'Pay', 'Submit order', 'Start Uber One trial'). "
    "Never enter payment card details, gift cards, or promo codes."
)

EXTRA_CATEGORIES = (
    "delivery_fee, service_fee, small_order_fee, priority_delivery, express_delivery, "
    "uber_one_membership, item_modifier_paid, checkout_upsell, tip, bag_fee, "
    "payment_surcharge, other"
)


# ---------------------------------------------------------------------------
# Logging & stream noise filter
# ---------------------------------------------------------------------------
_NOISY_CDP_PHRASES = (
    "CDP response failed",
    "Frame with the given id was not found",
    "Cannot find context with specified id",
    "Session with given id not found",
)


def _is_noise(text: str) -> bool:
    return any(phrase in text for phrase in _NOISY_CDP_PHRASES)


class _CdpNoiseFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return not _is_noise(record.getMessage())


for _logger_name in ("", "stagehand", "stagehand.cdp_client", "websockets"):
    logging.getLogger(_logger_name).addFilter(_CdpNoiseFilter())


class _FilteredStream:
    def __init__(self, stream):
        self._stream = stream

    def write(self, text: str) -> int:
        return len(text) if _is_noise(text) else self._stream.write(text)

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


sys.stdout = _FilteredStream(sys.stdout)
sys.stderr = _FilteredStream(sys.stderr)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class AddressAndFeedState(BaseModel):
    displayed_address: str | None = Field(
        description="Exact delivery address text currently shown in the top header location pill (e.g. '240 City Walk', '1 Jeffcott Pl'), or null if it says 'Enter delivery address' or is not set"
    )
    address_modal_or_input_open: bool = Field(
        description="True if the 'Deliver to' / address input dialog or text field is currently open on screen"
    )
    address_suggestions_open: bool = Field(
        description="True if an autocomplete dropdown list of address suggestions is currently visible below the address input"
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
    store_address_or_suburb: str | None = Field(
        description="Restaurant street address or suburb shown on the store page, if any"
    )
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
    delivery_fee: str | None = Field(
        description="Standard delivery fee shown in the fare breakdown, e.g. '$3.99', '$0.00'; null if not yet shown"
    )
    priority_delivery_fee: str | None = Field(
        description="Extra fee advertised for 'Priority' / faster delivery option if shown, e.g. '+$2.99'; otherwise null"
    )
    service_fee: str | None = Field(description="Service fee shown in the fare breakdown, e.g. '$4.00'; null if not yet shown")
    small_order_fee: str | None = Field(description="Small order / minimum basket fee if shown; otherwise null")
    other_fees: list[Fee] = Field(
        description="Any other fees or charges shown (e.g. bag fee, delivery adjustment, regulatory fee, tip) excluding Subtotal, Delivery Fee, Service Fee and Small Order Fee"
    )
    tax_amount: str | None = Field(description="Estimated taxes/GST if itemised separately")
    tax_included: bool = Field(description="True if GST/tax is included in prices")
    total: str | None = Field(description="Total amount due (or cart subtotal if full total not yet shown)")
    currency: str | None = Field(description="Currency code if shown, e.g. 'AUD'")
    offers: list[Offer] = Field(
        description="Every optional upsell, delivery speed tier (Priority vs Standard), Uber One prompt, tip preset, or fee visible on this screen"
    )
    summary: str


class DirectMenuMatch(BaseModel):
    uber_item_name: str = Field(description="The requested Uber Eats item name, copied exactly as given")
    direct_item_name: str | None = Field(
        description="Name of the same dish as written on this restaurant website; null if it is not on this page"
    )
    direct_price_display: str | None = Field(
        description="Unit price this website shows for that dish, e.g. '$19.00' or '20'; null if not found or no price listed"
    )


class DirectSiteExtraction(BaseModel):
    page_state: str = Field(
        description="One of: menu_with_prices (menu items with prices are listed), menu_without_prices, "
        "order_type_choice (must pick pickup vs delivery), store_selection (must pick a store/location), "
        "blocked (login, delivery address, or CAPTCHA required), other"
    )
    matches: list[DirectMenuMatch] = Field(description="One entry per requested Uber Eats item, in the order given")
    summary: str


# ---------------------------------------------------------------------------
# Pure helpers
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
    try:
        return float(number.rstrip("."))
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


def pct_diff(higher: float | None, base: float | None) -> float | None:
    if higher is None or base is None or base <= 0:
        return None
    return round(((higher - base) / base) * 100.0, 1)


def pct_of(amount: float | None, base: float | None) -> float | None:
    return round((amount / base) * 100.0, 1) if amount and base else None


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "step"


def norm_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def best_match(name: str, candidates: list[str], threshold: float = 0.4) -> int | None:
    """Index of the candidate most similar to `name` (exact > containment > token Jaccard), or None."""
    target = norm_name(name)
    target_tokens = set(target.split())
    best_idx, best_score = None, 0.0
    for idx, cand in enumerate(candidates):
        c = norm_name(cand)
        c_tokens = set(c.split())
        if not target or not c:
            score = 0.0
        elif c == target:
            score = 1.0
        elif target in c or c in target:
            score = 0.8
        else:
            score = len(target_tokens & c_tokens) / len(target_tokens | c_tokens)
        if score > best_score:
            best_idx, best_score = idx, score
    return best_idx if best_score >= threshold else None


def clean_store_search_name(name: str | None) -> str:
    """Strip suburb suffixes in parentheses e.g. 'BZ Burgers (Belconnen)' -> 'BZ Burgers'."""
    if not name:
        return ""
    return re.sub(r"\s*\([^)]*\)\s*", " ", name).strip() or name.strip()


def extract_suburb_hint(name: str | None) -> str | None:
    """Extract a suburb hint like 'Braddon' from 'Grease Monkey (BRADDON)'."""
    m = re.search(r"\(([^)]+)\)", name or "")
    return m.group(1).strip() if m else None


def location_tokens(store_name: str | None, *addresses: str | None) -> list[str]:
    """Suburb/city words, most specific first, used to pick the right branch on the restaurant's site.

    ('Grease Monkey (Braddon)', '1 Lonsdale St, Braddon ACT 2612', '260 City Walk, Canberra ACT 2601')
    -> ['braddon', 'canberra']
    """
    sources = [extract_suburb_hint(store_name)]
    for addr in addresses:
        if addr:
            sources.append(addr.split(",", 1)[1] if "," in addr else addr)  # drop the street part
    tokens: list[str] = []
    for src in sources:
        for word in re.findall(r"[a-z]{3,}", (src or "").lower()):
            if word not in NON_SUBURB_WORDS and word not in tokens:
                tokens.append(word)
    return tokens


def is_valid_restaurant_name(name: str | None) -> bool:
    return bool(name) and not any(bad in name.lower().strip() for bad in NON_RESTAURANT_NAMES)


def is_login_url(url: str) -> bool:
    low = url.lower()
    return any(w in low for w in ("auth.uber.com", "login.uber.com", "/login-redirect", "/login"))


def _extract_street_tokens(address: str) -> list[str]:
    """Extract normalized primary street tokens (e.g. '1 Jeffcott Place, Canberra' -> ['1', 'jeffcott'])."""
    words = re.findall(r"[a-z0-9]+", address.split(",")[0].lower())
    return ([t for t in words if t not in STREET_TYPES] or words)[:2]


def addresses_match(displayed: str | None, target: str) -> bool:
    """Return True only if `displayed` contains the key street number/name tokens of `target`."""
    if not displayed:
        return False
    disp_low = displayed.lower()
    if "enter delivery address" in disp_low or disp_low.strip() == "deliver now":
        return False
    req_tokens = _extract_street_tokens(target)
    disp_tokens = set(re.findall(r"[a-z0-9]+", disp_low))
    return bool(req_tokens) and all(tok in disp_tokens for tok in req_tokens)


def extract_pl_address_from_url(url: str) -> str | None:
    """Decode the `pl=` base64/URL-encoded JSON parameter on Uber Eats URLs to check the active address."""
    with contextlib.suppress(Exception):
        pl_val = parse_qs(urlparse(url).query).get("pl", [None])[0]
        if not pl_val:
            return None
        decoded = unquote(pl_val)
        if not decoded.strip().startswith("{"):
            padded = pl_val + "=" * (-len(pl_val) % 4)
            decoded = unquote(base64.b64decode(padded).decode("utf-8", errors="ignore"))
        data = json.loads(decoded)
        return data.get("address") or data.get("title")
    return None


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


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
    store_location: str | None = None
    direct_website_url: str | None = None
    advertised_delivery_fee_display: str | None = None
    advertised_delivery_fee: float | None = None
    advertised_service_fee_display: str | None = None
    items: list[dict] = field(default_factory=list)
    extras: list[dict] = field(default_factory=list)
    fee_breakdown_rows: list[dict] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    comparison: dict = field(default_factory=dict)

    @property
    def added(self) -> list[dict]:
        return [i for i in self.items if i.get("status") == "added"]


class Blocked(RuntimeError):
    """The site presented a mandatory login wall, CAPTCHA, or bot check."""


# ---------------------------------------------------------------------------
# Page helpers. All DOM work goes through `js()` (page.evaluate only - no page.keyboard).
# ---------------------------------------------------------------------------
JS_LIB = r"""
const vis = (el, w = 8, h = 8) => { const r = el.getBoundingClientRect(); return r.width > w && r.height > h; };
const txt = el => (el.innerText || '').trim();
const clean = s => (s || '').replace(/\s+/g, ' ').trim();
const dialogs = () => Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"]'));
const pressEsc = () => {
    for (const t of [document, document.body]) {
        t.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', code: 'Escape', keyCode: 27, bubbles: true }));
    }
};
const closeDialog = () => {
    for (const d of dialogs()) {
        const b = d.querySelector('button[aria-label*="close" i]');
        if (b) { b.click(); return true; }
    }
    return false;
};
// Uber Eats
const addButton = () => Array.from(document.querySelectorAll('button')).find(
    b => /^(Add\s+\d*\s*to\s+(order|cart)|Update\s+order)/i.test(txt(b)) && vis(b, 80, 25));
const cartButtons = () => Array.from(document.querySelectorAll(
    '[data-testid*="cart" i], button[aria-label*="cart" i], button[aria-label*="bag" i]'));
"""


async def js(page: Page, fn: str, arg=None, default=None):
    """Run the JS arrow function `fn(arg)` in the page with JS_LIB in scope; `default` on failure or null."""
    try:
        res = await page.evaluate(f"(() => {{ {JS_LIB}\nreturn ({fn})({json.dumps(arg)}); }})()")
    except Exception:
        return default
    return default if res is None else res


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


async def interactive_login_pause(page: Page, prompt_msg: str) -> bool:
    """Pause in headed mode so the user can sign in inside the persistent browser window."""
    if os.getenv("HEADLESS") == "true" or not sys.stdin.isatty():
        return False
    print(f"\n  [LOGIN REQUIRED] {prompt_msg}")
    print("  1. Complete the Uber Eats sign-in (and any SMS/email OTP) in the open browser window.")
    print("  2. Wait until the browser redirects back to Uber Eats.")
    await asyncio.to_thread(input, "  3. Press Enter here to continue... ")
    await settle(page, 2500)
    return not is_login_url(await page.url())


JS_HAS_OVERLAY = r"""() => {
    const sel = '[role="dialog"], [aria-modal="true"], [class*="cookie" i], [id*="cookie" i], [class*="promo" i], [class*="banner" i]';
    for (const el of document.querySelectorAll(sel)) {
        const s = getComputedStyle(el);
        if (s.display !== 'none' && s.visibility !== 'hidden' && parseFloat(s.opacity || '1') > 0.1 && vis(el, 120, 50)) return true;
    }
    return false;
}"""


async def maybe_dismiss_popups(stagehand: Stagehand, page: Page) -> None:
    """Close cookie banners / promo popups / app-install banners; the LLM is only invoked if the DOM shows an overlay."""
    if not await js(page, JS_HAS_OVERLAY, default=False):
        return
    with contextlib.suppress(Exception):
        found = await stagehand.observe(
            "Find the close, dismiss, 'No thanks', 'Maybe later', 'Skip', or accept-cookies button of any visible "
            "cookie banner, promotional popup, Uber One trial offer, or app-install banner. "
            "Do NOT click 'Sign in' or 'Continue' on an order. Return nothing if there is none."
        )
        for action in found.data[:2]:
            print(f"  Dismissing popup: {action.description}")
            await stagehand.act(action)


# ---------------------------------------------------------------------------
# Uber Eats: DOM snippets
# ---------------------------------------------------------------------------
JS_SET_ADDRESS = r"""({ target, tokens }) => {
    const hasTokens = s => tokens.length > 0 && tokens.every(t => s.toLowerCase().includes(t));

    // 1. An autocomplete suggestion matching our street tokens
    for (const sel of ['[role="listbox"] [role="option"]', '[role="listbox"] li', 'ul[role="listbox"] > *',
                       '[data-testid*="address" i] li', '[role="dialog"] ul li']) {
        for (const el of document.querySelectorAll(sel)) {
            if (vis(el, 80, 20) && hasTokens(txt(el))) {
                el.click();
                return 'Clicked autocomplete suggestion: ' + txt(el).split('\n')[0];
            }
        }
    }

    // 2. A visible address input: clear stale text and fill it
    for (const inp of document.querySelectorAll(
        'input[placeholder*="address" i], input[aria-label*="address" i], input[placeholder*="Deliver" i], [role="dialog"] input[type="text"], [role="dialog"] input:not([type])')) {
        if (!vis(inp, 100, 20)) continue;
        if (inp.value && !hasTokens(inp.value)) {
            const box = inp.closest('[role="dialog"], [aria-modal="true"]') || inp.parentElement?.parentElement;
            box?.querySelector('button[aria-label*="clear" i], button[aria-label*="reset" i]')?.click();
        }
        inp.focus();
        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
        if (setter) setter.call(inp, target); else inp.value = target;
        inp.dispatchEvent(new Event('input', { bubbles: true }));
        inp.dispatchEvent(new Event('change', { bubbles: true }));
        return 'Typed target address into input: ' + target;
    }

    // 3. The header delivery-address button, to open the address modal
    for (const btn of document.querySelectorAll(
        'header button, header a, [data-testid*="location" i], [data-testid*="address" i], button[aria-label*="deliver" i]')) {
        const label = (txt(btn) + ' ' + (btn.getAttribute('aria-label') || '')).trim();
        if (vis(btn, 50, 20) && /deliver|address|now|•|\d+\s+[a-z]/i.test(label) && !/cart|bag|sign\s*in|log\s*in|search/i.test(label)) {
            btn.click();
            return 'Clicked header address button: ' + txt(btn).split('\n')[0];
        }
    }
    return null;
}"""

JS_CART_COUNT = r"""() => {
    const els = [...cartButtons(), ...document.querySelectorAll('header button')];
    for (const el of els) {
        const m = ((el.getAttribute('aria-label') || '') + ' ' + txt(el)).match(/(\d+)\s*item/i);
        if (m) return parseInt(m[1], 10);
    }
    for (const el of els) if (/^\d+$/.test(txt(el))) return parseInt(txt(el), 10);
    return 0;
}"""

JS_OPEN_CART = r"""() => {
    const btn = cartButtons().find(el => vis(el, 20, 20))
        || Array.from(document.querySelectorAll('header button')).find(
            b => /\d+\s*item/i.test(txt(b) + ' ' + (b.getAttribute('aria-label') || '')));
    if (btn) btn.click();
    return !!btn;
}"""

JS_CLEAR_CART_STEP = r"""() => {
    // Scope strictly to the open cart drawer so page/header buttons are never clicked
    const drawer = [...dialogs(), ...document.querySelectorAll('aside')].find(
        d => vis(d, 220, 250) && /checkout|subtotal|clear cart|your order/i.test(d.innerText || ''));
    if (!drawer) return null;
    const buttons = Array.from(drawer.querySelectorAll('button'));
    const aria = b => (b.getAttribute('aria-label') || '').trim();

    const clear = buttons.find(b => /^(Clear cart|Empty cart|Remove all|Clear)/i.test(txt(b)) || /^clear cart$/i.test(aria(b)));
    if (clear) { clear.click(); return 'clear_button'; }

    for (const sel of drawer.querySelectorAll('select')) {
        const zero = Array.from(sel.options).find(o => o.value === '0' || /^remove$/i.test((o.text || '').trim()));
        if (zero) {
            sel.value = zero.value;
            sel.dispatchEvent(new Event('change', { bubbles: true }));
            return 'select_zero';
        }
    }

    const remove = buttons.find(b => /^(Remove item|Delete item|Decrease quantity)/i.test(aria(b)));
    if (remove) { remove.click(); return 'remove_item_btn'; }
    return null;
}"""

JS_SELECT_REQUIRED = r"""() => {
    const chosen = [];
    const root = dialogs().find(d => d.getBoundingClientRect().height > 200) || document.body;
    const groups = new Map();
    for (const r of root.querySelectorAll('input[type="radio"]')) {
        const key = r.name || r.closest('[role="radiogroup"]')?.id || '';
        if (!key) continue;
        if (!groups.has(key)) groups.set(key, []);
        groups.get(key).push(r);
    }
    for (const list of groups.values()) {
        if (list.some(r => r.checked)) continue;
        const containerText = list[0].closest('div')?.parentElement?.innerText || '';
        if (groups.size > 1 && /most popular|#1 ordered/i.test(containerText)) continue;

        // First option without a '+$' surcharge, else the first option
        let best = list[0], bestLabel = '';
        for (const r of list) {
            const row = r.closest('label') || r.parentElement?.parentElement || r.parentElement;
            const label = txt(row || r).split('\n')[0].trim();
            if (!/\+\s*(A?\$|AUD)/i.test(txt(row || r))) { best = r; bestLabel = label; break; }
            if (!bestLabel) bestLabel = label;
        }
        const target = best.closest('label') || best;
        target.scrollIntoView({ block: 'center', behavior: 'instant' });
        target.click();
        if (bestLabel) chosen.push(bestLabel);
    }
    return chosen;
}"""

JS_CLICK_ADD = r"""() => {
    const btn = addButton();
    if (!btn) return { clicked: false, price: null };
    const m = txt(btn).match(/(A?\$[\d.,]+)/);
    btn.click();
    return { clicked: true, price: m ? m[1] : null };
}"""

JS_CLICK_CHECKOUT = r"""() => {
    const el = Array.from(document.querySelectorAll('a[href*="/checkout"], button')).find(
        e => /^(Go to checkout|Checkout|Continue to checkout)/i.test(txt(e)));
    if (el) el.click();
    return !!el;
}"""

JS_DECLINE_UPSELL = r"""() => {
    // Only buttons inside an open dialog, and only ones that decline / move on (never add, never place the order)
    const DECLINE = /^(skip|no,? thanks?|not now|maybe later|continue( to checkout)?|go to checkout|next|done)$/i;
    for (const d of dialogs().filter(d => vis(d, 200, 150)).reverse()) {
        const btn = Array.from(d.querySelectorAll('button, a')).find(b => vis(b, 30, 20) && DECLINE.test(clean(txt(b))));
        if (btn) { btn.click(); return clean(txt(btn)); }
    }
    return null;
}"""

JS_FIND_STORE = r"""({ preferred, excluded }) => {
    const norm = s => (s || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
    const pref = norm(preferred);
    const found = [];
    for (const a of document.querySelectorAll('a[href*="/store/"]')) {
        const heading = a.querySelector('h1, h2, h3, h4, [data-testid*="store" i]') || a;
        const name = (heading.innerText || a.getAttribute('aria-label') || a.innerText || '').split('\n')[0].trim();
        const low = name.toLowerCase();
        if (!name || !vis(a, 40, 20)) continue;
        if (excluded.some(bad => low.includes(bad) || a.href.toLowerCase().includes('/store/' + bad))) continue;
        found.push({ name, url: a.href, match: !!pref && norm(name).includes(pref) });
    }
    return found.find(c => c.match) || found[0] || null;
}"""


async def is_item_modal_open(page: Page) -> bool:
    return bool(await js(page, "() => !!addButton()", default=False))


async def get_cart_count(page: Page) -> int:
    count = await js(page, JS_CART_COUNT, default=0)
    return count if isinstance(count, int) else 0


async def open_cart_drawer(page: Page) -> bool:
    return bool(await js(page, JS_OPEN_CART, default=False))


async def close_item_modal(stagehand: Stagehand, page: Page) -> None:
    """Ensure no lingering item modal blocks the rest of the page."""
    if not await is_item_modal_open(page):
        return
    print("    Closing lingering item modal...")
    await js(page, "() => { pressEsc(); closeDialog(); }")
    await page.wait_for_timeout(1000)
    if await is_item_modal_open(page):
        with contextlib.suppress(Exception):
            await stagehand.act("Click the 'X' close button in the top-left corner of the open item modal.")
            await page.wait_for_timeout(1000)


async def clear_existing_cart(stagehand: Stagehand, page: Page) -> None:
    """If the persistent profile has leftover items in the cart, clear them inside the cart drawer."""
    existing = await get_cart_count(page)
    if existing <= 0:
        return
    print(f"  Detected {existing} leftover item(s) in cart from a previous session; clearing cart...")
    if not await open_cart_drawer(page):
        return
    await settle(page, 1800)

    llm_clears = 0
    for _ in range(12):
        if await get_cart_count(page) == 0:
            break
        if not await js(page, JS_CLEAR_CART_STEP):
            llm_clears += 1
            if llm_clears > 3:
                break
            with contextlib.suppress(Exception):
                await stagehand.act(
                    "Inside the open cart drawer, click 'Clear cart' (or remove one item from the cart, confirming "
                    "any 'Remove' prompt). Do NOT click 'Go to checkout'."
                )
        await page.wait_for_timeout(1000)

    await js(page, "() => { closeDialog() || pressEsc(); }")
    await settle(page, 1200)
    left = await get_cart_count(page)
    if left:
        print(f"  WARNING: {left} leftover item(s) could not be removed; the checkout subtotal will include them.")


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
# Uber Eats: flow
# ---------------------------------------------------------------------------
async def ensure_address_and_open_store(
    stagehand: Stagehand,
    page: Page,
    address: str,
    search_query: str | None,
    result: SiteResult,
) -> str:
    """Ensure the active delivery address matches `address` (updating if stale), run search, and open a `/store/...` page."""
    question = (
        "Look at the top header bar and any open address modal. Report the exact delivery address text currently shown "
        "in the header (`displayed_address`), whether an address input dialog is open (`address_modal_or_input_open`), "
        "whether autocomplete suggestions are visible (`address_suggestions_open`), and any `blocked_reason`."
    )
    address_args = {"target": address, "tokens": _extract_street_tokens(address)}
    query_submitted = False
    address_verified = False

    for step_idx in range(MAX_ADDRESS_STEPS):
        curr_url = await page.url()
        if "login.microsoftonline.com" in curr_url or "login.windows.net" in curr_url:
            print("  Waiting for corporate SSO redirect...")
            await page.wait_for_timeout(3000)
            continue

        if is_login_url(curr_url):
            if not await interactive_login_pause(page, f"Uber login wall detected ({curr_url})."):
                raise Blocked(f"Redirected to Uber login wall ({curr_url})")
            curr_url = await page.url()

        if not address_verified:
            state = (await stagehand.extract(question, AddressAndFeedState)).data
            if state.blocked_reason:
                raise Blocked(state.blocked_reason)

            pl_addr = extract_pl_address_from_url(curr_url)
            if addresses_match(state.displayed_address, address) or addresses_match(pl_addr, address):
                print(f"  Delivery address confirmed: {state.displayed_address or pl_addr}")
                address_verified = True
            else:
                if state.displayed_address and not state.address_modal_or_input_open:
                    print(
                        f"  Header shows previous address '{state.displayed_address}' "
                        f"(expected '{address}'); updating delivery address..."
                    )
                dom_step = await js(page, JS_SET_ADDRESS, address_args)
                if dom_step:
                    print(f"  Address step {step_idx + 1} (DOM): {dom_step}")
                else:
                    if state.address_suggestions_open:
                        instruction = f"Click the first autocomplete address suggestion matching '{address}' (or click 'Done' / 'Save')."
                    elif state.address_modal_or_input_open:
                        instruction = f"Clear any existing text in the delivery address input box and type '{address}'."
                    else:
                        instruction = (
                            f"Click the current delivery address button ('{state.displayed_address or 'Enter delivery address'}') "
                            f"in the top header so we can change the delivery address to '{address}'."
                        )
                    act_res = await stagehand.act(instruction)
                    print(f"  Address step {step_idx + 1}: {act_res.data.action_description}")
                await settle(page, 2200)
                continue

        if "/store/" in curr_url:
            return curr_url

        if search_query and not query_submitted and "q=" not in curr_url.lower():
            search_url = f"{SITE_URL}/search?q={quote_plus(search_query)}"
            print(f"  Navigating to search results: {search_url}")
            await page.goto(search_url)
            query_submitted = True
            await settle(page, 3000)

        feed = (
            await stagehand.extract(
                "Extract the prepared-food restaurants visible on this page with their advertised delivery fee, "
                "service fee, ETA, availability, and `/store/...` link. "
                "Exclude supermarkets, grocery stores, convenience stores, and liquor stores (e.g. Coles, Woolworths, 7-Eleven, BWS).",
                StoreFeed,
            )
        ).data

        named = [s for s in feed.stores if is_valid_restaurant_name(s.name)]
        chosen = next((s for s in named if s.available and s.is_restaurant), named[0] if named else None)
        if chosen:
            result.store_name = chosen.name
            result.store_location = result.store_location or extract_suburb_hint(chosen.name)
            result.advertised_delivery_fee_display = chosen.delivery_fee_display
            result.advertised_delivery_fee = parse_price(chosen.delivery_fee_display)
            result.advertised_service_fee_display = chosen.service_fee_display

        target_url: str | None = None
        if chosen and chosen.url and "/store/" in chosen.url:
            target_url = urljoin("https://www.ubereats.com", chosen.url)
        else:
            dom_store = await js(
                page, JS_FIND_STORE,
                {"preferred": chosen.name if chosen else "", "excluded": list(NON_RESTAURANT_NAMES)},
            )
            if dom_store:
                target_url = dom_store["url"]
                result.store_name = result.store_name or dom_store["name"]

        if target_url:
            print(
                f"  Picking store: {result.store_name or target_url} "
                f"(Advertised delivery: {result.advertised_delivery_fee_display or 'not shown'})"
            )
            await page.goto(target_url)
            await settle(page, 2800)
        else:
            await stagehand.act(
                f"Click on the restaurant card '{chosen.name}' to open its menu page."
                if chosen
                else "Scroll down and click on the first open restaurant card (not a grocery store)."
            )
            await settle(page, 2500)
        if "/store/" in await page.url():
            return await page.url()

    raise RuntimeError(f"Could not open a `/store/` restaurant page (ended on {await page.url()})")


def choose_menu_items(items: list[MenuItem], n: int) -> list[MenuItem]:
    return [i for i in items if i.is_food_or_drink and not i.sold_out and (parse_price(i.price) or 0) > 3.0][:n]


def new_item(menu_item: MenuItem, **overrides) -> dict:
    return {
        "name": menu_item.name,
        "category": menu_item.category,
        "shop_price_display": menu_item.price,
        "shop_price": parse_price(menu_item.price),
        "selected_variant": None,
        "selected_price_display": None,
        "selected_price": None,
        "direct_item_name": None,
        "direct_price_display": None,
        "direct_price": None,
        "uber_vs_direct_diff": None,
        "uber_vs_direct_markup_pct": None,
        "status": "skipped",
        **overrides,
    }


async def add_menu_item_to_cart(
    stagehand: Stagehand,
    page: Page,
    menu_item: MenuItem,
    result: SiteResult,
    item_idx: int,
) -> dict:
    item = new_item(menu_item)

    def set_price(display: str | None) -> None:
        item["selected_price_display"] = display
        item["selected_price"] = parse_price(display) or item["shop_price"]

    await close_item_modal(stagehand, page)
    before_cart_count = await get_cart_count(page)

    try:
        step = await stagehand.act(
            f"Click on the menu item '{menu_item.name}' (priced around {menu_item.price}) to open its customization modal."
        )
        print(f"    Open item: {step.data.action_description}")
        await settle(page, 1800)

        if not await is_item_modal_open(page):
            if await get_cart_count(page) > before_cart_count:  # quick-add without a modal
                set_price(menu_item.price)
                item["status"] = "added"
                return item
            retry = await stagehand.act(
                f"Scroll to and click directly on the menu item title '{menu_item.name}' to open its popup."
            )
            print(f"    Retry open item: {retry.data.action_description}")
            await settle(page, 1800)
            if not await is_item_modal_open(page):
                print(f"    Modal did not open for '{menu_item.name}', skipping.")
                return item

        selected_modifiers: list[str] = []
        for _ in range(MAX_MODIFIER_STEPS):
            if not await is_item_modal_open(page):
                item["status"] = "added"
                break

            dom_picked = [str(x) for x in await js(page, JS_SELECT_REQUIRED, default=[]) if x]
            if dom_picked:
                print(f"    Auto-selected required option(s): {', '.join(dom_picked)}")
                selected_modifiers.extend(dom_picked)
                await page.wait_for_timeout(800)

            modal_state = (
                await stagehand.extract(
                    f"We are inside the item modal for '{menu_item.name}' (menu price {menu_item.price}). "
                    "Check if any section marked 'Required' (or 'Choose 1') still has NO option selected. "
                    "Also extract the price on the bottom 'Add to order' button and up to 10 paid modifier options.",
                    ItemModalState,
                )
            ).data
            record_offers(result, item_idx, "item_modal", menu_item.name, modal_state.paid_modifier_offers)
            if modal_state.button_price_display:
                set_price(modal_state.button_price_display)

            if modal_state.has_unmet_required_choices and modal_state.next_required_group:
                cheapest = modal_state.cheapest_option_in_required_group
                act_mod = await stagehand.act(
                    f"In the required section '{modal_state.next_required_group}', "
                    f"select '{cheapest or 'the first $0 / cheapest option'}'."
                )
                print(f"    Modifier ({modal_state.next_required_group}): {act_mod.data.action_description}")
                if cheapest:
                    selected_modifiers.append(cheapest)
                await page.wait_for_timeout(900)
                continue

            add = await js(page, JS_CLICK_ADD, default={"clicked": False, "price": None})
            if add["price"] and not item["selected_price_display"]:
                set_price(add["price"])
            if add["clicked"]:
                print(f"    Clicked Add to order button ({item['selected_price_display'] or menu_item.price})")
            else:
                add_act = await stagehand.act(
                    "Click the black 'Add 1 to order' / 'Add to order' button at the bottom of the item modal."
                )
                print(f"    Click Add: {add_act.data.action_description}")
            await settle(page, 1800)

            if not await is_item_modal_open(page) or await get_cart_count(page) > before_cart_count:
                item["status"] = "added"
                break
            print("    Modal remained open after clicking Add; checking newly scrolled required section...")

        if item["selected_price"] is None:
            set_price(menu_item.price)
        item["selected_variant"] = ", ".join(dict.fromkeys(selected_modifiers)) or None
        if item["status"] != "added":
            item["status"] = "add_failed"
        return item
    finally:
        await close_item_modal(stagehand, page)


async def walk_to_checkout(
    stagehand: Stagehand,
    page: Page,
    result: SiteResult,
) -> tuple[CartAndCheckoutState | None, CartAndCheckoutState | None]:
    """Open the cart drawer, decline interstitial upsells, and proceed to the checkout screen."""
    out = result.out_dir
    cart_snapshot: CartAndCheckoutState | None = None
    upsell_declines = 0

    async def login_or_block(reason: str) -> None:
        if not await interactive_login_pause(page, reason):
            raise Blocked(reason)
        if "/checkout" not in await page.url():
            print("  Navigating to /au/checkout after login...")
            await page.goto(CHECKOUT_URL)
            await settle(page, 3000)

    await close_item_modal(stagehand, page)
    if await open_cart_drawer(page):
        print("  Opened cart drawer via header cart button")
        await settle(page, 2000)

    for step_idx in range(1, MAX_CHECKOUT_STEPS + 1):
        curr_url = await page.url()
        if is_login_url(curr_url):
            await shot(page, out / f"step-{step_idx:02d}-login-wall.png")
            await login_or_block(f"Uber login wall reached before checkout ({curr_url}).")
            curr_url = await page.url()

        state = (
            await stagehand.extract(
                "Examine the current screen in the Uber Eats ordering flow. "
                "IMPORTANT: `is_cart_visible` is True ONLY if the side cart drawer/panel is open showing line items "
                "and a 'Go to checkout' button (NOT just the small green cart icon in the top header). "
                "Extract all visible line items, subtotal, standard delivery fee, priority delivery fee (if shown), "
                "service fee, small order fee, any other fees, total, and every optional upsell, Uber One offer, or tip.",
                CartAndCheckoutState,
            )
        ).data

        step_type = "checkout" if state.is_checkout_page else state.step_type
        print(
            f"  Step {step_idx}: {step_type} | subtotal={state.subtotal} | "
            f"delivery={state.delivery_fee} | service={state.service_fee} | "
            f"small_order={state.small_order_fee} | total={state.total}"
        )
        await shot(page, out / f"step-{step_idx:02d}-{slug(step_type)}.png")
        record_offers(result, step_idx, step_type, step_type, state.offers)
        result.steps.append({
            "step": step_idx,
            "step_type": step_type,
            "url": curr_url,
            "subtotal_display": state.subtotal,
            "delivery_fee_display": state.delivery_fee,
            "priority_delivery_fee_display": state.priority_delivery_fee,
            "service_fee_display": state.service_fee,
            "small_order_fee_display": state.small_order_fee,
            "total_display": state.total,
            "total": parse_price(state.total),
            "offers": len(state.offers),
            "summary": state.summary,
        })

        if await is_item_modal_open(page):
            await close_item_modal(stagehand, page)
            await open_cart_drawer(page)
            await settle(page, 1800)
            continue

        if state.is_cart_visible and cart_snapshot is None:
            cart_snapshot = state
            await shot(page, out / "cart.png")

        # An upsell popup can sit on top of /checkout: it must be dismissed before the fee breakdown counts
        upsell = state.is_upsell_modal or state.step_type == "upsell_modal"
        has_fees = state.delivery_fee is not None or state.service_fee is not None
        if (state.is_checkout_page or "/checkout" in curr_url) and (has_fees or not upsell):
            await shot(page, out / "checkout.png")
            return cart_snapshot, state

        if state.blocked_reason or step_type == "login_wall":
            await login_or_block(state.blocked_reason or "Mandatory sign-in screen before checkout.")
            continue

        if upsell:
            upsell_declines += 1
            clicked = await js(page, JS_DECLINE_UPSELL)
            if clicked:
                print(f"    Declining upsell: clicked '{clicked}'")
            elif upsell_declines <= 2:
                act_res = await stagehand.act(
                    "An upsell popup ('Complete your meal', 'Add more items', 'Try Uber One') is blocking checkout. "
                    "Dismiss it WITHOUT adding anything: click 'Skip', 'No thanks', 'Not now', 'Next' or "
                    "'Continue to checkout', or its X close button. Never start an Uber One trial or place the order."
                )
                print(f"    Declining upsell: {act_res.data.action_description}")
            else:  # last resort: close whatever dialog is open
                await js(page, "() => { closeDialog() || pressEsc(); }")
                print("    Declining upsell: closed the dialog")
            await settle(page, 2000)
            if "/checkout" not in await page.url() and upsell_declines >= 2:
                await page.goto(CHECKOUT_URL)  # popup closed back to the menu: go straight to checkout
                await settle(page, 3000)
            continue

        if not state.is_cart_visible:
            if await open_cart_drawer(page):
                print("    Opening cart via DOM click on header cart button")
            else:
                act_res = await stagehand.act(
                    "Click the green cart button in the top-right header (or 'View cart') to open the cart drawer."
                )
                print(f"    Opening cart: {act_res.data.action_description}")
            await settle(page, 2200)
            continue

        if await js(page, JS_CLICK_CHECKOUT, default=False):
            print("    Clicked 'Go to checkout' button")
        else:
            act_res = await stagehand.act(
                "Click the 'Go to checkout' / 'Checkout' button in the cart drawer. Never click 'Place order' or 'Pay'."
            )
            print(f"    Proceeding to checkout: {act_res.data.action_description}")
        await settle(page, 3000)

    return cart_snapshot, None


# ---------------------------------------------------------------------------
# Restaurant's own website: find it, walk Menu -> Order online -> Pickup -> Store, read prices
# ---------------------------------------------------------------------------
JS_SEARCH_RESULTS = r"""({ excluded }) => {
    const out = [], seen = new Set();
    for (const a of document.querySelectorAll('a[href]')) {
        let href = a.href || '';
        try {  // unwrap DuckDuckGo (?uddg=) and Google (/url?q=) redirect links
            const u = new URL(href);
            href = u.searchParams.get('uddg') || (u.pathname === '/url' && u.searchParams.get('q')) || href;
        } catch (e) { continue; }
        let host = '';
        try { host = new URL(href).hostname.toLowerCase().replace(/^www\./, ''); } catch (e) { continue; }
        if (!href.startsWith('http') || excluded.some(bad => host.includes(bad)) || seen.has(host)) continue;
        const text = clean(a.innerText);
        if (text.length < 3) continue;
        seen.add(host);
        out.push({ href, host, text });
    }
    return out.slice(0, 10);
}"""

# Scans the page for (a) price-looking text and (b) the next navigation candidates, classified as
# menu / order / pickup / store / frame and tagged with data-audit-id so Python can click them.
JS_DIRECT_SCAN = r"""({ tokens }) => {
    // Keep everything in this tab
    document.querySelectorAll('a[target]').forEach(a => a.removeAttribute('target'));
    window.open = u => { if (u) location.href = u; return null; };
    document.querySelectorAll('[data-audit-id]').forEach(e => e.removeAttribute('data-audit-id'));

    const body = document.body ? document.body.innerText || '' : '';
    const dollars = (body.match(/\$\s?\d{1,3}(\.\d{2})?/g) || []).length;
    const bare = body.split('\n').filter(l => /^\s*\d{1,3}(\.\d{1,2})?\s*$/.test(l)).length;  // menus that list '20' not '$20'
    const prices = dollars + (bare >= 5 ? bare : 0);

    // Location affinity: earlier tokens (the suburb) outweigh later ones (the city)
    const locScore = s => {
        const low = s.toLowerCase();
        return tokens.reduce((n, t, i) => n + (new RegExp('\\b' + t + '\\b').test(low) ? tokens.length - i : 0), 0);
    };
    const cardScore = (el, siblings) => {  // score the element's own card: stop climbing once another store's button is inside
        let best = 0;
        for (let p = el.parentElement, i = 0; p && i < 6; p = p.parentElement, i++) {
            if (siblings.some(o => o.el !== el && p.contains(o.el))) break;
            best = Math.max(best, locScore(clean(p.innerText)));
        }
        return best;
    };

    const THIRD_PARTY = /uber|door\s?dash|menulog|deliveroo|hungry\s?panda|facebook|instagram|tiktok|maps|goo\.gl|mailto:|tel:/i;
    const SKIP = /gift|voucher|catering|function|career|jobs?\b|book|reserv|franchise|contact|about|log\s?in|sign\s?(in|up)|account|cart|checkout|privacy|terms|track|history|app store|google play|download|allergen|nutrition|toggle|navigation|^open|^close/i;
    const classify = (text, href) => {
        if (text.length < 3 || text.length > 80 || THIRD_PARTY.test(text + ' ' + href) || SKIP.test(text)) return null;
        if (/\.(pdf|jpe?g|png|webp)(\?|$)/i.test(href)) return null;
        const pickup = /\bpick[\s-]?up\b|\bcollect(ion)?\b|\btake[\s-]?away\b/i.test(text);
        if (/deliver/i.test(text) && !pickup) return null;
        if (text.length <= 45) {
            if (pickup) return 'pickup';
            if (/\border(ing)?\b/i.test(text)) return 'order';
            if (/\bmenus?\b/i.test(text) && text.length <= 30) return 'menu';
        }
        return locScore(text) > 0 ? 'store' : null;
    };

    const label = el => clean(el.innerText)
        || clean(el.getAttribute('aria-label') || el.getAttribute('title') || el.value || el.querySelector('img[alt]')?.alt);
    const collect = root => {
        const out = [];
        for (const el of root.querySelectorAll(
            'a[href], button, [role="button"], [role="tab"], [role="link"], [role="option"], [role="menuitem"], input[type="submit"], input[type="button"]')) {
            if (!vis(el)) continue;
            const text = label(el).slice(0, 80);
            const href = el.tagName === 'A' ? el.href : '';
            const kind = classify(text, href);
            if (kind) out.push({ el, kind, text, href, value: null, loc: locScore(text) });
        }
        for (const sel of root.querySelectorAll('select')) {  // store dropdowns
            if (!vis(sel)) continue;
            const best = Array.from(sel.options).map(o => ({ o, s: locScore(o.text) })).sort((a, b) => b.s - a.s)[0];
            if (best && best.s > 0) out.push({ el: sel, kind: 'store', text: clean(best.o.text), href: '', value: best.o.value, loc: best.s });
        }
        return out;
    };

    // A blocking modal (pickup/delivery chooser, store picker) takes precedence over the page behind it
    const modal = dialogs().filter(d => vis(d, 200, 150)).pop();
    let cands = modal ? collect(modal) : [];
    if (!cands.length) cands = collect(document);

    // The same button repeated once per store ('Order now' x N): keep only the one on our suburb's card
    let needsStore = false;
    const groups = {};
    for (const c of cands) if (c.kind !== 'store') (groups[c.kind + '|' + c.text.toLowerCase()] ||= []).push(c);
    for (const g of Object.values(groups)) {
        if (g.length < 2) continue;
        const hrefs = new Set(g.map(c => c.href));
        if (hrefs.size === 1 && g[0].href) continue;            // one link shown twice (header + footer)
        g.forEach(c => { c.loc = cardScore(c.el, g); });
        const best = g.slice().sort((a, b) => b.loc - a.loc)[0];
        const perStore = best.loc > 0 || hrefs.size > 1;
        if (!perStore) { g.slice(1).forEach(c => { c.kind = null; }); continue; }
        g.forEach(c => { c.kind = null; });
        if (best.loc > 0) best.kind = 'store'; else needsStore = true;
    }

    const rank = c => c.kind === 'store'
        ? -c.loc * 100 + c.text.length
        : (/drink|wine|cocktail|beverage|\bbar\b|kids|dessert/i.test(c.text) ? 100 : 0) + (c.href ? 0 : 20) + c.text.length;
    const seen = new Set();
    const actions = cands.filter(c => c.kind).sort((a, b) => rank(a) - rank(b)).filter(c => {
        const key = c.kind + '|' + c.text.toLowerCase() + '|' + c.href;
        return seen.has(key) ? false : seen.add(key);
    }).slice(0, 40).map((c, i) => {
        c.el.setAttribute('data-audit-id', String(i));
        return { id: i, kind: c.kind, text: c.text, href: c.href, value: c.value };
    });

    // Ordering widgets embedded as iframes: open the frame's own URL so its menu can be read
    for (const f of document.querySelectorAll('iframe[src^="http"]')) {
        if (vis(f, 300, 300) && !/youtube|vimeo|google|facebook|instagram|recaptcha|spotify|typeform|mailchimp/i.test(f.src)) {
            actions.push({ id: -1, kind: 'frame', text: 'embedded ordering frame', href: f.src, value: null });
        }
    }
    return { url: location.href, prices, needsStore, actions };
}"""

JS_DIRECT_CLICK = r"""({ id, value }) => {
    const el = document.querySelector('[data-audit-id="' + id + '"]');
    if (!el) return false;
    el.scrollIntoView({ block: 'center', behavior: 'instant' });
    if (value !== null && el.tagName === 'SELECT') {
        el.value = value;
        el.dispatchEvent(new Event('change', { bubbles: true }));
    } else {
        el.click();
    }
    return true;
}"""


async def find_direct_restaurant_urls(page: Page, store_name: str, loc_tokens: list[str]) -> list[str]:
    """Search for the restaurant's own site; results whose domain or title carries the restaurant name rank first."""
    name = clean_store_search_name(store_name)
    name_tokens = [t for t in norm_name(name).split() if len(t) >= 3 and t not in ("the", "and")]
    joined = re.sub(r"[^a-z0-9]", "", name.lower())
    query = quote_plus(f"{name} {loc_tokens[0] if loc_tokens else ''} menu order online".replace("  ", " "))

    def score(c: dict) -> int:
        host = re.sub(r"[^a-z0-9]", "", c["host"].rsplit(".", 2)[0] if c["host"].count(".") >= 2 else c["host"].split(".")[0])
        title = norm_name(c["text"])
        return (
            (4 if joined and (joined in host or (len(host) >= 5 and host in joined)) else 0)
            + sum(1 for t in name_tokens if t in host)
            + (2 if name_tokens and all(t in title for t in name_tokens) else 0)
        )

    for engine_url in (f"https://html.duckduckgo.com/html/?q={query}", f"https://www.google.com/search?q={query}&hl=en"):
        with contextlib.suppress(Exception):
            await page.goto(engine_url)
            await settle(page, 2000)
        candidates = await js(page, JS_SEARCH_RESULTS, {"excluded": list(EXCLUDED_SEARCH_DOMAINS)}, default=[])
        if candidates:
            ranked = sorted(candidates, key=score, reverse=True)  # stable: search order breaks ties
            named = [c for c in ranked if score(c) > 0]
            return [c["href"] for c in (named or ranked[:1])][:MAX_DIRECT_SITES]
    return []


def apply_direct_matches(pending: list[dict], matches: list[DirectMenuMatch]) -> None:
    """Write matched direct prices onto the items and drop them from `pending`."""
    for m in matches:
        price = parse_price(m.direct_price_display)
        idx = best_match(m.uber_item_name, [i["name"] for i in pending])
        if idx is None or not m.direct_item_name or not price:
            continue
        item = pending.pop(idx)
        uber_price = item.get("shop_price") or item.get("selected_price")
        item["direct_item_name"] = m.direct_item_name
        item["direct_price_display"] = f"${price:.2f}"
        item["direct_price"] = price
        item["uber_vs_direct_diff"] = diff(uber_price, price)
        item["uber_vs_direct_markup_pct"] = pct_diff(uber_price, price)
        if uber_price is not None:
            print(
                f"    Direct match: '{item['name']}' -> Uber Eats ${uber_price:.2f} vs "
                f"direct '{m.direct_item_name}' ${price:.2f} "
                f"({item['uber_vs_direct_diff']:+.2f} / {item['uber_vs_direct_markup_pct']:+}%)"
            )


async def extract_direct_prices(stagehand: Stagehand, store_name: str, pending: list[dict]) -> str:
    """One LLM extract for the still-unmatched items; returns the page_state."""
    wanted = "; ".join(
        f"'{i['name']}'" + (f" [{i['category']}]" if i.get("category") else "") + f" (Uber Eats {i['shop_price_display']})"
        for i in pending
    )
    data = (
        await stagehand.extract(
            f"This is the restaurant's own website or ordering page for '{store_name}'. Find these Uber Eats menu "
            f"items on it and report the price THIS site charges for each: {wanted}. "
            "Match the same dish even if worded slightly differently, at the same size (prefer the base/regular "
            "version); do NOT match a different dish, a combo/meal deal, or a different size. Prices may be bare "
            "numbers like '20'. Leave direct_item_name and direct_price_display null when the dish or its price is "
            "not shown on this page. Also classify `page_state`.",
            DirectSiteExtraction,
        )
    ).data
    apply_direct_matches(pending, data.matches)
    print(f"    Direct page: {data.page_state} | still missing {len(pending)} item(s) | {data.summary[:100]}")
    return data.page_state


async def take_direct_step(page: Page, scan: dict, tried: set[str], used: dict[str, int]) -> str | None:
    """Take the next untried navigation candidate in Menu -> Order -> Pickup -> Store -> Frame order."""
    here = scan["url"].split("#")[0]
    for kind in DIRECT_KIND_ORDER:
        for a in scan["actions"]:
            href = a["href"].split("#")[0]
            key = f"{kind}|{a['text'].lower()}|{href}"
            if a["kind"] != kind or key in tried or used.get(kind, 0) >= DIRECT_KIND_BUDGET[kind]:
                continue
            tried.add(key)
            if href.startswith("http") and href != here:
                with contextlib.suppress(Exception):  # slow sites can time out yet still render
                    await page.goto(a["href"])
            elif not await js(page, JS_DIRECT_CLICK, {"id": a["id"], "value": a["value"]}, default=False):
                continue
            used[kind] = used.get(kind, 0) + 1
            return f"{kind} -> '{a['text']}'" + (f" ({href})" if href.startswith("http") and href != here else "")
    return None


async def walk_direct_site(
    stagehand: Stagehand,
    page: Page,
    store_name: str,
    pending: list[dict],
    loc_tokens: list[str],
) -> None:
    """Navigate one restaurant site until the pending items' prices are read (items are popped from `pending`)."""
    where = " / ".join(loc_tokens[:2]) or "the nearest location"
    tried: set[str] = set()
    used: dict[str, int] = {}
    extracted: set[tuple[str, int]] = set()
    llm_navs = category_hops = 0
    force_extract = False

    for step in range(1, MAX_DIRECT_SITE_STEPS + 1):
        for frac in (0.4, 0.8, 1.0, 0):  # trigger lazy-loaded menu sections, then return to the top
            await js(page, "f => window.scrollTo(0, document.documentElement.scrollHeight * f)", frac)
            await page.wait_for_timeout(350)

        scan = await js(page, JS_DIRECT_SCAN, {"tokens": loc_tokens},
                        default={"url": await page.url(), "prices": 0, "needsStore": False, "actions": []})
        can_navigate = any(
            f"{a['kind']}|{a['text'].lower()}|{a['href'].split('#')[0]}" not in tried
            and used.get(a["kind"], 0) < DIRECT_KIND_BUDGET[a["kind"]]
            for a in scan["actions"]
        )
        print(f"    Direct step {step}: {scan['url']} | price signals={scan['prices']} | nav options={len(scan['actions'])}")

        # Only spend an LLM extract where prices are actually on screen (or there is nowhere left to click)
        page_key = (scan["url"], scan["prices"] // 5)
        if force_extract or ((scan["prices"] >= MIN_PRICE_SIGNALS or not can_navigate) and page_key not in extracted):
            extracted.add(page_key)
            force_extract = False
            page_state = await extract_direct_prices(stagehand, store_name, pending)
            if not pending:
                return
            # Prices are showing but some items are in another category tab: open it and re-read
            if page_state == "menu_with_prices" and category_hops < MAX_CATEGORY_HOPS:
                category_hops += 1
                target = pending[0]
                act_res = await stagehand.act(
                    f"On this menu, click the category tab or section link most likely to contain '{target['name']}'"
                    + (f" (it is under '{target['category']}' on Uber Eats)" if target.get("category") else "")
                    + ". Do not add anything to the cart."
                )
                print(f"    Category hop: {act_res.data.action_description}")
                force_extract = True
                await settle(page, 1800)
                continue

        action = await take_direct_step(page, scan, tried, used)
        if not action:
            if llm_navs >= MAX_DIRECT_LLM_NAV:
                return
            llm_navs += 1
            act_res = await stagehand.act(
                "We need this restaurant's food menu WITH PRICES. Do the single next step towards it, in this order "
                "of preference: (1) open the food 'Menu'; (2) otherwise click 'Order online' / 'Order now'; "
                "(3) choose 'Pickup' / 'Takeaway' rather than delivery; "
                f"(4) if asked to choose a store or location, choose the one in or nearest to {where} "
                "(type the suburb into the store search if needed), and choose ASAP / the earliest time if asked. "
                "Never open Uber Eats, DoorDash, Menulog or Deliveroo links, never sign in, never add items or pay."
                + (" A store must be selected first." if scan["needsStore"] else "")
            )
            action = f"LLM -> {act_res.data.action_description}"
        print(f"    Direct navigation: {action}")
        await settle(page, 2500)


async def audit_direct_restaurant_site(
    stagehand: Stagehand,
    page: Page,
    result: SiteResult,
    address: str,
    explicit_restaurant_url: str | None = None,
) -> None:
    """Visit the restaurant's own website and compare unit prices against Uber Eats."""
    added = result.added
    if not added or not result.store_name:
        return

    print(f"\n  --- Auditing Direct Restaurant Website for '{result.store_name}' ---")
    loc_tokens = location_tokens(result.store_name, result.store_location, address)
    urls = [explicit_restaurant_url] if explicit_restaurant_url else await find_direct_restaurant_urls(
        page, result.store_name, loc_tokens
    )
    if not urls:
        print(f"  Could not locate an independent website for '{result.store_name}'.")
        return

    pending = list(added)
    for n, url in enumerate(urls, start=1):
        print(f"  Direct restaurant URL ({n}/{len(urls)}): {url} | branch hints: {loc_tokens}")
        try:
            await page.goto(url)
            await settle(page, 2500)
            await maybe_dismiss_popups(stagehand, page)
            await walk_direct_site(stagehand, page, result.store_name, pending, loc_tokens)
        except Exception as error:
            print(f"  Direct website check failed on {url}: {error}")
        await shot(page, result.out_dir / f"restaurant_direct_{n}.png")
        if result.direct_website_url is None or len(pending) < len(added):
            with contextlib.suppress(Exception):
                result.direct_website_url = await page.url()
        if len(pending) < len(added):  # this site yielded prices; keep its page text as evidence
            text = await js(page, "() => document.body.innerText", default="")
            (result.out_dir / "restaurant_direct_menu.txt").write_text(text, encoding="utf-8")
            break

    for item in pending:
        print(f"    Direct match: '{item['name']}' -> not found with a price on the restaurant's site")


# ---------------------------------------------------------------------------
# Price & fee comparison
# ---------------------------------------------------------------------------
def compare_prices(
    result: SiteResult,
    cart: CartAndCheckoutState | None,
    checkout: CartAndCheckoutState | None,
) -> dict:
    final = checkout or cart
    added = result.added

    def shown(attr: str):
        return getattr(final, attr) if final else None

    remaining = list(final.items) if final else []
    for item in added:
        expected = item["selected_price"] if item["selected_price"] is not None else item["shop_price"]
        idx = best_match(item["name"], [li.name for li in remaining])
        match = remaining.pop(idx) if idx is not None else None
        unit = parse_price(match.price) if match else None
        if unit is not None and match.quantity > 1:
            unit = round(unit / match.quantity, 2)
        item["expected_price"] = expected
        item["selected_vs_shop_diff"] = diff(item["selected_price"], item["shop_price"])
        item["checkout_price_display"] = match.price if match else None
        item["checkout_price"] = unit
        item["checkout_vs_expected_diff"] = diff(unit, expected)
        item["price_increased"] = (item["checkout_vs_expected_diff"] or 0) > 0

    expected_subtotal = money_sum([i["expected_price"] for i in added])
    subtotal = parse_price(shown("subtotal"))
    delivery = parse_price(shown("delivery_fee"))
    service = parse_price(shown("service_fee"))
    small_order = parse_price(shown("small_order_fee"))
    base = subtotal or expected_subtotal
    service_pct = pct_of(service, base)
    delivery_diff = diff(delivery, result.advertised_delivery_fee)
    other = [(f, parse_amount(f.amount)) for f in (final.other_fees if final else [])]

    fees = [{"label": label, "amount": amt} for label, amt in (("Service Fee", service), ("Small Order Fee", small_order)) if amt]
    fees += [{"label": f.label, "amount": amt} for f, amt in other]
    fees_total = money_sum([f["amount"] for f in fees]) or 0.0

    def fee_row(fee_type, label, advertised, checkout_display, amount, diff_adv, pct, mandatory=True) -> dict:
        return {
            "fee_type": fee_type, "label": label, "advertised_display": advertised,
            "checkout_display": checkout_display, "amount": amount, "diff_vs_advertised": diff_adv,
            "pct_of_subtotal": pct, "mandatory": mandatory,
        }

    undisclosed = "not disclosed upfront"
    result.fee_breakdown_rows = [
        fee_row("delivery_fee", "Standard Delivery Fee", result.advertised_delivery_fee_display or "not shown",
                shown("delivery_fee") or "not shown", delivery, delivery_diff,
                round((delivery / base) * 100.0, 1) if delivery is not None and base else None),
        fee_row("service_fee", "Service Fee", result.advertised_service_fee_display or undisclosed,
                shown("service_fee") or "$0.00 / not shown", service, service, service_pct),
        fee_row("small_order_fee", "Small Order Fee", undisclosed,
                shown("small_order_fee") or "$0.00", small_order or 0.0, small_order, pct_of(small_order, base) or 0.0),
        fee_row("priority_delivery", "Priority Delivery Upgrade (Optional)", "n/a",
                shown("priority_delivery_fee") or "not offered", parse_price(shown("priority_delivery_fee")),
                None, None, mandatory=False),
        *(fee_row("other_fee", f.label, undisclosed, f.amount, amt, amt, pct_of(amt, base)) for f, amt in other),
    ]

    for category, label, amount, attr in (
        ("service_fee", f"Service Fee ({service_pct}% of subtotal)" if service_pct else "Service Fee", service, "service_fee"),
        ("small_order_fee", "Small Order Fee", small_order, "small_order_fee"),
    ):
        if amount is not None:
            result.extras.append({
                "step": len(result.steps), "step_type": "checkout", "step_heading": "Checkout Breakdown",
                "category": category, "label": label, "price_display": shown(attr), "price": amount,
                "price_basis": "per order", "selected_by_default": True, "mandatory": True,
            })

    matched_direct = [i for i in added if i.get("direct_price") is not None and i.get("shop_price") is not None]
    direct_subtotal = money_sum([i["direct_price"] for i in matched_direct])
    uber_matched_subtotal = money_sum([i["shop_price"] for i in matched_direct])
    markup_dollars = diff(uber_matched_subtotal, direct_subtotal)
    markup_pct = pct_diff(uber_matched_subtotal, direct_subtotal)
    preselected = [
        e["label"] for e in result.extras
        if e["selected_by_default"] and not e["mandatory"] and (e["price"] or 0) > 0
    ]

    reasons = [
        reason for reason, hit in (
            ("menu item price increased at checkout", any(i["price_increased"] for i in added)),
            ("checkout item subtotal higher than menu prices", (diff(subtotal, expected_subtotal) or 0) > 0),
            (f"service fee added at checkout ({shown('service_fee')}"
             + (f", {service_pct}% of subtotal)" if service_pct else ")"), (service or 0) > 0),
            (f"small order fee added ({shown('small_order_fee')})", (small_order or 0) > 0),
            (f"delivery fee higher than advertised ({result.advertised_delivery_fee_display} -> {shown('delivery_fee')})",
             (delivery_diff or 0) > 0),
            ("other non-tax fees added", fees_total > (service or 0) + (small_order or 0)),
            (f"Uber Eats menu prices marked up +{markup_pct}% (+${markup_dollars or 0:.2f}) vs restaurant direct website",
             (markup_dollars or 0) > 0),
            ("optional extras pre-selected: " + ", ".join(preselected), bool(preselected)),
        )
        if hit
    ]

    by_category: dict[str, int] = {}
    for extra in result.extras:
        by_category[extra["category"]] = by_category.get(extra["category"], 0) + 1

    total = parse_price(shown("total"))
    return {
        "store_name": result.store_name,
        "direct_website_url": result.direct_website_url,
        "items_added": len(added),
        "expected_subtotal": expected_subtotal,
        "cart_subtotal_display": cart.subtotal if cart else None,
        "checkout_subtotal_display": shown("subtotal"),
        "checkout_subtotal": subtotal,
        "subtotal_vs_expected_diff": diff(subtotal, expected_subtotal),
        "advertised_delivery_fee_display": result.advertised_delivery_fee_display,
        "advertised_delivery_fee": result.advertised_delivery_fee,
        "shipping": shown("delivery_fee") or result.advertised_delivery_fee_display
        or "not calculated (login/checkout required)",
        "checkout_delivery_fee": delivery,
        "delivery_fee_diff": delivery_diff,
        "priority_delivery_fee_display": shown("priority_delivery_fee"),
        "service_fee_display": shown("service_fee"),
        "service_fee": service,
        "service_fee_pct_of_subtotal": service_pct,
        "small_order_fee_display": shown("small_order_fee"),
        "small_order_fee": small_order,
        "other_fees": fees,
        "other_fees_total": fees_total,
        "direct_items_matched": len(matched_direct),
        "direct_matched_subtotal": direct_subtotal,
        "uber_matched_subtotal": uber_matched_subtotal,
        "uber_vs_direct_subtotal_diff": markup_dollars,
        "uber_vs_direct_markup_pct": markup_pct,
        "extras_offered": len(result.extras),
        "extras_offered_by_category": by_category,
        "extras_preselected": preselected,
        "tax_amount_display": shown("tax_amount"),
        "tax_included": shown("tax_included"),
        "checkout_total_display": shown("total"),
        "checkout_total": total,
        "total_vs_expected_diff": diff(total, expected_subtotal),
        "currency": shown("currency") or "AUD",
        "drip_detected": bool(reasons),
        "drip_reasons": reasons,
        "checkout_summary": shown("summary"),
    }


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
EXTRA_FIELDS = [
    "step", "step_type", "step_heading", "category", "label",
    "price_display", "price", "price_basis", "selected_by_default", "mandatory",
]
ITEM_FIELDS = [
    "name", "selected_variant", "shop_price_display", "selected_price_display",
    "checkout_price_display", "checkout_vs_expected_diff", "price_increased",
    "direct_item_name", "direct_price_display", "uber_vs_direct_diff", "uber_vs_direct_markup_pct",
    "status",
]
FEE_BREAKDOWN_FIELDS = [
    "fee_type", "label", "advertised_display", "checkout_display",
    "amount", "diff_vs_advertised", "pct_of_subtotal", "mandatory",
]


def write_reports(result: SiteResult, address: str, query: str | None, store_url: str | None, checkout_url: str | None) -> None:
    """Written even when a run ends early, preserving all captured items, fees, and direct-menu comparisons."""
    out = result.out_dir
    report = {
        "site": result.site,
        "shop_url": store_url,
        "checkout_url": checkout_url,
        "direct_website_url": result.direct_website_url,
        "status": result.status,
        "message": result.message,
        "search": {"address": address, "query": query},
        "items": result.items,
        "fee_breakdown": result.fee_breakdown_rows,
        **result.comparison,
        "extras": result.extras,
        "steps": result.steps,
    }
    (out / "price_comparison.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    write_csv(out / "price_comparison.csv", ITEM_FIELDS, result.items)
    write_csv(out / "extras.csv", EXTRA_FIELDS, result.extras)
    write_csv(out / "fees_breakdown.csv", FEE_BREAKDOWN_FIELDS, result.fee_breakdown_rows)


def print_audit_tables(result: SiteResult) -> None:
    c = result.comparison
    print("\n  --- Extra Fees Breakdown (Uber Eats) ---")
    for row in result.fee_breakdown_rows:
        pct_str = f" ({row['pct_of_subtotal']}% of subtotal)" if row.get("pct_of_subtotal") else ""
        print(
            f"    {row['label']:<34} | Adv: {str(row['advertised_display']):<24} "
            f"| Checkout: {row['checkout_display']}{pct_str}"
        )

    print("\n  --- Menu Item Unit Price Comparison (Uber Eats vs Restaurant Direct) ---")
    for item in result.added:
        uber_p = item.get("shop_price_display") or item.get("selected_price_display")
        delta, pct = item.get("uber_vs_direct_diff"), item.get("uber_vs_direct_markup_pct")
        markup = f"{delta:+.2f} ({pct:+}%)" if delta is not None else "n/a"
        print(
            f"    {item['name']:<34} | Uber Eats: {str(uber_p):<10} "
            f"| Direct: {item.get('direct_price_display') or 'not found':<10} | Diff: {markup}"
        )

    print(
        f"\n  Summary: Uber Subtotal={c['expected_subtotal']} | Checkout Subtotal={c['checkout_subtotal_display']} "
        f"| Delivery={c['shipping']} | Service/Other Fees={c['other_fees_total']} | Total={c['checkout_total_display']}"
    )
    print(f"  Drip / Markup Detected: {'YES - ' + '; '.join(c['drip_reasons']) if c['drip_detected'] else 'NO'}")


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
    rows: list[dict] = []
    if path.exists():
        with open(path, newline="", encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r.get("site") != result.site]
    write_csv(path, list(row), rows + [row])


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
async def run_site(
    stagehand: Stagehand,
    page: Page,
    url: str,
    address: str,
    query: str | None,
    n_items: int,
    explicit_restaurant_url: str | None,
    skip_direct_check: bool,
    result: SiteResult,
) -> None:
    out = result.out_dir
    store_url: str | None = None
    checkout_url: str | None = None
    cart_state: CartAndCheckoutState | None = None
    checkout_state: CartAndCheckoutState | None = None

    print(f"\n=== {url} | Address: {address} | Query: {query or 'any'} ===")
    try:
        await page.goto(url)
        await settle(page)
        await maybe_dismiss_popups(stagehand, page)

        store_url = await ensure_address_and_open_store(stagehand, page, address, query, result)
        print(f"  Store page: {store_url}")
        await clear_existing_cart(stagehand, page)
        await shot(page, out / "store.png")

        menu = (
            await stagehand.extract(
                "Extract the restaurant name, street/suburb location (if shown), advertised delivery fee and service fee "
                "shown in the store header/banner, and extract the menu items listed on this page with their category, "
                "price, availability, and whether each is food/drink.",
                StoreMenu,
            )
        ).data
        header = menu.store_header
        result.store_name = header.store_name or result.store_name
        result.store_location = header.store_address_or_suburb or result.store_location
        if header.advertised_delivery_fee and not result.advertised_delivery_fee_display:
            result.advertised_delivery_fee_display = header.advertised_delivery_fee
            result.advertised_delivery_fee = parse_price(header.advertised_delivery_fee)
        result.advertised_service_fee_display = result.advertised_service_fee_display or header.advertised_service_fee

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
                item = new_item(menu_item, error=str(error))
                print(f"    Skipped: {error}")
            result.items.append(item)

        if not result.added:
            result.status, result.message = "add_to_cart_failed", "No menu items could be added to the order"
            return

        cart_state, checkout_state = await walk_to_checkout(stagehand, page, result)
        with contextlib.suppress(Exception):
            checkout_url = await page.url()

        if checkout_state and (checkout_state.is_checkout_page or checkout_state.delivery_fee or checkout_state.service_fee):
            result.status = "ok"
        elif cart_state or checkout_state:
            result.status, result.message = "no_checkout", "Cart reached, but final checkout screen was not reached"
        else:
            result.status, result.message = "no_checkout", "Checkout screen not reached"

    except Blocked as error:
        result.status, result.message = "blocked", str(error)
        with contextlib.suppress(Exception):
            checkout_url = await page.url()
        print(f"  Blocked: {result.message}")
    finally:
        if not skip_direct_check and result.added:
            try:
                await audit_direct_restaurant_site(stagehand, page, result, address, explicit_restaurant_url)
            except Exception as error:
                print(f"  Direct website check skipped/failed: {error}")
        result.comparison = compare_prices(result, cart_state, checkout_state)
        write_reports(result, address, query, store_url, checkout_url)

    print_audit_tables(result)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Uber Eats food-delivery drip-pricing & direct-menu markup POC")
    parser.add_argument("urls", nargs="*", default=[SITE_URL],
                        help=f"Uber Eats homepage or direct `/store/...` URL(s) (default: {SITE_URL})")
    parser.add_argument("--address", default=DEFAULT_ADDRESS,
                        help=f"Delivery address to enter (default: '{DEFAULT_ADDRESS}')")
    parser.add_argument("--query", default=None,
                        help="Optional search term when starting from the homepage (e.g. 'burgers', 'thai')")
    parser.add_argument("--items", "--products", dest="items", type=int, default=2, choices=range(1, 6), metavar="N",
                        help="Number of menu items to add to the order (1-5, default 2)")
    parser.add_argument("--restaurant-url", default=None,
                        help="Optional direct URL to the restaurant's own website/ordering page for unit-price comparison")
    parser.add_argument("--skip-direct-site", action="store_true",
                        help="Skip visiting the restaurant's own website at the end of the Uber Eats run")
    parser.add_argument("--login", action="store_true",
                        help="Launch the persistent local Chrome profile on Uber Eats so you can log in manually, then exit")
    parser.add_argument("--local", action="store_true",
                        help="Force local Chrome with `.chrome-profile` even if BROWSERBASE_API_KEY is set in .env")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    api_key = os.environ["GOOGLE_GENERATIVE_AI_API_KEY"]
    model = "google/" + os.getenv("GEMINI_MODEL", "gemini-3.8-flash").removeprefix("google/")

    history: list[dict] = []

    def record(log) -> None:
        if _is_noise(getattr(log, "message", "") or ""):
            return
        entry = log.model_dump(mode="json")
        entry["timestamp"] = datetime.now(timezone.utc).isoformat()
        history.append(entry)

    bb_api_key, bb_project_id = os.getenv("BROWSERBASE_API_KEY"), os.getenv("BROWSERBASE_PROJECT_ID")
    bb_client: Browserbase | None = None
    bb_session_id: str | None = None

    if bb_api_key and bb_project_id and not args.local and not args.login:
        bb_client = Browserbase(api_key=bb_api_key)
        bb_session_id = bb_client.sessions.create(project_id=bb_project_id).id
        browser = await stagehand_browserbase.connect(api_key=bb_api_key, session_id=bb_session_id)
    else:
        profile_dir = CHROME_PROFILE_DIR.resolve()
        profile_dir.mkdir(parents=True, exist_ok=True)
        browser = await local_browser.launch(
            headless=False if args.login else (os.getenv("HEADLESS") == "true"),
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
                    page, "Click 'Sign in' in the top-right corner of the browser window and log in to Uber Eats."
                )
                print(f"  Login session saved to {CHROME_PROFILE_DIR.resolve()}")
                return

            for url in args.urls:
                url = url if url.startswith("http") else f"https://{url}"
                parsed = urlparse(url)
                folder = parsed.netloc.removeprefix("www.")
                if "/store/" in parsed.path:
                    folder += "-" + slug(parsed.path.split("/store/")[-1].split("/")[0])
                out_dir = OUTPUT_ROOT / folder / datetime.now().strftime("%Y%m%d-%H%M%S")
                out_dir.mkdir(parents=True, exist_ok=True)
                result = SiteResult(site=url, out_dir=out_dir)

                history.clear()
                start = perf_counter()
                before = await stagehand.metrics()

                try:
                    await run_site(
                        stagehand=stagehand,
                        page=page,
                        url=url,
                        address=args.address,
                        query=args.query,
                        n_items=args.items,
                        explicit_restaurant_url=args.restaurant_url,
                        skip_direct_check=args.skip_direct_site,
                        result=result,
                    )
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
                    "direct_website_url": result.direct_website_url,
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