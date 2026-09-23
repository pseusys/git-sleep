"""Command line entry point: parse arguments, load the plan and run the orchestrator."""

from __future__ import annotations

# Not `from sys import stderr`: that binds the stream at import time, so anything rebinding sys.stderr later is bypassed.
import sys
from argparse import Action, ArgumentParser, Namespace
from logging import DEBUG, INFO, WARNING, basicConfig, getLogger
from os import environ
from pathlib import Path
from typing import Any

from git_sleep import __version__
from git_sleep.config import ConfigError, load_plan
from git_sleep.engine import Orchestrator
from git_sleep.github import DEFAULT_API_URL, GitHubClient
from git_sleep.schema import schema_json

EXIT_ALL_MERGED = 0
EXIT_NOT_ALL_MERGED = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class PrintSchema(Action):
    """``--schema``: print the plan file's JSON schema and exit, the way ``--version`` prints the version."""

    def __init__(self, option_strings: list[str], dest: str, **kwargs: Any) -> None:
        super().__init__(option_strings, dest, nargs=0, **kwargs)

    def __call__(self, parser: ArgumentParser, namespace: Namespace, values: Any, option_string: str | None = None) -> None:
        print(schema_json(), end="")
        parser.exit()


def build_parser() -> ArgumentParser:
    """The ``git-sleep`` argument parser."""
    parser = ArgumentParser(prog="git-sleep", description="Wait for pull requests across repositories, restart failed GitHub Actions runs and merge the pull requests in dependency order.")
    parser.add_argument("plan", type=Path, help="JSON file listing pull requests and their dependencies")
    parser.add_argument("--token", help="GitHub token (default: $GITHUB_TOKEN, then $GH_TOKEN)")
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="GitHub API URL, e.g. for GitHub Enterprise (default: %(default)s)")
    parser.add_argument("--poll-interval", type=float, metavar="SECONDS", help="override the plan's poll interval")
    parser.add_argument("--dry-run", action="store_true", help="only report what would be done: no restarts, branch updates or merges")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every poll, not only status changes")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--schema", action=PrintSchema, help="print the JSON schema of the plan file and exit")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run ``git-sleep`` and return its exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    basicConfig(level=DEBUG if args.verbose else INFO, format=LOG_FORMAT, datefmt=LOG_DATE_FORMAT)
    # httpx logs every request at INFO; only show that with --verbose.
    getLogger("httpx").setLevel(DEBUG if args.verbose else WARNING)

    try:
        plan = load_plan(args.plan)
    except ConfigError as e:
        print(f"{parser.prog}: {e}", file=sys.stderr)
        return EXIT_USAGE
    if args.poll_interval is not None:
        if args.poll_interval <= 0:
            print(f"{parser.prog}: --poll-interval must be positive", file=sys.stderr)
            return EXIT_USAGE
        plan = plan.model_copy(update={"poll_interval": args.poll_interval})

    token = args.token or environ.get("GITHUB_TOKEN") or environ.get("GH_TOKEN")
    if not token:
        print(f"{parser.prog}: no GitHub token: pass --token or set GITHUB_TOKEN", file=sys.stderr)
        return EXIT_USAGE

    orchestrator = Orchestrator(plan, GitHubClient(token, args.api_url), dry_run=args.dry_run)
    try:
        return EXIT_ALL_MERGED if orchestrator.run() else EXIT_NOT_ALL_MERGED
    except KeyboardInterrupt:
        getLogger("git_sleep").warning("interrupted")
        return EXIT_INTERRUPTED
