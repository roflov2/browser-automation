# Review of `audit/` at `0de8176` (Phase 1)

Branch: `claude/harden-audit`, cut from `feature/consolidation`. Nothing in the package has been changed yet.
No live workflow was run. Evidence comes from reading every module, diffing `main` (`85e08d1`), the committed
`output/` runs, the installed `stagehand==4.4.0` source, and one local probe of an init script in headless Chromium
against an HTML fixture (no site, no LLM).

Status column: every finding is **open** until you approve the plan. Later phases will mark each one
fixed, deferred (with a reason) or rejected.

## Baseline (measured at `0de8176`)

| Metric | Value |
| --- | --- |
| Python lines / source lines (radon SLOC) | 3,418 / 2,736 |
| `ruff check` (default rules) | 15 issues; 9 of 15 files fail `ruff format --check` |
| Functions with cyclomatic complexity ≥ 11 (radon) | 14; worst: `pricing.compare_prices` 46, `store.ensure_address_and_open_store` 39, `virgin.compare_prices` 32, `store.walk_to_checkout` 25, `store.add_menu_item_to_cart` 23 |
| Broad excepts | 25 (7 `except Exception`, 18 `suppress(Exception)`) |
| Fixed sleeps | 52 call sites (35 `settle`, 17 `wait_for_timeout`) plus 5 one-second polling loops |
| LLM call sites | 26 `act`, 19 `extract`, 1 `observe` |
| Direct JS clicks | 12 `.click()` (10 in `ubereats/store.py`, 1 in `direct_site.py`, 1 in `page.py:closeDialog`) |
| `print()` calls | 92 |
| Tests / lint config / type config / pins | none |

The brief quoted complexity 19 / 18 / 16 for the three `store.py` functions and 11 raw clicks; radon gives 39 / 25 / 23
(it counts boolean operators) and there are 12 clicks (`closeDialog` in `page.py` is the twelfth).

Token split by operation, from the one run per process whose `metrics_cumulative` is not polluted by earlier sites:

| Run | Tokens | `extract` | `act` | `observe` |
| --- | --- | --- | --- | --- |
| `coffee` (redbrick) | 134k | 50% | 33% | 18% |
| `virgin` CBR→SYD | 554k | 52% | 46% | 2% |
| `virgin` BNE→PER | 286k | 50% | 50% | <1% |
| `ubereats` | 467k | 78% | 19% | 3% |

So `virgin`'s cost is half `act`, not just the per-step `extract`; Phase 5 has to attack both.

## Stagehand 4.4.0 facts, re-verified

Confirmed as stated in the brief: Python ≥ 3.11; `act()` takes `str | ActionInput | Action`; `act`/`observe` take
`variables`, `timeout`, `locator`, `ignore_locators`, `cache`; `extract` takes all of those except `variables`;
`Stagehand.create()` takes `dom_settle_timeout_ms`, `self_heal`, `cache`; `local_browser.launch()` takes
`executable_path`; `browserbase` is a declared dependency; `py.typed` ships.

Additions and corrections that change the plan:

- **`cache` does nothing locally.** The schema docstring reads "Server-side caching of act/observe/extract results …
  Requires client Browserbase transport and a browser sessionId." With local Chrome the status is `DISABLED`. It only
  serves a key after a hit-count `threshold`. Not a lever for local runs; see Phase 5.
- **`variables` keeps values from the model.** The schema says variable *names* are exposed "so observe() returns
  `%variableName%` placeholders in suggested action arguments instead of literal values". `extract()` has no
  `variables`, so the passenger details in `fill_passenger_details`' extract instruction need a different fix (H7).
- **`BrowserContext.add_init_script` exists** as well as `Page.add_init_script`, so the guard can be installed once
  for every page and tab. `browser.context` is only available after `Stagehand.create()`.
- **`BrowserContext.set_domain_policy(allowed_domains/blocked_domains)` exists.** Possible extra layer; not adopted
  without your say (it can break checkout pages that load payment scripts).
- **`page.evaluate()` and `wait_for_function()` take an expression, not a function.** A bare `"() => …"` evaluates to
  a function object, which serialises to `{}` (truthy). This is why `js()` wraps an IIFE, and it means the old
  `airlines-virgin.py` `wait_for_render` was a one-second no-op (see consolidation change 5).
- **No `__aenter__`** on `Stagehand` or the browser: `async with` needs an `asynccontextmanager` wrapper.
- `Page.click(x, y)` is coordinate-based; element clicks are `page.locator(sel).click()`.

## What the consolidation changed (main `85e08d1` → `0de8176`)

Method: every string literal, numeric wait/retry argument and constant was extracted from the three old scripts with
`ast` and diffed against the new modules, then the runner/compare/report code was read side by side.

**Came across intact:** every `act`/`extract` instruction in the three workflows; `SAFETY_PROMPT` for `virgin` and
`ubereats` (and the absence of one for `coffee`); all retry limits (`MAX_*`, loop ranges); every `settle` and
`wait_for_timeout` duration; viewport (`coffee` default, others 1440×1000); cookie policy (`coffee` domain-only,
`virgin` all, `ubereats` none); profile policy (`virgin` fresh, others persistent); log levels (`virgin` info, others
warn); matching thresholds (`coffee` 0.5 without containment on affix-stripped names, `ubereats` 0.4 with containment).

**Changed:**

| # | Workflow | Change | Effect |
| --- | --- | --- | --- |
| 1 | coffee, virgin | `parse_price("Free")` returns `0.0`, was `None` (the Uber Eats parser was adopted for all) | An offer priced "Free" now has `price` 0.0 in `extras.csv`; see H2 for the worse side of this rule |
| 2 | virgin | Telemetry samples current RSS via psutil, was peak RSS via `resource.getrusage` | `avg_memory_mb` now means something different; zeros if psutil is missing |
| 3 | virgin | `session.json` key `avg_cpu_load` (1-minute load average) became `avg_cpu_percent` (process CPU %) | **Renamed key and changed quantity** in an output file; needs a `MIGRATION.md` entry |
| 4 | all | The popup `observe()` prompt was reworded: `coffee`/`virgin` gained "'Maybe later' or 'Skip'"; `ubereats` lost "accept-cookies" for "accept" and was reordered | LLM prompt change; `coffee`/`virgin` may now click a "Skip" they used to ignore |
| 5 | virgin | `wait_for_render` now actually waits. The old expression was never invoked, so it returned after 1 s | Up to 20 s (results) and 10 s per booking step of real waiting that `main` never did; part of `virgin` wall time from here on |
| 6 | coffee | Checkout render wait went from "body text > 100 chars" to the shared check (readyState complete, > 200 chars, no visible spinner/loader) | Can wait the full 15 s on a page with a permanently visible `[class*=loader]` element |
| 7 | coffee, ubereats | `LineItem` field descriptions (sent to the model as schema) changed: variant text; price example `$16.50` → `$21.00` | Schema prompt change |
| 8 | virgin, ubereats | `extras.csv` is written only when there are extras; both scripts used to write it always (header only) | A file in the output contract can now be missing |
| 9 | virgin | The CDP noise filter now applies to its `history` and stdout; it used to record everything (60 of 133 entries in the saved run are that noise) | Smaller `history` |
| 10 | coffee | Reports are written on error and early exit too; JSON gains `extras`, `steps`; skipped items gain `selected_*: null` | Additive |
| 11 | coffee | Overlay pre-check minimum height 60 px → 50 px | Slightly more `observe` calls |
| 12 | ubereats | `direct_website_url`, `fee_breakdown` moved inside the comparison block of the JSON | Key order only |
| 13 | all | `session.json` gains `workflow`; `--local` and `--login` exist on every subcommand (`--login` rejected for `virgin`); missing API key is a `SystemExit` message, was `KeyError` | Additive / CLI |

Changes 3, 4, 7 and 8 are the ones I would not have guessed from the commit message. Question 5 below asks whether to
keep or revert 4 and 7.

## Findings

Format: **ID `module:function`** — what is wrong; a concrete failure; proposed fix.

### Critical

**C1 `ubereats/store.py`, `coffee.py`, `virgin.py`, `page.py` (all click paths)** — Nothing in code prevents a
purchase. The only protection is a sentence in the system prompt for two workflows, and `CoffeeWorkflow` has none.
The committed Uber Eats run ends signed in, with `Mastercard ••••2905` saved and two live "Place order" buttons.
Specific holes:

- `store.py:JS_CLICK_CHECKOUT` prefix-matches `^(Go to checkout|Checkout|Continue to checkout)`, so a button reading
  "Checkout · Pay $54.20" or "Checkout with PayPal" is clicked without any model involved.
- `store.py:JS_DECLINE_UPSELL` clicks an exact "Continue", "Next" or "Done" in any open dialog. On `/checkout` a
  confirmation dialog ("Your total changed. Continue?") is exactly that.
- `store.py:walk_to_checkout` keeps acting after the URL is already `/checkout` whenever the model flags an upsell
  and has not read fees: a free-text `act("… click 'Skip', 'Next' or 'Continue to checkout' …")` runs on the page
  that carries "Place order". The upsell screenshot (`step-02-upsell-modal.png`) is that state.
- The cart drawer has a pre-checkout checkbox "Save an extra $4.99 … with a free trial of Uber One" and the store
  page a "Join now" tile; an `act()` that mis-targets either starts a membership.
- `coffee.py:go_to_checkout` lands on Shopify checkout, which opens with one-click "Shop Pay", "PayPal" and "G Pay"
  buttons (see `redbrick.coffee/…/checkout.png`). The persistent profile can hold a Shop Pay login.
- `virgin.py:walk_to_payment` gives the model "click the button that continues … towards payment" up to 25 times; the
  payment page has "Book with Apple Pay" and "Join for free".
- `page.py:dismiss_popups` executes whatever `observe()` returns, unvetted.
- `direct_site.py:take_direct_step` navigates with `page.goto(href)` and clicks tagged elements on third-party
  restaurant ordering sites; its `SKIP` regex is the only filter.

Failure: one wrong click places a real paid order on your card. Fix: the guard in "Guard design" below.
Operational advice that code cannot enforce: use an Uber account with **no saved payment method** for audits.

**C2 `output/` (tracked, 69 files, 33 MB, public repo)** — Personal and account data is committed. `output/` is not
in `.gitignore`. Files that show it:

| File (under `output/ubereats.com/20261116-155719/`) | Shows |
| --- | --- |
| `checkout.png`, `step-03-checkout.png` | Delivery address "1 Jeffcott Place, Latham Canberra ACT"; "Mastercard ••••2905"; Uber balance; signed-in checkout |
| `step-02-upsell-modal.png` | Same address and the card line, partly visible behind the modal |
| `store.png`, `cart.png`, `step-01-cart-drawer.png` | Address "1 Jeffcott Place" in the header |
| `price_comparison.json` | `search.address` "1 Jeffcott Place, Canberra ACT 2615"; `shop_url` carries a `pl=` parameter that base64-decodes to the address, **latitude/longitude** and an Uber place reference ID |

Not personal, for completeness: both `virginaustralia.com/*/session.json` files hold the passenger details in
`history`, but they are the placeholders (`alex.testbooking@example.com`); the Virgin screenshots show empty forms;
the coffee screenshots show no account data; reviewer names in `store.png` are public page content. `summary.csv`
holds no personal data.

I have removed nothing. Options are in question 1. Note that untracking does not remove the files from history or
from GitHub's caches; only a history rewrite (which the rules forbid me to do) or deleting the repo does, and the
card's last four digits plus home address are already public as of the push.

Related leak that will recur: `runner.py` writes Stagehand's `history` to `session.json`, and at log level `info`
that includes full instructions, so real `PAX_*` values would be written there (H7).

### High

**H1 `coffee.py:compare_prices`, `virgin.py:compare_prices`, `ubereats/pricing.py:compare_prices`,
`core.py:compare_item_prices`** — `drip_detected = bool(reasons)` and `price_increased = (diff or 0) > 0` turn
"not read" into "no". Evidence in the saved runs: all four coffee runs report `False` with shipping "not calculated"
(the final price was never on screen); the Uber Eats run has `checkout_price: null` for both items yet
`price_increased: false`. A run that dies before checkout also reports `False`. Fix: three-valued result. `True` if
any reason was found (positive evidence stands even from a partial run); `False` only if the final price screen was
read and no reason was found; otherwise `None`. Same for `price_increased` when the checkout line is missing. Add a
`final_price_read` field. This changes a value in output files; listed in `MIGRATION.md`.

**H2 `core.py:parse_price`** — Measured outputs:

| Input | Now | Should be |
| --- | --- | --- |
| `-$11.85`, `($5.00)` | 11.85, 5.0 | −11.85, −5.0 |
| `Free on $50+` (a real badge in the saved Uber Eats screenshot) | 0.0 | None (conditional) |
| `Gluten free base $3` | 0.0 | 3.0 |
| `2 for $30`, `3x $5` | 2.0, 3.0 | None or 30.0 / 5.0 |
| `$.50` | 50.0 | 0.5 |
| `1,5` | 15.0 | 1.5 |
| `$5 10 min` | 510.0 | 5.0 |
| `1,234` / `12,50` / `1.234,56 €` / `1,234,567` | 1234.0 / 12.5 / 1234.56 / 1234567.0 | correct |
| `1.3%` via `parse_amount` | None | correct |

Failure: a promotion line the model files under `other_fees` is added as a positive fee and flags drip; a "Free on
$50+" delivery badge records an advertised fee of $0 and then flags "delivery fee higher than advertised". Fix:
keep the sign; treat "free" as zero only when the string has no digits; prefer the number attached to a currency
marker; accept a leading decimal point; treat a single trailing group of 1–2 digits after a comma as decimals; stop
at the first whitespace that is not a thousands separator. All covered by Phase 2 tests before the change.

**H3 `ubereats/pricing.py:compare_prices`** — Discounts are not modelled. Saved run: subtotal $47.40, service $5.69,
delivery $4.99, promotion −$11.85, total $46.23, so `total_vs_expected_diff` is −1.17 and reads as "cheaper than
advertised" while $10.68 of fees were added. Fix: add `discounts_total` and `fees_and_delivery_total`, and compute
the total check as `total − (subtotal + fees − discounts)`. Additive fields; `total_vs_expected_diff` keeps its
current definition.

**H4 `core.py:update_summary`** — One row per `site`, rewritten in place with no temp file. Evidence: two Virgin
runs are committed (BNE→PER and CBR→SYD) and `summary.csv` holds only the second. Every Uber Eats run has
`site = https://www.ubereats.com/au`, so restaurants replace one another. A crash mid-write truncates the file.
Fix: append every run to `output/runs.csv`; rebuild `summary.csv` from it with the same 13 columns; write both via
temp file and `os.replace`. Proposed key in question 3.

**H5 falsy-zero and currency** — Every `or`/truthiness test on a price, checked:

| Location | Code | Failure |
| --- | --- | --- |
| `coffee.py:257` | `parse_price(state.current_price) or item["shop_price"]` | A real $0.00 selected price becomes the shop price |
| `store.py:481` `set_price` | `parse_price(display) or item["shop_price"]` | Same |
| `pricing.py:44` | `… for label, amt in (…) if amt` | A $0.00 service or small-order fee row disappears from `other_fees` |
| `pricing.py:39` | `base = subtotal or expected_subtotal` | A $0 subtotal silently switches base |
| `pricing.py:63` | `small_order or 0.0`, `pct_of(…) or 0.0` | "Not shown" is reported as a charged $0.00 |
| `core.py:pct_of` | `if amount and base` | 0% is reported as `None` |
| `pricing.py:71,94` | `if service_pct` | A 0% service fee loses its label |
| `direct_site.py:231` | `not price` | A $0 direct price is dropped |
| `direct_site.py:234` | `item.get("shop_price") or item.get("selected_price")` | $0 shop price falls through |
| `pricing.py:142` | `shown("currency") or "AUD"` | Currency is asserted when none was shown |

Not bugs (checked): `summarise_extras` `(price or 0) > 0`, `choose_menu_items` `> 3.0`, the `chargeable` filter, and
the display-string fallbacks (`shown("delivery_fee") or "not shown"`). Fix: `is None` tests throughout; currency
stays `None` when not shown, with a separate `currency_assumed: "AUD"` field (changes a value: question 4).

**H6 silent failures** — 25 broad handlers. The ones that can corrupt a result:

- `page.py:js` returns `default` on any exception. A typo in `JS_CART_COUNT` makes `get_cart_count` return 0 for
  ever: `clear_existing_cart` believes the cart is empty, leftovers inflate the checkout subtotal, and the run flags
  "checkout subtotal higher than menu prices". Fix: `js()` returns `None` only when the page returned null and raises
  `PageScriptError` when evaluate threw; add `js_or(default)` for the few call sites that want a fallback on null.
- `runner.py:audit_site` suppresses a failed `clear_cookies`; the run continues with a stale cart or session.
- `virgin.py:fill_passenger_details` suppresses every failed `act`; the loop then reports "stuck" three steps later.
- `page.py:dismiss_popups` suppresses everything, including a refusal the guard will raise.
- `runner.py:run` sets `after = before` when `metrics()` fails, so `tokens_used` is a silent 0.
- `store.py:extract_pl_address_from_url` suppresses everything around base64/JSON decoding (fine in intent; narrow
  it to `ValueError`, `binascii.Error`, `UnicodeDecodeError`).
- The runner maps outcomes to `blocked` or `error` only. A timeout, a missing element and a changed flow all read
  `error: RuntimeError: …`.

Fix: the exception hierarchy and `failure_reason` in Phase 4; each remaining `suppress` names its exception types
and logs at debug.

**H7 `virgin.py:fill_passenger_details`, `runner.py:run`** — Passenger details go to the model and to disk. They
are interpolated into the `extract()` instruction and into each `act()` instruction; the saved `session.json`
`history` contains the full instruction text. With real `PAX_*` values, name, date of birth, email and phone would
be sent to Gemini and written to a file that is currently tracked. Fix: have the extract return which *field kind*
each form input is (`first_name`, `email`, …) without being told the values, then `act("Type %first_name% into …",
variables={…})`. Scrub variable values from `history` and from `extractions.jsonl`. This changes two prompts
(question 5).

**H8 `ubereats/__init__.py:UberEatsWorkflow.run`, `virgin.py:VirginWorkflow.run`** — Work in `finally` runs after
any failure. In `ubereats`, the direct-restaurant audit (navigation plus up to five `act` calls on a third-party
ordering site) runs after `Blocked`, after an arbitrary exception, and would run after a guard refusal. An exception
raised by `compare_prices` inside `finally` replaces the original one. Fix: the direct-site audit becomes an explicit
step that is skipped on `unsafe_action_refused`; `finally` only computes the comparison, guarded so it cannot mask.

### Medium

**M1 `runner.py:ResourceMonitor`, `runner.py:run`** — One monitor per process. Evidence: the four coffee
`session.json` files (one invocation) have `data_points` 217, 439, 622, 834, and `metrics_cumulative` grows the same
way. Fix: one monitor per site; add `metrics` (delta) next to `metrics_cumulative`.

**M2 `audit/__init__.py`, `ubereats/__init__.py:DEFAULT_ADDRESS`, `page.py:pause_for_login`, `runner.py`,
`virgin.py:Passenger.from_env`** — Import-time side effects and config in five places. Importing `audit` overwrites
`NO_PROXY` (it replaces a corporate value instead of appending) and reads `.env`. `UBEREATS_ADDRESS` is read at
import and only works because `audit/__init__` happens to run `load_dotenv()` first. `HEADLESS` is read in two
modules. `CHROME_PATH` is documented and never read. Fix: `audit/config.py` with one frozen settings object built in
`cli.main`; `NO_PROXY` is appended to, not replaced; `CHROME_PATH` → `executable_path`.

**M3 `page.py:install_noise_filter`, `_FilteredStream`** — Replaces `sys.stdout`/`sys.stderr` for the life of the
process, adds a new filter object to four loggers on every call, and drops a whole `write()` chunk if any part of it
contains a noisy phrase (a traceback that quotes one disappears). Fix: a context manager scoped to the session that
restores streams and removes filters; filter per line.

**M4 fixed sleeps** — 52 call sites. `settle()` always sleeps its full duration after load. `wait_for_render()`
sleeps a second before its first check. `coffee.py` has 13 waits on its path and `store.py` 22. Fix in Phase 4:
`wait_for_selector`/`wait_for_function` with bounded timeouts, `dom_settle_timeout_ms` tried first.

**M5 `runner.py:run`, `launch_browser`** — Nested `try/finally` with suppressed closes; the monitor task is
cancelled but never awaited; a Browserbase session created before a failed `connect()` is never released; output
folders use naive local time at one-second resolution. In my probe the process did not exit after
`stagehand.close()` followed by `browser.close()` (it had to be killed); I will find out which of the two calls is
redundant before restructuring. Fix: `asynccontextmanager` wrappers and an `AsyncExitStack`.

**M6 `ubereats/pricing.py:compare_prices` (complexity 46), `store.py` three functions (39, 25, 23),
`virgin.py:compare_prices` (32), `UberEatsWorkflow.run` (22), `coffee.py:compare_prices` (20)** — Too long to test
piecewise. Fix: split into named steps with no behaviour change, after the characterisation tests exist.

**M7 `ubereats/pricing.py:compare_prices`** — Item-level comparison is vacuous in the saved run: the checkout page
collapses the cart ("Cart summary (2 items)"), `final = checkout or cart` then uses the checkout's empty item list,
and both items get `checkout_price: null`. Fix: fall back to the cart-drawer lines when the checkout lists none, and
record which source was used.

**M8 `core.py:best_match`** — Containment scores 0.8 for any substring. `best_match("Fries", ["OOSH Fries Large",
"Lebo Fries"])` returns the first, so a direct-site price can be attached to the wrong dish and reported as a
markup. Fix: require containment on whole tokens and prefer the candidate with the highest token overlap.

**M9 `coffee.py:add_product_to_cart`** — `status = "added"` rests on the model's own `success` flag. An item that
never reached the cart still counts toward `expected_subtotal`; its checkout line is missing, which today reads as
"no increase". Fix: confirm against `/cart.js` on Shopify (also Phase 5's cheapest win), else against the cart
extraction.

**M10 `ubereats/__init__.py:UberEatsWorkflow.run`** — `status = "ok"` if any fee string was read, even when
`is_checkout_page` is false, so a cart-drawer reading can be reported as a checkout result. Fix: tie `ok` to
`final_price_read`.

**M11 no record of what the model read** — `price_comparison.json` holds derived values only, and five of seven
`session.json` files have an empty `history`. A wrong figure cannot be traced. Fix: `extractions.jsonl` (Phase 2).

**M12 dependencies** — Nothing is pinned; `psutil` is "optional" and its absence produces zeros that look like
measurements. Fix: pins in `pyproject.toml`; `resource_usage: null` when psutil is missing.

### Low

- **L1** 92 `print()` calls, no levels, no `--verbose`/`--quiet`.
- **L2** No `pyproject.toml`, tests, lint or type config. README says Python 3.10+; the dependency needs 3.11.
- **L3** `workflow.py:Workflow.item_fields` is a mutable class-level list; `clear_cookies` is a stringly-typed
  `"domain" | "all" | None`; statuses are bare strings in 20 places.
- **L4** `virgin.py` redefines `LineItem`; `core.SiteResult.items` and friends are `list[dict]` with ~25 implicit keys.
- **L5** `store.py:is_login_url` matches any URL containing `/login`.
- **L6** `direct_site.py:find_direct_restaurant_urls` scrapes DuckDuckGo then Google result pages. Google will often
  answer with a CAPTCHA; that must be recorded and skipped, never worked around (rule 2). Today it silently returns
  no candidates.
- **L7** Blocking file writes inside async functions (4 sites). Harmless at this scale; left as is unless ruff's
  `ASYNC` rules are enabled.

### Decimal

Recommended, narrowly. All arithmetic already funnels through `money_sum`, `diff`, `pct_diff`, `pct_of`, plus two
inline expressions (`round(unit / quantity, 2)` and the delivery percentage in `pricing.py`). Parsing to `Decimal`,
doing the arithmetic in `Decimal` inside those helpers, quantising half-up and returning `float` at the boundary
costs about 15 lines and keeps the JSON and CSV byte-identical for every value in the saved runs (the
characterisation tests will prove that). `SiteResult` and the dict fields stay floats. If the tests show any
serialised value changing, I will stop and leave floats.

## Guard design (`audit/safety.py`)

One rule set, written once in Python as data and injected into the page script with `json.dumps`, so the Python and
JavaScript lists cannot drift. Patterns are restricted to syntax that Python `re` and JavaScript `RegExp` read the
same way, and the unit tests run every case through both.

**Deny (click, press, submit)** on the normalised label (visible text + `aria-label` + `title` + `value` + image
`alt`) of the target or its nearest `button`, `a`, `[role=button]`, `input[type=submit|button|image]`, `label`,
`summary` ancestor:

- purchase: `place (your )?order`, `pay now`, `pay` followed by an amount, `confirm (and|&) pay`, `purchase`,
  `complete (booking|order|purchase|payment)`, `buy (it )?now`, `book (now|with …)`, `submit (order|payment)`,
  `confirm (booking|order|purchase)`, `order now` on a checkout URL only (restaurant sites use it as navigation)
- wallets: `apple pay`, `google pay`, `g pay`, `shop pay`, `paypal`, `afterpay`, `zip`, `klarna`, `payid`
- memberships and trials: `start (your )?(free )?trial`, `free trial`, `join (uber one|now|for free|velocity)`,
  `try uber one`, `subscribe`, `sign up`, `create account`
- sign-in buttons are not denied here (the `--login` flow is manual and the prompts already forbid it)

**Deny (typing)** when the target field's `autocomplete` starts `cc-`, or its name/id/placeholder/label matches
`card ?number`, `cvv`, `cvc`, `security code`, `expir`, `mm ?/ ?yy`, `gift ?card`, `voucher`, `promo`,
`discount code`, `coupon`, `travel bank`.

**Allow** (asserted by tests, and deny always wins): "Checkout", "Go to checkout", "Continue to checkout",
"Continue to payment", "Continue", "Add to cart", "Next", plus "Skip", "No thanks", "Not now".
"Continue to payment" passes because `pay` only matches as a whole word followed by `now` or an amount.

Layers:

1. **In-page blocker.** `browser.context.add_init_script(GUARD_JS)` once per session, plus one `evaluate` for the
   already-open page. Capture-phase listeners on `click`, `pointerdown/up`, `mousedown/up`, `keydown` (Enter/Space on
   a denied target, any key in a denied field), `beforeinput`, `paste` and `submit` (checks `event.submitter` and
   the form's default submit button). `HTMLFormElement.prototype.submit`/`requestSubmit` are wrapped for forms whose
   submit control is denied. Uses `event.composedPath()` so open shadow DOM is covered. Every block is appended to
   `window.__auditGuard.blocked`; Python reads that after each action and raises, so an in-page block also ends the
   run. Probe result (headless Chromium, local fixture, no LLM): installed in the top frame and a same-origin
   iframe, survived a navigation, and blocked JS `.click()`, a real CDP click, a nested `<span>` inside a button, a
   `role="button"` div and a shadow-DOM button, while "Go to checkout" went through. `form.requestSubmit()` was
   **not** blocked by a click-only listener, which is why the submit handling above is in the design.
2. **`safe_act(stagehand, page, instruction, …)`.** `observe(instruction)` → check each returned `Action`
   (`description`, `selector`, `method`, `arguments`) against the rules, and resolve the selector in the page to
   check the element's real label too → refuse or `act(action)`. No call site passes a string to `act()` any more.
   `variables` and `timeout` pass through. If `observe` returns nothing it raises `ElementNotFound`. I will measure
   LLM calls and tokens for `act(str)` versus `observe` + `act(Action)` on the fixture using a counting
   `LLMGenerateCallback` (no API key needed for the call count; real token figures need a key, see question 6).
3. **`safe_click` for JS.** `JS_LIB` gains `safeClick(el)`, `safeSelect(el, value)` and `safeFill(el, text)`, which
   apply the same rules and return `{done, refused, label}`; the Python `safe_click()` wrapper raises on `refused`.
   All 12 raw `.click()` sites, the two `select` changes and the address `input` fill move to these.
   `JS_CLICK_CHECKOUT` becomes an exact match.
4. **Hard stop.** `guard.restrict(page)` when a workflow first sees a checkout/payment URL or
   `is_checkout_page`/`is_payment_page`: from then on only an allow-list is clickable (Skip, No thanks, Not now,
   close). `guard.freeze(page)` once the fee breakdown has been read: every `safe_act`/`safe_click` raises and the
   in-page script cancels every click, key and submit on that origin; only `extract`, `evaluate` reads and
   screenshots remain. `page.goto` to a different origin (the Uber Eats direct-site step) is still possible.
5. **Prompts** stay as the last layer. `CoffeeWorkflow` gets: "You are auditing an online store for price
   transparency. Never click a button that pays for or places an order (e.g. 'Pay now', 'Complete order', 'Shop
   Pay', 'PayPal', 'G Pay', 'Buy it now'). Never enter card, gift-card or discount-code details, and never choose a
   subscription. Never log in or create an account."

A refusal raises `UnsafeActionRefused`; the runner records `status = "error"`, `failure_reason =
"unsafe_action_refused"`, takes a screenshot, skips every remaining step for that site and continues with the next
site. (If you would rather it abort the whole invocation, say so.)

**Tests:** both lists as parametrised unit tests in Python; the same cases through the injected script in headless
Chromium against a fixture (skipped with a clear message if no browser is found); iframe, navigation, shadow DOM,
Enter-key and `requestSubmit` cases; and a source test that fails on any `.act(` or `.click()` (including inside JS
string constants) outside `safety.py`.

**What it will not cover**, as far as I can tell today:

- Cross-origin iframes: not yet verified. The fixture had one but I did not get a reading from it; it is the first
  thing I will test. Payment buttons such as PayPal's usually live in one.
- Closed shadow roots, canvas-drawn buttons, and buttons whose only label is an unlabeled icon.
- Labels in languages other than English.
- A page that completes a purchase on a non-user event (a timer, or auto-submit after a wallet returns).
- Browser-native UI (the Apple/Google Pay sheet), which is outside the page.
- Purchases reachable by a plain GET navigation; `page.goto` targets are checked against a small URL deny list only.
- A human clicking in the headed window.
- A site that removes or overrides the listeners deliberately. Not expected from the audited sites, but possible.

## Phase plan

One commit per phase; nothing pushed. Each phase ends with `ruff`, the type checker and `pytest` green.

| Phase | Commit | Contents |
| --- | --- | --- |
| 1 | `docs: review of the consolidated audit package` | This file |
| 2 | `test: characterise core, compare_prices, writers and CLI` | `pyproject.toml` test/lint scaffolding only; unit tests for `core`; characterisation tests for the three `compare_prices` rebuilt from the saved reports, with the assertions that will change marked; `extractions.jsonl` recorder; writer and `--help` tests |
| 3a | `feat: purchase guard` | `audit/safety.py`, fixtures, guard tests, every `act`/click routed through it, hard stop, coffee prompt. Done before any other refactor because it is priority 1 |
| 3b | `refactor: settings object, packaging, split long functions` | `audit/config.py`, `drip-audit` script, pins, the three `store.py` functions and the `compare_prices` functions split with tests unchanged |
| 4 | `fix: trustworthy results and specific failures` | H1–H8, M1–M10: three-valued drip, parser, zero handling, `runs.csv`, exceptions and `failure_reason`, waits, lifecycle, logging |
| 5 | `perf: fewer and smaller LLM calls` | Shopify `/cart.js` and `/products/<handle>.js`; `locator`/`ignore_locators` on extractions; `virgin.walk_to_payment` extracts offers once per distinct step and uses a light state check otherwise; DOM-first clicks for known buttons; report estimates against the baselines |
| 6 | `docs: migration notes, README, final review status` | `MIGRATION.md`, README and `.env.example`, statuses in this file |

Phase 5 will not use `cache` for local runs (it is Browserbase-only). If you run on Browserbase I can enable it for
`observe`/`act` only and report hit rates; caching `extract` would return stale prices, so never for extraction.

If budget runs short the order of sacrifice is Phase 5, then the function splits in 3b, then logging.

## Questions before I continue

1. **`output/` (C2).** Choose one: (a) add to `.gitignore` and `git rm --cached` all 69 files; (b) the same but keep
   the non-image text reports for the four coffee runs as test fixtures under `tests/fixtures/`, which hold no
   personal data; (c) leave tracked. I recommend (b). In every case the history still holds the address, coordinates
   and card suffix; that needs your decision on a history rewrite or repo visibility, which I will not touch.
2. **Python 3.11+ in the README and `.env.example`.** Approve the correction?
3. **Summary key.** Proposed: `runs.csv` gets `workflow` and `target` columns. `target` is the site URL for
   `coffee`; `<site> <ORIGIN>-<DEST>` for `virgin` (so routes do not replace each other, but dates on the same route
   do); and the store path `/store/<slug>/<id>` for `ubereats`, falling back to the store name, then the site.
   `summary.csv` keeps its 13 columns and holds the latest run per `(workflow, target)`. The `site` value is
   unchanged, so several Uber Eats rows will share it. Should existing `summary.csv` rows seed `runs.csv`?
4. **Changed values in output files.** Beyond `drip_detected`/`price_increased` becoming `null` when unknown, which
   the brief already asks for: may `currency` be `null` when the page showed none (with `currency_assumed`), and may
   `small_order_fee` rows show `null` for "not shown" where they now show `0.0`?
5. **Prompts.** (a) Approve the coffee safety prompt above. (b) Approve the H7 change to the two passenger-form
   prompts. (c) For consolidation changes 4 and 7 (popup prompt, `LineItem` descriptions): keep the consolidated
   wording, or restore each workflow's original?
6. **Measuring `safe_act` cost.** I have no Gemini key here. I can report call counts with a stub model now; real
   token figures need either a key in this environment or you running one fixture script locally. Which?
7. **`extras.csv` when empty (consolidation change 8)** and **`avg_cpu_load` (change 3):** restore the always-written
   file and the old key, or document both in `MIGRATION.md`?

## Out of scope, noted only

A scheduler; a database behind `runs.csv`; hashing screenshots and extractions as evidence; per-jurisdiction rules;
redacting address and payment regions from screenshots before they are saved (would address the cause of C2);
`set_domain_policy` to block payment-processor domains as a network-level guard layer.
