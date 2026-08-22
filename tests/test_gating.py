import copy
import io
from datetime import datetime, timezone

import pytest

from internetnl_cli.errors import ConfigError, GateTripped
from internetnl_cli.gating import evaluate, gate, parse_allowlist
from internetnl_cli.render import build_document, render_table
from fakes import RESULTS_REPLY, REQUEST_ID


def test_failed_test_produces_failed_entry_and_gate_trips():
    checks = evaluate(RESULTS_REPLY["domains"], set())
    assert {"host": "example.nl", "test": "web_dnssec_exist"} in checks["failed"]
    with pytest.raises(GateTripped) as excinfo:
        gate(checks)
    assert excinfo.value.exit_code == 3


def test_allowlisted_pair_moves_to_accepted_and_gate_passes():
    allowlist = {("example.nl", "web_dnssec_exist")}
    checks = evaluate(RESULTS_REPLY["domains"], allowlist)
    assert checks["failed"] == []
    assert {"host": "example.nl", "test": "web_dnssec_exist"} in checks["accepted"]
    gate(checks)  # must not raise


def test_informational_statuses_never_gate_but_are_shown():
    reply = copy.deepcopy(RESULTS_REPLY)
    tests = reply["domains"]["example.nl"]["results"]["tests"]
    # keep only informational / passed statuses, no failed
    tests.pop("web_dnssec_exist")
    checks = evaluate(reply["domains"], set())
    assert checks["failed"] == []
    gate(checks)  # must not raise

    doc = build_document("batch.example", REQUEST_ID, reply, datetime.now(timezone.utc), checks)
    stream = io.StringIO()
    render_table(doc, stream)
    output = stream.getvalue()
    assert "web_https_hsts warning" in output
    assert "web_appsecpriv_csp info" in output


def test_missing_subtest_is_unknown_never_passing():
    # RESULTS_REPLY only has one "ok" host, so the reference set (the union
    # of test names across "ok" hosts) is built from it alone; deleting a
    # test there would remove it from the reference set entirely rather
    # than leaving it "missing for this host". A second "ok" host that
    # keeps the subtest is added here (a working copy, RESULTS_REPLY
    # itself is untouched) so the reference set still contains it and the
    # missing-from-one-host semantics can actually be exercised.
    reply = copy.deepcopy(RESULTS_REPLY)
    reply["domains"]["another.nl"] = copy.deepcopy(reply["domains"]["example.nl"])
    del reply["domains"]["example.nl"]["results"]["tests"]["web_dnssec_exist"]
    checks = evaluate(reply["domains"], set())
    assert {"host": "example.nl", "test": "web_dnssec_exist"} in checks["unknown"]
    assert {"host": "example.nl", "test": "web_dnssec_exist"} not in checks["failed"]

    doc = build_document("batch.example", REQUEST_ID, reply, datetime.now(timezone.utc), checks)
    stream = io.StringIO()
    render_table(doc, stream)
    output = stream.getvalue()
    assert "unknown:" in output
    assert "example.nl web_dnssec_exist" in output
    lines = output.splitlines()
    row = [line for line in lines if line.startswith("example.nl ")][0]
    columns = row.split()
    # HOST STATUS SCORE FAILED WARNING INFO ERROR UNKNOWN
    assert columns[-1] == "1"


def test_allowlist_parsing_comments_blank_lines_whitespace(tmp_path):
    allowlist_file = tmp_path / "allow.txt"
    allowlist_file.write_text(
        "\n"
        "# a comment\n"
        "  example.nl   web_dnssec_exist  # trailing comment\n"
        "\n"
        "other.nl web_https_hsts\n"
    )
    entries = parse_allowlist(allowlist_file)
    assert entries == {("example.nl", "web_dnssec_exist"), ("other.nl", "web_https_hsts")}


def test_malformed_allowlist_line_raises_config_error_with_line_number(tmp_path):
    allowlist_file = tmp_path / "allow.txt"
    allowlist_file.write_text("example.nl web_dnssec_exist\nbad-line-only-one-field\n")
    with pytest.raises(ConfigError) as excinfo:
        parse_allowlist(allowlist_file)
    assert ":2" in str(excinfo.value)


def test_unreadable_allowlist_is_config_error(tmp_path):
    with pytest.raises(ConfigError):
        parse_allowlist(tmp_path / "does-not-exist.txt")


def test_errored_domain_contributes_no_entries():
    checks = evaluate(RESULTS_REPLY["domains"], set())
    for section in ("failed", "accepted", "unknown"):
        assert all(entry["host"] != "broken.nl" for entry in checks[section])
