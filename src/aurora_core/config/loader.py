"""Safe YAML, environment, and command-line configuration loading."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from os import environ
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from aurora_core.config.errors import (
    ConfigurationFileError,
    ConfigurationValidationError,
)
from aurora_core.config.models import AuroraSettings

ConfigMapping = Mapping[str, Any]

_ENVIRONMENT_FIELDS: dict[
    tuple[str, ...], type[bool] | type[int] | type[float] | type[str]
] = {
    ("application", "name"): str,
    ("application", "configuration_profile"): str,
    ("logging", "level"): str,
    ("logging", "structured_output"): bool,
    ("hyperhdr", "enabled"): bool,
    ("hyperhdr", "host"): str,
    ("hyperhdr", "port"): int,
    ("wled", "enabled"): bool,
    ("wled", "host"): str,
    ("wled", "port"): int,
    ("wled", "validation_timeout_seconds"): float,
    ("ddp", "enabled"): bool,
    ("ddp", "host"): str,
    ("ddp", "port"): int,
    ("capture_device", "enabled"): bool,
    ("capture_device", "identifier"): str,
    ("led_layout", "orientation"): str,
    ("led_layout", "starting_corner"): str,
    ("mqtt", "enabled"): bool,
    ("mqtt", "host"): str,
    ("mqtt", "port"): int,
    ("mqtt", "username"): str,
    ("mqtt", "password"): str,
}


def deep_merge(base: ConfigMapping, override: ConfigMapping) -> dict[str, Any]:
    """Return a non-mutating recursive merge in which ``override`` wins."""
    result = deepcopy(dict(base))
    for key, value in override.items():
        if isinstance(result.get(key), Mapping) and isinstance(value, Mapping):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def load_yaml_file(path: Path) -> dict[str, Any]:
    """Read one explicitly requested YAML file using PyYAML's safe loader."""
    try:
        with path.open(encoding="utf-8") as config_file:
            document = yaml.safe_load(config_file)
    except FileNotFoundError as error:
        raise ConfigurationFileError(f"Configuration file not found: {path}") from error
    except PermissionError as error:
        raise ConfigurationFileError(
            f"Configuration file is unreadable: {path}"
        ) from error
    except OSError as error:
        raise ConfigurationFileError(
            f"Configuration file could not be read: {path}"
        ) from error
    except yaml.YAMLError as error:
        raise ConfigurationFileError(
            f"Configuration file contains malformed YAML: {path}"
        ) from error

    if document is None:
        return {}
    if not isinstance(document, dict):
        raise ConfigurationFileError("Configuration YAML root must be a mapping")
    return document


def environment_overrides(source: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Extract and type supported ``AURORA_`` environment variables."""
    source = environ if source is None else source
    overrides: dict[str, Any] = {}
    for path, target_type in _ENVIRONMENT_FIELDS.items():
        variable = "AURORA_" + "__".join(part.upper() for part in path)
        if variable not in source:
            continue
        value = _parse_environment_value(variable, source[variable], target_type)
        nested: dict[str, Any] = overrides
        for part in path[:-1]:
            nested = nested.setdefault(part, {})
        nested[path[-1]] = value
    return overrides


def _parse_environment_value(variable: str, value: str, target_type: type[Any]) -> Any:
    if target_type is str:
        return value
    if target_type is float:
        try:
            return float(value)
        except ValueError as error:
            raise ConfigurationValidationError(
                f"Invalid float value for environment variable {variable}"
            ) from error
    if target_type is int:
        try:
            return int(value)
        except ValueError as error:
            raise ConfigurationValidationError(
                f"Invalid integer value for environment variable {variable}"
            ) from error
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationValidationError(
        f"Invalid boolean value for environment variable {variable}"
    )


def load_settings(
    *,
    config_path: Path | None = None,
    environment: Mapping[str, str] | None = None,
    cli_overrides: ConfigMapping | None = None,
) -> AuroraSettings:
    """Load settings with precedence CLI > environment > YAML > defaults."""
    merged: dict[str, Any] = {}
    if config_path is not None:
        merged = deep_merge(merged, load_yaml_file(config_path))
    merged = deep_merge(merged, environment_overrides(environment))
    if cli_overrides is not None:
        merged = deep_merge(merged, cli_overrides)
    try:
        # model_validate deliberately validates only our merged sources. BaseSettings'
        # constructor source order would otherwise put YAML init values above env.
        return AuroraSettings.model_validate(merged)
    except ValidationError as error:
        raise ConfigurationValidationError(_safe_validation_message(error)) from error


def _safe_validation_message(error: ValidationError) -> str:
    details = []
    for item in error.errors(include_input=False):
        location = ".".join(str(part) for part in item["loc"])
        details.append(f"{location}: {item['msg']}")
    return "Invalid configuration: " + "; ".join(details)
