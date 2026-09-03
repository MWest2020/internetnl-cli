import pytest

from internetnl_cli.config import Config, default_config_path, resolve
from internetnl_cli.errors import ConfigError


def _env(home, **overrides):
    env = {"HOME": str(home)}
    env.update(overrides)
    return env


def test_missing_endpoint_raises_config_error(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        resolve(_env(tmp_path))
    assert "INTERNETNL_ENDPOINT" in str(excinfo.value)


def test_env_endpoint_is_used(tmp_path):
    cfg = resolve(_env(tmp_path, INTERNETNL_ENDPOINT="https://batch.example/api/batch/v2"))
    assert cfg.endpoint == "https://batch.example/api/batch/v2"


def test_env_beats_config_file(tmp_path):
    config_dir = tmp_path / ".config" / "internetnl"
    config_dir.mkdir(parents=True)
    (config_dir / "config.ini").write_text("[internetnl]\nendpoint = https://from-file.example/api/batch/v2\n")
    cfg = resolve(_env(tmp_path, INTERNETNL_ENDPOINT="https://from-env.example/api/batch/v2"))
    assert cfg.endpoint == "https://from-env.example/api/batch/v2"


def test_config_file_only_endpoint_via_internetnl_config(tmp_path):
    config_file = tmp_path / "custom-config.ini"
    config_file.write_text("[internetnl]\nendpoint = https://from-file.example/api/batch/v2\n")
    cfg = resolve(_env(tmp_path, INTERNETNL_CONFIG=str(config_file)))
    assert cfg.endpoint == "https://from-file.example/api/batch/v2"


def test_default_config_path_without_creating_it(tmp_path):
    path = default_config_path(_env(tmp_path))
    assert path == tmp_path / ".config" / "internetnl" / "config.ini"
    assert not path.exists()


def test_credential_key_in_ini_is_rejected_and_secret_not_echoed(tmp_path):
    config_dir = tmp_path / ".config" / "internetnl"
    config_dir.mkdir(parents=True)
    secret = "s3cr3t-should-not-appear"
    (config_dir / "config.ini").write_text(
        f"[internetnl]\nendpoint = https://from-file.example/api/batch/v2\npassword = {secret}\n"
    )
    with pytest.raises(ConfigError) as excinfo:
        resolve(_env(tmp_path))
    message = str(excinfo.value)
    assert secret not in message
    assert "password" in message


def test_userinfo_in_endpoint_is_rejected(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        resolve(_env(tmp_path, INTERNETNL_ENDPOINT="https://user:pass@batch.example/api/batch/v2"))
    message = str(excinfo.value)
    assert "pass" not in message
    assert "INTERNETNL_ENDPOINT" in message


def test_non_numeric_timeout_is_rejected(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        resolve(
            _env(
                tmp_path,
                INTERNETNL_ENDPOINT="https://batch.example/api/batch/v2",
                INTERNETNL_TIMEOUT="abc",
            )
        )
    assert "INTERNETNL_TIMEOUT" in str(excinfo.value)


def test_negative_poll_interval_is_rejected(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        resolve(
            _env(
                tmp_path,
                INTERNETNL_ENDPOINT="https://batch.example/api/batch/v2",
                INTERNETNL_POLL_INTERVAL="-5",
            )
        )
    assert "INTERNETNL_POLL_INTERVAL" in str(excinfo.value)


def test_endpoint_host_is_host_only():
    cfg = Config(
        endpoint="https://batch.example/api/batch/v2/",
        username="",
        password="",
        timeout=30,
        poll_interval=30,
        poll_max=3600,
        batch_size=5000,
    )
    assert cfg.endpoint_host == "batch.example"


def test_missing_config_file_is_not_an_error_but_still_needs_endpoint(tmp_path):
    with pytest.raises(ConfigError):
        resolve(_env(tmp_path, INTERNETNL_CONFIG=str(tmp_path / "does-not-exist.ini")))


def test_config_file_without_internetnl_section_is_config_error(tmp_path):
    config_file = tmp_path / "config.ini"
    config_file.write_text("[other]\nkey = value\n")
    with pytest.raises(ConfigError):
        resolve(_env(tmp_path, INTERNETNL_CONFIG=str(config_file)))


def test_unparseable_config_file_is_config_error(tmp_path):
    config_file = tmp_path / "config.ini"
    config_file.write_text("this is not valid ini [[[")
    with pytest.raises(ConfigError):
        resolve(_env(tmp_path, INTERNETNL_CONFIG=str(config_file)))


def test_defaults_applied(tmp_path):
    cfg = resolve(_env(tmp_path, INTERNETNL_ENDPOINT="https://batch.example/api/batch/v2"))
    assert cfg.timeout == 30
    assert cfg.poll_interval == 30
    assert cfg.poll_max == 3600
    assert cfg.batch_size == 5000
    assert cfg.username == ""
    assert cfg.password == ""


def test_credentials_only_from_environment(tmp_path):
    cfg = resolve(
        _env(
            tmp_path,
            INTERNETNL_ENDPOINT="https://batch.example/api/batch/v2",
            INTERNETNL_USERNAME="alice",
            INTERNETNL_PASSWORD="secret",
        )
    )
    assert cfg.username == "alice"
    assert cfg.password == "secret"


# --- INTERNETNL_CREDENTIAL: single-secret alternative to USERNAME/PASSWORD --


def test_credential_env_var_is_split_on_first_colon(tmp_path):
    cfg = resolve(
        _env(
            tmp_path,
            INTERNETNL_ENDPOINT="https://batch.example/api/batch/v2",
            INTERNETNL_CREDENTIAL="alice:secret",
        )
    )
    assert cfg.username == "alice"
    assert cfg.password == "secret"


def test_credential_env_var_password_with_colon_is_kept_whole(tmp_path):
    cfg = resolve(
        _env(
            tmp_path,
            INTERNETNL_ENDPOINT="https://batch.example/api/batch/v2",
            INTERNETNL_CREDENTIAL="alice:sec:ret",
        )
    )
    assert cfg.username == "alice"
    assert cfg.password == "sec:ret"


def test_credential_and_username_together_is_config_error(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        resolve(
            _env(
                tmp_path,
                INTERNETNL_ENDPOINT="https://batch.example/api/batch/v2",
                INTERNETNL_CREDENTIAL="alice:secret",
                INTERNETNL_USERNAME="alice",
            )
        )
    message = str(excinfo.value)
    assert "INTERNETNL_CREDENTIAL" in message
    assert "INTERNETNL_USERNAME" in message


def test_credential_and_password_together_is_config_error(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        resolve(
            _env(
                tmp_path,
                INTERNETNL_ENDPOINT="https://batch.example/api/batch/v2",
                INTERNETNL_CREDENTIAL="alice:secret",
                INTERNETNL_PASSWORD="secret",
            )
        )
    message = str(excinfo.value)
    assert "INTERNETNL_CREDENTIAL" in message
    assert "INTERNETNL_PASSWORD" in message


def test_credential_without_colon_is_config_error_and_does_not_echo_value(tmp_path):
    secret_looking_value = "not-a-valid-credential-format"
    with pytest.raises(ConfigError) as excinfo:
        resolve(
            _env(
                tmp_path,
                INTERNETNL_ENDPOINT="https://batch.example/api/batch/v2",
                INTERNETNL_CREDENTIAL=secret_looking_value,
            )
        )
    message = str(excinfo.value)
    assert secret_looking_value not in message
    assert "username:password" in message


def test_credential_not_set_falls_back_to_username_password_unchanged(tmp_path):
    cfg = resolve(
        _env(
            tmp_path,
            INTERNETNL_ENDPOINT="https://batch.example/api/batch/v2",
            INTERNETNL_USERNAME="alice",
            INTERNETNL_PASSWORD="secret",
        )
    )
    assert cfg.username == "alice"
    assert cfg.password == "secret"


def test_credential_key_in_ini_is_also_rejected_and_secret_not_echoed(tmp_path):
    config_dir = tmp_path / ".config" / "internetnl"
    config_dir.mkdir(parents=True)
    secret = "s3cr3t-should-not-appear"
    (config_dir / "config.ini").write_text(
        f"[internetnl]\nendpoint = https://from-file.example/api/batch/v2\ncredential = alice:{secret}\n"
    )
    with pytest.raises(ConfigError) as excinfo:
        resolve(_env(tmp_path))
    message = str(excinfo.value)
    assert secret not in message
    assert "credential" in message


def test_unknown_ini_keys_are_ignored(tmp_path):
    config_dir = tmp_path / ".config" / "internetnl"
    config_dir.mkdir(parents=True)
    (config_dir / "config.ini").write_text(
        "[internetnl]\nendpoint = https://from-file.example/api/batch/v2\nsomething_else = fine\n"
    )
    cfg = resolve(_env(tmp_path))
    assert cfg.endpoint == "https://from-file.example/api/batch/v2"


# --- M1: http endpoints send credentials in cleartext ------------------------


def test_http_endpoint_without_opt_in_is_config_error(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        resolve(_env(tmp_path, INTERNETNL_ENDPOINT="http://batch.example/api/batch/v2"))
    assert "INTERNETNL_ALLOW_HTTP" in str(excinfo.value)


def test_http_endpoint_with_opt_in_works(tmp_path):
    cfg = resolve(
        _env(
            tmp_path,
            INTERNETNL_ENDPOINT="http://batch.example/api/batch/v2",
            INTERNETNL_ALLOW_HTTP="1",
        )
    )
    assert cfg.endpoint == "http://batch.example/api/batch/v2"


# --- m3: empty/absent $HOME never degrades to a CWD-relative config path ----


def test_default_config_path_with_empty_home_is_none():
    assert default_config_path({}) is None
    assert default_config_path({"HOME": ""}) is None


def test_resolve_with_empty_home_does_not_read_a_cwd_relative_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config_dir = tmp_path / ".config" / "internetnl"
    config_dir.mkdir(parents=True)
    (config_dir / "config.ini").write_text(
        "[internetnl]\nendpoint = https://should-not-be-read.example/api/batch/v2\n"
    )
    with pytest.raises(ConfigError) as excinfo:
        resolve({"HOME": ""})
    assert "INTERNETNL_ENDPOINT" in str(excinfo.value)
