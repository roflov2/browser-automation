"""Coffee roaster / retailer stores (Shopify-style storefronts).

For each store URL: let Stagehand find the shop page, read one page of products, add 1-5 coffee
products (one-time purchase, smallest size) to the cart, go to checkout, screenshot it, and compare
shop-page prices against checkout prices to flag drip pricing.
"""

import argparse
import asyncio
import logging
import re
from urllib.parse import urljoin

from pydantic import BaseModel, Field
from stagehand import Page, Stagehand

from ..actions import LLM_TIMEOUT_MS, Acted, act_on, llm_act
from ..config import Settings
from ..core import (
    BROWSER_ERRORS,
    ElementNotFound,
    Fee,
    FlowChanged,
    Item,
    LineItem,
    Report,
    SiteResult,
    Status,
    compare_item_prices,
    diff,
    drip_outcome,
    drip_verdict,
    final_price_was_read,
    money_sum,
    new_item,
    norm_name,
    parse_price,
    pop_best_line,
    positive,
    write_csv,
)
from ..page import (
    dismiss_popups,
    fetch_json,
    is_sso_redirect,
    js,
    maybe_dismiss_popups,
    settle,
    shot,
    try_goto,
    wait_for_render,
    wait_out_sso,
    wait_until,
)
from ..workflow import Workflow

log = logging.getLogger(__name__)

SETTLE_MS = 1500  # fixed wait after navigation/clicks; increase for slow sites
MAX_SHOP_STEPS = 4
POPUPS = "cookie banner, newsletter/discount popup, region or currency selector modal, or age-verification modal"
SAFETY_PROMPT = (
    "You are auditing an online store for price transparency. Never click a button that pays for or places an "
    "order (e.g. 'Pay now', 'Complete order', 'Shop Pay', 'PayPal', 'G Pay', 'Buy it now'). Never enter card, "
    "gift-card or discount-code details, and never choose a subscription. Never log in or create an account."
)
COFFEE_AFFIX = re.compile(r"(?i)^coffee\s+|\s+coffee$")  # 'Coffee X' / 'X Coffee' -> 'X'


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class PageCheck(BaseModel):
    is_shop_page: bool = Field(
        description="True only on the store's shop/catalogue page listing coffee products for sale with prices, not the homepage"
    )
    reason: str | None = Field(
        default=None, description="One short sentence explaining this reading of the screen; null if unsure"
    )


class Product(BaseModel):
    name: str = Field(
        description="Clean product title without duplicated collection/category prefixes (e.g. 'Ethiopia Worka Sakaro', not 'Coffee Ethiopia Worka Sakaro')"
    )
    price: str | None = Field(default=None, description="Price as displayed, e.g. 'From $15.00'; null if none is shown")
    sold_out: bool
    is_coffee: bool = Field(
        description="True for coffee for drinking: whole bean or ground coffee, pods, capsules, drip bags or steep bags. "
        "False for equipment, mugs, merch, gift cards, courses and subscription-only products."
    )
    product_type: str | None = Field(
        default=None,
        description="One of: whole_bean_or_ground, pods_or_capsules, drip_or_steep_bags, equipment, merch, gift_card, "
        "subscription, course, other; null if unclear",
    )
    url: str | None = Field(description="Link (href) to the product page, if present")


class Products(BaseModel):
    products: list[Product]


class ProductPageState(BaseModel):
    is_product_page: bool = Field(
        description="True if on an individual product page with an Add to Cart / Buy button (not a collection or catalogue page)"
    )
    has_purchase_options: bool = Field(
        description="True if the product page offers a choice between one-time purchase and subscription"
    )
    one_time_selected: bool = Field(
        description="True if there is no subscription choice, or if one-time purchase is currently selected"
    )
    has_size_options: bool = Field(
        description="True if the product page has a package size, weight or quantity-pack selector (e.g. 225g, 250g, 500g, 1kg, 10 pack)"
    )
    lowest_size_selected: bool = Field(
        description="True only if there are no size options, or the smallest/cheapest size is currently selected"
    )
    selected_size: str | None = Field(description="Currently selected size option, e.g. '250g'")
    lowest_available_size: str | None = Field(description="Smallest available size option, e.g. '250g'")
    current_price: str | None = Field(
        description="Price currently displayed for the selected one-time purchase option, e.g. '$21.00'"
    )
    reason: str | None = Field(
        default=None, description="One short sentence explaining this reading of the screen; null if unsure"
    )


class CartLines(BaseModel):
    is_cart_visible: bool = Field(description="True if a cart page or cart drawer is open showing items")
    items: list[LineItem]
    subtotal: str | None
    has_checkout_button: bool
    reason: str | None = Field(
        default=None, description="One short sentence explaining this reading of the screen; null if unsure"
    )


class CheckoutDetails(BaseModel):
    is_checkout_page: bool = Field(description="True if on a checkout page showing an order summary")
    blocked_reason: str | None = Field(
        description="If progress is blocked by a required login, CAPTCHA, 'verify you are human' or bot check, describe it; otherwise null"
    )
    items: list[LineItem]
    subtotal: str | None
    shipping: str | None = Field(
        description="Shipping amount if a number is shown; null if not yet calculated (e.g. 'Enter shipping address')"
    )
    other_fees: list[Fee] = Field(
        description="Any other added charges that are NOT tax and NOT shipping, e.g. service fee, handling fee, card surcharge"
    )
    tax_amount: str | None
    tax_included: bool | None = Field(
        default=None,
        description="True if tax is included in item prices (e.g. 'Including $1.91 in taxes'); False if tax is "
        "added as a separate line; null if not stated",
    )
    total: str | None
    currency: str | None = Field(description="Currency code if shown, e.g. 'AUD'")
    summary: str | None = Field(default=None, description="One short sentence describing this screen; null if unsure")


# ---------------------------------------------------------------------------
# Site steps
# ---------------------------------------------------------------------------
async def find_shop_page(stagehand: Stagehand, page: Page) -> str:
    for _ in range(MAX_SHOP_STEPS + 2):
        curr_url = await page.url()
        if is_sso_redirect(curr_url):
            await wait_out_sso(page)
            continue
        if curr_url.startswith("chrome-error://"):
            raise FlowChanged(f"Page failed to load ({curr_url})")

        check = await stagehand.extract("Is the current page the store's coffee shop page?", PageCheck)
        if check.data.is_shop_page:
            return curr_url
        reason = check.data.reason or ""
        if "ERR_" in reason or "failed to load" in reason.lower():
            raise FlowChanged(reason)

        step = await llm_act(
            stagehand,
            "Take the next step to get to this store's shop page where its coffee is listed for sale "
            "(e.g. a 'Shop', 'Coffee' or 'Shop Coffee' link). If navigation links are hidden behind a menu button, open the menu first.",
        )
        await settle(page, SETTLE_MS)
        await maybe_dismiss_popups(stagehand, page, POPUPS)
        log.info(f"  {step.description} -> {check.data.reason}")
    raise ElementNotFound("Couldn't find the shop page")


def choose_coffees(products: list[Product], n: int) -> list[Product]:
    available = [p for p in products if p.is_coffee and not p.sold_out]
    preferred = [p for p in available if p.product_type == "whole_bean_or_ground"]
    others = [p for p in available if p.product_type != "whole_bean_or_ground"]
    return (preferred + others)[:n]


JS_FIND_PRODUCT_LINK = r"""({ names, modelUrl, currentUrl }) => {
    const norm = s => (s || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
    const bare = u => (u || '').split('#')[0].split('?')[0].replace(/\/+$/, '');
    const targets = new Set(names.map(norm).filter(Boolean));
    const base = bare(currentUrl);

    const anchors = Array.from(document.querySelectorAll('a[href]')).filter(a => {
        const raw = a.getAttribute('href') || '';
        return raw && !raw.startsWith('#') && !raw.startsWith('javascript:') && bare(a.href) && bare(a.href) !== base;
    });
    // Prefer links containing /products/ or /product/ when multiple links match
    anchors.sort((a, b) => (b.href.includes('/product') ? 1 : 0) - (a.href.includes('/product') ? 1 : 0));

    // 1. The link the page itself labels with the product's name.
    for (const a of anchors) {
        const heading = a.querySelector('h1, h2, h3, h4, h5, [class*="title" i], [class*="name" i]');
        const candidates = [
            heading ? norm(heading.innerText) : '',
            norm(a.getAttribute('aria-label')),
            norm(a.title),
            norm(a.innerText)
        ].filter(Boolean);
        if (candidates.some(c => targets.has(c))) return a.href;
    }
    // 2. The model's URL, but only if the page really links to it: the model sometimes reports a nearby
    //    link instead ('Shop singles' -> /collections/single) or an element id ('4-2475').
    const wanted = bare(modelUrl);
    if (wanted) for (const a of anchors) if (bare(a.href) === wanted) return a.href;
    return null;
}"""


async def resolve_product_url(page: Page, product: Product, shop_url: str) -> str | None:
    """The product page's URL as the shop page links to it; None if the product has to be clicked instead.

    The page's own link for the product's name wins over the URL the model extracted, which is used only
    when the page links to it.
    """
    model_url = urljoin(shop_url, product.url) if product.url and product.url.startswith(("http", "/")) else None
    names = [product.name, COFFEE_AFFIX.sub("", product.name).strip()]
    href = await js(page, JS_FIND_PRODUCT_LINK, {"names": names, "modelUrl": model_url, "currentUrl": shop_url})
    return href if isinstance(href, str) and href else None


# ---------------------------------------------------------------------------
# Add to cart: the product form's own button, a visible-only model fallback, and a patient cart check
# ---------------------------------------------------------------------------
CART_POLL_INTERVAL_MS = 500
CART_POLL_TIMEOUT_MS = 5000

# Find the main product form's add-to-cart button, scroll to it and click it. Returns {description, selector}
# or null. Skips sticky/quick-add bars (which only forward the click, and are hidden until the main button
# scrolls away), subscription buttons, product cards and other sections, and every express-checkout button.
JS_CLICK_SHOPIFY_ADD = r"""() => {
    const EXCLUDED = 'sticky-add-to-cart, .subscribe-block, product-card, .product-card, product-recommendations, '
        + '.product-recommendations, [class*="recommendations" i], [class*="related-products" i], '
        + '.shopify-payment-button, [data-shopify="payment-button"]';
    const visible = el => {
        if (el.checkVisibility && !el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) return false;
        const r = el.getBoundingClientRect();
        if (!(r.width > 0 && r.height > 0)) return false;
        for (let a = el; a; a = a.parentElement) {
            const s = getComputedStyle(a);
            if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity || '1') === 0) return false;
        }
        return true;
    };
    const form = document.querySelector('product-form-component');
    const h1 = document.querySelector('h1');
    const main = (form && form.closest('.shopify-section')) || (h1 && h1.closest('.shopify-section'));
    const usable = el => {
        if (el.closest(EXCLUDED) || el.classList.contains('subscribe-block__submit')) return false;
        if (el.disabled || el.getAttribute('aria-disabled') === 'true') return false;
        const section = el.closest('.shopify-section');
        if (main && section && section !== main) return false;
        return visible(el);
    };
    let found = Array.from(document.querySelectorAll('button[name="add"]')).filter(usable);
    if (!found.length) found = Array.from(document.querySelectorAll('form[action*="/cart/add"] button[type="submit"]')).filter(usable);
    if (!found.length) return null;
    const inForm = el => !!el.closest('product-form-component, form[action*="/cart/add"]');
    const inMain = el => !!main && main.contains(el);
    const rank = el => (inMain(el) ? 0 : 2) + (inForm(el) ? 0 : 1);
    const el = found.slice().sort((a, b) => rank(a) - rank(b))[0];  // stable: DOM order within a rank

    const cssPath = node => {
        const parts = [];
        for (let n = node; n && n.nodeType === 1 && n !== document.documentElement; n = n.parentElement) {
            if (n.id && document.querySelectorAll('#' + CSS.escape(n.id)).length === 1) {
                parts.unshift(n.tagName.toLowerCase() + '#' + CSS.escape(n.id));
                break;
            }
            const same = n.parentElement ? Array.from(n.parentElement.children).filter(c => c.tagName === n.tagName) : [];
            const tag = n.tagName.toLowerCase();
            parts.unshift(same.length > 1 ? `${tag}:nth-of-type(${same.indexOf(n) + 1})` : tag);
        }
        return parts.join(' > ');
    };
    el.scrollIntoView({ block: 'center' });
    clickEl(el);
    const label = clean(el.innerText) || el.getAttribute('aria-label') || el.value || 'Add to cart';
    return { description: `Clicked the product form's '${label}' button`, selector: 'css=' + cssPath(el) };
}"""

# Index of the first observed selector that points at a visible element outside a sticky/quick-add bar; null if none.
JS_FIRST_VISIBLE = r"""selectors => {
    const resolve = sel => {
        try {
            if (sel.startsWith('xpath=') || sel.startsWith('/') || sel.startsWith('(')) {
                const path = sel.startsWith('xpath=') ? sel.slice(6) : sel;
                return document.evaluate(path, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
            }
            return document.querySelector(sel.startsWith('css=') ? sel.slice(4) : sel);
        } catch (e) { return null; }
    };
    const shown = el => {
        if (el.checkVisibility && !el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    };
    for (let i = 0; i < selectors.length; i++) {
        const el = resolve(selectors[i] || '');
        if (el && el.nodeType === 1 && shown(el) && !el.closest('sticky-add-to-cart, [data-stuck="false"]')) return i;
    }
    return null;
}"""


async def click_shopify_add_button(page: Page) -> Acted | None:
    """Click the main product form's add-to-cart button in the page; None if there is no usable one."""
    found = await js(page, JS_CLICK_SHOPIFY_ADD)
    if not isinstance(found, dict):
        return None
    return Acted(True, str(found.get("description") or "Clicked 'Add to cart'"), str(found.get("selector") or ""))


async def first_visible_choice(page: Page, selectors: list[str]) -> int | None:
    """Which of the observed elements to act on: the first visible one outside a sticky bar; None if none is."""
    index = await js(page, JS_FIRST_VISIBLE, selectors)
    return index if isinstance(index, int) else None


async def act_on_visible(stagehand: Stagehand, page: Page, instruction: str) -> Acted:
    """llm_act(), but acting on the first observed element that is actually visible (else the first one, as before)."""
    found = await stagehand.observe(instruction, timeout=LLM_TIMEOUT_MS)
    if not found.data:
        log.debug("Nothing to act on for %r", instruction)
        return Acted(False, "no matching element found")
    index = await first_visible_choice(page, [str(action.selector or "") for action in found.data])
    if index is None:
        index = 0
    elif index > 0:
        log.info(f"    Skipped {index} hidden or quick-add-bar match(es); clicking: {found.data[index].description}")
    return await act_on(stagehand, found.data[index])


async def wait_for_cart_growth(page: Page, before: int | None) -> int | None:
    """Read /cart.js's item_count until it is above `before` or CART_POLL_TIMEOUT_MS runs out; the last count read.

    With no `before` to compare against (the cart can't be read), it is read once. A failed read mid-poll is
    retried: a theme that goes to /cart after adding makes /cart.js unreadable while the page changes.
    """
    reads = max(1, CART_POLL_TIMEOUT_MS // max(1, CART_POLL_INTERVAL_MS))
    count = None
    for attempt in range(reads):
        count = await shopify_item_count(page)
        if before is None or (count is not None and count > before):
            return count
        if attempt < reads - 1:
            await asyncio.sleep(CART_POLL_INTERVAL_MS / 1000)
    return count


def subscription_line(data: object, name: str) -> str | None:
    """The title of the just-added /cart.js line if it carries a selling plan (a subscription); else None.

    Shopify lists the newest line first; a line with the product's name counts too.
    """
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return None
    lines = [line for line in data["items"] if isinstance(line, dict)]
    wanted = match_name(name)
    for index, line in enumerate(lines):
        title = str(line.get("product_title") or line.get("title") or "")
        if (index == 0 or match_name(title) == wanted) and line.get("selling_plan_allocation"):
            plan = line["selling_plan_allocation"]
            plan_name = plan.get("selling_plan", {}).get("name") if isinstance(plan, dict) else None
            return f"{title}: {plan_name}" if plan_name else title
    return None


async def flag_subscription(page: Page, item: dict) -> None:
    """Mark `item` with subscription_added if Shopify's cart holds it on a selling plan. Never removes it."""
    if plan := subscription_line(await fetch_json(page, "/cart.js"), item["name"]):
        log.warning(f"    '{item['name']}' went into the cart as a subscription ({plan}); it was meant to be one-time")
        item["subscription_added"] = True


async def click_add_to_cart(stagehand: Stagehand, page: Page) -> tuple[Acted, int | None, int | None]:
    """Click 'Add to cart' on the open product page: what was clicked, and /cart.js's item_count before and after.

    On Shopify the product form's own button is clicked first, with no model call; the model is asked only if
    there is none or it did not grow the cart. Counts are None where the cart can't be read (not Shopify).
    """
    count_before = await shopify_item_count(page)
    add: Acted | None = None
    count_after: int | None = None
    if count_before is not None:
        # Shopify: click the main product form's own button, with no model call. The model tends to pick the
        # first match in the DOM, which on some themes is a hidden sticky "quick add" bar that does nothing.
        direct = await click_shopify_add_button(page)
        if direct is None:
            log.info("    No visible product-form 'Add to cart' button found; asking the model")
        else:
            count_after = await wait_for_cart_growth(page, count_before)
            if count_after is not None and count_after > count_before:
                log.info(f"    Added via the product form's own button (no model call): {direct.description}")
                add = direct
            else:
                log.info(
                    f"    The product form's button did not grow the cart ({count_before} -> {count_after}); asking the model"
                )
                count_before = await shopify_item_count(page)  # in case the add landed just after the wait
    if add is None:
        add = await act_on_visible(
            stagehand,
            page,
            "Click the 'Add to cart', 'ADD TO BAG' or 'Add to bag' button of the main product form for this product, "
            "not a sticky or quick-add bar, and not a subscription button. Never click 'Buy it now', "
            "'Shop Pay', 'PayPal', 'G Pay' or any other express-checkout button.",
        )
        log.info(f"    Added (model): {add.description}")
        await wait_until(page, "() => document.readyState === 'complete'", 3000)
        await settle(page, 1200)
        # Read the count again after any successful click, even when the first read failed, so a blocked
        # /cart.js shows up in the output as [None, None].
        count_after = await wait_for_cart_growth(page, count_before) if add.success else None
    return add, count_before, count_after


async def add_to_cart(stagehand: Stagehand, page: Page, item: dict) -> None:
    """Add the open product to the cart and record on `item` whether it worked, what was clicked, and the cart count."""
    add, count_before, count_after = await click_add_to_cart(stagehand, page)
    item["status"] = "added" if add.success else "add_failed"
    if count_before is not None and count_after is not None and count_after <= count_before:
        # On Shopify the cart itself says whether the click worked, rather than the model's own report.
        log.warning("    The cart did not grow after 'Add to cart'; counting the item as not added")
        item["status"] = "add_failed"
    # Why an add failed, saved in price_comparison.json: what was (or wasn't) clicked, and the cart's count.
    item["add_detail"] = add.description
    item["add_selector"] = add.selector
    item["cart_count"] = [count_before, count_after]
    if item["status"] == "add_failed":
        log.warning(
            f"    Add failed for '{item['name']}': detail={add.description!r}, selector={add.selector!r}, "
            f"cart_count={item['cart_count']}"
        )
    elif count_before is not None:
        await flag_subscription(page, item)


async def add_product_to_cart(stagehand: Stagehand, page: Page, product: Product, shop_url: str) -> dict:
    item = new_item(product.name, product.price)

    # Open the product page: navigate directly via resolved URL, otherwise fall back to Stagehand click.
    target_url = await resolve_product_url(page, product, shop_url)
    if target_url:
        await page.goto(target_url)
    else:
        await llm_act(stagehand, f"Click on the product '{product.name}' to open its product page.")
    await settle(page, 1000)
    await maybe_dismiss_popups(stagehand, page, POPUPS)

    # Combined check for product page validity, one-time purchase option, and smallest size (allows multi-step dropdowns).
    state: ProductPageState | None = None
    for attempt in range(3):
        state = (
            await stagehand.extract(
                f"Check this page for product '{product.name}' (shop price: {product.price or 'not shown'}): "
                "1) Is this an individual product page with an Add to Cart / Buy button? "
                "2) Does it offer one-time purchase vs subscription, and is one-time purchase selected? "
                "3) Does it have package size or weight options, and is the smallest/cheapest one selected? "
                "4) Report the price currently displayed for the selected one-time purchase option.",
                ProductPageState,
            )
        ).data

        if not state.is_product_page:
            if attempt == 0:
                # The link led elsewhere, or the click missed: click the product on the shop page once more
                if target_url:
                    log.info(f"    {await page.url()} is not the product page; clicking the product on the shop page")
                    await page.goto(shop_url)
                    await settle(page, 1000)
                await llm_act(
                    stagehand,
                    f"Click on the product link or title for '{product.name}' to open its product page.",
                )
                await settle(page, 1000)
                await maybe_dismiss_popups(stagehand, page, POPUPS)
                continue
            log.info(
                f"    Not on a valid product page for '{product.name}' at {await page.url()} ({state.reason}), skipping."
            )
            return item

        acted = False
        if state.has_purchase_options and not state.one_time_selected:
            step = await llm_act(stagehand, "Select the one-time purchase option instead of subscription.")
            log.info(f"    Purchase option: {step.description}")
            await settle(page, 800)
            acted = True

        log.info(
            f"    Size: selected={state.selected_size}, lowest={state.lowest_available_size}, price={state.current_price}"
        )
        if state.has_size_options and not state.lowest_size_selected:
            target = state.lowest_available_size or "the smallest/cheapest option"
            step = await llm_act(
                stagehand,
                f"Change the package size or weight selector (currently '{state.selected_size}') to {target}.",
                follow_up=True,
                page=page,  # a custom dropdown needs opening before the option can be picked
            )
            log.info(f"    Size selection: {step.description}")
            await settle(page, 800)
            acted = True

        if not acted:
            break

    if state is not None:
        item["selected_variant"] = state.selected_size
        item["selected_price_display"] = state.current_price or product.price
        selected = parse_price(state.current_price)
        item["selected_price"] = selected if selected is not None else item["shop_price"]  # a real $0.00 stays $0.00

    await add_to_cart(stagehand, page, item)
    return item


# ---------------------------------------------------------------------------
# Shopify's own cart API: exact prices with no model call
# ---------------------------------------------------------------------------
def money_from_cents(cents: object) -> str | None:
    return f"${int(cents) / 100:.2f}" if isinstance(cents, int | float) else None


def cart_from_shopify(data: object) -> CartLines | None:
    """Build the cart from Shopify's /cart.js answer; None if it is not one, or holds nothing."""
    if not isinstance(data, dict) or not isinstance(data.get("items"), list) or not data["items"]:
        return None
    lines = [
        LineItem(
            name=str(line.get("product_title") or line.get("title") or ""),
            variant=line.get("variant_title") or None,
            quantity=int(line.get("quantity") or 1),
            price=money_from_cents(line.get("final_line_price", line.get("line_price"))),
        )
        for line in data["items"]
        if isinstance(line, dict)
    ]
    return CartLines(
        is_cart_visible=True,
        items=lines,
        subtotal=money_from_cents(data.get("items_subtotal_price", data.get("total_price"))),
        has_checkout_button=True,  # every Shopify store checks out at /checkout
        reason="read from the store's /cart.js",
    )


async def shopify_cart(page: Page) -> CartLines | None:
    return cart_from_shopify(await fetch_json(page, "/cart.js"))


async def shopify_item_count(page: Page) -> int | None:
    data = await fetch_json(page, "/cart.js")
    return int(data["item_count"]) if isinstance(data, dict) and isinstance(data.get("item_count"), int) else None


async def open_cart(stagehand: Stagehand, page: Page) -> CartLines:
    """The cart's lines and subtotal: from Shopify's /cart.js when the store has it, else read by the model."""
    if cart := await shopify_cart(page):
        log.info("  Cart read from the store's /cart.js (no model call)")
        await try_goto(page, urljoin(await page.url(), "/cart"))  # for the cart.png evidence
        await settle(page, SETTLE_MS)
        return cart
    for _ in range(3):
        cart = (
            await stagehand.extract(
                "Is a cart page or cart drawer open? List its line items, subtotal and whether there is a checkout button.",
                CartLines,
            )
        ).data
        if cart.is_cart_visible and cart.items:
            return cart
        step = await llm_act(
            stagehand, "Open the shopping cart (click the cart/bag icon or link in the header, or 'View cart')."
        )
        log.info(f"  Cart navigation: {step.description}")
        await settle(page, SETTLE_MS)
    raise ElementNotFound("Couldn't open the shopping cart")


def match_name(name: str) -> str:
    """Normalised product name without a leading/trailing 'coffee', which carts add or drop freely."""
    return norm_name(COFFEE_AFFIX.sub("", name)) or norm_name(name)


def compare_prices(added: list[Item], cart: CartLines, checkout: CheckoutDetails | None) -> Report:
    """Shop-page prices against the checkout summary. `checkout` is None if checkout was never reached."""
    # No containment matching: 'Coffee' must not match 'Good Day Coffee'
    find_line = pop_best_line(checkout.items if checkout else [], threshold=0.5, containment=False, norm=match_name)
    expected_total = compare_item_prices(added, find_line)
    subtotal = parse_price(checkout.subtotal) if checkout else None
    total = parse_price(checkout.total) if checkout else None
    fees = [{"label": f.label, "amount": parse_price(f.amount)} for f in (checkout.other_fees if checkout else [])]
    fees_total = money_sum(parse_price(f.amount) for f in (checkout.other_fees if checkout else [])) or 0.0
    reasons = [
        reason
        for reason, hit in (
            ("item price increased at checkout", any(i["price_increased"] for i in added)),
            ("checkout subtotal higher than selected prices", positive(diff(subtotal, expected_total))),
            ("non-tax, non-shipping fees added", fees_total > 0),
        )
        if hit
    ]
    return {
        "items_added": len(added),
        "expected_subtotal": expected_total,
        "cart_subtotal_display": cart.subtotal,
        "checkout_subtotal_display": checkout.subtotal if checkout else None,
        "checkout_subtotal": subtotal,
        "subtotal_vs_expected_diff": diff(subtotal, expected_total),
        "shipping": (checkout.shipping if checkout and checkout.shipping else "not calculated (address required)"),
        "other_fees": fees,
        "other_fees_total": fees_total,
        "tax_amount_display": checkout.tax_amount if checkout else None,
        "tax_included": checkout.tax_included if checkout else None,
        "checkout_total_display": checkout.total if checkout else None,
        "checkout_total": total,
        "currency": checkout.currency if checkout else None,
        # The storefront's checkout opens before an address is entered, so shipping is usually still unknown here.
        "shipping_calculated": bool(checkout and parse_price(checkout.shipping) is not None),
        **drip_outcome(reasons, final_price_was_read(checkout, total)),
        "checkout_summary": checkout.summary if checkout else None,
    }


async def go_to_checkout(stagehand: Stagehand, page: Page, result: SiteResult, shopify: bool) -> CheckoutDetails:
    if shopify:  # Shopify starts checkout from a plain /checkout link: no click, no model call
        log.info("  Checkout: opening /checkout directly")
        await page.goto(urljoin(await page.url(), "/checkout"))
    else:
        step = await llm_act(
            stagehand, "Click the 'Check out' / 'CHECKOUT' / 'Checkout' button to proceed to the checkout page."
        )
        log.info(f"  Checkout: {step.description}")
    await settle(page, 3000)

    # If the page hasn't left /cart (e.g. the button was a decoy link or needed a form submit), navigating
    # straight to /checkout activates the session's checkout on Shopify and other platforms.
    curr_url = await page.url()
    if "checkouts" not in curr_url and "checkout" not in curr_url.split("?")[0].rstrip("/").split("/")[-1]:
        log.info("  Still on cart; navigating to /checkout directly...")
        await page.goto(urljoin(result.shop_url or await page.url(), "/checkout"))
        await settle(page, 3000)

    log.info("  Waiting for checkout page to render...")
    await wait_for_render(page, 15)
    await settle(page, 2000)
    await shot(page, result.out_dir / "checkout.png", full_page=False)
    return (
        await stagehand.extract(
            "Extract the checkout order summary: line items, subtotal, shipping, any other fees, tax, total and currency. "
            "Also report if a login, CAPTCHA or bot check is blocking checkout.",
            CheckoutDetails,
        )
    ).data


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------
class CoffeeWorkflow(Workflow):
    name = "coffee"
    help = "Coffee stores: add 1-5 coffees to the cart and compare shop prices with checkout"
    system_prompt = SAFETY_PROMPT
    clear_cookies = "domain"

    @classmethod
    def add_arguments(cls, parser: argparse.ArgumentParser, settings: Settings) -> None:
        parser.add_argument("urls", nargs="+", help="Coffee website URL(s)")
        parser.add_argument(
            "--products",
            type=int,
            default=3,
            choices=range(1, 6),
            metavar="N",
            help="Number of coffee products to add to the cart (1-5, default 3)",
        )

    def targets(self) -> list[str]:
        return self.args.urls

    async def run(self, stagehand: Stagehand, page: Page, url: str, result: SiteResult) -> None:
        out = result.out_dir
        log.info(f"\n=== {url} ===")
        await page.goto(url)
        await settle(page, SETTLE_MS)
        await dismiss_popups(stagehand, page, POPUPS)

        shop_url = result.shop_url = await find_shop_page(stagehand, page)
        log.info(f"  Shop page: {shop_url}")

        products = (
            await stagehand.extract(
                "Extract every product listed on this page with its price, availability, whether it is coffee, its type and link.",
                Products,
            )
        ).data.products
        write_csv(out / "products.csv", list(Product.model_fields), [p.model_dump() for p in products])
        log.info(f"  Extracted {len(products)} products ({sum(p.is_coffee for p in products)} coffee)")

        chosen = choose_coffees(products, self.args.products)
        if not chosen:
            result.fail(Status.NO_COFFEE_FOUND, "No in-stock coffee products on the shop page")
            return

        for idx, product in enumerate(chosen):
            log.info(f"  Adding ({idx + 1}/{len(chosen)}): {product.name} ({product.price})")
            try:
                item = await add_product_to_cart(stagehand, page, product, shop_url)
            except BROWSER_ERRORS as error:  # keep going with the remaining products
                item = new_item(product.name, product.price, error=str(error))
                log.warning(f"    Skipped: {type(error).__name__}: {error}")
            result.items.append(item)
            # Only go back to the shop if there are more products to add; staying on the final
            # product page preserves any auto-opened cart drawer.
            if idx < len(chosen) - 1:
                await page.goto(shop_url)
                await settle(page, 1000)

        if not result.added:
            result.fail(Status.ADD_TO_CART_FAILED, "No products could be added to the cart")
            return

        cart = await open_cart(stagehand, page)
        log.info(f"  Cart: {len(cart.items)} line(s), subtotal {cart.subtotal}")
        await shot(page, out / "cart.png", full_page=False)

        shopify = (cart.reason or "").endswith("/cart.js")
        checkout = await go_to_checkout(stagehand, page, result, shopify) if cart.has_checkout_button else None
        result.checkout_url = await page.url()
        result.comparison = compare_prices(result.added, cart, checkout)
        if checkout is None:
            result.fail(Status.NO_CHECKOUT, "Cart has no checkout button")
        elif checkout.blocked_reason:
            result.fail(Status.BLOCKED, checkout.blocked_reason)
        elif not checkout.is_checkout_page:
            result.fail(Status.NO_CHECKOUT, "Checkout page not reached")
        else:
            result.status = Status.OK

    def summarise(self, result: SiteResult) -> None:
        c = result.comparison
        log.info(
            f"  Expected subtotal: {c['expected_subtotal']} | Checkout subtotal: {c['checkout_subtotal_display']} "
            f"| Total: {c['checkout_total_display']} | Fees: {c['other_fees_total']} | Shipping: {c['shipping']}"
        )
        log.info(f"  Drip pricing detected: {drip_verdict(c)}")
