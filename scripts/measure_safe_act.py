"""Measure what the purchase guard's safe_act() costs compared with act() on a plain instruction.

Runs against the local HTML fixture in tests/fixtures/guard: no real site is touched.

    python scripts/measure_safe_act.py            # real model; needs GOOGLE_GENERATIVE_AI_API_KEY (or .env)
    python scripts/measure_safe_act.py --stub     # no API key: a stub model that counts calls and prompt size

Each instruction is run `--repeat` times both ways. Tokens come from stagehand.metrics().
"""

import argparse
import asyncio
import contextlib
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from stagehand import Stagehand, local_browser  # noqa: E402

from audit import safety  # noqa: E402
from audit.azure import open_azure_llm  # noqa: E402
from audit.config import Settings, load_settings  # noqa: E402
from tests.browser_support import StubModel, find_chrome, launch_args, serve  # noqa: E402

INSTRUCTIONS = (
    "Click the 'Go to checkout' button.",
    "Click the 'Add to cart' button.",
    "Type 'alex@example.com' into the Email field.",
)


async def tokens(stagehand: Stagehand) -> float:
    metrics = await stagehand.metrics()
    return metrics.total_prompt_tokens + metrics.total_completion_tokens


async def measure(args: argparse.Namespace, port: int) -> None:
    settings = load_settings()
    chrome = settings.chrome_path or find_chrome()
    stub = StubModel() if args.stub else None
    async with contextlib.AsyncExitStack() as stack:
        model = stub or (await stack.enter_async_context(open_azure_llm(settings)) if settings.use_azure else None)
        await run_measurement(args, port, settings, chrome, stub, model)


async def run_measurement(
    args: argparse.Namespace,
    port: int,
    settings: Settings,
    chrome: Path | str | None,
    stub: StubModel | None,
    model: Any,
) -> None:
    browser = await local_browser.launch(headless=True, executable_path=chrome, args=launch_args())
    stagehand = await Stagehand.create(
        browser=browser,
        **({"model": model} if model else {"model": settings.model, "model_api_key": settings.require_api_key()}),
    )
    try:
        page = (await browser.context.pages())[0]
        await safety.install(browser.context, page)
        totals = {"act(str)": [0.0, 0], "safe_act": [0.0, 0]}
        print(f"{'instruction':<48} {'act(str)':>12} {'safe_act':>12}   (tokens per call)")
        for instruction in INSTRUCTIONS:
            row = []
            for name in totals:
                spent = 0.0
                for _ in range(args.repeat):
                    await page.goto(f"http://127.0.0.1:{port}/index.html")
                    before, calls_before = await tokens(stagehand), stub.calls if stub else 0
                    if name == "safe_act":
                        await safety.safe_act(stagehand, page, instruction)
                    else:  # the unguarded baseline; this script is the only place that still does it
                        await stagehand.act(instruction, timeout=safety.LLM_TIMEOUT_MS)
                    spent += await tokens(stagehand) - before
                    totals[name][1] += (stub.calls - calls_before) if stub else 0
                totals[name][0] += spent
                row.append(spent / args.repeat)
            print(f"{instruction:<48} {row[0]:>12.0f} {row[1]:>12.0f}")
        runs = len(INSTRUCTIONS) * args.repeat
        print(f"{'mean':<48} {totals['act(str)'][0] / runs:>12.0f} {totals['safe_act'][0] / runs:>12.0f}")
        if stub:
            print(f"LLM calls per action: act(str) {totals['act(str)'][1] / runs:.1f}, safe_act {totals['safe_act'][1] / runs:.1f}"
                  " (stub model: token figures are prompt-size estimates, not billing)")  # fmt: skip
    finally:
        await stagehand.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--stub", action="store_true", help="Use a stub model instead of Gemini/Azure (no API key needed)"
    )
    parser.add_argument("--repeat", type=int, default=3, help="Runs per instruction and method (default 3)")
    args = parser.parse_args()
    with serve(REPO / "tests" / "fixtures" / "guard") as port:
        asyncio.run(measure(args, port))


if __name__ == "__main__":
    main()
