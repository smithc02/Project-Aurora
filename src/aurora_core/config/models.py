"""Validated, hardware-independent configuration models for Aurora."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

NonEmptyString = Annotated[str, Field(min_length=1, strict=True)]
Port = Annotated[int, Field(ge=1, le=65535, strict=True)]
PositiveInteger = Annotated[int, Field(gt=0, strict=True)]
StrictBoolean = Annotated[bool, Field(strict=True)]
ValidationTimeout = Annotated[float, Field(ge=0.1, le=10.0, strict=True)]


class LoggingLevel(StrEnum):
    """Supported application logging levels."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class AuroraModel(BaseModel):
    """Base model that rejects unrecognised configuration fields."""

    model_config = ConfigDict(extra="forbid")


class ApplicationSettings(AuroraModel):
    name: NonEmptyString = "Project Aurora"
    configuration_profile: NonEmptyString = "default"


class LoggingSettings(AuroraModel):
    level: LoggingLevel = LoggingLevel.INFO
    structured_output: StrictBoolean = False


class EndpointSettings(AuroraModel):
    enabled: StrictBoolean = False
    host: NonEmptyString | None = None
    port: Port | None = None

    @field_validator("host")
    @classmethod
    def host_is_hostname_or_ip_literal(cls, value: str | None) -> str | None:
        if value is not None and any(
            token in value for token in ("://", "/", "?", "#", "@")
        ):
            raise ValueError("host must be a hostname or IP literal without URL syntax")
        return value

    @model_validator(mode="after")
    def enabled_endpoint_has_host(self) -> EndpointSettings:
        if self.enabled and self.host is None:
            raise ValueError("host is required when enabled is true")
        return self


class HyperHDRSettings(EndpointSettings):
    """Description-only HyperHDR settings; no connection is attempted."""

    validation_timeout_seconds: ValidationTimeout = 2.0


class WLEDSettings(EndpointSettings):
    """WLED configuration for explicit, read-only information validation only."""

    validation_timeout_seconds: ValidationTimeout = 2.0


class DDPSettings(EndpointSettings):
    """Description-only DDP settings; no transmission is attempted."""


class CaptureDeviceSettings(AuroraModel):
    enabled: StrictBoolean = False
    identifier: NonEmptyString | None = None

    @field_validator("identifier")
    @classmethod
    def identifier_is_supported_linux_path(cls, value: str | None) -> str | None:
        """Validate only the supported identifier grammar; never inspect paths."""
        if value is None:
            return value
        if any(character in value for character in ("\x00", "\n", "\r")):
            raise ValueError("identifier contains unsafe control characters")
        if any(character in value for character in ("?", "#", "@", "*", "[", "]")):
            raise ValueError(
                "identifier must not use URL, credential, or wildcard syntax"
            )
        if "://" in value or value.startswith("~") or ".." in value.split("/"):
            raise ValueError(
                "identifier must be an absolute non-traversing device path"
            )
        if re.fullmatch(r"/dev/video[0-9]+", value) or re.fullmatch(
            r"/dev/v4l/(?:by-id|by-path)/[^/]+", value
        ):
            return value
        raise ValueError("identifier must be /dev/videoN or one stable /dev/v4l link")

    @model_validator(mode="after")
    def enabled_device_has_identifier(self) -> CaptureDeviceSettings:
        if self.enabled and self.identifier is None:
            raise ValueError("identifier is required when enabled is true")
        return self


class LightingZoneSettings(AuroraModel):
    name: NonEmptyString
    enabled: StrictBoolean = False
    led_count: PositiveInteger | None = None


class LEDLayoutSettings(AuroraModel):
    orientation: NonEmptyString | None = None
    starting_corner: NonEmptyString | None = None


class MQTTSettings(EndpointSettings):
    username: NonEmptyString | None = None
    password: SecretStr | None = None


class AuroraSettings(BaseSettings):
    """Complete validated Aurora configuration with safe, disabled defaults."""

    model_config = SettingsConfigDict(
        extra="forbid",
        env_prefix="AURORA_",
        env_nested_delimiter="__",
    )

    application: ApplicationSettings = Field(default_factory=ApplicationSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    hyperhdr: HyperHDRSettings = Field(default_factory=HyperHDRSettings)
    wled: WLEDSettings = Field(default_factory=WLEDSettings)
    ddp: DDPSettings = Field(default_factory=DDPSettings)
    capture_device: CaptureDeviceSettings = Field(default_factory=CaptureDeviceSettings)
    lighting_zones: tuple[LightingZoneSettings, ...] = ()
    led_layout: LEDLayoutSettings = Field(default_factory=LEDLayoutSettings)
    mqtt: MQTTSettings = Field(default_factory=MQTTSettings)
