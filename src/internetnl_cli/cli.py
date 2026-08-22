"""Command-line entry point for internetnl-cli.

The argparse tree here is the pinned CLI surface from design.md:

    internetnl [--debug] submit  [HOST ...] [--file FILE] [--type {web,mail}]
                                  [--name NAME] [--no-poll] [COMMON]
    internetnl [--debug] poll    REQUEST_ID [COMMON]
    internetnl [--debug] results REQUEST_ID [COMMON]

    COMMON: --json --fail-on-scored --allowlist FILE

Subcommand bodies are filled in as later tasks land; until then they raise
NotImplementedError so `--help` and argument parsing can already be tested.
"""

from __future__ import annotations

import argparse
import sys
from typing import IO

from internetnl_cli.errors import InternetnlError


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="emit one JSON document on stdout")
    common.add_argument(
        "--fail-on-scored",
        action="store_true",
        help="exit non-zero when a scored subtest failed",
    )
    common.add_argument("--allowlist", metavar="FILE", help="allowlist of accepted failures")

    parser = argparse.ArgumentParser(prog="internetnl")
    parser.add_argument("--debug", action="store_true", help="print each HTTP request to stderr")

    subparsers = parser.add_subparsers(dest="command", required=True)

    submit = subparsers.add_parser("submit", parents=[common], help="submit a batch request")
    submit.add_argument("hosts", metavar="HOST", nargs="*", help="hostnames to test")
    submit.add_argument("--file", metavar="FILE", help="file of hostnames, one per line")
    submit.add_argument("--type", choices=["web", "mail"], default="web", help="test type")
    submit.add_argument("--name", help="free-form label for the request")
    submit.add_argument("--no-poll", action="store_true", help="submit and exit without polling")

    poll = subparsers.add_parser("poll", parents=[common], help="resume polling a run")
    poll.add_argument("request_id", metavar="REQUEST_ID")

    results = subparsers.add_parser("results", parents=[common], help="render a finished run")
    results.add_argument("request_id", metavar="REQUEST_ID")

    return parser


def _run_submit(args: argparse.Namespace, *, opener, sleep, stdout: IO[str], stderr: IO[str]) -> int:
    raise NotImplementedError


def _run_poll(args: argparse.Namespace, *, opener, sleep, stdout: IO[str], stderr: IO[str]) -> int:
    raise NotImplementedError


def _run_results(args: argparse.Namespace, *, opener, sleep, stdout: IO[str], stderr: IO[str]) -> int:
    raise NotImplementedError


_DISPATCH = {
    "submit": _run_submit,
    "poll": _run_poll,
    "results": _run_results,
}


def main(
    argv: list[str] | None = None,
    *,
    opener=None,
    sleep=None,
    stdout: IO[str] | None = None,
    stderr: IO[str] | None = None,
) -> int:
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr

    parser = _build_parser()
    args = parser.parse_args(argv)

    handler = _DISPATCH[args.command]
    try:
        return handler(args, opener=opener, sleep=sleep, stdout=stdout, stderr=stderr)
    except InternetnlError as exc:
        stderr.write(f"error: {exc}\n")
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
