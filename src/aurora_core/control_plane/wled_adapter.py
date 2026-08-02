"""Fixed-destination synchronous WLED mutation adapter."""

from __future__ import annotations

import json
import socket
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from aurora_core.config.models import WLEDOperation
from aurora_core.control_plane.contracts import BrightnessInput, NoOperationInput
from aurora_core.hardware.transport import _state_url

MAX_MUTATION_RESPONSE_BYTES = 16 * 1024


class AdapterReason(StrEnum):
    VERIFIED = "verified"
    CONNECTION_FAILURE = "connection_failure"
    TIMEOUT = "timeout"
    HTTP_REJECTION = "http_rejection"
    REDIRECT_REJECTION = "redirect_rejection"
    OVERSIZED_RESPONSE = "oversized_response"
    MALFORMED_JSON = "malformed_json"
    MISSING_EXPECTED_STATE = "missing_expected_state"
    STATE_VERIFICATION_MISMATCH = "state_verification_mismatch"


@dataclass(frozen=True, slots=True)
class AdapterResult:
    verified: bool
    reason: AdapterReason


class WLEDMutationTransportFailure(Exception):
    """A fixed reason code from the bounded transport; never a raw exception."""

    def __init__(self, reason: AdapterReason) -> None:
        if reason not in {
            AdapterReason.CONNECTION_FAILURE,
            AdapterReason.TIMEOUT,
            AdapterReason.HTTP_REJECTION,
            AdapterReason.REDIRECT_REJECTION,
            AdapterReason.OVERSIZED_RESPONSE,
        }:
            raise ValueError("unsupported transport failure reason")
        self.reason = reason
        super().__init__(reason.value)


class WLEDMutationTransport(Protocol):
    def post_state(
        self,
        *,
        host: str,
        port: int,
        timeout_seconds: float,
        payload: bytes,
    ) -> bytes: ...


class WLEDMutationExecutor(Protocol):
    def execute(
        self,
        operation: WLEDOperation,
        operation_input: NoOperationInput | BrightnessInput,
    ) -> AdapterResult: ...


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
        raise WLEDMutationTransportFailure(AdapterReason.REDIRECT_REJECTION)


class UrllibWLEDMutationTransport:
    """POST exactly once to the fixed WLED state resource without redirects."""

    def post_state(
        self,
        *,
        host: str,
        port: int,
        timeout_seconds: float,
        payload: bytes,
    ) -> bytes:
        request = Request(
            _state_url(host, port),
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
                    raise WLEDMutationTransportFailure(AdapterReason.HTTP_REJECTION)
                body = cast(bytes, response.read(MAX_MUTATION_RESPONSE_BYTES + 1))
        except WLEDMutationTransportFailure:
            raise
        except HTTPError as error:
            if 300 <= error.code < 400:
                raise WLEDMutationTransportFailure(
                    AdapterReason.REDIRECT_REJECTION
                ) from error
            raise WLEDMutationTransportFailure(AdapterReason.HTTP_REJECTION) from error
        except TimeoutError as error:
            raise WLEDMutationTransportFailure(AdapterReason.TIMEOUT) from error
        except URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise WLEDMutationTransportFailure(AdapterReason.TIMEOUT) from error
            raise WLEDMutationTransportFailure(
                AdapterReason.CONNECTION_FAILURE
            ) from error
        except OSError as error:
            raise WLEDMutationTransportFailure(
                AdapterReason.CONNECTION_FAILURE
            ) from error
        if len(body) > MAX_MUTATION_RESPONSE_BYTES:
            raise WLEDMutationTransportFailure(AdapterReason.OVERSIZED_RESPONSE)
        return body


class WLEDMutationAdapter:
    """Generate fixed payloads and verify the one bounded response."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        timeout_seconds: float,
        transport: WLEDMutationTransport | None = None,
    ) -> None:
        if not host or not 0.1 <= timeout_seconds <= 5.0:
            raise ValueError("validated WLED destination and timeout are required")
        self._host = host
        self._port = port
        self._timeout_seconds = timeout_seconds
        self._transport = (
            UrllibWLEDMutationTransport() if transport is None else transport
        )

    def execute(
        self,
        operation: WLEDOperation,
        operation_input: NoOperationInput | BrightnessInput,
    ) -> AdapterResult:
        payload = _operation_payload(operation, operation_input)
        encoded = json.dumps(payload, separators=(",", ":")).encode("ascii")
        try:
            body = self._transport.post_state(
                host=self._host,
                port=self._port,
                timeout_seconds=self._timeout_seconds,
                payload=encoded,
            )
        except WLEDMutationTransportFailure as error:
            return AdapterResult(False, error.reason)
        if len(body) > MAX_MUTATION_RESPONSE_BYTES:
            return AdapterResult(False, AdapterReason.OVERSIZED_RESPONSE)
        return _verify_response(operation, operation_input, body)


def _operation_payload(
    operation: WLEDOperation,
    operation_input: NoOperationInput | BrightnessInput,
) -> Mapping[str, bool | int]:
    if operation is WLEDOperation.POWER_ON and isinstance(
        operation_input, NoOperationInput
    ):
        return {"on": True, "v": True}
    if operation is WLEDOperation.POWER_OFF and isinstance(
        operation_input, NoOperationInput
    ):
        return {"on": False, "v": True}
    if operation is WLEDOperation.BRIGHTNESS_SET and isinstance(
        operation_input, BrightnessInput
    ):
        return {"bri": operation_input.brightness, "v": True}
    raise ValueError("operation input does not match the fixed contract")


def _verify_response(
    operation: WLEDOperation,
    operation_input: NoOperationInput | BrightnessInput,
    body: bytes,
) -> AdapterResult:
    try:
        document = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return AdapterResult(False, AdapterReason.MALFORMED_JSON)
    if not isinstance(document, dict):
        return AdapterResult(False, AdapterReason.MALFORMED_JSON)
    if operation is WLEDOperation.POWER_ON:
        return _verify_exact(document, "on", True)
    if operation is WLEDOperation.POWER_OFF:
        return _verify_exact(document, "on", False)
    if not isinstance(operation_input, BrightnessInput):
        raise ValueError("brightness operation requires typed brightness input")
    return _verify_exact(document, "bri", operation_input.brightness)


def _verify_exact(
    document: dict[object, object],
    key: str,
    expected: bool | int,
) -> AdapterResult:
    if key not in document:
        return AdapterResult(False, AdapterReason.MISSING_EXPECTED_STATE)
    actual = document[key]
    if type(actual) is not type(expected) or actual != expected:
        return AdapterResult(False, AdapterReason.STATE_VERIFICATION_MISMATCH)
    return AdapterResult(True, AdapterReason.VERIFIED)
