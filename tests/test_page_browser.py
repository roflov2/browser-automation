"""page.js(), the waits and the screenshot helper in a real browser against local fixtures."""

import asyncio
from pathlib import Path
from time import perf_counter

import pytest

from audit import safety
from audit.core import PageScriptError, UnsafeActionRefused
from audit.page import js, settle, shot, try_goto, wait_for_render, wait_until
from audit.workflows.coffee import Product, resolve_product_url

from .browser_support import find_chrome, launch_args, serve

FIXTURE = Path(__file__).parent / "fixtures" / "pages"
pytestmark = pytest.mark.browser


async def observe_page(chrome: str, port: int, tmp: Path) -> dict:
    from stagehand import Stagehand, local_browser

    seen: dict = {}
    base = f"http://127.0.0.1:{port}"
    browser = await local_browser.launch(headless=True, executable_path=chrome, args=launch_args())
    stagehand = await Stagehand.create(browser=browser)
    try:
        page = (await browser.context.pages())[0]

        async def timed(awaitable) -> tuple[float, object]:
            start = perf_counter()
            value = await awaitable
            return perf_counter() - start, value

        async def error_of(awaitable) -> str | None:
            try:
                await awaitable
            except PageScriptError as error:
                return str(error)
            return None

        # A page's own script submitting a form: an ordinary form goes through; one submitted through a
        # 'Buy now' button is cancelled and logged, and the run goes on either way.
        await safety.install(browser.context, page)
        with safety.active_guard():
            for name, path in (("checkout", "/checkout/"), ("ordinary", "/self-submit.html")):
                await page.goto(f"{base}{path}")
                try:
                    await settle(page, 1000)
                    seen[f"self_submit_{name}"] = ("went on", await page.url())
                except UnsafeActionRefused as refusal:
                    seen[f"self_submit_{name}"] = ("refused", str(refusal))

        # Which link a coffee product is opened from, whatever URL the model extracted for it
        await page.goto(f"{base}/carousel.html")
        shop = f"{base}/carousel.html"
        for case, name, url in (
            ("model_gave_section_link", "Lomaverde, Colombia", "/collections/single"),
            ("model_gave_element_id", "Lomaverde, Colombia", "4-2475"),
            ("model_gave_nothing", "Kamvara Estate AA, Kenya", None),
            ("name_not_on_page_but_linked", "Mystery Blend", f"{base}/products/unnamed-card/"),
            ("name_not_on_page_and_not_linked", "Mystery Blend", "/products/made-up"),
        ):
            product = Product(name=name, price="", sold_out=False, is_coffee=True, product_type="other", url=url)
            found = await resolve_product_url(page, product, shop)
            seen[f"resolve_{case}"] = found.removeprefix(base) if found else None

        await page.goto(f"{base}/static.html")
        seen["value"] = await js(page, "() => document.title")
        seen["arg"] = await js(page, "who => 'hello ' + who.name", {"name": "audit"})
        seen["lib"] = await js(page, "() => txt(document.getElementById('text'))")
        seen["null"] = await js(page, "() => document.querySelector('#missing')", default="fallback")
        seen["undefined"] = await js(page, "() => undefined", default=0)
        seen["false"] = await js(page, "() => false", default="fallback")  # false is a value, not "missing"
        seen["zero"] = await js(page, "() => 0", default="fallback")
        seen["threw"] = await error_of(
            js(page, "() => document.querySelector('#missing').click_handler()", default="x")
        )
        seen["reference"] = await error_of(js(page, "() => notDefinedAnywhere + 1", default="x"))
        seen["syntax"] = await error_of(js(page, "() => { return (; }", default="x"))

        seen["settle_static"], _ = await timed(settle(page, 3000))
        seen["wait_true"], seen["wait_true_value"] = await timed(
            wait_until(page, "() => document.title.length > 0", 3000)
        )
        seen["wait_false"], seen["wait_false_value"] = await timed(wait_until(page, "() => false", 600))

        await page.goto(f"{base}/busy.html?ms=1200")
        seen["settle_busy"], _ = await timed(settle(page, 5000))
        seen["busy_rows_after_settle"] = await js(page, "() => document.querySelectorAll('#app p').length")
        await page.wait_for_timeout(400)
        seen["busy_rows_later"] = await js(page, "() => document.querySelectorAll('#app p').length")

        await page.goto(f"{base}/busy.html?ms=60000")
        seen["settle_capped"], _ = await timed(settle(page, 1000))
        seen["render_timeout"], seen["render_timeout_value"] = await timed(wait_for_render(page, 1))

        await page.goto(f"{base}/busy.html?ms=900")
        seen["render_ok"], seen["render_ok_value"] = await timed(wait_for_render(page, 10))
        seen["spinner_gone"] = await js(page, "() => !document.getElementById('spinner')")

        await shot(page, tmp / "page.png")
        seen["screenshot_bytes"] = (tmp / "page.png").stat().st_size
        seen["goto_ok"] = await try_goto(page, f"{base}/static.html")
    finally:
        await stagehand.close()
    return seen


@pytest.fixture(scope="module")
def seen(tmp_path_factory) -> dict:
    chrome = find_chrome()
    if chrome is None:
        pytest.skip("no Chrome/Chromium found (set CHROME_PATH to run the browser tests)")
    with serve(FIXTURE) as port:
        return asyncio.run(observe_page(chrome, port, tmp_path_factory.mktemp("shots")))


def test_js_returns_values(seen):
    assert seen["value"] == "Static page"
    assert seen["arg"] == "hello audit"
    assert seen["lib"] == "Nothing changes here."


def test_js_null_gives_the_default_but_falsy_values_are_values(seen):
    assert seen["null"] == "fallback"
    assert seen["undefined"] == 0
    assert seen["false"] is False
    assert seen["zero"] == 0


@pytest.mark.parametrize("case", ["threw", "reference", "syntax"])
def test_js_raises_when_the_snippet_is_broken(seen, case):
    """A snippet that throws or does not parse is a bug, not 'element not found' (REVIEW H6)."""
    assert seen[case] is not None, "expected PageScriptError, got the default"


def test_settle_returns_once_the_page_is_quiet(seen):
    assert seen["settle_static"] < 1.5  # was a fixed 3 s sleep
    assert 1.0 < seen["settle_busy"] < 3.5  # waits out 1.2 s of rendering, then about half a second of quiet
    assert seen["busy_rows_after_settle"] == seen["busy_rows_later"]  # nothing was still rendering


def test_settle_never_waits_longer_than_its_bound(seen):
    assert seen["settle_capped"] < 2.5  # bound of 1 s on a page that never stops


def test_wait_until(seen):
    assert seen["wait_true_value"] is True and seen["wait_true"] < 1.0
    assert seen["wait_false_value"] is False and 0.5 < seen["wait_false"] < 2.0


def test_wait_for_render(seen):
    assert seen["render_ok_value"] is True and seen["spinner_gone"] is True
    assert seen["render_ok"] < 4.0
    assert seen["render_timeout_value"] is False and seen["render_timeout"] < 3.0


def test_screenshot_and_navigation_helpers(seen):
    assert seen["screenshot_bytes"] > 1000
    assert seen["goto_ok"] is True


def test_a_checkout_page_submitting_its_own_plain_form_is_left_alone(seen):
    outcome, url = seen["self_submit_checkout"]
    assert outcome == "went on"
    assert "/static.html" in url  # no purchase control in the form, so nothing to cancel


def test_a_page_submitting_its_own_buy_now_form_is_cancelled_without_ending_the_run(seen):
    outcome, url = seen["self_submit_ordinary"]
    assert outcome == "went on"
    assert url.endswith("/self-submit.html")  # the submit was cancelled: the page did not navigate away


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("model_gave_section_link", "/products/lomaverde-colombia"),  # the redbrick.coffee skip
        ("model_gave_element_id", "/products/lomaverde-colombia"),
        ("model_gave_nothing", "/products/kamvara-estate-aa"),
        ("name_not_on_page_but_linked", "/products/unnamed-card"),
        ("name_not_on_page_and_not_linked", None),  # clicked by name instead
    ],
)
def test_a_product_is_opened_from_the_link_the_page_gives_it(seen, case, expected):
    assert seen[f"resolve_{case}"] == expected
