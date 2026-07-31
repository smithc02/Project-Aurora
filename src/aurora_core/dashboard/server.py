"""Dependency-free local HTTP server for the Aurora health dashboard."""

from __future__ import annotations

import argparse
import html
import json
import socket
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from aurora_core.config import AuroraConfigurationError, load_settings
from aurora_core.config.models import AuroraSettings
from aurora_core.dashboard.models import HealthReport
from aurora_core.dashboard.service import HealthService


def _display_value(value: object) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def _render_page(report: HealthReport, refresh_seconds: int) -> str:
    cards: list[str] = []
    for component in report.components:
        details = "".join(
            (
                f"<dt>{html.escape(key.replace('_', ' ').title())}</dt>"
                f"<dd>{html.escape(_display_value(value))}</dd>"
            )
            for key, value in component.details.items()
            if value is not None
        )
        last_success = component.last_successful_at or "No successful observation"
        cards.append(
            f"""
            <article class="card {component.status}">
              <div class="card-head">
                <h2>{html.escape(component.name.replace("_", " ").title())}</h2>
                <span>{html.escape(component.status)}</span>
              </div>
              <p>{html.escape(component.message)}</p>
              <dl>{details}</dl>
              <small>Latency: {component.latency_ms} ms<br>
              Last successful observation: {html.escape(last_success)}</small>
            </article>
            """
        )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="{refresh_seconds}">
<title>Project Aurora Health</title>
<style>
:root{{
  color-scheme:dark;background:#0c1017;color:#eef3f8;
  font-family:system-ui,sans-serif
}}
body{{max-width:1100px;margin:auto;padding:24px}}
header{{
  display:flex;justify-content:space-between;gap:16px;align-items:center
}}
.badge,.card-head span{{
  padding:6px 10px;border-radius:999px;text-transform:uppercase;
  font-size:.75rem;font-weight:700
}}
.healthy .card-head span,.badge.healthy{{background:#153d2d;color:#7ce3b0}}
.degraded .card-head span,.badge.degraded{{background:#493b12;color:#ffd76b}}
.unavailable .card-head span,.badge.unavailable{{
  background:#4b1d26;color:#ff9cab
}}
main{{
  display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));
  gap:16px;margin-top:24px
}}
.card{{
  background:#151b25;border:1px solid #2a3444;border-radius:14px;padding:18px
}}
.card-head{{display:flex;justify-content:space-between;gap:10px;align-items:center}}
h1,h2{{margin:0}}h2{{font-size:1.05rem}}p{{color:#b8c2d0}}
dl{{display:grid;grid-template-columns:1fr 1fr;gap:6px 12px}}
dt{{color:#8d9aad}}dd{{margin:0;text-align:right;overflow-wrap:anywhere}}
small{{color:#7f8b9a}}
</style></head><body><header><div><h1>Project Aurora</h1>
<p>Read-only system health dashboard</p></div>
<span class="badge {report.status}">{report.status}</span></header>
<p>Last checked: {html.escape(report.checked_at)} · Dashboard uptime:
{report.service_uptime_seconds}s</p><main>{"".join(cards)}</main></body></html>"""


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
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
        )
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlsplit(self.path).path
        if path not in {"/", "/api/health"}:
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
        page = _render_page(report, server.refresh_seconds).encode()
        self._send(page, "text/html; charset=utf-8")

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
