#!/usr/bin/env sh
# Habitat Stop-gate: a builder run cannot end while this script fails.
# Runs from the base commit, so the builder cannot disarm it.
set -eu
cd "$(dirname "$0")/.."

if [ -f pyproject.toml ]; then
    uv run pytest -q
else
    echo "verify: no pyproject.toml yet - nothing to test" >&2
fi

if command -v shellcheck >/dev/null 2>&1; then
    shellcheck scripts/*.sh
fi
