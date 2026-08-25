#!/usr/bin/env sh
# Acceptance test for openspec/changes/add-measurement-api task 4.2:
# run the *unmodified* internetnl CLI against a live netnl facade and
# confirm it behaves exactly as it does against a batch instance directly
# (Requirement "Batch API v2 compatible surface", scenario "The internetnl
# CLI works unchanged"), plus confirm the private instance stays private
# (Requirement "Deployment keeps the instance private").
#
# IMPORTANT: this script only measures $TEST_DOMAIN. Only ever point it at
# a domain you operate or have explicit permission to test — the same
# terms every netnl tenant credential is issued under (see
# docs/how-to/beta.md). It never echoes or logs INTERNETNL_PASSWORD (or
# any other credential); grep this file if in doubt.
#
# Required environment:
#   NETNL_FACADE_URL      https://netnl.<tailnet>.ts.net (or equivalent) —
#                          the facade's public base URL, *without* a
#                          trailing /api/batch/v2 (this script appends the
#                          right endpoint form for INTERNETNL_ENDPOINT).
#   INTERNETNL_USERNAME    a tenant credential issued via `netnl-admin
#                          user add` (see docs/how-to/beta.md).
#   INTERNETNL_PASSWORD    that credential's password.
#   TEST_DOMAIN            a domain you (the operator running this script)
#                          control. Measured for real by the upstream
#                          instance — do not point this at a domain you do
#                          not operate or have permission to test.
#
# Optional environment:
#   NETNL_INSTANCE_PROBE_URL   the upstream batch instance's own address
#                          (e.g. its VPS public IP:port), to confirm it is
#                          NOT publicly reachable. This check is only
#                          meaningful when run from a host outside the
#                          instance's tailnet; see the note below if this
#                          host happens to already be on that tailnet.
#   ACCEPTANCE_RESULTS_MAX_WAIT_SECONDS   how long to keep polling
#                          `results` for completion (default 1800 = 30m).
#   ACCEPTANCE_RESULTS_POLL_INTERVAL_SECONDS   seconds between `results`
#                          polls (default 30).
#   ACCEPTANCE_DEMO_POLL   set to 1 to also exercise `internetnl poll`
#                          once (bounded by ACCEPTANCE_POLL_MAX_SECONDS)
#                          as a smoke test of the poll subcommand itself.
#                          Skipped by default: a real batch run usually
#                          takes longer than a short demo window, and step
#                          3 below already exercises `results` polling to
#                          completion.
#   ACCEPTANCE_POLL_MAX_SECONDS   bound for the ACCEPTANCE_DEMO_POLL
#                          step (default 60).
#
# Exit status: 0 if every check passes, non-zero otherwise.

set -eu

_pass() {
    printf 'PASS: %s\n' "$1"
}

_fail() {
    printf 'FAIL: %s\n' "$1" >&2
    exit 1
}

_note() {
    printf 'NOTE: %s\n' "$1"
}

# --- Required environment ---------------------------------------------------

_require_var() {
    # $1 = variable name, $2 = human-readable hint. Never prints the
    # variable's value (it may be a credential).
    eval "value=\${$1:-}"
    if [ -z "$value" ]; then
        _fail "$1 is not set: $2"
    fi
}

value=""
_require_var NETNL_FACADE_URL "set it to the facade public base URL"
_require_var INTERNETNL_USERNAME "set it to an issued tenant credential name"
_require_var INTERNETNL_PASSWORD "set it to that tenant credential password"
_require_var TEST_DOMAIN "set it to a domain you operate or have permission to test"
unset value

RESULTS_MAX_WAIT_SECONDS="${ACCEPTANCE_RESULTS_MAX_WAIT_SECONDS:-1800}"
RESULTS_POLL_INTERVAL_SECONDS="${ACCEPTANCE_RESULTS_POLL_INTERVAL_SECONDS:-30}"
DEMO_POLL="${ACCEPTANCE_DEMO_POLL:-0}"
POLL_MAX_SECONDS="${ACCEPTANCE_POLL_MAX_SECONDS:-60}"

# The CLI's own endpoint variable, per docs/how-to/deploy-facade.md step 5
# ("Acceptance check") and the CLI's --json contract (design.md: submit,
# poll and results are unchanged when only INTERNETNL_ENDPOINT/USERNAME/
# PASSWORD point at the facade instead of a batch instance).
INTERNETNL_ENDPOINT="$NETNL_FACADE_URL"
export INTERNETNL_ENDPOINT INTERNETNL_USERNAME INTERNETNL_PASSWORD

cd "$(dirname "$0")/.."

REQUEST_ID_RE='^[a-f0-9]{32}$'

_json_valid() {
    # Reads one JSON document on stdin, exits 0 if it parses, non-zero
    # otherwise. Uses `uv run python` so no jq dependency is required.
    uv run python -c 'import json, sys; json.load(sys.stdin)'
}

echo "=== netnl acceptance test (task 4.2) ==="
echo "facade:      $NETNL_FACADE_URL"
echo "test domain: $TEST_DOMAIN"
echo

# --- Step 1: submit ----------------------------------------------------------
#
# NOTE on --json + --no-poll: the CLI only ever renders a JSON document via
# the `results`/`poll` code path (internetnl_cli/cli.py's _render()); a
# `submit --no-poll` call returns immediately after registering the run and
# writes nothing to stdout at all (stdout == "" — see
# tests/test_cli.py::test_submit_no_poll_makes_exactly_one_call), only the
# request id on stderr (`request-id: <id>`). That is the CLI's real,
# pinned behaviour, not a defect in this script: --json is still passed
# below for consistency with how a tenant would normally invoke submit,
# but this step's assertions follow the actual contract — request id on
# stderr, empty stdout — rather than expecting JSON on stdout.
submit_stdout="$(mktemp)"
submit_stderr="$(mktemp)"
trap 'rm -f "$submit_stdout" "$submit_stderr"' EXIT

submit_exit=0
uv run internetnl submit "$TEST_DOMAIN" --no-poll --json \
    >"$submit_stdout" 2>"$submit_stderr" || submit_exit=$?

if [ "$submit_exit" -ne 0 ]; then
    echo "--- submit stderr ---" >&2
    cat "$submit_stderr" >&2
    _fail "submit exited $submit_exit (expected 0)"
fi
_pass "submit exited 0"

if [ -s "$submit_stdout" ]; then
    _fail "submit --no-poll wrote to stdout unexpectedly (expected empty stdout; the CLI's request-id line belongs on stderr): $(cat "$submit_stdout")"
fi
_pass "submit --no-poll wrote nothing to stdout (matches the pinned CLI contract)"

request_id="$(sed -n 's/^request-id: //p' "$submit_stderr" | head -n1)"
if [ -z "$request_id" ]; then
    echo "--- submit stderr ---" >&2
    cat "$submit_stderr" >&2
    _fail "could not find a 'request-id: ...' line on submit's stderr"
fi

if ! printf '%s' "$request_id" | grep -Eq "$REQUEST_ID_RE"; then
    _fail "request id '$request_id' is not 32 lowercase hex characters (facade ids must match ^[a-f0-9]{32}\$)"
fi
_pass "submit returned a facade request id: $request_id"
echo

# --- Step 2: poll (optional smoke test) --------------------------------------

if [ "$DEMO_POLL" = "1" ]; then
    poll_stdout="$(mktemp)"
    poll_stderr="$(mktemp)"
    poll_exit=0
    INTERNETNL_POLL_MAX="$POLL_MAX_SECONDS" \
        uv run internetnl poll "$request_id" --json \
        >"$poll_stdout" 2>"$poll_stderr" || poll_exit=$?

    case "$poll_exit" in
        0)
            _pass "poll finished within ${POLL_MAX_SECONDS}s and exited 0"
            if ! _json_valid <"$poll_stdout"; then
                _fail "poll --json stdout is not one valid JSON document"
            fi
            _pass "poll --json stdout is one valid JSON document"
            ;;
        4)
            _note "poll did not finish within ${POLL_MAX_SECONDS}s (exit 4 = INTERNETNL_POLL_MAX exceeded) — this is expected for a real batch run and is not a failure; step 3 below polls 'results' to completion with a longer budget"
            ;;
        *)
            echo "--- poll stderr ---" >&2
            cat "$poll_stderr" >&2
            _fail "poll exited $poll_exit (expected 0 or 4)"
            ;;
    esac
    rm -f "$poll_stdout" "$poll_stderr"
    echo
else
    _note "ACCEPTANCE_DEMO_POLL not set to 1: skipping the 'internetnl poll' smoke test (step 3 already polls 'results' to completion)"
    echo
fi

# --- Step 3: results, polled to completion -----------------------------------

elapsed=0
status="unknown"
results_stdout="$(mktemp)"
results_stderr="$(mktemp)"
trap 'rm -f "$submit_stdout" "$submit_stderr" "$results_stdout" "$results_stderr"' EXIT

while [ "$elapsed" -lt "$RESULTS_MAX_WAIT_SECONDS" ]; do
    results_exit=0
    uv run internetnl results "$request_id" --json \
        >"$results_stdout" 2>"$results_stderr" || results_exit=$?

    if [ "$results_exit" -ne 0 ]; then
        echo "--- results stderr ---" >&2
        cat "$results_stderr" >&2
        _fail "results exited $results_exit (run likely ended as error/cancelled, or another API error)"
    fi

    if ! _json_valid <"$results_stdout"; then
        _fail "results --json stdout is not one valid JSON document"
    fi

    status="$(uv run python -c '
import json, sys
doc = json.load(open(sys.argv[1]))
request = doc.get("request") or {}
print(request.get("status", "unknown"))
' "$results_stdout")"

    if [ "$status" = "done" ]; then
        break
    fi

    printf 'waiting: status=%s (elapsed=%ss/%ss)\n' "$status" "$elapsed" "$RESULTS_MAX_WAIT_SECONDS"
    sleep "$RESULTS_POLL_INTERVAL_SECONDS"
    elapsed=$((elapsed + RESULTS_POLL_INTERVAL_SECONDS))
done

if [ "$status" != "done" ]; then
    _fail "run $request_id did not reach status 'done' within ${RESULTS_MAX_WAIT_SECONDS}s (last status: $status)"
fi
_pass "results exited 0 with a valid JSON document once the run finished"

uv run python -c '
import json, sys
doc = json.load(open(sys.argv[1]))
missing = [k for k in ("endpoint", "timestamp", "api_version", "request_id", "domains") if k not in doc]
if missing:
    sys.exit("missing keys in results document: " + ", ".join(missing))
if doc["domains"] is None:
    sys.exit("results document has domains=null even though status is done")
' "$results_stdout"
_pass "results document has endpoint/timestamp/api_version/request_id/domains"
echo

# --- Step 4: the instance must not be publicly reachable ---------------------

if [ -n "${NETNL_INSTANCE_PROBE_URL:-}" ]; then
    if curl --silent --show-error --max-time 5 --output /dev/null "$NETNL_INSTANCE_PROBE_URL" 2>/dev/null; then
        _fail "the upstream instance answered at $NETNL_INSTANCE_PROBE_URL — it must not be publicly reachable; only the facade may answer publicly"
    fi
    _pass "the upstream instance did not answer at $NETNL_INSTANCE_PROBE_URL (connection refused/timeout, as expected)"
else
    _note "NETNL_INSTANCE_PROBE_URL not set: skipping the 'instance not public' probe. Run this check from a host OUTSIDE the instance's tailnet (this script cannot tell from inside the tailnet whether the instance would also answer from the public internet)."
fi
echo

echo "=== all checks passed ==="
