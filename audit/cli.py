"""Command line: `python main.py <workflow> [options]`, one subcommand per registered workflow."""

import argparse
import asyncio

from .runner import run
from .workflows import WORKFLOWS


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--local", action="store_true",
                        help="Use local Chrome even if BROWSERBASE_API_KEY is set in .env")
    common.add_argument("--login", action="store_true",
                        help="Open the site in the persistent local Chrome profile so you can sign in manually, then exit")

    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Drive real purchase flows with Stagehand and audit them for drip pricing.",
    )
    subparsers = parser.add_subparsers(dest="workflow", metavar="workflow", required=True)
    for workflow in WORKFLOWS:
        sub = subparsers.add_parser(workflow.name, help=workflow.help, description=workflow.help, parents=[common])
        workflow.add_arguments(sub)
        sub.set_defaults(workflow_class=workflow, parser=sub)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.login and not args.workflow_class.persistent_profile:
        args.parser.error(f"--login is not available: '{args.workflow}' does not keep a browser profile between runs")
    asyncio.run(run(args.workflow_class(args), args))
