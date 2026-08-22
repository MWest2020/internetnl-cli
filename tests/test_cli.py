import base64
import copy
import io
import json
import urllib.error

import pytest

from internetnl_cli.cli import main
from internetnl_cli.client import HttpResponse
from fakes import (
    FakeOpener,
    METADATA_REPLY,
    RESULTS_REPLY,
    REGISTER_REPLY,
    STATUS_RUNNING,
    STATUS_DONE,
    raising_opener,
    REQUEST_ID,
)


ENDPOINT = "https://batch.example/api/batch/v2"


def _ok(payload):
    return HttpResponse(status=200, body=json.dumps(payload).encode())


def _run(argv, opener, monkeypatch, endpoint=ENDPOINT, extra_env=None):
    monkeypatch.setenv("INTERNETNL_ENDPOINT", endpoint)
    for key, value in (extra_env or {}).items():
        monkeypatch.setenv(key, value)
    calls = []
    stdout = io.StringIO()
    stderr = io.StringIO()

    def fake_sleep(seconds):
        calls.append(seconds)

    exit_code = main(argv, opener=opener, sleep=fake_sleep, stdout=stdout, stderr=stderr)
    return exit_code, stdout.getvalue(), stderr.getvalue(), calls


def test_submit_file_and_positional_dedup_order(tmp_path, monkeypatch):
    hosts_file = tmp_path / "hosts.txt"
    hosts_file.write_text("a.example\n# comment\nb.example\n")
    opener = FakeOpener([_ok(REGISTER_REPLY), _ok(STATUS_DONE), _ok(RESULTS_REPLY)])
    exit_code, stdout, stderr, _ = _run(
        ["submit", "--file", str(hosts_file), "b.example", "c.example", "--no-poll"],
        opener,
        monkeypatch,
    )
    payload = json.loads(opener.calls[0][2])
    assert payload["domains"] == ["a.example", "b.example", "c.example"]
    assert stderr.startswith(f"request-id: {REQUEST_ID}\n")
    assert exit_code == 0


def test_submit_no_poll_makes_exactly_one_call(monkeypatch):
    opener = FakeOpener([_ok(REGISTER_REPLY)])
    exit_code, stdout, stderr, _ = _run(
        ["submit", "example.nl", "--no-poll"], opener, monkeypatch
    )
    assert exit_code == 0
    assert len(opener.calls) == 1
    assert stderr == f"request-id: {REQUEST_ID}\n"
    assert stdout == ""


def test_submit_without_no_poll_polls_and_renders(monkeypatch):
    opener = FakeOpener(
        [
            _ok(REGISTER_REPLY),
            _ok(STATUS_RUNNING),
            _ok(STATUS_DONE),
            _ok(RESULTS_REPLY),
            _ok(METADATA_REPLY),
        ]
    )
    exit_code, stdout, stderr, sleeps = _run(["submit", "example.nl"], opener, monkeypatch)
    assert exit_code == 0
    assert f"request-id: {REQUEST_ID}\n" in stderr
    assert sleeps == [30]
    assert "example.nl" in stdout


def test_poll_resumes_a_run_this_process_did_not_submit(monkeypatch):
    opener = FakeOpener([_ok(STATUS_DONE), _ok(RESULTS_REPLY), _ok(METADATA_REPLY)])
    exit_code, stdout, stderr, _ = _run(["poll", REQUEST_ID], opener, monkeypatch)
    assert exit_code == 0
    doc = None
    # table output, not json - just check the endpoint/host appear
    assert "example.nl" in stdout
    assert "batch.example" in stdout


def test_results_on_running_run_exits_zero_no_rows(monkeypatch):
    opener = FakeOpener([_ok(STATUS_RUNNING)])
    exit_code, stdout, stderr, _ = _run(["results", REQUEST_ID], opener, monkeypatch)
    assert exit_code == 0
    assert stdout == ""
    assert "status: running" in stderr


def test_results_on_running_run_with_json_has_null_domains(monkeypatch):
    opener = FakeOpener([_ok(STATUS_RUNNING)])
    exit_code, stdout, stderr, _ = _run(["results", REQUEST_ID, "--json"], opener, monkeypatch)
    assert exit_code == 0
    doc = json.loads(stdout)
    assert doc["domains"] is None
    assert doc["checks"] is None


def test_json_on_finished_run_is_single_document_progress_on_stderr(monkeypatch):
    opener = FakeOpener([_ok(STATUS_DONE), _ok(RESULTS_REPLY), _ok(METADATA_REPLY)])
    exit_code, stdout, stderr, _ = _run(["poll", REQUEST_ID, "--json"], opener, monkeypatch)
    assert exit_code == 0
    doc = json.loads(stdout)  # must be exactly one document
    assert doc["request_id"] == REQUEST_ID
    assert "request-id" not in stdout


def test_fail_on_scored_trips_exit_3(monkeypatch):
    opener = FakeOpener([_ok(STATUS_DONE), _ok(RESULTS_REPLY), _ok(METADATA_REPLY)])
    exit_code, stdout, stderr, _ = _run(
        ["poll", REQUEST_ID, "--fail-on-scored"], opener, monkeypatch
    )
    assert exit_code == 3
    assert "example.nl web_dnssec_exist" in stdout


def test_fail_on_scored_with_allowlisted_pair_exits_zero(tmp_path, monkeypatch):
    allowlist = tmp_path / "allow.txt"
    allowlist.write_text("example.nl web_dnssec_exist\n")
    opener = FakeOpener([_ok(STATUS_DONE), _ok(RESULTS_REPLY), _ok(METADATA_REPLY)])
    exit_code, stdout, stderr, _ = _run(
        ["poll", REQUEST_ID, "--fail-on-scored", "--allowlist", str(allowlist)],
        opener,
        monkeypatch,
    )
    assert exit_code == 0
    assert "accepted:" in stdout
    assert "example.nl web_dnssec_exist" in stdout


def test_bad_allowlist_file_exits_one(tmp_path, monkeypatch):
    allowlist = tmp_path / "allow.txt"
    allowlist.write_text("only-one-field\n")
    opener = FakeOpener([_ok(STATUS_DONE), _ok(RESULTS_REPLY)])
    exit_code, stdout, stderr, _ = _run(
        ["poll", REQUEST_ID, "--allowlist", str(allowlist)], opener, monkeypatch
    )
    assert exit_code == 1


def test_unreadable_hosts_file_exits_one(monkeypatch):
    opener = FakeOpener([])
    exit_code, stdout, stderr, _ = _run(
        ["submit", "--file", "/no/such/file.txt"], opener, monkeypatch
    )
    assert exit_code == 1
    assert opener.calls == []


def test_no_hosts_exits_two(monkeypatch):
    opener = FakeOpener([])
    exit_code, stdout, stderr, _ = _run(["submit"], opener, monkeypatch)
    assert exit_code == 2
    assert "no hosts given" in stderr


def test_500_reply_exits_two_with_status_and_host_no_rows(monkeypatch):
    opener = FakeOpener(
        [HttpResponse(status=500, body=json.dumps({"error": {"label": "server-error", "msg": "boom"}}).encode())]
    )
    exit_code, stdout, stderr, _ = _run(["results", REQUEST_ID], opener, monkeypatch)
    assert exit_code == 2
    assert "500" in stderr
    assert "batch.example" in stderr
    assert stdout == ""


def test_urlerror_exits_two_with_no_rows(monkeypatch):
    opener = raising_opener(urllib.error.URLError("connection refused"))
    exit_code, stdout, stderr, _ = _run(["results", REQUEST_ID], opener, monkeypatch)
    assert exit_code == 2
    assert stdout == ""


def test_argparse_rejects_credential_arguments():
    with pytest.raises(SystemExit) as excinfo:
        main(["submit", "--password", "x"])
    assert excinfo.value.code == 2


def test_switching_instances_changes_nothing_but_endpoint(monkeypatch):
    opener_a = FakeOpener([_ok(STATUS_DONE), _ok(RESULTS_REPLY), _ok(METADATA_REPLY)])
    exit_a, stdout_a, _, _ = _run(
        ["poll", REQUEST_ID, "--json"], opener_a, monkeypatch, endpoint="https://hosted.example/api/batch/v2"
    )
    opener_b = FakeOpener([_ok(STATUS_DONE), _ok(RESULTS_REPLY), _ok(METADATA_REPLY)])
    exit_b, stdout_b, _, _ = _run(
        ["poll", REQUEST_ID, "--json"], opener_b, monkeypatch, endpoint="https://selfhosted.example/api/batch/v2"
    )
    doc_a = json.loads(stdout_a)
    doc_b = json.loads(stdout_b)
    assert exit_a == exit_b == 0
    assert doc_a["endpoint"] != doc_b["endpoint"]
    doc_a["endpoint"] = None
    doc_b["endpoint"] = None
    assert doc_a == doc_b


# --- B1: redirects are never followed -------------------------------------


def test_302_reply_maps_to_api_error_exit_two(monkeypatch):
    opener = FakeOpener([HttpResponse(status=302, body=b"")])
    exit_code, stdout, stderr, _ = _run(["results", REQUEST_ID], opener, monkeypatch)
    assert exit_code == 2
    assert stdout == ""


# --- B2: metadata reference set --------------------------------------------


def test_metadata_test_missing_for_every_host_renders_unknown(monkeypatch):
    metadata = copy.deepcopy(METADATA_REPLY)
    metadata["report"]["data"]["web_never_seen"] = {
        "type": "test",
        "translation_key": "t",
        "status_verdict_map": {},
    }
    metadata["report"]["hierarchy"]["web"].append({"name": "web_never_seen"})
    opener = FakeOpener([_ok(STATUS_DONE), _ok(RESULTS_REPLY), _ok(metadata)])
    exit_code, stdout, stderr, _ = _run(["poll", REQUEST_ID], opener, monkeypatch)
    assert exit_code == 0
    assert "example.nl web_never_seen" in stdout
    lines = stdout.splitlines()
    row = [line for line in lines if line.startswith("example.nl ")][0]
    columns = row.split()
    # HOST STATUS SCORE FAILED WARNING INFO ERROR UNKNOWN
    assert columns[-1] == "1"


def test_metadata_unavailable_warns_and_continues(monkeypatch):
    opener = FakeOpener(
        [
            _ok(STATUS_DONE),
            _ok(RESULTS_REPLY),
            HttpResponse(status=500, body=json.dumps({"error": {"label": "x", "msg": "y"}}).encode()),
        ]
    )
    exit_code, stdout, stderr, _ = _run(["poll", REQUEST_ID], opener, monkeypatch)
    assert exit_code == 0
    assert "warning: metadata unavailable" in stderr
    assert "example.nl" in stdout


# --- B3: `results` on error/cancelled/unknown status -----------------------


def test_results_on_error_status_exits_two(monkeypatch):
    reply = copy.deepcopy(STATUS_RUNNING)
    reply["request"]["status"] = "error"
    opener = FakeOpener([_ok(reply)])
    exit_code, stdout, stderr, _ = _run(["results", REQUEST_ID], opener, monkeypatch)
    assert exit_code == 2
    assert stdout == ""


def test_results_on_cancelled_status_exits_two(monkeypatch):
    reply = copy.deepcopy(STATUS_RUNNING)
    reply["request"]["status"] = "cancelled"
    opener = FakeOpener([_ok(reply)])
    exit_code, stdout, stderr, _ = _run(["results", REQUEST_ID], opener, monkeypatch)
    assert exit_code == 2
    assert stdout == ""


def test_results_on_unrecognised_status_exits_two(monkeypatch):
    reply = copy.deepcopy(STATUS_RUNNING)
    reply["request"]["status"] = "something-weird"
    opener = FakeOpener([_ok(reply)])
    exit_code, stdout, stderr, _ = _run(["results", REQUEST_ID], opener, monkeypatch)
    assert exit_code == 2
    assert stdout == ""


# --- M1: http endpoints require an explicit opt-in --------------------------


def test_http_endpoint_without_opt_in_exits_one(monkeypatch):
    opener = FakeOpener([])
    exit_code, stdout, stderr, _ = _run(
        ["results", REQUEST_ID], opener, monkeypatch, endpoint="http://batch.example/api/batch/v2"
    )
    assert exit_code == 1
    assert opener.calls == []
    assert "INTERNETNL_ALLOW_HTTP" in stderr


def test_http_endpoint_with_opt_in_works(monkeypatch):
    opener = FakeOpener([_ok(STATUS_RUNNING)])
    exit_code, stdout, stderr, _ = _run(
        ["results", REQUEST_ID],
        opener,
        monkeypatch,
        endpoint="http://batch.example/api/batch/v2",
        extra_env={"INTERNETNL_ALLOW_HTTP": "1"},
    )
    assert exit_code == 0


# --- M2: request_id validation ----------------------------------------------


def test_poll_path_traversal_request_id_rejected_before_any_http_call(monkeypatch):
    opener = FakeOpener([])
    exit_code, stdout, stderr, _ = _run(["poll", "../../../admin"], opener, monkeypatch)
    assert exit_code == 2
    assert opener.calls == []


def test_poll_request_id_with_control_characters_exits_cleanly(monkeypatch):
    opener = FakeOpener([])
    exit_code, stdout, stderr, _ = _run(["poll", "x\r\n"], opener, monkeypatch)
    assert exit_code == 2
    assert opener.calls == []


def test_results_invalid_request_id_rejected_before_any_http_call(monkeypatch):
    opener = FakeOpener([])
    exit_code, stdout, stderr, _ = _run(["results", "not-a-valid-id"], opener, monkeypatch)
    assert exit_code == 2
    assert opener.calls == []


def test_submit_reply_with_malformed_request_id_is_api_error(monkeypatch):
    bad_reply = copy.deepcopy(REGISTER_REPLY)
    bad_reply["request"] = dict(bad_reply["request"])
    bad_reply["request"]["request_id"] = "not-a-valid-id"
    opener = FakeOpener([_ok(bad_reply)])
    exit_code, stdout, stderr, _ = _run(
        ["submit", "example.nl", "--no-poll"], opener, monkeypatch
    )
    assert exit_code == 2


# --- M3: environment tunables proven end-to-end -----------------------------


def test_poll_interval_env_var_used_for_sleep(monkeypatch):
    opener = FakeOpener([_ok(STATUS_RUNNING), _ok(STATUS_DONE), _ok(RESULTS_REPLY), _ok(METADATA_REPLY)])
    exit_code, stdout, stderr, sleeps = _run(
        ["poll", REQUEST_ID], opener, monkeypatch, extra_env={"INTERNETNL_POLL_INTERVAL": "5"}
    )
    assert exit_code == 0
    assert sleeps == [5.0]


def test_poll_max_env_var_triggers_exit_four(monkeypatch):
    opener = FakeOpener([_ok(STATUS_RUNNING)])
    exit_code, stdout, stderr, sleeps = _run(
        ["poll", REQUEST_ID],
        opener,
        monkeypatch,
        extra_env={"INTERNETNL_POLL_MAX": "0", "INTERNETNL_POLL_INTERVAL": "30"},
    )
    assert exit_code == 4


def test_timeout_env_var_reaches_opener_seam(monkeypatch):
    opener = FakeOpener([_ok(STATUS_DONE), _ok(RESULTS_REPLY), _ok(METADATA_REPLY)])
    exit_code, stdout, stderr, _ = _run(
        ["poll", REQUEST_ID], opener, monkeypatch, extra_env={"INTERNETNL_TIMEOUT": "7"}
    )
    assert exit_code == 0
    timeouts = {call[4] for call in opener.calls}
    assert timeouts == {7.0}


def test_batch_size_env_var_caps_submit(monkeypatch):
    opener = FakeOpener([])
    exit_code, stdout, stderr, _ = _run(
        ["submit", "a.example", "b.example", "c.example", "--no-poll"],
        opener,
        monkeypatch,
        extra_env={"INTERNETNL_BATCH_SIZE": "2"},
    )
    assert exit_code == 2
    assert "INTERNETNL_BATCH_SIZE" in stderr
    assert opener.calls == []


# --- m7: no-network + credential-leak, proven CLI-wide ----------------------


def test_no_endpoint_configured_never_calls_opener():
    opener = FakeOpener([])
    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = main(
        ["poll", REQUEST_ID], opener=opener, sleep=lambda s: None, stdout=stdout, stderr=stderr
    )
    assert exit_code == 1
    assert opener.calls == []


def test_credential_leak_via_main_with_debug_and_401_and_transport_failure(monkeypatch):
    secret = "cli-s3cr3t"
    opener = FakeOpener(
        [HttpResponse(status=401, body=json.dumps({"error": {"label": "x", "msg": "y"}}).encode())]
    )
    exit_code, stdout, stderr, _ = _run(
        ["--debug", "results", REQUEST_ID],
        opener,
        monkeypatch,
        extra_env={"INTERNETNL_USERNAME": "alice", "INTERNETNL_PASSWORD": secret},
    )
    encoded = base64.b64encode(f"alice:{secret}".encode()).decode()
    assert secret not in stdout
    assert secret not in stderr
    assert encoded not in stdout
    assert encoded not in stderr

    opener2 = raising_opener(urllib.error.URLError("boom"))
    exit_code2, stdout2, stderr2, _ = _run(
        ["--debug", "results", REQUEST_ID],
        opener2,
        monkeypatch,
        extra_env={"INTERNETNL_USERNAME": "alice", "INTERNETNL_PASSWORD": secret},
    )
    assert secret not in stdout2
    assert secret not in stderr2
