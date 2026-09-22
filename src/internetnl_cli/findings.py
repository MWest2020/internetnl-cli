"""Build the `netnl-findings/v1` export document from a completed batch reply.

This is a pure transform of the shape `BatchClient.results()` returns (the
same `reply` dict `render.build_document` consumes): `request` for
`finished_date`/`request_type`/`request_id`, `api_version` and `domains`
for the per-domain payload. No network I/O, no aggregation, no scoring of
its own — see proposal.md's "Why" for why that judgement stays with the
API. `generated_at` and `endpoint_host` are the two pieces of the header
this module cannot derive from `reply` alone (the write time, and the
config-resolved endpoint); the caller (`cli.py`) supplies both.

Categorising a test does need the instance's `GET /metadata/report`
hierarchy (see runs/02-categorie-uit-metadata.md and runs/
03-hierarchie-is-drie-lagen.md): the hierarchy is a tree three (or four)
layers deep, so a test's category is generally not derivable from the test
name at all — the instance has to say it explicitly. Fetching that
hierarchy is I/O, so it stays the caller's job (`cli.py`); this module only
takes the already-parsed `categories_by_test` mapping (test name ->
category name, from `gating.category_by_test_from_metadata`) as a plain
argument and does an exact lookup. Without one (metadata unavailable or
unusable), it falls back to the old rule: the longest key in a domain's
own `results.categories` that prefixes the test name — worse (it misses
infixed subtestgroups like RPKI's nameserver variants) but requires no
network, and the caller warns before choosing it.
"""

from __future__ import annotations

import json
from datetime import datetime

SCHEMA = "netnl-findings/v1"


def _api_label(api_version: str | None) -> str:
    """`"internet.nl batch v2"` from an `api_version` like `"2.7.0"`.

    Only the major component is stable across patch releases of the same
    batch API generation; the contract pins the fixed "internet.nl batch v"
    prefix plus that component, not the full semver string.
    """
    major = (api_version or "unknown").split(".", 1)[0]
    return f"internet.nl batch v{major}"


def _category_for(test_name: str, categories: dict, categories_by_test: dict[str, str] | None) -> str | None:
    """The category for `test_name`.

    With `categories_by_test` (test name -> category name, from the
    instance's metadata hierarchy): an exact lookup — the instance says
    directly which category `test_name` belongs to. Without it: the
    longest key in `categories` that prefixes `test_name` (the fallback
    rule).
    """
    if categories_by_test is not None:
        return categories_by_test.get(test_name)

    best: str | None = None
    for key in categories:
        if test_name.startswith(key) and (best is None or len(key) > len(best)):
            best = key
    return best


def _domain_block(
    name: str,
    domain: dict,
    domain_type: str | None,
    measured_at: str | None,
    categories_by_test: dict[str, str] | None,
) -> dict:
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
                "category": _category_for(test_name, categories, categories_by_test),
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


def build_document(
    reply: dict,
    *,
    generated_at: datetime,
    endpoint_host: str,
    categories_by_test: dict[str, str] | None = None,
) -> dict:
    """Turn a completed batch `reply` into a `netnl-findings/v1` document.

    Callers are responsible for only calling this on a `done` batch —
    this function does not check `request.status` itself. `categories_by_test`
    is the test-name -> category-name mapping the caller derived from
    `GET /metadata/report` (see module docstring); `None` triggers the
    testname-prefix fallback.

    `generated_at` (the moment the file is written) and `endpoint_host` (the
    hostname only, never a URL with credentials) are the caller's job to
    supply — see docs/netnl-findings-v1.md's Traceability requirement. This
    function does no network I/O and never reads the clock itself, so the
    same reply with the same `generated_at` always produces the same bytes.
    """
    request = reply.get("request") or {}
    measured_at = request.get("finished_date")
    domain_type = request.get("request_type")
    domains = reply.get("domains") or {}

    domain_blocks = [
        _domain_block(name, domains[name] or {}, domain_type, measured_at, categories_by_test)
        for name in sorted(domains)
    ]

    return {
        "schema": SCHEMA,
        "generated_at": generated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": {
            "api": _api_label(reply.get("api_version")),
            "endpoint": endpoint_host,
            "request_id": request.get("request_id"),
        },
        "domains": domain_blocks,
    }


def render_findings(doc: dict, stream) -> None:
    stream.write(json.dumps(doc, indent=2))
    stream.write("\n")
