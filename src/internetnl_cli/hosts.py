"""Collect the hostnames to submit, from a file and/or positional arguments."""

from __future__ import annotations

from internetnl_cli.errors import ConfigError


def collect_hosts(file: str | None, args: list[str]) -> list[str]:
    hosts: list[str] = []

    if file is not None:
        try:
            with open(file, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except OSError as exc:
            raise ConfigError(f"cannot read hosts file {file}: {exc}") from exc
        for raw_line in lines:
            line = raw_line.split("#", 1)[0].strip()
            if line:
                hosts.append(line)

    hosts.extend(args)

    seen: set[str] = set()
    deduped: list[str] = []
    for host in hosts:
        if host not in seen:
            seen.add(host)
            deduped.append(host)
    return deduped
