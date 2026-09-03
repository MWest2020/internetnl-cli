"""`SupporterSettings` (`netnl.settings.SupporterSettings`) — fail-closed
opt-in for the `POST /webhooks/bmc` bridge. See openspec/changes/
add-supporter-issuance/design.md, D2/D5.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from netnl.errors import SettingsError
from netnl.settings import load


def _supporter_env(settings_env, **overrides) -> dict:
    env = dict(settings_env)
    env["NETNL_BMC_WEBHOOK_SECRET"] = "test-secret-that-is-32-chars-long!!"
    env["NETNL_PUBLIC_ENDPOINT"] = "https://facade.example.org"
    env["NETNL_SMTP_HOST"] = "smtp.example.org"
    env["NETNL_SMTP_FROM"] = "noreply@example.org"
    env.update(overrides)
    return env


def test_supporter_is_none_when_disabled_by_default(settings_env):
    s = load(settings_env)
    assert s.supporter is None


def test_supporter_is_none_with_other_vars_set_but_secret_unset(settings_env):
    # Every other NETNL_BMC_*/NETNL_SUPPORTER_*/NETNL_SMTP_* variable is
    # ignored (not even read) unless NETNL_BMC_WEBHOOK_SECRET is set.
    env = dict(settings_env)
    env["NETNL_SMTP_HOST"] = "not-a-real-host"
    env["NETNL_SUPPORTER_MIN_AMOUNT"] = "not-a-number-either"
    s = load(env)
    assert s.supporter is None


def test_enabled_without_public_endpoint_names_the_variable(settings_env):
    env = _supporter_env(settings_env)
    del env["NETNL_PUBLIC_ENDPOINT"]
    with pytest.raises(SettingsError, match="NETNL_PUBLIC_ENDPOINT"):
        load(env)


def test_enabled_without_smtp_host_names_the_variable(settings_env):
    env = _supporter_env(settings_env)
    del env["NETNL_SMTP_HOST"]
    with pytest.raises(SettingsError, match="NETNL_SMTP_HOST"):
        load(env)


def test_enabled_without_smtp_from_names_the_variable(settings_env):
    env = _supporter_env(settings_env)
    del env["NETNL_SMTP_FROM"]
    with pytest.raises(SettingsError, match="NETNL_SMTP_FROM"):
        load(env)


def test_defaults_match_design(settings_env):
    s = load(_supporter_env(settings_env))
    assert s.supporter is not None
    cfg = s.supporter
    assert cfg.webhook_secret == "test-secret-that-is-32-chars-long!!"
    assert cfg.signature_header == "X-Signature-Sha256"
    assert cfg.max_body_bytes == 65536
    assert cfg.accept_test_mode is False
    assert cfg.min_amount == Decimal("0")
    assert cfg.currency is None
    assert cfg.max_per_hour == 20
    assert cfg.max_attempts == 3
    assert cfg.username_prefix == "supporter-"
    assert cfg.public_endpoint == "https://facade.example.org"
    assert cfg.smtp_host == "smtp.example.org"
    assert cfg.smtp_port == 587
    assert cfg.smtp_username is None
    assert cfg.smtp_password is None
    assert cfg.smtp_from == "noreply@example.org"
    assert cfg.smtp_mode == "starttls"
    assert cfg.smtp_timeout == 15
    assert cfg.notify is None


def test_signature_header_is_overridable(settings_env):
    env = _supporter_env(settings_env, NETNL_BMC_SIGNATURE_HEADER="X-Bmc-Signature")
    s = load(env)
    assert s.supporter.signature_header == "X-Bmc-Signature"


def test_max_body_bytes_overridable_and_numeric(settings_env):
    env = _supporter_env(settings_env, NETNL_BMC_MAX_BODY_BYTES="1024")
    s = load(env)
    assert s.supporter.max_body_bytes == 1024


def test_max_body_bytes_rejects_non_numeric(settings_env):
    env = _supporter_env(settings_env, NETNL_BMC_MAX_BODY_BYTES="lots")
    with pytest.raises(SettingsError, match="NETNL_BMC_MAX_BODY_BYTES"):
        load(env)


@pytest.mark.parametrize("value", ["0", "true", "yes", "on", ""])
def test_accept_test_mode_is_false_for_anything_other_than_literal_one(settings_env, value):
    env = _supporter_env(settings_env, NETNL_BMC_ACCEPT_TEST_MODE=value)
    s = load(env)
    assert s.supporter.accept_test_mode is False


def test_accept_test_mode_true_for_literal_one(settings_env):
    env = _supporter_env(settings_env, NETNL_BMC_ACCEPT_TEST_MODE="1")
    s = load(env)
    assert s.supporter.accept_test_mode is True


def test_min_amount_parsed_as_decimal_not_float(settings_env):
    env = _supporter_env(settings_env, NETNL_SUPPORTER_MIN_AMOUNT="5.10")
    s = load(env)
    assert s.supporter.min_amount == Decimal("5.10")
    # A float round-trip of "5.10" can drift; Decimal must not.
    assert str(s.supporter.min_amount) == "5.10"


def test_min_amount_rejects_non_numeric(settings_env):
    env = _supporter_env(settings_env, NETNL_SUPPORTER_MIN_AMOUNT="lots")
    with pytest.raises(SettingsError, match="NETNL_SUPPORTER_MIN_AMOUNT"):
        load(env)


def test_min_amount_rejects_negative(settings_env):
    env = _supporter_env(settings_env, NETNL_SUPPORTER_MIN_AMOUNT="-1")
    with pytest.raises(SettingsError, match="NETNL_SUPPORTER_MIN_AMOUNT"):
        load(env)


def test_currency_unset_accepts_any(settings_env):
    s = load(_supporter_env(settings_env))
    assert s.supporter.currency is None


def test_currency_is_uppercased_for_case_insensitive_match(settings_env):
    env = _supporter_env(settings_env, NETNL_SUPPORTER_CURRENCY="eur")
    s = load(env)
    assert s.supporter.currency == "EUR"


def test_max_per_hour_overridable(settings_env):
    env = _supporter_env(settings_env, NETNL_SUPPORTER_MAX_PER_HOUR="5")
    s = load(env)
    assert s.supporter.max_per_hour == 5


def test_max_per_hour_rejects_negative(settings_env):
    env = _supporter_env(settings_env, NETNL_SUPPORTER_MAX_PER_HOUR="-1")
    with pytest.raises(SettingsError, match="NETNL_SUPPORTER_MAX_PER_HOUR"):
        load(env)


def test_max_attempts_overridable(settings_env):
    env = _supporter_env(settings_env, NETNL_SUPPORTER_MAX_ATTEMPTS="1")
    s = load(env)
    assert s.supporter.max_attempts == 1


def test_username_prefix_overridable(settings_env):
    env = _supporter_env(settings_env, NETNL_SUPPORTER_USERNAME_PREFIX="bmc-")
    s = load(env)
    assert s.supporter.username_prefix == "bmc-"


def test_public_endpoint_rejects_embedded_newline(settings_env):
    env = _supporter_env(
        settings_env, NETNL_PUBLIC_ENDPOINT="https://facade.example.org\nX-Injected: 1"
    )
    with pytest.raises(SettingsError, match="NETNL_PUBLIC_ENDPOINT"):
        load(env)


def test_smtp_port_overridable_and_numeric(settings_env):
    env = _supporter_env(settings_env, NETNL_SMTP_PORT="465")
    s = load(env)
    assert s.supporter.smtp_port == 465


def test_smtp_username_and_password_optional(settings_env):
    s = load(_supporter_env(settings_env))
    assert s.supporter.smtp_username is None
    assert s.supporter.smtp_password is None


def test_smtp_username_and_password_carried_through(settings_env):
    env = _supporter_env(
        settings_env, NETNL_SMTP_USERNAME="mailer", NETNL_SMTP_PASSWORD="hunter2"
    )
    s = load(env)
    assert s.supporter.smtp_username == "mailer"
    assert s.supporter.smtp_password == "hunter2"


def test_smtp_from_rejects_embedded_carriage_return(settings_env):
    env = _supporter_env(
        settings_env, NETNL_SMTP_FROM="noreply@example.org\r\nBcc: attacker@evil.example"
    )
    with pytest.raises(SettingsError, match="NETNL_SMTP_FROM"):
        load(env)


def test_smtp_mode_defaults_to_starttls(settings_env):
    s = load(_supporter_env(settings_env))
    assert s.supporter.smtp_mode == "starttls"


@pytest.mark.parametrize("mode", ["starttls", "ssl"])
def test_smtp_mode_accepts_starttls_and_ssl_without_extra_flag(settings_env, mode):
    env = _supporter_env(settings_env, NETNL_SMTP_MODE=mode)
    s = load(env)
    assert s.supporter.smtp_mode == mode


def test_smtp_mode_rejects_unknown_value(settings_env):
    env = _supporter_env(settings_env, NETNL_SMTP_MODE="carrier-pigeon")
    with pytest.raises(SettingsError, match="NETNL_SMTP_MODE"):
        load(env)


def test_smtp_mode_plaintext_requires_explicit_allow(settings_env):
    env = _supporter_env(settings_env, NETNL_SMTP_MODE="plaintext")
    with pytest.raises(SettingsError, match="NETNL_SMTP_ALLOW_PLAINTEXT"):
        load(env)


def test_smtp_mode_plaintext_accepted_with_explicit_allow(settings_env):
    env = _supporter_env(
        settings_env, NETNL_SMTP_MODE="plaintext", NETNL_SMTP_ALLOW_PLAINTEXT="1"
    )
    s = load(env)
    assert s.supporter.smtp_mode == "plaintext"


def test_smtp_timeout_overridable(settings_env):
    env = _supporter_env(settings_env, NETNL_SMTP_TIMEOUT="5")
    s = load(env)
    assert s.supporter.smtp_timeout == 5


def test_notify_unset_by_default(settings_env):
    s = load(_supporter_env(settings_env))
    assert s.supporter.notify is None


def test_notify_carried_through(settings_env):
    env = _supporter_env(settings_env, NETNL_SUPPORTER_NOTIFY="operator@example.org")
    s = load(env)
    assert s.supporter.notify == "operator@example.org"


# --- security review round: secret floor, notify validation, non-finite ---
# --- amounts (openspec/changes/add-supporter-issuance) --------------------


def test_webhook_secret_below_32_chars_is_rejected(settings_env):
    env = _supporter_env(settings_env, NETNL_BMC_WEBHOOK_SECRET="short-secret")
    with pytest.raises(SettingsError, match="NETNL_BMC_WEBHOOK_SECRET"):
        load(env)


def test_webhook_secret_exactly_32_chars_is_accepted(settings_env):
    env = _supporter_env(settings_env, NETNL_BMC_WEBHOOK_SECRET="a" * 32)
    s = load(env)
    assert s.supporter.webhook_secret == "a" * 32


def test_notify_rejects_embedded_carriage_return(settings_env):
    env = _supporter_env(
        settings_env, NETNL_SUPPORTER_NOTIFY="operator@example.org\r\nBcc: x@evil.example"
    )
    with pytest.raises(SettingsError, match="NETNL_SUPPORTER_NOTIFY"):
        load(env)


@pytest.mark.parametrize(
    "bad_notify",
    ["not-an-email", "a b@example.org", "a@example.org, b@evil.example", "<a@example.org>"],
)
def test_notify_rejects_unusable_address_shapes(settings_env, bad_notify):
    env = _supporter_env(settings_env, NETNL_SUPPORTER_NOTIFY=bad_notify)
    with pytest.raises(SettingsError, match="NETNL_SUPPORTER_NOTIFY"):
        load(env)


@pytest.mark.parametrize("bad_amount", ["NaN", "nan", "sNaN", "Infinity", "-Infinity", "inf"])
def test_min_amount_rejects_non_finite_values(settings_env, bad_amount):
    env = _supporter_env(settings_env, NETNL_SUPPORTER_MIN_AMOUNT=bad_amount)
    with pytest.raises(SettingsError, match="NETNL_SUPPORTER_MIN_AMOUNT"):
        load(env)


def test_min_amount_accepts_a_very_large_but_finite_value(settings_env):
    # A huge but syntactically finite decimal (unlike a float, `Decimal`
    # does not overflow this to infinity) is not itself a security
    # problem — only genuinely non-finite values are rejected above.
    env = _supporter_env(settings_env, NETNL_SUPPORTER_MIN_AMOUNT="1e10000")
    s = load(env)
    assert s.supporter.min_amount.is_finite()


def test_plaintext_mode_error_mentions_the_relay_password(settings_env):
    env = _supporter_env(settings_env, NETNL_SMTP_MODE="plaintext")
    with pytest.raises(SettingsError, match="password"):
        load(env)


# --- N3: non-finite values on every supporter numeric variable -------------


@pytest.mark.parametrize("bad_value", ["nan", "inf", "-inf", "1e400"])
@pytest.mark.parametrize(
    "var",
    [
        "NETNL_SMTP_TIMEOUT",
        "NETNL_SUPPORTER_MAX_ATTEMPTS",
        "NETNL_SUPPORTER_MAX_PER_HOUR",
        "NETNL_SMTP_PORT",
        "NETNL_BMC_MAX_BODY_BYTES",
    ],
)
def test_supporter_numeric_vars_reject_non_finite_values(settings_env, var, bad_value):
    env = _supporter_env(settings_env, **{var: bad_value})
    with pytest.raises(SettingsError, match=var):
        load(env)


# --- N1: the pending-lease derivation and its explicit override ------------


def test_lease_seconds_default_is_derived_from_smtp_timeout(settings_env):
    env = _supporter_env(settings_env, NETNL_SMTP_TIMEOUT="15")
    s = load(env)
    # 8x the timeout (a full send is several sequential round trips, each
    # individually bounded by the timeout — measured ~5.3x end to end for
    # a successful send; 8x leaves headroom) plus a fixed margin.
    assert s.supporter.pending_lease_seconds == 15 * 8 + 30


def test_lease_seconds_scales_with_a_longer_smtp_timeout(settings_env):
    env = _supporter_env(settings_env, NETNL_SMTP_TIMEOUT="60")
    s = load(env)
    assert s.supporter.pending_lease_seconds == 60 * 8 + 30


def test_lease_seconds_explicit_override(settings_env):
    env = _supporter_env(settings_env, NETNL_SUPPORTER_LEASE_SECONDS="600")
    s = load(env)
    assert s.supporter.pending_lease_seconds == 600


def test_lease_seconds_rejects_non_finite_values(settings_env):
    env = _supporter_env(settings_env, NETNL_SUPPORTER_LEASE_SECONDS="inf")
    with pytest.raises(SettingsError, match="NETNL_SUPPORTER_LEASE_SECONDS"):
        load(env)
