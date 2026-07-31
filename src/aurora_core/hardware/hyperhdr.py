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
    component_states: dict[str, bool] = {}
    components = info.get("components")
    if isinstance(components, list):
        for component in components:
            if not isinstance(component, Mapping):
                continue
            name = component.get("name")
            enabled = component.get("enabled")
            if isinstance(name, str) and isinstance(enabled, bool):
                component_states[name.upper()] = enabled
    grabber_active = component_states.get("VIDEOGRABBER")
    if grabber_active is None:
        grabber_active = component_states.get("GRABBER")
    led_output_active = component_states.get("LEDDEVICE")

    instance_running: bool | None = None
    instances = info.get("instance")
    if not isinstance(instances, list):
        instances = info.get("instances")
    if isinstance(instances, list):
        running_values = tuple(
            instance.get("running")
            for instance in instances
            if isinstance(instance, Mapping)
            and isinstance(instance.get("running"), bool)
        )
        if running_values:
            instance_running = any(running_values)

    return HyperHDRServerInfo(
        server_info_received=True,
        hdr_mode_enabled=hdr_mode_enabled,
        instance_running=instance_running,
        grabber_active=grabber_active,
        led_output_active=led_output_active,
    )


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
        component_id=ComponentId.HYPERHDR,
        state=ComponentHealthState.HEALTHY,
        reason_code="validated",
        message="Read-only HyperHDR validation succeeded.",
        server_info_received=info.server_info_received,
        hdr_mode_enabled=info.hdr_mode_enabled,
        instance_running=info.instance_running,
        grabber_active=info.grabber_active,
        led_output_active=info.led_output_active,
    )
