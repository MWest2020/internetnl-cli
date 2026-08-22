import copy
import io
import json
from datetime import datetime, timezone

from internetnl_cli.render import build_document, render_json, render_table
from fakes import REQUEST_ID, RESULTS_REPLY

RETRIEVED_AT = datetime(2026, 8, 22, 12, 0, 0, tzinfo=timezone.utc)

_EMPTY_CHECKS = {"failed": [], "accepted": [], "unknown": []}


def _doc(reply=RESULTS_REPLY, checks=None):
    return build_document(
        "batch.example",
        REQUEST_ID,
        reply,
        RETRIEVED_AT,
        checks if checks is not None else _EMPTY_CHECKS,
    )


def test_document_has_exactly_the_seven_pinned_keys():
    doc = _doc()
    assert list(doc.keys()) == [
        "endpoint",
        "timestamp",
        "api_version",
        "request_id",
        "request",
        "domains",
        "checks",
    ]


def test_render_json_round_trips():
    doc = _doc()
    stream = io.StringIO()
    render_json(doc, stream)
    loaded = json.loads(stream.getvalue())
    assert loaded == doc


def test_domains_passed_through_unmodified():
    doc = _doc()
    assert doc["domains"] == RESULTS_REPLY["domains"]


def test_finished_date_used_as_timestamp():
    doc = _doc()
    assert doc["timestamp"] == RESULTS_REPLY["request"]["finished_date"]


def test_missing_finished_date_falls_back_to_retrieved_at():
    reply = dict(RESULTS_REPLY)
    reply["request"] = dict(reply["request"])
    reply["request"]["finished_date"] = None
    doc = build_document("batch.example", REQUEST_ID, reply, RETRIEVED_AT, _EMPTY_CHECKS)
    assert doc["timestamp"] == "2026-08-22T12:00:00Z"


def test_missing_api_version_renders_unknown():
    reply = {k: v for k, v in RESULTS_REPLY.items() if k != "api_version"}
    doc = build_document("batch.example", REQUEST_ID, reply, RETRIEVED_AT, _EMPTY_CHECKS)
    assert doc["api_version"] == "unknown"


def test_table_contains_metadata_quartet():
    doc = _doc()
    stream = io.StringIO()
    render_table(doc, stream)
    output = stream.getvalue()
    assert "batch.example" in output
    assert REQUEST_ID in output
    assert doc["timestamp"] in output
    assert "2.6.0" in output


def test_json_contains_metadata_quartet():
    doc = _doc()
    stream = io.StringIO()
    render_json(doc, stream)
    output = stream.getvalue()
    assert "batch.example" in output
    assert REQUEST_ID in output
    assert doc["timestamp"] in output
    assert "2.6.0" in output


def test_errored_domain_row_shows_error_and_dashes():
    doc = _doc()
    stream = io.StringIO()
    render_table(doc, stream)
    lines = stream.getvalue().splitlines()
    error_lines = [line for line in lines if line.startswith("broken.nl")]
    assert len(error_lines) == 1
    assert "error" in error_lines[0]
    assert "-" in error_lines[0]


def test_no_escape_bytes_in_table_output():
    doc = _doc()
    stream = io.StringIO()
    render_table(doc, stream)
    assert "\x1b" not in stream.getvalue()


def test_exactly_one_json_document_written():
    doc = _doc()
    stream = io.StringIO()
    render_json(doc, stream)
    # A single json.loads over the whole stream content must succeed.
    json.loads(stream.getvalue())


def test_scoring_explicitly_null_renders_dash():
    reply = copy.deepcopy(RESULTS_REPLY)
    reply["domains"]["example.nl"]["scoring"] = None
    doc = build_document("batch.example", REQUEST_ID, reply, RETRIEVED_AT, _EMPTY_CHECKS)
    stream = io.StringIO()
    render_table(doc, stream)
    lines = stream.getvalue().splitlines()
    row = [line for line in lines if line.startswith("example.nl ")][0]
    # HOST STATUS SCORE ...
    assert row.split()[2] == "-"


def test_control_characters_in_host_name_are_filtered_from_table():
    reply = copy.deepcopy(RESULTS_REPLY)
    evil_host = "example\x1b[31m.nl\r"
    reply["domains"][evil_host] = reply["domains"].pop("example.nl")
    doc = build_document("batch.example", REQUEST_ID, reply, RETRIEVED_AT, _EMPTY_CHECKS)
    stream = io.StringIO()
    render_table(doc, stream)
    output = stream.getvalue()
    assert "\x1b" not in output
    assert "\r" not in output
