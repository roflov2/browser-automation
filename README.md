# browser-automation

A [Stagehand](https://docs.stagehand.dev) (v4) script that opens
[redbrick.coffee](https://redbrick.coffee/), lets Stagehand find the shop page on its own,
and records every product with its price.

## How it works

1. Opens the homepage in a local Chrome.
2. **Discovers the shop page.** The script never gives Stagehand a shop URL or link name.
   It repeatedly asks `act()` to "take the next step to get to this store's shop page", which might
   mean opening a collapsed menu and then clicking a link. After each step it asks `extract()` whether
   the current page is the shop page. It stops after 4 steps if it hasn't arrived.
3. **Extracts products** with `extract()` and a Zod schema (name, price, sale price, sold out, link).
   If the page has a "Next" or "Load more" control, it follows it, up to 10 pages.
4. Prints a table and saves `output/redbrick-products-<timestamp>.json` and `.csv`.

## Setup

Requires Node 20+, pnpm and Google Chrome.

```bash
pnpm install
```

## Run

```bash
export OPENAI_API_KEY="sk-..."
pnpm scrape
```

A Chrome window opens so you can watch each step.

| Env var | Default | Purpose |
| --- | --- | --- |
| `OPENAI_API_KEY` | (required) | API key for the default model |
| `STAGEHAND_MODEL` | `openai/gpt-5.6-sol` | Any `openai/`, `anthropic/`, `google/`, `groq/` or `cerebras/` model. Set the matching key, e.g. `ANTHROPIC_API_KEY` |
| `HEADLESS` | `false` | Set to `true` to run without a window |
| `CHROME_PATH` | auto-detected | Path to Chrome/Chromium if it isn't found automatically |

## Output

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

## Notes

- `zod` is pinned to `~4.6.6` to match the version Stagehand 4.4.0 uses internally. The quickstart's
  `~4.4.3` causes type errors when passing schemas to `extract()` with this Stagehand release.
- `pnpm typecheck` runs the TypeScript compiler.
