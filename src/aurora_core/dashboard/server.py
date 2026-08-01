"""Dependency-free local HTTP server for the Aurora health dashboard."""

from __future__ import annotations

import argparse
import json
import socket
import sys
from collections.abc import Mapping
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from aurora_core.config import AuroraConfigurationError, load_settings
from aurora_core.config.models import AuroraSettings
from aurora_core.dashboard.assets import PORTAL_CSS, PORTAL_CSS_PATH
from aurora_core.dashboard.models import HealthReport
from aurora_core.dashboard.portal import PORTAL_PATHS, render_portal
from aurora_core.dashboard.service import HealthService


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
    ) -> None:
        self.health_service = service
        self.refresh_seconds = refresh_seconds
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
            "base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
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
        if path == PORTAL_CSS_PATH:
            self._send(PORTAL_CSS, "text/css; charset=utf-8")
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
        page = render_portal(report, path, server.refresh_seconds).encode()
        self._send(page, "text/html; charset=utf-8")

    def _method_not_allowed(self) -> None:
        self.close_connection = True
        self._send(
            b"method not allowed\n",
            "text/plain; charset=utf-8",
            HTTPStatus.METHOD_NOT_ALLOWED,
            {"Allow": "GET"},
        )

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._method_not_allowed()

    def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._method_not_allowed()

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
    port: int | None = None,
) -> DashboardHTTPServer:
    """Build a server without starting its request loop."""
    active_service = HealthService(settings) if service is None else service
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
