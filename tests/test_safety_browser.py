"""The in-page blocker and the safe click helpers, in a real browser against tests/fixtures/guard.

One browser session produces every observation (launching Chrome per test would be slow); the
tests below each assert on one aspect. Skipped when no Chrome/Chromium is installed.
"""

import asyncio
import json
from pathlib import Path

import pytest

from audit import safety
from audit.core import UnsafeActionRefused
from audit.page import js

from .browser_support import find_chrome, launch_args, serve
from .test_safety import MUST_ALLOW, MUST_BLOCK, ORDINARY_FIELDS, SENSITIVE_FIELDS

FIXTURE = Path(__file__).parent / "fixtures" / "guard"
pytestmark = pytest.mark.browser

DENIED_IDS = ["place", "pay-inner", "confirm", "trial", "wallet", "uberone-box", "complete"]
ALLOWED_IDS = ["checkout", "add", "continue", "onetime", "search"]
LOG_NAME = {"pay-inner": "pay", "uberone-box": "uberone", "complete": "orderform", "search": "form"}


async def observe_guard(chrome: str, port: int) -> dict:
    from stagehand import Action, Stagehand, local_browser

    seen: dict = {"llm_calls": 0}

    async def no_llm(params):
        """Stagehand insists on a configured model before act(); nothing here may actually call it."""
        seen["llm_calls"] += 1
        raise AssertionError("the guard tests must not need an LLM")

    browser = await local_browser.launch(headless=True, executable_path=chrome, args=launch_args())
    stagehand = await Stagehand.create(browser=browser, model=no_llm)
    try:
        page = (await browser.context.pages())[0]
        await safety.install(browser.context, page)

        async def run(expression: str):
            return await page.evaluate(f"(() => {{ {expression} }})()")

        async def take_log() -> list[str]:
            return await run("const out = window.log.slice(); window.log.length = 0; return out;")

        async def drain() -> list[dict]:
            return await run("return window.__auditGuard.drain();")

        async def attempt(click) -> dict:
            await click()
            return {"log": await take_log(), "blocked": await drain()}

        def raw_click(element_id: str):
            return lambda: run(f"document.getElementById({json.dumps(element_id)}).click();")

        def real_click(element_id: str):
            return lambda: page.locator(f"#{element_id}").click()

        async def refusal(awaitable) -> str | None:
            try:
                await awaitable
            except UnsafeActionRefused as error:
                return str(error)
            return None

        await page.goto(f"http://127.0.0.1:{port}/index.html")
        await page.wait_for_load_state("load")
        await page.wait_for_timeout(700)  # both frames report in
        seen["installed"] = await run("return Boolean(window.__auditGuard);")
        seen["frames"] = await take_log()
        await drain()

        seen["raw"] = {i: await attempt(raw_click(i)) for i in DENIED_IDS + ALLOWED_IDS}
        seen["real"] = {i: await attempt(real_click(i)) for i in DENIED_IDS + ALLOWED_IDS}
        seen["shadow"] = await attempt(
            lambda: run("document.getElementById('host').shadowRoot.getElementById('buy').click();")
        )

        # Keyboard and programmatic submits
        await run("document.getElementById('note').focus();")
        seen["enter_in_form"] = await attempt(lambda: page.key_press("Enter"))
        seen["request_submit"] = await attempt(lambda: run("document.getElementById('orderform').requestSubmit();"))
        seen["form_submit"] = await attempt(lambda: run("document.getElementById('orderform').submit();"))
        seen["url_after_submits"] = await page.url()
        await run("document.getElementById('confirm').focus();")
        seen["enter_on_button"] = await attempt(lambda: page.key_press("Enter"))

        # Typing
        for field in ("card", "cvv", "expiry", "gift", "email"):
            await run(f"document.getElementById('{field}').focus();")
            await page.type("4111")
            seen[f"typed_{field}"] = {
                "value": await run(f"return document.getElementById('{field}').value;"),
                "blocked": await drain(),
            }

        # Layer 3: the helpers snippets must use
        seen["safe_click_denied"] = await refusal(js(page, "() => safeClick(document.getElementById('place'))"))
        seen["safe_click_allowed"] = await js(page, "() => safeClick(document.getElementById('checkout'))")
        seen["safe_fill_denied"] = await refusal(js(page, "() => safeFill(document.getElementById('gift'), 'SAVE10')"))
        seen["safe_fill_allowed"] = await js(page, "() => safeFill(document.getElementById('email'), 'a@b.test')")
        seen["guard_allows"] = await js(
            page, "() => ['place', 'checkout'].map(id => guardAllows(document.getElementById(id)))"
        )
        seen["after_helpers"] = {"log": await take_log(), "gift": await run("return document.getElementById('gift').value;"),
                                 "email": await run("return document.getElementById('email').value;")}  # fmt: skip

        # Layer 2 against the real client: act(Action) with no model
        go = Action(selector="#checkout", description="Go to checkout button", method="click", arguments=[])
        seen["act_allowed"] = await safety.act_on(stagehand, page, go)
        lie = Action(
            selector="xpath=//button[@id='place']", description="Go to checkout button", method="click", arguments=[]
        )
        seen["act_denied"] = await refusal(safety.act_on(stagehand, page, lie))
        card = Action(selector="#card", description="Email field", method="fill", arguments=["4111111111111111"])
        seen["act_type_denied"] = await refusal(safety.act_on(stagehand, page, card))
        seen["after_act"] = {
            "log": await take_log(),
            "card": await run("return document.getElementById('card').value;"),
        }

        # Python and JavaScript read the rules the same way
        labels = MUST_BLOCK + MUST_ALLOW
        cases = [[label, url, mode] for label in labels for url, mode in (("https://shop.test/cart", "normal"),
                 ("https://shop.test/checkouts/x", "normal"), ("https://shop.test/cart", "restricted"))]  # fmt: skip
        script = f"{safety.RULES_JS}\nreturn {json.dumps(cases)}.map(([l, u, m]) => auditClickVerdict([l], u, m));"
        seen["parity_clicks"] = (cases, await run(script))
        fields = SENSITIVE_FIELDS + ORDINARY_FIELDS
        seen["parity_fields"] = (
            fields,
            await run(f"{safety.RULES_JS}\nreturn {json.dumps(fields)}.map(auditFieldVerdict);"),
        )

        # Navigation: the script is re-installed, and a checkout URL is restricted without being asked
        await page.locator("#next").click()
        await page.wait_for_load_state("load")
        seen["checkout_url"] = await page.url()
        seen["checkout_installed"] = await run("return Boolean(window.__auditGuard);")
        seen["restricted"] = {
            i: await attempt(real_click(i)) for i in ("skip", "close", "dialog-continue", "edit", "place")
        }

        # Hard stop
        with safety.active_guard() as guard:
            await guard.freeze(page)
            seen["frozen"] = {i: await attempt(real_click(i)) for i in ("skip", "close")}
            seen["frozen_safe_click"] = await refusal(js(page, "() => safeClick(document.getElementById('skip'))"))
            skip = Action(selector="#skip", description="Skip button", method="click", arguments=[])
            seen["frozen_act"] = await refusal(safety.act_on(stagehand, page, skip))
            seen["frozen_reads_work"] = await js(page, "() => document.title")
    finally:
        await stagehand.close()
    return seen


@pytest.fixture(scope="module")
def seen() -> dict:
    chrome = find_chrome()
    if chrome is None:
        pytest.skip("no Chrome/Chromium found (set CHROME_PATH to run the browser tests)")
    with serve(FIXTURE) as port:
        return asyncio.run(observe_guard(chrome, port))


def test_blocker_is_installed(seen):
    assert seen["installed"] is True


@pytest.mark.parametrize("how", ["raw", "real"])
@pytest.mark.parametrize("element_id", DENIED_IDS)
def test_denied_controls_are_cancelled(seen, how, element_id):
    """`raw` is element.click() from script (what a JS snippet or the LLM driver does); `real` is a mouse click."""
    outcome = seen[how][element_id]
    assert LOG_NAME.get(element_id, element_id) not in outcome["log"]
    assert outcome["blocked"], "the block must be reported so the run can be ended"


@pytest.mark.parametrize("how", ["raw", "real"])
@pytest.mark.parametrize("element_id", ALLOWED_IDS)
def test_allowed_controls_still_work(seen, how, element_id):
    outcome = seen[how][element_id]
    assert LOG_NAME.get(element_id, element_id) in outcome["log"]
    assert outcome["blocked"] == []


def test_open_shadow_dom_is_covered(seen):
    assert seen["shadow"]["log"] == [] and seen["shadow"]["blocked"]


def test_same_origin_and_cross_origin_frames_are_covered(seen):
    frames = set(seen["frames"])
    assert {"frame:guard=true@127.0.0.1", "frame:guard=true@localhost"} <= frames
    assert {"frame:ok@127.0.0.1", "frame:ok@localhost"} <= frames
    assert not {f for f in frames if f.startswith("frame:paid")}


def test_enter_key_and_programmatic_submits_are_cancelled(seen):
    for case in ("enter_in_form", "request_submit", "form_submit", "enter_on_button"):
        assert seen[case]["log"] == [], case
        assert seen[case]["blocked"], case
    assert seen["url_after_submits"].endswith("/index.html")  # form.submit() did not navigate


@pytest.mark.parametrize("field", ["card", "cvv", "expiry", "gift"])
def test_typing_into_payment_fields_is_cancelled(seen, field):
    assert seen[f"typed_{field}"]["value"] == ""
    assert seen[f"typed_{field}"]["blocked"]


def test_typing_elsewhere_still_works(seen):
    assert seen["typed_email"] == {"value": "4111", "blocked": []}


def test_safe_helpers(seen):
    assert "Place order" in seen["safe_click_denied"]
    assert seen["safe_click_allowed"] is True
    assert "gift card" in seen["safe_fill_denied"].lower()
    assert seen["safe_fill_allowed"] is True
    assert seen["guard_allows"] == [False, True]
    assert seen["after_helpers"] == {"log": ["checkout"], "gift": "", "email": "a@b.test"}


def test_act_on_vets_the_real_element_and_makes_no_llm_call(seen):
    assert seen["act_allowed"] == safety.Acted(True, "Go to checkout button")
    assert "Place order" in seen["act_denied"]  # the description claimed 'Go to checkout'
    assert seen["act_type_denied"] is not None
    assert seen["after_act"] == {"log": ["checkout"], "card": ""}
    assert seen["llm_calls"] == 0  # acting on a vetted Action costs no tokens; only observe() does


def test_python_and_javascript_agree_on_every_rule(seen):
    cases, verdicts = seen["parity_clicks"]
    for (label, url, mode), verdict in zip(cases, verdicts, strict=True):
        assert verdict == safety.click_verdict(label, url=url, mode=safety.Mode(mode)), (label, url, mode)
    fields, verdicts = seen["parity_fields"]
    for descriptor, verdict in zip(fields, verdicts, strict=True):
        assert verdict == safety.field_verdict(descriptor), descriptor


def test_guard_survives_navigation_and_restricts_checkout_urls(seen):
    assert "/checkout/" in seen["checkout_url"] and seen["checkout_installed"] is True
    assert seen["restricted"]["skip"] == {"log": ["skip"], "blocked": []}
    assert seen["restricted"]["close"] == {"log": ["close"], "blocked": []}
    for element_id in ("dialog-continue", "edit", "place"):
        assert seen["restricted"][element_id]["log"] == [], element_id
        assert seen["restricted"][element_id]["blocked"], element_id


def test_frozen_page_allows_only_reads(seen):
    for element_id in ("skip", "close"):
        assert seen["frozen"][element_id]["log"] == []
        assert seen["frozen"][element_id]["blocked"]
    assert "final checkout" in seen["frozen_safe_click"]
    assert "final checkout" in seen["frozen_act"]
    assert seen["frozen_reads_work"] == "Guard fixture: checkout page (the URL makes it restricted)"
