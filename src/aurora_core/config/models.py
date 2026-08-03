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

from aurora_core.security.passwords import PasswordHashError, validate_password_hash

NonEmptyString = Annotated[str, Field(min_length=1, strict=True)]
Port = Annotated[int, Field(ge=1, le=65535, strict=True)]
PositiveInteger = Annotated[int, Field(gt=0, strict=True)]
NonNegativeInteger = Annotated[int, Field(ge=0, strict=True)]
StrictBoolean = Annotated[bool, Field(strict=True)]
ValidationTimeout = Annotated[float, Field(ge=0.1, le=10.0, strict=True)]
RefreshInterval = Annotated[int, Field(ge=1, le=3600, strict=True)]
WarningPercentage = Annotated[float, Field(ge=0.0, le=100.0, strict=True)]
TemperatureWarning = Annotated[float, Field(ge=0.0, le=120.0, strict=True)]
AuthenticationUsername = Annotated[str, Field(min_length=1, max_length=64, strict=True)]
SessionTTLMinutes = Annotated[int, Field(ge=5, le=1440, strict=True)]
MaximumSessions = Annotated[int, Field(ge=1, le=64, strict=True)]
LoginAttemptLimit = Annotated[int, Field(ge=1, le=20, strict=True)]
LoginAttemptWindow = Annotated[int, Field(ge=30, le=3600, strict=True)]
WLEDControlTimeout = Annotated[float, Field(ge=0.1, le=5.0, strict=True)]
WLEDMaximumBrightness = Annotated[int, Field(ge=1, le=255, strict=True)]
WLEDOperationLimit = Annotated[int, Field(ge=1, le=120, strict=True)]
WLEDOperationWindow = Annotated[int, Field(ge=1, le=3600, strict=True)]
HyperHDRControlTimeout = Annotated[float, Field(ge=0.1, le=5.0, strict=True)]
HyperHDROperationLimit = Annotated[int, Field(ge=1, le=120, strict=True)]
HyperHDROperationWindow = Annotated[int, Field(ge=1, le=3600, strict=True)]


class LoggingLevel(StrEnum):
    """Supported application logging levels."""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class WLEDOperation(StrEnum):
    """The complete Milestone 15 WLED mutation allowlist."""

    POWER_ON = "wled.power_on"
    POWER_OFF = "wled.power_off"
    BRIGHTNESS_SET = "wled.brightness_set"


class HyperHDROperation(StrEnum):
    """The complete Milestone 16 HyperHDR mutation allowlist."""

    VIDEO_GRABBER_ENABLE = "hyperhdr.video_grabber_enable"
    VIDEO_GRABBER_DISABLE = "hyperhdr.video_grabber_disable"
    LED_OUTPUT_ENABLE = "hyperhdr.led_output_enable"
    LED_OUTPUT_DISABLE = "hyperhdr.led_output_disable"


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


class HyperHDRControlSettings(AuroraModel):
    """Fail-closed settings for bounded HyperHDR component-state mutations."""

    enabled: StrictBoolean = False
    allowed_operations: tuple[HyperHDROperation, ...] = ()
    timeout_seconds: HyperHDRControlTimeout = 2.0
    operation_limit: HyperHDROperationLimit = 20
    operation_window_seconds: HyperHDROperationWindow = 60

    @field_validator("allowed_operations", mode="before")
    @classmethod
    def operation_allowlist_is_unique(cls, value: object) -> object:
        if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
            raise ValueError(
                "allowed_operations must be a list of operation identifiers"
            )
        if len(value) != len(set(value)):
            raise ValueError("allowed_operations must not contain duplicates")
        return value


class HyperHDRSettings(EndpointSettings):
    """HyperHDR health and separately activated bounded-control configuration."""

    validation_timeout_seconds: ValidationTimeout = 2.0
    controls: HyperHDRControlSettings = Field(default_factory=HyperHDRControlSettings)

    @model_validator(mode="after")
    def controls_require_validated_endpoint(self) -> HyperHDRSettings:
        if self.controls.enabled and (
            not self.enabled or self.host is None or self.port is None
        ):
            raise ValueError(
                "HyperHDR must be enabled with a validated host and port before "
                "controls can be enabled"
            )
        return self


class WLEDControlSettings(AuroraModel):
    """Fail-closed settings for the bounded WLED mutation adapter."""

    enabled: StrictBoolean = False
    allowed_operations: tuple[WLEDOperation, ...] = ()
    timeout_seconds: WLEDControlTimeout = 2.0
    maximum_brightness: WLEDMaximumBrightness = 255
    operation_limit: WLEDOperationLimit = 20
    operation_window_seconds: WLEDOperationWindow = 60

    @field_validator("allowed_operations", mode="before")
    @classmethod
    def operation_allowlist_is_unique(cls, value: object) -> object:
        if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
            raise ValueError(
                "allowed_operations must be a list of operation identifiers"
            )
        if len(value) != len(set(value)):
            raise ValueError("allowed_operations must not contain duplicates")
        return value


class WLEDSettings(EndpointSettings):
    """WLED health and separately activated bounded-control configuration."""

    validation_timeout_seconds: ValidationTimeout = 2.0
    expected_led_count: PositiveInteger | None = None
    expected_active_led_count: PositiveInteger | None = None
    expected_skipped_leds: NonNegativeInteger | None = None
    controls: WLEDControlSettings = Field(default_factory=WLEDControlSettings)

    @model_validator(mode="after")
    def expected_led_counts_are_consistent(self) -> WLEDSettings:
        if self.controls.enabled and (not self.enabled or self.host is None):
            raise ValueError(
                "WLED must be enabled with a validated host before controls can be "
                "enabled"
            )
        if (
            self.expected_led_count is not None
            and self.expected_active_led_count is not None
            and self.expected_skipped_leds is not None
        ):
            if (
                self.expected_active_led_count + self.expected_skipped_leds
                != self.expected_led_count
            ):
                raise ValueError(
                    "expected active and skipped LEDs must total expected LED count"
                )
        return self


class DDPSettings(EndpointSettings):
    """DDP endpoint used only by explicit bounded output validation."""

    port: Port | None = 4048


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


class DashboardAuthenticationSettings(AuroraModel):
    """Fail-closed settings for the separate protected control plane."""

    enabled: StrictBoolean = False
    username: AuthenticationUsername | None = None
    password_hash: SecretStr | None = None
    session_ttl_minutes: SessionTTLMinutes = 480
    maximum_sessions: MaximumSessions = 16
    secure_cookie: StrictBoolean = False
    login_attempt_limit: LoginAttemptLimit = 5
    login_attempt_window_seconds: LoginAttemptWindow = 300

    @field_validator("username")
    @classmethod
    def username_uses_bounded_operator_grammar(cls, value: str | None) -> str | None:
        if (
            value is not None
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value) is None
        ):
            raise ValueError("username must use the supported operator-name grammar")
        return value

    @field_validator("password_hash")
    @classmethod
    def password_hash_is_supported(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        try:
            validate_password_hash(value.get_secret_value())
        except PasswordHashError as error:
            raise ValueError(
                "password_hash must use a supported bounded format"
            ) from error
        return value

    @model_validator(mode="after")
    def enabled_authentication_has_credentials(self) -> DashboardAuthenticationSettings:
        if self.enabled and (self.username is None or self.password_hash is None):
            raise ValueError(
                "username and password_hash are required when authentication is enabled"
            )
        return self


class DashboardServerSettings(AuroraModel):
    """Validated settings for the public portal and protected control plane."""

    bind_host: NonEmptyString = "localhost"
    port: Port = 8080
    refresh_seconds: RefreshInterval = 5
    cpu_temperature_warning_c: TemperatureWarning = 80.0
    memory_warning_percent: WarningPercentage = 90.0
    storage_warning_percent: WarningPercentage = 90.0
    authentication: DashboardAuthenticationSettings = Field(
        default_factory=DashboardAuthenticationSettings
    )

    @field_validator("bind_host")
    @classmethod
    def bind_host_has_no_url_syntax(cls, value: str) -> str:
        if any(token in value for token in ("://", "/", "?", "#", "@")) or any(
            ord(character) <= 32 or ord(character) == 127 for character in value
        ):
            raise ValueError(
                "bind_host must be a hostname or IP literal without URL syntax"
            )
        return value


class MQTTSettings(EndpointSettings):
    username: NonEmptyString | None = None
    password: SecretStr | None = None


class AuroraSettings(AuroraModel):
    """Complete validated Aurora configuration with safe, disabled defaults."""

    application: ApplicationSettings = Field(default_factory=ApplicationSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    hyperhdr: HyperHDRSettings = Field(default_factory=HyperHDRSettings)
    wled: WLEDSettings = Field(default_factory=WLEDSettings)
    ddp: DDPSettings = Field(default_factory=DDPSettings)
    capture_device: CaptureDeviceSettings = Field(default_factory=CaptureDeviceSettings)
    lighting_zones: tuple[LightingZoneSettings, ...] = ()
    led_layout: LEDLayoutSettings = Field(default_factory=LEDLayoutSettings)
    dashboard: DashboardServerSettings = Field(default_factory=DashboardServerSettings)
    mqtt: MQTTSettings = Field(default_factory=MQTTSettings)
