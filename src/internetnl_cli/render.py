"""Build the result document and render it as JSON or a plain-text table.

Every rendering carries the endpoint host, run timestamp, API version and
request id — see design.md's Output section. `domains` is passed through
from the API reply unmodified.
"""

from __future__ import annotations

import json
from datetime import datetime


def build_document(
    endpoint_host: str,
    request_id: str,
    reply: dict,
    retrieved_at: datetime,
    checks: dict | None,
) -> dict:
    request = reply.get("request") or {}
    finished_date = request.get("finished_date")
    if finished_date:
        timestamp = finished_date
    else:
        timestamp = retrieved_at.strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "endpoint": endpoint_host,
        "timestamp": timestamp,
        "api_version": reply.get("api_version", "unknown"),
        "request_id": request_id,
        "request": reply.get("request"),
        "domains": reply.get("domains"),
        "checks": checks,
    }


def render_json(doc: dict, stream) -> None:
    stream.write(json.dumps(doc, indent=2))
    stream.write("\n")


_TEST_STATUS_ORDER = ("passed", "failed", "warning", "info", "error", "not_tested")
_COLUMNS = ("STATUS", "SCORE", "FAILED", "WARNING", "INFO", "ERROR", "UNKNOWN")
_COL_WIDTH = 7


def _format_row(host: str, host_width: int, values: list[str]) -> str:
    cells = [host.ljust(host_width)] + [v.ljust(_COL_WIDTH) for v in values[:-1]] + [values[-1]]
    return " ".join(cells).rstrip()


def render_table(doc: dict, stream) -> None:
    lines = []
    lines.append(f"endpoint: {doc['endpoint']}")
    lines.append(f"request-id: {doc['request_id']}")
    lines.append(f"timestamp: {doc['timestamp']}")
    lines.append(f"api_version: {doc['api_version']}")
    lines.append("")

    domains = doc.get("domains") or {}
    checks = doc.get("checks") or {"failed": [], "accepted": [], "unknown": []}

    hosts = list(domains.keys())
    host_width = max([len("HOST")] + [len(h) for h in hosts])

    unknown_by_host: dict[str, int] = {}
    for entry in checks.get("unknown", []):
        unknown_by_host[entry["host"]] = unknown_by_host.get(entry["host"], 0) + 1

    header = "HOST".ljust(host_width) + " " + " ".join(c.ljust(_COL_WIDTH) for c in _COLUMNS[:-1]) + " " + _COLUMNS[-1]
    lines.append(header.rstrip())

    for host in hosts:
        domain = domains[host]
        status = domain.get("status")
        if status == "error":
            row = _format_row(host, host_width, ["error"] + ["-"] * 6)
            lines.append(row)
            continue

        counts = {s: 0 for s in _TEST_STATUS_ORDER}
        tests = ((domain.get("results") or {}).get("tests")) or {}
        for test in tests.values():
            test_status = test.get("status")
            if test_status in counts:
                counts[test_status] += 1

        score = domain.get("scoring", {}).get("percentage")
        score_str = str(score) if score is not None else "-"
        unknown_count = unknown_by_host.get(host, 0)

        row = _format_row(
            host,
            host_width,
            [
                "ok",
                score_str,
                str(counts["failed"]),
                str(counts["warning"]),
                str(counts["info"]),
                str(counts["error"]),
                str(unknown_count),
            ],
        )
        lines.append(row)

    for section in ("failed", "accepted", "unknown"):
        entries = checks.get(section, [])
        if entries:
            lines.append("")
            lines.append(f"{section}:")
            for entry in entries:
                lines.append(f"  {entry['host']} {entry['test']}")

    informational = []
    for host in hosts:
        domain = domains[host]
        if domain.get("status") == "error":
            continue
        tests = ((domain.get("results") or {}).get("tests")) or {}
        for test_name in sorted(tests):
            test_status = tests[test_name].get("status")
            if test_status in ("warning", "info", "error"):
                informational.append(f"  {host} {test_name} {test_status}")

    lines.append("")
    lines.append("informational:")
    lines.extend(informational)

    stream.write("\n".join(lines))
    stream.write("\n")
