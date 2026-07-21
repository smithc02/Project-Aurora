"""Safe exceptions raised while loading Aurora configuration."""

from __future__ import annotations


class AuroraConfigurationError(Exception):
    """Base class for configuration errors that are safe to display to users."""


class ConfigurationFileError(AuroraConfigurationError):
    """A requested configuration file could not be safely loaded."""


class ConfigurationValidationError(AuroraConfigurationError):
    """Configuration data did not satisfy the Aurora settings schema."""
