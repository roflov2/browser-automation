"""The restaurant's own website: find it, walk Menu -> Order online -> Pickup -> Store, and read its prices."""

import contextlib
import logging
import re
from urllib.parse import quote_plus, urlparse

from stagehand import Page, Stagehand

from ...core import BROWSER_ERRORS, best_match, diff, norm_name, parse_price, pct_diff
from ...page import js, maybe_dismiss_popups, settle, shot, try_goto
from ...safety import safe_act
from .models import DirectMenuMatch, DirectSiteExtraction, UberResult, extract_suburb_hint
from .store import STREET_TYPES

log = logging.getLogger(__name__)

MAX_DIRECT_SITES = 2  # search results to try before giving up
MAX_DIRECT_SITE_STEPS = 8  # navigation steps per site
MAX_DIRECT_LLM_NAV = 3  # LLM-driven clicks per site once DOM navigation runs dry
MAX_CATEGORY_HOPS = 2  # "open the category containing X" attempts per site
MIN_PRICE_SIGNALS = 4  # price-looking strings on a page before it is worth an LLM extract
DIRECT_KIND_ORDER = ("menu", "order", "pickup", "store", "frame")
DIRECT_KIND_BUDGET = {"menu": 2, "order": 2, "pickup": 2, "store": 2, "frame": 1}

# Never treated as "the restaurant's own site": delivery platforms, search engines, social, review/listing sites.
EXCLUDED_SEARCH_DOMAINS = (
    "ubereats.com",
    "uber.com",
    "doordash.com",
    "menulog.com",
    "deliveroo.",
    "hungrypanda.",
    "easi.",
    "google.",
    "bing.com",
    "duckduckgo.com",
    "yahoo.com",
    "facebook.com",
    "instagram.com",
    "tiktok.com",
    "x.com",
    "twitter.com",
    "pinterest.",
    "youtube.com",
    "linkedin.com",
    "reddit.com",
    "wikipedia.org",
    "apple.com",
    "tripadvisor.",
    "yelp.",
    "zomato.com",
    "restaurantguru.",
    "foursquare.com",
    "opentable.",
    "thefork.",
    "quandoo.",
    "agfg.com.au",
    "broadsheet.com.au",
    "timeout.com",
    "yellowpages.",
    "whitepages.",
    "truelocal.",
    "localsearch.",
    "eatability.",
)
NON_SUBURB_WORDS = STREET_TYPES | {
    "act",
    "nsw",
    "vic",
    "qld",
    "sa",
    "wa",
    "tas",
    "nt",
    "australia",
    "shop",
    "unit",
    "level",
    "the",
    "and",
}


def clean_store_search_name(name: str | None) -> str:
    """Strip suburb suffixes in parentheses e.g. 'BZ Burgers (Belconnen)' -> 'BZ Burgers'."""
    if not name:
        return ""
    return re.sub(r"\s*\([^)]*\)\s*", " ", name).strip() or name.strip()


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
            if (!vis(el) || !guardAllows(el)) continue;  // never offer a control the purchase guard would refuse
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
        return safeSelect(el, value);
    }
    return safeClick(el);
}"""


async def find_direct_restaurant_urls(page: Page, store_name: str, loc_tokens: list[str]) -> list[str]:
    """Search for the restaurant's own site; results whose domain or title carries the restaurant name rank first."""
    name = clean_store_search_name(store_name)
    name_tokens = [t for t in norm_name(name).split() if len(t) >= 3 and t not in ("the", "and")]
    joined = re.sub(r"[^a-z0-9]", "", name.lower())
    query = quote_plus(f"{name} {loc_tokens[0] if loc_tokens else ''} menu order online".replace("  ", " "))

    def score(c: dict) -> int:
        host = re.sub(
            r"[^a-z0-9]", "", c["host"].rsplit(".", 2)[0] if c["host"].count(".") >= 2 else c["host"].split(".")[0]
        )
        title = norm_name(c["text"])
        return (
            (4 if joined and (joined in host or (len(host) >= 5 and host in joined)) else 0)
            + sum(1 for t in name_tokens if t in host)
            + (2 if name_tokens and all(t in title for t in name_tokens) else 0)
        )

    for engine_url in (
        f"https://html.duckduckgo.com/html/?q={query}",
        f"https://www.google.com/search?q={query}&hl=en",
    ):
        if await try_goto(page, engine_url):
            await settle(page, 2000)
        candidates = await js(page, JS_SEARCH_RESULTS, {"excluded": list(EXCLUDED_SEARCH_DOMAINS)}, default=[])
        if candidates:
            ranked = sorted(candidates, key=score, reverse=True)  # stable: search order breaks ties
            named = [c for c in ranked if score(c) > 0]
            return [c["href"] for c in (named or ranked[:1])][:MAX_DIRECT_SITES]
        # No results usually means the search engine showed a bot check. That is never worked around.
        log.info(f"    No usable search results from {urlparse(engine_url).netloc}")
    return []


def apply_direct_matches(pending: list[dict], matches: list[DirectMenuMatch]) -> None:
    """Write matched direct prices onto the items and drop them from `pending`."""
    for m in matches:
        price = parse_price(m.direct_price_display)
        idx = best_match(m.uber_item_name, [i["name"] for i in pending])
        if idx is None or not m.direct_item_name or price is None:
            continue
        item = pending.pop(idx)
        uber_price = item["shop_price"] if item.get("shop_price") is not None else item.get("selected_price")
        item["direct_item_name"] = m.direct_item_name
        item["direct_price_display"] = f"${price:.2f}"
        item["direct_price"] = price
        item["uber_vs_direct_diff"] = diff(uber_price, price)
        item["uber_vs_direct_markup_pct"] = pct_diff(uber_price, price)
        if uber_price is not None:
            log.info(
                f"    Direct match: '{item['name']}' -> Uber Eats ${uber_price:.2f} vs "
                f"direct '{m.direct_item_name}' ${price:.2f} "
                f"({item['uber_vs_direct_diff']:+.2f} / {item['uber_vs_direct_markup_pct']:+}%)"
            )


async def extract_direct_prices(stagehand: Stagehand, store_name: str, pending: list[dict]) -> str:
    """One LLM extract for the still-unmatched items; returns the page_state."""
    wanted = "; ".join(
        f"'{i['name']}'"
        + (f" [{i['category']}]" if i.get("category") else "")
        + f" (Uber Eats {i['shop_price_display']})"
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
    log.info(f"    Direct page: {data.page_state} | still missing {len(pending)} item(s) | {data.summary[:100]}")
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
                await try_goto(page, a["href"])
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
            await settle(page, 350)

        scan = await js(
            page,
            JS_DIRECT_SCAN,
            {"tokens": loc_tokens},
            default={"url": await page.url(), "prices": 0, "needsStore": False, "actions": []},
        )
        can_navigate = any(
            f"{a['kind']}|{a['text'].lower()}|{a['href'].split('#')[0]}" not in tried
            and used.get(a["kind"], 0) < DIRECT_KIND_BUDGET[a["kind"]]
            for a in scan["actions"]
        )
        log.info(
            f"    Direct step {step}: {scan['url']} | price signals={scan['prices']} | nav options={len(scan['actions'])}"
        )

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
                act_res = await safe_act(
                    stagehand,
                    page,
                    f"On this menu, click the category tab or section link most likely to contain '{target['name']}'"
                    + (f" (it is under '{target['category']}' on Uber Eats)" if target.get("category") else "")
                    + ". Do not add anything to the cart.",
                )
                log.info(f"    Category hop: {act_res.description}")
                force_extract = True
                await settle(page, 1800)
                continue

        action = await take_direct_step(page, scan, tried, used)
        if not action:
            if llm_navs >= MAX_DIRECT_LLM_NAV:
                return
            llm_navs += 1
            act_res = await safe_act(
                stagehand,
                page,
                "We need this restaurant's food menu WITH PRICES. Do the single next step towards it, in this order "
                "of preference: (1) open the food 'Menu'; (2) otherwise click 'Order online' / 'Order now'; "
                "(3) choose 'Pickup' / 'Takeaway' rather than delivery; "
                f"(4) if asked to choose a store or location, choose the one in or nearest to {where} "
                "(type the suburb into the store search if needed), and choose ASAP / the earliest time if asked. "
                "Never open Uber Eats, DoorDash, Menulog or Deliveroo links, never sign in, never add items or pay."
                + (" A store must be selected first." if scan["needsStore"] else ""),
            )
            action = f"LLM -> {act_res.description}"
        log.info(f"    Direct navigation: {action}")
        await settle(page, 2500)


async def audit_direct_restaurant_site(
    stagehand: Stagehand,
    page: Page,
    result: UberResult,
    address: str,
    explicit_restaurant_url: str | None = None,
) -> None:
    """Visit the restaurant's own website and compare unit prices against Uber Eats."""
    added = result.added
    if not added or not result.store_name:
        return

    log.info(f"\n  --- Auditing Direct Restaurant Website for '{result.store_name}' ---")
    loc_tokens = location_tokens(result.store_name, result.store_location, address)
    urls = (
        [explicit_restaurant_url]
        if explicit_restaurant_url
        else await find_direct_restaurant_urls(page, result.store_name, loc_tokens)
    )
    if not urls:
        log.info(f"  Could not locate an independent website for '{result.store_name}'.")
        return

    pending = list(added)
    for n, url in enumerate(urls, start=1):
        log.info(f"  Direct restaurant URL ({n}/{len(urls)}): {url} | branch hints: {loc_tokens}")
        try:
            await page.goto(url)
            await settle(page, 2500)
            await maybe_dismiss_popups(
                stagehand,
                page,
                "cookie banner, promotional/newsletter popup or app-install banner",
                caution="Do NOT sign in or start an order. ",
            )
            await walk_direct_site(stagehand, page, result.store_name, pending, loc_tokens)
        except BROWSER_ERRORS as error:  # try the next candidate site
            log.warning(f"  Direct website check failed on {url}: {type(error).__name__}: {error}")
        await shot(page, result.out_dir / f"restaurant_direct_{n}.png")
        if result.direct_website_url is None or len(pending) < len(added):
            with contextlib.suppress(*BROWSER_ERRORS):  # the browser may be gone; the URL is a nicety
                result.direct_website_url = await page.url()
        if len(pending) < len(added):  # this site yielded prices; keep its page text as evidence
            text = await js(page, "() => document.body.innerText", default="")
            (result.out_dir / "restaurant_direct_menu.txt").write_text(text, encoding="utf-8")
            break

    for item in pending:
        log.info(f"    Direct match: '{item['name']}' -> not found with a price on the restaurant's site")
