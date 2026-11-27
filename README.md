# browser-automation: checkout navigator

A site-agnostic [Stagehand](https://docs.stagehand.dev) (v4, Python) navigator. From any starting URL (a shop, a store page, an event, a travel or accommodation search) it walks the purchase or booking funnel and **stops at the payment wall**. It does not read prices, never fills in payment details and never submits an order.

Every website goes through the same loop and the same generic helpers. There is no per-site code. The only per-site input is optional timing and never-click data in `checkout_navigator/site_hints.yaml`.

## Setup

Requires Python 3.11+ and Google Chrome or Chromium.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # or: pip install -e .
cp .env.example .env                     # then set GOOGLE_GENERATIVE_AI_API_KEY (or Azure, below)
```

## Usage

```bash
python main.py --url https://shop.example.com.au
python main.py --url https://shop.example.com.au --max-steps 20 --headless
python -m checkout_navigator.run --url ...          # same thing; `checkout-navigator --url ...` once installed
```

| Option | Default | Purpose |
| --- | --- | --- |
| `--url` | (required) | Where to start |
| `--max-steps` | 15 (`MAX_STEPS`) | Step budget |
| `--headless` | off (`HEADLESS`) | Run Chrome without a window |
| `--profile-dir` | `.chrome-profile/` (`CHROME_PROFILE_DIR`) | Persistent Chrome profile, so a sign-in is kept between runs |
| `--address` | `240 City Walk, Canberra ACT 2601` (`DEFAULT_ADDRESS`) | Delivery address to use |
| `-v` | off | Debug logging |

It prints one JSON status dictionary and exits with 0 for `DONE` and 1 for anything else:

```json
{"status": "DONE", "steps": 7, "url": "https://.../checkout", "history": ["step 0: DO_DISMISS_OVERLAY ... -> ok", "..."],
 "tokens": {"input": 84210, "cached_input": 0, "output": 3120, "reasoning": 0, "total": 87330}}
```

`tokens` adds up every model call in the run, as Stagehand counts them (Azure calls included). It is followed by a one-line total on stderr, e.g. `Tokens: 87,330 (84,210 in, 3,120 out)`.

| Status | Meaning |
| --- | --- |
| `DONE` | The payment page was detected, or the safety brake refused a final-purchase action (`"stopped_by": "safety_brake"`) |
| `BLOCKED` | A CAPTCHA or bot check, a virtual queue that never let it through, or a sign-in with no guest path when the run cannot pause for one |
| `MAX_STEPS_EXCEEDED` | The step budget ran out |
| `ERROR` | The browser or the model failed outright (the message is in `error`) |

## How it works

```
observe ─► Are we at checkout? ── yes ─► DONE
               │ no
               ▼
     same URL and same page controls as last step? ── yes ─► scroll, dismiss any overlay, loop
               │ no
               ▼
     determine_next_step (one model call) ─► route
               │
               ▼
     DO_DISMISS_OVERLAY │ DO_LOCATION │ DO_CALENDAR │ DO_OPTIONS │ DO_AUTH_OR_DETAILS │ DO_ACT
               │          (every click and keystroke goes through cdp_act's payment brake)
               └──────────────────────────────► loop
```

On each step:

1. **Observe.** Stagehand's `observe()` lists the elements that matter (buttons, empty fields, pickers, popups), and one in-page script reads every visible control.
2. **Are we at checkout?** This check needs no model call. It counts as checkout when there is a payment-provider frame (Stripe, PayPal, Adyen, Braintree, Afterpay, Klarna and others) alongside a payment button. It also counts when at least two signals are present: card number, expiry, CVV or billing-postcode fields, or "Place order", "Pay now", "Complete purchase", "Pay with card" or "Submit payment" buttons. Frames are read from the parent page, so cross-origin payment frames count.
3. **Stuck?** If the URL and the page's controls (text, values, checked states, open dialogs) are both unchanged since the last step, the last action did nothing. The navigator scrolls to reveal off-screen buttons and sticky footers, clears any overlay, and loops.
4. **What next?** One structured `extract()` call picks a route. For a plain action it also gives the exact atomic instruction and, where it can, the observed element to act on, so the act needs no second model call.
5. **Do it**, then loop.

| Route | When | What the helper does |
| --- | --- | --- |
| `DO_DISMISS_OVERLAY` | A cookie banner, promo popup, app banner or age gate is in the way | Clicks a known consent button, otherwise asks the model to close it, otherwise presses Escape |
| `DO_LOCATION` | A delivery-address prompt, or a from/to, city or suburb search bar | Types the default address or search value (key by key if the type-ahead ignores a fill), then picks the first matching suggestion |
| `DO_CALENDAR` | A date, departure/return or check-in/check-out picker | Fills native date inputs. Otherwise opens the picker, reads the month header, pages forward to today + 14 (and today + 17 for a return or check-out), clicks the days and confirms |
| `DO_OPTIONS` | A required size, side, variant, fare, ticket tier or quantity | Picks the free, default or cheapest option (quantity 1), declines optional extras and insurance, and clicks the confirm button |
| `DO_AUTH_OR_DETAILS` | A sign-in wall, a guest choice, or a contact, passenger or guest form | Takes "Continue as guest" if offered. Otherwise fills the placeholder shopper and submits. With no guest path, a headed interactive run pauses so you can sign in by hand |
| `DO_ACT` | Nothing special blocks the page | One safety-checked action, e.g. "Click the 'Continue to seat selection' button" |

## Safety

Everything that clicks or types goes through `cdp_act` in `checkout_navigator/engine/cdp_guard.py`:

1. Every act carries the constraint *"NEVER click Pay, Place Order, Complete Purchase, Confirm & Pay, or Submit Payment."*
2. Before anything is touched, three things are checked against final-purchase phrases: the instruction (ignoring negated clauses such as "do not click Place order"), the model's description of the element it chose, and that element's live text in the page.
3. On a match it does not act. It raises `CheckoutReachedInterrupt`, and the loop ends with `DONE`.
4. Typing into a card number, expiry, CVV or card-holder field is refused the same way.

A small never-click list (account creation, free trials, subscriptions, plus any per-site words in `site_hints.yaml`) is refused too. A label that declines something ("No thanks", "Continue without…") is still allowed.

**Still use accounts and a browser profile with no saved payment method.** The brake is keyword-based, and a wallet button such as Shop Pay, PayPal or Google Pay can pay in one click under an unexpected label.

## Configuration

Defaults live in `checkout_navigator/config.py`. Every one can be overridden in the environment or in `.env` (see `.env.example`).

| Setting | Default | Variables |
| --- | --- | --- |
| Step budget | 15 | `MAX_STEPS` |
| Delivery address | `240 City Walk, Canberra ACT 2601` | `DEFAULT_ADDRESS` |
| Search defaults | `CBR → SYD`, city `Sydney`, 1 guest | `SEARCH_ORIGIN`, `SEARCH_DESTINATION`, `SEARCH_CITY`, `SEARCH_GUESTS` |
| Placeholder shopper | Alex Testbooking, `alex.testbooking@example.com`, `0491 570 156` (ACMA fictional range) | `PERSONA_<FIELD>` |
| Model | `google/gemini-3.8-flash` | `NAVIGATOR_MODEL`, `GOOGLE_GENERATIVE_AI_API_KEY` or `MODEL_API_KEY` |
| Azure OpenAI (replaces the model) | off | `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`, `AZURE_OPENAI_API_VERSION`, `AZURE_OPENAI_MANAGED_IDENTITY` |
| Browser | headed, `.chrome-profile/`, no proxy | `HEADLESS`, `CHROME_PATH`, `CHROME_PROFILE_DIR`, `BROWSER_PROXY_SERVER`, `BROWSER_PROXY_BYPASS` |

Typed shopper details reach the page as Stagehand `%name%` variables, so they are never sent to the model.

### Site hints

`checkout_navigator/site_hints.yaml` holds data, not code. Its `default` block sets the waits (page hydration, post-action settle, virtual-queue limit), the common consent-tool "accept" selectors, and the default never-click words. To tune one site, add an entry keyed by its domain (it also covers that domain's subdomains) with any of `hydration_wait_s`, `settle_ms`, `queue_wait_s`, `dismiss_selectors`, `never_click` or `overlay_hint`. None ship by default.

### Azure OpenAI

Stagehand 4.4 has no setting for an Azure endpoint. Instead, `checkout_navigator/engine/azure_llm.py` plugs in as Stagehand's LLM callback: each model request becomes a chat completion on your deployment, signed in with a Microsoft Entra ID token from `DefaultAzureCredential`. No API key is used.

```bash
pip install -r requirements-azure.txt
az login        # or set AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_CLIENT_SECRET for a service principal
export AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com AZURE_OPENAI_DEPLOYMENT=gpt-4o
python main.py --url https://shop.example.com.au
```

How the adapter behaves:

- **Sign-in first.** The token is fetched before Chrome opens, so a sign-in problem stops the run straight away.
- **Schema repair.** Off-schema answers have forbidden keys dropped and missing nullable keys filled with null, and the model is asked once more if the answer still doesn't fit.
- **Retries.** Throttling, server errors and dropped connections are retried up to three times, honouring `retry-after` (at most 60 s), and each retry is logged.

Requirements:

- The signed-in identity needs the *Cognitive Services OpenAI User* role on the resource.
- The deployment must accept JSON-schema response formats: gpt-4o 2024-08-06 or later, with API version 2024-08-01-preview or later.

## Code layout

```
main.py                     # entry point: python main.py --url ...
checkout_navigator/
├── config.py               # defaults, env/.env loading, Azure settings, system prompt
├── site_hints.yaml         # optional per-domain timing / never-click data
├── run.py                  # CLI: parse --url, open the session, run the loop, print the status
├── engine/
│   ├── navigator.py        # the loop: Are we at checkout? -> what next? -> do
│   ├── classifier.py       # is_at_checkout (no model call) and determine_next_step (one model call)
│   ├── cdp_guard.py        # frame retries, waits, profile-lock cleanup, DOM hashing, cdp_act and the payment brake
│   └── azure_llm.py        # Azure OpenAI as Stagehand's LLM callback
└── helpers/
    ├── overlay.py          # DO_DISMISS_OVERLAY
    ├── location.py         # DO_LOCATION
    ├── calendar.py         # DO_CALENDAR
    ├── options.py          # DO_OPTIONS
    └── auth.py             # DO_AUTH_OR_DETAILS
```

## Development

```bash
pip install -e ".[dev]"
ruff check . && ruff format --check .
mypy
```

There is no test suite. The navigator is validated by running it against real sites.

## Limits

- Local Chrome only (no Browserbase).
- Some flows hide their next step behind a wallet button or a sign-in only. Those end as `BLOCKED` or at the step budget rather than being forced.
