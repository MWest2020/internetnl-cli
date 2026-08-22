"""Test doubles for the HTTP opener seam, plus sample API payloads.

Sample payloads are built from the vendored openapi.yaml (v2.6.0):
`RegisterReply`/`RequestReply` (request), `Domain` (status + results +
scoring), `Test` (status + verdict).
"""

from __future__ import annotations

from internetnl_cli.client import HttpResponse

REQUEST_ID = "e94251da69c54da7b16fc5202a69c5c2"


class FakeOpener:
    """Records calls and returns queued responses in order."""

    def __init__(self, responses=None):
        self.calls: list[tuple] = []
        self._responses = list(responses or [])

    def __call__(self, method, url, body, headers, timeout) -> HttpResponse:
        self.calls.append((method, url, body, headers, timeout))
        if not self._responses:
            raise AssertionError(
                f"FakeOpener ran out of queued responses (call #{len(self.calls)}: {method} {url})"
            )
        return self._responses.pop(0)


def raising_opener(exc: Exception):
    def _opener(method, url, body, headers, timeout):
        raise exc

    return _opener


REGISTER_REPLY = {
    "api_version": "2.6.0",
    "request": {
        "request_id": REQUEST_ID,
        "name": "Web test - 1/1/1970",
        "request_type": "web",
        "status": "registering",
        "submit_date": "2026-08-22T10:00:00+00:00",
        "finished_date": None,
    },
}

STATUS_RUNNING = {
    "api_version": "2.6.0",
    "request": {
        "request_id": REQUEST_ID,
        "name": "Web test - 1/1/1970",
        "request_type": "web",
        "status": "running",
        "submit_date": "2026-08-22T10:00:00+00:00",
        "finished_date": None,
    },
}

STATUS_DONE = {
    "api_version": "2.6.0",
    "request": {
        "request_id": REQUEST_ID,
        "name": "Web test - 1/1/1970",
        "request_type": "web",
        "status": "done",
        "submit_date": "2026-08-22T10:00:00+00:00",
        "finished_date": "2026-08-22T10:05:00+00:00",
    },
}

RESULTS_REPLY = {
    "api_version": "2.6.0",
    "request": {
        "request_id": REQUEST_ID,
        "name": "Web test - 1/1/1970",
        "request_type": "web",
        "status": "done",
        "submit_date": "2026-08-22T10:00:00+00:00",
        "finished_date": "2026-08-22T10:05:00+00:00",
    },
    "domains": {
        "example.nl": {
            "status": "ok",
            "scoring": {"percentage": 82},
            "results": {
                "categories": {
                    "web_ipv6": {"status": "passed", "verdict": "good"},
                    "web_dnssec": {"status": "failed", "verdict": "bad"},
                },
                "custom": None,
                "tests": {
                    "web_ipv6_ns_address": {"status": "passed", "verdict": "good"},
                    "web_dnssec_exist": {"status": "failed", "verdict": "bad"},
                    "web_https_hsts": {"status": "warning", "verdict": "phase-out"},
                    "web_appsecpriv_csp": {"status": "info", "verdict": "not-tested"},
                    "web_https_cert_domain": {"status": "not_tested", "verdict": "not-tested"},
                    "web_https_starttls": {"status": "error", "verdict": "error"},
                },
            },
        },
        "broken.nl": {
            "status": "error",
        },
    },
}
