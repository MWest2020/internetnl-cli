import io
import json
import urllib.error

import pytest

from internetnl_cli.cli import main
from internetnl_cli.client import HttpResponse
from fakes import FakeOpener, RESULTS_REPLY, REGISTER_REPLY, STATUS_RUNNING, STATUS_DONE, raising_opener, REQUEST_ID


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
        [_ok(REGISTER_REPLY), _ok(STATUS_RUNNING), _ok(STATUS_DONE), _ok(RESULTS_REPLY)]
    )
    exit_code, stdout, stderr, sleeps = _run(["submit", "example.nl"], opener, monkeypatch)
    assert exit_code == 0
    assert f"request-id: {REQUEST_ID}\n" in stderr
    assert sleeps == [30]
    assert "example.nl" in stdout


def test_poll_resumes_a_run_this_process_did_not_submit(monkeypatch):
    opener = FakeOpener([_ok(STATUS_DONE), _ok(RESULTS_REPLY)])
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
    opener = FakeOpener([_ok(STATUS_DONE), _ok(RESULTS_REPLY)])
    exit_code, stdout, stderr, _ = _run(["poll", REQUEST_ID, "--json"], opener, monkeypatch)
    assert exit_code == 0
    doc = json.loads(stdout)  # must be exactly one document
    assert doc["request_id"] == REQUEST_ID
    assert "request-id" not in stdout


def test_fail_on_scored_trips_exit_3(monkeypatch):
    opener = FakeOpener([_ok(STATUS_DONE), _ok(RESULTS_REPLY)])
    exit_code, stdout, stderr, _ = _run(
        ["poll", REQUEST_ID, "--fail-on-scored"], opener, monkeypatch
    )
    assert exit_code == 3
    assert "example.nl web_dnssec_exist" in stdout


def test_fail_on_scored_with_allowlisted_pair_exits_zero(tmp_path, monkeypatch):
    allowlist = tmp_path / "allow.txt"
    allowlist.write_text("example.nl web_dnssec_exist\n")
    opener = FakeOpener([_ok(STATUS_DONE), _ok(RESULTS_REPLY)])
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
    opener_a = FakeOpener([_ok(STATUS_DONE), _ok(RESULTS_REPLY)])
    exit_a, stdout_a, _, _ = _run(
        ["poll", REQUEST_ID, "--json"], opener_a, monkeypatch, endpoint="https://hosted.example/api/batch/v2"
    )
    opener_b = FakeOpener([_ok(STATUS_DONE), _ok(RESULTS_REPLY)])
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
