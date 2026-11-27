"""CLI entrypoint: walk any purchase / booking funnel from --url to the payment wall, then stop.

    python -m checkout_navigator.run --url https://example-shop.com.au
    python checkout_navigator/run.py --url https://example-shop.com.au --max-steps 20 --headless

Prints the terminal status dictionary as JSON (status DONE, BLOCKED or MAX_STEPS_EXCEEDED) and exits 0 for DONE,
1 for anything else. It never fills payment details and never submits an order.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # run as a script: make the package importable
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from checkout_navigator.config import load_config
from checkout_navigator.engine.cdp_guard import load_site_hints, open_session
from checkout_navigator.engine.navigator import reach_checkout


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Navigate any checkout funnel to the payment wall, then stop.")
    parser.add_argument("--url", required=True, help="Starting URL (a home, product, store, event or search page)")
    parser.add_argument("--max-steps", type=int, default=None, help="Step budget (default 15, or MAX_STEPS)")
    parser.add_argument("--headless", action="store_true", default=None, help="Run Chrome without a window")
    parser.add_argument("--profile-dir", type=Path, default=None, help="Persistent Chrome userDataDir")
    parser.add_argument("--address", default=None, help="Delivery address to use (default: the Canberra address)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    return parser.parse_args(argv)


async def main_async(args: argparse.Namespace) -> dict[str, Any]:
    url = args.url if args.url.startswith(("http://", "https://")) else f"https://{args.url}"
    config = load_config(
        max_steps=args.max_steps,
        default_address=args.address,
        headless=args.headless,
        user_data_dir=args.profile_dir,
    )
    if config.llm.azure is None and not config.llm.api_key:
        logging.getLogger(__name__).warning(
            "No model API key found (MODEL_API_KEY / GOOGLE_GENERATIVE_AI_API_KEY / ANTHROPIC_API_KEY); "
            "Stagehand will rely on its own environment lookup"
        )
    context = config.context(load_site_hints(url))
    async with open_session(config) as (stagehand, page):
        try:
            result = await reach_checkout(stagehand, page, url, context)
        except Exception as error:  # noqa: BLE001 - still report the tokens spent before the failure
            result = error_result(url, error)
        tokens = await token_usage(stagehand)
        if tokens is not None:
            result["tokens"] = tokens
        return result


def error_result(url: str, error: BaseException) -> dict[str, Any]:
    logging.getLogger(__name__).debug("Run failed", exc_info=error)
    return {"status": "ERROR", "url": url, "error": f"{type(error).__name__}: {error}"}


async def token_usage(stagehand: Any) -> dict[str, int] | None:
    """The model tokens Stagehand counted across every act, extract and observe call in the session."""
    try:
        metrics = await stagehand.metrics()
    except Exception as error:  # noqa: BLE001 - the browser may be gone; the count is a nice-to-have
        logging.getLogger(__name__).debug("Could not read Stagehand metrics: %s", error)
        return None
    prompt = int(metrics.get("total_prompt_tokens") or 0)
    completion = int(metrics.get("total_completion_tokens") or 0)
    return {
        "input": prompt,
        "cached_input": int(metrics.get("total_cached_input_tokens") or 0),
        "output": completion,
        "reasoning": int(metrics.get("total_reasoning_tokens") or 0),
        "total": prompt + completion,
    }


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")
    result: dict[str, Any]
    try:
        result = asyncio.run(main_async(args))
    except KeyboardInterrupt:
        result = {"status": "INTERRUPTED", "url": args.url}
    except Exception as error:  # noqa: BLE001 - any failure becomes the ERROR status
        result = error_result(args.url, error)
    print(json.dumps(result, indent=2, default=str))
    if tokens := result.get("tokens"):
        print(f"Tokens: {tokens['total']:,} ({tokens['input']:,} in, {tokens['output']:,} out)", file=sys.stderr)
    sys.exit(0 if result.get("status") == "DONE" else 1)


if __name__ == "__main__":
    main()
