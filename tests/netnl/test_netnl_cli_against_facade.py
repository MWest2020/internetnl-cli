"""Acceptance test (design.md, tasks 4.2's build-time proxy): the
`internetnl` CLI works against the facade unchanged, with only
`INTERNETNL_*` env vars pointed at it instead of a real instance.

No CLI source or existing CLI test is touched to make this pass — see the
`git diff --stat` check in this task's verification command.
"""

from __future__ import annotations

import io
import json
import re

from starlette.testclient import TestClient

from fakes import (
    METADATA_REPLY,
    REGISTER_REPLY,
    REQUEST_ID,
    RESULTS_REPLY,
    STATUS_DONE,
    STATUS_RUNNING,
    FakeOpener,
)

from conftest import queue_json
from internetnl_cli import cli as cli_module
from internetnl_cli.client import HttpResponse
from internetnl_cli.errors import EXIT_API
from netnl import admin
from netnl.api import create_app
from netnl.settings import load

_FACADE_BASE_URL = "https://facade.test"
_FACADE_ID_RE = re.compile(r"^[a-f0-9]{32}$")


def facade_opener(testclient: TestClient, base_url: str):
    """The same injectable-opener seam the CLI already uses in
    `tests/fakes.py` — here it is backed by an in-process facade
    `TestClient` instead of a network socket.
    """

    def _opener(method, url, body, headers, timeout):
        assert url.startswith(base_url), url
        path = url[len(base_url):]
        response = testclient.request(method, path, content=body, headers=headers)
        return HttpResponse(status=response.status_code, body=response.content)

    return _opener


def _issue_credential(env, name: str) -> str:
    stdout, stderr = io.StringIO(), io.StringIO()
    code = admin.main(["user", "add", name], stdout=stdout, stderr=stderr, env=env)
    assert code == 0, stderr.getvalue()
    return stdout.getvalue().strip()


def test_cli_submit_poll_results_against_the_facade(settings_env, monkeypatch):
    password = _issue_credential(settings_env, "cli-tenant")

    settings = load(settings_env)
    fake_opener = FakeOpener()
    app = create_app(settings, opener=fake_opener)
    facade_client = TestClient(app, raise_server_exceptions=False)
    opener = facade_opener(facade_client, _FACADE_BASE_URL)

    monkeypatch.setenv("INTERNETNL_ENDPOINT", _FACADE_BASE_URL)
    monkeypatch.setenv("INTERNETNL_USERNAME", "cli-tenant")
    monkeypatch.setenv("INTERNETNL_PASSWORD", password)

    # --- submit ------------------------------------------------------
    queue_json(fake_opener, REGISTER_REPLY)
    submit_stdout, submit_stderr = io.StringIO(), io.StringIO()
    code = cli_module.main(
        ["submit", "example.nl", "--no-poll"],
        opener=opener,
        stdout=submit_stdout,
        stderr=submit_stderr,
    )
    assert code == 0
    request_line = [
        line for line in submit_stderr.getvalue().splitlines() if line.startswith("request-id:")
    ]
    assert len(request_line) == 1
    facade_id = request_line[0].split("request-id:", 1)[1].strip()
    assert _FACADE_ID_RE.fullmatch(facade_id)
    assert facade_id != REQUEST_ID  # the facade id, never the upstream one

    # --- poll ----------------------------------------------------------
    queue_json(fake_opener, STATUS_RUNNING)
    queue_json(fake_opener, STATUS_DONE)
    queue_json(fake_opener, RESULTS_REPLY)
    queue_json(fake_opener, METADATA_REPLY)
    poll_stdout, poll_stderr = io.StringIO(), io.StringIO()
    code = cli_module.main(
        ["poll", facade_id, "--json"],
        opener=opener,
        sleep=lambda _: None,
        stdout=poll_stdout,
        stderr=poll_stderr,
    )
    assert code == 0, poll_stderr.getvalue()

    # --- results -----------------------------------------------------------
    # `internetnl results` checks status first, then renders (which fetches
    # `results` and — cache miss aside, already warm from the poll above —
    # `metadata/report`).
    queue_json(fake_opener, STATUS_DONE)
    queue_json(fake_opener, RESULTS_REPLY)
    results_stdout, results_stderr = io.StringIO(), io.StringIO()
    code = cli_module.main(
        ["results", facade_id, "--json"],
        opener=opener,
        stdout=results_stdout,
        stderr=results_stderr,
    )
    assert code == 0, results_stderr.getvalue()
    doc = json.loads(results_stdout.getvalue())
    assert doc["domains"] == RESULTS_REPLY["domains"]
    assert doc["request_id"] == facade_id

    # --- someone else's (here: simply nonexistent) id -> 404, never the
    # upstream id, mapped onto the CLI's own API exit code -----------------
    foreign_stdout, foreign_stderr = io.StringIO(), io.StringIO()
    code = cli_module.main(
        ["results", "0" * 32],
        opener=opener,
        stdout=foreign_stdout,
        stderr=foreign_stderr,
    )
    assert code == EXIT_API
    assert "unknown-request" in foreign_stderr.getvalue()
    assert REQUEST_ID not in foreign_stderr.getvalue()
