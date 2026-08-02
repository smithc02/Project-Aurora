"""Authenticated, serialized orchestration for bounded WLED mutations."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock

from aurora_core.config.models import WLEDOperation, WLEDSettings
from aurora_core.control_plane.audit import AuditEvent, AuditReason, SecurityAudit
from aurora_core.control_plane.contracts import (
    POWER_OFF_CONFIRMATION_VALUE,
    BrightnessInput,
    ControlCapabilities,
    NoOperationInput,
    OperationContract,
    operation_registry,
)
from aurora_core.control_plane.limiter import MutationAttemptLimiter
from aurora_core.control_plane.sessions import SessionContext, csrf_is_valid
from aurora_core.control_plane.wled_adapter import (
    AdapterReason,
    AdapterResult,
    WLEDMutationAdapter,
    WLEDMutationExecutor,
)

Clock = Callable[[], float]
CacheInvalidator = Callable[[], None]


class WLEDControlAvailability(StrEnum):
    AUTHENTICATION_UNAVAILABLE = "authentication_unavailable"
    WLED_UNAVAILABLE = "wled_unavailable"
    CONTROLS_DISABLED = "controls_disabled"
    NO_OPERATIONS = "no_operations"
    AVAILABLE = "available"


class WLEDControlStatus(StrEnum):
    VERIFIED = "verified"
    DENIED = "denied"
    RATE_LIMITED = "rate_limited"
    BUSY = "busy"
    FAILED = "failed"
    UNVERIFIED = "unverified"


@dataclass(frozen=True, slots=True)
class WLEDControlResult:
    status: WLEDControlStatus
    reason: AuditReason


_ADAPTER_AUDIT_REASONS = {
    AdapterReason.CONNECTION_FAILURE: AuditReason.CONNECTION_FAILURE,
    AdapterReason.TIMEOUT: AuditReason.TIMEOUT,
    AdapterReason.HTTP_REJECTION: AuditReason.HTTP_REJECTION,
    AdapterReason.REDIRECT_REJECTION: AuditReason.REDIRECT_REJECTION,
    AdapterReason.OVERSIZED_RESPONSE: AuditReason.OVERSIZED_RESPONSE,
    AdapterReason.MALFORMED_JSON: AuditReason.MALFORMED_JSON,
    AdapterReason.MISSING_EXPECTED_STATE: AuditReason.MISSING_EXPECTED_STATE,
    AdapterReason.STATE_VERIFICATION_MISMATCH: (
        AuditReason.STATE_VERIFICATION_MISMATCH
    ),
}
_VERIFICATION_FAILURES = frozenset(
    {
        AdapterReason.MALFORMED_JSON,
        AdapterReason.MISSING_EXPECTED_STATE,
        AdapterReason.STATE_VERIFICATION_MISMATCH,
    }
)


class WLEDControlService:
    """Apply all security gates before exactly one fixed adapter invocation."""

    def __init__(
        self,
        settings: WLEDSettings,
        *,
        authentication_enabled: bool,
        adapter: WLEDMutationExecutor | None = None,
        clock: Clock = time.monotonic,
        limiter_digest_key: bytes | None = None,
        audit: SecurityAudit | None = None,
        cache_invalidator: CacheInvalidator | None = None,
    ) -> None:
        self._settings = settings
        self._authentication_enabled = authentication_enabled
        self._operation_contracts = operation_registry(
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
        self._adapter: WLEDMutationExecutor | None
        if adapter is not None:
            self._adapter = adapter
        elif settings.host is not None:
            self._adapter = WLEDMutationAdapter(
                host=settings.host,
                port=settings.port or 80,
                timeout_seconds=settings.controls.timeout_seconds,
            )
        else:
            self._adapter = None

    @property
    def availability(self) -> WLEDControlAvailability:
        if not self._authentication_enabled:
            return WLEDControlAvailability.AUTHENTICATION_UNAVAILABLE
        if not self._settings.enabled or self._settings.host is None:
            return WLEDControlAvailability.WLED_UNAVAILABLE
        if not self._settings.controls.enabled:
            return WLEDControlAvailability.CONTROLS_DISABLED
        if not self.available_operations:
            return WLEDControlAvailability.NO_OPERATIONS
        return WLEDControlAvailability.AVAILABLE

    @property
    def available_operations(self) -> tuple[WLEDOperation, ...]:
        if (
            not self._authentication_enabled
            or not self._settings.enabled
            or self._settings.host is None
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
    def operation_contracts(self) -> tuple[OperationContract, ...]:
        return self._operation_contracts

    @property
    def maximum_brightness(self) -> int:
        return self._settings.controls.maximum_brightness

    @property
    def tracked_client_count(self) -> int:
        return self._limiter.tracked_client_count

    def capabilities(self) -> ControlCapabilities:
        operations = tuple(operation.value for operation in self.available_operations)
        return ControlCapabilities(
            mutations_enabled=bool(operations),
            available_operations=operations,
        )

    def power_on(
        self,
        session: SessionContext | None,
        submitted_csrf: str | None,
        client_identifier: str,
    ) -> WLEDControlResult:
        return self._execute(
            WLEDOperation.POWER_ON,
            NoOperationInput(),
            session,
            submitted_csrf,
            client_identifier,
        )

    def power_off(
        self,
        session: SessionContext | None,
        submitted_csrf: str | None,
        confirmation: str | None,
        client_identifier: str,
    ) -> WLEDControlResult:
        return self._execute(
            WLEDOperation.POWER_OFF,
            NoOperationInput(),
            session,
            submitted_csrf,
            client_identifier,
            confirmation=confirmation,
        )

    def set_brightness(
        self,
        session: SessionContext | None,
        submitted_csrf: str | None,
        brightness: int,
        client_identifier: str,
    ) -> WLEDControlResult:
        authentication_failure = self._authentication_failure(
            WLEDOperation.BRIGHTNESS_SET,
            session,
            submitted_csrf,
        )
        if authentication_failure is not None:
            return authentication_failure
        try:
            operation_input = BrightnessInput(brightness)
        except ValueError:
            return self._deny(
                WLEDOperation.BRIGHTNESS_SET,
                AuditReason.INVALID_BRIGHTNESS,
            )
        return self._execute(
            WLEDOperation.BRIGHTNESS_SET,
            operation_input,
            session,
            submitted_csrf,
            client_identifier,
        )

    def audit_denied(self, operation: WLEDOperation, reason: AuditReason) -> None:
        self._audit.emit_operation(AuditEvent.WLED_OPERATION_DENIED, reason, operation)

    def request_security_failure(
        self,
        operation: WLEDOperation,
        session: SessionContext | None,
        submitted_csrf: str | None,
    ) -> WLEDControlResult | None:
        """Apply authentication and CSRF before route-specific value errors."""
        return self._authentication_failure(operation, session, submitted_csrf)

    def _execute(
        self,
        operation: WLEDOperation,
        operation_input: NoOperationInput | BrightnessInput,
        session: SessionContext | None,
        submitted_csrf: str | None,
        client_identifier: str,
        *,
        confirmation: str | None = None,
    ) -> WLEDControlResult:
        authentication_failure = self._authentication_failure(
            operation,
            session,
            submitted_csrf,
        )
        if authentication_failure is not None:
            return authentication_failure
        if self.availability in {
            WLEDControlAvailability.AUTHENTICATION_UNAVAILABLE,
            WLEDControlAvailability.WLED_UNAVAILABLE,
            WLEDControlAvailability.CONTROLS_DISABLED,
        }:
            return self._deny(operation, AuditReason.CONTROLS_DISABLED)
        if operation not in self.available_operations:
            return self._deny(operation, AuditReason.OPERATION_NOT_ALLOWLISTED)
        if operation is WLEDOperation.POWER_OFF:
            if confirmation is None:
                return self._confirmation_denied(
                    operation,
                    AuditReason.MISSING_CONFIRMATION,
                )
            if confirmation != POWER_OFF_CONFIRMATION_VALUE:
                return self._confirmation_denied(
                    operation,
                    AuditReason.INVALID_CONFIRMATION,
                )
        if isinstance(operation_input, BrightnessInput) and (
            operation_input.brightness > self.maximum_brightness
        ):
            return self._deny(operation, AuditReason.INVALID_BRIGHTNESS)
        if not self._limiter.begin_attempt(client_identifier):
            self._audit.emit_operation(
                AuditEvent.WLED_OPERATION_RATE_LIMITED,
                AuditReason.OPERATION_LIMIT,
                operation,
            )
            return WLEDControlResult(
                WLEDControlStatus.RATE_LIMITED,
                AuditReason.OPERATION_LIMIT,
            )
        if not self._operation_lock.acquire(blocking=False):
            self._audit.emit_operation(
                AuditEvent.WLED_OPERATION_BUSY,
                AuditReason.OPERATION_IN_PROGRESS,
                operation,
            )
            return WLEDControlResult(
                WLEDControlStatus.BUSY,
                AuditReason.OPERATION_IN_PROGRESS,
            )
        try:
            if self._adapter is None:
                return self._deny(operation, AuditReason.CONTROLS_DISABLED)
            try:
                adapter_result = self._adapter.execute(operation, operation_input)
            except Exception:
                adapter_result = AdapterResult(
                    False,
                    AdapterReason.CONNECTION_FAILURE,
                )
        finally:
            self._operation_lock.release()

        reason = _ADAPTER_AUDIT_REASONS.get(adapter_result.reason)
        if adapter_result.verified:
            self._audit.emit_operation(
                AuditEvent.WLED_OPERATION_SUCCEEDED,
                AuditReason.VERIFIED,
                operation,
            )
            self._cache_invalidator()
            return WLEDControlResult(
                WLEDControlStatus.VERIFIED,
                AuditReason.VERIFIED,
            )
        if reason is None:
            reason = AuditReason.CONNECTION_FAILURE
        if adapter_result.reason in _VERIFICATION_FAILURES:
            event = AuditEvent.WLED_STATE_VERIFICATION_FAILED
            status = WLEDControlStatus.UNVERIFIED
        else:
            event = AuditEvent.WLED_OPERATION_FAILED
            status = WLEDControlStatus.FAILED
        self._audit.emit_operation(event, reason, operation)
        return WLEDControlResult(status, reason)

    def _authentication_failure(
        self,
        operation: WLEDOperation,
        session: SessionContext | None,
        submitted_csrf: str | None,
    ) -> WLEDControlResult | None:
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
        operation: WLEDOperation,
        reason: AuditReason,
    ) -> WLEDControlResult:
        self.audit_denied(operation, reason)
        return WLEDControlResult(WLEDControlStatus.DENIED, reason)

    def _confirmation_denied(
        self,
        operation: WLEDOperation,
        reason: AuditReason,
    ) -> WLEDControlResult:
        self._audit.emit_operation(
            AuditEvent.WLED_CONFIRMATION_REJECTED,
            reason,
            operation,
        )
        return WLEDControlResult(WLEDControlStatus.DENIED, reason)
