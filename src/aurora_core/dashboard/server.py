"""Dependency-free local HTTP server for the Aurora health dashboard."""

from __future__ import annotations

import argparse
import html
import json
from dataclasses import replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from aurora_core.dashboard.service import collect_health
from aurora_core.dashboard.settings import DashboardSettings


def _render_page(report: dict[str, Any], refresh_seconds: int) -> str:
    cards: list[str] = []
    for component in report["components"]:
        details = "".join(
            f"<dt>{html.escape(str(key).replace('_', ' ').title())}</dt>"
            f"<dd>{html.escape(str(value))}</dd>"
            for key, value in component["details"].items()
            if value is not None
        )
        cards.append(
            f"""
            <article class="card {component['status']}">
              <div class="card-head">
                <h2>{html.escape(component['name'].replace('_', ' ').title())}</h2>
                <span>{html.escape(component['status'])}</span>
              </div>
              <p>{html.escape(component['message'])}</p>
              <dl>{details}</dl>
              <small>{component['latency_ms']} ms</small>
            </article>
            """
        )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="{refresh_seconds}">
<title>Project Aurora Health</title>
<style>
:root{{color-scheme:dark;background:#0c1017;color:#eef3f8;font-family:system-ui,sans-serif}}
body{{max-width:1100px;margin:auto;padding:24px}}
header{{display:flex;justify-content:space-between;gap:16px;align-items:center}}
.badge,.card-head span{{padding:6px 10px;border-radius:999px;text-transform:uppercase;font-size:.75rem;font-weight:700}}
.healthy .card-head span,.badge.healthy{{background:#153d2d;color:#7ce3b0}}
.degraded .card-head span,.badge.degraded{{background:#493b12;color:#ffd76b}}
.unavailable .card-head span,.badge.unavailable{{background:#4b1d26;color:#ff9cab}}
main{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px;margin-top:24px}}
.card{{background:#151b25;border:1px solid #2a3444;border-radius:14px;padding:18px}}
.card-head{{display:flex;justify-content:space-between;gap:10px;align-items:center}}
h1,h2{{margin:0}}h2{{font-size:1.05rem}}p{{color:#b8c2d0}}
dl{{display:grid;grid-template-columns:1fr 1fr;gap:6px 12px}}
dt{{color:#8d9aad}}dd{{margin:0;text-align:right;overflow-wrap:anywhere}}
small{{color:#7f8b9a}}
</style></head><body><header><div><h1>Project Aurora</h1>
<p>Read-only system health dashboard</p></div>
<span class="badge {report['status']}">{report['status']}</span></header>
<p>Last checked: {html.escape(report['checked_at'])} · Dashboard uptime:
{report['service_uptime_seconds']}s</p><main>{''.join(cards)}</main></body></html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    """Serve the status page and stable JSON endpoint."""

    settings = DashboardSettings()

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
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path not in {"/", "/api/health"}:
            self._send(
                b"not found\n",
                "text/plain; charset=utf-8",
                HTTPStatus.NOT_FOUND,
            )
            return
        report = collect_health(self.settings).to_dict()
        if self.path == "/api/health":
            body = json.dumps(report, separators=(",", ":")).encode()
            self._send(body, "application/json")
            return
        page = _render_page(report, self.settings.refresh_seconds).encode()
        self._send(page, "text/html; charset=utf-8")

    def log_message(self, format: str, *args: object) -> None:
        """Suppress routine request logging."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Project Aurora read-only health dashboard"
    )
    parser.add_argument(
        "--host",
        help="Bind address; defaults to AURORA_DASHBOARD_BIND_HOST or 0.0.0.0",
    )
    parser.add_argument(
        "--port",
        type=int,
        help="TCP port; defaults to AURORA_DASHBOARD_PORT or 8080",
    )
    return parser


def main() -> int:
    """Run the local dashboard until interrupted."""
    args = build_parser().parse_args()
    settings = DashboardSettings.from_environment()
    if args.host is not None:
        settings = replace(settings, bind_host=args.host)
    if args.port is not None:
        settings = replace(settings, port=args.port)
    DashboardHandler.settings = settings
    server = ThreadingHTTPServer((settings.bind_host, settings.port), DashboardHandler)
    print(f"Project Aurora dashboard: http://{settings.bind_host}:{settings.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
