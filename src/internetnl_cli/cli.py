"""Command-line entry point for internetnl-cli.

The argparse tree here is the pinned CLI surface from design.md:

    internetnl [--debug] submit  [HOST ...] [--file FILE] [--type {web,mail}]
                                  [--name NAME] [--no-poll] [COMMON]
    internetnl [--debug] poll    REQUEST_ID [COMMON]
    internetnl [--debug] results REQUEST_ID [COMMON]

    COMMON: --json --fail-on-scored --allowlist FILE
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from typing import IO

from internetnl_cli import config as config_module
from internetnl_cli import gating
from internetnl_cli.client import BatchClient, is_valid_request_id, urllib_opener
from internetnl_cli.errors import ApiError, InternetnlError, RunFailed, TransportError
from internetnl_cli.hosts import collect_hosts
from internetnl_cli.poll import poll_until_done
from internetnl_cli.render import build_document, render_json, render_table, sanitize


# Kept identical to poll._UNFINISHED (not imported to avoid reaching into a
# private name across modules); see design.md's Commands section.
_UNFINISHED_STATUSES = {"registering", "running", "generating"}


def _write_stderr(stream: IO[str], message: str) -> None:
    """Write one sanitized line to stderr (review round 2, m1).

    Every stderr line — the `error: …` path, status lines, warnings — must
    go through the same control-character filter as table cells, since all
    of them can carry API- or file-supplied bytes (an error message quoting
    a bad request id, an unrecognised status string, ...). `message` must
    not itself contain a trailing newline.
    """
    stream.write(sanitize(message) + "\n")


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


def _render(client, cfg, request_id, args, stdout, stderr) -> int:
    reply = client.results(request_id)
    allow = gating.parse_allowlist(args.allowlist) if args.allowlist else set()

    # Review round 1 (B2): fold in the reference subtest names the instance
    # itself declares, so a test the server omitted for every host still
    # renders unknown instead of vanishing. A failed or malformed metadata
    # fetch is a degraded render, never a hard failure.
    request_type = (reply.get("request") or {}).get("request_type")
    metadata_reference: set[str] = set()
    degraded_reason: str | None = None
    try:
        metadata = client.metadata_report()
    except (ApiError, TransportError) as exc:
        degraded_reason = str(exc)
    else:
        result = gating.reference_from_metadata(metadata, request_type)
        # Round 2 (m4): a metadata reply that parses but yields no usable
        # test list (missing/malformed report/hierarchy/data) is a
        # degradation just like a failed call — it must not warn silently.
        # A structurally valid reply that simply has few/no tests for this
        # request type returns an (possibly empty) set, not None, and is
        # not a degradation.
        if result is None:
            degraded_reason = "metadata reply had no usable test list"
        else:
            metadata_reference = result

    if degraded_reason is not None:
        _write_stderr(
            stderr,
            f"warning: metadata unavailable ({degraded_reason}): "
            "unknown-detection limited to tests in this response",
        )

    checks = gating.evaluate(reply.get("domains") or {}, allow, extra_reference=metadata_reference)
    doc = build_document(cfg.endpoint_host, request_id, reply, datetime.now(timezone.utc), checks)
    if args.json:
        render_json(doc, stdout)
    else:
        render_table(doc, stdout)
    if args.fail_on_scored:
        gating.gate(checks)
    return 0


def _run_submit(args: argparse.Namespace, *, cfg, client, sleep, stdout: IO[str], stderr: IO[str]) -> int:
    hosts = collect_hosts(args.file, args.hosts)
    if not hosts:
        _write_stderr(stderr, "error: no hosts given")
        return 2
    if len(hosts) > cfg.batch_size:
        _write_stderr(
            stderr, f"error: {len(hosts)} hosts exceeds INTERNETNL_BATCH_SIZE ({cfg.batch_size})"
        )
        return 2

    reply = client.submit(hosts, args.type, args.name)
    request_id = reply["request"]["request_id"]
    _write_stderr(stderr, f"request-id: {request_id}")

    if args.no_poll:
        return 0

    def _progress(status):
        _write_stderr(stderr, f"status: {status}")

    poll_until_done(
        client,
        request_id,
        cfg.poll_interval,
        cfg.poll_max,
        sleep=sleep,
        progress=_progress,
    )
    return _render(client, cfg, request_id, args, stdout, stderr)


def _run_poll(args: argparse.Namespace, *, cfg, client, sleep, stdout: IO[str], stderr: IO[str]) -> int:
    if not is_valid_request_id(args.request_id):
        _write_stderr(
            stderr,
            f"error: invalid request id {args.request_id!r}: expected 32 lowercase hex characters",
        )
        return 2

    def _progress(status):
        _write_stderr(stderr, f"status: {status}")

    poll_until_done(
        client,
        args.request_id,
        cfg.poll_interval,
        cfg.poll_max,
        sleep=sleep,
        progress=_progress,
    )
    return _render(client, cfg, args.request_id, args, stdout, stderr)


def _run_results(args: argparse.Namespace, *, cfg, client, sleep, stdout: IO[str], stderr: IO[str]) -> int:
    if not is_valid_request_id(args.request_id):
        _write_stderr(
            stderr,
            f"error: invalid request id {args.request_id!r}: expected 32 lowercase hex characters",
        )
        return 2

    status_reply = client.status(args.request_id)
    status = status_reply["request"]["status"]
    if status == "done":
        return _render(client, cfg, args.request_id, args, stdout, stderr)

    if status in ("error", "cancelled"):
        raise RunFailed(f"run {args.request_id} ended as {status}")

    if status not in _UNFINISHED_STATUSES:
        raise ApiError(f"unexpected request status '{status}' from {client.endpoint_host}")

    _write_stderr(stderr, f"status: {status}")
    if args.json:
        doc = build_document(
            cfg.endpoint_host, args.request_id, status_reply, datetime.now(timezone.utc), None
        )
        doc["domains"] = None
        doc["checks"] = None
        render_json(doc, stdout)
    return 0


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
    for stream in (stdout, stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="backslashreplace")
            except (ValueError, TypeError):
                pass

    parser = _build_parser()
    args = parser.parse_args(argv)

    handler = _DISPATCH[args.command]
    try:
        cfg = config_module.resolve()
        client = BatchClient(
            cfg,
            opener=opener or urllib_opener,
            debug_stream=stderr if args.debug else None,
        )
        return handler(
            args,
            cfg=cfg,
            client=client,
            sleep=sleep or time.sleep,
            stdout=stdout,
            stderr=stderr,
        )
    except InternetnlError as exc:
        _write_stderr(stderr, f"error: {exc}")
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
