"""`DemoSettings` (`netnl.settings.DemoSettings`) — fail-closed opt-in for
the anonymous `/demo/*` route family. See
`openspec/changes/add-demo-run/design.md`, D2/D6.
"""

from __future__ import annotations

import pytest

from netnl.errors import SettingsError
from netnl.settings import load


def _demo_env(settings_env, **overrides) -> dict:
    env = dict(settings_env)
    env["NETNL_DEMO_ENABLED"] = "1"
    env["NETNL_DEMO_ALLOWED_ORIGIN"] = "https://demo.example.org"
    env["NETNL_DEMO_TENANT"] = "netnl-demo"
    env.update(overrides)
    return env


def test_demo_is_none_when_disabled_by_default(settings_env):
    s = load(settings_env)
    assert s.demo is None


def test_demo_is_none_with_other_demo_vars_set_but_not_enabled(settings_env):
    # Every other NETNL_DEMO_* variable is ignored (not even read) unless
    # NETNL_DEMO_ENABLED=1 — an operator who never opts in must get exactly
    # the pre-existing behaviour regardless of what else is in the
    # environment.
    env = dict(settings_env)
    env["NETNL_DEMO_ALLOWED_ORIGIN"] = "not even a url"
    env["NETNL_DEMO_TENANT"] = "whatever"
    s = load(env)
    assert s.demo is None


@pytest.mark.parametrize("value", ["0", "true", "yes", "on", ""])
def test_demo_stays_disabled_for_any_value_other_than_the_literal_one(settings_env, value):
    env = dict(settings_env)
    env["NETNL_DEMO_ENABLED"] = value
    env["NETNL_DEMO_ALLOWED_ORIGIN"] = "https://demo.example.org"
    env["NETNL_DEMO_TENANT"] = "netnl-demo"
    s = load(env)
    assert s.demo is None


def test_enabled_without_allowed_origin_names_the_variable(settings_env):
    env = dict(settings_env)
    env["NETNL_DEMO_ENABLED"] = "1"
    env["NETNL_DEMO_TENANT"] = "netnl-demo"
    with pytest.raises(SettingsError, match="NETNL_DEMO_ALLOWED_ORIGIN"):
        load(env)


def test_enabled_without_tenant_names_the_variable(settings_env):
    env = dict(settings_env)
    env["NETNL_DEMO_ENABLED"] = "1"
    env["NETNL_DEMO_ALLOWED_ORIGIN"] = "https://demo.example.org"
    with pytest.raises(SettingsError, match="NETNL_DEMO_TENANT"):
        load(env)


def test_defaults_match_design(settings_env):
    s = load(_demo_env(settings_env))
    assert s.demo is not None
    assert s.demo.allowed_origin == "https://demo.example.org"
    assert s.demo.tenant == "netnl-demo"
    assert s.demo.max_per_hour == 6
    assert s.demo.max_concurrent == 2
    assert s.demo.per_ip_per_hour == 2
    assert s.demo.client_ip_header == "CF-Connecting-IP"
    assert s.demo.domain_cooldown_seconds == 900
    assert s.demo.retention_hours == 24
    # Builder-review fix (M2): the poll budget, bounding anonymous GET
    # status/results calls the way `per_ip_per_hour` bounds accepted POSTs.
    assert s.demo.polls_per_ip_per_hour == 120


def test_numeric_overrides_are_applied(settings_env):
    env = _demo_env(
        settings_env,
        NETNL_DEMO_MAX_PER_HOUR="12",
        NETNL_DEMO_MAX_CONCURRENT="4",
        NETNL_DEMO_PER_IP_PER_HOUR="3",
        NETNL_DEMO_DOMAIN_COOLDOWN_SECONDS="60",
        NETNL_DEMO_RETENTION_HOURS="1",
        NETNL_DEMO_POLLS_PER_IP_PER_HOUR="30",
    )
    s = load(env)
    assert s.demo.max_per_hour == 12
    assert s.demo.max_concurrent == 4
    assert s.demo.per_ip_per_hour == 3
    assert s.demo.domain_cooldown_seconds == 60
    assert s.demo.retention_hours == 1
    assert s.demo.polls_per_ip_per_hour == 30


def test_client_ip_header_is_overridable(settings_env):
    env = _demo_env(settings_env, NETNL_DEMO_CLIENT_IP_HEADER="X-Forwarded-For")
    s = load(env)
    assert s.demo.client_ip_header == "X-Forwarded-For"


@pytest.mark.parametrize(
    "bad_origin",
    [
        "http://demo.example.org",  # http without the localhost carve-out
        "demo.example.org",  # no scheme at all
        "https://demo.example.org/",  # trailing slash / path
        "https://user:pw@demo.example.org",  # embedded credentials
        "https://demo.example.org:notaport",  # non-numeric port
        "https://",  # empty host
        "https://demo.example.org, https://other.example.org",  # a list, not one origin
    ],
)
def test_bad_origin_shapes_are_rejected(settings_env, bad_origin):
    env = _demo_env(settings_env, NETNL_DEMO_ALLOWED_ORIGIN=bad_origin)
    with pytest.raises(SettingsError, match="NETNL_DEMO_ALLOWED_ORIGIN"):
        load(env)


def test_https_origin_with_port_is_accepted(settings_env):
    env = _demo_env(settings_env, NETNL_DEMO_ALLOWED_ORIGIN="https://demo.example.org:8443")
    s = load(env)
    assert s.demo.allowed_origin == "https://demo.example.org:8443"


def test_localhost_http_rejected_without_allow_http(settings_env):
    env = _demo_env(settings_env, NETNL_DEMO_ALLOWED_ORIGIN="http://localhost:3000")
    with pytest.raises(SettingsError, match="NETNL_DEMO_ALLOWED_ORIGIN"):
        load(env)


def test_localhost_http_accepted_under_allow_http(settings_env):
    env = _demo_env(
        settings_env,
        NETNL_DEMO_ALLOWED_ORIGIN="http://localhost:3000",
        NETNL_ALLOW_HTTP="1",
    )
    s = load(env)
    assert s.demo.allowed_origin == "http://localhost:3000"


def test_non_localhost_http_still_rejected_under_allow_http(settings_env):
    # NETNL_ALLOW_HTTP=1 carves out exactly http://localhost[:port], not
    # "any http origin".
    env = _demo_env(
        settings_env,
        NETNL_DEMO_ALLOWED_ORIGIN="http://demo.example.org",
        NETNL_ALLOW_HTTP="1",
    )
    with pytest.raises(SettingsError, match="NETNL_DEMO_ALLOWED_ORIGIN"):
        load(env)


def test_negative_demo_numeric_rejected(settings_env):
    env = _demo_env(settings_env, NETNL_DEMO_MAX_PER_HOUR="-1")
    with pytest.raises(SettingsError, match="NETNL_DEMO_MAX_PER_HOUR"):
        load(env)


def test_non_numeric_demo_value_rejected(settings_env):
    env = _demo_env(settings_env, NETNL_DEMO_PER_IP_PER_HOUR="lots")
    with pytest.raises(SettingsError, match="NETNL_DEMO_PER_IP_PER_HOUR"):
        load(env)


def test_negative_poll_budget_rejected(settings_env):
    env = _demo_env(settings_env, NETNL_DEMO_POLLS_PER_IP_PER_HOUR="-1")
    with pytest.raises(SettingsError, match="NETNL_DEMO_POLLS_PER_IP_PER_HOUR"):
        load(env)
