"""Batch API v2 client: submit, status, results — through an injectable opener.

The transport seam is pinned here and must not change signature in later
tasks: `Opener = Callable[[method, url, body, headers, timeout], HttpResponse]`.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Callable, TextIO

from internetnl_cli.config import Config
from internetnl_cli.errors import ApiError, TransportError


def _package_version() -> str:
    """The installed distribution version, or a safe fallback.

    An editable install without build metadata (or any other reason the
    distribution can't be found) must not crash request-building — a
    missing version string is not a reason to fail an HTTP call. This is
    computed at *import* time (see `_USER_AGENT` below), so any exception
    here — not just `PackageNotFoundError` (e.g. corrupt dist-info
    METADATA) — must be swallowed: an exception escaping this function
    would crash importing this module, and with it `netnl-serve`.
    """
    try:
        return version("internetnl-cli")
    except Exception:
        return "unknown"


_USER_AGENT = f"internetnl-cli/{_package_version()}"


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes


Opener = Callable[[str, str, object, dict, float], HttpResponse]
# opener(method, url, body: bytes | None, headers, timeout) -> HttpResponse

# Upstream `RequestId` pattern (openapi.yaml, ~line 762): a UUID with the
# dashes stripped, always lowercase hex.
_REQUEST_ID_RE = re.compile(r"^[a-f0-9]{32}$")


def is_valid_request_id(value: object) -> bool:
    return isinstance(value, str) and bool(_REQUEST_ID_RE.fullmatch(value))


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a 3xx: `Authorization` must not leak to another host.

    Returning `None` from `redirect_request` makes `urllib` raise the
    original response as an `HTTPError` instead of re-issuing the request,
    so a redirect surfaces exactly like any other non-200 reply.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_no_redirect_opener = urllib.request.build_opener(_RefuseRedirects)


def urllib_opener(method, url, body, headers, timeout) -> HttpResponse:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with _no_redirect_opener.open(request, timeout=timeout) as response:
            return HttpResponse(status=response.status, body=response.read())
    except urllib.error.HTTPError as exc:
        return HttpResponse(status=exc.code, body=exc.read())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        host = urllib.parse.urlsplit(url).hostname or "unknown"
        reason = getattr(exc, "reason", None) or str(exc)
        raise TransportError(f"{reason} while contacting {host}") from exc


class BatchClient:
    def __init__(
        self,
        config: Config,
        opener: Opener = urllib_opener,
        debug_stream: TextIO | None = None,
    ) -> None:
        self._config = config
        self._opener = opener
        self._debug_stream = debug_stream

    @property
    def endpoint_host(self) -> str:
        return self._config.endpoint_host

    def submit(self, domains: list[str], request_type: str, name: str | None) -> dict:
        payload: dict = {"type": request_type, "domains": domains}
        if name is not None:
            payload["name"] = name
        parsed = self._call("POST", "/requests", payload)
        self._validate_request_object(parsed, "/requests")
        return parsed

    def status(self, request_id: str) -> dict:
        path = f"/requests/{self._encoded_request_id(request_id)}"
        parsed = self._call("GET", path, None)
        self._validate_request_object(parsed, path)
        return parsed

    def results(self, request_id: str) -> dict:
        path = f"/requests/{self._encoded_request_id(request_id)}/results"
        parsed = self._call("GET", path, None)
        self._validate_request_object(parsed, path)
        self._validate_domains(parsed, path)
        return parsed

    def metadata_report(self) -> dict:
        return self._call("GET", "/metadata/report", None)

    def _encoded_request_id(self, request_id: str) -> str:
        if not is_valid_request_id(request_id):
            raise ApiError(
                f"invalid request id from {self._config.endpoint_host}: "
                "expected 32 lowercase hex characters"
            )
        # Belt and braces: quote even though the pattern above already
        # excludes anything a URL path could not carry literally.
        return urllib.parse.quote(request_id, safe="")

    def _validate_request_object(self, parsed: dict, path: str) -> None:
        host = self._config.endpoint_host
        request = parsed.get("request")
        if not isinstance(request, dict):
            raise ApiError(f"malformed reply from {host} (missing 'request', {path})")
        request_id = request.get("request_id")
        status = request.get("status")
        if not is_valid_request_id(request_id):
            raise ApiError(f"malformed reply from {host} (invalid request_id, {path})")
        if not isinstance(status, str):
            raise ApiError(f"malformed reply from {host} (invalid status, {path})")

    def _validate_domains(self, parsed: dict, path: str) -> None:
        """Round 2 (m2): fail closed on a malformed `domains` block.

        A `domains` value that is present but not an object, or a
        domain/results/tests entry within it that is not an object, is an
        `ApiError` — the same fail-closed rule already applied to the
        `request` object one layer up. A `domains` key that is entirely
        absent is left to callers (an unfinished-run status reply has none).
        """
        host = self._config.endpoint_host
        domains = parsed.get("domains")
        if domains is None:
            return
        if not isinstance(domains, dict):
            raise ApiError(f"malformed reply from {host} (domains is not an object, {path})")
        for domain in domains.values():
            if not isinstance(domain, dict):
                raise ApiError(
                    f"malformed reply from {host} (domain entry is not an object, {path})"
                )
            results = domain.get("results")
            if results is None:
                continue
            if not isinstance(results, dict):
                raise ApiError(
                    f"malformed reply from {host} (domain results is not an object, {path})"
                )
            tests = results.get("tests")
            if tests is None:
                continue
            if not isinstance(tests, dict):
                raise ApiError(f"malformed reply from {host} (tests is not an object, {path})")
            for test in tests.values():
                if not isinstance(test, dict):
                    raise ApiError(
                        f"malformed reply from {host} (test entry is not an object, {path})"
                    )

    def _headers(self) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _USER_AGENT,
        }
        if self._config.username:
            token = f"{self._config.username}:{self._config.password}".encode()
            headers["Authorization"] = "Basic " + base64.b64encode(token).decode()
        return headers

    def _call(self, method: str, path: str, payload: dict | None) -> dict:
        url = self._config.endpoint + path
        body = json.dumps(payload).encode() if payload is not None else None
        headers = self._headers()
        host = self._config.endpoint_host

        if self._debug_stream is not None:
            self._debug_stream.write(f"> {method} {url}\n")

        try:
            response = self._opener(method, url, body, headers, self._config.timeout)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", None) or str(exc)
            raise TransportError(f"{reason} while contacting {host}") from exc

        if response.status == 200:
            try:
                parsed = json.loads(response.body)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ApiError(f"malformed reply from {host} (HTTP 200, {path})") from exc
            if not isinstance(parsed, dict):
                raise ApiError(f"malformed reply from {host} (HTTP 200, {path})")
            return parsed

        detail = ""
        try:
            error_body = json.loads(response.body)
            if isinstance(error_body, dict):
                error = error_body.get("error")
                if isinstance(error, dict) and "label" in error and "msg" in error:
                    detail = f": {error['label']}: {error['msg']}"
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

        raise ApiError(f"HTTP {response.status} from {host} ({method} {path}){detail}")
