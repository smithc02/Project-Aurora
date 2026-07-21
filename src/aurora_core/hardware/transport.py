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
    bracketed = f"[{host}]" if ":" in host else host
    return f"http://{bracketed}:{port}/json/info"


class UrllibWLEDInfoTransport:
    """Fetch exactly one GET /json/info response with no redirect following."""

    def fetch_info(self, *, host: str, port: int, timeout_seconds: float) -> bytes:
        request = Request(
            _info_url(host, port),
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
