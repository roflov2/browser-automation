"""The Uber Eats side of the audit: delivery address, store, menu items, cart and checkout."""

import base64
import contextlib
import functools
import json
import logging
import re
from pathlib import Path
from typing import NamedTuple, TypeVar
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

from pydantic import BaseModel
from stagehand import Page, Stagehand

from ... import page as page_helpers
from ...core import (
    BROWSER_ERRORS,
    Blocked,
    ElementNotFound,
    ExtractFailed,
    Item,
    StepTimeout,
    new_item,
    parse_price,
    record_offers,
    select_price,
    slug,
)
from ...page import is_sso_redirect, pause_for_login, settle, shot, wait_for_render, wait_out_sso
from ...safety import Mode, current_guard, safe_act, try_act
from .models import (
    AddressAndFeedState,
    CartAndCheckoutState,
    ItemModalState,
    MenuItem,
    StoreCard,
    StoreFeed,
    UberResult,
    extract_suburb_hint,
)

log = logging.getLogger(__name__)
ModelT = TypeVar("ModelT", bound=BaseModel)

SITE_URL = "https://www.ubereats.com/au"
CHECKOUT_URL = f"{SITE_URL}/checkout"
MAX_ADDRESS_STEPS = 12
MAX_CLOSED_STORES = 4
MAX_MODIFIER_STEPS = 6
MAX_CHECKOUT_STEPS = 10

NON_RESTAURANT_NAMES = (
    "coles",
    "woolworths",
    "aldi",
    "iga",
    "costco",
    "7-eleven",
    "7 eleven",
    "liquorland",
    "bws",
    "dan murphy",
    "first choice liquor",
    "vintage cellars",
    "chemist warehouse",
    "priceline",
    "ampol",
    "bp ",
    "caltex",
    "officeworks",
    # Kinds of shop rather than chains: the feed card is picked from the page, without the model's judgement
    "supermarket",
    "grocer",
    "liquor",
    "cellars",
    "bottle shop",
    "pharmacy",
    "chemist",
    "convenience",
    "petrol",
)
STREET_TYPES = {
    "st",
    "street",
    "pl",
    "place",
    "rd",
    "road",
    "ave",
    "avenue",
    "dr",
    "drive",
    "ct",
    "court",
    "cct",
    "circuit",
    "ln",
    "lane",
    "way",
    "blvd",
    "boulevard",
    "pde",
    "parade",
    "tce",
    "terrace",
    "cl",
    "close",
    "cr",
    "cres",
    "crescent",
    "hwy",
    "highway",
    "walk",
    "grove",
    "gr",
}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def is_valid_restaurant_name(name: str | None) -> bool:
    return bool(name) and not any(bad in (name or "").lower().strip() for bad in NON_RESTAURANT_NAMES)


def is_login_url(url: str) -> bool:
    low = url.lower()
    return any(w in low for w in ("auth.uber.com", "login.uber.com", "/login-redirect", "/login"))


def street_tokens(address: str) -> list[str]:
    """Extract normalized primary street tokens (e.g. '1 Jeffcott Place, Canberra' -> ['1', 'jeffcott'])."""
    words = re.findall(r"[a-z0-9]+", address.split(",", maxsplit=1)[0].lower())
    return ([t for t in words if t not in STREET_TYPES] or words)[:2]


def addresses_match(displayed: str | None, target: str) -> bool:
    """Return True only if `displayed` contains the key street number/name tokens of `target`."""
    if not displayed:
        return False
    disp_low = displayed.lower()
    if "enter delivery address" in disp_low or disp_low.strip() == "deliver now":
        return False
    req_tokens = street_tokens(target)
    disp_tokens = set(re.findall(r"[a-z0-9]+", disp_low))
    return bool(req_tokens) and all(tok in disp_tokens for tok in req_tokens)


def extract_pl_address_from_url(url: str) -> str | None:
    """Decode the `pl=` base64/URL-encoded JSON parameter on Uber Eats URLs to check the active address."""
    pl_val = parse_qs(urlparse(url).query).get("pl", [None])[0]
    if not pl_val:
        return None
    try:
        decoded = unquote(pl_val)
        if not decoded.strip().startswith("{"):
            padded = pl_val + "=" * (-len(pl_val) % 4)
            decoded = unquote(base64.b64decode(padded).decode("utf-8", errors="ignore"))
        data = json.loads(decoded)
    except ValueError as error:  # bad base64 or JSON: the parameter is simply not what we expected
        log.debug("Unreadable pl= parameter: %s", error)
        return None
    return (data.get("address") or data.get("title")) if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Page helpers
# ---------------------------------------------------------------------------
UBER_JS = r"""
// Wording Uber Eats shows on a store card or store page that cannot take an order now
const STORE_CLOSED = /(?<![a-z])(closed|currently unavailable|unavailable|not accepting orders|opens? (at|in|tomorrow|on|\d)|available (at|from|tomorrow|on|in) |schedule (an? |your )?order|order (ahead|later) for)/i;
const addButton = () => Array.from(document.querySelectorAll('button')).find(
    b => /^(Add\s+\d*\s*to\s+(order|cart)|Update\s+order)/i.test(txt(b)) && vis(b, 80, 25));
const cartButtons = () => Array.from(document.querySelectorAll(
    '[data-testid*="cart" i], button[aria-label*="cart" i], button[aria-label*="bag" i]'));
// Tag the one element an extraction should be limited to (see SCOPE); null clears the tag
const markScope = el => {
    document.querySelectorAll('[data-audit-scope]').forEach(n => n.removeAttribute('data-audit-scope'));
    if (!el) return false;
    el.setAttribute('data-audit-scope', '1');
    return true;
};
// Uber's location-manager 'building type' / dropoff-details prompt ('Let us know your building type for more
// accurate deliveries'), or null. Its wording is required, so an item modal, the cart or the 'new order' prompt
// never counts: a strong phrase, or at least two building kinds offered as choices.
const LOCATION_PROMPT = /building type|type of building|more accurate deliver|choose your building|drop-?off (options?|instructions?|details?)/i;
const BUILDING_KINDS = /\b(apartment|house|townhouse|business)\b/gi;
const locationPrompt = () => dialogs().filter(d => vis(d, 200, 150)).reverse().find(d => {
    const t = clean(txt(d));
    if (addButton() && d.contains(addButton())) return false;
    if (/subtotal|checkout|add to (order|cart)/i.test(t)) return false;
    if (LOCATION_PROMPT.test(t)) return true;
    return new Set((t.match(BUILDING_KINDS) || []).map(k => k.toLowerCase())).size >= 2;
}) || null;
"""


async def dismiss_uber_popups(stagehand: Stagehand, page: Page) -> None:
    await page_helpers.maybe_dismiss_popups(
        stagehand,
        page,
        "cookie banner, promotional popup, Uber One trial offer, app-install banner, or delivery building-type / "
        "dropoff-details prompt ('Let us know your building type')",
        caution="Do NOT click 'Sign in' or 'Continue' on an order. For the building-type prompt only, 'Skip', "
        "'Not now' or 'Save' are fine; never edit or delete a saved address. ",
        selectors='[role="dialog"], [aria-modal="true"], [class*="cookie" i], [id*="cookie" i], '
        '[class*="promo" i], [class*="banner" i]',
    )


async def login_pause(page: Page, message: str, interactive: bool) -> bool:
    """Let the user sign in (headed runs only); True once the browser has left Uber's login pages."""
    return await pause_for_login(page, message, interactive) and not is_login_url(await page.url())


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
                safeClick(el);
                return 'Clicked autocomplete suggestion: ' + txt(el).split('\n')[0];
            }
        }
    }

    // 2. A 'Save and continue' / 'Done' / 'Save' confirmation button in the dropoff-options dialog
    const openDialogs = dialogs().filter(d => vis(d, 200, 150));
    for (const d of openDialogs) {
        const confirmBtn = Array.from(d.querySelectorAll('button')).find(
            b => vis(b, 60, 25) && /^(save( and continue)?|done|update|confirm( address)?)$/i.test(clean(txt(b))) && guardAllows(b));
        if (confirmBtn) {
            safeClick(confirmBtn);
            return 'Clicked address confirmation button: ' + clean(txt(confirmBtn));
        }
    }

    // 3. A visible address input: clear stale text and fill it
    for (const inp of document.querySelectorAll(
        'input[placeholder*="address" i], input[aria-label*="address" i], input[placeholder*="Deliver" i], [role="dialog"] input[type="text"], [role="dialog"] input:not([type])')) {
        if (!vis(inp, 100, 20)) continue;
        if (inp.type === 'email' || inp.type === 'password'
            || /e-?mail/i.test((inp.placeholder || '') + ' ' + (inp.getAttribute('aria-label') || ''))) continue;
        if (hasTokens(inp.value || '')) return 'Waiting for autocomplete suggestions...';
        if (inp.value && !hasTokens(inp.value)) {
            const box = inp.closest('[role="dialog"], [aria-modal="true"]') || inp.parentElement?.parentElement;
            const clear = box?.querySelector('button[aria-label*="clear" i], button[aria-label*="reset" i]');
            if (clear) safeClick(clear);
        }
        safeFill(inp, target);
        return 'Typed target address into input: ' + target;
    }

    // If the location-manager dialog is already open (e.g. 'Choose your building' or dropoff options),
    // return null so Stagehand can handle the dialog instead of re-clicking the header button behind it
    if (openDialogs.length > 0 || location.href.includes('mod=locationManager')) {
        return null;
    }

    // 4. The header delivery-address button, to open the address modal
    for (const btn of document.querySelectorAll(
        'header button, header a, [data-testid*="location" i], [data-testid*="address" i], button[aria-label*="deliver" i]')) {
        const label = (txt(btn) + ' ' + (btn.getAttribute('aria-label') || '')).trim();
        if (vis(btn, 50, 20) && /deliver|address|now|•|\d+\s+[a-z]/i.test(label) && !/cart|bag|sign\s*in|log\s*in|search/i.test(label)) {
            safeClick(btn);
            return 'Clicked header address button: ' + txt(btn).split('\n')[0];
        }
    }
    return null;
}"""

# One step past Uber's location-manager popups: the 'building type' / dropoff-details prompt, and (only once the
# header shows the target address) a leftover 'Addresses' dialog. Skipping is preferred so nothing is saved to the
# account; never anything that edits or deletes a saved address. Returns what was done, 'STUCK: ...' when the
# prompt is open but offers nothing to click, or null when neither dialog is open.
JS_DISMISS_LOCATION_PROMPT = r"""({ addressConfirmed }) => {
    const SKIP = /^(skip|not now|maybe later|no,? thanks|later)$/i;
    const SAVE = /^(save( and continue| address)?|done|continue|confirm)$/i;
    const EDITS = /edit|delete|remove|change|add (a |new )?address|\$\s*\d/i;
    const disabled = b => b.disabled || b.getAttribute('aria-disabled') === 'true';
    const buttons = d => Array.from(d.querySelectorAll('button, [role="button"]')).filter(b => vis(b, 20, 15));
    const closeControl = d => buttons(d).find(b => guardAllows(b) && (
        /close|dismiss/i.test(b.getAttribute('aria-label') || '') || (!clean(txt(b)) && b.querySelector('svg'))));

    const prompt = locationPrompt();
    if (prompt) {
        const all = buttons(prompt);
        const skip = all.find(b => SKIP.test(clean(txt(b))) && !disabled(b) && guardAllows(b));
        if (skip) { safeClick(skip); return 'clicked ' + clean(txt(skip)); }
        const saves = all.filter(b => SAVE.test(clean(txt(b))) && guardAllows(b));
        const save = saves.find(b => !disabled(b));
        if (save) { safeClick(save); return 'clicked ' + clean(txt(save)); }
        if (saves.length || all.some(b => SKIP.test(clean(txt(b))))) {
            // Save waits for a building type: choose the first one offered
            for (const el of prompt.querySelectorAll('[role="radio"], input[type="radio"], [role="option"], li, button')) {
                const target = el.matches('input') ? (el.closest('label') || (el.id && prompt.querySelector(`label[for="${el.id}"]`)) || el) : el;
                const label = clean(txt(target) || el.getAttribute('aria-label') || el.value || '');
                if (!vis(target, 5, 5) || SKIP.test(label) || SAVE.test(label) || EDITS.test(label)) continue;
                if (el.matches('li, button') && (!label || el.querySelector('button, li, input'))) continue;
                if ((el.checked || el.getAttribute('aria-checked') === 'true' || el.getAttribute('aria-selected') === 'true')) continue;
                if (!guardAllows(target)) continue;
                safeClick(target);
                return 'chose option ' + (label || '(unlabelled)');
            }
        }
        const close = closeControl(prompt);
        if (close) { safeClick(close); return 'clicked close (X)'; }
        return 'STUCK: ' + clean(txt(prompt)).slice(0, 160);
    }

    // A leftover 'Addresses' dialog over a header that already shows the target address: close it, never type in it
    if (!addressConfirmed) return null;
    for (const d of dialogs().filter(d => vis(d, 200, 150)).reverse()) {
        const heading = d.querySelector('h1, h2, h3, [role="heading"]');
        const isAddresses = (heading && /^addresses$/i.test(clean(txt(heading))))
            || (location.href.includes('mod=locationManager') && d.querySelector('input'));
        if (!isAddresses) continue;
        const close = closeControl(d);
        if (close) { safeClick(close); return 'closed the Addresses dialog'; }
    }
    return null;
}"""

# Labels of the header's delivery-address control(s), so the address can be checked without the model
JS_HEADER_ADDRESS = r"""() => {
    const out = [];
    for (const el of document.querySelectorAll(
        'header button, header a, [data-testid*="location" i], [data-testid*="address" i], button[aria-label*="deliver" i]')) {
        if (el.closest('[role="dialog"], [aria-modal="true"], [role="listbox"]') || !vis(el, 50, 20)) continue;
        const label = clean(txt(el) + ' ' + (el.getAttribute('aria-label') || ''));
        if (label && label.length < 160 && !/cart|bag|sign\s*in|log\s*in|search/i.test(label)) out.push(label);
    }
    return out;
}"""

# The open item modal: the dialog holding the 'Add to order' button
JS_MARK_ITEM_MODAL = r"""() => {
    const btn = addButton();
    return markScope(btn && btn.closest('[role="dialog"], [aria-modal="true"]'));
}"""

# The open cart drawer, but only when nothing else is open beside it (an upsell or item modal must be seen)
JS_MARK_CART_DRAWER = r"""() => {
    const drawer = cartDrawer();
    if (!drawer || addButton()) return markScope(null);
    const others = dialogs().filter(d => vis(d, 120, 80) && !drawer.contains(d) && !d.contains(drawer));
    return markScope(others.length ? null : drawer);
}"""

# Open a menu item's modal by clicking the card whose title is exactly the item's name (never a '+' quick-add)
JS_CLICK_MENU_ITEM = r"""({ name, price }) => {
    const norm = s => clean(s).toLowerCase();
    const want = norm(name);
    const cents = (price || '').replace(/[^\d.]/g, '');
    const firstLine = el => norm(txt(el).split('\n')[0]);
    const cards = [];
    for (const el of (document.querySelector('main') || document.body).querySelectorAll('h1, h2, h3, h4, h5, h6, span, p, div')) {
        if (el.closest('[role="dialog"], [aria-modal="true"], header, footer') || !vis(el, 20, 8)) continue;
        if (firstLine(el) !== want || Array.from(el.children).some(c => firstLine(c) === want)) continue;
        const card = el.closest('a[href], button, [role="button"]') || el.closest('li');
        if (card && vis(card, 40, 20) && !cards.includes(card)) cards.push(card);
    }
    const card = cards.find(c => cents && txt(c).includes(cents)) || cards[0];
    if (!card) return null;
    card.scrollIntoView({ block: 'center', behavior: 'instant' });
    return safeClick(card) ? clean(txt(card)).slice(0, 80) : null;
}"""

# Why the open store page cannot take an order now (closed, opens later, scheduled orders only, or outside the
# menu's hours), or null. Only the store header and the menu selector in the sidebar are read, so a menu
# section's own hours ('Breakfast available from 7am', a section heading's '7:00 am - 11:00 am') do not count.
# The menu's hours ('Hackett Uber Menu · 12:00 pm - 2:29 pm') are compared with the browser's clock: the store
# is open when any range shown covers now, and a range that ends before it starts runs past midnight.
JS_STORE_CLOSED = r"""() => {
    const main = document.querySelector('main') || document.body;
    const heading = main.querySelector('h1');
    const top = heading ? heading.getBoundingClientRect().top + window.scrollY : 0;
    // The store's menu selector: Uber's desktop sidebar, else any side navigation
    const SIDEBAR = '#store-desktop-vertical-menu-nav, [id*="menu-nav" i], aside, nav';
    const TIME = '(\\d{1,2})(?::(\\d{2}))?\\s*([ap])\\.?\\s*m\\.?';
    const RANGE = new RegExp(TIME + '\\s*(?:[-\u2013\u2014]|to)\\s*' + TIME, 'gi');
    const minutes = (h, m, ap) => (parseInt(h, 10) % 12 + (/p/i.test(ap) ? 12 : 0)) * 60 + (parseInt(m || '0', 10));
    const lines = [], hours = [], ranges = [];
    for (const el of main.querySelectorAll('p, span, div, h2, h3, button')) {
        if (el.children.length || el.closest('[role="dialog"], [aria-modal="true"], li') || !vis(el, 10, 6)) continue;
        const inHeader = el.getBoundingClientRect().top + window.scrollY <= top + 700;
        if (!inHeader && !el.closest(SIDEBAR)) continue;
        const t = clean(txt(el));
        if (!t || t.length >= 120) continue;
        if (inHeader && STORE_CLOSED.test(t)) lines.push(t);
        // An hours line: nothing but time ranges, perhaps after the menu's name and a dot ('Lunch \u00b7 12:00 pm \u2013 2:29 pm')
        const bare = t.replace(/^[^\u00b7\u2022]{1,60}[\u00b7\u2022]\s*(?=\d)/, '');
        const found = [...bare.matchAll(RANGE)];
        if (!found.length || bare.replace(RANGE, '').replace(/[\s,;&|\u00b7\u2022]|and/gi, '')) continue;
        hours.push(bare);
        for (const m of found) ranges.push([minutes(m[1], m[2], m[3]), minutes(m[4], m[5], m[6])]);
    }
    if (lines.length) return lines[0];
    if (!ranges.length) return null;
    const d = new Date();
    const now = d.getHours() * 60 + d.getMinutes();
    const covers = ([from, to]) => from === to || (from < to ? now >= from && now <= to : now >= from || now <= to);
    return ranges.some(covers) ? null : 'menu hours ' + [...new Set(hours)].join('; ');
}"""

# The open item popup when it offers no 'Add to order' button, or null when no popup is open (or it can add).
# A popup is a visible dialog; when Uber's popup carries no dialog role but the URL says one is open
# (?mod=quickView), it is the box at the middle of the screen. Returns {kind, ...}:
#   new_order: Uber's 'another store' prompt (text); button: its main button (label, disabled);
#   text: no usable button, a sample of what it shows (text); unrecognised: the URL says open, nothing found.
JS_ITEM_DIALOG_BLOCKER = r"""() => {
    if (addButton()) return null;
    const quickView = /[?&]mod=quickView\b/i.test(location.search);
    const usable = d => Array.from(d.querySelectorAll('button, [role="button"]')).filter(b => vis(b, 10, 10) && clean(txt(b)));
    const open = dialogs().filter(d => vis(d, 150, 80)).reverse();
    for (const d of open) {
        if (NEW_ORDER_PROMPT.test(clean(txt(d))) && usable(d).length) return { kind: 'new_order', text: clean(txt(d)).slice(0, 120) };
    }
    let popup = open.find(d => vis(d, 200, 150)) || null;
    if (!popup && quickView) {
        // The innermost box around the middle of the screen that is smaller than the screen (not the backdrop)
        let el = document.elementFromPoint(innerWidth / 2, innerHeight / 2);
        for (; el && el !== document.body; el = el.parentElement) {
            const r = el.getBoundingClientRect();
            if (r.width >= innerWidth * 0.95 && r.height >= innerHeight * 0.95) break;
            if (vis(el, 200, 150)) popup = el;
        }
    }
    if (!popup) return quickView ? { kind: 'unrecognised' } : null;
    const buttons = usable(popup);
    // The main button is the widest one (the bottom call to action); of equal ones, the last
    const main = buttons.reduce((best, b) =>
        !best || b.getBoundingClientRect().width >= best.getBoundingClientRect().width ? b : best, null);
    if (main && vis(main, 60, 20)) {
        const disabled = main.disabled || main.getAttribute('aria-disabled') === 'true';
        return { kind: 'button', label: clean(txt(main)).slice(0, 80), disabled };
    }
    return { kind: 'text', text: clean(txt(popup)).slice(0, 120) };
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
    return !!btn && safeClick(btn);
}"""

CART_DRAWER_JS = r"""
// The open cart drawer (never the item modal or a confirmation prompt), or null
const cartDrawer = () => [...dialogs(), ...document.querySelectorAll('aside')].find(
    d => vis(d, 220, 250) && /checkout|subtotal|clear cart|your (order|cart)|^\s*carts\b|\bview cart\b/im.test(d.innerText || ''));
// 'Clear cart' / 'Delete cart' and the like, exactly: a menu entry or button that empties a whole cart
const CLEAR_CART = /^(clear|delete|remove|empty)( this| the)? (cart|order|basket)$|^(clear|remove|delete) all( items)?$/i;
// The '...' button that opens a cart's menu (where Uber keeps 'Clear cart' / 'Delete cart')
const MORE = /\b(more|options|actions|menu|overflow)\b|^\s*(…|\.\.\.|⋯|⋮)\s*$/i;
// What the open cart panels show, for the log when the cart could not be cleared
const JS_CART_PANELS_TEXT = () => [...dialogs(), ...document.querySelectorAll('aside')].filter(d => vis(d, 150, 100))
    .map(d => clean(txt(d)).slice(0, 200)).join(' | ');
const ctrlLabel = b => [b.getAttribute('aria-label'), b.getAttribute('title'), b.getAttribute('data-testid'),
    ...Array.from(b.querySelectorAll('svg title, [aria-label]'), n => n.textContent || n.getAttribute('aria-label'))]
    .filter(Boolean).join(' ');
const INCREASE = /increase|increment|\bplus\b|\badd\b|\bmore\b/i;
const REMOVE = /remove|delete|trash|bin\b|decrease|decrement|minus|less\b/i;
// A quantity stepper: a leaf holding just the quantity, next to the stepper's buttons
const stepperQty = el => Array.from(el.querySelectorAll('span, div, p')).find(
    n => n.children.length === 0 && /^\d{1,2}$/.test(txt(n)) && !!n.parentElement && !!n.parentElement.querySelector('button'));
const PRICE = /\$\s*\d/;
// Rows of the cart: list items in the drawer that show a price; when the drawer has none (its rows are
// <div>s), the smallest elements that hold both a price and a quantity stepper
const cartRows = drawer => {
    const lis = Array.from(drawer.querySelectorAll('li')).filter(
        li => vis(li, 60, 20) && PRICE.test(li.innerText || '') && !li.querySelector('li'));
    if (lis.length) return lis;
    const rows = Array.from(drawer.querySelectorAll('div, section, article')).filter(
        el => vis(el, 60, 20) && PRICE.test(el.innerText || '') && stepperQty(el));
    return rows.filter(r => !rows.some(o => o !== r && r.contains(o)));
};
// Uber's wording when an item is added while the cart holds another store's items
const NEW_ORDER_PROMPT = /new order|start new|items from|another (store|restaurant)/i;
const NEW_ORDER_YES = /^(new order|start new order|create new order|continue)$/i;
// The store a cart belongs to, as the open cart panel shows it: its first heading that is not the panel's own title
const cartStoreName = () => {
    const panel = cartDrawer() || [...dialogs(), ...document.querySelectorAll('aside')].find(d => vis(d, 150, 100));
    if (!panel) return null;
    const heading = Array.from(panel.querySelectorAll('h1, h2, h3, h4, [data-testid*="store" i]')).map(h => clean(txt(h)))
        .find(t => t && t.length < 80 && !/^(your (cart|order|basket)|carts?|cart|subtotal|order summary)$/i.test(t));
    return heading || null;
};
"""

JS_CART_ITEMS_IN_DRAWER = r"""() => {
    const drawer = cartDrawer();
    if (!drawer) return null;
    let total = 0;
    for (const row of cartRows(drawer)) {
        const sel = row.querySelector('select');
        const stepper = stepperQty(row);
        const q = parseInt(sel ? sel.value : (stepper ? txt(stepper) : '1'), 10);
        total += Number.isFinite(q) && q > 0 ? q : 1;
    }
    return total;
}"""

JS_CLEAR_CART_STEP = r"""() => {
    // 1. A 'Remove item?' / 'Clear cart?' confirmation prompt on top of the drawer
    for (const d of dialogs().filter(d => vis(d, 150, 80) && !/subtotal|checkout/i.test(d.innerText || '')).reverse()) {
        const yes = Array.from(d.querySelectorAll('button')).find(
            b => vis(b, 30, 20) && /^(remove( item)?|delete( item)?|clear( cart)?|empty cart|yes,? (remove|clear|delete)\b.*)$/i.test(clean(txt(b))));
        if (yes) { safeClick(yes); return 'confirm'; }
    }

    // 1b. An open cart menu (from the '...' button): its 'Clear cart' / 'Delete cart' entry
    const entries = Array.from(document.querySelectorAll(
        '[role="menu"] *, [role="menuitem"], [role="listbox"] *, [role="option"], [data-baseweb="popover"] *, [role="dialog"] li, [role="dialog"] button'));
    const entry = entries.find(e => vis(e, 30, 12) && CLEAR_CART.test(clean(txt(e)))
        && !Array.from(e.children).some(c => CLEAR_CART.test(clean(txt(c)))));
    if (entry) { safeClick(entry.closest('button, [role="menuitem"], [role="option"], li') || entry); return 'menu_clear'; }

    // Scope strictly to the open cart drawer so page/header buttons are never clicked
    const drawer = cartDrawer();
    if (!drawer) return 'no_drawer';
    const buttons = Array.from(drawer.querySelectorAll('button')).filter(b => vis(b, 8, 8) && !b.disabled);

    // 2. A 'Clear cart' / 'Remove all' button
    const clear = buttons.find(b => /^(clear cart|empty cart|remove all|clear all)$/i.test(clean(txt(b)))
        || /^(clear|empty) cart$/i.test(ctrlLabel(b).trim()));
    if (clear) { safeClick(clear); return 'clear_button'; }

    // 3. A quantity <select> that can be set to 0 / 'Remove'
    for (const sel of drawer.querySelectorAll('select')) {
        const zero = Array.from(sel.options).find(o => o.value === '0' || /^remove$/i.test((o.text || '').trim()));
        if (zero && sel.value !== zero.value) { safeSelect(sel, zero.value); return 'select_zero'; }
    }

    // 4. A labelled remove / delete / trash / decrease control (not the '+' next to it)
    const labelled = buttons.find(b => {
        const label = ctrlLabel(b) + ' ' + clean(txt(b));
        return REMOVE.test(label) && !INCREASE.test(label);
    });
    if (labelled) { safeClick(labelled); return 'remove_button'; }

    // 5. An unlabelled icon-only button in a cart row: the first one of the quantity stepper is the
    //    trash can (quantity 1) or the '-' (quantity > 1). The '+' comes after it, so never pick the last
    //    of two or more.
    for (const row of cartRows(drawer)) {
        const icons = Array.from(row.querySelectorAll('button')).filter(
            b => vis(b, 8, 8) && !b.disabled && !clean(txt(b)).replace(/[−–-]/g, '') && b.querySelector('svg, img')
                && !INCREASE.test(ctrlLabel(b)));
        if (icons.length) { safeClick(icons[0]); return 'stepper_button'; }
    }

    // 6. The drawer's '...' menu, which holds 'Clear cart' (opened at most a few times per page)
    window.__auditCartMenus = (window.__auditCartMenus || 0) + 1;
    const more = window.__auditCartMenus <= 3 && buttons.find(b => MORE.test(ctrlLabel(b) + ' ' + clean(txt(b))));
    if (more) { safeClick(more); return 'menu_open'; }
    return null;
}"""

# One way of closing the open cart drawer ('x': its own close control; 'backdrop': the page beside it).
# Returns what was clicked, or null.
JS_CLOSE_CART_DRAWER = r"""how => {
    const drawer = cartDrawer();
    if (!drawer || addButton()) return null;
    if (how === 'x') {
        const top = drawer.getBoundingClientRect().top;
        const rows = cartRows(drawer);
        const buttons = Array.from(drawer.querySelectorAll('button')).filter(
            b => vis(b, 8, 8) && !b.disabled && !rows.some(r => r.contains(b)) && !b.closest('li'));
        const labelled = buttons.find(b => /close|dismiss/i.test(ctrlLabel(b)));
        const icon = buttons.find(b => !clean(txt(b)) && b.querySelector('svg, img')
            && b.getBoundingClientRect().top - top < 80
            && !MORE.test(ctrlLabel(b)) && !INCREASE.test(ctrlLabel(b)) && !REMOVE.test(ctrlLabel(b)));
        const btn = labelled || icon;
        if (btn && safeClick(btn)) return labelled ? 'close_button' : 'x_icon';
        return null;
    }
    // The backdrop: a point on the side of the viewport away from the drawer, never a control
    const r = drawer.getBoundingClientRect();
    const x = r.left > 120 ? r.left / 2 : (r.right + window.innerWidth) / 2;
    const el = document.elementFromPoint(x, window.innerHeight / 2);
    if (!el || drawer.contains(el) || el.closest(AUDIT_ACTIONABLE)) return null;
    return safeClick(el) ? 'backdrop' : null;
}"""

# What a cart clear that left items behind leaves for the run folder: the open panel's HTML (cut short)
JS_CART_DRAWER_HTML = r"""limit => {
    const panel = cartDrawer() || [...dialogs(), ...document.querySelectorAll('aside')].find(d => vis(d, 150, 100));
    return panel ? panel.outerHTML.slice(0, limit) : null;
}"""

# Uber's 'Create new order?' prompt (the cart holds another store's items): click its confirming button.
# Returns the button's label, or null when no such prompt is open.
JS_CONFIRM_NEW_ORDER = r"""() => {
    for (const d of dialogs().filter(d => vis(d, 150, 80)).reverse()) {
        if (!NEW_ORDER_PROMPT.test(clean(txt(d)))) continue;
        const yes = Array.from(d.querySelectorAll('button')).find(
            b => vis(b, 30, 20) && NEW_ORDER_YES.test(clean(txt(b))) && guardAllows(b));
        if (yes && safeClick(yes)) return clean(txt(yes));
    }
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
        safeClick(target);
        if (bestLabel) chosen.push(bestLabel);
    }
    return chosen;
}"""

JS_CLICK_ADD = r"""() => {
    const btn = addButton();
    if (!btn) return { clicked: false, price: null };
    const m = txt(btn).match(/(A?\$[\d.,]+)/);
    return { clicked: safeClick(btn), price: m ? m[1] : null };
}"""

JS_CLICK_CHECKOUT = r"""() => {
    // Exact labels only: 'Checkout · Pay $54.20' or 'Checkout with PayPal' must never match
    const el = Array.from(document.querySelectorAll('a[href*="/checkout"], button')).find(
        e => /^(Go to checkout|Checkout|Continue to checkout)$/i.test(clean(txt(e))) && guardAllows(e));
    return !!el && safeClick(el);
}"""

JS_DECLINE_UPSELL = r"""() => {
    // Only buttons inside an open dialog, and only ones that decline / move on (never add, never place the order,
    // and never submit a <form> on a checkout page)
    const DECLINE = /^(skip|no,? thanks?|not now|maybe later|continue( to checkout)?|go to checkout|next|done)$/i;
    for (const d of dialogs().filter(d => vis(d, 200, 150)).reverse()) {
        const closeBtn = d.querySelector('button[aria-label*="close" i]');
        if (closeBtn && vis(closeBtn, 20, 20) && !closeBtn.form && guardAllows(closeBtn)) {
            safeClick(closeBtn);
            return 'Close (X)';
        }
        const btn = Array.from(d.querySelectorAll('button, a')).find(
            b => vis(b, 30, 20) && !b.form && DECLINE.test(clean(txt(b))) && guardAllows(b));
        if (btn) { safeClick(btn); return clean(txt(btn)); }
    }
    return null;
}"""

JS_FIND_STORE = r"""({ preferred, excluded, skip }) => {
    const norm = s => (s || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
    const pref = norm(preferred);
    const storePath = a => { try { return new URL(a.href).pathname; } catch (e) { return a.href; } };
    // The whole card: the largest ancestor whose store links all lead to this one store
    const cardOf = a => {
        const path = storePath(a);
        let card = a;
        for (let el = a.parentElement; el && el !== document.body; el = el.parentElement) {
            if (Array.from(el.querySelectorAll('a[href*="/store/"]')).some(o => storePath(o) !== path)) break;
            card = el;
        }
        return card;
    };
    const FEE = /(A?\$\s?\d+(?:\.\d{1,2})?\s*Delivery\s*Fee\^?|Delivery\s*Fee:?\s*A?\$\s?\d+(?:\.\d{1,2})?|Free\s*Delivery)/i;
    const found = [];
    for (const a of document.querySelectorAll('a[href*="/store/"]')) {
        const heading = a.querySelector('h1, h2, h3, h4, [data-testid*="store" i]') || a;
        const name = (heading.innerText || a.getAttribute('aria-label') || a.innerText || '').split('\n')[0].trim();
        const low = name.toLowerCase();
        if (!name || !vis(a, 40, 20)) continue;
        if (excluded.some(bad => low.includes(bad) || a.href.toLowerCase().includes('/store/' + bad))) continue;
        if ((skip || []).includes(storePath(a))) continue;
        const cardText = clean(cardOf(a).innerText);
        const feeMatch = cardText.match(FEE);
        found.push({
            name,
            url: a.href,
            deliveryFee: feeMatch ? feeMatch[1] : null,
            closed: STORE_CLOSED.test(cardText),
            match: !!pref && norm(name).includes(pref)
        });
    }
    return found.find(c => c.match) || found.find(c => !c.closed) || found[0] || null;
}"""


js = functools.partial(page_helpers.js, lib=UBER_JS + CART_DRAWER_JS)

# The element markScope() tagged. An extraction limited to it reads that dialog or drawer, not the whole
# store page behind it (a full menu is tens of thousands of tokens; a modal is a few thousand).
SCOPE = '[data-audit-scope="1"]'


async def scoped_extract(
    stagehand: Stagehand, page: Page, mark_js: str, instruction: str, schema: type[ModelT], scope_note: str
) -> ModelT:
    """extract() limited to the element `mark_js` tags, or of the whole page when it tags nothing.

    A scoped extraction that fails (the element went away mid-call) is asked again of the whole page.
    """
    if await js(page, mark_js, default=False):
        try:
            return (await stagehand.extract(f"{instruction} {scope_note}", schema, locator=page.locator(SCOPE))).data
        except ExtractFailed as error:
            log.debug("Scoped %s extraction failed (%s); reading the whole page", schema.__name__, error)
    return (await stagehand.extract(instruction, schema)).data


async def is_item_modal_open(page: Page) -> bool:
    return bool(await js(page, "() => !!addButton()", default=False))


async def item_popup_blocker(page: Page) -> str | None:
    """Why the open item popup offers no usable 'Add to order' button, or None when no popup is open."""
    found = await js(page, JS_ITEM_DIALOG_BLOCKER, default=None)
    if not isinstance(found, dict):
        return None
    kind = found.get("kind")
    if kind == "new_order":
        return f"a cart from another store blocks this item (Uber asked: '{found.get('text')}')"
    if kind == "button" and found.get("disabled"):
        return f"item popup's '{found.get('label')}' button is disabled"
    if kind == "button":
        return f"item popup offers '{found.get('label')}' instead of 'Add to order'"
    if kind == "text":
        return f"item popup has no usable button (it shows: '{found.get('text')}')"
    return "item popup is open (URL mod=quickView) but its content was not recognised"


def url_mod(url: str) -> str | None:
    """The `mod=` parameter Uber sets while a popup is open (`quickView` for an item), or None."""
    return parse_qs(urlparse(url).query).get("mod", [None])[0]


async def item_popup_open_without_add(page: Page) -> bool:
    """True when Uber says an item popup is open (`?mod=quickView`) but it offers no usable 'Add to order'."""
    return url_mod(await page.url()) == "quickView" and bool(await item_popup_blocker(page))


async def store_closed_reason(page: Page) -> str | None:
    """Why the open store page cannot take an order now: the header's 'Closed' / 'Opens at ...' wording, or
    'menu hours ...' when the browser's clock is outside every hours range the menu selector shows."""
    why = await js(page, JS_STORE_CLOSED, default=None)
    return why if isinstance(why, str) and why else None


async def get_cart_count(page: Page) -> int:
    count = await js(page, JS_CART_COUNT, default=0)
    return count if isinstance(count, int) else 0


async def open_cart_drawer(page: Page) -> bool:
    return bool(await js(page, JS_OPEN_CART, default=False))


async def close_item_modal(stagehand: Stagehand, page: Page) -> None:
    """Ensure no lingering item modal blocks the rest of the page."""
    if not await is_item_modal_open(page):
        return
    log.info("    Closing lingering item modal...")
    await js(page, "() => { pressEsc(); closeDialog(); }")
    await settle(page, 1000)
    if await is_item_modal_open(page):
        await try_act(stagehand, page, "Click the 'X' close button in the top-left corner of the open item modal.")
        await settle(page, 1000)


MAX_CART_CLEAR_STEPS = 25
MAX_CART_CLEAR_LLM_STEPS = 4
CLEAR_CART_INSTRUCTION = (
    "Empty the Uber Eats cart. If a cart drawer is open, click the trash-can / remove button (or '-' quantity "
    "button) of its first item, or 'Clear cart' / 'Delete cart'. If a list of carts by store is shown, open the "
    "'...' menu of the first cart and choose 'Delete cart' / 'Clear cart'; if that menu is already open, click "
    "that entry. If no cart is open, click the cart button in the header. "
    "Do NOT click 'Go to checkout', 'View cart', a store name or '+'."
)


DRAWER_HTML_LIMIT = 50_000


class CartLeft(NamedTuple):
    """What clear_existing_cart left behind: items still in the cart, and the store the cart drawer named."""

    items: int
    store: str | None


async def items_left_in_cart(page: Page) -> int:
    """Items still in the cart: the larger of the open drawer's rows and the header badge.

    The rows can only add to the badge, never zero it: a drawer whose rows are not recognised counts none.
    """
    in_drawer = await js(page, JS_CART_ITEMS_IN_DRAWER)
    return max(in_drawer if isinstance(in_drawer, int) else 0, await get_cart_count(page))


async def is_cart_drawer_open(page: Page) -> bool:
    return bool(await js(page, "() => !!cartDrawer()", default=False))


async def close_cart_drawer(page: Page) -> bool:
    """Close the open cart drawer: its own X, then a real Escape key press, then a click on the page beside it.

    Stops as soon as the drawer is gone. True when no drawer is open at the end.
    """
    for how in ("x", "escape", "backdrop"):
        if not await is_cart_drawer_open(page):
            return True
        if how == "escape":
            with contextlib.suppress(*BROWSER_ERRORS):
                await page.key_press("Escape")
        elif not await js(page, JS_CLOSE_CART_DRAWER, how):
            continue
        log.debug("Closing the cart drawer: %s", how)
        await settle(page, 700)
    return not await is_cart_drawer_open(page)


def write_cart_clear_report(out_dir: Path, name: str, report: dict) -> None:
    with contextlib.suppress(OSError):  # the record is a diagnostic; a full disk must not end the run
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / name).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")


async def finish_cart_clear(
    page: Page, existing: int, steps: list[dict], out_dir: Path | None, report_name: str
) -> CartLeft:
    """Close the cart drawer and say what was left; a cart not emptied is recorded in `out_dir`/`report_name`."""
    left = await items_left_in_cart(page)
    panels = await js(page, "() => JS_CART_PANELS_TEXT()", default="") if left else ""
    store = await js(page, "() => cartStoreName()", default=None) if left else None
    drawer_html = await js(page, JS_CART_DRAWER_HTML, DRAWER_HTML_LIMIT, default=None) if left else None
    closed = await close_cart_drawer(page)
    await settle(page, 800)
    left = await get_cart_count(page)
    if left:
        log.warning(f"  {left} leftover item(s) could not be removed; the checkout subtotal may include them.")
        if panels:
            log.warning(f"  The cart showed: {panels}")
        if out_dir is not None:
            write_cart_clear_report(
                out_dir,
                report_name,
                {
                    "items_before": existing,
                    "items_left": left,
                    "store": store,
                    "steps": steps,
                    "drawer_closed": closed,
                    "panels_text": panels,
                    "drawer_html": drawer_html,
                },
            )
    else:
        log.info("  Cart cleared.")
    if not closed:
        log.warning("  The cart drawer could not be closed; it may cover the menu.")
    return CartLeft(left, store if left else None)


async def clear_existing_cart(
    stagehand: Stagehand, page: Page, out_dir: Path | None = None, report_name: str = "cart_clear.json"
) -> CartLeft:
    """If the persistent profile has leftover items in the cart, clear them inside the cart drawer.

    Uber Eats re-renders (and often closes) the drawer after each removal, so the drawer is reopened
    whenever it is gone and every step works from the page as it is now. When items are left, the steps
    taken and the drawer's HTML are written to `out_dir`/`report_name`, so the next layout change can be
    fixed from the run folder alone.
    """
    existing = await get_cart_count(page)
    if existing <= 0:
        await close_cart_drawer(page)
        return CartLeft(0, None)
    log.info(f"  Detected {existing} leftover item(s) in cart from a previous session; clearing cart...")

    steps: list[dict] = []
    llm_steps = 0
    unrecognised_opens = 0
    for _ in range(MAX_CART_CLEAR_STEPS):
        step = await js(page, JS_CLEAR_CART_STEP)
        taken = step
        if step == "no_drawer":
            if await get_cart_count(page) == 0:
                steps.append({"step": step, "left": 0})
                break
            # The cart button was clicked but nothing the page steps know opened (a list of carts by store,
            # say): the model reads the page and takes one step instead of the button being clicked again
            if unrecognised_opens < 2:
                if not await open_cart_drawer(page):
                    log.debug("Cart button not found while clearing the cart")
                    steps.append({"step": "no_cart_button", "left": await get_cart_count(page)})
                    break
                unrecognised_opens += 1
                await settle(page, 1500)
                steps.append({"step": "open_cart", "left": await get_cart_count(page)})
                continue
        if step and step != "no_drawer":
            log.debug("Cart clear step: %s", step)
            unrecognised_opens = 0  # the drawer was there; it may close again after this removal
        else:
            if step is None and await items_left_in_cart(page) == 0:
                steps.append({"step": "none_left", "left": 0})
                break
            llm_steps += 1
            if llm_steps > MAX_CART_CLEAR_LLM_STEPS:
                break
            taken = "llm" if await try_act(stagehand, page, CLEAR_CART_INSTRUCTION) else "llm_failed"
        await settle(page, 1000)
        steps.append({"step": taken, "left": await items_left_in_cart(page)})

    return await finish_cart_clear(page, existing, steps, out_dir, report_name)


async def accept_new_order_prompt(page: Page) -> bool:
    """Confirm Uber's 'Create new order?' prompt (the cart holds another store's items), if it is open.

    This replaces the old store's cart with this store's, so a leftover cart cannot block adding items.
    """
    clicked = await js(page, JS_CONFIRM_NEW_ORDER, default=None)
    if not clicked:
        return False
    log.info(f"    The cart held another store's items; clicked '{clicked}' to start a new order")
    await settle(page, 1500)
    return True


# ---------------------------------------------------------------------------
# Uber Eats: flow
# ---------------------------------------------------------------------------
ADDRESS_QUESTION = (
    "Look at the top header bar and any open address modal. Report the exact delivery address text currently shown "
    "in the header (`displayed_address`), whether an address input dialog is open (`address_modal_or_input_open`), "
    "whether autocomplete suggestions are visible (`address_suggestions_open`), and any `blocked_reason`. "
    "A 'Let us know your building type' / dropoff-details prompt counts as an open address dialog."
)
FEED_QUESTION = (
    "Extract up to 8 prepared-food restaurants visible near the top of this page with their advertised delivery fee, "
    "service fee, ETA, availability, and `/store/...` link. "
    "Exclude supermarkets, grocery stores, convenience stores, and liquor stores (e.g. Coles, Woolworths, 7-Eleven, BWS)."
)


def address_instruction(state: AddressAndFeedState, address: str) -> str:
    """What to ask the LLM to do next when the DOM snippet could not move the address forward."""
    if state.address_suggestions_open:
        return f"Click the first autocomplete address suggestion matching '{address}' (or click 'Done' / 'Save')."
    if state.address_modal_or_input_open:
        return (
            f"In the open address dialog: if an address input box is visible, clear it and type '{address}'; "
            "if a 'Choose your building', 'Let us know your building type' or dropoff-options step is shown, "
            "click 'Skip' / 'Not now' if offered, else select the first building type/option and click "
            "'Save and continue' / 'Save' / 'Done'. Never edit or delete a saved address."
        )
    return (
        f"Click the current delivery address button ('{state.displayed_address or 'Enter delivery address'}') "
        f"in the top header so we can change the delivery address to '{address}'."
    )


LOCATION_PROMPT_TRIES = 3
STUCK = "STUCK: "


async def location_prompt_open(page: Page) -> bool:
    return bool(await js(page, "() => !!locationPrompt()", default=False))


async def dismiss_location_prompt(page: Page, out: Path | None = None, address_confirmed: bool = False) -> bool:
    """Get past Uber's 'building type' popup (and, once `address_confirmed`, a leftover 'Addresses' dialog).

    Up to LOCATION_PROMPT_TRIES steps, so 'choose an option' then 'Save' can both happen; if the prompt is
    still open after that, Escape is pressed. True if anything was done. A prompt that offers nothing to
    click is saved as a screenshot to `out` (its labels were never captured; keep the regexes honest).
    """
    acted = False
    for _ in range(LOCATION_PROMPT_TRIES):
        step = await js(page, JS_DISMISS_LOCATION_PROMPT, {"addressConfirmed": address_confirmed})
        if not step:
            return acted
        if step.startswith(STUCK):
            log.info(f"  Location prompt: found but nothing to click ({step.removeprefix(STUCK)})")
            if out is not None:
                await shot(page, out / "location-prompt.png")
            break
        log.info(f"  Location prompt: {step}")
        acted = True
        await settle(page, 1200)
    if await location_prompt_open(page):
        log.info("  Location prompt: still open; pressing Escape")
        await page.key_press("Escape")
        await settle(page, 800)
        acted = True
    return acted


async def confirm_address_step(stagehand: Stagehand, page: Page, address: str, url: str, step_no: int) -> bool:
    """One round of making the header show `address`: True once it does, otherwise one step towards it.

    The header and the `pl=` URL parameter are read from the page first; the model is only asked to read
    the screen (the whole feed page, the costliest extraction of the run) when the page snippets can
    neither confirm the address nor move it forward.
    """
    # The building-type popup sits on top of a page whose header and pl= may already be right: it must be gone
    # before the address counts as set, or the store is picked and clicked behind it
    if await dismiss_location_prompt(page):
        url = await page.url()
    if await location_prompt_open(page):
        log.info(f"  Address step {step_no}: the building-type prompt is still open")
        return False
    pl_addr = extract_pl_address_from_url(url)
    shown = next(
        (label for label in await js(page, JS_HEADER_ADDRESS, default=[]) if addresses_match(label, address)), None
    )
    if shown or addresses_match(pl_addr, address):
        log.info(f"  Delivery address confirmed: {shown or pl_addr}")
        if shown:  # the header behind it is right, so a leftover 'Addresses' dialog only needs closing
            await dismiss_location_prompt(page, address_confirmed=True)
        return True

    dom_step = await js(page, JS_SET_ADDRESS, {"target": address, "tokens": street_tokens(address)})
    if dom_step:
        log.info(f"  Address step {step_no} (DOM): {dom_step}")
        await settle(page, 2200)
        return False

    state = (await stagehand.extract(ADDRESS_QUESTION, AddressAndFeedState)).data
    if state.blocked_reason:
        raise Blocked(state.blocked_reason)
    if addresses_match(state.displayed_address, address):
        log.info(f"  Delivery address confirmed: {state.displayed_address}")
        return True
    if state.displayed_address and not state.address_modal_or_input_open:
        log.info(
            f"  Header shows previous address '{state.displayed_address}' (expected '{address}'); updating delivery address..."
        )
    act_res = await safe_act(stagehand, page, address_instruction(state, address))
    log.info(f"  Address step {step_no}: {act_res.description}")
    await settle(page, 2200)
    return False


def choose_store(stores: list[StoreCard]) -> StoreCard | None:
    """The first open restaurant on the feed, else the first restaurant-looking card."""
    named = [s for s in stores if is_valid_restaurant_name(s.name)]
    return next((s for s in named if s.available and s.is_restaurant), named[0] if named else None)


def note_advertised_fees(result: UberResult, chosen: StoreCard) -> None:
    result.store_name = chosen.name
    result.store_location = result.store_location or extract_suburb_hint(chosen.name)
    result.advertised_delivery_fee_display = chosen.delivery_fee_display
    result.advertised_delivery_fee = parse_price(chosen.delivery_fee_display)
    result.advertised_service_fee_display = chosen.service_fee_display


async def store_url(
    page: Page, chosen: StoreCard | None, result: UberResult, skip_paths: frozenset[str] = frozenset()
) -> str | None:
    """The chosen store's `/store/...` URL, from the extraction or else from the page's own links.

    The model often answers with an element id instead of the link, or leaves the card's fee out, so the
    card is also read from the page whenever either is missing.
    """
    model_url = (
        urljoin("https://www.ubereats.com", chosen.url) if chosen and chosen.url and "/store/" in chosen.url else None
    )
    if model_url and result.advertised_delivery_fee_display:
        return model_url
    dom_store = await js(
        page,
        JS_FIND_STORE,
        {
            "preferred": chosen.name if chosen else "",
            "excluded": list(NON_RESTAURANT_NAMES),
            "skip": sorted(skip_paths),
        },
    )
    if not dom_store:
        return model_url
    result.store_name = result.store_name or dom_store["name"]
    if dom_store.get("deliveryFee") and not result.advertised_delivery_fee_display:
        result.advertised_delivery_fee_display = dom_store["deliveryFee"]
        result.advertised_delivery_fee = parse_price(dom_store["deliveryFee"])
    return model_url or dom_store["url"]


async def first_open_store_card(page: Page, skip_paths: frozenset[str] = frozenset()) -> dict | None:
    """The first open store card on the page that is not a supermarket, liquor store or the like, else None.

    `skip_paths` are `/store/...` paths already tried (found closed once opened).
    """
    card = await js(
        page, JS_FIND_STORE, {"preferred": "", "excluded": list(NON_RESTAURANT_NAMES), "skip": sorted(skip_paths)}
    )
    return card if card and not card.get("closed") and is_valid_restaurant_name(card.get("name")) else None


async def open_store_from_feed(
    stagehand: Stagehand, page: Page, result: UberResult, skip_paths: frozenset[str] = frozenset()
) -> None:
    """Pick a restaurant from the feed or search results and open its page.

    The store cards are read from the page (name, link, the fee printed on the card, closed or not); the
    model reads the whole feed only when no open restaurant card is found that way.
    """
    chosen: StoreCard | None = None
    card = await first_open_store_card(page, skip_paths)
    if card:
        result.store_name = card["name"]
        result.store_location = result.store_location or extract_suburb_hint(card["name"])
        if card.get("deliveryFee"):
            result.advertised_delivery_fee_display = card["deliveryFee"]
            result.advertised_delivery_fee = parse_price(card["deliveryFee"])
        target_url: str | None = card["url"]
    else:
        try:
            chosen = choose_store((await stagehand.extract(FEED_QUESTION, StoreFeed, timeout=25_000)).data.stores)
        except (ExtractFailed, StepTimeout) as error:
            log.info(f"  Could not read the store cards ({error}); picking the first store link on the page")
        if chosen:
            note_advertised_fees(result, chosen)
        target_url = await store_url(page, chosen, result, skip_paths)
        if target_url and urlparse(target_url).path in skip_paths:
            target_url = None
    if target_url:
        log.info(
            f"  Picking store: {result.store_name or target_url} "
            f"(Advertised delivery: {result.advertised_delivery_fee_display or 'not shown'})"
        )
        await page.goto(target_url)
        await settle(page, 2800)
        await dismiss_location_prompt(page, result.out_dir, address_confirmed=True)
    else:
        await safe_act(
            stagehand,
            page,
            f"Click on the restaurant card '{chosen.name}' to open its menu page."
            if chosen
            else "Scroll down and click on the first open restaurant card (not a grocery store).",
        )
        await settle(page, 2500)


async def keep_store_unless_closed(page: Page, result: UberResult, closed: set[str], feed_url: str | None) -> bool:
    """True to keep the open store page. A closed one picked from the feed at `feed_url` is added to `closed`
    and the feed reopened (False); with no `feed_url` the user named this store, so it is kept with a warning.
    """
    why = await store_closed_reason(page)
    if not why:
        return True
    if feed_url is None:
        log.warning(f"  This store looks closed ({why}); its items may not be addable right now.")
        return True
    closed.add(urlparse(await page.url()).path)
    log.info(f"  Store is closed ({why}); trying the next one.")
    if len(closed) >= MAX_CLOSED_STORES:
        raise ElementNotFound(
            f"Every store tried was closed ({len(closed)}, the last said '{why}'); try again when they are open"
        )
    result.store_name = result.store_location = None
    result.advertised_delivery_fee_display = result.advertised_delivery_fee = None
    await page.goto(feed_url)
    await settle(page, 3000)
    return False


async def ensure_address_and_open_store(
    stagehand: Stagehand,
    page: Page,
    address: str,
    search_query: str | None,
    result: UberResult,
    interactive: bool = True,
) -> str:
    """Ensure the active delivery address matches `address` (updating if stale), run search, and open a `/store/...` page.

    A store picked from the feed that turns out to be closed (it opens later, or takes scheduled orders only)
    is left for the next one, up to MAX_CLOSED_STORES times; its items could not be added to an order.
    """
    query_submitted = False
    address_verified = False
    started_on_store = "/store/" in await page.url()
    feed_url: str | None = None
    closed: set[str] = set()

    for step_idx in range(MAX_ADDRESS_STEPS):
        curr_url = await page.url()
        if is_sso_redirect(curr_url):
            await wait_out_sso(page)
            continue

        if is_login_url(curr_url):
            if not await login_pause(page, f"Uber login wall detected ({curr_url}).", interactive):
                raise Blocked(f"Redirected to Uber login wall ({curr_url})")
            curr_url = await page.url()

        await dismiss_location_prompt(page, result.out_dir, address_confirmed=address_verified)
        curr_url = await page.url()

        if not address_verified:
            address_verified = await confirm_address_step(stagehand, page, address, curr_url, step_idx + 1)
            if not address_verified:
                continue

        if "/store/" in curr_url:
            if await keep_store_unless_closed(page, result, closed, None if started_on_store else feed_url):
                return curr_url
            continue

        if search_query and not query_submitted and "q=" not in curr_url.lower():
            search_url = f"{SITE_URL}/search?q={quote_plus(search_query)}"
            log.info(f"  Navigating to search results: {search_url}")
            await page.goto(search_url)
            query_submitted = True
            await settle(page, 3000)

        feed_url = await page.url()
        await open_store_from_feed(stagehand, page, result, frozenset(closed))
        if "/store/" in await page.url():
            if urlparse(await page.url()).path in closed:
                raise ElementNotFound("Only closed stores are left in the results; try again when they are open")
            continue  # checked for being closed at the top of the loop

    raise ElementNotFound(f"Could not open a `/store/` restaurant page (ended on {await page.url()})")


def choose_menu_items(items: list[MenuItem], n: int) -> list[MenuItem]:
    """The first `n` available dishes; cheap add-ons (sauces, extras under $3) are not a meaningful order."""
    return [i for i in items if i.is_food_or_drink and not i.sold_out and (parse_price(i.price) or 0) > 3.0][:n]


def order_item(menu_item: MenuItem, **extra: object) -> Item:
    return new_item(
        menu_item.name,
        menu_item.price,
        category=menu_item.category,
        direct_item_name=None,
        direct_price_display=None,
        direct_price=None,
        uber_vs_direct_diff=None,
        uber_vs_direct_markup_pct=None,
        **extra,
    )


async def open_item_modal(stagehand: Stagehand, page: Page, menu_item: MenuItem, cart_count_before: int) -> bool | None:
    """Open the item's customisation modal. True if it is open, None if the item was quick-added
    without a modal, False if no popup with an 'Add to order' button could be opened: either no popup
    opened, or one opened that cannot add the item (see item_popup_blocker for which).

    The item's card is clicked from the page first; the model (which reads the whole menu) only looks
    for it when that click does not open the modal.
    """
    clicked = await js(page, JS_CLICK_MENU_ITEM, {"name": menu_item.name, "price": menu_item.price})
    if clicked:
        log.info(f"    Open item (DOM): {clicked}")
        await settle(page, 1800)
        if await accept_new_order_prompt(page) and not await is_item_modal_open(page):
            await js(page, JS_CLICK_MENU_ITEM, {"name": menu_item.name, "price": menu_item.price})
            await settle(page, 1800)
        if await is_item_modal_open(page):
            return True
        if await get_cart_count(page) > cart_count_before:
            return None
        if await item_popup_open_without_add(page):  # clicking the menu behind the popup would not help
            return False

    step = await safe_act(
        stagehand,
        page,
        f"Click on the menu item '{menu_item.name}' (priced around {menu_item.price}) to open its customization modal.",
    )
    log.info(f"    Open item: {step.description}")
    await settle(page, 1800)
    if await is_item_modal_open(page):
        return True
    if await get_cart_count(page) > cart_count_before:
        return None
    if await item_popup_open_without_add(page):
        return False

    retry = await safe_act(
        stagehand, page, f"Scroll to and click directly on the menu item title '{menu_item.name}' to open its popup."
    )
    log.info(f"    Retry open item: {retry.description}")
    await settle(page, 1800)
    return await is_item_modal_open(page)


async def modal_round(
    stagehand: Stagehand, page: Page, menu_item: MenuItem, result: UberResult, item: dict, item_idx: int
) -> tuple[list[str], bool]:
    """One pass over the open item modal: choose required options, then click Add.

    Returns (modifiers chosen this pass, whether Add was clicked). Add is not clicked while a
    required section is still unmet; the caller comes round again.
    """
    picked = [str(x) for x in await js(page, JS_SELECT_REQUIRED, default=[]) if x]
    if picked:
        log.info(f"    Auto-selected required option(s): {', '.join(picked)}")
        await settle(page, 800)

    modal_state = await scoped_extract(
        stagehand,
        page,
        JS_MARK_ITEM_MODAL,
        f"We are inside the item modal for '{menu_item.name}' (menu price {menu_item.price}). "
        "Check if any section marked 'Required' (or 'Choose 1') still has NO option selected. "
        "Also extract the price on the bottom 'Add to order' button and up to 10 paid modifier options.",
        ItemModalState,
        "Only the item modal is shown.",
    )
    record_offers(result, item_idx, "item_modal", menu_item.name, modal_state.paid_modifier_offers)
    if modal_state.button_price_display:
        select_price(item, modal_state.button_price_display)

    if modal_state.has_unmet_required_choices and modal_state.next_required_group:
        cheapest = modal_state.cheapest_option_in_required_group
        act_mod = await safe_act(
            stagehand,
            page,
            f"In the required section '{modal_state.next_required_group}', "
            f"select '{cheapest or 'the first $0 / cheapest option'}'.",
        )
        log.info(f"    Modifier ({modal_state.next_required_group}): {act_mod.description}")
        await settle(page, 900)
        return picked + ([cheapest] if cheapest else []), False

    add = await js(page, JS_CLICK_ADD, default={"clicked": False, "price": None})
    if add["price"] and not item["selected_price_display"]:
        select_price(item, add["price"])
    if add["clicked"]:
        log.info(f"    Clicked Add to order button ({item['selected_price_display'] or menu_item.price})")
    else:
        add_act = await safe_act(
            stagehand, page, "Click the black 'Add 1 to order' / 'Add to order' button at the bottom of the item modal."
        )
        log.info(f"    Click Add: {add_act.description}")
    await settle(page, 1800)
    await accept_new_order_prompt(page)
    return picked, True


async def add_menu_item_to_cart(
    stagehand: Stagehand,
    page: Page,
    menu_item: MenuItem,
    result: UberResult,
    item_idx: int,
) -> dict:
    """Open the item, choose the cheapest required options, add it, and record what it cost."""
    item = order_item(menu_item)
    await close_item_modal(stagehand, page)
    await close_cart_drawer(page)  # a stray drawer must never cover the menu
    before_cart_count = await get_cart_count(page)

    try:
        opened = await open_item_modal(stagehand, page, menu_item, before_cart_count)
        if opened is None:  # quick-add without a modal
            select_price(item, menu_item.price)
            item["status"] = "added"
            return item
        if not opened:
            blocker = await item_popup_blocker(page)
            if blocker:
                item["error"] = blocker
                log.info(f"    '{menu_item.name}' cannot be added: {blocker}; skipping.")
            else:
                mod = url_mod(await page.url())
                item["error"] = f"item popup did not open (page URL mod={mod or 'none'})"
                log.info(f"    Modal did not open for '{menu_item.name}' (page URL mod={mod or 'none'}), skipping.")
            return item

        selected_modifiers: list[str] = []
        for _ in range(MAX_MODIFIER_STEPS):
            if not await is_item_modal_open(page):
                item["status"] = "added"
                break
            picked, add_clicked = await modal_round(stagehand, page, menu_item, result, item, item_idx)
            selected_modifiers.extend(picked)
            if not add_clicked:
                continue
            if not await is_item_modal_open(page) or await get_cart_count(page) > before_cart_count:
                item["status"] = "added"
                break
            log.info("    Modal remained open after clicking Add; checking newly scrolled required section...")

        if item["selected_price"] is None:
            select_price(item, menu_item.price)
        item["selected_variant"] = ", ".join(dict.fromkeys(selected_modifiers)) or None
        if item["status"] != "added":
            item["status"] = "add_failed"
        return item
    finally:
        await close_item_modal(stagehand, page)


CHECKOUT_QUESTION = (
    "Examine the current screen in the Uber Eats ordering flow. "
    "IMPORTANT: `is_cart_visible` is True ONLY if the side cart drawer/panel is open showing line items "
    "and a 'Go to checkout' button (NOT just the small green cart icon in the top header). "
    "Extract all visible line items, subtotal, standard delivery fee, priority delivery fee (if shown), "
    "service fee, small order fee, any other fees, total, and every optional upsell, Uber One offer, or tip."
)


async def read_checkout_step(
    stagehand: Stagehand, page: Page, result: UberResult, step_idx: int, url: str
) -> tuple[CartAndCheckoutState, str]:
    """Extract the current screen of the ordering flow and record it as a step. Returns (state, step type)."""
    if "/checkout" in url:
        state = (await stagehand.extract(CHECKOUT_QUESTION, CartAndCheckoutState)).data
    else:
        state = await scoped_extract(
            stagehand,
            page,
            JS_MARK_CART_DRAWER,
            CHECKOUT_QUESTION,
            CartAndCheckoutState,
            "Only the open cart drawer is shown (nothing else is open over the store page).",
        )
    step_type = "checkout" if state.is_checkout_page else state.step_type
    log.info(
        f"  Step {step_idx}: {step_type} | subtotal={state.subtotal} | "
        f"delivery={state.delivery_fee} | service={state.service_fee} | "
        f"small_order={state.small_order_fee} | total={state.total}"
    )
    await shot(page, result.out_dir / f"step-{step_idx:02d}-{slug(step_type)}.png")
    record_offers(result, step_idx, step_type, step_type, state.offers)
    result.steps.append({
        "step": step_idx,
        "step_type": step_type,
        "url": url,
        "subtotal_display": state.subtotal,
        "delivery_fee_display": state.delivery_fee,
        "priority_delivery_fee_display": state.priority_delivery_fee,
        "service_fee_display": state.service_fee,
        "small_order_fee_display": state.small_order_fee,
        "total_display": state.total,
        "total": parse_price(state.total),
        "offers": len(state.offers),
        "summary": state.summary,
    })  # fmt: skip
    return state, step_type


async def decline_upsell(stagehand: Stagehand, page: Page, declines: int) -> None:
    """Dismiss an upsell popup without adding anything: DOM first, then the LLM, then just close the dialog.

    On a checkout URL, avoid in-page JS clicks (Uber Eats' checkout page submits background forms on load
    that trip the checkout submit guard); instead dismiss via Escape / stripping `mod=magicUpsell`.
    """
    curr_url = await page.url()
    if "/checkout" in curr_url:
        with contextlib.suppress(*BROWSER_ERRORS):
            await page.key_press("Escape")
        await page.wait_for_timeout(1500)
        curr_url = await page.url()
        if "mod=magicUpsell" in curr_url:
            clean_url = re.sub(r"([?&])mod=magicUpsell(&|$)", r"\1", curr_url).rstrip("?&")
            log.info("    Declining upsell: navigating to checkout without magicUpsell...")
            await page.goto(clean_url)
            await wait_for_render(page, 10)
            await page.wait_for_timeout(2000)
        else:
            log.info("    Declining upsell: dismissed checkout modal via Escape")
        return

    clicked = await js(page, JS_DECLINE_UPSELL)
    if clicked:
        log.info(f"    Declining upsell: clicked '{clicked}'")
    elif declines <= 2 and current_guard().mode_for(curr_url) is Mode.NORMAL:
        act_res = await safe_act(
            stagehand,
            page,
            "An upsell popup ('Complete your meal', 'Add more items', 'Try Uber One') is blocking checkout. "
            "Dismiss it WITHOUT adding anything: click 'Skip', 'No thanks', 'Not now', 'Next' or "
            "'Continue to checkout', or its X close button. Never start an Uber One trial or place the order.",
        )
        log.info(f"    Declining upsell: {act_res.description}")
    else:  # last resort: close whatever dialog is open
        await js(page, "() => { closeDialog() || pressEsc(); }")
        log.info("    Declining upsell: closed the dialog")
    await settle(page, 2000)
    if "/checkout" not in await page.url() and declines >= 2:
        await page.goto(CHECKOUT_URL)  # popup closed back to the menu: go straight to checkout
        await wait_for_render(page, 10)
        await page.wait_for_timeout(2000)


async def open_cart_or_checkout(stagehand: Stagehand, page: Page, cart_visible: bool) -> None:
    """Open the cart drawer if it is closed; otherwise press its 'Go to checkout' button."""
    if not cart_visible:
        if await open_cart_drawer(page):
            log.info("    Opening cart via DOM click on header cart button")
        else:
            act_res = await safe_act(
                stagehand,
                page,
                "Click the green cart button in the top-right header (or 'View cart') to open the cart drawer.",
            )
            log.info(f"    Opening cart: {act_res.description}")
        await settle(page, 2200)
        return

    if await js(page, JS_CLICK_CHECKOUT, default=False):
        log.info("    Clicked 'Go to checkout' button")
    else:
        act_res = await safe_act(
            stagehand,
            page,
            "Click the 'Go to checkout' / 'Checkout' button in the cart drawer. Never click 'Place order' or 'Pay'.",
        )
        log.info(f"    Proceeding to checkout: {act_res.description}")
    await wait_for_render(page, 15)
    await page.wait_for_timeout(2500)


async def walk_to_checkout(
    stagehand: Stagehand,
    page: Page,
    result: UberResult,
    interactive: bool = True,
) -> tuple[CartAndCheckoutState | None, CartAndCheckoutState | None]:
    """Open the cart drawer, decline interstitial upsells, and proceed to the checkout screen."""
    out = result.out_dir
    cart_snapshot: CartAndCheckoutState | None = None
    upsell_declines = 0

    async def login_or_block(reason: str) -> None:
        if not await login_pause(page, reason, interactive):
            raise Blocked(reason)
        if "/checkout" not in await page.url():
            log.info("  Navigating to /au/checkout after login...")
            await page.goto(CHECKOUT_URL)
            await settle(page, 3000)

    await close_item_modal(stagehand, page)
    if await open_cart_drawer(page):
        log.info("  Opened cart drawer via header cart button")
        await settle(page, 2000)

    for step_idx in range(1, MAX_CHECKOUT_STEPS + 1):
        curr_url = await page.url()
        if is_login_url(curr_url):
            await shot(page, out / f"step-{step_idx:02d}-login-wall.png")
            await login_or_block(f"Uber login wall reached before checkout ({curr_url}).")
            curr_url = await page.url()

        state, step_type = await read_checkout_step(stagehand, page, result, step_idx, curr_url)

        if "/checkout" not in curr_url and await is_item_modal_open(page):
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
            await current_guard().freeze(page)  # hard stop: the page with 'Place order' is only read from here on
            await shot(page, out / "checkout.png")
            return cart_snapshot, state

        if state.blocked_reason or step_type == "login_wall":
            await login_or_block(state.blocked_reason or "Mandatory sign-in screen before checkout.")
        elif upsell:
            upsell_declines += 1
            await decline_upsell(stagehand, page, upsell_declines)
        else:
            await open_cart_or_checkout(stagehand, page, state.is_cart_visible)

    return cart_snapshot, None
