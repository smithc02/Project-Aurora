"""HTTP boundary tests for the Milestone 14 protected control plane."""

from __future__ import annotations

import io
import json
import re
import threading
from email.message import Message
from functools import cache
from http import HTTPStatus
from http.cookies import SimpleCookie
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from aurora_core.config import load_settings
from aurora_core.control_plane.audit import SecurityAudit
from aurora_core.control_plane.cookies import SESSION_COOKIE_NAME
from aurora_core.control_plane.rendering import render_controls
from aurora_core.control_plane.service import ControlPlaneService
from aurora_core.control_plane.sessions import SessionContext
from aurora_core.dashboard.assets import PORTAL_CSS_PATH
from aurora_core.dashboard.models import HealthReport, HealthStatus
from aurora_core.dashboard.server import DashboardHandler
from aurora_core.security.passwords import hash_password


def _password() -> str:
    return "".join(("http", "-", "credential", "-", "canary"))


@cache
def _encoded_hash() -> str:
    return hash_password(_password(), salt=bytes(range(16)))


def _settings(**authentication_overrides: object):
    authentication: dict[str, object] = {
        "enabled": True,
        "username": "test_operator",
        "password_hash": _encoded_hash(),
        "session_ttl_minutes": 5,
        "maximum_sessions": 4,
        "secure_cookie": False,
        "login_attempt_limit": 3,
        "login_attempt_window_seconds": 30,
    }
    authentication.update(authentication_overrides)
    return load_settings(
        environment={},
        cli_overrides={"dashboard": {"authentication": authentication}},
    )


class SequenceTokenFactory:
    def __init__(self) -> None:
        self._value = 0
        self._lock = threading.Lock()

    def __call__(self, byte_count: int) -> str:
        with self._lock:
            self._value += 1
            return f"{self._value:043d}"


class StubHealthService:
    def __init__(self) -> None:
        self.calls = 0
        self.report = HealthReport(
            status=HealthStatus.HEALTHY,
            checked_at="2026-01-01T00:00:00+00:00",
            service_uptime_seconds=1.0,
            components=(),
        )

    def get_health(self) -> HealthReport:
        self.calls += 1
        return self.report


class AuditCapture:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, str | int]]] = []

    def __call__(self, event: str, fields: object) -> None:
        self.events.append((event, dict(fields)))  # type: ignore[arg-type]


def _control(
    *,
    audit_capture: AuditCapture | None = None,
    **overrides: object,
) -> ControlPlaneService:
    audit = None
    if audit_capture is not None:
        audit = SecurityAudit(audit_capture)
    return ControlPlaneService(
        _settings(**overrides).dashboard.authentication,
        token_factory=SequenceTokenFactory(),
        limiter_digest_key=b"test-limiter-key",
        audit=audit,
    )


def _headers(
    *,
    body: bytes | None = None,
    content_type: str | None = None,
    cookie: str | None = None,
    content_length: str | None = None,
    transfer_encoding: str | None = None,
) -> Message:
    headers = Message()
    if content_type is not None:
        headers["Content-Type"] = content_type
    if body is not None:
        headers["Content-Length"] = (
            str(len(body)) if content_length is None else content_length
        )
    elif content_length is not None:
        headers["Content-Length"] = content_length
    if cookie is not None:
        headers["Cookie"] = cookie
    if transfer_encoding is not None:
        headers["Transfer-Encoding"] = transfer_encoding
    return headers


def _request(
    service: StubHealthService,
    control: ControlPlaneService | None,
    path: str,
    *,
    method: str = "GET",
    body: bytes = b"",
    headers: Message | None = None,
) -> tuple[bytes, str, HTTPStatus, dict[str, str]]:
    handler = DashboardHandler.__new__(DashboardHandler)
    server_values: dict[str, object] = {
        "health_service": service,
        "refresh_seconds": 5,
    }
    if control is not None:
        server_values["control_plane"] = control
    handler.server = SimpleNamespace(**server_values)
    handler.path = path
    handler.headers = Message() if headers is None else headers
    handler.rfile = io.BytesIO(body)
    handler.client_address = ("test-client", 0)
    responses: list[tuple[bytes, str, HTTPStatus, dict[str, str]]] = []

    def capture(
        response_body: bytes,
        content_type: str,
        status: HTTPStatus = HTTPStatus.OK,
        response_headers: dict[str, str] | None = None,
    ) -> None:
        responses.append(
            (
                response_body,
                content_type,
                status,
                {} if response_headers is None else dict(response_headers),
            )
        )

    handler._send = capture  # type: ignore[method-assign]
    getattr(handler, f"do_{method}")()
    return responses[-1]


def _login_body(
    *,
    username: str = "test_operator",
    password: str | None = None,
    next_path: str = "/controls",
) -> bytes:
    return urlencode(
        {
            "username": username,
            "password": _password() if password is None else password,
            "next": next_path,
        }
    ).encode()


def _login(
    service: StubHealthService,
    control: ControlPlaneService,
    *,
    cookie: str | None = None,
    next_path: str = "/controls",
) -> tuple[bytes, str, HTTPStatus, dict[str, str]]:
    body = _login_body(next_path=next_path)
    return _request(
        service,
        control,
        "/login",
        method="POST",
        body=body,
        headers=_headers(
            body=body,
            content_type="application/x-www-form-urlencoded",
            cookie=cookie,
        ),
    )


def _cookie_value(set_cookie: str) -> str:
    parsed = SimpleCookie()
    parsed.load(set_cookie)
    return parsed[SESSION_COOKIE_NAME].value


def _request_cookie(set_cookie: str) -> str:
    return f"{SESSION_COOKIE_NAME}={_cookie_value(set_cookie)}"


def test_authentication_disabled_fails_closed_without_health_polling() -> None:
    service = StubHealthService()
    audit = AuditCapture()
    disabled = ControlPlaneService(
        load_settings(environment={}).dashboard.authentication,
        audit=SecurityAudit(audit),
    )
    for path in ("/login", "/controls", "/api/control/status"):
        body, _, status, _ = _request(service, disabled, path)
        assert status is HTTPStatus.NOT_FOUND
        assert b"control" in body.lower()
    assert service.calls == 0
    serialized = json.dumps(audit.events)
    assert "protected_page_denied" in serialized
    assert "protected_api_denied" in serialized
    assert "authentication_disabled" in serialized


def test_public_portal_and_health_api_remain_backward_compatible() -> None:
    service = StubHealthService()
    disabled = ControlPlaneService(
        load_settings(environment={}).dashboard.authentication
    )
    page, content_type, status, _ = _request(service, disabled, "/")
    assert status is HTTPStatus.OK
    assert content_type == "text/html; charset=utf-8"
    assert b"Project Aurora" in page
    assert b'href="/login"' not in page
    api, _, status, _ = _request(service, disabled, "/api/health")
    assert status is HTTPStatus.OK
    payload = json.loads(api)
    assert payload == json.loads(json.dumps(service.report.to_dict()))
    assert payload["schema_version"] == 1
    assert service.calls == 2


def test_enabled_public_portal_has_server_rendered_login_link() -> None:
    service = StubHealthService()
    page, _, status, _ = _request(service, _control(), "/")
    assert status is HTTPStatus.OK
    assert b'href="/login">Login</a>' in page
    assert service.calls == 1


def test_login_page_is_local_unpopulated_and_hardware_free() -> None:
    service = StubHealthService()
    page, content_type, status, _ = _request(service, _control(), "/login")
    assert status is HTTPStatus.OK
    assert content_type == "text/html; charset=utf-8"
    assert b'action="/login"' in page
    assert b'name="username"' in page
    assert b'name="password"' in page
    assert b"test_operator" not in page
    assert b"<script" not in page
    assert service.calls == 0


def test_successful_login_sets_opaque_cookie_and_does_not_poll_health() -> None:
    service = StubHealthService()
    audit = AuditCapture()
    control = _control(audit_capture=audit)
    body, _, status, headers = _login(service, control)
    assert body == b""
    assert status is HTTPStatus.SEE_OTHER
    assert headers["Location"] == "/controls"
    cookie = headers["Set-Cookie"]
    assert cookie.startswith(f"{SESSION_COOKIE_NAME}=")
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie
    assert "Path=/" in cookie
    assert "Secure" not in cookie
    assert _password() not in cookie
    assert control.active_session_count == 1
    assert service.calls == 0
    assert any(
        fields["security_event"] == "login_succeeded" for _, fields in audit.events
    )


def test_secure_cookie_flag_is_configurable() -> None:
    service = StubHealthService()
    _, _, status, headers = _login(service, _control(secure_cookie=True))
    assert status is HTTPStatus.SEE_OTHER
    assert "Secure" in headers["Set-Cookie"]


def test_invalid_login_is_generic_and_rate_limited() -> None:
    service = StubHealthService()
    audit = AuditCapture()
    control = _control(audit_capture=audit, login_attempt_limit=2)
    invalid_body = _login_body(username="unknown_operator", password="wrong-value")
    request_headers = _headers(
        body=invalid_body,
        content_type="application/x-www-form-urlencoded",
    )
    for expected in (HTTPStatus.UNAUTHORIZED, HTTPStatus.UNAUTHORIZED):
        body, _, status, _ = _request(
            service,
            control,
            "/login",
            method="POST",
            body=invalid_body,
            headers=request_headers,
        )
        assert status is expected
        assert b"Authentication failed." in body
        assert b"unknown_operator" not in body
        assert b"wrong-value" not in body
    body, _, status, _ = _request(
        service,
        control,
        "/login",
        method="POST",
        body=invalid_body,
        headers=request_headers,
    )
    assert status is HTTPStatus.TOO_MANY_REQUESTS
    assert b"Authentication failed." in body
    serialized = json.dumps(audit.events)
    assert "login_failed" in serialized
    assert "login_rate_limited" in serialized
    assert "unknown_operator" not in serialized
    assert "wrong-value" not in serialized
    assert service.calls == 0


@pytest.mark.parametrize(
    ("body", "headers", "expected_status"),
    (
        (
            b"username=x&password=y",
            _headers(body=b"username=x&password=y", content_type="application/json"),
            HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
        ),
        (
            b"username=x&password=y",
            _headers(
                body=b"username=x&password=y",
                content_type="application/x-www-form-urlencoded",
                content_length="word",
            ),
            HTTPStatus.BAD_REQUEST,
        ),
        (
            b"username=x&password=y",
            _headers(
                body=b"username=x&password=y",
                content_type="application/x-www-form-urlencoded",
                content_length="9" * 100,
            ),
            HTTPStatus.BAD_REQUEST,
        ),
        (
            b"username=x&password=y",
            _headers(content_type="application/x-www-form-urlencoded"),
            HTTPStatus.LENGTH_REQUIRED,
        ),
        (
            b"x" * 4097,
            _headers(
                body=b"x" * 4097,
                content_type="application/x-www-form-urlencoded",
            ),
            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
        ),
        (
            b"username=x&password=y",
            _headers(
                body=b"username=x&password=y",
                content_type="application/x-www-form-urlencoded",
                transfer_encoding="chunked",
            ),
            HTTPStatus.BAD_REQUEST,
        ),
        (
            b"username=%ZZ&password=y",
            _headers(
                body=b"username=%ZZ&password=y",
                content_type="application/x-www-form-urlencoded",
            ),
            HTTPStatus.BAD_REQUEST,
        ),
        (
            b"username=%FF&password=y",
            _headers(
                body=b"username=%FF&password=y",
                content_type="application/x-www-form-urlencoded",
            ),
            HTTPStatus.BAD_REQUEST,
        ),
        (
            b"username=\xff&password=y",
            _headers(
                body=b"username=\xff&password=y",
                content_type="application/x-www-form-urlencoded",
            ),
            HTTPStatus.BAD_REQUEST,
        ),
        (
            b"username=x&username=y&password=z",
            _headers(
                body=b"username=x&username=y&password=z",
                content_type="application/x-www-form-urlencoded",
            ),
            HTTPStatus.BAD_REQUEST,
        ),
    ),
)
def test_login_request_boundary_rejects_malformed_input(
    body: bytes,
    headers: Message,
    expected_status: HTTPStatus,
) -> None:
    service = StubHealthService()
    audit = AuditCapture()
    response, _, status, _ = _request(
        service,
        _control(audit_capture=audit),
        "/login",
        method="POST",
        body=body,
        headers=headers,
    )
    assert status is expected_status
    assert b"Unable to process the authentication request." in response
    assert json.dumps(audit.events).count("authentication_request_rejected") == 1
    assert service.calls == 0


def test_login_rejects_repeated_content_type_headers() -> None:
    service = StubHealthService()
    body = b"username=x&password=y"
    headers = _headers(
        body=body,
        content_type="application/x-www-form-urlencoded",
    )
    headers.add_header("Content-Type", "application/x-www-form-urlencoded")
    response, _, status, _ = _request(
        service,
        _control(),
        "/login",
        method="POST",
        body=body,
        headers=headers,
    )
    assert status is HTTPStatus.UNSUPPORTED_MEDIA_TYPE
    assert b"Unable to process the authentication request." in response
    assert service.calls == 0


def test_login_rejects_repeated_content_length_headers() -> None:
    service = StubHealthService()
    body = b"username=x&password=y"
    headers = _headers(
        body=body,
        content_type="application/x-www-form-urlencoded",
    )
    headers.add_header("Content-Length", str(len(body)))
    response, _, status, _ = _request(
        service,
        _control(),
        "/login",
        method="POST",
        body=body,
        headers=headers,
    )
    assert status is HTTPStatus.BAD_REQUEST
    assert b"Unable to process the authentication request." in response
    assert service.calls == 0


@pytest.mark.parametrize(
    "next_path",
    (
        "https://example.invalid/path",
        "//example.invalid/path",
        "scheme:opaque",
        "/controls?unexpected=true",
    ),
)
def test_login_rejects_external_protocol_relative_and_unlisted_redirects(
    next_path: str,
) -> None:
    service = StubHealthService()
    _, _, status, headers = _login(service, _control(), next_path=next_path)
    assert status is HTTPStatus.SEE_OTHER
    assert headers["Location"] == "/controls"
    assert next_path not in headers["Location"]


def test_get_login_sanitizes_next_and_redirects_authenticated_session() -> None:
    service = StubHealthService()
    control = _control()
    page, _, status, _ = _request(
        service,
        control,
        "/login?next=https%3A%2F%2Fexample.invalid",
    )
    assert status is HTTPStatus.OK
    assert b'value="/controls"' in page
    assert b"example.invalid" not in page
    _, _, _, login_headers = _login(service, control)
    cookie = _request_cookie(login_headers["Set-Cookie"])
    _, _, status, headers = _request(
        service,
        control,
        "/login",
        headers=_headers(cookie=cookie),
    )
    assert status is HTTPStatus.SEE_OTHER
    assert headers["Location"] == "/controls"


def test_session_identifier_rotates_and_old_cookie_is_invalid() -> None:
    service = StubHealthService()
    control = _control()
    _, _, _, first_headers = _login(service, control)
    first_cookie = _request_cookie(first_headers["Set-Cookie"])
    _, _, _, second_headers = _login(service, control, cookie=first_cookie)
    second_cookie = _request_cookie(second_headers["Set-Cookie"])
    assert first_cookie != second_cookie
    _, _, old_status, old_headers = _request(
        service,
        control,
        "/controls",
        headers=_headers(cookie=first_cookie),
    )
    assert old_status is HTTPStatus.SEE_OTHER
    assert old_headers["Location"].startswith("/login")
    page, _, new_status, _ = _request(
        service,
        control,
        "/controls",
        headers=_headers(cookie=second_cookie),
    )
    assert new_status is HTTPStatus.OK
    assert b"Control-plane status" in page


def test_protected_page_redirect_and_api_json_unauthorized_behavior() -> None:
    service = StubHealthService()
    audit = AuditCapture()
    control = _control(audit_capture=audit)
    _, _, page_status, page_headers = _request(service, control, "/controls")
    assert page_status is HTTPStatus.SEE_OTHER
    assert page_headers["Location"] == "/login?next=%2Fcontrols"
    api, content_type, api_status, api_headers = _request(
        service,
        control,
        "/api/control/status",
    )
    assert api_status is HTTPStatus.UNAUTHORIZED
    assert content_type == "application/json; charset=utf-8"
    assert "Location" not in api_headers
    assert json.loads(api) == {
        "schema_version": 1,
        "authenticated": False,
        "error": "authentication_required",
    }
    serialized = json.dumps(audit.events)
    assert "protected_page_denied" in serialized
    assert "protected_api_denied" in serialized
    assert service.calls == 0


def test_authenticated_controls_and_status_are_sanitized_and_hardware_free() -> None:
    service = StubHealthService()
    control = _control()
    _, _, _, login_headers = _login(service, control)
    cookie = _request_cookie(login_headers["Set-Cookie"])
    page, _, status, _ = _request(
        service,
        control,
        "/controls",
        headers=_headers(cookie=cookie),
    )
    assert status is HTTPStatus.OK
    assert b"test_operator" in page
    assert b"Mutations enabled" in page and b">No<" in page
    assert b"Available operations" in page and b">None<" in page
    assert b"Strict control boundary" in page
    assert b"WLED" in page and b"HyperHDR" in page
    session_token = _cookie_value(login_headers["Set-Cookie"])
    assert session_token.encode() not in page
    assert _encoded_hash().encode() not in page
    csrf_match = re.search(rb'name="csrf_token" value="([A-Za-z0-9_-]+)"', page)
    assert csrf_match is not None
    assert page.count(csrf_match.group(1)) == 1

    api, _, api_status, _ = _request(
        service,
        control,
        "/api/control/status",
        headers=_headers(cookie=cookie),
    )
    assert api_status is HTTPStatus.OK
    assert json.loads(api) == {
        "schema_version": 1,
        "authenticated": True,
        "mutations_enabled": False,
        "available_operations": [],
    }
    assert service.calls == 0


def test_controls_renderer_escapes_operator_name() -> None:
    page = render_controls(SessionContext("<unsafe-operator>", "A" * 43, 60.0))
    assert "&lt;unsafe-operator&gt;" in page
    assert "<unsafe-operator>" not in page


def test_logout_requires_csrf_then_invalidates_session_and_clears_cookie() -> None:
    service = StubHealthService()
    audit = AuditCapture()
    control = _control(audit_capture=audit)
    _, _, _, login_headers = _login(service, control)
    cookie = _request_cookie(login_headers["Set-Cookie"])
    controls, _, _, _ = _request(
        service,
        control,
        "/controls",
        headers=_headers(cookie=cookie),
    )
    csrf_match = re.search(rb'name="csrf_token" value="([A-Za-z0-9_-]+)"', controls)
    assert csrf_match is not None
    csrf = csrf_match.group(1).decode()

    empty = b""
    _, _, missing_status, _ = _request(
        service,
        control,
        "/logout",
        method="POST",
        body=empty,
        headers=_headers(
            body=empty,
            content_type="application/x-www-form-urlencoded",
            cookie=cookie,
        ),
    )
    assert missing_status is HTTPStatus.FORBIDDEN

    wrong = urlencode({"csrf_token": "Z" * 43}).encode()
    _, _, wrong_status, _ = _request(
        service,
        control,
        "/logout",
        method="POST",
        body=wrong,
        headers=_headers(
            body=wrong,
            content_type="application/x-www-form-urlencoded",
            cookie=cookie,
        ),
    )
    assert wrong_status is HTTPStatus.FORBIDDEN

    valid = urlencode({"csrf_token": csrf}).encode()
    _, _, status, headers = _request(
        service,
        control,
        "/logout",
        method="POST",
        body=valid,
        headers=_headers(
            body=valid,
            content_type="application/x-www-form-urlencoded",
            cookie=cookie,
        ),
    )
    assert status is HTTPStatus.SEE_OTHER
    assert headers["Location"] == "/"
    assert f"{SESSION_COOKIE_NAME}=" in headers["Set-Cookie"]
    assert "Max-Age=0" in headers["Set-Cookie"]
    _, _, after_status, _ = _request(
        service,
        control,
        "/controls",
        headers=_headers(cookie=cookie),
    )
    assert after_status is HTTPStatus.SEE_OTHER
    serialized = json.dumps(audit.events)
    assert serialized.count("csrf_rejected") == 2
    assert "logout_succeeded" in serialized
    assert csrf not in serialized
    assert cookie not in serialized
    assert _cookie_value(login_headers["Set-Cookie"]) not in serialized
    assert service.calls == 0


@pytest.mark.parametrize(
    "cookie",
    (
        f"{SESSION_COOKIE_NAME}=short",
        f"{SESSION_COOKIE_NAME}=" + "Z" * 43,
        f'{SESSION_COOKIE_NAME}="unterminated',
    ),
)
def test_malformed_and_unknown_cookies_fail_safely_without_leakage(
    cookie: str,
) -> None:
    service = StubHealthService()
    audit = AuditCapture()
    _, _, status, headers = _request(
        service,
        _control(audit_capture=audit),
        "/controls",
        headers=_headers(cookie=cookie),
    )
    assert status is HTTPStatus.SEE_OTHER
    assert headers["Location"].startswith("/login")
    assert cookie not in json.dumps(audit.events)
    assert service.calls == 0


def test_static_login_logout_and_control_status_never_poll_health() -> None:
    service = StubHealthService()
    control = _control()
    _request(service, control, PORTAL_CSS_PATH)
    _request(service, control, "/login")
    _request(service, control, "/api/control/status")
    empty = b""
    _request(
        service,
        control,
        "/logout",
        method="POST",
        body=empty,
        headers=_headers(
            body=empty,
            content_type="application/x-www-form-urlencoded",
        ),
    )
    assert service.calls == 0


@pytest.mark.parametrize(
    ("method", "path"),
    (
        ("GET", "/api/control/execute"),
        ("GET", "/api/control/wled"),
        ("GET", "/api/control/hyperhdr"),
        ("POST", "/api/control/execute"),
        ("POST", "/api/control/wled"),
    ),
)
def test_no_device_control_or_generic_execute_endpoint_exists(
    method: str,
    path: str,
) -> None:
    service = StubHealthService()
    body, _, status, _ = _request(service, _control(), path, method=method)
    expected = (
        HTTPStatus.NOT_FOUND if method == "GET" else HTTPStatus.METHOD_NOT_ALLOWED
    )
    assert status is expected
    assert b"operation" not in body
    assert service.calls == 0
