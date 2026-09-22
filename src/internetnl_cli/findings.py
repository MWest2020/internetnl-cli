"""Build the `netnl-findings/v1` export document from a completed batch reply.

This is a pure transform of the shape `BatchClient.results()` returns (the
same `reply` dict `render.build_document` consumes): `request` for
`finished_date`/`request_type`, `domains` for the per-domain payload. No
network I/O, no aggregation, no scoring of its own — see proposal.md's
"Why" for why that judgement stays with the API.
"""

from __future__ import annotations

import json

SCHEMA = "netnl-findings/v1"


def _category_for(test_name: str, categories: dict) -> str | None:
    """The longest key in `categories` that prefixes `test_name`, or None."""
    best: str | None = None
    for key in categories:
        if test_name.startswith(key) and (best is None or len(key) > len(best)):
            best = key
    return best


def _domain_block(name: str, domain: dict, domain_type: str | None, measured_at: str | None) -> dict:
    results = domain.get("results") or {}
    categories = results.get("categories") or {}
    tests = results.get("tests") or {}
    report = domain.get("report") or {}
    scoring = domain.get("scoring") or {}

    entries = []
    for test_name in sorted(tests):
        test = tests[test_name] or {}
        entries.append(
            {
                "test": test_name,
                "category": _category_for(test_name, categories),
                "status": test.get("status"),
                "verdict": test.get("verdict"),
                "detail": None,
            }
        )

    return {
        "domain": name,
        "type": domain_type,
        "status": domain.get("status"),
        "measured_at": measured_at,
        "score_percent": scoring.get("percentage"),
        "report_url": report.get("url"),
        "results": entries,
    }


def build_document(reply: dict) -> dict:
    """Turn a completed batch `reply` into a `netnl-findings/v1` document.

    Callers are responsible for only calling this on a `done` batch —
    this function does not check `request.status` itself.
    """
    request = reply.get("request") or {}
    measured_at = request.get("finished_date")
    domain_type = request.get("request_type")
    domains = reply.get("domains") or {}

    domain_blocks = [
        _domain_block(name, domains[name] or {}, domain_type, measured_at)
        for name in sorted(domains)
    ]

    return {"schema": SCHEMA, "domains": domain_blocks}


def render_findings(doc: dict, stream) -> None:
    stream.write(json.dumps(doc, indent=2))
    stream.write("\n")
