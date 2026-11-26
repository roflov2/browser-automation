# checkout_navigator

A site-agnostic checkout navigator built on Stagehand 4 (Python). From any starting URL it walks a purchase or booking funnel and **stops at the payment wall**. It does not scrape prices, fill in payment details or submit orders.

```bash
pip install -r checkout_navigator/requirements.txt
export GOOGLE_GENERATIVE_AI_API_KEY=...      # or MODEL_API_KEY + NAVIGATOR_MODEL=anthropic/...
python -m checkout_navigator.run --url https://shop.example.com.au
```

It prints one JSON status dictionary and exits with 0 for `DONE` and 1 for anything else:

```json
{"status": "DONE", "steps": 7, "url": "https://.../checkout", "history": ["step 0: DO_DISMISS_OVERLAY ...", "..."]}
```

| Status | Meaning |
| --- | --- |
| `DONE` | The payment wall was detected, or the safety brake refused a final-purchase action (`"stopped_by": "safety_brake"`) |
| `BLOCKED` | A CAPTCHA, bot check, virtual queue that never cleared, or a mandatory sign-in with no guest path (headless / non-interactive) |
| `MAX_STEPS_EXCEEDED` | The step budget (`--max-steps`, default 15) ran out |
| `ERROR` | The browser or the model failed outright (the message is in `error`) |

## The loop

```
observe ─► Are we at checkout? ── yes ─► DONE
               │ no
               ▼
     same URL + same DOM hash as last step? ── yes ─► scroll / dismiss overlay, loop
               │ no
               ▼
     determine_next_step (one model call) ─► route
               │
               ▼
     DO_DISMISS_OVERLAY │ DO_LOCATION │ DO_CALENDAR │ DO_OPTIONS │ DO_AUTH_OR_DETAILS │ DO_ACT
               │          (every click/keystroke goes through cdp_act's payment brake)
               └──────────────────────────────► loop
```

* **`engine/classifier.py`**: `is_at_checkout` is deterministic and makes no model call. It reads payment-processor iframes (Stripe, PayPal, Adyen, Braintree, Afterpay, Klarna and others, read from the parent frame so cross-origin frames count), card-field inputs and final-payment buttons. It returns True for a processor iframe plus a payment button, or for 2 or more input/action signals. `determine_next_step` makes one `extract()` call that returns the route. For `DO_ACT` that call also returns the exact atomic instruction and, when possible, the index of an observed element, so the act itself needs no second model call.
* **`engine/cdp_guard.py`** handles stability and safety. It retries observe/act when a frame is detached (never clicking twice), waits for the page to settle and hydrate, waits out virtual queues, and clears a stale `userDataDir` lock. `compute_dom_hash` hashes the visible controls' text, values and checked states, so stagnation is detected on SPAs whose URL never changes. `cdp_act` adds a "NEVER click Pay…" constraint to every act. It then checks the instruction (ignoring negated clauses), the model's chosen element and that element's live text against final-purchase keywords. On a match it raises `CheckoutReachedInterrupt` instead of clicking. It also refuses to type into card, expiry or CVV fields.
* **`helpers/`** each clear one obstacle and return to the loop: `overlay.py`, `location.py` (delivery address or origin/destination/city, then the first autocomplete match), `calendar.py` (pages month by month to today+14 and today+17), `options.py` (free/default/cheapest choice, declines extras, confirms), and `auth.py` (guest path first, then persona fields via `%variables%`).

## Configuration

`config.py` holds the defaults. Any of them can be overridden by an environment variable or `.env`:

| Setting | Default | Env |
| --- | --- | --- |
| Step budget | 15 | `MAX_STEPS` |
| Delivery address | `240 City Walk, Canberra ACT 2601` | `DEFAULT_ADDRESS` |
| Search defaults | `CBR → SYD`, city `Sydney`, 1 guest | `SEARCH_ORIGIN`, `SEARCH_DESTINATION`, `SEARCH_CITY`, `SEARCH_GUESTS` |
| Persona | Alex Testbooking, `alex.testbooking@example.com`, `0491 570 156` (ACMA fictional range) | `PERSONA_<FIELD>` |
| Model | `google/gemini-3.8-flash` | `NAVIGATOR_MODEL`, `MODEL_API_KEY` / `GOOGLE_GENERATIVE_AI_API_KEY` |
| Azure OpenAI (replaces the model above) | off | `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`, `AZURE_OPENAI_API_VERSION` (`2024-12-01-preview`), `AZURE_OPENAI_MANAGED_IDENTITY` (`false`) |
| Chrome profile | `.chrome-profile/` (shared with the legacy `audit` runs) | `CHROME_PROFILE_DIR`, `--profile-dir` |
| Browser | headed; proxy unset | `HEADLESS`, `CHROME_PATH`, `BROWSER_PROXY_SERVER`, `BROWSER_PROXY_BYPASS` |

`site_hints.yaml` holds the per-domain quirks that could not be removed: hydration waits, settle times, queue limits, consent-button selectors and extra never-click words. It is data matched by host, and no Python code branches on a domain.

## Harvested from the legacy `audit/` workflows (Phase 0)

| Legacy source | Became |
| --- | --- |
| `qantas.py` airport type-ahead (real key presses, polling for the suggestion, code as an upper-case word) | `helpers/location.py` `fill_location_field`, `JS_SUGGESTION` |
| `ubereats/store.py` address modal prompts and building-type "Skip / Not now" | `helpers/location.py` confirm step and the `ubereats.com` `overlay_hint` |
| `qantas.py` date modal (`JS_DAY_CELL`, `date_label_pattern`, month scrolling, Continue) | `helpers/calendar.py` |
| `ubereats/store.py` `JS_SELECT_REQUIRED` (first option without a `+$`), `modal_round` | `helpers/options.py` `JS_SELECT_DEFAULTS`, required-group acts |
| `qantas.py` insurance decline, `BOOKING_GOAL` "choose the free or decline option" | `helpers/options.py` declines, the classifier's goal prompt |
| `qantas.py` `PassengerForm` / `passenger_field_instruction` (`%var%` typing, list choices named) | `helpers/auth.py` `AuthState` / `field_instruction` |
| `page.py` `dismiss_popups` prompt; coffee/ubereats/ticketek popup lists | `helpers/overlay.py` `DISMISS_INSTRUCTION` |
| `core.py` transient CDP errors; `actions.py` `act_once_more`; `runner.py` observe retry | `cdp_guard.with_frame_retry`, `cdp_act` hiccup path |
| `page.py` `settle` / `JS_QUIET`, `wait_for_render`, CDP noise filter | `cdp_guard.settle`, `wait_for_hydration`, `cdp_noise_filtered` |
| `runner.py` persistent profile, fresh tab, `chromium_sandbox=False` as root; `config.py` loopback `NO_PROXY` | `cdp_guard.open_session`, `prepare_user_data_dir`, `keep_loopback_off_proxy` |
| `ticketek/flow.py` queue wait | `cdp_guard.wait_out_queue` (generic queue detection, limit from hints) |
| The four workflows' `SAFETY_PROMPT`s; `qantas.py` `NEVER_CLICK` / `NEVER_FORWARD` | `config.SYSTEM_PROMPT`, `cdp_guard.FINAL_PURCHASE`, `never_click` hints |

## Azure OpenAI

`engine/azure_llm.py` is the legacy `audit/azure.py` adapter, carried over unchanged apart from where it reads its settings. Stagehand 4.4 has no setting for an Azure endpoint, so the adapter plugs in as Stagehand's LLM callback. Each model request becomes a chat completion on your deployment, signed in with a Microsoft Entra ID token from `DefaultAzureCredential`. No API key is used.

```bash
pip install -r checkout_navigator/requirements-azure.txt
az login        # or set AZURE_CLIENT_ID, AZURE_TENANT_ID, AZURE_CLIENT_SECRET for a service principal
export AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com AZURE_OPENAI_DEPLOYMENT=gpt-4o
python -m checkout_navigator.run --url https://shop.example.com.au
```

The adapter keeps all of its legacy behaviour:

- **Sign-in check.** The token is fetched before Chrome opens, so a sign-in problem stops the run straight away with a message.
- **Schema repair.** Off-schema answers have forbidden keys dropped and missing nullable keys filled with null, and the model is asked once more if the answer still doesn't fit.
- **Retries.** Throttling, server errors and dropped connections are retried up to three times, honouring `retry-after` (at most 60 s each), and every retry is logged.

**Requirements:**

- The signed-in identity needs the *Cognitive Services OpenAI User* role on the resource.
- The deployment must accept JSON-schema response formats: gpt-4o 2024-08-06 or later, with API version 2024-08-01-preview or later.

Not carried over: Browserbase. It still works for the legacy `audit` package. If needed, it can be wired into the browser launch in `open_session`.
