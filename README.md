# browser-automation

[Stagehand](https://docs.stagehand.dev) (v4) Python workflows that drive real purchase flows and audit checkout pricing for **drip pricing** (hidden fees, surcharges, or unexpected price hikes between the advertised price and checkout).

Everything runs through one entry point, `main.py` (or the `drip-audit` command once the package is installed), with one subcommand per workflow:

| Workflow | Target | What it does |
| --- | --- | --- |
| `coffee` | Coffee roaster / retailer stores | Adds 1–5 coffees to the cart and compares shop prices with checkout |
| `virgin` | [Virgin Australia](https://www.virginaustralia.com/au/en/) | Books a one-way flight through to the payment page and records every extra and fee offered |
| `ubereats` | [Uber Eats AU](https://www.ubereats.com/au) | Orders 1–5 menu items through to checkout, records every fee, and compares menu prices with the restaurant's own website |
| `ticketek` | [Ticketek AU](https://premier.ticketek.com.au/) | Holds 1–8 tickets for an event through to the payment page and records every booking, service, handling, delivery and card fee |

No workflow ever pays: runs stop on the checkout / payment page, and a purchase guard in the code makes sure of it (see [Safety](#safety)).

**Use an account with no saved payment method** for any workflow that signs in (`ubereats`, `ticketek`). The guard is designed to make a purchase impossible, but an account that cannot pay is the last line of defence.

## Setup

Requires Python 3.11+ (the Stagehand 4 SDK needs it) and Google Chrome/Chromium. Uses Google Gemini via [Google AI Studio](https://aistudio.google.com/apikey).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"        # or: pip install -r requirements.txt
cp .env.example .env
```

Open `.env` and set `GOOGLE_GENERATIVE_AI_API_KEY`. Settings are read once, from the environment and then `.env`, and checked before anything starts: a missing key or a `CHROME_PATH` that does not exist stops the run with a one-line message.

| Variable | Default | Purpose |
| --- | --- | --- |
| `GOOGLE_GENERATIVE_AI_API_KEY` | (required) | Gemini API key |
| `GEMINI_MODEL` | `gemini-3.8-flash` | Gemini model name (with or without `google/` prefix) |
| `HEADLESS` | `false` | Run without a visible browser window when set to `true` |
| `CHROME_PATH` | auto-detected | Path to the Chrome/Chromium executable used for local runs |
| `BROWSERBASE_API_KEY`, `BROWSERBASE_PROJECT_ID` | (optional) | Run on Browserbase instead of local Chrome |
| `PAX_TITLE`, `PAX_FIRST_NAME`, `PAX_LAST_NAME`, `PAX_GENDER`, `PAX_DATE_OF_BIRTH`, `PAX_EMAIL`, `PAX_PHONE`, `PAX_COUNTRY` | placeholder guest | Guest details typed into the airline booking form (`virgin` only). Names, date of birth, email and phone are passed to the page as Stagehand variables: they are not sent to the model and are replaced by `%name%` placeholders in `session.json` and `extractions.jsonl` |
| `UBEREATS_ADDRESS` | `260 City Walk, Canberra ACT 2601` | Default delivery address (`ubereats` only) |

## Usage

```bash
python main.py --help              # list workflows
python main.py <workflow> --help   # a workflow's options
```

Options shared by every workflow:

- `--local`: use local Chrome even if Browserbase credentials are set.
- `--login`: open the site in the persistent local Chrome profile (`.chrome-profile/`) so you can sign in by hand, then exit. Later local runs reuse that login. Not available for `virgin`, which always starts from a fresh browser.
- `-v` / `--verbose`: also show debug detail (waits, errors that were handled). `-q` / `--quiet`: warnings and errors only.

## Safety

The audits drive real checkouts, so every click goes through `audit/safety.py`:

1. **In-page blocker.** A script installed on every page and frame cancels any click, Enter/Space key press or form submit on a control labelled like a purchase ("Place order", "Pay now", "Pay $54.20", "Confirm and pay", "Complete booking", "Buy now", wallet buttons such as Shop Pay, PayPal, Apple/Google Pay), a trial or membership ("Start free trial", "Join Uber One", "Subscribe", "Sign up"), and any typing into card, CVV, expiry, gift-card, voucher or discount fields. It catches LLM clicks, the workflows' own JavaScript clicks and mis-clicks alike.
2. **Vetted LLM actions.** Every LLM action is first *observed*; the proposed element and its real label are checked against the same rules before anything is clicked.
3. **Guarded JavaScript.** Workflow scripts can click and type only through `safeClick`, `safeSelect` and `safeFill`, which apply the same rules.
4. **Hard stop.** On a checkout or payment URL only dismiss controls (Skip, No thanks, Close) can be clicked, and once a workflow has identified the final page nothing on it can be: only reads and screenshots.
5. **Prompts.** Each workflow still tells the model never to buy anything.

A refused action ends that site's run with `status: error` and `failure_reason: unsafe_action_refused`, and the run moves on to the next site. A test fails if any LLM action or raw click is added outside `safety.py`.

What it does not cover: labels in languages other than English, unlabelled icon-only buttons inside closed shadow roots or canvas, a site that submits a purchase by itself (a timer or wallet callback), the browser's own payment sheets, and a person clicking in a headed window.

### Coffee stores

```bash
# Audit a single coffee site (adds 3 products by default)
python main.py coffee https://redbrick.coffee

# Audit several sites and choose how many products to add (1 to 5)
python main.py coffee https://redbrick.coffee https://marketlane.com.au https://onacoffee.com.au --products 3
```

Finds the store's coffee catalogue without hardcoded URLs or selectors, dismisses popups, picks in-stock drinkable coffee (not equipment, merch or gift cards), forces one-time purchase over subscription and the smallest size, then compares the shop price, the selected variant price and the checkout line, subtotal and fees. On Shopify stores the cart is read from the store's own `/cart.js` (exact prices, no model call), which also confirms that each item really reached the cart. Login walls and CAPTCHAs are recorded as `blocked`, never bypassed.

### Virgin Australia

```bash
# Sydney to Melbourne, 30 days from today
python main.py virgin

# Choose the route and date
python main.py virgin --origin BNE --destination PER --date 2027-02-14
```

Fills the homepage search form (one way, 1 adult, paying with money) one action at a time, picks the cheapest direct flight and fare, then steps through guest details, seats, bags, extras and review to the payment page, declining every optional extra. Each add-on, upsell, fee and surcharge offered on the way is logged with its price, whether it was pre-selected and whether it is mandatory. Guest details are placeholders (override with `PAX_*`).

### Uber Eats

```bash
# Sign in once; the login is kept in .chrome-profile
python main.py ubereats --login

# Search from the homepage
python main.py ubereats --address "1 Jeffcott Place, Canberra ACT 2615" --query "pizza" --items 2

# Start from a store page and name the restaurant's own site
python main.py ubereats https://www.ubereats.com/au/store/... --restaurant-url https://...
```

Sets the delivery address, records the delivery and service fees advertised on the feed card and store banner, clears any leftover cart, adds 1–5 menu items (cheapest required modifiers, no paid add-ons), and walks the cart to the checkout screen to read every fee. It then finds the restaurant's own website (by search, or `--restaurant-url`), walks Menu → Order online → Pickup → nearest store until prices are on screen, and reports the Uber Eats markup per item. `--skip-direct-site` skips that last part.

### Ticketek

```bash
# Ticketek sells only to signed-in accounts: sign in once (an account with no saved card)
python main.py ticketek --login

# Search for an event and hold 2 tickets for its first session on sale
python main.py ticketek --query "Hamilton"

# Start from an event page, pick a session and hold 4 tickets
python main.py ticketek https://premier.ticketek.com.au/shows/show.aspx?sh=... --session "Sat 14 Mar" --tickets 4
```

Opens the event (by search, or from the URL), records the advertised "from" price, what the prices are said to include, and every session, then picks a session (`--session` matches words in its date and venue). On the ticket page it records every area and price type, holds the cheapest standard adult ticket on "Best available" (not child, concession, member or package tickets), and walks cart → delivery → extras to the payment page, choosing free delivery and declining refund protection, parking and every other extra. A sign-in page pauses a headed run so you can sign in; a headless run without a saved login is recorded as `blocked`. An on-sale queue is waited out for up to 15 minutes. Drip pricing is flagged for booking/service/handling fees, a paid delivery method, pre-selected extras, card surcharges, a ticket price that rises in the cart, and an advertised "from" price lower than any ticket actually offered for the session.

## Code Layout

```
main.py                     # entry point: python main.py <workflow> ...
audit/
├── cli.py                  # one subcommand per registered workflow; logging
├── config.py               # Settings: every environment variable, read and validated once
├── safety.py               # the purchase guard
├── runner.py               # browser + Stagehand session, per-site loop, failure reasons, session.json
├── workflow.py             # Workflow base class: the contract between runner and workflows
├── core.py                 # browser-free helpers: price parsing, name matching, results, report files
├── page.py                 # browser helpers: JS evaluation, waits, screenshots, popups, login pause
└── workflows/
    ├── __init__.py         # WORKFLOWS registry
    ├── coffee.py
    ├── virgin.py
    └── ubereats/           # the largest workflow, split by concern
        ├── __init__.py     # UberEatsWorkflow: options and orchestration
        ├── models.py       # extraction schemas and the run result
        ├── store.py        # Uber Eats: address, store, menu items, cart, checkout
        ├── direct_site.py  # the restaurant's own website
        └── pricing.py      # fee and markup comparison
    └── ticketek/           # same split: models.py, flow.py (event → tickets → payment), pricing.py
```

A workflow holds only what is particular to its site: CLI options, prompts, schemas, navigation and its price comparison. The runner and shared modules know nothing about any site.

### Adding a workflow

1. Create `audit/workflows/<name>.py` with a `Workflow` subclass. Set `name` and `help`, and implement `add_arguments`, `targets`, `run` and `summarise`. Class attributes choose the session behaviour (`system_prompt`, `persistent_profile`, `viewport`, `clear_cookies`).
2. In `run`, fill in the `SiteResult` (`status`, `items`, `extras`, `steps`, `comparison`). Use `safe_act()` for every LLM action and `safeClick`/`safeSelect`/`safeFill` in page scripts. Raise `Blocked` for login walls and bot checks, or another `AuditError` (`ElementNotFound`, `FlowChanged`, ...) for a specific failure. Build the verdict with `drip_outcome()`, so that a run that never read the final price reports `drip_detected: null`. Override `target_key()` if one start URL can lead to different things.
3. Add the class to `WORKFLOWS` in `audit/workflows/__init__.py`.

The runner then provides the browser, output folder, error handling, report files and the `runs.csv` row.

## Development

```bash
pip install -e ".[dev]"
ruff check . && ruff format --check .
mypy
pytest                      # the browser tests are skipped if no Chrome/Chromium is found
```

The tests never touch a real site or call a real model: browser tests run against local HTML fixtures with a stub model. `python scripts/measure_safe_act.py` compares the token cost of a vetted action with a plain `act()` on a fixture (it needs `GOOGLE_GENERATIVE_AI_API_KEY`; `--stub` runs without one).

## Outputs

```
output/                            # git-ignored: runs can hold addresses and account details
├── runs.csv                       # every run, newest last
├── summary.csv                    # the latest run of each target, rebuilt from runs.csv
└── <domain>/<YYYYMMDD-HHMMSS>/
    ├── products.csv               # what was on offer: shop products, flights, or menu items
    ├── cart.png                   # cart drawer/page, or first view of the selected trip
    ├── checkout.png               # checkout / payment page
    ├── price_comparison.json      # the full audit: items, comparison, extras, per-step log
    ├── price_comparison.csv       # shop vs selected vs checkout price per item
    ├── extras.csv                 # every extra, add-on, fee and surcharge offered (when any were seen)
    ├── session.json               # this site's token usage, timing, resource usage and Stagehand action log
    ├── extractions.jsonl          # every extraction: instruction, timestamp and what the model read
    └── error.png                  # only if the run failed
```

`price_comparison.json` and `price_comparison.csv` are written for every run, including blocked and failed ones, so whatever was captured is kept.

Workflow-specific additions:

- `virgin`: `flights.png` and `step-NN-<step>.png` for each booking step. The JSON also holds the search, `payment_surcharges`, `extras_preselected` and `total_vs_expected_diff`.
- `ubereats`: `store.png`, `step-NN-<step>.png`, `fees_breakdown.csv` (advertised vs charged, per fee), `restaurant_direct_N.png` and `restaurant_direct_menu.txt`. The JSON also holds `fee_breakdown` and the direct-site markup figures. Runs started from a `/store/...` URL go to `output/ubereats.com-<store>/`.

`summary.csv` columns: run timestamp, site, status (`ok`, `blocked`, `no_checkout`, `no_coffee_found`, `no_flights_found`, `no_items_found`, `add_to_cart_failed`, `error`), items added, expected subtotal, checkout subtotal, checkout total, currency, other fees, shipping/delivery, drip-detected flag and the run's output folder. `runs.csv` has the same columns plus `workflow`, `target` and `failure_reason`. The target is the site for `coffee`, the site and route for `virgin`, and the store (`/store/<slug>/<id>`) for `ubereats`, so audits of different restaurants or routes keep their own summary row.

`drip_detected` is `true` when any drip-pricing reason was found, `false` only when the final price screen was read and nothing was found, and empty/`null` when the run never read the final price (so it cannot say). `price_comparison.json` and `session.json` also carry `failure_reason`: `blocked`, `out_of_stock`, `flow_changed`, `element_not_found`, `extract_failed`, `timeout` or `unsafe_action_refused`.

See [MIGRATION.md](MIGRATION.md) for what changed in the output files.
