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


def reference_from_metadata(metadata: dict, request_type: str | None) -> set[str]:
    """Test names declared by `GET {endpoint}/metadata/report` for `request_type`.

    Defensive by design: any missing or malformed piece of the metadata
    reply simply yields fewer (or no) names rather than raising — the
    caller treats a failed or malformed metadata fetch as a degraded
    render, never a hard failure.
    """
    if not request_type or not isinstance(metadata, dict):
        return set()

    report = metadata.get("report")
    if not isinstance(report, dict):
        return set()

    data = report.get("data")
    hierarchy = report.get("hierarchy")
    if not isinstance(data, dict) or not isinstance(hierarchy, dict):
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
