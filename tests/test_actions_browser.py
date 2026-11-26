"""popup_hints reads a real element's ARIA attributes through the xpath Stagehand gives it."""

import asyncio

import pytest

from audit.actions import popup_hints

from .browser_support import find_chrome, launch_args

HTML = """<form><div role="combobox" aria-haspopup="listbox" aria-expanded="false"><span>Return</span></div>
<button type="submit">Search flights</button></form>"""


@pytest.mark.browser
def test_popup_hints_in_a_real_page():
    chrome = find_chrome()
    if chrome is None:
        pytest.skip("no Chrome/Chromium found (set CHROME_PATH to run the browser tests)")

    async def scenario():
        from stagehand import Stagehand, local_browser

        browser = await local_browser.launch(headless=True, executable_path=chrome, args=launch_args())
        stagehand = await Stagehand.create(browser=browser)
        try:
            page = (await browser.context.pages())[0]
            await page.evaluate(f"document.body.innerHTML = {HTML!r}")
            return (
                await popup_hints(page, "xpath=/html[1]/body[1]/form[1]/div[1]/span[1]"),
                await popup_hints(page, "xpath=/html[1]/body[1]/form[1]/button[1]"),
                await popup_hints(page, "xpath=/html[1]/body[1]/form[1]/button[9]"),
                await popup_hints(page, "css=button"),
            )
        finally:
            await stagehand.close()
            await browser.close()

    inside, submit, missing, css = asyncio.run(scenario())
    assert inside == {"haspopup": "listbox", "role": "combobox", "expanded": "false"}  # from the enclosing combobox
    assert submit == {"haspopup": None, "role": None, "expanded": None}
    assert missing is None and css is None
