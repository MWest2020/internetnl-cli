import base64
import copy
import importlib.metadata
import json

import pytest

from internetnl_cli.client import BatchClient, HttpResponse
from internetnl_cli.config import Config
from internetnl_cli.errors import ApiError, TransportError
from fakes import METADATA_REPLY, REQUEST_ID, RESULTS_REPLY, FakeOpener, raising_opener


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


def _request_reply(**overrides):
    request = {"request_id": REQUEST_ID, "status": "registering"}
    request.update(overrides)
    return {"api_version": "2.6.0", "request": request}


def test_submit_posts_to_requests_with_body():
    opener = FakeOpener([_ok(_request_reply())])
    client = BatchClient(_config(), opener=opener)
    client.submit(["a.example", "b.example"], "web", None)
    method, url, body, headers, timeout = opener.calls[0]
    assert method == "POST"
    assert url == "https://batch.example/api/batch/v2/requests"
    payload = json.loads(body)
    assert payload == {"type": "web", "domains": ["a.example", "b.example"]}
    assert "name" not in payload


def test_submit_includes_name_when_given():
    opener = FakeOpener([_ok(_request_reply())])
    client = BatchClient(_config(), opener=opener)
    client.submit(["a.example"], "mail", "my-run")
    payload = json.loads(opener.calls[0][2])
    assert payload["name"] == "my-run"


def test_status_gets_request_by_id():
    opener = FakeOpener([_ok(_request_reply(status="running"))])
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
    opener = FakeOpener([_ok(_request_reply())])
    client = BatchClient(_config(username="", password=""), opener=opener)
    client.status(REQUEST_ID)
    headers = opener.calls[0][3]
    assert "Authorization" not in headers


def test_authorization_header_present_and_correct_with_credentials():
    opener = FakeOpener([_ok(_request_reply())])
    client = BatchClient(_config(username="alice", password="wonderland"), opener=opener)
    client.status(REQUEST_ID)
    headers = opener.calls[0][3]
    expected = "Basic " + base64.b64encode(b"alice:wonderland").decode()
    assert headers["Authorization"] == expected


def _submit_call(client):
    client.submit(["a.example"], "web", None)


def _status_call(client):
    client.status(REQUEST_ID)


def _results_call(client):
    client.results(REQUEST_ID)


def _metadata_report_call(client):
    client.metadata_report()


@pytest.mark.parametrize(
    "response, make_call",
    [
        (_ok(_request_reply()), _submit_call),
        (_ok(_request_reply(status="running")), _status_call),
        (_ok(RESULTS_REPLY), _results_call),
        (_ok(METADATA_REPLY), _metadata_report_call),
    ],
    ids=["submit", "status", "results", "metadata_report"],
)
def test_user_agent_header_present_on_every_request(response, make_call):
    opener = FakeOpener([response])
    client = BatchClient(_config(), opener=opener)
    make_call(client)
    headers = opener.calls[0][3]
    expected = f"internetnl-cli/{importlib.metadata.version('internetnl-cli')}"
    assert headers["User-Agent"] == expected


def test_package_version_falls_back_to_unknown_on_any_lookup_failure(monkeypatch):
    import internetnl_cli.client as client_module

    def raise_not_found(name):
        raise client_module.PackageNotFoundError(name)

    monkeypatch.setattr(client_module, "version", raise_not_found)
    assert client_module._package_version() == "unknown"


def test_debug_stream_shows_method_and_url_but_not_secret():
    import io

    stream = io.StringIO()
    opener = FakeOpener([_ok(_request_reply())])
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


# --- B1: redirects are never followed ---------------------------------------


def test_redirect_status_maps_to_api_error_not_followed():
    opener = FakeOpener([HttpResponse(status=302, body=b"")])
    client = BatchClient(_config(), opener=opener)
    with pytest.raises(ApiError) as excinfo:
        client.status(REQUEST_ID)
    assert "302" in str(excinfo.value)
    # Only the one (non-followed) call was ever made.
    assert len(opener.calls) == 1


def test_urllib_opener_redirect_handler_refuses_every_redirect():
    import urllib.request

    from internetnl_cli.client import _RefuseRedirects

    handler = _RefuseRedirects()
    result = handler.redirect_request(
        urllib.request.Request("https://batch.example/requests/x"),
        None,
        302,
        "Found",
        {},
        "https://evil.example/steal",
    )
    assert result is None


# --- u1: the urllib layer itself must not fall back to its default UA -------


def test_urllib_opener_does_not_let_urllib_inject_its_default_user_agent():
    """Regression: a header dict handed to `urllib_opener` must win.

    The unit tests above only prove `BatchClient` puts `User-Agent` into the
    headers dict it builds; they never exercise `urllib_opener` itself, so
    they could not catch `urllib.request` silently overriding — or
    appending its own `Python-urllib/x.y` — default `User-Agent`. Cloudflare
    (and other bot-protection in front of a batch instance) blocks exactly
    that default string with a 403, which is the real-world failure this
    header exists to prevent. This drives a real request through
    `urllib_opener` to a real local HTTP server, so nothing about the
    `urllib` request/opener plumbing is faked away.
    """
    import http.server
    import threading

    from internetnl_cli.client import _USER_AGENT, urllib_opener

    received: dict = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            received["user_agent"] = self.headers.get("User-Agent")
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A002 - stdlib signature
            pass  # keep test output quiet

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        host, port = server.server_address
        url = f"http://{host}:{port}/"
        response = urllib_opener("GET", url, None, {"User-Agent": _USER_AGENT}, 5.0)
    finally:
        server.shutdown()
        thread.join()

    assert response.status == 200
    user_agent = received.get("user_agent")
    assert user_agent is not None
    assert user_agent.startswith("internetnl-cli/")
    assert "Python-urllib" not in user_agent


# --- m1: malformed 200 reply fails closed ------------------------------------


def test_reply_without_request_object_is_api_error():
    opener = FakeOpener([_ok({"api_version": "2.6.0"})])
    client = BatchClient(_config(), opener=opener)
    with pytest.raises(ApiError):
        client.status(REQUEST_ID)


def test_reply_with_non_string_status_is_api_error():
    opener = FakeOpener([_ok(_request_reply(status=None))])
    client = BatchClient(_config(), opener=opener)
    with pytest.raises(ApiError):
        client.status(REQUEST_ID)


# --- M2: request_id validated and quoted before it reaches a URL path ------


def test_status_with_invalid_request_id_is_api_error_and_makes_no_call():
    opener = FakeOpener([])
    client = BatchClient(_config(), opener=opener)
    with pytest.raises(ApiError):
        client.status("../../../etc/passwd")
    assert opener.calls == []


def test_results_with_control_characters_in_request_id_is_api_error_and_makes_no_call():
    opener = FakeOpener([])
    client = BatchClient(_config(), opener=opener)
    with pytest.raises(ApiError):
        client.results("x\r\n")
    assert opener.calls == []


def test_submit_reply_with_malformed_request_id_is_api_error():
    opener = FakeOpener([_ok(_request_reply(request_id="not-a-valid-id"))])
    client = BatchClient(_config(), opener=opener)
    with pytest.raises(ApiError):
        client.submit(["a.example"], "web", None)


# --- Round 2 (m2): malformed `domains` block fails closed --------------------


def test_results_with_domains_as_list_is_api_error():
    reply = copy.deepcopy(RESULTS_REPLY)
    reply["domains"] = ["not", "a", "dict"]
    opener = FakeOpener([_ok(reply)])
    client = BatchClient(_config(), opener=opener)
    with pytest.raises(ApiError):
        client.results(REQUEST_ID)


def test_results_with_domain_entry_as_string_is_api_error():
    reply = copy.deepcopy(RESULTS_REPLY)
    reply["domains"]["example.nl"] = "not-a-dict"
    opener = FakeOpener([_ok(reply)])
    client = BatchClient(_config(), opener=opener)
    with pytest.raises(ApiError):
        client.results(REQUEST_ID)


def test_results_with_test_entry_as_string_is_api_error():
    reply = copy.deepcopy(RESULTS_REPLY)
    reply["domains"]["example.nl"]["results"]["tests"]["web_dnssec_exist"] = "not-a-dict"
    opener = FakeOpener([_ok(reply)])
    client = BatchClient(_config(), opener=opener)
    with pytest.raises(ApiError):
        client.results(REQUEST_ID)


def test_results_with_valid_domains_is_unaffected():
    opener = FakeOpener([_ok(RESULTS_REPLY)])
    client = BatchClient(_config(), opener=opener)
    reply = client.results(REQUEST_ID)
    assert reply == RESULTS_REPLY
