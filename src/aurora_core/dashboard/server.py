"""Dependency-free local HTTP server for the Aurora health dashboard."""

from __future__ import annotations

import argparse
import json
import re
import socket
import sys
from collections.abc import Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit

from aurora_core.config import AuroraConfigurationError, load_settings
from aurora_core.config.models import AuroraSettings, HyperHDROperation, WLEDOperation
from aurora_core.control_plane.audit import AuditReason
from aurora_core.control_plane.contracts import ControlCapabilities
from aurora_core.control_plane.cookies import (
    cleared_session_cookie,
    read_session_cookie,
    session_cookie,
)
from aurora_core.control_plane.forms import (
    HYPERHDR_CONTROL_BODY_LIMIT,
    LOGIN_BODY_LIMIT,
    LOGOUT_BODY_LIMIT,
    WLED_CONTROL_BODY_LIMIT,
    FormError,
    parse_form,
    safe_next_path,
    validate_form_headers,
)
from aurora_core.control_plane.hyperhdr_service import (
    HyperHDRControlAvailability,
    HyperHDRControlResult,
    HyperHDRControlService,
    HyperHDRControlStatus,
)
from aurora_core.control_plane.rendering import (
    render_controls,
    render_hyperhdr_controls,
    render_login,
    render_wled_controls,
)
from aurora_core.control_plane.service import ControlPlaneService, LoginStatus
from aurora_core.control_plane.sessions import SessionContext
from aurora_core.control_plane.wled_service import (
    WLEDControlAvailability,
    WLEDControlResult,
    WLEDControlService,
    WLEDControlStatus,
)
from aurora_core.dashboard.assets import PORTAL_CSS, PORTAL_CSS_PATH
from aurora_core.dashboard.models import HealthReport
from aurora_core.dashboard.portal import (
    PORTAL_PATHS,
    ControlNavigationLink,
    render_portal,
)
from aurora_core.dashboard.service import HealthService

_BRIGHTNESS_FORM_VALUE = re.compile(r"[1-9][0-9]{0,2}")
_WLED_POST_OPERATIONS = {
    "/controls/wled/power-on": WLEDOperation.POWER_ON,
    "/controls/wled/power-off": WLEDOperation.POWER_OFF,
    "/controls/wled/brightness": WLEDOperation.BRIGHTNESS_SET,
}
_WLED_NOTICE_VALUES = frozenset(
    {"verified", "denied", "invalid", "rate_limited", "busy", "failed", "unverified"}
)
_HYPERHDR_POST_OPERATIONS = {
    "/controls/hyperhdr/video-grabber/enable": (HyperHDROperation.VIDEO_GRABBER_ENABLE),
    "/controls/hyperhdr/video-grabber/disable": (
        HyperHDROperation.VIDEO_GRABBER_DISABLE
    ),
    "/controls/hyperhdr/led-output/enable": HyperHDROperation.LED_OUTPUT_ENABLE,
    "/controls/hyperhdr/led-output/disable": HyperHDROperation.LED_OUTPUT_DISABLE,
}
_HYPERHDR_NOTICE_VALUES = frozenset(
    {"verified", "denied", "rate_limited", "busy", "failed", "unverified"}
)


def _parse_brightness(value: str | None) -> int | None:
    if value is None or _BRIGHTNESS_FORM_VALUE.fullmatch(value) is None:
        return None
    parsed = int(value)
    return parsed if 1 <= parsed <= 255 else None


def _result_notice(result: WLEDControlResult) -> str:
    if result.reason is AuditReason.INVALID_BRIGHTNESS:
        return "invalid"
    return {
        WLEDControlStatus.VERIFIED: "verified",
        WLEDControlStatus.DENIED: "denied",
        WLEDControlStatus.RATE_LIMITED: "rate_limited",
        WLEDControlStatus.BUSY: "busy",
        WLEDControlStatus.FAILED: "failed",
        WLEDControlStatus.UNVERIFIED: "unverified",
    }[result.status]


def _hyperhdr_result_notice(result: HyperHDRControlResult) -> str:
    return {
        HyperHDRControlStatus.VERIFIED: "verified",
        HyperHDRControlStatus.DENIED: "denied",
        HyperHDRControlStatus.RATE_LIMITED: "rate_limited",
        HyperHDRControlStatus.BUSY: "busy",
        HyperHDRControlStatus.FAILED: "failed",
        HyperHDRControlStatus.UNVERIFIED: "unverified",
    }[result.status]


def _render_page(report: HealthReport, refresh_seconds: int) -> str:
    """Retain the original rendering helper as an overview-page wrapper."""
    return render_portal(report, "/", refresh_seconds)


class DashboardHTTPServer(ThreadingHTTPServer):
    """Threaded HTTP server carrying one shared single-flight health service."""

    daemon_threads = True
    request_queue_size = 16

    def __init__(
        self,
        server_address: tuple[str, int],
        service: HealthService,
        refresh_seconds: int,
        control_plane: ControlPlaneService,
        wled_controls: WLEDControlService,
        hyperhdr_controls: HyperHDRControlService,
        configuration_profile: object = None,
    ) -> None:
        self.health_service = service
        self.refresh_seconds = refresh_seconds
        self.control_plane = control_plane
        self.wled_controls = wled_controls
        self.hyperhdr_controls = hyperhdr_controls
        self.configuration_profile = configuration_profile
        super().__init__(server_address, DashboardHandler)


class DashboardIPv6HTTPServer(DashboardHTTPServer):
    """IPv6 variant selected for an explicit IPv6 bind literal."""

    address_family = socket.AF_INET6


class DashboardHandler(BaseHTTPRequestHandler):
    """Serve the status page and stable JSON endpoint."""

    server_version = "ProjectAuroraDashboard/1"
    sys_version = ""

    def _send(
        self,
        body: bytes,
        content_type: str,
        status: HTTPStatus = HTTPStatus.OK,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'self'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        if headers is not None:
            for name, value in headers.items():
                self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlsplit(self.path).path
        if path in _WLED_POST_OPERATIONS or path in _HYPERHDR_POST_OPERATIONS:
            self._method_not_allowed()
            return
        if path == PORTAL_CSS_PATH:
            self._send(PORTAL_CSS, "text/css; charset=utf-8")
            return
        if path == "/login":
            self._get_login()
            return
        if path == "/controls":
            self._get_controls()
            return
        if path == "/controls/wled":
            self._get_wled_controls()
            return
        if path == "/controls/hyperhdr":
            self._get_hyperhdr_controls()
            return
        if path == "/api/control/status":
            self._get_control_status()
            return
        if path not in PORTAL_PATHS and path != "/api/health":
            self._send(
                b"not found\n",
                "text/plain; charset=utf-8",
                HTTPStatus.NOT_FOUND,
            )
            return
        server = cast(DashboardHTTPServer, self.server)
        report = server.health_service.get_health()
        if path == "/api/health":
            body = json.dumps(
                report.to_dict(),
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode()
            self._send(body, "application/json; charset=utf-8")
            return
        control_link = self._portal_control_link()
        page = render_portal(
            report,
            path,
            server.refresh_seconds,
            control_link=control_link,
            configuration_profile=getattr(server, "configuration_profile", None),
        ).encode()
        self._send(page, "text/html; charset=utf-8")

    def _get_login(self) -> None:
        control = self._control_plane()
        if control is None or not control.authentication_enabled:
            page = render_login(
                authentication_enabled=False,
                next_path="/controls",
            ).encode()
            self._send(
                page,
                "text/html; charset=utf-8",
                HTTPStatus.NOT_FOUND,
            )
            return
        _, session = self._request_session(control)
        if session is not None:
            self._redirect("/controls")
            return
        page = render_login(
            authentication_enabled=True,
            next_path=self._query_next_path(),
        ).encode()
        self._send(page, "text/html; charset=utf-8")

    def _get_controls(self) -> None:
        control = self._control_plane()
        if control is None or not control.authentication_enabled:
            if control is not None:
                control.audit_page_denied(AuditReason.AUTHENTICATION_DISABLED)
            self._control_unavailable(html=True)
            return
        _, session = self._request_session(control)
        if session is None:
            control.audit_page_denied()
            self._redirect("/login?next=%2Fcontrols")
            return
        wled = self._wled_control_plane()
        hyperhdr = self._hyperhdr_control_plane()
        self._send(
            render_controls(
                session,
                capabilities=self._combined_capabilities(),
                wled_availability=(
                    WLEDControlAvailability.CONTROLS_DISABLED
                    if wled is None
                    else wled.availability
                ),
                hyperhdr_availability=(
                    HyperHDRControlAvailability.CONTROLS_DISABLED
                    if hyperhdr is None
                    else hyperhdr.availability
                ),
            ).encode(),
            "text/html; charset=utf-8",
        )

    def _get_hyperhdr_controls(self) -> None:
        control = self._control_plane()
        hyperhdr = self._hyperhdr_control_plane()
        if control is None or not control.authentication_enabled or hyperhdr is None:
            if control is not None:
                control.audit_page_denied(AuditReason.AUTHENTICATION_DISABLED)
            self._control_unavailable(html=True)
            return
        _, session = self._request_session(control)
        if session is None:
            control.audit_page_denied()
            self._redirect("/login?next=%2Fcontrols%2Fhyperhdr")
            return
        server = cast(DashboardHTTPServer, self.server)
        report = server.health_service.get_health()
        component = next(
            (item for item in report.components if item.name == "hyperhdr"),
            None,
        )
        self._send(
            render_hyperhdr_controls(
                session,
                component=component,
                availability=hyperhdr.availability,
                operations=hyperhdr.available_operations,
                notice=self._hyperhdr_notice(),
            ).encode(),
            "text/html; charset=utf-8",
        )

    def _get_wled_controls(self) -> None:
        control = self._control_plane()
        wled = self._wled_control_plane()
        if control is None or not control.authentication_enabled or wled is None:
            if control is not None:
                control.audit_page_denied(AuditReason.AUTHENTICATION_DISABLED)
            self._control_unavailable(html=True)
            return
        _, session = self._request_session(control)
        if session is None:
            control.audit_page_denied()
            self._redirect("/login?next=%2Fcontrols%2Fwled")
            return
        server = cast(DashboardHTTPServer, self.server)
        report = server.health_service.get_health()
        component = next(
            (item for item in report.components if item.name == "wled"),
            None,
        )
        self._send(
            render_wled_controls(
                session,
                component=component,
                availability=wled.availability,
                operations=wled.available_operations,
                maximum_brightness=wled.maximum_brightness,
                notice=self._wled_notice(),
            ).encode(),
            "text/html; charset=utf-8",
        )

    def _get_control_status(self) -> None:
        control = self._control_plane()
        if control is None or not control.authentication_enabled:
            if control is not None:
                control.audit_api_denied(AuditReason.AUTHENTICATION_DISABLED)
            self._control_unavailable(html=False)
            return
        _, session = self._request_session(control)
        if session is None:
            control.audit_api_denied()
            self._send_json(
                {
                    "schema_version": 1,
                    "authenticated": False,
                    "error": "authentication_required",
                },
                HTTPStatus.UNAUTHORIZED,
            )
            return
        self._send_json(self._combined_capabilities().to_dict())

    def _method_not_allowed(self) -> None:
        self.close_connection = True
        path = urlsplit(getattr(self, "path", "")).path
        if path == "/login":
            allowed = "GET, POST"
        elif (
            path == "/logout"
            or path in _WLED_POST_OPERATIONS
            or path in _HYPERHDR_POST_OPERATIONS
        ):
            allowed = "POST"
        else:
            allowed = "GET"
        self._send(
            b"method not allowed\n",
            "text/plain; charset=utf-8",
            HTTPStatus.METHOD_NOT_ALLOWED,
            {"Allow": allowed},
        )

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlsplit(getattr(self, "path", "")).path
        if path == "/login":
            self._post_login()
            return
        if path == "/logout":
            self._post_logout()
            return
        operation = _WLED_POST_OPERATIONS.get(path)
        if operation is not None:
            self._post_wled_operation(operation)
            return
        hyperhdr_operation = _HYPERHDR_POST_OPERATIONS.get(path)
        if hyperhdr_operation is not None:
            self._post_hyperhdr_operation(hyperhdr_operation)
            return
        self._method_not_allowed()

    def _post_wled_operation(self, operation: WLEDOperation) -> None:
        control = self._control_plane()
        wled = self._wled_control_plane()
        if control is None or not control.authentication_enabled or wled is None:
            if wled is not None:
                wled.audit_denied(operation, AuditReason.AUTHENTICATION_DISABLED)
            self.close_connection = True
            self._control_unavailable(html=True)
            return
        _, session = self._request_session(control)
        if session is None:
            wled.audit_denied(operation, AuditReason.AUTHENTICATION_REQUIRED)
            self._redirect("/login?next=%2Fcontrols%2Fwled")
            return

        allowed_fields = {"csrf_token"}
        if operation is WLEDOperation.POWER_OFF:
            allowed_fields.add("confirmation")
        elif operation is WLEDOperation.BRIGHTNESS_SET:
            allowed_fields.add("brightness")
        fields, error = self._read_form(
            maximum_body_bytes=WLED_CONTROL_BODY_LIMIT,
            allowed_fields=frozenset(allowed_fields),
        )
        if error is not None or fields is None:
            active_error = error or FormError(
                HTTPStatus.BAD_REQUEST,
                AuditReason.MALFORMED_FORM,
            )
            wled.audit_denied(operation, active_error.reason)
            self._send(
                b"Unable to process the WLED operation request.\n",
                "text/plain; charset=utf-8",
                active_error.status,
            )
            return

        csrf_token = fields.get("csrf_token")
        client_identifier = self._client_identifier()
        if operation is WLEDOperation.POWER_ON:
            result = wled.power_on(session, csrf_token, client_identifier)
        elif operation is WLEDOperation.POWER_OFF:
            result = wled.power_off(
                session,
                csrf_token,
                fields.get("confirmation"),
                client_identifier,
            )
        else:
            brightness = _parse_brightness(fields.get("brightness"))
            if brightness is None:
                security_failure = wled.request_security_failure(
                    operation,
                    session,
                    csrf_token,
                )
                if security_failure is not None:
                    self._redirect(
                        f"/controls/wled?notice={_result_notice(security_failure)}"
                    )
                    return
                wled.audit_denied(operation, AuditReason.INVALID_BRIGHTNESS)
                self._redirect("/controls/wled?notice=invalid")
                return
            result = wled.set_brightness(
                session,
                csrf_token,
                brightness,
                client_identifier,
            )
        self._redirect(f"/controls/wled?notice={_result_notice(result)}")

    def _post_hyperhdr_operation(self, operation: HyperHDROperation) -> None:
        control = self._control_plane()
        hyperhdr = self._hyperhdr_control_plane()
        if control is None or not control.authentication_enabled or hyperhdr is None:
            if hyperhdr is not None:
                hyperhdr.audit_denied(operation, AuditReason.AUTHENTICATION_DISABLED)
            self.close_connection = True
            self._control_unavailable(html=True)
            return
        _, session = self._request_session(control)
        if session is None:
            hyperhdr.audit_denied(operation, AuditReason.AUTHENTICATION_REQUIRED)
            self._redirect("/login?next=%2Fcontrols%2Fhyperhdr")
            return

        allowed_fields = {"csrf_token"}
        if operation in {
            HyperHDROperation.VIDEO_GRABBER_DISABLE,
            HyperHDROperation.LED_OUTPUT_DISABLE,
        }:
            allowed_fields.add("confirmation")
        fields, error = self._read_form(
            maximum_body_bytes=HYPERHDR_CONTROL_BODY_LIMIT,
            allowed_fields=frozenset(allowed_fields),
        )
        if error is not None or fields is None:
            active_error = error or FormError(
                HTTPStatus.BAD_REQUEST,
                AuditReason.MALFORMED_FORM,
            )
            hyperhdr.audit_denied(operation, active_error.reason)
            self._send(
                b"Unable to process the HyperHDR operation request.\n",
                "text/plain; charset=utf-8",
                active_error.status,
            )
            return

        csrf_token = fields.get("csrf_token")
        client_identifier = self._client_identifier()
        if operation is HyperHDROperation.VIDEO_GRABBER_ENABLE:
            result = hyperhdr.video_grabber_enable(
                session,
                csrf_token,
                client_identifier,
            )
        elif operation is HyperHDROperation.VIDEO_GRABBER_DISABLE:
            result = hyperhdr.video_grabber_disable(
                session,
                csrf_token,
                fields.get("confirmation"),
                client_identifier,
            )
        elif operation is HyperHDROperation.LED_OUTPUT_ENABLE:
            result = hyperhdr.led_output_enable(
                session,
                csrf_token,
                client_identifier,
            )
        else:
            result = hyperhdr.led_output_disable(
                session,
                csrf_token,
                fields.get("confirmation"),
                client_identifier,
            )
        self._redirect(f"/controls/hyperhdr?notice={_hyperhdr_result_notice(result)}")

    def _post_login(self) -> None:
        control = self._control_plane()
        if control is None or not control.authentication_enabled:
            self.close_connection = True
            self._control_unavailable(html=True)
            return
        fields, error = self._read_form(
            maximum_body_bytes=LOGIN_BODY_LIMIT,
            allowed_fields=frozenset({"username", "password", "next"}),
            required_fields=frozenset({"username", "password"}),
        )
        if error is not None or fields is None:
            active_error = error or FormError(
                HTTPStatus.BAD_REQUEST,
                AuditReason.MALFORMED_FORM,
            )
            control.audit_malformed_request(active_error.reason)
            self._send(
                render_login(
                    authentication_enabled=True,
                    next_path="/controls",
                    error_message="Unable to process the authentication request.",
                ).encode(),
                "text/html; charset=utf-8",
                active_error.status,
            )
            return
        if len(fields["username"]) > 64 or len(fields["password"]) > 1024:
            control.audit_malformed_request(AuditReason.MALFORMED_FORM)
            self._send(
                render_login(
                    authentication_enabled=True,
                    next_path="/controls",
                    error_message="Unable to process the authentication request.",
                ).encode(),
                "text/html; charset=utf-8",
                HTTPStatus.BAD_REQUEST,
            )
            return

        prior_token, _ = self._request_session(control)
        result = control.authenticate(
            fields["username"],
            fields["password"],
            self._client_identifier(),
            prior_session_token=prior_token,
        )
        if result.status is LoginStatus.SUCCESS:
            assert result.created_session is not None
            cookie = session_cookie(
                result.created_session.token,
                max_age_seconds=control.session_ttl_seconds,
                secure=control.secure_cookie,
            )
            self._redirect(
                safe_next_path(fields.get("next")),
                cookie=cookie,
            )
            return
        status = (
            HTTPStatus.TOO_MANY_REQUESTS
            if result.status is LoginStatus.RATE_LIMITED
            else HTTPStatus.UNAUTHORIZED
        )
        self._send(
            render_login(
                authentication_enabled=True,
                next_path=safe_next_path(fields.get("next")),
                error_message="Authentication failed.",
            ).encode(),
            "text/html; charset=utf-8",
            status,
        )

    def _post_logout(self) -> None:
        control = self._control_plane()
        if control is None or not control.authentication_enabled:
            self.close_connection = True
            if control is not None:
                control.audit_page_denied(AuditReason.AUTHENTICATION_DISABLED)
            self._control_unavailable(html=True)
            return
        session_token, session = self._request_session(control)
        if session_token is None or session is None:
            self.close_connection = True
            control.audit_page_denied()
            self._send(
                b"Authentication required.\n",
                "text/plain; charset=utf-8",
                HTTPStatus.UNAUTHORIZED,
                {"Set-Cookie": cleared_session_cookie(secure=control.secure_cookie)},
            )
            return
        fields, error = self._read_form(
            maximum_body_bytes=LOGOUT_BODY_LIMIT,
            allowed_fields=frozenset({"csrf_token"}),
        )
        if error is not None or fields is None:
            active_error = error or FormError(
                HTTPStatus.BAD_REQUEST,
                AuditReason.MALFORMED_FORM,
            )
            control.audit_malformed_request(active_error.reason)
            self._send(
                b"Unable to process the authentication request.\n",
                "text/plain; charset=utf-8",
                active_error.status,
            )
            return
        if not control.logout(session_token, session, fields.get("csrf_token")):
            self._send(
                b"Unable to process the authentication request.\n",
                "text/plain; charset=utf-8",
                HTTPStatus.FORBIDDEN,
            )
            return
        self._redirect(
            "/",
            cookie=cleared_session_cookie(secure=control.secure_cookie),
        )

    def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._method_not_allowed()

    def _control_plane(self) -> ControlPlaneService | None:
        return getattr(self.server, "control_plane", None)

    def _wled_control_plane(self) -> WLEDControlService | None:
        return getattr(self.server, "wled_controls", None)

    def _hyperhdr_control_plane(self) -> HyperHDRControlService | None:
        return getattr(self.server, "hyperhdr_controls", None)

    def _combined_capabilities(self) -> ControlCapabilities:
        operations: list[str] = []
        wled = self._wled_control_plane()
        if wled is not None:
            operations.extend(wled.capabilities().available_operations)
        hyperhdr = self._hyperhdr_control_plane()
        if hyperhdr is not None:
            operations.extend(hyperhdr.capabilities().available_operations)
        return ControlCapabilities(
            mutations_enabled=bool(operations),
            available_operations=tuple(operations),
        )

    def _portal_control_link(self) -> ControlNavigationLink | None:
        control = self._control_plane()
        if control is None or not control.authentication_enabled:
            return None
        _, session = self._request_session(control)
        return (
            ControlNavigationLink.CONTROLS
            if session is not None
            else ControlNavigationLink.LOGIN
        )

    def _request_session(
        self,
        control: ControlPlaneService,
    ) -> tuple[str | None, SessionContext | None]:
        cookie = read_session_cookie(self._header("Cookie"))
        if cookie.malformed:
            control.audit_malformed_cookie()
            return None, None
        return cookie.token, control.resolve_session(cookie.token)

    def _read_form(
        self,
        *,
        maximum_body_bytes: int,
        allowed_fields: frozenset[str],
        required_fields: frozenset[str] = frozenset(),
    ) -> tuple[dict[str, str] | None, FormError | None]:
        length, error = validate_form_headers(
            content_types=self._header_values("Content-Type"),
            content_lengths=self._header_values("Content-Length"),
            transfer_encoding=self._header("Transfer-Encoding"),
            maximum_body_bytes=maximum_body_bytes,
        )
        if error is not None or length is None:
            self.close_connection = True
            return None, error
        body = self.rfile.read(length)
        if len(body) != length:
            self.close_connection = True
            return None, FormError(
                HTTPStatus.BAD_REQUEST,
                AuditReason.MALFORMED_FORM,
            )
        fields = parse_form(
            body,
            allowed_fields=allowed_fields,
            required_fields=required_fields,
        )
        if fields is None:
            return None, FormError(
                HTTPStatus.BAD_REQUEST,
                AuditReason.MALFORMED_FORM,
            )
        return fields, None

    def _query_next_path(self) -> str:
        try:
            query = parse_qs(
                urlsplit(self.path).query,
                keep_blank_values=True,
                max_num_fields=4,
            )
        except ValueError:
            return "/controls"
        values = query.get("next")
        candidate = values[0] if values is not None and len(values) == 1 else None
        return safe_next_path(candidate)

    def _wled_notice(self) -> str | None:
        try:
            query = parse_qs(
                urlsplit(self.path).query,
                keep_blank_values=True,
                max_num_fields=2,
            )
        except ValueError:
            return None
        values = query.get("notice")
        if values is None or len(values) != 1 or values[0] not in _WLED_NOTICE_VALUES:
            return None
        return values[0]

    def _hyperhdr_notice(self) -> str | None:
        try:
            query = parse_qs(
                urlsplit(self.path).query,
                keep_blank_values=True,
                max_num_fields=2,
            )
        except ValueError:
            return None
        values = query.get("notice")
        if (
            values is None
            or len(values) != 1
            or values[0] not in _HYPERHDR_NOTICE_VALUES
        ):
            return None
        return values[0]

    def _client_identifier(self) -> str:
        address = getattr(self, "client_address", None)
        if isinstance(address, tuple) and address:
            return str(address[0])
        return "unidentified-client"

    def _header(self, name: str) -> str | None:
        headers = getattr(self, "headers", None)
        return None if headers is None else headers.get(name)

    def _header_values(self, name: str) -> list[str] | None:
        headers = getattr(self, "headers", None)
        if headers is None:
            return None
        values = headers.get_all(name)
        return None if values is None else list(values)

    def _redirect(self, location: str, *, cookie: str | None = None) -> None:
        headers = {"Location": location}
        if cookie is not None:
            headers["Set-Cookie"] = cookie
        self._send(
            b"",
            "text/plain; charset=utf-8",
            HTTPStatus.SEE_OTHER,
            headers,
        )

    def _send_json(
        self,
        payload: Mapping[str, object],
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode()
        self._send(body, "application/json; charset=utf-8", status)

    def _control_unavailable(self, *, html: bool) -> None:
        if html:
            body = render_login(
                authentication_enabled=False,
                next_path="/controls",
            ).encode()
            self._send(
                body,
                "text/html; charset=utf-8",
                HTTPStatus.NOT_FOUND,
            )
            return
        self._send_json(
            {
                "schema_version": 1,
                "error": "control_plane_unavailable",
            },
            HTTPStatus.NOT_FOUND,
        )

    def log_message(self, format: str, *args: object) -> None:
        """Suppress routine request logging."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Project Aurora read-only health dashboard"
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Path to the existing Aurora YAML configuration.",
    )
    parser.add_argument(
        "--host",
        help="Override dashboard.bind_host.",
    )
    parser.add_argument(
        "--port",
        type=int,
        help="Override dashboard.port.",
    )
    parser.add_argument(
        "--refresh-seconds",
        type=int,
        help="Override dashboard.refresh_seconds.",
    )
    return parser


def _cli_overrides(args: argparse.Namespace) -> dict[str, object] | None:
    dashboard: dict[str, object] = {}
    if args.host is not None:
        dashboard["bind_host"] = args.host
    if args.port is not None:
        dashboard["port"] = args.port
    if args.refresh_seconds is not None:
        dashboard["refresh_seconds"] = args.refresh_seconds
    return {"dashboard": dashboard} if dashboard else None


def build_server(
    settings: AuroraSettings,
    *,
    service: HealthService | None = None,
    control_plane: ControlPlaneService | None = None,
    wled_controls: WLEDControlService | None = None,
    hyperhdr_controls: HyperHDRControlService | None = None,
    port: int | None = None,
) -> DashboardHTTPServer:
    """Build a server without starting its request loop."""
    active_service = HealthService(settings) if service is None else service
    active_control_plane = (
        ControlPlaneService(settings.dashboard.authentication)
        if control_plane is None
        else control_plane
    )
    active_wled_controls = (
        WLEDControlService(
            settings.wled,
            authentication_enabled=active_control_plane.authentication_enabled,
            cache_invalidator=active_service.invalidate,
        )
        if wled_controls is None
        else wled_controls
    )
    active_hyperhdr_controls = (
        HyperHDRControlService(
            settings.hyperhdr,
            authentication_enabled=active_control_plane.authentication_enabled,
            cache_invalidator=active_service.invalidate,
        )
        if hyperhdr_controls is None
        else hyperhdr_controls
    )
    address = (
        settings.dashboard.bind_host,
        settings.dashboard.port if port is None else port,
    )
    server_type = (
        DashboardIPv6HTTPServer
        if ":" in settings.dashboard.bind_host
        else DashboardHTTPServer
    )
    return server_type(
        address,
        active_service,
        settings.dashboard.refresh_seconds,
        active_control_plane,
        active_wled_controls,
        active_hyperhdr_controls,
        settings.application.configuration_profile,
    )


def main() -> int:
    """Run the local dashboard until interrupted."""
    args = build_parser().parse_args()
    try:
        settings = load_settings(
            config_path=args.config,
            cli_overrides=_cli_overrides(args),
        )
    except AuroraConfigurationError as error:
        print(f"Dashboard configuration failed: {error}", file=sys.stderr)
        return 2
    try:
        server = build_server(settings)
    except OSError:
        print("Dashboard could not bind the configured local address.", file=sys.stderr)
        return 1
    print(
        "Project Aurora dashboard: "
        f"http://{settings.dashboard.bind_host}:{settings.dashboard.port}"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
