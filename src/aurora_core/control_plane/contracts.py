"""Non-executable contracts for future bounded control operations."""

from __future__ import annotations

import re
from dataclasses import dataclass

_OPERATION_ID = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_INPUT_MODEL_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.]{0,127}")


@dataclass(frozen=True, slots=True)
class ControlCapabilities:
    """Public capability status; Milestone 14 deliberately registers nothing."""

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
class FutureOperationContract:
    """Metadata every later, separately reviewed operation must define.

    No instances are registered in Milestone 14 and this module exposes no
    executor. A future registry must allowlist the identifier and destination
    adapter in code, validate a strict typed input model, apply the timeout,
    require authenticated CSRF validation, emit audit events, and require
    confirmation metadata for disruptive operations.
    """

    operation_id: str
    input_model_name: str
    timeout_seconds: float
    destination_adapter_id: str
    disruptive: bool
    confirmation_metadata_id: str | None = None
    authentication_required: bool = True
    csrf_required: bool = True
    audit_required: bool = True
    sanitized_errors_required: bool = True

    def __post_init__(self) -> None:
        if _OPERATION_ID.fullmatch(self.operation_id) is None:
            raise ValueError("operation identifiers must use the bounded grammar")
        if _INPUT_MODEL_ID.fullmatch(self.input_model_name) is None:
            raise ValueError("a strict typed input model must be explicit")
        if _OPERATION_ID.fullmatch(self.destination_adapter_id) is None:
            raise ValueError("a destination adapter must be explicitly allowlisted")
        if not 0.1 <= self.timeout_seconds <= 10.0:
            raise ValueError("operation timeout must be within bounded limits")
        if not (
            self.authentication_required
            and self.csrf_required
            and self.audit_required
            and self.sanitized_errors_required
        ):
            raise ValueError(
                "control-operation security requirements cannot be disabled"
            )
        if self.disruptive and (
            self.confirmation_metadata_id is None
            or _OPERATION_ID.fullmatch(self.confirmation_metadata_id) is None
        ):
            raise ValueError("disruptive operations require confirmation metadata")


CONTROL_CAPABILITIES = ControlCapabilities()
REGISTERED_OPERATIONS: tuple[FutureOperationContract, ...] = ()
