# browser-automation

A [Stagehand](https://docs.stagehand.dev) (v4) script that opens
[redbrick.coffee](https://redbrick.coffee/), lets Stagehand find the shop page on its own,
and records every product with its price. Both versions use Google Gemini and read the API key from `.env`.

There are two versions:

- **Python:** `scrape_shop.py`, a short version that writes `output/redbrick_products.csv`.
- **TypeScript:** `scrape-shop.ts`, which adds pagination, JSON output and derived price fields.

## Python

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env     # then set GOOGLE_GENERATIVE_AI_API_KEY
python scrape_shop.py
```

`GEMINI_MODEL`, `HEADLESS` and `CHROME_PATH` work the same way as described for the TypeScript version below.

## TypeScript

### How it works

1. Opens the homepage in a local Chrome.
2. **Discovers the shop page.** The script never gives Stagehand a shop URL or link name.
   It repeatedly asks `act()` to "take the next step to get to this store's shop page", which might
   mean opening a collapsed menu and then clicking a link. After each step it asks `extract()` whether
   the current page is the shop page. It stops after 4 steps if it hasn't arrived.
3. **Extracts products** with `extract()` and a Zod schema (name, price, sale price, sold out, link).
   If the page has a "Next" or "Load more" control, it follows it, up to 10 pages.
4. Prints a table and saves `output/redbrick-products-<timestamp>.json` and `.csv`.

### Setup

Requires Node 20.12+, pnpm and Google Chrome. The script uses Google Gemini, so you also need a
Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey).

```bash
pnpm install
cp .env.example .env
```

Then open `.env` and set your key:

```
GOOGLE_GENERATIVE_AI_API_KEY=your-gemini-key
```

### Run

```bash
pnpm scrape
```

A Chrome window opens so you can watch each step.

The script loads `.env` on startup. Variables already set in your shell take precedence.

| Variable | Default | Purpose |
| --- | --- | --- |
| `GOOGLE_GENERATIVE_AI_API_KEY` | (required) | Gemini API key. `GEMINI_API_KEY` is accepted too |
| `GEMINI_MODEL` | `gemini-3.8-flash` | Gemini model to use, with or without the `google/` prefix |
| `HEADLESS` | `false` | Set to `true` to run without a window |
| `CHROME_PATH` | auto-detected | Path to Chrome/Chromium if it isn't found automatically |

### Output

Each product row has:

- `name`
- `price`: as displayed, e.g. `From $15.00`
- `amount`: the number, e.g. `15`
- `priceFrom`: true when the price is a "From" price because the product comes in several sizes or variants
- `originalPrice` and `onSale`
- `soldOut`
- `url`

Prices are in whatever currency the store shows your location. The detected currency is saved in the
JSON file.

### Notes

- `zod` is pinned to `~4.6.6` to match the version Stagehand 4.4.0 uses internally. The quickstart's
  `~4.4.3` causes type errors when passing schemas to `extract()` with this Stagehand release.
- `pnpm typecheck` runs the TypeScript compiler.
