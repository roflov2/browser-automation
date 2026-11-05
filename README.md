# browser-automation

A [Stagehand](https://docs.stagehand.dev) (v4) Python script that:

1. Opens [redbrick.coffee](https://redbrick.coffee/) and lets Stagehand discover the shop page on its own.
2. Extracts every product with its price to `output/redbrick_products.csv`.
3. Clicks on an in-stock product, selects the lowest package size/weight (cheapest option) if sizing options exist, and adds it to the cart.
4. Opens the cart, proceeds to checkout (if a checkout button is available), and saves a screenshot of the checkout to `output/checkout.png`.
5. Compares the shop page price against the selected product price and checkout price to detect drip pricing increases (`output/price_comparison.json` and `output/price_comparison.csv`).
6. Records the session (operation history timeline, token/inference metrics, system resource usage, and optional Browserbase session metadata) to `output/session.json`.

## Setup

Requires Python 3.10+ and Google Chrome/Chromium. The script uses Google Gemini, so you also need a
Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey).

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Then open `.env` and set your key:

```env
GOOGLE_GENERATIVE_AI_API_KEY=your-gemini-key
```

## Run

```bash
python scrape_shop.py
```

The script loads `.env` on startup:

| Variable | Default | Purpose |
| --- | --- | --- |
| `GOOGLE_GENERATIVE_AI_API_KEY` | (required) | Gemini API key |
| `GEMINI_MODEL` | `gemini-3.8-flash` | Gemini model to use, with or without the `google/` prefix |
| `HEADLESS` | `false` | Set to `true` to run without a window |
| `CHROME_PATH` | auto-detected | Path to Chrome/Chromium if it isn't found automatically |
| `BROWSERBASE_API_KEY` | (optional) | Browserbase API key for cloud session recording |
| `BROWSERBASE_PROJECT_ID` | (optional) | Browserbase project ID |

## Output

- `output/redbrick_products.csv` — Extracted products (`name`, `price`, `original_price`, `sold_out`, `url`).
- `output/checkout.png` — Screenshot of the checkout page showing the order summary and pricing.
- `output/price_comparison.json` & `output/price_comparison.csv` — Drip pricing comparison between the shop catalog price, selected option price, cart price, and checkout total.
- `output/session.json` — Recorded session timeline (`history`), `StagehandMetrics` breakdown, local performance summary, and resource usage (`LocalResourceMonitor` / Browserbase session info).
