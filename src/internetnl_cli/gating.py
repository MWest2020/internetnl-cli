"""Classify API results into failed / accepted / unknown, and gate on it.

Per design.md: only test status `failed` gates, an allowlisted pair is
reported as accepted, and a test missing for a host renders as `unknown` —
shown, never gating, never passing.
"""

from __future__ import annotations

from internetnl_cli.errors import ConfigError, GateTripped


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


def evaluate(domains: dict, allowlist: set[tuple[str, str]]) -> dict:
    reference: set[str] = set()
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
