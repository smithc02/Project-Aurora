"""Fixed-destination HyperHDR mutation and read-only verification adapter."""

from __future__ import annotations

import json
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from aurora_core.config.models import HyperHDROperation
from aurora_core.hardware.errors import (
    HyperHDRAuthorizationError,
    HyperHDRHTTPError,
    HyperHDRRedirectError,
    HyperHDRResponseTooLargeError,
    HyperHDRTimeoutError,
    HyperHDRTransportError,
)
from aurora_core.hardware.hyperhdr import parse_hyperhdr_server_info
from aurora_core.hardware.hyperhdr_transport import (
    HyperHDRServerInfoTransport,
    UrllibHyperHDRServerInfoTransport,
)

MAX_MUTATION_RESPONSE_BYTES = 16 * 1024
_COMMAND = "componentstate"


class HyperHDRAdapterReason(StrEnum):
    VERIFIED = "verified"
    CONNECTION_FAILURE = "connection_failure"
    TIMEOUT = "timeout"
    HTTP_REJECTION = "http_rejection"
    REDIRECT_REJECTION = "redirect_rejection"
    UNAUTHORIZED_RESPONSE = "unauthorized_response"
    OVERSIZED_RESPONSE = "oversized_response"
    MALFORMED_JSON = "malformed_json"
    NON_OBJECT_JSON = "non_object_json"
    MISSING_SUCCESS = "missing_success"
    SUCCESS_WRONG_TYPE = "success_wrong_type"
    SUCCESS_FALSE = "success_false"
    MISMATCHED_COMMAND = "mismatched_command"
    VERIFICATION_CONNECTION_FAILURE = "verification_connection_failure"
    VERIFICATION_TIMEOUT = "verification_timeout"
    VERIFICATION_HTTP_REJECTION = "verification_http_rejection"
    VERIFICATION_REDIRECT_REJECTION = "verification_redirect_rejection"
    VERIFICATION_UNAUTHORIZED_RESPONSE = "verification_unauthorized_response"
    VERIFICATION_OVERSIZED_RESPONSE = "verification_oversized_response"
    VERIFICATION_MALFORMED_RESPONSE = "verification_malformed_response"
    MISSING_COMPONENT_STATE = "missing_component_state"
    AMBIGUOUS_COMPONENT_STATE = "ambiguous_component_state"
    STATE_VERIFICATION_MISMATCH = "state_verification_mismatch"


@dataclass(frozen=True, slots=True)
class HyperHDRAdapterResult:
    verified: bool
    acknowledged: bool
    reason: HyperHDRAdapterReason


class HyperHDRMutationTransportFailure(Exception):
    """A fixed mutation transport reason; never a raw transport exception."""

    def __init__(self, reason: HyperHDRAdapterReason) -> None:
        if reason not in {
            HyperHDRAdapterReason.CONNECTION_FAILURE,
            HyperHDRAdapterReason.TIMEOUT,
            HyperHDRAdapterReason.HTTP_REJECTION,
            HyperHDRAdapterReason.REDIRECT_REJECTION,
            HyperHDRAdapterReason.UNAUTHORIZED_RESPONSE,
            HyperHDRAdapterReason.OVERSIZED_RESPONSE,
        }:
            raise ValueError("unsupported HyperHDR mutation transport reason")
        self.reason = reason
        super().__init__(reason.value)


class HyperHDRMutationTransport(Protocol):
    def post_component_state(
        self,
        *,
        host: str,
        port: int,
        timeout_seconds: float,
        payload: bytes,
    ) -> bytes: ...


class HyperHDRMutationExecutor(Protocol):
    def execute(self, operation: HyperHDROperation) -> HyperHDRAdapterResult: ...


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
        raise HyperHDRMutationTransportFailure(HyperHDRAdapterReason.REDIRECT_REJECTION)


def _json_rpc_url(host: str, port: int) -> str:
    bracketed = f"[{host}]" if ":" in host else host
    return f"http://{bracketed}:{port}/json-rpc"


class UrllibHyperHDRMutationTransport:
    """POST exactly once to the fixed HyperHDR JSON-RPC resource."""

    def post_component_state(
        self,
        *,
        host: str,
        port: int,
        timeout_seconds: float,
        payload: bytes,
    ) -> bytes:
        request = Request(
            _json_rpc_url(host, port),
            data=payload,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "Project-Aurora",
            },
            method="POST",
        )
        try:
            response = build_opener(_RejectRedirects()).open(
                request,
                timeout=timeout_seconds,
            )
            with response:
                status = response.getcode()
                if not isinstance(status, int) or not 200 <= status < 300:
                    raise HyperHDRMutationTransportFailure(
                        HyperHDRAdapterReason.HTTP_REJECTION
                    )
                body = cast(bytes, response.read(MAX_MUTATION_RESPONSE_BYTES + 1))
        except HyperHDRMutationTransportFailure:
            raise
        except HTTPError as error:
            if 300 <= error.code < 400:
                reason = HyperHDRAdapterReason.REDIRECT_REJECTION
            elif error.code in {401, 403}:
                reason = HyperHDRAdapterReason.UNAUTHORIZED_RESPONSE
            else:
                reason = HyperHDRAdapterReason.HTTP_REJECTION
            raise HyperHDRMutationTransportFailure(reason) from error
        except TimeoutError as error:
            raise HyperHDRMutationTransportFailure(
                HyperHDRAdapterReason.TIMEOUT
            ) from error
        except URLError as error:
            reason = (
                HyperHDRAdapterReason.TIMEOUT
                if isinstance(error.reason, (TimeoutError, socket.timeout))
                else HyperHDRAdapterReason.CONNECTION_FAILURE
            )
            raise HyperHDRMutationTransportFailure(reason) from error
        except OSError as error:
            raise HyperHDRMutationTransportFailure(
                HyperHDRAdapterReason.CONNECTION_FAILURE
            ) from error
        if len(body) > MAX_MUTATION_RESPONSE_BYTES:
            raise HyperHDRMutationTransportFailure(
                HyperHDRAdapterReason.OVERSIZED_RESPONSE
            )
        return body


_OPERATION_COMPONENT_STATE: dict[HyperHDROperation, tuple[str, bool]] = {
    HyperHDROperation.VIDEO_GRABBER_ENABLE: ("VIDEOGRABBER", True),
    HyperHDROperation.VIDEO_GRABBER_DISABLE: ("VIDEOGRABBER", False),
    HyperHDROperation.LED_OUTPUT_ENABLE: ("LEDDEVICE", True),
    HyperHDROperation.LED_OUTPUT_DISABLE: ("LEDDEVICE", False),
}


class HyperHDRMutationAdapter:
    """Generate one fixed mutation, then verify with one fixed serverinfo GET."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        timeout_seconds: float,
        mutation_transport: HyperHDRMutationTransport | None = None,
        server_info_transport: HyperHDRServerInfoTransport | None = None,
    ) -> None:
        if not host or not 1 <= port <= 65535 or not 0.1 <= timeout_seconds <= 5.0:
            raise ValueError("validated HyperHDR destination and timeout are required")
        self._host = host
        self._port = port
        self._timeout_seconds = timeout_seconds
        self._mutation_transport = (
            UrllibHyperHDRMutationTransport()
            if mutation_transport is None
            else mutation_transport
        )
        self._server_info_transport = (
            UrllibHyperHDRServerInfoTransport()
            if server_info_transport is None
            else server_info_transport
        )

    def execute(self, operation: HyperHDROperation) -> HyperHDRAdapterResult:
        component, requested_state = _OPERATION_COMPONENT_STATE[operation]
        payload = {
            "command": _COMMAND,
            "componentstate": {
                "component": component,
                "state": requested_state,
            },
        }
        encoded = json.dumps(payload, separators=(",", ":")).encode("ascii")
        try:
            body = self._mutation_transport.post_component_state(
                host=self._host,
                port=self._port,
                timeout_seconds=self._timeout_seconds,
                payload=encoded,
            )
        except HyperHDRMutationTransportFailure as error:
            return HyperHDRAdapterResult(False, False, error.reason)
        if len(body) > MAX_MUTATION_RESPONSE_BYTES:
            return HyperHDRAdapterResult(
                False,
                False,
                HyperHDRAdapterReason.OVERSIZED_RESPONSE,
            )
        acknowledgement_failure = _acknowledgement_failure(body)
        if acknowledgement_failure is not None:
            return HyperHDRAdapterResult(False, False, acknowledgement_failure)
        return self._verify_component(component, requested_state)

    def _verify_component(
        self,
        component: str,
        requested_state: bool,
    ) -> HyperHDRAdapterResult:
        try:
            body = self._server_info_transport.fetch_server_info(
                host=self._host,
                port=self._port,
                timeout_seconds=self._timeout_seconds,
            )
            info = parse_hyperhdr_server_info(body)
        except HyperHDRTimeoutError:
            reason = HyperHDRAdapterReason.VERIFICATION_TIMEOUT
        except HyperHDRRedirectError:
            reason = HyperHDRAdapterReason.VERIFICATION_REDIRECT_REJECTION
        except HyperHDRAuthorizationError:
            reason = HyperHDRAdapterReason.VERIFICATION_UNAUTHORIZED_RESPONSE
        except HyperHDRResponseTooLargeError:
            reason = HyperHDRAdapterReason.VERIFICATION_OVERSIZED_RESPONSE
        except HyperHDRHTTPError:
            reason = HyperHDRAdapterReason.VERIFICATION_HTTP_REJECTION
        except HyperHDRTransportError:
            reason = HyperHDRAdapterReason.VERIFICATION_CONNECTION_FAILURE
        except ValueError:
            reason = HyperHDRAdapterReason.VERIFICATION_MALFORMED_RESPONSE
        else:
            if component in info.ambiguous_component_states:
                reason = HyperHDRAdapterReason.AMBIGUOUS_COMPONENT_STATE
            elif component not in info.component_state_names:
                reason = HyperHDRAdapterReason.MISSING_COMPONENT_STATE
            else:
                actual = (
                    info.grabber_active
                    if component == "VIDEOGRABBER"
                    else info.led_output_active
                )
                if actual is None:
                    reason = HyperHDRAdapterReason.AMBIGUOUS_COMPONENT_STATE
                elif actual is not requested_state:
                    reason = HyperHDRAdapterReason.STATE_VERIFICATION_MISMATCH
                else:
                    return HyperHDRAdapterResult(
                        True,
                        True,
                        HyperHDRAdapterReason.VERIFIED,
                    )
        return HyperHDRAdapterResult(False, True, reason)


def _acknowledgement_failure(body: bytes) -> HyperHDRAdapterReason | None:
    try:
        document = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return HyperHDRAdapterReason.MALFORMED_JSON
    if not isinstance(document, Mapping):
        return HyperHDRAdapterReason.NON_OBJECT_JSON
    if "success" not in document:
        return HyperHDRAdapterReason.MISSING_SUCCESS
    success = document["success"]
    if type(success) is not bool:
        return HyperHDRAdapterReason.SUCCESS_WRONG_TYPE
    if success is not True:
        return HyperHDRAdapterReason.SUCCESS_FALSE
    if "command" in document and document["command"] != _COMMAND:
        return HyperHDRAdapterReason.MISMATCHED_COMMAND
    return None
