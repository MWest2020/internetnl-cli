"""Batch API v2 client: submit, status, results — through an injectable opener.

The transport seam is pinned here and must not change signature in later
tasks: `Opener = Callable[[method, url, body, headers, timeout], HttpResponse]`.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable, TextIO

from internetnl_cli.config import Config
from internetnl_cli.errors import ApiError, TransportError


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes


Opener = Callable[[str, str, object, dict, float], HttpResponse]
# opener(method, url, body: bytes | None, headers, timeout) -> HttpResponse


def urllib_opener(method, url, body, headers, timeout) -> HttpResponse:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
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
        return self._call("POST", "/requests", payload)

    def status(self, request_id: str) -> dict:
        return self._call("GET", f"/requests/{request_id}", None)

    def results(self, request_id: str) -> dict:
        return self._call("GET", f"/requests/{request_id}/results", None)

    def _headers(self) -> dict:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
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
