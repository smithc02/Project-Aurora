"""Service boundary for explicit bounded DDP output validation."""

from __future__ import annotations

from typing import Protocol

from aurora_core.config.models import AuroraSettings
from aurora_core.hardware.ddp_output_probe import (
    DDP_MAX_DATA_PAYLOAD,
    DDP_MAX_LEDS,
    BoundedDDPOutputProbe,
)
from aurora_core.hardware.models import (
    DDPOutputProbeResult,
    DDPOutputValidationReport,
)
from aurora_core.runtime.models import ComponentHealthState, ComponentId


class DDPOutputProbe(Protocol):
    def probe(
        self, *, host: str, port: int, led_count: int
    ) -> DDPOutputProbeResult: ...


def _expected_packet_count(frame_payload_bytes: int) -> int:
    return (frame_payload_bytes + DDP_MAX_DATA_PAYLOAD - 1) // DDP_MAX_DATA_PAYLOAD


def _complete_result(result: DDPOutputProbeResult) -> bool:
    return (
        result.test_packets_sent == result.test_packets_planned
        and result.blackout_packets_sent == result.blackout_packets_planned
        and result.test_frame_completed
        and result.blackout_attempted
        and result.blackout_completed
        and result.socket_was_created
    )


def _healthy_result(result: DDPOutputProbeResult) -> bool:
    return (
        result.reason_code == "validated"
        and _complete_result(result)
        and result.socket_was_closed
        and result.cleanup_completed
        and not _unsafe_activity(result)
    )


def _degraded_result(result: DDPOutputProbeResult) -> bool:
    return (
        result.reason_code == "ddp_output_cleanup_unconfirmed"
        and _complete_result(result)
        and not result.socket_was_closed
        and not result.cleanup_completed
        and not _unsafe_activity(result)
    )


def _unsafe_activity(result: DDPOutputProbeResult) -> bool:
    return any(
        (
            result.broadcast_was_used,
            result.multicast_was_used,
            result.discovery_was_used,
            result.retry_was_used,
        )
    )


def _consistent_result(result: DDPOutputProbeResult, led_count: int) -> bool:
    boolean_fields = (
        result.socket_was_created,
        result.socket_was_closed,
        result.test_frame_completed,
        result.blackout_attempted,
        result.blackout_completed,
        result.cleanup_completed,
        result.broadcast_was_used,
        result.multicast_was_used,
        result.discovery_was_used,
        result.retry_was_used,
    )
    integer_fields = (
        result.frame_payload_bytes,
        result.test_packets_planned,
        result.test_packets_sent,
        result.blackout_packets_planned,
        result.blackout_packets_sent,
    )
    if type(result.reason_code) is not str:
        return False
    if any(type(value) is not bool for value in boolean_fields):
        return False
    if any(type(value) is not int for value in integer_fields):
        return False
    if type(result.led_count) is not int or result.led_count != led_count:
        return False
    if not 1 <= result.led_count <= DDP_MAX_LEDS:
        return False
    expected_bytes = result.led_count * 3
    expected_packets = _expected_packet_count(expected_bytes)
    if (
        result.frame_payload_bytes != expected_bytes
        or result.test_packets_planned != expected_packets
        or result.blackout_packets_planned != expected_packets
        or not 1 <= expected_packets <= 2
        or not 0 <= result.test_packets_sent <= expected_packets
        or not 0 <= result.blackout_packets_sent <= expected_packets
    ):
        return False
    if result.test_frame_completed != (
        result.test_packets_sent == result.test_packets_planned
    ):
        return False
    if result.blackout_completed != (
        result.blackout_attempted
        and result.blackout_packets_sent == result.blackout_packets_planned
    ):
        return False
    if result.socket_was_closed and not result.socket_was_created:
        return False
    if result.cleanup_completed != result.socket_was_closed:
        return False
    if result.blackout_attempted != result.socket_was_created:
        return False
    if not result.socket_was_created and any(
        (
            result.test_packets_sent,
            result.blackout_packets_sent,
            result.test_frame_completed,
            result.blackout_completed,
            result.cleanup_completed,
        )
    ):
        return False
    if _unsafe_activity(result):
        return False
    if result.reason_code == "validated":
        return _healthy_result(result)
    if result.reason_code == "ddp_output_cleanup_unconfirmed":
        return _degraded_result(result)
    if result.reason_code in {
        "destination_resolution_failed",
        "destination_resolution_ambiguous",
        "destination_not_unicast",
        "socket_creation_failed",
    }:
        return not result.socket_was_created
    if result.reason_code in {
        "test_frame_deadline_exceeded",
        "test_frame_send_failed",
        "test_frame_partial_send",
    }:
        return (
            result.socket_was_created
            and not result.test_frame_completed
            and result.blackout_completed
        )
    if result.reason_code in {
        "blackout_deadline_exceeded",
        "blackout_send_failed",
        "blackout_partial_send",
    }:
        return result.socket_was_created and not result.blackout_completed
    if result.reason_code == "unexpected_ddp_output_failure":
        return (
            not result.socket_was_created
            or not result.test_frame_completed
            or not result.blackout_completed
        )
    return False


def _empty_result(
    reason_code: str, led_count: int | None = None
) -> DDPOutputProbeResult:
    return DDPOutputProbeResult(reason_code=reason_code, led_count=led_count)


def validate_ddp_output(
    settings: AuroraSettings, probe: DDPOutputProbe | None = None
) -> DDPOutputValidationReport:
    """Validate configuration, invoke one probe, and enforce result invariants."""
    if not settings.ddp.enabled:
        result = _empty_result("ddp_disabled")
        state = ComponentHealthState.DISABLED
        reason = result.reason_code
    else:
        enabled_zones = tuple(zone for zone in settings.lighting_zones if zone.enabled)
        if not enabled_zones:
            result = _empty_result("enabled_zone_required")
        elif len(enabled_zones) > 1:
            result = _empty_result("multiple_enabled_zones_not_supported")
        else:
            led_count = enabled_zones[0].led_count
            if type(led_count) is not int or led_count <= 0:
                result = _empty_result("led_count_required", led_count)
            elif led_count > DDP_MAX_LEDS:
                result = _empty_result("led_count_exceeds_limit", led_count)
            else:
                try:
                    result = (probe or BoundedDDPOutputProbe()).probe(
                        host=settings.ddp.host or "",
                        port=settings.ddp.port or 4048,
                        led_count=led_count,
                    )
                except Exception:
                    result = _empty_result("unexpected_ddp_output_failure", led_count)
                if not isinstance(result, DDPOutputProbeResult) or not (
                    _consistent_result(result, led_count)
                ):
                    if not isinstance(result, DDPOutputProbeResult):
                        result = _empty_result(
                            "unexpected_ddp_output_failure", led_count
                        )
                    reason = "unexpected_ddp_output_failure"
                    state = ComponentHealthState.UNHEALTHY
                elif _healthy_result(result):
                    reason = result.reason_code
                    state = ComponentHealthState.HEALTHY
                elif _degraded_result(result):
                    reason = result.reason_code
                    state = ComponentHealthState.DEGRADED
                else:
                    reason = result.reason_code
                    state = ComponentHealthState.UNHEALTHY
                return _report(result, state, reason)
        state = ComponentHealthState.UNHEALTHY
        reason = result.reason_code
    return _report(result, state, reason)


def _report(
    result: DDPOutputProbeResult,
    state: ComponentHealthState,
    reason_code: str,
) -> DDPOutputValidationReport:
    return DDPOutputValidationReport(
        component_id=ComponentId.DDP,
        state=state,
        reason_code=reason_code,
        message="Bounded DDP output validation completed."
        if state is ComponentHealthState.HEALTHY
        else "Bounded DDP output validation was unsuccessful.",
        socket_was_created=result.socket_was_created,
        socket_was_closed=result.socket_was_closed,
        led_count=result.led_count,
        frame_payload_bytes=result.frame_payload_bytes,
        test_packets_planned=result.test_packets_planned,
        test_packets_sent=result.test_packets_sent,
        blackout_packets_planned=result.blackout_packets_planned,
        blackout_packets_sent=result.blackout_packets_sent,
        test_frame_completed=result.test_frame_completed,
        blackout_attempted=result.blackout_attempted,
        blackout_completed=result.blackout_completed,
        cleanup_completed=result.cleanup_completed,
        broadcast_was_used=result.broadcast_was_used,
        multicast_was_used=result.multicast_was_used,
        discovery_was_used=result.discovery_was_used,
        retry_was_used=result.retry_was_used,
    )
