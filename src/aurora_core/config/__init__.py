"""Aurora's validated configuration public API."""

from aurora_core.config.errors import AuroraConfigurationError
from aurora_core.config.loader import deep_merge, load_settings, load_yaml_file
from aurora_core.config.models import (
    AuroraSettings,
    HealthHistoryDatabaseMode,
    HealthHistorySettings,
)

__all__ = [
    "AuroraConfigurationError",
    "AuroraSettings",
    "HealthHistoryDatabaseMode",
    "HealthHistorySettings",
    "deep_merge",
    "load_settings",
    "load_yaml_file",
]
