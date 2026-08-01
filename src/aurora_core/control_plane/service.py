"""Fail-closed authentication orchestration with no device operations."""

from __future__ import annotations

import hmac
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from aurora_core.config.models import DashboardAuthenticationSettings
from aurora_core.control_plane.audit import (
    AuditEvent,
    AuditReason,
    SecurityAudit,
)
from aurora_core.control_plane.limiter import LoginAttemptLimiter
from aurora_core.control_plane.sessions import (
    CreatedSession,
    SessionContext,
    SessionLookupStatus,
    SessionStore,
    TokenFactory,
    csrf_is_valid,
)
from aurora_core.security.passwords import verify_password

Clock = Callable[[], float]


class LoginStatus(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class LoginResult:
    status: LoginStatus
    created_session: CreatedSession | None = None


class ControlPlaneService:
    """Own authentication state separately from the public health service."""

    def __init__(
        self,
        settings: DashboardAuthenticationSettings,
        *,
        clock: Clock = time.monotonic,
        token_factory: TokenFactory | None = None,
        limiter_digest_key: bytes | None = None,
        audit: SecurityAudit | None = None,
    ) -> None:
        self._settings = settings
        self._audit = SecurityAudit() if audit is None else audit
        if token_factory is None:
            self._sessions = SessionStore(
                ttl_seconds=settings.session_ttl_minutes * 60,
                maximum_sessions=settings.maximum_sessions,
                clock=clock,
            )
        else:
            self._sessions = SessionStore(
                ttl_seconds=settings.session_ttl_minutes * 60,
                maximum_sessions=settings.maximum_sessions,
                clock=clock,
                token_factory=token_factory,
            )
        self._limiter = LoginAttemptLimiter(
            attempt_limit=settings.login_attempt_limit,
            window_seconds=settings.login_attempt_window_seconds,
            clock=clock,
            digest_key=limiter_digest_key,
        )
        self._username = settings.username or ""
        self._password_hash = (
            ""
            if settings.password_hash is None
            else settings.password_hash.get_secret_value()
        )

    @property
    def authentication_enabled(self) -> bool:
        return self._settings.enabled

    @property
    def secure_cookie(self) -> bool:
        return self._settings.secure_cookie

    @property
    def session_ttl_seconds(self) -> int:
        return self._settings.session_ttl_minutes * 60

    @property
    def active_session_count(self) -> int:
        return self._sessions.active_count

    @property
    def tracked_client_count(self) -> int:
        return self._limiter.tracked_client_count

    def authenticate(
        self,
        username: str,
        password: str,
        client_identifier: str,
        *,
        prior_session_token: str | None = None,
    ) -> LoginResult:
        if not self.authentication_enabled:
            return LoginResult(LoginStatus.UNAVAILABLE)
        if not self._limiter.begin_attempt(client_identifier):
            self._audit.emit(AuditEvent.LOGIN_RATE_LIMITED, AuditReason.ATTEMPT_LIMIT)
            return LoginResult(LoginStatus.RATE_LIMITED)

        username_matches = hmac.compare_digest(
            username.encode("utf-8"),
            self._username.encode("utf-8"),
        )
        password_matches = verify_password(password, self._password_hash)
        if not (username_matches and password_matches):
            self._audit.emit(AuditEvent.LOGIN_FAILED, AuditReason.INVALID_CREDENTIALS)
            return LoginResult(LoginStatus.FAILURE)

        self._limiter.clear(client_identifier)
        if prior_session_token is not None:
            self._sessions.invalidate(prior_session_token)
        created = self._sessions.create(self._username)
        self._audit.emit(AuditEvent.LOGIN_SUCCEEDED, AuditReason.AUTHENTICATED)
        return LoginResult(LoginStatus.SUCCESS, created)

    def resolve_session(self, token: str | None) -> SessionContext | None:
        if not self.authentication_enabled or token is None:
            return None
        lookup = self._sessions.resolve(token)
        if lookup.status is SessionLookupStatus.VALID:
            return lookup.session
        reason = (
            AuditReason.EXPIRED_SESSION
            if lookup.status is SessionLookupStatus.EXPIRED
            else AuditReason.INVALID_SESSION
        )
        self._audit.emit(AuditEvent.SESSION_REJECTED, reason)
        return None

    def logout(
        self,
        session_token: str,
        session: SessionContext,
        submitted_csrf: str | None,
    ) -> bool:
        if submitted_csrf is None:
            self._audit.emit(AuditEvent.CSRF_REJECTED, AuditReason.MISSING_CSRF)
            return False
        if not csrf_is_valid(session, submitted_csrf):
            self._audit.emit(AuditEvent.CSRF_REJECTED, AuditReason.INVALID_CSRF)
            return False
        self._sessions.invalidate(session_token)
        self._audit.emit(AuditEvent.LOGOUT_SUCCEEDED, AuditReason.LOGGED_OUT)
        return True

    def audit_page_denied(
        self,
        reason: AuditReason = AuditReason.AUTHENTICATION_REQUIRED,
    ) -> None:
        self._audit.emit(
            AuditEvent.PROTECTED_PAGE_DENIED,
            reason,
        )

    def audit_api_denied(
        self,
        reason: AuditReason = AuditReason.AUTHENTICATION_REQUIRED,
    ) -> None:
        self._audit.emit(
            AuditEvent.PROTECTED_API_DENIED,
            reason,
        )

    def audit_malformed_cookie(self) -> None:
        self._audit.emit(AuditEvent.SESSION_REJECTED, AuditReason.MALFORMED_COOKIE)

    def audit_malformed_request(self, reason: AuditReason) -> None:
        self._audit.emit(AuditEvent.AUTHENTICATION_REQUEST_REJECTED, reason)
