"""Sanitized, non-persistent security audit events."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from enum import StrEnum

import structlog


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
