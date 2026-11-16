# browser-automation

[Stagehand](https://docs.stagehand.dev) (v4) Python workflows that drive real purchase flows and audit checkout pricing for **drip pricing** (hidden fees, surcharges, or unexpected price hikes between the advertised price and checkout).

Everything runs through one entry point, `main.py`, with one subcommand per workflow:

| Workflow | Target | What it does |
| --- | --- | --- |
| `coffee` | Coffee roaster / retailer stores | Adds 1–5 coffees to the cart and compares shop prices with checkout |
| `virgin` | [Virgin Australia](https://www.virginaustralia.com/au/en/) | Books a one-way flight through to the payment page and records every extra and fee offered |
| `ubereats` | [Uber Eats AU](https://www.ubereats.com/au) | Orders 1–5 menu items through to checkout, records every fee, and compares menu prices with the restaurant's own website |

No workflow ever pays: runs stop on the checkout / payment page.

## Setup

Requires Python 3.10+ and Google Chrome/Chromium. Uses Google Gemini via [Google AI Studio](https://aistudio.google.com/apikey).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Open `.env` and set `GOOGLE_GENERATIVE_AI_API_KEY`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `GOOGLE_GENERATIVE_AI_API_KEY` | (required) | Gemini API key |
| `GEMINI_MODEL` | `gemini-3.8-flash` | Gemini model name (with or without `google/` prefix) |
| `HEADLESS` | `false` | Run without a visible browser window when set to `true` |
| `CHROME_PATH` | auto-detected | Path to Chrome/Chromium executable |
| `BROWSERBASE_API_KEY`, `BROWSERBASE_PROJECT_ID` | (optional) | Run on Browserbase instead of local Chrome |
| `PAX_TITLE`, `PAX_FIRST_NAME`, `PAX_LAST_NAME`, `PAX_GENDER`, `PAX_DATE_OF_BIRTH`, `PAX_EMAIL`, `PAX_PHONE`, `PAX_COUNTRY` | placeholder guest | Guest details typed into the airline booking form (`virgin` only) |
| `UBEREATS_ADDRESS` | `260 City Walk, Canberra ACT 2601` | Default delivery address (`ubereats` only) |

## Usage

```bash
python main.py --help              # list workflows
python main.py <workflow> --help   # a workflow's options
```

Options shared by every workflow:

- `--local`: use local Chrome even if Browserbase credentials are set.
- `--login`: open the site in the persistent local Chrome profile (`.chrome-profile/`) so you can sign in by hand, then exit. Later local runs reuse that login. Not available for `virgin`, which always starts from a fresh browser.

### Coffee stores

```bash
# Audit a single coffee site (adds 3 products by default)
python main.py coffee https://redbrick.coffee

# Audit several sites and choose how many products to add (1 to 5)
python main.py coffee https://redbrick.coffee https://marketlane.com.au https://onacoffee.com.au --products 3
```

Finds the store's coffee catalogue without hardcoded URLs or selectors, dismisses popups, picks in-stock drinkable coffee (not equipment, merch or gift cards), forces one-time purchase over subscription and the smallest size, then compares the shop price, the selected variant price and the checkout line, subtotal and fees. Login walls and CAPTCHAs are recorded as `blocked`, never bypassed.

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

## Code Layout

```
main.py                     # entry point: python main.py <workflow> ...
audit/
├── cli.py                  # one subcommand per registered workflow
├── runner.py               # browser + Stagehand session, per-site loop, error handling, session.json
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
```

A workflow holds only what is particular to its site: CLI options, prompts, schemas, navigation and its price comparison. The runner and shared modules know nothing about any site.

### Adding a workflow

1. Create `audit/workflows/<name>.py` with a `Workflow` subclass. Set `name` and `help`, and implement `add_arguments`, `targets`, `run` and `summarise`. Class attributes choose the session behaviour (`system_prompt`, `persistent_profile`, `viewport`, `clear_cookies`).
2. In `run`, fill in the `SiteResult` (`status`, `items`, `extras`, `steps`, `comparison`). Raise `Blocked` for login walls and bot checks.
3. Add the class to `WORKFLOWS` in `audit/workflows/__init__.py`.

The runner then provides the browser, output folder, error handling, report files and `summary.csv` row.

## Outputs

```
output/
├── summary.csv                    # one row per site: its latest run, from any workflow
└── <domain>/<YYYYMMDD-HHMMSS>/
    ├── products.csv               # what was on offer: shop products, flights, or menu items
    ├── cart.png                   # cart drawer/page, or first view of the selected trip
    ├── checkout.png               # checkout / payment page
    ├── price_comparison.json      # the full audit: items, comparison, extras, per-step log
    ├── price_comparison.csv       # shop vs selected vs checkout price per item
    ├── extras.csv                 # every extra, add-on, fee and surcharge offered (when any were seen)
    ├── session.json               # token usage, timing, resource usage and Stagehand action log
    └── error.png                  # only if the run failed
```

`price_comparison.json` and `price_comparison.csv` are written for every run, including blocked and failed ones, so whatever was captured is kept.

Workflow-specific additions:

- `virgin`: `flights.png` and `step-NN-<step>.png` for each booking step. The JSON also holds the search, `payment_surcharges`, `extras_preselected` and `total_vs_expected_diff`.
- `ubereats`: `store.png`, `step-NN-<step>.png`, `fees_breakdown.csv` (advertised vs charged, per fee), `restaurant_direct_N.png` and `restaurant_direct_menu.txt`. The JSON also holds `fee_breakdown` and the direct-site markup figures. Runs started from a `/store/...` URL go to `output/ubereats.com-<store>/`.

`summary.csv` columns: run timestamp, site, status (`ok`, `blocked`, `no_checkout`, `no_coffee_found`, `no_flights_found`, `no_items_found`, `add_to_cart_failed`, `error`), items added, expected subtotal, checkout subtotal, checkout total, currency, other fees, shipping/delivery, drip-detected flag and the run's output folder.
