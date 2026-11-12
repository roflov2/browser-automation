# browser-automation

[Stagehand](https://docs.stagehand.dev) (v4) Python workflows that drive real purchase flows and audit checkout pricing for **drip pricing increases** (hidden fees, surcharges, or unexpected price hikes between the advertised price and checkout).

| Workflow | Target | What it does |
| --- | --- | --- |
| `coffee-shopify.py` | Coffee roaster / retailer stores | Adds 1–5 coffees to the cart and compares shop prices with checkout |
| `airlines-virgin.py` | [Virgin Australia](https://www.virginaustralia.com/au/en/) | Books a one-way flight through to the payment page and records every extra and fee offered |

Both write the same output layout and share `output/summary.csv`.

## `coffee-shopify.py`: Key Capabilities & Coffee Site Calibration

- **Autonomous Shop Navigation**: Discovers and navigates to the store's dedicated coffee catalog without hardcoded URLs or element selectors.
- **Popup & Overlay Dismissal**: Observes and dismisses cookie consent banners, newsletter/discount popups, region/currency selectors, and age verification modals.
- **Coffee-Specific Filtering**: Differentiates drinkable coffee (whole bean, ground coffee, single-serve pods/capsules, drip/steep bags) from equipment, mugs, merch, apparel, gift cards, and courses.
- **One-Time Purchase Enforcement**: Prioritizes standard one-time purchases over recurring subscription models so subscription discounts do not distort pricing comparisons.
- **Lowest Package Sizing / Weight Selection**: Detects weight and package options (e.g., 200g, 225g, 250g, 500g, 1kg) and selects the smallest/cheapest size option prior to adding to cart.
- **Multi-Item Cart & Checkout**: Adds 1–5 in-stock coffee items to the cart, opens the cart drawer/page, and proceeds to the checkout flow.
- **Obstacle Detection**: Detects if checkout is blocked (e.g., mandatory user login, CAPTCHA, or bot-verification challenges) and records the status cleanly without attempting unauthorized bypasses.
- **Drip Pricing Analysis**: Performs per-item and whole-order price comparisons across:
  1. Initial shop catalog price (`From $X`)
  2. Selected package variant price
  3. Cart line-item price
  4. Checkout line item, subtotal, shipping, and extra fees (service fees, payment surcharges, handling fees).
- **Session Observability**: Records local system resources (CPU, RAM), execution duration, token usage metrics, and full action logs.

## `airlines-virgin.py`: Flight Booking Audit

- **Flight search from the homepage**: Fills the Virgin Australia search form (one way, 1 adult, paying with money rather than Points) one action at a time, falling back to the booking engine's search link if the form can't be driven.
- **Cheapest flight and fare**: Extracts the results page, picks the cheapest direct flight and its cheapest fare brand, and declines fare upsells.
- **Whole booking chain**: Steps through login prompt (continues as guest), guest details, seats, bags, extras and review until the payment page, declining every optional extra.
- **Extras and fees log**: At each step records every add-on, upsell, fee and surcharge shown: checked and carry-on baggage, seat selection, fare upgrades, travel insurance, carbon offset, lounge, car hire, service fees and card payment surcharges, with price, price basis, whether it was pre-selected and whether it is mandatory.
- **Drip pricing analysis**: Compares the advertised fare with the selected fare, the fare at checkout and the total due with no extras chosen, and flags added fees, pre-selected extras and payment surcharges.
- **Stops before paying**: The run ends on the payment page. No payment details are entered and no booking is made. Guest details are placeholders (override with the `PAX_*` variables below).

## Setup

Requires Python 3.10+ and Google Chrome/Chromium. Uses Google Gemini via [Google AI Studio](https://aistudio.google.com/apikey).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Open `.env` and configure your API key:

```env
GOOGLE_GENERATIVE_AI_API_KEY=your-gemini-key
```

### Environment Variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `GOOGLE_GENERATIVE_AI_API_KEY` | (required) | Gemini API key |
| `GEMINI_MODEL` | `gemini-3.8-flash` | Gemini model name (with or without `google/` prefix) |
| `HEADLESS` | `false` | Run without a visible browser window when set to `true` |
| `CHROME_PATH` | auto-detected | Path to Chrome/Chromium executable |
| `BROWSERBASE_API_KEY` | (optional) | Browserbase API key for cloud execution |
| `BROWSERBASE_PROJECT_ID` | (optional) | Browserbase project ID |

| `PAX_TITLE`, `PAX_FIRST_NAME`, `PAX_LAST_NAME`, `PAX_GENDER`, `PAX_DATE_OF_BIRTH`, `PAX_EMAIL`, `PAX_PHONE`, `PAX_COUNTRY` | placeholder guest | Guest details typed into the airline booking form (`airlines-virgin.py` only) |

## Usage

### Coffee stores

Pass one or more coffee store URLs as positional arguments:

```bash
# Audit a single coffee site (adds default 3 products)
python coffee-shopify.py https://redbrick.coffee

# Audit multiple coffee sites and specify how many products to add (1 to 5)
python coffee-shopify.py https://redbrick.coffee https://marketlane.com.au https://onacoffee.com.au --products 3
```

### Virgin Australia

```bash
# Sydney to Melbourne, 30 days from today
python airlines-virgin.py

# Choose the route and date
python airlines-virgin.py --origin BNE --destination PER --date 2027-02-14
```

## Where Outputs Live

All output files and run artifacts are saved under the [`output/`](file:///home/kpokaluhin/Documents/AG/browser-automation/output/) directory:

```
output/
├── summary.csv                               # Cross-site summary table for all runs
└── <domain>/                                 # Per-site directory (e.g., redbrick.coffee)
    └── <YYYYMMDD-HHMMSS>/                    # Timestamped session directory
        ├── products.csv                      # Extracted shop page products and classifications
        ├── cart.png                          # Screenshot of cart drawer / page with added items
        ├── checkout.png                      # Screenshot of checkout page and order summary
        ├── price_comparison.json             # Structured drip pricing audit results & fee breakdown
        ├── price_comparison.csv              # Tabular comparison of shop vs selected vs checkout prices
        └── session.json                      # Full Stagehand token metrics, system resource usage, and action logs
```

`airlines-virgin.py` writes the same files under `output/virginaustralia.com/<YYYYMMDD-HHMMSS>/`, where `products.csv` lists the flights on the results page, `cart.png` is the first view of the selected trip and `checkout.png` is the payment page. It also adds:

```
        ├── flights.png                       # Screenshot of the flight results page
        ├── step-NN-<step>.png                # Screenshot of each booking step (guest details, seats, extras, payment, ...)
        └── extras.csv                        # Every extra, add-on, fee and surcharge offered, by step
```

`price_comparison.json` additionally holds the search, the `extras` list, the per-step log (`steps`), `payment_surcharges`, `extras_preselected` and `total_vs_expected_diff`.

### Summary File (`output/summary.csv`)
Maintains a persistent record across all evaluated sites:
- Run timestamp
- Site URL
- Run status (`ok`, `blocked`, `no_checkout`, `no_coffee_found`, `no_flights_found`, `add_to_cart_failed`, `error`)
- Items added count (coffees, or flights)
- Expected subtotal vs checkout subtotal vs final total
- Other non-tax, non-shipping fees
- Drip pricing detected flag (`True` / `False`)
- Path to session output directory
