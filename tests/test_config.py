"""Tests for the hardware-free Aurora configuration foundation."""

from __future__ import annotations

import os
from functools import cache
from pathlib import Path

import pytest

from aurora_core.config import AuroraConfigurationError, deep_merge, load_settings
from aurora_core.config.models import AuroraSettings, WLEDOperation
from aurora_core.security.passwords import hash_password


def _clear_aurora_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name.startswith("AURORA_"):
            monkeypatch.delenv(name, raising=False)


def _write_enabled_wled_yaml(tmp_path: Path) -> Path:
    path = tmp_path / "aurora.yaml"
    path.write_text(
        "wled:\n"
        "  enabled: true\n"
        "  host: yaml-device.invalid\n"
        "  controls:\n"
        "    enabled: false\n"
        "    allowed_operations:\n"
        "      - wled.power_off\n"
        "    timeout_seconds: 1.0\n"
        "    maximum_brightness: 64\n"
        "    operation_limit: 2\n"
        "    operation_window_seconds: 5\n"
    )
    return path


@cache
def _authentication_hash() -> str:
    return hash_password("environment-test-password", salt=bytes(range(16)))


def test_safe_defaults_load() -> None:
    settings = load_settings(environment={})
    assert settings.application.name == "Project Aurora"
    assert not settings.wled.enabled
    assert settings.wled.expected_led_count is None
    assert settings.wled.expected_active_led_count is None
    assert settings.wled.expected_skipped_leds is None
    assert settings.dashboard.bind_host == "localhost"
    assert settings.dashboard.port == 8080
    assert settings.lighting_zones == ()


def test_repository_example_loads() -> None:
    settings = load_settings(
        config_path=Path("configs/aurora.example.yaml"), environment={}
    )
    assert settings.application.configuration_profile == "example"


def test_yaml_environment_and_cli_precedence(tmp_path: Path) -> None:
    path = tmp_path / "aurora.yaml"
    path.write_text(
        "logging:\n  level: INFO\nwled:\n  enabled: false\n  host: yaml.local\n"
    )
    settings = load_settings(
        config_path=path,
        environment={
            "AURORA_LOGGING__LEVEL": "WARNING",
            "AURORA_WLED__ENABLED": "true",
        },
        cli_overrides={"logging": {"level": "DEBUG"}},
    )
    assert settings.logging.level == "DEBUG"
    assert settings.wled.enabled
    assert settings.wled.host == "yaml.local"


def test_in_memory_yaml_uses_existing_environment_and_cli_precedence() -> None:
    settings = load_settings(
        config_data={"logging": {"level": "INFO"}},
        environment={"AURORA_LOGGING__LEVEL": "WARNING"},
        cli_overrides={"logging": {"level": "DEBUG"}},
    )
    assert settings.logging.level == "DEBUG"


def test_configuration_path_and_in_memory_yaml_are_mutually_exclusive(
    tmp_path: Path,
) -> None:
    path = tmp_path / "aurora.yaml"
    path.write_text("{}")
    with pytest.raises(AuroraConfigurationError, match="one YAML configuration source"):
        load_settings(config_path=path, config_data={})


@pytest.mark.parametrize(
    ("content", "message"),
    [("[unclosed", "malformed YAML"), ("- item", "root must be a mapping")],
)
def test_invalid_yaml_is_rejected(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(content)
    with pytest.raises(AuroraConfigurationError, match=message):
        load_settings(config_path=path, environment={})


def test_missing_requested_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(AuroraConfigurationError, match="not found"):
        load_settings(config_path=tmp_path / "missing.yaml", environment={})


@pytest.mark.parametrize(
    "data",
    [
        {"unexpected": True},
        {"wled": {"port": 0}},
        {"lighting_zones": [{"name": "rear", "led_count": 0}]},
        {"logging": {"level": "VERBOSE"}},
        {"wled": {"host": ""}},
        {"lighting_zones": [{"name": ""}]},
        {"dashboard": {"bind_host": "http://bad"}},
        {"dashboard": {"port": 0}},
        {"dashboard": {"refresh_seconds": 0}},
        {"dashboard": {"memory_warning_percent": 100.1}},
        {
            "wled": {
                "expected_led_count": 8,
                "expected_active_led_count": 7,
                "expected_skipped_leds": 2,
            }
        },
    ],
)
def test_invalid_settings_are_rejected(data: dict[str, object]) -> None:
    with pytest.raises(AuroraConfigurationError):
        load_settings(environment={}, cli_overrides=data)


def test_invalid_boolean_environment_value_is_rejected() -> None:
    with pytest.raises(AuroraConfigurationError, match="AURORA_WLED__ENABLED"):
        load_settings(environment={"AURORA_WLED__ENABLED": "perhaps"})


@pytest.mark.parametrize(
    "data",
    [
        {"wled": {"enabled": True}},
        {"hyperhdr": {"enabled": True}},
        {"ddp": {"enabled": True}},
        {"mqtt": {"enabled": True}},
        {"capture_device": {"enabled": True}},
    ],
)
def test_enabled_integrations_need_descriptive_field(data: dict[str, object]) -> None:
    with pytest.raises(AuroraConfigurationError):
        load_settings(environment={}, cli_overrides=data)


def test_disabled_integrations_allow_unset_optional_fields() -> None:
    settings = load_settings(environment={}, cli_overrides={"wled": {"enabled": False}})
    assert settings.wled.host is None


def test_secret_is_redacted_from_repr_and_validation_errors() -> None:
    settings = load_settings(
        environment={},
        cli_overrides={"mqtt": {"password": "not-a-real-secret"}},
    )
    assert "not-a-real-secret" not in repr(settings)
    with pytest.raises(AuroraConfigurationError) as error:
        load_settings(
            environment={},
            cli_overrides={"mqtt": {"password": "not-a-real-secret", "port": 0}},
        )
    assert "not-a-real-secret" not in str(error.value)


def test_deep_merge_does_not_mutate_inputs() -> None:
    base = {"wled": {"enabled": False, "host": "yaml.local"}}
    override = {"wled": {"enabled": True}}
    assert deep_merge(base, override) == {
        "wled": {"enabled": True, "host": "yaml.local"}
    }
    assert base["wled"]["enabled"] is False
    assert override["wled"] == {"enabled": True}


def test_settings_model_does_not_read_environment_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_aurora_environment(monkeypatch)
    monkeypatch.setenv("AURORA_LOGGING__LEVEL", "DEBUG")

    assert AuroraSettings().logging.level == "INFO"
    assert load_settings().logging.level == "DEBUG"


def test_dashboard_and_expected_led_environment_values_are_validated() -> None:
    settings = load_settings(
        environment={
            "AURORA_DASHBOARD__BIND_HOST": "dashboard.local",
            "AURORA_DASHBOARD__PORT": "9090",
            "AURORA_DASHBOARD__REFRESH_SECONDS": "10",
            "AURORA_WLED__EXPECTED_LED_COUNT": "8",
            "AURORA_WLED__EXPECTED_ACTIVE_LED_COUNT": "6",
            "AURORA_WLED__EXPECTED_SKIPPED_LEDS": "2",
        }
    )
    assert settings.dashboard.bind_host == "dashboard.local"
    assert settings.dashboard.port == 9090
    assert settings.dashboard.refresh_seconds == 10
    assert settings.wled.expected_led_count == 8


def test_real_environment_wled_allowlist_overrides_yaml(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_aurora_environment(monkeypatch)
    path = _write_enabled_wled_yaml(tmp_path)
    monkeypatch.setenv("AURORA_WLED__CONTROLS__ENABLED", "true")
    monkeypatch.setenv(
        "AURORA_WLED__CONTROLS__ALLOWED_OPERATIONS",
        "wled.power_on,wled.power_off,wled.brightness_set",
    )
    monkeypatch.setenv("AURORA_WLED__CONTROLS__TIMEOUT_SECONDS", "2.0")
    monkeypatch.setenv("AURORA_WLED__CONTROLS__MAXIMUM_BRIGHTNESS", "255")
    monkeypatch.setenv("AURORA_WLED__CONTROLS__OPERATION_LIMIT", "20")
    monkeypatch.setenv("AURORA_WLED__CONTROLS__OPERATION_WINDOW_SECONDS", "60")

    settings = load_settings(config_path=path)

    assert settings.wled.controls.enabled
    assert settings.wled.controls.allowed_operations == (
        WLEDOperation.POWER_ON,
        WLEDOperation.POWER_OFF,
        WLEDOperation.BRIGHTNESS_SET,
    )
    assert settings.wled.controls.timeout_seconds == 2.0
    assert settings.wled.controls.maximum_brightness == 255
    assert settings.wled.controls.operation_limit == 20
    assert settings.wled.controls.operation_window_seconds == 60


def test_real_environment_empty_wled_allowlist_is_empty_tuple(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_aurora_environment(monkeypatch)
    path = _write_enabled_wled_yaml(tmp_path)
    monkeypatch.setenv("AURORA_WLED__CONTROLS__ALLOWED_OPERATIONS", "")

    settings = load_settings(config_path=path)

    assert settings.wled.controls.allowed_operations == ()


@pytest.mark.parametrize(
    "allowlist",
    (
        "wled.unknown",
        "wled.power_on,wled.power_on",
        "wled.power_on,,wled.power_off",
        "wled.power_on, ,wled.power_off",
    ),
)
def test_real_environment_invalid_wled_allowlists_fail_safely(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    allowlist: str,
) -> None:
    _clear_aurora_environment(monkeypatch)
    path = _write_enabled_wled_yaml(tmp_path)
    monkeypatch.setenv("AURORA_WLED__CONTROLS__ALLOWED_OPERATIONS", allowlist)

    with pytest.raises(AuroraConfigurationError):
        load_settings(config_path=path)


def test_real_dashboard_authentication_environment_is_authoritative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_aurora_environment(monkeypatch)
    monkeypatch.setenv("AURORA_DASHBOARD__AUTHENTICATION__ENABLED", "true")
    monkeypatch.setenv(
        "AURORA_DASHBOARD__AUTHENTICATION__USERNAME", "environment_operator"
    )
    monkeypatch.setenv(
        "AURORA_DASHBOARD__AUTHENTICATION__PASSWORD_HASH", _authentication_hash()
    )

    settings = load_settings()

    assert settings.dashboard.authentication.enabled
    assert settings.dashboard.authentication.username == "environment_operator"
    assert settings.dashboard.authentication.password_hash is not None


def test_yaml_only_wled_allowlist_still_loads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_aurora_environment(monkeypatch)
    path = _write_enabled_wled_yaml(tmp_path)

    settings = load_settings(config_path=path)

    assert settings.wled.controls.allowed_operations == (WLEDOperation.POWER_OFF,)
    assert settings.wled.controls.maximum_brightness == 64


def test_cli_overrides_real_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_aurora_environment(monkeypatch)
    path = _write_enabled_wled_yaml(tmp_path)
    monkeypatch.setenv("AURORA_WLED__CONTROLS__MAXIMUM_BRIGHTNESS", "200")

    settings = load_settings(
        config_path=path,
        cli_overrides={"wled": {"controls": {"maximum_brightness": 128}}},
    )

    assert settings.wled.controls.maximum_brightness == 128


def test_real_environment_validation_errors_do_not_echo_values_or_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _clear_aurora_environment(monkeypatch)
    path = _write_enabled_wled_yaml(tmp_path)
    malformed_allowlist = "private-operation-canary,wled.power_off"
    secret = "private-environment-secret-canary"
    monkeypatch.setenv("AURORA_WLED__CONTROLS__ALLOWED_OPERATIONS", malformed_allowlist)
    monkeypatch.setenv("AURORA_MQTT__PASSWORD", secret)

    with pytest.raises(AuroraConfigurationError) as error:
        load_settings(config_path=path)

    message = str(error.value)
    assert malformed_allowlist not in message
    assert "private-operation-canary" not in message
    assert secret not in message
