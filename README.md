# browser-automation

A [Stagehand](https://docs.stagehand.dev) (v4) Python tool designed to automate coffee e-commerce store interactions and audit checkout pricing for **drip pricing increases** (hidden fees, surcharges, or unexpected price hikes between catalog display and checkout).

The automation is specifically calibrated for coffee roaster and coffee retailer websites.

## Key Capabilities & Coffee Site Calibration

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

## Usage

Pass one or more coffee store URLs as positional arguments:

```bash
# Audit a single coffee site (adds default 3 products)
python scrape_shop.py https://redbrick.coffee

# Audit multiple coffee sites and specify how many products to add (1 to 5)
python scrape_shop.py https://redbrick.coffee https://marketlane.com.au https://onacoffee.com.au --products 3
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

### Summary File (`output/summary.csv`)
Maintains a persistent record across all evaluated sites:
- Run timestamp
- Store domain URL
- Run status (`ok`, `blocked`, `no_checkout`, `no_coffee_found`, `error`)
- Items added count
- Expected subtotal vs checkout subtotal vs final total
- Other non-tax, non-shipping fees
- Drip pricing detected flag (`True` / `False`)
- Path to session output directory
