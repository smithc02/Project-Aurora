"""Tests for the disabled-by-default Milestone 18 configuration contract."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from aurora_core.config import (
    AuroraConfigurationError,
    AuroraSettings,
    HealthHistoryDatabaseMode,
    HealthHistorySettings,
    load_settings,
)


def _enabled_history(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "enabled": True,
        "database_path": "/var/lib/aurora/history.sqlite3",
    }
    values.update(overrides)
    return values


def test_history_configuration_defaults_are_disabled() -> None:
    settings = AuroraSettings()

    assert not settings.health_history.enabled
    assert settings.health_history.database_path is None
    assert (
        settings.health_history.database_mode is HealthHistoryDatabaseMode.OPEN_EXISTING
    )
    assert settings.health_history.sample_interval_seconds == 30
    assert settings.health_history.retention_days == 30


def test_existing_configuration_without_history_section_uses_defaults() -> None:
    settings = load_settings(
        config_data={"dashboard": {"refresh_seconds": 60}}, environment={}
    )

    assert not settings.health_history.enabled
    assert settings.health_history.sample_interval_seconds == 30


@pytest.mark.parametrize(
    "mode",
    (
        HealthHistoryDatabaseMode.OPEN_EXISTING,
        HealthHistoryDatabaseMode.CREATE_IF_MISSING,
        "open_existing",
        "create_if_missing",
    ),
)
def test_enabled_history_accepts_both_fixed_database_modes(
    mode: HealthHistoryDatabaseMode | str,
) -> None:
    settings = AuroraSettings.model_validate(
        {"health_history": _enabled_history(database_mode=mode)}
    )

    assert settings.health_history.database_mode == mode


def test_enabled_history_requires_database_path() -> None:
    with pytest.raises(ValidationError, match="database_path is required"):
        AuroraSettings.model_validate({"health_history": {"enabled": True}})


@pytest.mark.parametrize("mode", ("unknown", 1, True, object()))
def test_history_database_mode_rejects_unknown_values(mode: object) -> None:
    with pytest.raises(ValidationError):
        HealthHistorySettings.model_validate({"database_mode": mode})


@pytest.mark.parametrize("value", (1, 0, "true", "false"))
def test_history_enabled_is_strict_boolean(value: object) -> None:
    with pytest.raises(ValidationError):
        HealthHistorySettings.model_validate({"enabled": value})


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("sample_interval_seconds", True),
        ("sample_interval_seconds", 30.0),
        ("sample_interval_seconds", "30"),
        ("retention_days", True),
        ("retention_days", 30.0),
        ("retention_days", "30"),
        ("database_path", 123),
        ("database_path", Path("/var/lib/aurora/history.sqlite3")),
    ),
)
def test_history_scalar_fields_reject_type_coercion(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        HealthHistorySettings.model_validate({field: value})


@pytest.mark.parametrize("value", (5, 300))
def test_history_sample_interval_accepts_fixed_boundaries(value: int) -> None:
    assert (
        HealthHistorySettings(sample_interval_seconds=value).sample_interval_seconds
        == value
    )


@pytest.mark.parametrize("value", (4, 301))
def test_history_sample_interval_rejects_outside_fixed_boundaries(value: int) -> None:
    with pytest.raises(ValidationError):
        HealthHistorySettings(sample_interval_seconds=value)


@pytest.mark.parametrize("value", (1, 365))
def test_history_retention_accepts_fixed_boundaries(value: int) -> None:
    assert HealthHistorySettings(retention_days=value).retention_days == value


@pytest.mark.parametrize("value", (0, 366))
def test_history_retention_rejects_outside_fixed_boundaries(value: int) -> None:
    with pytest.raises(ValidationError):
        HealthHistorySettings(retention_days=value)


@pytest.mark.parametrize(
    ("sample_interval", "refresh_interval"),
    ((5, 5), (6, 5), (300, 300)),
)
def test_enabled_history_sampling_is_compatible_with_dashboard_refresh(
    sample_interval: int, refresh_interval: int
) -> None:
    settings = AuroraSettings.model_validate(
        {
            "dashboard": {"refresh_seconds": refresh_interval},
            "health_history": _enabled_history(sample_interval_seconds=sample_interval),
        }
    )

    assert settings.health_history.sample_interval_seconds == sample_interval


def test_enabled_history_rejects_sampling_below_dashboard_refresh() -> None:
    with pytest.raises(ValidationError, match="at least the dashboard refresh"):
        AuroraSettings.model_validate(
            {
                "dashboard": {"refresh_seconds": 6},
                "health_history": _enabled_history(sample_interval_seconds=5),
            }
        )


def test_disabled_history_does_not_restrict_dashboard_refresh() -> None:
    settings = AuroraSettings.model_validate(
        {
            "dashboard": {"refresh_seconds": 300},
            "health_history": {
                "enabled": False,
                "sample_interval_seconds": 5,
            },
        }
    )

    assert settings.dashboard.refresh_seconds == 300
    assert settings.health_history.sample_interval_seconds == 5


@pytest.mark.parametrize(
    "database_path",
    (
        "/var/lib/aurora/history.sqlite3",
        "/srv/aurora/history.db",
        "/history.db",
        "/" + "a" * 4095,
    ),
)
def test_history_database_path_accepts_fixed_lexical_grammar(
    database_path: str,
) -> None:
    settings = HealthHistorySettings(database_path=database_path)

    assert settings.database_path == database_path
    assert type(settings.database_path) is str


@pytest.mark.parametrize(
    "database_path",
    (
        "",
        "/",
        "relative/history.db",
        "~",
        "~/history.db",
        "./history.db",
        "../history.db",
        "/var/../tmp/history.db",
        "/var/./history.db",
        "/var//lib/history.db",
        "/var/lib/history.db/",
        "/var/lib/private\x00history.db",
        "/var/lib/private\x01history.db",
        "/var/lib/private\nhistory.db",
        "/var/lib/private\rhistory.db",
        "/var/lib/private\x1fhistory.db",
        "/var/lib/private\x7fhistory.db",
        "/" + "a" * 4096,
    ),
)
def test_history_database_path_rejects_unsafe_lexical_forms(
    database_path: str,
) -> None:
    with pytest.raises(ValidationError):
        HealthHistorySettings(database_path=database_path)


def test_history_database_path_error_does_not_disclose_rejected_value() -> None:
    private_token = "private-history-path-canary"
    rejected_path = f"/srv/{private_token}/../history.db"

    with pytest.raises(AuroraConfigurationError) as error:
        load_settings(
            config_data={
                "health_history": {
                    "enabled": True,
                    "database_path": rejected_path,
                }
            },
            environment={},
        )

    assert private_token not in str(error.value)
    assert rejected_path not in str(error.value)


def test_history_environment_path_error_does_not_disclose_rejected_value() -> None:
    private_token = "private-environment-history-canary"
    rejected_path = f"/srv/{private_token}/../history.db"

    with pytest.raises(AuroraConfigurationError) as error:
        load_settings(
            environment={
                "AURORA_HEALTH_HISTORY__ENABLED": "true",
                "AURORA_HEALTH_HISTORY__DATABASE_PATH": rejected_path,
            }
        )

    assert private_token not in str(error.value)
    assert rejected_path not in str(error.value)


def test_all_fixed_history_environment_fields_parse() -> None:
    settings = load_settings(
        environment={
            "AURORA_HEALTH_HISTORY__ENABLED": "true",
            "AURORA_HEALTH_HISTORY__DATABASE_PATH": "/srv/aurora/history.db",
            "AURORA_HEALTH_HISTORY__DATABASE_MODE": "create_if_missing",
            "AURORA_HEALTH_HISTORY__SAMPLE_INTERVAL_SECONDS": "45",
            "AURORA_HEALTH_HISTORY__RETENTION_DAYS": "60",
        }
    )

    assert settings.health_history.enabled
    assert type(settings.health_history.database_path) is str
    assert settings.health_history.database_path == "/srv/aurora/history.db"
    assert (
        settings.health_history.database_mode
        is HealthHistoryDatabaseMode.CREATE_IF_MISSING
    )
    assert settings.health_history.sample_interval_seconds == 45
    assert settings.health_history.retention_days == 60


@pytest.mark.parametrize(
    ("variable", "value", "kind"),
    (
        ("AURORA_HEALTH_HISTORY__ENABLED", "perhaps", "boolean"),
        ("AURORA_HEALTH_HISTORY__SAMPLE_INTERVAL_SECONDS", "5.0", "integer"),
        ("AURORA_HEALTH_HISTORY__RETENTION_DAYS", "many", "integer"),
    ),
)
def test_invalid_history_environment_scalars_fail_safely(
    variable: str, value: str, kind: str
) -> None:
    with pytest.raises(AuroraConfigurationError, match=kind) as error:
        load_settings(environment={variable: value})

    assert variable in str(error.value)
    assert value not in str(error.value)


def test_history_precedence_remains_cli_then_environment_then_yaml() -> None:
    settings = load_settings(
        config_data={
            "health_history": {
                "enabled": False,
                "database_path": "/yaml/history.db",
                "database_mode": "open_existing",
                "sample_interval_seconds": 30,
                "retention_days": 30,
            }
        },
        environment={
            "AURORA_HEALTH_HISTORY__ENABLED": "true",
            "AURORA_HEALTH_HISTORY__DATABASE_PATH": "/environment/history.db",
            "AURORA_HEALTH_HISTORY__DATABASE_MODE": "create_if_missing",
            "AURORA_HEALTH_HISTORY__SAMPLE_INTERVAL_SECONDS": "60",
            "AURORA_HEALTH_HISTORY__RETENTION_DAYS": "60",
        },
        cli_overrides={
            "health_history": {
                "database_path": "/cli/history.db",
                "sample_interval_seconds": 90,
            }
        },
    )

    assert settings.health_history.enabled
    assert settings.health_history.database_path == "/cli/history.db"
    assert (
        settings.health_history.database_mode
        is HealthHistoryDatabaseMode.CREATE_IF_MISSING
    )
    assert settings.health_history.sample_interval_seconds == 90
    assert settings.health_history.retention_days == 60


def test_history_model_validation_performs_no_filesystem_io(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    database_path = tmp_path / "unprovisioned" / "history.sqlite3"

    def unexpected_filesystem_access(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("history configuration validation touched the filesystem")

    with monkeypatch.context() as guard:
        for method in ("open", "exists", "stat", "lstat", "resolve", "mkdir"):
            guard.setattr(Path, method, unexpected_filesystem_access)
        guard.setattr(os, "stat", unexpected_filesystem_access)
        guard.setattr(os, "lstat", unexpected_filesystem_access)

        settings = AuroraSettings.model_validate(
            {"health_history": _enabled_history(database_path=str(database_path))}
        )

    assert settings.health_history.database_path == str(database_path)
    assert not database_path.exists()


def test_repository_example_keeps_history_disabled_without_path() -> None:
    settings = load_settings(
        config_path=Path("configs/aurora.example.yaml"), environment={}
    )

    assert not settings.health_history.enabled
    assert settings.health_history.database_path is None
    assert (
        settings.health_history.database_mode is HealthHistoryDatabaseMode.OPEN_EXISTING
    )
