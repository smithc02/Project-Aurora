"""Authenticated, serialized orchestration for bounded HyperHDR mutations."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock

from aurora_core.config.models import HyperHDROperation, HyperHDRSettings
from aurora_core.control_plane.audit import AuditEvent, AuditReason, SecurityAudit
from aurora_core.control_plane.contracts import (
    LED_OUTPUT_DISABLE_CONFIRMATION_VALUE,
    VIDEO_GRABBER_DISABLE_CONFIRMATION_VALUE,
    ControlCapabilities,
    HyperHDROperationContract,
    hyperhdr_operation_registry,
)
from aurora_core.control_plane.hyperhdr_adapter import (
    HyperHDRAdapterReason,
    HyperHDRAdapterResult,
    HyperHDRMutationAdapter,
    HyperHDRMutationExecutor,
)
from aurora_core.control_plane.limiter import MutationAttemptLimiter
from aurora_core.control_plane.mutation_gate import ControlMutationGate
from aurora_core.control_plane.sessions import SessionContext, csrf_is_valid

Clock = Callable[[], float]
CacheInvalidator = Callable[[], None]


class HyperHDRControlAvailability(StrEnum):
    AUTHENTICATION_UNAVAILABLE = "authentication_unavailable"
    HYPERHDR_UNAVAILABLE = "hyperhdr_unavailable"
    CONTROLS_DISABLED = "controls_disabled"
    NO_OPERATIONS = "no_operations"
    AVAILABLE = "available"


class HyperHDRControlStatus(StrEnum):
    VERIFIED = "verified"
    DENIED = "denied"
    RATE_LIMITED = "rate_limited"
    BUSY = "busy"
    FAILED = "failed"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class HyperHDRControlResult:
    status: HyperHDRControlStatus
    reason: AuditReason


_ADAPTER_AUDIT_REASONS = {
    HyperHDRAdapterReason.CONNECTION_FAILURE: AuditReason.CONNECTION_FAILURE,
    HyperHDRAdapterReason.TIMEOUT: AuditReason.TIMEOUT,
    HyperHDRAdapterReason.HTTP_REJECTION: AuditReason.HTTP_REJECTION,
    HyperHDRAdapterReason.REDIRECT_REJECTION: AuditReason.REDIRECT_REJECTION,
    HyperHDRAdapterReason.UNAUTHORIZED_RESPONSE: AuditReason.UNAUTHORIZED_RESPONSE,
    HyperHDRAdapterReason.OVERSIZED_RESPONSE: AuditReason.OVERSIZED_RESPONSE,
    HyperHDRAdapterReason.MALFORMED_JSON: AuditReason.MALFORMED_JSON,
    HyperHDRAdapterReason.NON_OBJECT_JSON: AuditReason.MALFORMED_JSON,
    HyperHDRAdapterReason.MISSING_SUCCESS: AuditReason.MISSING_SUCCESS,
    HyperHDRAdapterReason.SUCCESS_WRONG_TYPE: AuditReason.INVALID_SUCCESS,
    HyperHDRAdapterReason.SUCCESS_FALSE: AuditReason.INVALID_SUCCESS,
    HyperHDRAdapterReason.MISMATCHED_COMMAND: AuditReason.COMMAND_MISMATCH,
    HyperHDRAdapterReason.VERIFICATION_CONNECTION_FAILURE: (
        AuditReason.CONNECTION_FAILURE
    ),
    HyperHDRAdapterReason.VERIFICATION_TIMEOUT: AuditReason.TIMEOUT,
    HyperHDRAdapterReason.VERIFICATION_HTTP_REJECTION: AuditReason.HTTP_REJECTION,
    HyperHDRAdapterReason.VERIFICATION_REDIRECT_REJECTION: (
        AuditReason.REDIRECT_REJECTION
    ),
    HyperHDRAdapterReason.VERIFICATION_UNAUTHORIZED_RESPONSE: (
        AuditReason.UNAUTHORIZED_RESPONSE
    ),
    HyperHDRAdapterReason.VERIFICATION_OVERSIZED_RESPONSE: (
        AuditReason.OVERSIZED_RESPONSE
    ),
    HyperHDRAdapterReason.VERIFICATION_MALFORMED_RESPONSE: (AuditReason.MALFORMED_JSON),
    HyperHDRAdapterReason.MISSING_COMPONENT_STATE: (
        AuditReason.MISSING_COMPONENT_STATE
    ),
    HyperHDRAdapterReason.AMBIGUOUS_COMPONENT_STATE: (
        AuditReason.AMBIGUOUS_COMPONENT_STATE
    ),
    HyperHDRAdapterReason.STATE_VERIFICATION_MISMATCH: (
        AuditReason.STATE_VERIFICATION_MISMATCH
    ),
}


class HyperHDRControlService:
    """Apply all gates before one POST and, after acknowledgement, one GET."""

    def __init__(
        self,
        settings: HyperHDRSettings,
        *,
        authentication_enabled: bool,
        adapter: HyperHDRMutationExecutor | None = None,
        clock: Clock = time.monotonic,
        limiter_digest_key: bytes | None = None,
        audit: SecurityAudit | None = None,
        cache_invalidator: CacheInvalidator | None = None,
        mutation_gate: ControlMutationGate | None = None,
    ) -> None:
        self._settings = settings
        self._authentication_enabled = authentication_enabled
        self._operation_contracts = hyperhdr_operation_registry(
            settings.controls.timeout_seconds
        )
        self._audit = SecurityAudit() if audit is None else audit
        self._cache_invalidator: CacheInvalidator = (
            (lambda: None) if cache_invalidator is None else cache_invalidator
        )
        self._limiter = MutationAttemptLimiter(
            attempt_limit=settings.controls.operation_limit,
            window_seconds=settings.controls.operation_window_seconds,
            clock=clock,
            digest_key=limiter_digest_key,
        )
        self._operation_lock = Lock()
        self._mutation_gate = (
            ControlMutationGate() if mutation_gate is None else mutation_gate
        )
        if adapter is not None:
            self._adapter: HyperHDRMutationExecutor | None = adapter
        elif settings.host is not None and settings.port is not None:
            self._adapter = HyperHDRMutationAdapter(
                host=settings.host,
                port=settings.port,
                timeout_seconds=settings.controls.timeout_seconds,
            )
        else:
            self._adapter = None

    @property
    def availability(self) -> HyperHDRControlAvailability:
        if not self._authentication_enabled:
            return HyperHDRControlAvailability.AUTHENTICATION_UNAVAILABLE
        if (
            not self._settings.enabled
            or self._settings.host is None
            or self._settings.port is None
        ):
            return HyperHDRControlAvailability.HYPERHDR_UNAVAILABLE
        if not self._settings.controls.enabled:
            return HyperHDRControlAvailability.CONTROLS_DISABLED
        if not self.available_operations:
            return HyperHDRControlAvailability.NO_OPERATIONS
        return HyperHDRControlAvailability.AVAILABLE

    @property
    def available_operations(self) -> tuple[HyperHDROperation, ...]:
        if (
            not self._authentication_enabled
            or not self._settings.enabled
            or self._settings.host is None
            or self._settings.port is None
            or not self._settings.controls.enabled
        ):
            return ()
        configured = frozenset(self._settings.controls.allowed_operations)
        return tuple(
            contract.operation_id
            for contract in self._operation_contracts
            if contract.operation_id in configured
        )

    @property
    def operation_contracts(self) -> tuple[HyperHDROperationContract, ...]:
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

    def video_grabber_enable(
        self,
        session: SessionContext | None,
        submitted_csrf: str | None,
        client_identifier: str,
    ) -> HyperHDRControlResult:
        return self._execute(
            HyperHDROperation.VIDEO_GRABBER_ENABLE,
            session,
            submitted_csrf,
            client_identifier,
        )

    def video_grabber_disable(
        self,
        session: SessionContext | None,
        submitted_csrf: str | None,
        confirmation: str | None,
        client_identifier: str,
    ) -> HyperHDRControlResult:
        return self._execute(
            HyperHDROperation.VIDEO_GRABBER_DISABLE,
            session,
            submitted_csrf,
            client_identifier,
            confirmation=confirmation,
        )

    def led_output_enable(
        self,
        session: SessionContext | None,
        submitted_csrf: str | None,
        client_identifier: str,
    ) -> HyperHDRControlResult:
        return self._execute(
            HyperHDROperation.LED_OUTPUT_ENABLE,
            session,
            submitted_csrf,
            client_identifier,
        )

    def led_output_disable(
        self,
        session: SessionContext | None,
        submitted_csrf: str | None,
        confirmation: str | None,
        client_identifier: str,
    ) -> HyperHDRControlResult:
        return self._execute(
            HyperHDROperation.LED_OUTPUT_DISABLE,
            session,
            submitted_csrf,
            client_identifier,
            confirmation=confirmation,
        )

    def audit_denied(
        self,
        operation: HyperHDROperation,
        reason: AuditReason,
    ) -> None:
        self._audit.emit_operation(
            AuditEvent.HYPERHDR_OPERATION_DENIED,
            reason,
            operation,
        )

    def _execute(
        self,
        operation: HyperHDROperation,
        session: SessionContext | None,
        submitted_csrf: str | None,
        client_identifier: str,
        *,
        confirmation: str | None = None,
    ) -> HyperHDRControlResult:
        authentication_failure = self._authentication_failure(
            operation,
            session,
            submitted_csrf,
        )
        if authentication_failure is not None:
            return authentication_failure
        if self.availability in {
            HyperHDRControlAvailability.AUTHENTICATION_UNAVAILABLE,
            HyperHDRControlAvailability.HYPERHDR_UNAVAILABLE,
            HyperHDRControlAvailability.CONTROLS_DISABLED,
        }:
            return self._deny(operation, AuditReason.CONTROLS_DISABLED)
        if operation not in self.available_operations:
            return self._deny(operation, AuditReason.OPERATION_NOT_ALLOWLISTED)
        expected_confirmation = {
            HyperHDROperation.VIDEO_GRABBER_DISABLE: (
                VIDEO_GRABBER_DISABLE_CONFIRMATION_VALUE
            ),
            HyperHDROperation.LED_OUTPUT_DISABLE: (
                LED_OUTPUT_DISABLE_CONFIRMATION_VALUE
            ),
        }.get(operation)
        if expected_confirmation is not None:
            if confirmation is None:
                return self._confirmation_denied(
                    operation,
                    AuditReason.MISSING_CONFIRMATION,
                )
            if confirmation != expected_confirmation:
                return self._confirmation_denied(
                    operation,
                    AuditReason.INVALID_CONFIRMATION,
                )
        if not self._limiter.begin_attempt(client_identifier):
            self._audit.emit_operation(
                AuditEvent.HYPERHDR_OPERATION_RATE_LIMITED,
                AuditReason.OPERATION_LIMIT,
                operation,
            )
            return HyperHDRControlResult(
                HyperHDRControlStatus.RATE_LIMITED,
                AuditReason.OPERATION_LIMIT,
            )
        if not self._mutation_gate.acquire():
            self._audit.emit_operation(
                AuditEvent.HYPERHDR_OPERATION_BUSY,
                AuditReason.OPERATION_IN_PROGRESS,
                operation,
            )
            return HyperHDRControlResult(
                HyperHDRControlStatus.BUSY,
                AuditReason.OPERATION_IN_PROGRESS,
            )
        try:
            if not self._operation_lock.acquire(blocking=False):
                self._audit.emit_operation(
                    AuditEvent.HYPERHDR_OPERATION_BUSY,
                    AuditReason.OPERATION_IN_PROGRESS,
                    operation,
                )
                return HyperHDRControlResult(
                    HyperHDRControlStatus.BUSY,
                    AuditReason.OPERATION_IN_PROGRESS,
                )
            try:
                if self._adapter is None:
                    return self._deny(operation, AuditReason.CONTROLS_DISABLED)
                try:
                    adapter_result = self._adapter.execute(operation)
                except Exception:
                    adapter_result = HyperHDRAdapterResult(
                        False,
                        False,
                        HyperHDRAdapterReason.CONNECTION_FAILURE,
                    )
            finally:
                self._operation_lock.release()
        finally:
            self._mutation_gate.release()

        if adapter_result.verified:
            self._audit.emit_operation(
                AuditEvent.HYPERHDR_OPERATION_SUCCEEDED,
                AuditReason.VERIFIED,
                operation,
            )
            self._cache_invalidator()
            return HyperHDRControlResult(
                HyperHDRControlStatus.VERIFIED,
                AuditReason.VERIFIED,
            )
        reason = _ADAPTER_AUDIT_REASONS.get(
            adapter_result.reason,
            AuditReason.CONNECTION_FAILURE,
        )
        if adapter_result.acknowledged:
            event = AuditEvent.HYPERHDR_STATE_VERIFICATION_FAILED
            status = HyperHDRControlStatus.UNVERIFIED
        else:
            event = AuditEvent.HYPERHDR_OPERATION_FAILED
            status = HyperHDRControlStatus.FAILED
        self._audit.emit_operation(event, reason, operation)
        return HyperHDRControlResult(status, reason)

    def _authentication_failure(
        self,
        operation: HyperHDROperation,
        session: SessionContext | None,
        submitted_csrf: str | None,
    ) -> HyperHDRControlResult | None:
        if not self._authentication_enabled or session is None:
            return self._deny(operation, AuditReason.AUTHENTICATION_REQUIRED)
        if submitted_csrf is None:
            self._audit.emit(AuditEvent.CSRF_REJECTED, AuditReason.MISSING_CSRF)
            return self._deny(operation, AuditReason.MISSING_CSRF)
        if not csrf_is_valid(session, submitted_csrf):
            self._audit.emit(AuditEvent.CSRF_REJECTED, AuditReason.INVALID_CSRF)
            return self._deny(operation, AuditReason.INVALID_CSRF)
        return None

    def _deny(
        self,
        operation: HyperHDROperation,
        reason: AuditReason,
    ) -> HyperHDRControlResult:
        self.audit_denied(operation, reason)
        return HyperHDRControlResult(HyperHDRControlStatus.DENIED, reason)

    def _confirmation_denied(
        self,
        operation: HyperHDROperation,
        reason: AuditReason,
    ) -> HyperHDRControlResult:
        self._audit.emit_operation(
            AuditEvent.HYPERHDR_CONFIRMATION_REJECTED,
            reason,
            operation,
        )
        return HyperHDRControlResult(HyperHDRControlStatus.DENIED, reason)
