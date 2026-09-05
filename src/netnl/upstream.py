"""The single path into the upstream instance.

Reuses `internetnl_cli.client.BatchClient` and `internetnl_cli.config`
unchanged: same injectable opener, same redirect refusal, same
request-id validation and leak-free error discipline.
"""

from __future__ import annotations

from importlib.metadata import version

from internetnl_cli import config as cli_config
from internetnl_cli.client import BatchClient, HttpResponse, Opener, urllib_opener
from internetnl_cli.errors import ConfigError

from netnl.errors import SettingsError
from netnl.settings import Settings


def _package_version() -> str:
    """The installed distribution version, or a safe fallback.

    Same distribution, same fallback as `internetnl_cli.client._package_
    version` (not imported directly: that name is the CLI's private
    implementation detail, not part of its public surface).
    """
    try:
        return version("internetnl-cli")
    except Exception:
        return "unknown"


_FACADE_USER_AGENT = f"netnl/{_package_version()} internetnl-cli/{_package_version()}"


def _with_facade_user_agent(opener: Opener) -> Opener:
    """Wrap an `Opener` so every call it makes carries the facade's own
    `User-Agent` ahead of the CLI's, without touching anything else about
    the request `BatchClient` builds (design.md D1).
    """

    def _opener(method, url, body, headers, timeout) -> HttpResponse:
        wrapped_headers = dict(headers)
        wrapped_headers["User-Agent"] = _FACADE_USER_AGENT
        return opener(method, url, body, wrapped_headers, timeout)

    return _opener


def build_config(settings: Settings) -> cli_config.Config:
    """Build the CLI's `Config` from facade settings, without touching disk.

    Passes an explicit mapping to `internetnl_cli.config.resolve` — no
    `HOME` key, so `default_config_path` is `None` and no config file is
    ever read (`config.py:128`).
    """
    env: dict[str, str] = {
        "INTERNETNL_ENDPOINT": settings.upstream_endpoint,
        "INTERNETNL_USERNAME": settings.upstream_username,
        "INTERNETNL_PASSWORD": settings.upstream_password,
        "INTERNETNL_TIMEOUT": str(settings.timeout),
    }
    if settings.allow_http:
        env["INTERNETNL_ALLOW_HTTP"] = "1"

    try:
        return cli_config.resolve(env)
    except ConfigError as exc:
        message = str(exc)
        message = message.replace("INTERNETNL_ENDPOINT", "NETNL_UPSTREAM_ENDPOINT")
        message = message.replace("INTERNETNL_ALLOW_HTTP", "NETNL_ALLOW_HTTP")
        raise SettingsError(message) from exc


def build_client(settings: Settings, opener: Opener | None = None) -> BatchClient:
    """The only path into the upstream instance in this package."""
    return BatchClient(
        build_config(settings), opener=_with_facade_user_agent(opener or urllib_opener)
    )
