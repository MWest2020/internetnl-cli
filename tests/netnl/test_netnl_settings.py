from __future__ import annotations

import pytest

from netnl.errors import SettingsError
from netnl.settings import load
from netnl.upstream import build_config

REQUIRED_VARS = (
    "NETNL_UPSTREAM_ENDPOINT",
    "NETNL_UPSTREAM_USERNAME",
    "NETNL_UPSTREAM_PASSWORD",
    "NETNL_DB",
)


@pytest.mark.parametrize("missing", REQUIRED_VARS)
def test_missing_required_var_names_itself(settings_env, missing):
    env = dict(settings_env)
    del env[missing]
    with pytest.raises(SettingsError, match=missing):
        load(env)


def test_defaults_match_design(settings_env):
    s = load(settings_env)
    assert s.instance == "netnl"
    assert s.rate_limit == 10
    assert s.max_domains == 500
    assert s.max_concurrent == 2
    assert s.result_retention_days == 7
    assert s.audit_retention_days == 90
    assert s.metadata_ttl == 3600
    assert s.timeout == 30
    assert s.reserving_grace_seconds == 300
    assert s.allow_http is False
    assert s.security_contact is None


def test_no_default_points_at_a_host(settings_env):
    # Every field that could carry a host is required, not defaulted.
    env = {"NETNL_DB": settings_env["NETNL_DB"]}
    with pytest.raises(SettingsError):
        load(env)


def test_http_upstream_without_allow_http_rejected(settings_env):
    env = dict(settings_env)
    env["NETNL_UPSTREAM_ENDPOINT"] = "http://batch.internal/api/batch/v2"
    s = load(env)
    with pytest.raises(SettingsError, match="NETNL_ALLOW_HTTP"):
        build_config(s)


def test_http_upstream_with_allow_http_ok(settings_env):
    env = dict(settings_env)
    env["NETNL_UPSTREAM_ENDPOINT"] = "http://batch.internal/api/batch/v2"
    env["NETNL_ALLOW_HTTP"] = "1"
    s = load(env)
    cfg = build_config(s)
    assert cfg.endpoint == "http://batch.internal/api/batch/v2"


def test_credentials_in_endpoint_url_rejected(settings_env):
    env = dict(settings_env)
    env["NETNL_UPSTREAM_ENDPOINT"] = "https://user:pw@batch.internal/api/batch/v2"
    s = load(env)
    with pytest.raises(SettingsError, match="NETNL_UPSTREAM_ENDPOINT"):
        build_config(s)


def test_instance_rejects_header_injection(settings_env):
    env = dict(settings_env)
    env["NETNL_INSTANCE"] = "a\r\nX: y"
    with pytest.raises(SettingsError, match="NETNL_INSTANCE"):
        load(env)


def test_instance_accepts_plain_name(settings_env):
    env = dict(settings_env)
    env["NETNL_INSTANCE"] = "my-instance_01. "
    s = load(env)
    assert s.instance == "my-instance_01. "


def test_non_numeric_rate_limit_rejected(settings_env):
    env = dict(settings_env)
    env["NETNL_RATE_LIMIT"] = "lots"
    with pytest.raises(SettingsError, match="NETNL_RATE_LIMIT"):
        load(env)


def test_negative_numeric_rejected(settings_env):
    env = dict(settings_env)
    env["NETNL_MAX_DOMAINS"] = "-1"
    with pytest.raises(SettingsError, match="NETNL_MAX_DOMAINS"):
        load(env)


def test_build_config_reuses_cli_config(settings_env):
    s = load(settings_env)
    cfg = build_config(s)
    assert cfg.endpoint == settings_env["NETNL_UPSTREAM_ENDPOINT"]
    assert cfg.username == settings_env["NETNL_UPSTREAM_USERNAME"]
    assert cfg.password == settings_env["NETNL_UPSTREAM_PASSWORD"]
    assert cfg.timeout == 30


def test_security_contact_unset_is_none(settings_env):
    s = load(settings_env)
    assert s.security_contact is None


def test_security_contact_set_is_carried_through(settings_env):
    env = dict(settings_env)
    env["NETNL_SECURITY_CONTACT"] = "mailto:security@example.org"
    s = load(env)
    assert s.security_contact == "mailto:security@example.org"


def test_security_contact_empty_string_treated_as_unset(settings_env):
    # Opt-in var: an accidentally-set-but-empty value must not enable the
    # route with a blank contact.
    env = dict(settings_env)
    env["NETNL_SECURITY_CONTACT"] = ""
    s = load(env)
    assert s.security_contact is None


def test_build_config_does_not_read_home_config(settings_env, tmp_path, monkeypatch):
    # A config file under $HOME must never be consulted for facade settings.
    # Uses a directory distinct from the one the `isolated_home` fixture
    # (tests/conftest.py, unmodified) itself manages and asserts is empty.
    alt_home = tmp_path / "alt-home"
    (alt_home / ".config" / "internetnl").mkdir(parents=True)
    (alt_home / ".config" / "internetnl" / "config.ini").write_text(
        "[internetnl]\nendpoint = https://should-not-be-used.example\n"
    )
    monkeypatch.setenv("HOME", str(alt_home))
    s = load(settings_env)
    cfg = build_config(s)
    assert cfg.endpoint == settings_env["NETNL_UPSTREAM_ENDPOINT"]
