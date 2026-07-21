"""Fixed, read-only standard-library transport for HyperHDR server information."""

from __future__ import annotations

import json
import socket
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from aurora_core.hardware.errors import (
    HyperHDRAuthorizationError,
    HyperHDRHTTPError,
    HyperHDRRedirectError,
    HyperHDRResponseTooLargeError,
    HyperHDRTimeoutError,
    HyperHDRTransportError,
)

MAX_RESPONSE_BYTES = 256 * 1024


class HyperHDRServerInfoTransport(Protocol):
    def fetch_server_info(
        self, *, host: str, port: int, timeout_seconds: float
    ) -> bytes: ...


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
        raise HyperHDRRedirectError()


def _serverinfo_url(host: str, port: int) -> str:
    bracketed = f"[{host}]" if ":" in host else host
    command = json.dumps({"command": "serverinfo"}, separators=(",", ":"))
    return f"http://{bracketed}:{port}/json-rpc?{urlencode({'request': command})}"


class UrllibHyperHDRServerInfoTransport:
    """Fetch exactly one GET serverinfo response without following redirects."""

    def fetch_server_info(
        self, *, host: str, port: int, timeout_seconds: float
    ) -> bytes:
        request = Request(
            _serverinfo_url(host, port),
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
                    raise HyperHDRHTTPError()
                body = cast(bytes, response.read(MAX_RESPONSE_BYTES + 1))
        except HyperHDRTransportError:
            raise
        except HTTPError as error:
            if 300 <= error.code < 400:
                raise HyperHDRRedirectError() from error
            if error.code in {401, 403}:
                raise HyperHDRAuthorizationError() from error
            raise HyperHDRHTTPError() from error
        except TimeoutError as error:
            raise HyperHDRTimeoutError() from error
        except URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise HyperHDRTimeoutError() from error
            raise HyperHDRTransportError() from error
        except OSError as error:
            raise HyperHDRTransportError() from error
        if len(body) > MAX_RESPONSE_BYTES:
            raise HyperHDRResponseTooLargeError()
        return body
