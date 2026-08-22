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

_FORBIDDEN_KEYS = {"username", "password", "passwd", "token", "secret"}

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

    username = env.get("INTERNETNL_USERNAME", "")
    password = env.get("INTERNETNL_PASSWORD", "")

    kwargs = {}
    for var, (attr, default) in _NUMERIC_DEFAULTS.items():
        kwargs[attr] = _resolve_numeric(env, var, attr, default)

    return Config(
        endpoint=endpoint,
        username=username,
        password=password,
        **kwargs,
    )
