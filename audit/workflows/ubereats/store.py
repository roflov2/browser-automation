"""The Uber Eats side of the audit: delivery address, store, menu items, cart and checkout."""

import base64
import contextlib
import functools
import json
import re
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

from stagehand import Page, Stagehand

from ... import page as page_helpers
from ...core import Blocked, new_item, parse_price, record_offers, slug
from ...page import is_sso_redirect, pause_for_login, settle, shot
from .models import (
    AddressAndFeedState,
    CartAndCheckoutState,
    ItemModalState,
    MenuItem,
    StoreFeed,
    UberResult,
    extract_suburb_hint,
)

SITE_URL = "https://www.ubereats.com/au"
CHECKOUT_URL = f"{SITE_URL}/checkout"
MAX_ADDRESS_STEPS = 12
MAX_MODIFIER_STEPS = 6
MAX_CHECKOUT_STEPS = 10

NON_RESTAURANT_NAMES = (
    "coles", "woolworths", "aldi", "iga", "costco", "7-eleven", "7 eleven",
    "liquorland", "bws", "dan murphy", "first choice liquor", "vintage cellars",
    "chemist warehouse", "priceline", "ampol", "bp ", "caltex", "officeworks",
)
STREET_TYPES = {
    "st", "street", "pl", "place", "rd", "road", "ave", "avenue", "dr", "drive", "ct", "court",
    "cct", "circuit", "ln", "lane", "way", "blvd", "boulevard", "pde", "parade", "tce", "terrace",
    "cl", "close", "cr", "cres", "crescent", "hwy", "highway", "walk", "grove", "gr",
}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def is_valid_restaurant_name(name: str | None) -> bool:
    return bool(name) and not any(bad in name.lower().strip() for bad in NON_RESTAURANT_NAMES)


def is_login_url(url: str) -> bool:
    low = url.lower()
    return any(w in low for w in ("auth.uber.com", "login.uber.com", "/login-redirect", "/login"))


def street_tokens(address: str) -> list[str]:
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
    req_tokens = street_tokens(target)
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


# ---------------------------------------------------------------------------
# Page helpers
# ---------------------------------------------------------------------------
UBER_JS = r"""
const addButton = () => Array.from(document.querySelectorAll('button')).find(
    b => /^(Add\s+\d*\s*to\s+(order|cart)|Update\s+order)/i.test(txt(b)) && vis(b, 80, 25));
const cartButtons = () => Array.from(document.querySelectorAll(
    '[data-testid*="cart" i], button[aria-label*="cart" i], button[aria-label*="bag" i]'));
"""
js = functools.partial(page_helpers.js, lib=UBER_JS)


async def dismiss_uber_popups(stagehand: Stagehand, page: Page) -> None:
    await page_helpers.maybe_dismiss_popups(
        stagehand, page,
        "cookie banner, promotional popup, Uber One trial offer, or app-install banner",
        caution="Do NOT click 'Sign in' or 'Continue' on an order. ",
        selectors='[role="dialog"], [aria-modal="true"], [class*="cookie" i], [id*="cookie" i], '
                  '[class*="promo" i], [class*="banner" i]',
    )


async def login_pause(page: Page, message: str) -> bool:
    """Let the user sign in (headed runs only); True once the browser has left Uber's login pages."""
    return await pause_for_login(page, message) and not is_login_url(await page.url())


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


# ---------------------------------------------------------------------------
# Uber Eats: flow
# ---------------------------------------------------------------------------
async def ensure_address_and_open_store(
    stagehand: Stagehand,
    page: Page,
    address: str,
    search_query: str | None,
    result: UberResult,
) -> str:
    """Ensure the active delivery address matches `address` (updating if stale), run search, and open a `/store/...` page."""
    question = (
        "Look at the top header bar and any open address modal. Report the exact delivery address text currently shown "
        "in the header (`displayed_address`), whether an address input dialog is open (`address_modal_or_input_open`), "
        "whether autocomplete suggestions are visible (`address_suggestions_open`), and any `blocked_reason`."
    )
    address_args = {"target": address, "tokens": street_tokens(address)}
    query_submitted = False
    address_verified = False

    for step_idx in range(MAX_ADDRESS_STEPS):
        curr_url = await page.url()
        if is_sso_redirect(curr_url):
            print("  Waiting for corporate SSO redirect...")
            await page.wait_for_timeout(3000)
            continue

        if is_login_url(curr_url):
            if not await login_pause(page, f"Uber login wall detected ({curr_url})."):
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


def order_item(menu_item: MenuItem, **extra) -> dict:
    return new_item(
        menu_item.name, menu_item.price, category=menu_item.category,
        direct_item_name=None, direct_price_display=None, direct_price=None,
        uber_vs_direct_diff=None, uber_vs_direct_markup_pct=None, **extra,
    )


async def add_menu_item_to_cart(
    stagehand: Stagehand,
    page: Page,
    menu_item: MenuItem,
    result: UberResult,
    item_idx: int,
) -> dict:
    item = order_item(menu_item)

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
    result: UberResult,
) -> tuple[CartAndCheckoutState | None, CartAndCheckoutState | None]:
    """Open the cart drawer, decline interstitial upsells, and proceed to the checkout screen."""
    out = result.out_dir
    cart_snapshot: CartAndCheckoutState | None = None
    upsell_declines = 0

    async def login_or_block(reason: str) -> None:
        if not await login_pause(page, reason):
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
