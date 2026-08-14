"""Strict code-owned contracts for bounded control-plane operations."""

from __future__ import annotations

from dataclasses import dataclass

from aurora_core.config.models import AuroraOperation, HyperHDROperation, WLEDOperation

WLED_ADAPTER_ID = "wled.fixed_state_adapter"
POWER_OFF_CONFIRMATION_ID = "wled.confirm_power_off"
POWER_OFF_CONFIRMATION_VALUE = "confirm_power_off"
HYPERHDR_ADAPTER_ID = "hyperhdr.fixed_component_state_adapter"
VIDEO_GRABBER_DISABLE_CONFIRMATION_ID = "hyperhdr.confirm_video_grabber_disable"
VIDEO_GRABBER_DISABLE_CONFIRMATION_VALUE = "confirm_video_grabber_disable"
LED_OUTPUT_DISABLE_CONFIRMATION_ID = "hyperhdr.confirm_led_output_disable"
LED_OUTPUT_DISABLE_CONFIRMATION_VALUE = "confirm_led_output_disable"
AURORA_AMBIENT_OFF_CONFIRMATION_ID = "aurora.confirm_ambient_off"
AURORA_AMBIENT_OFF_CONFIRMATION_VALUE = "confirm_ambient_off"


@dataclass(frozen=True, slots=True)
class NoOperationInput:
    """Typed empty input for fixed-payload power operations."""


@dataclass(frozen=True, slots=True)
class BrightnessInput:
    """Strict absolute brightness input; booleans are not integers here."""

    brightness: int

    def __post_init__(self) -> None:
        if type(self.brightness) is not int or not 1 <= self.brightness <= 255:
            raise ValueError("brightness must be an integer from 1 through 255")


OperationInput = NoOperationInput | BrightnessInput
OperationInputType = type[NoOperationInput] | type[BrightnessInput]


@dataclass(frozen=True, slots=True)
class ControlCapabilities:
    """Sanitized capabilities exposed only to an authenticated operator."""

    schema_version: int = 1
    authenticated: bool = True
    mutations_enabled: bool = False
    available_operations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "authenticated": self.authenticated,
            "mutations_enabled": self.mutations_enabled,
            "available_operations": list(self.available_operations),
        }


@dataclass(frozen=True, slots=True)
class OperationContract:
    """Code-owned metadata every executable operation must enforce."""

    operation_id: WLEDOperation
    input_model: OperationInputType
    timeout_seconds: float
    destination_adapter_id: str
    disruptive: bool
    confirmation_metadata_id: str | None = None
    authentication_required: bool = True
    csrf_required: bool = True
    audit_required: bool = True
    sanitized_errors_required: bool = True

    def __post_init__(self) -> None:
        if self.input_model not in {NoOperationInput, BrightnessInput}:
            raise ValueError("a supported strict input model must be explicit")
        if self.destination_adapter_id != WLED_ADAPTER_ID:
            raise ValueError("the fixed WLED destination adapter is required")
        if not 0.1 <= self.timeout_seconds <= 5.0:
            raise ValueError("operation timeout must be from 0.1 through 5 seconds")
        if not (
            self.authentication_required
            and self.csrf_required
            and self.audit_required
            and self.sanitized_errors_required
        ):
            raise ValueError(
                "control-operation security requirements cannot be disabled"
            )
        if (
            self.disruptive
            and self.confirmation_metadata_id != POWER_OFF_CONFIRMATION_ID
        ):
            raise ValueError("power-off requires its fixed confirmation metadata")
        if not self.disruptive and self.confirmation_metadata_id is not None:
            raise ValueError("non-disruptive operations cannot request confirmation")


@dataclass(frozen=True, slots=True)
class HyperHDROperationContract:
    """Code-owned metadata for one fixed HyperHDR component-state operation."""

    operation_id: HyperHDROperation
    input_model: type[NoOperationInput]
    timeout_seconds: float
    destination_adapter_id: str
    disruptive: bool
    confirmation_metadata_id: str | None = None
    authentication_required: bool = True
    csrf_required: bool = True
    audit_required: bool = True
    sanitized_errors_required: bool = True

    def __post_init__(self) -> None:
        if self.input_model is not NoOperationInput:
            raise ValueError("HyperHDR operations accept no browser-selected input")
        if self.destination_adapter_id != HYPERHDR_ADAPTER_ID:
            raise ValueError("the fixed HyperHDR destination adapter is required")
        if not 0.1 <= self.timeout_seconds <= 5.0:
            raise ValueError("operation timeout must be from 0.1 through 5 seconds")
        if not (
            self.authentication_required
            and self.csrf_required
            and self.audit_required
            and self.sanitized_errors_required
        ):
            raise ValueError(
                "control-operation security requirements cannot be disabled"
            )
        expected_confirmation = {
            HyperHDROperation.VIDEO_GRABBER_DISABLE: (
                VIDEO_GRABBER_DISABLE_CONFIRMATION_ID
            ),
            HyperHDROperation.LED_OUTPUT_DISABLE: LED_OUTPUT_DISABLE_CONFIRMATION_ID,
        }.get(self.operation_id)
        if self.disruptive and self.confirmation_metadata_id != expected_confirmation:
            raise ValueError("disable operations require fixed confirmation metadata")
        if not self.disruptive and self.confirmation_metadata_id is not None:
            raise ValueError("enable operations cannot request confirmation")


@dataclass(frozen=True, slots=True)
class AmbientOperationContract:
    """Code-owned metadata for one bounded Aurora composite operation."""

    operation_id: AuroraOperation
    disruptive: bool
    confirmation_metadata_id: str | None = None
    authentication_required: bool = True
    csrf_required: bool = True
    audit_required: bool = True
    sanitized_errors_required: bool = True

    def __post_init__(self) -> None:
        if not (
            self.authentication_required
            and self.csrf_required
            and self.audit_required
            and self.sanitized_errors_required
        ):
            raise ValueError(
                "control-operation security requirements cannot be disabled"
            )
        if self.operation_id is AuroraOperation.AMBIENT_OFF:
            if (
                not self.disruptive
                or self.confirmation_metadata_id != AURORA_AMBIENT_OFF_CONFIRMATION_ID
            ):
                raise ValueError("ambient off requires its fixed confirmation metadata")
        elif self.disruptive or self.confirmation_metadata_id is not None:
            raise ValueError("ambient on cannot request confirmation")


def operation_registry(timeout_seconds: float) -> tuple[OperationContract, ...]:
    """Build the deterministic, code-owned registry with a configured timeout."""
    return (
        OperationContract(
            WLEDOperation.POWER_ON,
            NoOperationInput,
            timeout_seconds,
            WLED_ADAPTER_ID,
            False,
        ),
        OperationContract(
            WLEDOperation.POWER_OFF,
            NoOperationInput,
            timeout_seconds,
            WLED_ADAPTER_ID,
            True,
            POWER_OFF_CONFIRMATION_ID,
        ),
        OperationContract(
            WLEDOperation.BRIGHTNESS_SET,
            BrightnessInput,
            timeout_seconds,
            WLED_ADAPTER_ID,
            False,
        ),
    )


def hyperhdr_operation_registry(
    timeout_seconds: float,
) -> tuple[HyperHDROperationContract, ...]:
    """Build the exact deterministic Milestone 16 HyperHDR registry."""
    return (
        HyperHDROperationContract(
            HyperHDROperation.VIDEO_GRABBER_ENABLE,
            NoOperationInput,
            timeout_seconds,
            HYPERHDR_ADAPTER_ID,
            False,
        ),
        HyperHDROperationContract(
            HyperHDROperation.VIDEO_GRABBER_DISABLE,
            NoOperationInput,
            timeout_seconds,
            HYPERHDR_ADAPTER_ID,
            True,
            VIDEO_GRABBER_DISABLE_CONFIRMATION_ID,
        ),
        HyperHDROperationContract(
            HyperHDROperation.LED_OUTPUT_ENABLE,
            NoOperationInput,
            timeout_seconds,
            HYPERHDR_ADAPTER_ID,
            False,
        ),
        HyperHDROperationContract(
            HyperHDROperation.LED_OUTPUT_DISABLE,
            NoOperationInput,
            timeout_seconds,
            HYPERHDR_ADAPTER_ID,
            True,
            LED_OUTPUT_DISABLE_CONFIRMATION_ID,
        ),
    )


def ambient_operation_registry() -> tuple[AmbientOperationContract, ...]:
    """Build the exact deterministic Milestone 19 composite registry."""
    return (
        AmbientOperationContract(AuroraOperation.AMBIENT_ON, False),
        AmbientOperationContract(
            AuroraOperation.AMBIENT_OFF,
            True,
            AURORA_AMBIENT_OFF_CONFIRMATION_ID,
        ),
    )


REGISTERED_OPERATIONS = operation_registry(2.0)
IMPLEMENTED_OPERATION_ORDER = tuple(
    contract.operation_id for contract in REGISTERED_OPERATIONS
)
REGISTERED_HYPERHDR_OPERATIONS = hyperhdr_operation_registry(2.0)
HYPERHDR_IMPLEMENTED_OPERATION_ORDER = tuple(
    contract.operation_id for contract in REGISTERED_HYPERHDR_OPERATIONS
)
REGISTERED_AMBIENT_OPERATIONS = ambient_operation_registry()
AMBIENT_IMPLEMENTED_OPERATION_ORDER = tuple(
    contract.operation_id for contract in REGISTERED_AMBIENT_OPERATIONS
)
CONTROL_CAPABILITIES = ControlCapabilities()

# Import compatibility for Milestone 14 callers. New code should use OperationContract.
FutureOperationContract = OperationContract
