"""Sanitized, non-persistent security audit events."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from enum import StrEnum

import structlog

from aurora_core.config.models import WLEDOperation


class AuditEvent(StrEnum):
    """Fixed security event identifiers safe for structured logs."""

    LOGIN_SUCCEEDED = "login_succeeded"
    LOGIN_FAILED = "login_failed"
    LOGIN_RATE_LIMITED = "login_rate_limited"
    LOGOUT_SUCCEEDED = "logout_succeeded"
    PROTECTED_PAGE_DENIED = "protected_page_denied"
    PROTECTED_API_DENIED = "protected_api_denied"
    CSRF_REJECTED = "csrf_rejected"
    AUTHENTICATION_REQUEST_REJECTED = "authentication_request_rejected"
    SESSION_REJECTED = "session_rejected"
    WLED_OPERATION_SUCCEEDED = "wled_operation_succeeded"
    WLED_OPERATION_FAILED = "wled_operation_failed"
    WLED_OPERATION_DENIED = "wled_operation_denied"
    WLED_OPERATION_RATE_LIMITED = "wled_operation_rate_limited"
    WLED_OPERATION_BUSY = "wled_operation_busy"
    WLED_CONFIRMATION_REJECTED = "wled_confirmation_rejected"
    WLED_STATE_VERIFICATION_FAILED = "wled_state_verification_failed"


class AuditReason(StrEnum):
    """Bounded reason codes; raw inputs and exceptions are never accepted."""

    AUTHENTICATED = "authenticated"
    INVALID_CREDENTIALS = "invalid_credentials"
    ATTEMPT_LIMIT = "attempt_limit"
    LOGGED_OUT = "logged_out"
    AUTHENTICATION_REQUIRED = "authentication_required"
    AUTHENTICATION_DISABLED = "authentication_disabled"
    INVALID_SESSION = "invalid_session"
    EXPIRED_SESSION = "expired_session"
    MALFORMED_COOKIE = "malformed_cookie"
    MISSING_CSRF = "missing_csrf"
    INVALID_CSRF = "invalid_csrf"
    CONTENT_TYPE = "unsupported_content_type"
    CONTENT_LENGTH = "invalid_content_length"
    BODY_TOO_LARGE = "body_too_large"
    TRANSFER_ENCODING = "unsupported_transfer_encoding"
    MALFORMED_FORM = "malformed_form"
    CONTROLS_DISABLED = "controls_disabled"
    OPERATION_NOT_ALLOWLISTED = "operation_not_allowlisted"
    INVALID_BRIGHTNESS = "invalid_brightness"
    MISSING_CONFIRMATION = "missing_confirmation"
    INVALID_CONFIRMATION = "invalid_confirmation"
    OPERATION_LIMIT = "operation_limit"
    OPERATION_IN_PROGRESS = "operation_in_progress"
    CONNECTION_FAILURE = "connection_failure"
    TIMEOUT = "timeout"
    HTTP_REJECTION = "http_rejection"
    REDIRECT_REJECTION = "redirect_rejection"
    OVERSIZED_RESPONSE = "oversized_response"
    MALFORMED_JSON = "malformed_json"
    MISSING_EXPECTED_STATE = "missing_expected_state"
    STATE_VERIFICATION_MISMATCH = "state_verification_mismatch"
    VERIFIED = "verified"


AuditFields = Mapping[str, str | int]
AuditSink = Callable[[str, AuditFields], None]


def _default_sink(event: str, fields: AuditFields) -> None:
    structlog.get_logger("aurora.security").info(event, **fields)


class SecurityAudit:
    """Emit only fixed event names, schema version, and bounded reason codes."""

    def __init__(self, sink: AuditSink | None = None) -> None:
        self._sink = _default_sink if sink is None else sink

    def emit(self, event: AuditEvent, reason: AuditReason) -> None:
        self._sink(
            "security_audit",
            {
                "schema_version": 1,
                "security_event": event.value,
                "reason_code": reason.value,
            },
        )

    def emit_operation(
        self,
        event: AuditEvent,
        reason: AuditReason,
        operation: WLEDOperation,
    ) -> None:
        """Emit a fixed WLED event without accepting arbitrary audit fields."""
        self._sink(
            "security_audit",
            {
                "schema_version": 1,
                "security_event": event.value,
                "reason_code": reason.value,
                "operation_id": operation.value,
            },
        )
