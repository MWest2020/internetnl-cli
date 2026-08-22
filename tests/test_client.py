import base64
import json

import pytest

from internetnl_cli.client import BatchClient, HttpResponse
from internetnl_cli.config import Config
from internetnl_cli.errors import ApiError, TransportError
from fakes import REQUEST_ID, RESULTS_REPLY, FakeOpener, raising_opener


def _config(**overrides):
    defaults = dict(
        endpoint="https://batch.example/api/batch/v2",
        username="",
        password="",
        timeout=30.0,
        poll_interval=30.0,
        poll_max=3600.0,
        batch_size=5000,
    )
    defaults.update(overrides)
    return Config(**defaults)


def _ok(payload):
    return HttpResponse(status=200, body=json.dumps(payload).encode())


def test_submit_posts_to_requests_with_body():
    opener = FakeOpener([_ok({"api_version": "2.6.0", "request": {"request_id": REQUEST_ID}})])
    client = BatchClient(_config(), opener=opener)
    client.submit(["a.example", "b.example"], "web", None)
    method, url, body, headers, timeout = opener.calls[0]
    assert method == "POST"
    assert url == "https://batch.example/api/batch/v2/requests"
    payload = json.loads(body)
    assert payload == {"type": "web", "domains": ["a.example", "b.example"]}
    assert "name" not in payload


def test_submit_includes_name_when_given():
    opener = FakeOpener([_ok({"api_version": "2.6.0", "request": {}})])
    client = BatchClient(_config(), opener=opener)
    client.submit(["a.example"], "mail", "my-run")
    payload = json.loads(opener.calls[0][2])
    assert payload["name"] == "my-run"


def test_status_gets_request_by_id():
    opener = FakeOpener([_ok({"api_version": "2.6.0", "request": {"status": "running"}})])
    client = BatchClient(_config(), opener=opener)
    client.status(REQUEST_ID)
    method, url, body, headers, timeout = opener.calls[0]
    assert method == "GET"
    assert url == f"https://batch.example/api/batch/v2/requests/{REQUEST_ID}"
    assert body is None


def test_results_gets_results_path():
    opener = FakeOpener([_ok(RESULTS_REPLY)])
    client = BatchClient(_config(), opener=opener)
    reply = client.results(REQUEST_ID)
    method, url, body, headers, timeout = opener.calls[0]
    assert method == "GET"
    assert url == f"https://batch.example/api/batch/v2/requests/{REQUEST_ID}/results"
    assert reply == RESULTS_REPLY


def test_authorization_header_absent_with_empty_username():
    opener = FakeOpener([_ok({"api_version": "2.6.0", "request": {}})])
    client = BatchClient(_config(username="", password=""), opener=opener)
    client.status(REQUEST_ID)
    headers = opener.calls[0][3]
    assert "Authorization" not in headers


def test_authorization_header_present_and_correct_with_credentials():
    opener = FakeOpener([_ok({"api_version": "2.6.0", "request": {}})])
    client = BatchClient(_config(username="alice", password="wonderland"), opener=opener)
    client.status(REQUEST_ID)
    headers = opener.calls[0][3]
    expected = "Basic " + base64.b64encode(b"alice:wonderland").decode()
    assert headers["Authorization"] == expected


def test_debug_stream_shows_method_and_url_but_not_secret():
    import io

    stream = io.StringIO()
    opener = FakeOpener([_ok({"api_version": "2.6.0", "request": {"request_id": REQUEST_ID}})])
    client = BatchClient(
        _config(username="alice", password="s3cr3t"), opener=opener, debug_stream=stream
    )
    client.submit(["a.example"], "web", None)
    output = stream.getvalue()
    assert "> POST https://batch.example/api/batch/v2/requests" in output
    assert "s3cr3t" not in output
    assert "Authorization" not in output


@pytest.mark.parametrize("status", [401, 404, 500])
def test_http_error_status_maps_to_api_error(status):
    opener = FakeOpener(
        [HttpResponse(status=status, body=json.dumps({"error": {"label": "x", "msg": "y"}}).encode())]
    )
    client = BatchClient(_config(), opener=opener)
    with pytest.raises(ApiError) as excinfo:
        client.status(REQUEST_ID)
    message = str(excinfo.value)
    assert str(status) in message
    assert "batch.example" in message


def test_urlerror_maps_to_transport_error():
    import urllib.error

    opener = raising_opener(urllib.error.URLError("connection refused"))
    client = BatchClient(_config(), opener=opener)
    with pytest.raises(TransportError):
        client.status(REQUEST_ID)


def test_malformed_200_body_maps_to_api_error():
    opener = FakeOpener([HttpResponse(status=200, body=b"not json")])
    client = BatchClient(_config(), opener=opener)
    with pytest.raises(ApiError):
        client.status(REQUEST_ID)


def test_non_object_200_body_maps_to_api_error():
    opener = FakeOpener([HttpResponse(status=200, body=b"[1, 2, 3]")])
    client = BatchClient(_config(), opener=opener)
    with pytest.raises(ApiError):
        client.status(REQUEST_ID)


def test_credentials_never_appear_in_output():
    import io
    import urllib.error

    secret = "s3cr3t-never-print"
    stream = io.StringIO()
    captured_exceptions = []

    opener = FakeOpener(
        [HttpResponse(status=401, body=json.dumps({"error": {"label": "x", "msg": "y"}}).encode())]
    )
    client = BatchClient(
        _config(username="alice", password=secret), opener=opener, debug_stream=stream
    )
    try:
        client.status(REQUEST_ID)
    except ApiError as exc:
        captured_exceptions.append(str(exc))

    transport_client = BatchClient(
        _config(username="alice", password=secret),
        opener=raising_opener(urllib.error.URLError("boom")),
        debug_stream=stream,
    )
    try:
        transport_client.status(REQUEST_ID)
    except TransportError as exc:
        captured_exceptions.append(str(exc))

    encoded = base64.b64encode(f"alice:{secret}".encode()).decode()
    everything = stream.getvalue() + "\n".join(captured_exceptions)
    assert secret not in everything
    assert encoded not in everything
