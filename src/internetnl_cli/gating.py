"""Classify API results into failed / accepted / unknown, and gate on it.

Per design.md: only test status `failed` gates, an allowlisted pair is
reported as accepted, and a test missing for a host renders as `unknown` —
shown, never gating, never passing.

Review round 1 (B2): the reference set — "the subtests the CLI knows
about" — is not just the union of test names observed across hosts in a
single response, it also includes every test name the instance's own
`GET {endpoint}/metadata/report` declares for the run's request type. That
way a subtest the server omitted for *every* host still renders as
`unknown` instead of silently disappearing. `reference_from_metadata` walks
`report.hierarchy.<web|mail>` and keeps only the names that `report.data`
marks `type: "test"`; `evaluate` takes that set as an optional extra
reference to union in.
"""

from __future__ import annotations

from internetnl_cli.errors import ConfigError, GateTripped


def reference_from_metadata(metadata: dict, request_type: str | None) -> set[str] | None:
    """Test names declared by `GET {endpoint}/metadata/report` for `request_type`.

    Returns `None` when the reply is structurally unusable — missing or
    non-dict `report`/`hierarchy`/`data` — so the caller can warn instead of
    silently degrading (review round 2, m4). Returns a set otherwise: empty
    when the reply is structurally fine but simply has no (or few) tests for
    `request_type` — that is a normal, valid outcome, not a degradation.
    """
    if not isinstance(metadata, dict):
        return None

    report = metadata.get("report")
    if not isinstance(report, dict):
        return None

    data = report.get("data")
    hierarchy = report.get("hierarchy")
    if not isinstance(data, dict) or not isinstance(hierarchy, dict):
        return None

    if not request_type:
        return set()

    tree = hierarchy.get(request_type)
    if not isinstance(tree, list):
        return set()

    names: set[str] = set()

    def _walk(items) -> None:
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if isinstance(name, str):
                entry = data.get(name)
                if isinstance(entry, dict) and entry.get("type") == "test":
                    names.add(name)
            children = item.get("children")
            if isinstance(children, list):
                _walk(children)

    _walk(tree)
    return names


def category_by_test_from_metadata(metadata: dict, request_type: str | None) -> dict[str, str] | None:
    """Test name -> category name, from `report.hierarchy.<request_type>`.

    Used by `findings.py` (via `cli.py`, which owns the network call) to
    place a test in its category. `report.hierarchy.<web|mail>` is not a
    flat category -> tests mapping: it is a tree, three layers deep for
    most tests and four for RPKI's nameserver variants (runs/
    03-hierarchie-is-drie-lagen.md) —

        web_ipv6 -> web_ipv6_nameservers -> web_ipv6_ns_address
        web_rpki -> web_ns_rpki          -> web_ns_rpki_exists

    so a test's category is not generally a prefix of the test name
    (`web_ipv6_nameservers` is not a prefix of `web_ipv6_ns_address`) and a
    single-level `children` read (the old behaviour here) or a
    longest-prefix rule both miss most tests. This walks the tree
    recursively instead, exactly like `reference_from_metadata`: every
    top-level entry in the hierarchy list *is* a category (that's what
    "top-level" means in this tree — `report.data` does not reliably mark
    it `type: "category"`, e.g. RPKI's top-level `web_rpki` node is typed
    `"section"` because the name is reused one level down), and every
    descendant `report.data` marks `type: "test"` is recorded under that
    top-level category's name, however deep it sits.

    Returns `None` when the reply is structurally unusable — same rule as
    `reference_from_metadata` — so the caller can warn and fall back
    instead of silently degrading.
    """
    if not isinstance(metadata, dict):
        return None

    report = metadata.get("report")
    if not isinstance(report, dict):
        return None

    data = report.get("data")
    hierarchy = report.get("hierarchy")
    if not isinstance(data, dict) or not isinstance(hierarchy, dict):
        return None

    if not request_type:
        return {}

    tree = hierarchy.get(request_type)
    if not isinstance(tree, list):
        return {}

    categories: dict[str, str] = {}

    def _walk(items, category_name: str) -> None:
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if isinstance(name, str):
                entry = data.get(name)
                if isinstance(entry, dict) and entry.get("type") == "test":
                    categories[name] = category_name
            children = item.get("children")
            if isinstance(children, list):
                _walk(children, category_name)

    for category in tree:
        if not isinstance(category, dict):
            continue
        category_name = category.get("name")
        if isinstance(category_name, str):
            _walk([category], category_name)

    return categories


def parse_allowlist(path) -> set[tuple[str, str]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError as exc:
        raise ConfigError(f"cannot read allowlist file {path}: {exc}") from exc

    entries: set[tuple[str, str]] = set()
    for lineno, raw_line in enumerate(lines, start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) != 2:
            raise ConfigError(f"bad allowlist {path}:{lineno}: expected 'host testname'")
        entries.add((fields[0], fields[1]))
    return entries


def evaluate(
    domains: dict,
    allowlist: set[tuple[str, str]],
    extra_reference: set[str] | None = None,
) -> dict:
    reference: set[str] = set(extra_reference or ())
    for domain in domains.values():
        if domain.get("status") == "ok":
            tests = ((domain.get("results") or {}).get("tests")) or {}
            reference.update(tests.keys())

    failed: list[dict] = []
    accepted: list[dict] = []
    unknown: list[dict] = []

    for host, domain in domains.items():
        if domain.get("status") != "ok":
            continue
        tests = ((domain.get("results") or {}).get("tests")) or {}
        for test_name in sorted(reference):
            if test_name not in tests:
                unknown.append({"host": host, "test": test_name})
                continue
            status = tests[test_name].get("status")
            if status == "failed":
                if (host, test_name) in allowlist:
                    accepted.append({"host": host, "test": test_name})
                else:
                    failed.append({"host": host, "test": test_name})

    return {"failed": failed, "accepted": accepted, "unknown": unknown}


def gate(checks: dict) -> None:
    if checks["failed"]:
        raise GateTripped("--fail-on-scored: one or more scored subtests failed")
