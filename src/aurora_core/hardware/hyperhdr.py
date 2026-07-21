"""One-shot, read-only HyperHDR /json-rpc serverinfo validation."""

from __future__ import annotations

import json
from collections.abc import Mapping

from aurora_core.config.models import AuroraSettings
from aurora_core.hardware.errors import HyperHDRTransportError
from aurora_core.hardware.hyperhdr_transport import (
    HyperHDRServerInfoTransport,
    UrllibHyperHDRServerInfoTransport,
)
from aurora_core.hardware.models import HyperHDRServerInfo, HyperHDRValidationReport
from aurora_core.runtime.models import ComponentHealthState, ComponentId


def parse_hyperhdr_server_info(body: bytes) -> HyperHDRServerInfo:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid_json") from error
    if not isinstance(payload, Mapping):
        raise ValueError("invalid_response")
    success = payload.get("success")
    if success is False:
        raise ValueError("server_reported_failure")
    if not isinstance(success, bool):
        raise ValueError("invalid_response")
    command = payload.get("command")
    if command is not None and command != "serverinfo":
        raise ValueError("invalid_response")
    info = payload.get("info")
    if not isinstance(info, Mapping):
        raise ValueError("invalid_response")
    value = info.get("videomodehdr")
    hdr_mode_enabled: bool | None
    if isinstance(value, bool):
        hdr_mode_enabled = value
    elif isinstance(value, int) and value in {0, 1}:
        hdr_mode_enabled = bool(value)
    else:
        hdr_mode_enabled = None
    return HyperHDRServerInfo(True, hdr_mode_enabled)


def validate_hyperhdr(
    settings: AuroraSettings,
    transport: HyperHDRServerInfoTransport | None = None,
    *,
    timeout_seconds: float | None = None,
) -> HyperHDRValidationReport:
    if not settings.hyperhdr.enabled:
        return HyperHDRValidationReport(
            ComponentId.HYPERHDR,
            ComponentHealthState.DISABLED,
            "hyperhdr_disabled",
            "HyperHDR is disabled.",
        )
    active_transport = (
        UrllibHyperHDRServerInfoTransport() if transport is None else transport
    )
    try:
        body = active_transport.fetch_server_info(
            host=settings.hyperhdr.host or "",
            port=settings.hyperhdr.port or 8090,
            timeout_seconds=settings.hyperhdr.validation_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds,
        )
        info = parse_hyperhdr_server_info(body)
    except HyperHDRTransportError as error:
        return HyperHDRValidationReport(
            ComponentId.HYPERHDR,
            ComponentHealthState.UNHEALTHY,
            error.reason_code,
            "Read-only HyperHDR validation failed.",
        )
    except ValueError as error:
        reason = str(error)
        return HyperHDRValidationReport(
            ComponentId.HYPERHDR,
            ComponentHealthState.UNHEALTHY,
            reason,
            "HyperHDR returned an invalid server information response.",
        )
    return HyperHDRValidationReport(
        ComponentId.HYPERHDR,
        ComponentHealthState.HEALTHY,
        "validated",
        "Read-only HyperHDR validation succeeded.",
        info.server_info_received,
        info.hdr_mode_enabled,
    )
