# browser-automation

[Stagehand](https://docs.stagehand.dev) (v4) Python workflows that drive real purchase flows and audit checkout pricing for **drip pricing** (hidden fees, surcharges, or unexpected price hikes between the advertised price and checkout).

Everything runs through one entry point, `main.py` (or the `drip-audit` command once the package is installed), with one subcommand per workflow:

| Workflow | Target | What it does |
| --- | --- | --- |
| `coffee` | Coffee roaster / retailer stores | Adds 1–5 coffees to the cart and compares shop prices with checkout |
| `virgin` | [Virgin Australia](https://www.virginaustralia.com/au/en/) | Books a one-way flight through to the payment page and records every extra and fee offered |
| `ubereats` | [Uber Eats AU](https://www.ubereats.com/au) | Orders 1–5 menu items through to checkout and records every fee |
| `ticketek` | [Ticketek AU](https://premier.ticketek.com.au/) | Holds 1–8 tickets for an event through to the payment page and records every booking, service, handling, delivery and card fee |

Workflows stop on the checkout / payment page and never pay. That relies on the workflows' flow and the instructions given to the model; there is no code-level purchase guard (see [Safety](#safety)).

**Use an account with no saved payment method** for any workflow that signs in (`ubereats`, `ticketek`). Nothing in the code blocks a purchase, so an account that cannot pay is the only hard safeguard.

## Setup

Requires Python 3.11+ (the Stagehand 4 SDK needs it) and Google Chrome/Chromium. Uses Google Gemini via [Google AI Studio](https://aistudio.google.com/apikey), or an Azure OpenAI deployment (see [Azure OpenAI](#azure-openai)).

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
| `AZURE_OPENAI_ENDPOINT` | (optional) | Azure OpenAI resource URL; when set, Azure replaces Gemini and no Gemini key is needed |
| `AZURE_OPENAI_DEPLOYMENT` | (required with the endpoint) | Deployment name, e.g. `gpt-4o` |
| `AZURE_OPENAI_API_VERSION` | `2024-12-01-preview` | Azure OpenAI API version |
| `AZURE_OPENAI_MANAGED_IDENTITY` | `false` | Let `DefaultAzureCredential` also try the host's managed identity |
| `HEADLESS` | `false` | Run without a visible browser window when set to `true` |
| `CHROME_PATH` | auto-detected | Path to the Chrome/Chromium executable used for local runs |
| `BROWSERBASE_API_KEY`, `BROWSERBASE_PROJECT_ID` | (optional) | Run on Browserbase instead of local Chrome |
| `PAX_TITLE`, `PAX_FIRST_NAME`, `PAX_LAST_NAME`, `PAX_GENDER`, `PAX_DATE_OF_BIRTH`, `PAX_EMAIL`, `PAX_PHONE`, `PAX_COUNTRY` | placeholder guest | Guest details typed into the airline booking form (`virgin` only). Names, date of birth, email and phone are passed to the page as Stagehand variables: they are not sent to the model and are replaced by `%name%` placeholders in `session.json` and `extractions.jsonl` |
| `UBEREATS_ADDRESS` | `260 City Walk, Canberra ACT 2601` | Default delivery address (`ubereats` only) |

### Azure OpenAI

Stagehand 4.4 only accepts OpenAI, Anthropic, Google, Groq and Cerebras model names and has no setting for an Azure endpoint, so `audit/azure.py` plugs an Azure deployment in through Stagehand's LLM callback: each model request becomes a chat completion on the deployment, signed in with a Microsoft Entra ID token from `DefaultAzureCredential` (cached and refreshed before it expires). No API key is used.

```bash
pip install -e ".[azure]"
az login                    # or set AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_CLIENT_SECRET for a service principal
```

Then set `AZURE_OPENAI_ENDPOINT` and `AZURE_OPENAI_DEPLOYMENT` in `.env`. The signed-in identity needs the *Cognitive Services OpenAI User* role on the resource. The token is fetched before the first site is opened, so a sign-in problem stops the run with a message. The deployment must accept JSON-schema response formats (gpt-4o 2024-08-06 or later, and API version 2024-08-01-preview or later). Stagehand's agent mode, which uses tool calls, is not supported by the adapter; the workflows here do not use it.

Throttling is printed rather than hidden: the adapter retries a 429, a server error or a dropped connection itself (up to three times, waiting as long as the endpoint's `retry-after` asks, at most 60 s) and prints each with the endpoint's message, the tokens and requests its rate-limit headers say are left this minute, and the request id. A call that leaves less than 10% of the per-minute limit is printed too, and so is a call Stagehand gives up waiting for. Run with `-v` to print every call's time, tokens and remaining quota.

## Usage

```bash
python main.py --help              # list workflows
python main.py <workflow> --help   # a workflow's options
```

Options shared by every workflow:

- `--local`: use local Chrome even if Browserbase credentials are set.
- `--login`: open the site in the local Chrome profile (`.chrome-profile/`) so you can sign in by hand, then exit. Later local runs reuse that login.

Every workflow's local runs share the one `.chrome-profile/`, so a sign-in done for one workflow holds for the others. Each run opens a new tab and closes any tabs the profile had open. A workflow with `clear_cookies = "domain"` clears only that site's own cookies (its domain and subdomains) before each run; other sites' sign-ins stay.

If your network requires a Microsoft (Azure) sign-in before the browser can reach the internet, a headed run opens the first site, sees the sign-in page and waits: sign in in the browser window, then press Enter in the terminal. A site that ends on that sign-in page partway through a run pauses the same way and is then run again once. The sign-in is kept in the profile for later runs. A headless run, or one without an interactive terminal, cannot pause, so those sites are recorded as `blocked`.
- `-v` / `--verbose`: also show debug detail (waits, errors that were handled). `-q` / `--quiet`: warnings and errors only.

## Safety

The audits drive real checkouts. There is no code-level purchase guard: nothing in the code blocks a click or a keystroke. What keeps a run from paying is:

1. **The flow.** Each workflow stops once it has read the checkout/payment page, and never asks the model to go further.
2. **Prompts.** Each workflow gives the model a system prompt that tells it never to pay, place or submit an order, enter card, gift-card or voucher details, or start a trial or membership, and individual `act` instructions repeat that where a purchase button is close by.

The model follows those instructions, but nothing enforces them: a misread page or a mis-click can still reach a "Place order" or "Pay now" button. **Use accounts with no saved payment method**, and do not keep a wallet (Shop Pay, PayPal, Google Pay) signed in to the browser profile.

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

Sets the delivery address, records the delivery and service fees advertised on the feed card and store banner, clears any leftover cart (or accepts Uber's 'new order' prompt when another store's cart remains), adds 1–5 menu items (cheapest required modifiers, no paid add-ons), and walks the cart to the checkout screen to read every fee.

To keep the token count down, the steps the page can answer itself never go to the model: the header's delivery address, the first open restaurant card on the feed (its link and printed fee), and the click that opens a menu item. Each falls back to the model when the page does not give the answer. The item modal and the cart drawer are read on their own rather than with the whole menu behind them; when another popup is open beside the drawer, the whole page is read so the popup is seen. Run with `-v` to see the input tokens of every observe and act call (extractions are in `extractions.jsonl`).

### Ticketek

```bash
# Ticketek sells only to signed-in accounts: sign in once (an account with no saved card)
python main.py ticketek --login

# Search for an event and hold 2 tickets for its first session on sale
python main.py ticketek --query "Hamilton"

# Start from an event page, pick a session and hold 4 tickets
python main.py ticketek https://premier.ticketek.com.au/shows/show.aspx?sh=... --session "Sat 14 Mar" --tickets 4
```

Opens the event (by search, or from the URL), records the advertised "from" price, what the prices are said to include, and every session, then picks a session (`--session` matches words in its date and venue). On the ticket page it records every area and price type, holds the cheapest standard adult ticket on "Best available" (not child, concession, member or package tickets). If Ticketek answers "There aren't enough tickets to meet your request right now" (or the session has no tickets left), the run first retries the same session with fewer tickets (half each time, down to 1: 8 → 4 → 2 → 1), as the message suggests. If even one ticket cannot be held, it goes back to the event and tries another date, time or venue for the full quantity again (those matching `--session` first, then any other listed on the page or in its date dropdown), up to 4 sessions and 8 tries in all. Every try given up on is recorded in `sessions_tried` (date, venue, tickets, reason) in `price_comparison.json`, with a screenshot of each message, next to `tickets_requested` and the `tickets` actually held. It then walks cart → delivery → extras to the payment page, choosing free delivery and declining refund protection, parking and every other extra. A sign-in page pauses a headed run so you can sign in; a headless run without a saved login is recorded as `blocked`. An on-sale queue is waited out for up to 15 minutes. Drip pricing is flagged for booking/service/handling fees, a paid delivery method, pre-selected extras, card surcharges, a ticket price that rises in the cart, and an advertised "from" price lower than any ticket actually offered for the session.

## Code Layout

```
main.py                     # entry point: python main.py <workflow> ...
audit/
├── cli.py                  # one subcommand per registered workflow; logging
├── config.py               # Settings: every environment variable, read and validated once
├── azure.py                # Azure OpenAI (Entra ID sign-in) as Stagehand's LLM callback
├── actions.py              # LLM actions and in-page click/type helpers
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
        └── pricing.py      # fee comparison
    └── ticketek/           # same split: models.py, flow.py (event → tickets → payment), pricing.py
```

A workflow holds only what is particular to its site: CLI options, prompts, schemas, navigation and its price comparison. The runner and shared modules know nothing about any site.

### Adding a workflow

1. Create `audit/workflows/<name>.py` with a `Workflow` subclass. Set `name` and `help`, and implement `add_arguments`, `targets`, `run` and `summarise`. Class attributes choose the session behaviour (`system_prompt`, `viewport`, `clear_cookies`).
2. In `run`, fill in the `SiteResult` (`status`, `items`, `extras`, `steps`, `comparison`). Use `llm_act()` for LLM actions and `clickEl`/`selectOption`/`fillInput` in page scripts, and give the workflow a `system_prompt` that forbids paying. Raise `Blocked` for login walls and bot checks, or another `AuditError` (`ElementNotFound`, `FlowChanged`, ...) for a specific failure. Build the verdict with `drip_outcome()`, so that a run that never read the final price reports `drip_detected: null`. Override `target_key()` if one start URL can lead to different things.
3. Add the class to `WORKFLOWS` in `audit/workflows/__init__.py`.

The runner then provides the browser, output folder, error handling, report files and the `runs.csv` row.

## Development

```bash
pip install -e ".[dev]"
ruff check . && ruff format --check .
mypy
pytest                      # the browser tests are skipped if no Chrome/Chromium is found
```

The tests never touch a real site or call a real model: browser tests run against local HTML fixtures with a stub model.

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
- `ubereats`: `store.png`, `step-NN-<step>.png`, and `fees_breakdown.csv` (advertised vs charged, per fee). When a leftover cart could not be emptied, `cart_clear.json` (or `cart_clear-after.json`, for the clear at the end of a run that stopped before checkout) holds the steps tried, the count after each, and the cart drawer's HTML. A run that fails also leaves `error-before-cleanup.png`, the screen it failed on (its `error.png` is taken after the cart has been emptied on the way out, so it shows the store page), and a cart/checkout screen that could not be read leaves `step-NN-extract-failed.png` and a step with its URL and error. The JSON also holds `fee_breakdown`. Runs started from a `/store/...` URL go to `output/ubereats.com-<store>/`.

`summary.csv` columns: run timestamp, site, status (`ok`, `blocked`, `no_checkout`, `no_coffee_found`, `no_flights_found`, `no_items_found`, `add_to_cart_failed`, `error`), items added, expected subtotal, checkout subtotal, checkout total, currency, other fees, shipping/delivery, drip-detected flag, the run's output folder, and its time and LLM usage (see [Time and tokens per run](#time-and-tokens-per-run)). `runs.csv` has the same columns plus `workflow`, `target` and `failure_reason`. The target is the site for `coffee`, the site and route for `virgin`, and the store (`/store/<slug>/<id>`) for `ubereats`, so audits of different restaurants or routes keep their own summary row.

`drip_detected` is `true` when any drip-pricing reason was found, `false` only when the final price screen was read and nothing was found, and empty/`null` when the run never read the final price (so it cannot say). `price_comparison.json` and `session.json` also carry `failure_reason`: `blocked`, `out_of_stock`, `flow_changed`, `element_not_found`, `extract_failed`, `timeout` or `cart_lost` (`ubereats`: the cart came up empty on the way to checkout).

### Time and tokens per run

Every run records how long it took and how much of the model it used, in `runs.csv`, `summary.csv` and `session.json` (`usage`), and prints it after the run's status line:

```
  Time 3m 19s (LLM 1m 54s, 14 calls) | Tokens 104,184 (95,045 in / 9,139 out)
```

| Column | Meaning |
| --- | --- |
| `duration_seconds` | Wall-clock time of the run, from opening the site to writing its reports |
| `llm_time_seconds` | Time spent waiting on the model (Stagehand's inference time) |
| `llm_calls` | Model calls made: every `extract`, `observe` and `act`, retries included |
| `prompt_tokens`, `completion_tokens`, `total_tokens` | Tokens sent to and returned by the model during this run |

A token or LLM-time column is empty, not zero, when Stagehand's metrics could not be read. A command auditing several targets also prints the total of all its runs at the end. To see the history:

```bash
python main.py usage            # per-workflow runs, average time, LLM time, calls and tokens, then the last 10 runs
python main.py usage --last 25
```

`usage` reads `output/runs.csv` and opens no browser. Runs recorded before these columns existed are filled in from their `session.json` when it is still there.

See [MIGRATION.md](MIGRATION.md) for what changed in the output files.
