"""Narrow standard-library transport for WLED's fixed read-only info endpoint."""

from __future__ import annotations

import socket
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from aurora_core.hardware.errors import (
    WLEDHTTPError,
    WLEDRedirectError,
    WLEDResponseTooLargeError,
    WLEDTimeoutError,
    WLEDTransportError,
)

MAX_RESPONSE_BYTES = 64 * 1024


class WLEDInfoTransport(Protocol):
    def fetch_info(self, *, host: str, port: int, timeout_seconds: float) -> bytes: ...


class WLEDStateTransport(Protocol):
    def fetch_state(self, *, host: str, port: int, timeout_seconds: float) -> bytes: ...


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> Request | None:
        raise WLEDRedirectError()


def _info_url(host: str, port: int) -> str:
    return _wled_url(host, port, "/json/info")


def _state_url(host: str, port: int) -> str:
    return _wled_url(host, port, "/json/state")


def _wled_url(host: str, port: int, path: str) -> str:
    bracketed = f"[{host}]" if ":" in host else host
    return f"http://{bracketed}:{port}{path}"


def _fetch_wled_json(*, url: str, timeout_seconds: float) -> bytes:
    """Fetch one fixed WLED JSON resource without redirects or unbounded reads."""
    request = Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "Project-Aurora"},
        method="GET",
    )
    try:
        response = build_opener(_RejectRedirects()).open(
            request, timeout=timeout_seconds
        )
        with response:
            status = response.getcode()
            if not isinstance(status, int) or not 200 <= status < 300:
                raise WLEDHTTPError()
            body = cast(bytes, response.read(MAX_RESPONSE_BYTES + 1))
    except WLEDTransportError:
        raise
    except HTTPError as error:
        if 300 <= error.code < 400:
            raise WLEDRedirectError() from error
        raise WLEDHTTPError() from error
    except TimeoutError as error:
        raise WLEDTimeoutError() from error
    except URLError as error:
        if isinstance(error.reason, (TimeoutError, socket.timeout)):
            raise WLEDTimeoutError() from error
        raise WLEDTransportError() from error
    except OSError as error:
        raise WLEDTransportError() from error
    if len(body) > MAX_RESPONSE_BYTES:
        raise WLEDResponseTooLargeError()
    return body


class UrllibWLEDInfoTransport:
    """Fetch exactly one GET /json/info response with no redirect following."""

    def fetch_info(self, *, host: str, port: int, timeout_seconds: float) -> bytes:
        return _fetch_wled_json(
            url=_info_url(host, port), timeout_seconds=timeout_seconds
        )


class UrllibWLEDStateTransport:
    """Fetch exactly one GET /json/state response with no redirect following."""

    def fetch_state(self, *, host: str, port: int, timeout_seconds: float) -> bytes:
        return _fetch_wled_json(
            url=_state_url(host, port), timeout_seconds=timeout_seconds
        )
