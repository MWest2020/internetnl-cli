"""Configuration resolution: environment first, then config file, then error.

No network I/O happens anywhere in this module, and no default endpoint is
ever compiled in — see design.md's configuration table.
"""

from __future__ import annotations

import configparser
import os
import pathlib
import urllib.parse
from dataclasses import dataclass
from typing import Mapping

from internetnl_cli.errors import ConfigError

# Rejected if present in the config *file* — credentials are environment-
# only. Important limitation, not a general scanner: the file (and this
# check) is only ever consulted at all when INTERNETNL_ENDPOINT is absent
# from the environment (see `resolve` below) — an operator who sets
# INTERNETNL_ENDPOINT in the environment and *also* has one of these keys
# sitting in an old config file will never have it flagged, because the
# file is never opened in that case. This mirrors the CLI's own
# env-beats-file precedence for `endpoint`; it is not a credential-leak
# scanner that runs unconditionally.
_FORBIDDEN_KEYS = {"username", "password", "passwd", "token", "secret", "credential"}

_NUMERIC_DEFAULTS = {
    "INTERNETNL_TIMEOUT": ("timeout", 30.0),
    "INTERNETNL_POLL_INTERVAL": ("poll_interval", 30.0),
    "INTERNETNL_POLL_MAX": ("poll_max", 3600.0),
    "INTERNETNL_BATCH_SIZE": ("batch_size", 5000),
}


@dataclass(frozen=True)
class Config:
    endpoint: str
    username: str
    password: str
    timeout: float
    poll_interval: float
    poll_max: float
    batch_size: int

    @property
    def endpoint_host(self) -> str:
        return urllib.parse.urlsplit(self.endpoint).hostname or "unknown"


def default_config_path(env: Mapping[str, str]) -> pathlib.Path | None:
    if "INTERNETNL_CONFIG" in env:
        return pathlib.Path(env["INTERNETNL_CONFIG"])
    home = env.get("HOME")
    if not home:
        # A missing or empty $HOME means "no default config file" — never
        # degrade to a CWD-relative lookup (review round 1, m3).
        return None
    return pathlib.Path(home) / ".config" / "internetnl" / "config.ini"


def _read_config_file(path: pathlib.Path) -> configparser.ConfigParser | None:
    """Return a parsed config, or None if the file does not exist."""
    if not path.exists():
        return None
    try:
        text = path.read_text()
    except OSError as exc:
        raise ConfigError(f"cannot read config file {path}: {exc}") from exc

    parser = configparser.ConfigParser()
    try:
        parser.read_string(text, source=str(path))
    except configparser.Error as exc:
        raise ConfigError(f"cannot parse config file {path}: {exc}") from exc
    return parser


def _config_section(path: pathlib.Path) -> configparser.SectionProxy | None:
    parser = _read_config_file(path)
    if parser is None:
        return None
    if not parser.has_section("internetnl"):
        raise ConfigError(f"config file {path} has no [internetnl] section")
    section = parser["internetnl"]
    for key in section:
        if key.lower() in _FORBIDDEN_KEYS:
            raise ConfigError(
                f"credentials must come from the environment, not {path}: remove '{key}'"
            )
    return section


def _validate_endpoint(endpoint: str, env: Mapping[str, str]) -> str:
    parsed = urllib.parse.urlsplit(endpoint)
    if parsed.username is not None:
        raise ConfigError("credentials must not appear in INTERNETNL_ENDPOINT")
    if parsed.scheme not in ("http", "https"):
        raise ConfigError(
            f"INTERNETNL_ENDPOINT must use http or https: got scheme '{parsed.scheme}'"
        )
    if parsed.scheme == "http" and env.get("INTERNETNL_ALLOW_HTTP") != "1":
        raise ConfigError(
            "INTERNETNL_ENDPOINT uses http, which sends HTTP Basic credentials "
            "in cleartext: set INTERNETNL_ALLOW_HTTP=1 to permit this (lab use only)"
        )
    if not parsed.hostname:
        raise ConfigError("INTERNETNL_ENDPOINT must include a host")
    return endpoint.rstrip("/")


def _resolve_numeric(env: Mapping[str, str], var: str, attr: str, default):
    raw = env.get(var)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{var} must be a number: got '{raw}'") from exc
    if value < 0:
        raise ConfigError(f"{var} must not be negative: got '{raw}'")
    if isinstance(default, int):
        if value != int(value):
            raise ConfigError(f"{var} must be an integer: got '{raw}'")
        return int(value)
    return value


def _resolve_credential(env: Mapping[str, str]) -> tuple[str, str]:
    """Resolve username/password from either the single-credential env var
    or the pair of legacy env vars — never both.

    `INTERNETNL_CREDENTIAL` is `username:password`, split on the *first*
    colon: a password may contain one, but per RFC 7617 a Basic userid
    never can — any username containing a colon could never authenticate
    via HTTP Basic in the first place (a compliant server, this repo's own
    facade included, would parse everything from the first colon onward as
    the password), so this split is unambiguous for every credential that
    could ever actually work. When set, `INTERNETNL_USERNAME`/
    `INTERNETNL_PASSWORD` must not also be set — silently preferring one
    over the other would mask a misconfiguration instead of surfacing it.
    """
    credential = env.get("INTERNETNL_CREDENTIAL")
    username_set = "INTERNETNL_USERNAME" in env
    password_set = "INTERNETNL_PASSWORD" in env

    if credential is not None:
        if username_set or password_set:
            raise ConfigError(
                "set either INTERNETNL_CREDENTIAL or INTERNETNL_USERNAME/"
                "INTERNETNL_PASSWORD, not both"
            )
        if ":" not in credential:
            raise ConfigError(
                "INTERNETNL_CREDENTIAL must be 'username:password' "
                "(split on the first ':')"
            )
        username, password = credential.split(":", 1)
        if not username or not password:
            # A degenerate split (":secret", "user:", or just ":") must not
            # silently produce an empty username: an empty username makes
            # `client.py` skip the Authorization header entirely, turning a
            # config typo into a silent, unauthenticated request instead of
            # a loud failure.
            raise ConfigError(
                "INTERNETNL_CREDENTIAL must be 'username:password' with a "
                "non-empty username and password"
            )
        return username, password

    return env.get("INTERNETNL_USERNAME", ""), env.get("INTERNETNL_PASSWORD", "")


def resolve(env: Mapping[str, str] | None = None) -> Config:
    if env is None:
        env = os.environ

    endpoint = env.get("INTERNETNL_ENDPOINT")
    config_path = default_config_path(env)
    section = None
    if not endpoint and config_path is not None:
        section = _config_section(config_path)
        if section is not None:
            endpoint = section.get("endpoint")

    if not endpoint:
        if config_path is not None:
            raise ConfigError(
                f"no endpoint configured: set INTERNETNL_ENDPOINT (or endpoint in {config_path})"
            )
        raise ConfigError("no endpoint configured: set INTERNETNL_ENDPOINT")

    endpoint = _validate_endpoint(endpoint, env)

    username, password = _resolve_credential(env)

    kwargs = {}
    for var, (attr, default) in _NUMERIC_DEFAULTS.items():
        kwargs[attr] = _resolve_numeric(env, var, attr, default)

    return Config(
        endpoint=endpoint,
        username=username,
        password=password,
        **kwargs,
    )
