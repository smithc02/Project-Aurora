"""Bounded synchronous composition for Aurora ambient-mode controls."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from aurora_core.config.models import (
    AmbientControlSettings,
    AuroraOperation,
    HyperHDROperation,
    WLEDOperation,
)
from aurora_core.control_plane.audit import AuditEvent, AuditReason, SecurityAudit
from aurora_core.control_plane.contracts import (
    AURORA_AMBIENT_OFF_CONFIRMATION_VALUE,
    LED_OUTPUT_DISABLE_CONFIRMATION_VALUE,
    POWER_OFF_CONFIRMATION_VALUE,
    VIDEO_GRABBER_DISABLE_CONFIRMATION_VALUE,
    AmbientOperationContract,
    ControlCapabilities,
    ambient_operation_registry,
)
from aurora_core.control_plane.hyperhdr_service import (
    HyperHDRControlResult,
    HyperHDRControlService,
    HyperHDRControlStatus,
)
from aurora_core.control_plane.limiter import MutationAttemptLimiter
from aurora_core.control_plane.mutation_gate import ControlMutationGate
from aurora_core.control_plane.sessions import SessionContext, csrf_is_valid
from aurora_core.control_plane.wled_service import (
    WLEDControlResult,
    WLEDControlService,
    WLEDControlStatus,
)

Clock = Callable[[], float]
ChildResult = HyperHDRControlResult | WLEDControlResult


class AmbientControlAvailability(StrEnum):
    AUTHENTICATION_UNAVAILABLE = "authentication_unavailable"
    CONTROLS_DISABLED = "controls_disabled"
    NO_OPERATIONS = "no_operations"
    REQUIRED_CHILDREN_UNAVAILABLE = "required_children_unavailable"
    AVAILABLE = "available"


class AmbientControlStatus(StrEnum):
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    UNVERIFIED = "unverified"
    FAILED = "failed"
    DENIED = "denied"
    BUSY = "busy"
    RATE_LIMITED = "rate_limited"


class AmbientControlStep(StrEnum):
    VIDEO_GRABBER_ENABLE = "hyperhdr.video_grabber_enable"
    WLED_POWER_ON = "wled.power_on"
    LED_OUTPUT_ENABLE = "hyperhdr.led_output_enable"
    LED_OUTPUT_DISABLE = "hyperhdr.led_output_disable"
    VIDEO_GRABBER_DISABLE = "hyperhdr.video_grabber_disable"
    WLED_POWER_OFF = "wled.power_off"


@dataclass(frozen=True, slots=True)
class AmbientControlResult:
    status: AmbientControlStatus
    attempted_steps: tuple[AmbientControlStep, ...] = ()
    verified_steps: tuple[AmbientControlStep, ...] = ()


_AMBIENT_CHILDREN = {
    AuroraOperation.AMBIENT_ON: (
        HyperHDROperation.VIDEO_GRABBER_ENABLE,
        WLEDOperation.POWER_ON,
        HyperHDROperation.LED_OUTPUT_ENABLE,
    ),
    AuroraOperation.AMBIENT_OFF: (
        HyperHDROperation.LED_OUTPUT_DISABLE,
        HyperHDROperation.VIDEO_GRABBER_DISABLE,
        WLEDOperation.POWER_OFF,
    ),
}

_PARENT_AUDIT_EVENTS = {
    AmbientControlStatus.COMPLETED: AuditEvent.AURORA_OPERATION_SUCCEEDED,
    AmbientControlStatus.PARTIALLY_COMPLETED: (
        AuditEvent.AURORA_OPERATION_PARTIALLY_COMPLETED
    ),
    AmbientControlStatus.UNVERIFIED: AuditEvent.AURORA_OPERATION_UNVERIFIED,
    AmbientControlStatus.FAILED: AuditEvent.AURORA_OPERATION_FAILED,
    AmbientControlStatus.DENIED: AuditEvent.AURORA_OPERATION_DENIED,
    AmbientControlStatus.BUSY: AuditEvent.AURORA_OPERATION_BUSY,
    AmbientControlStatus.RATE_LIMITED: AuditEvent.AURORA_OPERATION_RATE_LIMITED,
}


class AmbientControlService:
    """Coordinate at most three existing child operations under one shared gate."""

    def __init__(
        self,
        settings: AmbientControlSettings,
        *,
        authentication_enabled: bool,
        wled_controls: WLEDControlService,
        hyperhdr_controls: HyperHDRControlService,
        mutation_gate: ControlMutationGate,
        clock: Clock = time.monotonic,
        limiter_digest_key: bytes | None = None,
        audit: SecurityAudit | None = None,
    ) -> None:
        if (
            wled_controls.mutation_gate is not mutation_gate
            or hyperhdr_controls.mutation_gate is not mutation_gate
        ):
            raise ValueError("shared_mutation_gate_required")
        self._settings = settings
        self._authentication_enabled = authentication_enabled
        self._wled = wled_controls
        self._hyperhdr = hyperhdr_controls
        self._mutation_gate = mutation_gate
        self._operation_contracts = ambient_operation_registry()
        self._limiter = MutationAttemptLimiter(
            attempt_limit=settings.operation_limit,
            window_seconds=settings.operation_window_seconds,
            clock=clock,
            digest_key=limiter_digest_key,
        )
        self._audit = SecurityAudit() if audit is None else audit

    @property
    def availability(self) -> AmbientControlAvailability:
        if not self._authentication_enabled:
            return AmbientControlAvailability.AUTHENTICATION_UNAVAILABLE
        if not self._settings.enabled:
            return AmbientControlAvailability.CONTROLS_DISABLED
        if not self._settings.allowed_operations:
            return AmbientControlAvailability.NO_OPERATIONS
        if not self.available_operations:
            return AmbientControlAvailability.REQUIRED_CHILDREN_UNAVAILABLE
        return AmbientControlAvailability.AVAILABLE

    @property
    def available_operations(self) -> tuple[AuroraOperation, ...]:
        if not self._authentication_enabled or not self._settings.enabled:
            return ()
        configured = frozenset(self._settings.allowed_operations)
        wled = frozenset(self._wled.available_operations)
        hyperhdr = frozenset(self._hyperhdr.available_operations)
        available: list[AuroraOperation] = []
        for contract in self._operation_contracts:
            if contract.operation_id not in configured:
                continue
            required = _AMBIENT_CHILDREN[contract.operation_id]
            if all(
                child in wled if isinstance(child, WLEDOperation) else child in hyperhdr
                for child in required
            ):
                available.append(contract.operation_id)
        return tuple(available)

    @property
    def operation_contracts(self) -> tuple[AmbientOperationContract, ...]:
        return self._operation_contracts

    @property
    def tracked_client_count(self) -> int:
        return self._limiter.tracked_client_count

    @property
    def mutation_gate(self) -> ControlMutationGate:
        return self._mutation_gate

    def capabilities(self) -> ControlCapabilities:
        operations = tuple(operation.value for operation in self.available_operations)
        return ControlCapabilities(
            mutations_enabled=bool(operations),
            available_operations=operations,
        )

    def ambient_on(
        self,
        session: SessionContext | None,
        submitted_csrf: str | None,
        client_identifier: str,
    ) -> AmbientControlResult:
        return self._execute(
            AuroraOperation.AMBIENT_ON,
            session,
            submitted_csrf,
            client_identifier,
        )

    def ambient_off(
        self,
        session: SessionContext | None,
        submitted_csrf: str | None,
        confirmation: str | None,
        client_identifier: str,
    ) -> AmbientControlResult:
        return self._execute(
            AuroraOperation.AMBIENT_OFF,
            session,
            submitted_csrf,
            client_identifier,
            confirmation=confirmation,
        )

    def audit_denied(self, operation: AuroraOperation, reason: AuditReason) -> None:
        self._audit.emit_operation(
            AuditEvent.AURORA_OPERATION_DENIED,
            reason,
            operation,
        )

    def _execute(
        self,
        operation: AuroraOperation,
        session: SessionContext | None,
        submitted_csrf: str | None,
        client_identifier: str,
        *,
        confirmation: str | None = None,
    ) -> AmbientControlResult:
        authentication_failure = self._authentication_failure(
            operation,
            session,
            submitted_csrf,
        )
        if authentication_failure is not None:
            return authentication_failure
        assert session is not None
        assert submitted_csrf is not None
        if not self._settings.enabled:
            return self._finish(
                operation,
                AmbientControlStatus.DENIED,
                AuditReason.CONTROLS_DISABLED,
            )
        if operation not in self._settings.allowed_operations:
            return self._finish(
                operation,
                AmbientControlStatus.DENIED,
                AuditReason.OPERATION_NOT_ALLOWLISTED,
            )
        if operation not in self.available_operations:
            return self._finish(
                operation,
                AmbientControlStatus.DENIED,
                AuditReason.CONTROLS_DISABLED,
            )
        if operation is AuroraOperation.AMBIENT_OFF:
            if confirmation is None:
                return self._finish(
                    operation,
                    AmbientControlStatus.DENIED,
                    AuditReason.MISSING_CONFIRMATION,
                )
            if confirmation != AURORA_AMBIENT_OFF_CONFIRMATION_VALUE:
                return self._finish(
                    operation,
                    AmbientControlStatus.DENIED,
                    AuditReason.INVALID_CONFIRMATION,
                )
        if not self._limiter.begin_attempt(client_identifier):
            return self._finish(
                operation,
                AmbientControlStatus.RATE_LIMITED,
                AuditReason.OPERATION_LIMIT,
            )
        if not self._mutation_gate.acquire():
            return self._finish(
                operation,
                AmbientControlStatus.BUSY,
                AuditReason.OPERATION_IN_PROGRESS,
            )
        try:
            if operation is AuroraOperation.AMBIENT_ON:
                return self._run_ambient_on(
                    operation,
                    session,
                    submitted_csrf,
                    client_identifier,
                )
            return self._run_ambient_off(
                operation,
                session,
                submitted_csrf,
                client_identifier,
            )
        finally:
            self._mutation_gate.release()

    def _run_ambient_on(
        self,
        operation: AuroraOperation,
        session: SessionContext,
        submitted_csrf: str,
        client_identifier: str,
    ) -> AmbientControlResult:
        attempted: list[AmbientControlStep] = []
        verified: list[AmbientControlStep] = []
        calls: tuple[tuple[AmbientControlStep, Callable[[], ChildResult]], ...] = (
            (
                AmbientControlStep.VIDEO_GRABBER_ENABLE,
                lambda: self._hyperhdr.video_grabber_enable(
                    session, submitted_csrf, client_identifier
                ),
            ),
            (
                AmbientControlStep.WLED_POWER_ON,
                lambda: self._wled.power_on(session, submitted_csrf, client_identifier),
            ),
            (
                AmbientControlStep.LED_OUTPUT_ENABLE,
                lambda: self._hyperhdr.led_output_enable(
                    session, submitted_csrf, client_identifier
                ),
            ),
        )
        for step, call in calls:
            attempted.append(step)
            result = self._safe_child_call(call)
            if self._child_verified(result):
                verified.append(step)
                continue
            status = self._aggregate_child_status(result, bool(verified))
            return self._finish(
                operation,
                status,
                self._aggregate_reason(result, status),
                attempted,
                verified,
            )
        return self._finish(
            operation,
            AmbientControlStatus.COMPLETED,
            AuditReason.VERIFIED,
            attempted,
            verified,
        )

    def _run_ambient_off(
        self,
        operation: AuroraOperation,
        session: SessionContext,
        submitted_csrf: str,
        client_identifier: str,
    ) -> AmbientControlResult:
        attempted: list[AmbientControlStep] = []
        verified: list[AmbientControlStep] = []

        attempted.append(AmbientControlStep.LED_OUTPUT_DISABLE)
        led_result = self._safe_child_call(
            lambda: self._hyperhdr.led_output_disable(
                session,
                submitted_csrf,
                LED_OUTPUT_DISABLE_CONFIRMATION_VALUE,
                client_identifier,
            )
        )
        if not self._child_verified(led_result):
            status = self._aggregate_child_status(led_result, False)
            return self._finish(
                operation,
                status,
                self._aggregate_reason(led_result, status),
                attempted,
                verified,
            )
        verified.append(AmbientControlStep.LED_OUTPUT_DISABLE)

        incomplete = False
        attempted.append(AmbientControlStep.VIDEO_GRABBER_DISABLE)
        grabber_result = self._safe_child_call(
            lambda: self._hyperhdr.video_grabber_disable(
                session,
                submitted_csrf,
                VIDEO_GRABBER_DISABLE_CONFIRMATION_VALUE,
                client_identifier,
            )
        )
        if self._child_verified(grabber_result):
            verified.append(AmbientControlStep.VIDEO_GRABBER_DISABLE)
        else:
            incomplete = True

        attempted.append(AmbientControlStep.WLED_POWER_OFF)
        wled_result = self._safe_child_call(
            lambda: self._wled.power_off(
                session,
                submitted_csrf,
                POWER_OFF_CONFIRMATION_VALUE,
                client_identifier,
            )
        )
        if self._child_verified(wled_result):
            verified.append(AmbientControlStep.WLED_POWER_OFF)
        else:
            incomplete = True

        status = (
            AmbientControlStatus.PARTIALLY_COMPLETED
            if incomplete
            else AmbientControlStatus.COMPLETED
        )
        reason = AuditReason.PARTIAL_COMPLETION if incomplete else AuditReason.VERIFIED
        return self._finish(operation, status, reason, attempted, verified)

    @staticmethod
    def _safe_child_call(call: Callable[[], ChildResult]) -> ChildResult | None:
        try:
            return call()
        except Exception:
            return None

    @staticmethod
    def _child_verified(result: ChildResult | None) -> bool:
        return result is not None and result.status in {
            WLEDControlStatus.VERIFIED,
            HyperHDRControlStatus.VERIFIED,
        }

    @staticmethod
    def _aggregate_child_status(
        result: ChildResult | None,
        any_verified: bool,
    ) -> AmbientControlStatus:
        if any_verified:
            return AmbientControlStatus.PARTIALLY_COMPLETED
        if result is None:
            return AmbientControlStatus.FAILED
        return {
            WLEDControlStatus.DENIED: AmbientControlStatus.DENIED,
            WLEDControlStatus.RATE_LIMITED: AmbientControlStatus.RATE_LIMITED,
            WLEDControlStatus.BUSY: AmbientControlStatus.BUSY,
            WLEDControlStatus.FAILED: AmbientControlStatus.FAILED,
            WLEDControlStatus.UNVERIFIED: AmbientControlStatus.UNVERIFIED,
            HyperHDRControlStatus.DENIED: AmbientControlStatus.DENIED,
            HyperHDRControlStatus.RATE_LIMITED: AmbientControlStatus.RATE_LIMITED,
            HyperHDRControlStatus.BUSY: AmbientControlStatus.BUSY,
            HyperHDRControlStatus.FAILED: AmbientControlStatus.FAILED,
            HyperHDRControlStatus.UNVERIFIED: AmbientControlStatus.UNVERIFIED,
        }[result.status]

    @staticmethod
    def _aggregate_reason(
        result: ChildResult | None,
        status: AmbientControlStatus,
    ) -> AuditReason:
        if status is AmbientControlStatus.PARTIALLY_COMPLETED:
            return AuditReason.PARTIAL_COMPLETION
        if result is None:
            return AuditReason.CONNECTION_FAILURE
        return result.reason

    def _authentication_failure(
        self,
        operation: AuroraOperation,
        session: SessionContext | None,
        submitted_csrf: str | None,
    ) -> AmbientControlResult | None:
        if not self._authentication_enabled or session is None:
            return self._finish(
                operation,
                AmbientControlStatus.DENIED,
                AuditReason.AUTHENTICATION_REQUIRED,
            )
        if submitted_csrf is None:
            self._audit.emit(AuditEvent.CSRF_REJECTED, AuditReason.MISSING_CSRF)
            return self._finish(
                operation,
                AmbientControlStatus.DENIED,
                AuditReason.MISSING_CSRF,
            )
        if not csrf_is_valid(session, submitted_csrf):
            self._audit.emit(AuditEvent.CSRF_REJECTED, AuditReason.INVALID_CSRF)
            return self._finish(
                operation,
                AmbientControlStatus.DENIED,
                AuditReason.INVALID_CSRF,
            )
        return None

    def _finish(
        self,
        operation: AuroraOperation,
        status: AmbientControlStatus,
        reason: AuditReason,
        attempted: list[AmbientControlStep] | None = None,
        verified: list[AmbientControlStep] | None = None,
    ) -> AmbientControlResult:
        self._audit.emit_operation(
            _PARENT_AUDIT_EVENTS[status],
            reason,
            operation,
        )
        return AmbientControlResult(
            status,
            () if attempted is None else tuple(attempted),
            () if verified is None else tuple(verified),
        )
