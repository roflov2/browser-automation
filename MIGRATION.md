# Migration notes

What changed for anyone who reads the output files or uses the command line, from `main` (`85e08d1`,
three standalone scripts) through the consolidation (`0de8176`) to this branch. Added fields and
files are listed too; nothing was removed from the CLI.

## Command line

| Change | Before | Now |
| --- | --- | --- |
| Entry point | `python coffee-shopify.py`, `python airlines-virgin.py`, `python uber-eats.py` (`main`) | `python main.py <workflow>` or `drip-audit <workflow>`; same options per workflow |
| `--local`, `--login` | `ubereats` only (`main`) | every workflow; `--login` is refused for `virgin` (fresh browser every time) |
| `-v/--verbose`, `-q/--quiet` | none | new |
| Invalid configuration | a `KeyError` traceback or a silent default | a one-line message naming the variable (`GOOGLE_GENERATIVE_AI_API_KEY`, `CHROME_PATH`, `HEADLESS`, ...) |
| `CHROME_PATH` | documented, never read | used for local runs; must point to a file |
| `NO_PROXY` | overwritten with `127.0.0.1,localhost` on import | loopback appended to whatever was set, at start-up |
| Python | README said 3.10+ | 3.11+ (required by `stagehand` 4.4.0) |

## Changed values in existing fields

| File / field | Before | Now | Why |
| --- | --- | --- | --- |
| `price_comparison.json` `drip_detected`, `summary.csv` `drip_detected` | `false` whenever no reason was found, including runs that never reached the final price screen | `true` if any reason was found; `false` only if the final price screen was read; `null` (empty in CSV) otherwise | a run that saw nothing cannot say "no drip" |
| item `price_increased` | `false` when the item had no checkout line | `null` | same; nothing was compared |
| `parse_price` results (every `*_price`, fee `amount`, `extras.csv` `price`) | `-$11.85` read as 11.85; `($5.00)` as 5; `Free on $50+` as 0; `Gluten free base $3` as 0; `2 for $30` as 2; `$.50` as 50; `1,5` as 15; `$5 10 min` as 510 | -11.85; -5; null; 3; 30; 0.5; 1.5; 5 | REVIEW H2. None of the seven saved runs contained such a value |
| `parse_price("Free")` (coffee, virgin) | `null` on `main` | `0.0` (since the consolidation) | consolidation adopted the Uber Eats parser |
| selected price shown as `$0.00` (coffee, ubereats) | replaced by the shop price | `0.0` | a real zero is not "missing" |
| ubereats `fee_breakdown` small-order row when the fee is not shown | `checkout_display: "$0.00"`, `amount: 0.0`, `pct_of_subtotal: 0.0` | `"not shown"`, `null`, `null` | not shown is unknown, not zero |
| ubereats `other_fees` | a `Service Fee` / `Small Order Fee` of $0.00 was dropped | listed with `amount: 0.0` | |
| ubereats `service_fee_pct_of_subtotal`, any `pct_of_subtotal` | `null` for a 0% fee | `0.0` | |
| ubereats `currency` | `"AUD"` even when the page showed no currency | the currency shown, or `null`; see `currency_assumed` | |
| ubereats item checkout prices | read only from the checkout page, which collapses the cart, so usually `null` | from the checkout page, else from the cart drawer read just before it (`item_prices_source`) | REVIEW M7 |
| `best_match` (direct-site and checkout-line matching) | any substring counted as containment (`Fries` matched `OOSH Fries Large` before `Lebo Fries`) | whole words only; the closest name wins | REVIEW M8 |
| coffee item `status` on Shopify stores | `added` whenever the model reported success | `add_failed` if `/cart.js` shows the cart did not grow | REVIEW M9 |
| coffee `cart_subtotal_display` on Shopify stores | the model's reading of the cart, e.g. `$67.00 AUD` | from `/cart.js`, e.g. `$67.00` | no model call |
| virgin `steps` | one entry per extraction, including repeats on a step where nothing changed | a repeat on an unchanged screen is counted for the "stuck" check but not extracted or listed again | fewer extractions |
| `session.json` `resource_usage` | covered every site of the invocation so far | covers this site only; `null` if `psutil` is missing (was zeros) | REVIEW M1 |
| `session.json` `tokens_used` | 0 when the metrics call failed | `null` | |
| `session.json` `history` | full instruction text, including passenger details | typed passenger details replaced by `%first_name%`, `%email%`, ... | REVIEW H7 |
| virgin `session.json` `resource_usage.avg_cpu_load` | 1-minute load average (`main`) | renamed `avg_cpu_percent`, the process's CPU % (since the consolidation) | consolidation change 3 |
| virgin `peak_memory_mb` | peak RSS via `getrusage` (`main`) | maximum of 1 s RSS samples (since the consolidation) | consolidation change 2 |
| `extras.csv` (virgin, ubereats) | always written, header only when empty (`main`) | written only when extras were seen (since the consolidation) | consolidation change 8 |
| `summary.csv` rows | one per `site`, rewritten in place | the latest run per (`workflow`, `target`), rebuilt from `runs.csv`, written atomically; same columns | REVIEW H4 |
| run folder timestamp | local time | local time (now timezone-aware; same format) | |

## New fields and files

- `price_comparison.json`: `failure_reason`, `final_price_read`; coffee `shipping_calculated`; ubereats
  `item_prices_source`, `fees_and_delivery_total`, `total_vs_components_diff` (a negative gap is a
  promotion or discount), `currency_assumed`.
- `session.json`: `failure_reason`, `metrics` (this site's share of the cumulative counters).
- `output/runs.csv`: every run, with the summary columns plus `workflow`, `target`, `failure_reason`.
  On first use it is seeded from an existing `summary.csv`.
- `<run>/extractions.jsonl`: every `extract()` call with its instruction, timestamp, schema and result.
- `failure_reason` values: `blocked`, `out_of_stock`, `flow_changed`, `element_not_found`,
  `extract_failed`, `timeout`, `unsafe_action_refused`.

## Prompts sent to the model

| Workflow | Change |
| --- | --- |
| coffee | new system prompt forbidding payment, wallet buttons, card/gift/discount details, subscriptions and accounts (approved) |
| virgin | passenger form: the extraction asks which detail belongs in each field instead of being given the values; typed details reach the page as `%name%` variables (approved) |
| all | popup-dismissal wording and the shared `LineItem` field descriptions as reworded by the consolidation (kept, approved) |

## Not changed

Subcommands and their options; status values; file names; the columns of `price_comparison.csv`,
`extras.csv`, `fees_breakdown.csv`, `products.csv` and `summary.csv`; JSON keys other than the
additions above; retry limits, viewports, cookie and profile policy.
