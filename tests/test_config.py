"""Tests for the hardware-free Aurora configuration foundation."""

from __future__ import annotations

from pathlib import Path

import pytest

from aurora_core.config import AuroraConfigurationError, deep_merge, load_settings
from aurora_core.config.models import AuroraSettings


def test_safe_defaults_load() -> None:
    settings = load_settings(environment={})
    assert settings.application.name == "Project Aurora"
    assert not settings.wled.enabled
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


def test_settings_model_is_pydantic_settings_model() -> None:
    assert AuroraSettings.model_config["env_prefix"] == "AURORA_"
