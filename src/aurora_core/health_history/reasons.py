"""Finite production normalized-reason registry.

The implementation deliberately does not import the Milestone 18 validation
registry. Parity is enforced by tests while the production boundary remains
independent.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum


class NormalizedReason(StrEnum):
    WLED_DISABLED = "wled.disabled"
    WLED_COLLECTOR_FAILED = "wled.collector_failed"
    WLED_HEALTHY = "wled.healthy"
    WLED_INFO_LED_COUNT_MISMATCH = "wled.info.led_count_mismatch"
    WLED_INFO_CONNECTION_FAILED = "wled.info.connection_failed"
    WLED_INFO_TIMEOUT = "wled.info.timeout"
    WLED_INFO_REDIRECT_REJECTED = "wled.info.redirect_rejected"
    WLED_INFO_HTTP_ERROR = "wled.info.http_error"
    WLED_INFO_RESPONSE_TOO_LARGE = "wled.info.response_too_large"
    WLED_INFO_INVALID_JSON = "wled.info.invalid_json"
    WLED_INFO_INVALID_RESPONSE = "wled.info.invalid_response"
    WLED_STATE_CONNECTION_FAILED = "wled.state.connection_failed"
    WLED_STATE_TIMEOUT = "wled.state.timeout"
    WLED_STATE_REDIRECT_REJECTED = "wled.state.redirect_rejected"
    WLED_STATE_HTTP_ERROR = "wled.state.http_error"
    WLED_STATE_RESPONSE_TOO_LARGE = "wled.state.response_too_large"
    WLED_STATE_INVALID_JSON = "wled.state.invalid_json"
    WLED_STATE_INVALID_RESPONSE = "wled.state.invalid_response"
    HYPERHDR_DISABLED = "hyperhdr.disabled"
    HYPERHDR_COLLECTOR_FAILED = "hyperhdr.collector_failed"
    HYPERHDR_HEALTHY = "hyperhdr.healthy"
    HYPERHDR_CONNECTION_FAILED = "hyperhdr.connection_failed"
    HYPERHDR_TIMEOUT = "hyperhdr.timeout"
    HYPERHDR_REDIRECT_REJECTED = "hyperhdr.redirect_rejected"
    HYPERHDR_AUTHORIZATION_REQUIRED = "hyperhdr.authorization_required"
    HYPERHDR_HTTP_ERROR = "hyperhdr.http_error"
    HYPERHDR_RESPONSE_TOO_LARGE = "hyperhdr.response_too_large"
    HYPERHDR_INVALID_JSON = "hyperhdr.invalid_json"
    HYPERHDR_INVALID_RESPONSE = "hyperhdr.invalid_response"
    HYPERHDR_SERVER_REPORTED_FAILURE = "hyperhdr.server_reported_failure"
    HYPERHDR_INSTANCE_INACTIVE = "hyperhdr.instance_inactive"
    HYPERHDR_VIDEO_GRABBER_INACTIVE = "hyperhdr.video_grabber_inactive"
    HYPERHDR_LED_OUTPUT_INACTIVE = "hyperhdr.led_output_inactive"
    CAPTURE_DISABLED = "capture.disabled"
    CAPTURE_COLLECTOR_FAILED = "capture.collector_failed"
    CAPTURE_HEALTHY = "capture.healthy"
    CAPTURE_UNSUPPORTED_PLATFORM = "capture.unsupported_platform"
    CAPTURE_DEVICE_NOT_FOUND = "capture.device_not_found"
    CAPTURE_PROBE_FAILED = "capture.probe_failed"
    CAPTURE_SYMLINK_RESOLUTION_FAILED = "capture.symlink_resolution_failed"
    CAPTURE_INVALID_DEVICE_TARGET = "capture.invalid_device_target"
    CAPTURE_NOT_CHARACTER_DEVICE = "capture.not_character_device"
    CAPTURE_V4L2_REGISTRATION_MISSING = "capture.v4l2_registration_missing"
    CAPTURE_METADATA_UNAVAILABLE = "capture.metadata_unavailable"
    CAPTURE_INVALID_DEVICE_NAME = "capture.invalid_device_name"
    CAPTURE_PERMISSION_DENIED = "capture.permission_denied"
    CAPTURE_GRABBER_INACTIVE = "capture.grabber_inactive"
    CAPTURE_ACTIVITY_UNREPORTED = "capture.activity_unreported"
    RASPBERRY_PI_COLLECTOR_FAILED = "raspberry_pi.collector_failed"
    RASPBERRY_PI_HEALTHY = "raspberry_pi.healthy"
    RASPBERRY_PI_DEGRADED = "raspberry_pi.degraded"
    RASPBERRY_PI_UNAVAILABLE = "raspberry_pi.unavailable"


class ReasonDecision(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class RejectionCode(StrEnum):
    UNKNOWN_SCHEMA = "unknown_schema"
    UNKNOWN_COMPONENT = "unknown_component"
    UNKNOWN_STATUS = "unknown_status"
    UNKNOWN_DETAILS = "unknown_details"
    UNKNOWN_VALUE = "unknown_value"
    INCONSISTENT_SNAPSHOT = "inconsistent_snapshot"


@dataclass(frozen=True, slots=True)
class ReasonResult:
    decision: ReasonDecision
    reasons: tuple[NormalizedReason, ...] = ()
    rejection: RejectionCode | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.decision, ReasonDecision):
            raise ValueError("invalid_reason_decision")
        if self.decision is ReasonDecision.ACCEPTED:
            if not 1 <= len(self.reasons) <= 3 or self.rejection is not None:
                raise ValueError("invalid_accepted_reason_result")
        elif self.reasons or not isinstance(self.rejection, RejectionCode):
            raise ValueError("invalid_rejected_reason_result")
        if any(not isinstance(reason, NormalizedReason) for reason in self.reasons):
            raise ValueError("invalid_normalized_reason")


_STATUSES = frozenset({"healthy", "degraded", "unavailable"})
_NETWORK_UNAVAILABLE = frozenset({"connection_failed", "timeout"})
_WLED_FAILURES = frozenset(
    {
        "connection_failed",
        "timeout",
        "redirect_rejected",
        "http_error",
        "response_too_large",
        "invalid_json",
        "invalid_response",
    }
)
_WLED_INFO_REASONS = _WLED_FAILURES | {"validated", "led_count_mismatch"}
_WLED_STATE_REASONS = _WLED_FAILURES | {"validated"}
_HYPERHDR_FAILURES = frozenset(
    {
        "connection_failed",
        "timeout",
        "redirect_rejected",
        "authorization_required",
        "http_error",
        "response_too_large",
        "invalid_json",
        "invalid_response",
        "server_reported_failure",
    }
)
_CAPTURE_REASONS = frozenset(
    {
        "validated",
        "capture_device_disabled",
        "unsupported_platform",
        "device_not_found",
        "probe_failed",
        "symlink_resolution_failed",
        "invalid_device_target",
        "not_character_device",
        "v4l2_registration_missing",
        "metadata_unavailable",
        "invalid_device_name",
        "permission_denied",
    }
)
_CAPTURE_UNAVAILABLE = frozenset(
    {
        "capture_device_disabled",
        "device_not_found",
        "probe_failed",
        "symlink_resolution_failed",
        "unsupported_platform",
    }
)
_WLED_DETAIL_KEYS = frozenset(
    {
        "info_reason_code",
        "state_reason_code",
        "firmware_version",
        "uptime_seconds",
        "reported_led_count",
        "expected_led_count",
        "expected_active_led_count",
        "expected_skipped_leds",
        "led_count_matches",
        "estimated_current_milliamps",
        "current_limit_milliamps",
        "brightness",
        "output_on",
    }
)
_HYPERHDR_DETAIL_KEYS = frozenset(
    {
        "reason_code",
        "server_info_received",
        "hdr_mode_enabled",
        "instance_running",
        "grabber_active",
        "led_output_active",
    }
)
_CAPTURE_DETAIL_KEYS = frozenset(
    {
        "reason_code",
        "device_node_present",
        "character_device",
        "v4l2_registered",
        "process_read_access",
        "device_name",
    }
)
_CAPTURE_ACTIVITY_KEYS = frozenset({"activity_source", "grabber_active"})
_PI_DETAIL_KEYS = frozenset(
    {
        "cpu_temperature_c",
        "cpu_temperature_warning_c",
        "load_average_1m",
        "load_average_5m",
        "load_average_15m",
        "logical_cpu_count",
        "memory_used_percent",
        "memory_warning_percent",
        "root_storage_used_percent",
        "storage_warning_percent",
        "host_uptime_seconds",
    }
)
_WLED_INFO_MAP = {
    value: NormalizedReason(f"wled.info.{value}")
    for value in _WLED_FAILURES | {"led_count_mismatch"}
}
_WLED_STATE_MAP = {
    value: NormalizedReason(f"wled.state.{value}") for value in _WLED_FAILURES
}
_HYPERHDR_MAP = {
    value: NormalizedReason(f"hyperhdr.{value}") for value in _HYPERHDR_FAILURES
}
_CAPTURE_MAP = {
    "unsupported_platform": NormalizedReason.CAPTURE_UNSUPPORTED_PLATFORM,
    "device_not_found": NormalizedReason.CAPTURE_DEVICE_NOT_FOUND,
    "probe_failed": NormalizedReason.CAPTURE_PROBE_FAILED,
    "symlink_resolution_failed": NormalizedReason.CAPTURE_SYMLINK_RESOLUTION_FAILED,
    "invalid_device_target": NormalizedReason.CAPTURE_INVALID_DEVICE_TARGET,
    "not_character_device": NormalizedReason.CAPTURE_NOT_CHARACTER_DEVICE,
    "v4l2_registration_missing": NormalizedReason.CAPTURE_V4L2_REGISTRATION_MISSING,
    "metadata_unavailable": NormalizedReason.CAPTURE_METADATA_UNAVAILABLE,
    "invalid_device_name": NormalizedReason.CAPTURE_INVALID_DEVICE_NAME,
    "permission_denied": NormalizedReason.CAPTURE_PERMISSION_DENIED,
}


def normalize_component_reason(
    *,
    schema_version: object,
    component: object,
    status: object,
    details: object,
    message: object = None,
) -> ReasonResult:
    """Return fixed reasons for one exact current sanitized collector shape."""
    del message
    if type(schema_version) is not int or schema_version != 1:
        return _rejected(RejectionCode.UNKNOWN_SCHEMA)
    if not isinstance(component, str) or component not in {
        "wled",
        "hyperhdr",
        "capture",
        "raspberry_pi",
    }:
        return _rejected(RejectionCode.UNKNOWN_COMPONENT)
    if not isinstance(status, str) or status not in _STATUSES:
        return _rejected(RejectionCode.UNKNOWN_STATUS)
    if not isinstance(details, Mapping) or any(
        not isinstance(key, str) for key in details
    ):
        return _rejected(RejectionCode.UNKNOWN_DETAILS)
    typed_details = dict(details)
    if component == "wled":
        return _normalize_wled(str(status), typed_details)
    if component == "hyperhdr":
        return _normalize_hyperhdr(str(status), typed_details)
    if component == "capture":
        return _normalize_capture(str(status), typed_details)
    return _normalize_pi(str(status), typed_details)


def _normalize_wled(status: str, details: dict[str, object]) -> ReasonResult:
    fixed = _fixed_wled_reason(status, details)
    if fixed is not None:
        return fixed
    if set(details) != _WLED_DETAIL_KEYS:
        return _rejected(RejectionCode.UNKNOWN_DETAILS)
    info = details["info_reason_code"]
    state = details["state_reason_code"]
    if (
        not isinstance(info, str)
        or not isinstance(state, str)
        or info not in _WLED_INFO_REASONS
        or state not in _WLED_STATE_REASONS
    ):
        return _rejected(RejectionCode.UNKNOWN_VALUE)
    if status != _wled_status(str(info), str(state)):
        return _rejected(RejectionCode.INCONSISTENT_SNAPSHOT)
    if info == state == "validated":
        return _accepted(NormalizedReason.WLED_HEALTHY)
    return ReasonResult(
        ReasonDecision.ACCEPTED,
        tuple(
            reason
            for reason in (
                _WLED_INFO_MAP.get(str(info)),
                _WLED_STATE_MAP.get(str(state)),
            )
            if reason is not None
        ),
    )


def _wled_status(info: str, state: str) -> str:
    info_status = (
        "healthy"
        if info == "validated"
        else "degraded"
        if info == "led_count_mismatch" or info not in _NETWORK_UNAVAILABLE
        else "unavailable"
    )
    state_status = (
        "healthy"
        if state == "validated"
        else "unavailable"
        if state in _NETWORK_UNAVAILABLE
        else "degraded"
    )
    if info_status == state_status == "healthy":
        return "healthy"
    if info_status == state_status == "unavailable":
        return "unavailable"
    return "degraded"


def _normalize_hyperhdr(status: str, details: dict[str, object]) -> ReasonResult:
    if details == {"reason_code": "collector_failed"}:
        return (
            _accepted(NormalizedReason.HYPERHDR_COLLECTOR_FAILED)
            if status == "unavailable"
            else _rejected(RejectionCode.INCONSISTENT_SNAPSHOT)
        )
    if set(details) != _HYPERHDR_DETAIL_KEYS:
        return _rejected(RejectionCode.UNKNOWN_DETAILS)
    reason = details["reason_code"]
    if not isinstance(reason, str):
        return _rejected(RejectionCode.UNKNOWN_VALUE)
    if type(details["server_info_received"]) is not bool or any(
        details[key] is not None and type(details[key]) is not bool
        for key in (
            "hdr_mode_enabled",
            "instance_running",
            "grabber_active",
            "led_output_active",
        )
    ):
        return _rejected(RejectionCode.UNKNOWN_VALUE)
    if not (
        reason == "hyperhdr_disabled"
        or reason == "validated"
        or reason in _HYPERHDR_FAILURES
    ):
        return _rejected(RejectionCode.UNKNOWN_VALUE)
    empty = details["server_info_received"] is False and all(
        details[key] is None
        for key in (
            "hdr_mode_enabled",
            "instance_running",
            "grabber_active",
            "led_output_active",
        )
    )
    if reason == "hyperhdr_disabled":
        if status != "unavailable" or not empty:
            return _rejected(RejectionCode.INCONSISTENT_SNAPSHOT)
        return _accepted(NormalizedReason.HYPERHDR_DISABLED)
    if reason in _HYPERHDR_FAILURES:
        expected = "unavailable" if reason in _NETWORK_UNAVAILABLE else "degraded"
        if status != expected or not empty:
            return _rejected(RejectionCode.INCONSISTENT_SNAPSHOT)
        return _accepted(_HYPERHDR_MAP[reason])
    states = tuple(
        details[key]
        for key in ("instance_running", "grabber_active", "led_output_active")
    )
    if details["server_info_received"] is not True or any(
        type(value) is not bool for value in states
    ):
        return _rejected(RejectionCode.INCONSISTENT_SNAPSHOT)
    reasons = tuple(
        reason_code
        for value, reason_code in zip(
            states,
            (
                NormalizedReason.HYPERHDR_INSTANCE_INACTIVE,
                NormalizedReason.HYPERHDR_VIDEO_GRABBER_INACTIVE,
                NormalizedReason.HYPERHDR_LED_OUTPUT_INACTIVE,
            ),
            strict=True,
        )
        if value is False
    )
    if status != ("degraded" if reasons else "healthy"):
        return _rejected(RejectionCode.INCONSISTENT_SNAPSHOT)
    return ReasonResult(
        ReasonDecision.ACCEPTED,
        reasons or (NormalizedReason.HYPERHDR_HEALTHY,),
    )


def _normalize_capture(status: str, details: dict[str, object]) -> ReasonResult:
    if details == {"reason_code": "collector_failed"}:
        return (
            _accepted(NormalizedReason.CAPTURE_COLLECTOR_FAILED)
            if status == "unavailable"
            else _rejected(RejectionCode.INCONSISTENT_SNAPSHOT)
        )
    keys = set(details)
    if keys == _CAPTURE_DETAIL_KEYS:
        activity: object = True
    elif keys == _CAPTURE_DETAIL_KEYS | _CAPTURE_ACTIVITY_KEYS:
        if details["activity_source"] != "HyperHDR serverinfo":
            return _rejected(RejectionCode.UNKNOWN_VALUE)
        activity = details["grabber_active"]
        if activity is not None and type(activity) is not bool:
            return _rejected(RejectionCode.UNKNOWN_VALUE)
    else:
        return _rejected(RejectionCode.UNKNOWN_DETAILS)
    reason = details["reason_code"]
    if not isinstance(reason, str) or reason not in _CAPTURE_REASONS:
        return _rejected(RejectionCode.UNKNOWN_VALUE)
    base_status = (
        "healthy"
        if reason == "validated"
        else "unavailable"
        if reason in _CAPTURE_UNAVAILABLE
        else "degraded"
    )
    expected = (
        "degraded"
        if base_status != "unavailable" and activity is not True
        else base_status
    )
    if status != expected:
        return _rejected(RejectionCode.INCONSISTENT_SNAPSHOT)
    reasons: tuple[NormalizedReason, ...]
    if reason == "capture_device_disabled":
        reasons = (NormalizedReason.CAPTURE_DISABLED,)
    elif reason == "validated":
        reasons = (NormalizedReason.CAPTURE_HEALTHY,)
    else:
        reasons = (_CAPTURE_MAP[str(reason)],)
    if base_status != "unavailable" and activity is False:
        reasons += (NormalizedReason.CAPTURE_GRABBER_INACTIVE,)
    elif base_status != "unavailable" and activity is None:
        reasons += (NormalizedReason.CAPTURE_ACTIVITY_UNREPORTED,)
    return ReasonResult(ReasonDecision.ACCEPTED, reasons)


def _normalize_pi(status: str, details: dict[str, object]) -> ReasonResult:
    if details == {"reason_code": "collector_failed"}:
        return (
            _accepted(NormalizedReason.RASPBERRY_PI_COLLECTOR_FAILED)
            if status == "unavailable"
            else _rejected(RejectionCode.INCONSISTENT_SNAPSHOT)
        )
    if set(details) != _PI_DETAIL_KEYS:
        return _rejected(RejectionCode.UNKNOWN_DETAILS)
    if any(
        value is not None
        and (isinstance(value, bool) or not isinstance(value, int | float))
        for value in details.values()
    ):
        return _rejected(RejectionCode.UNKNOWN_VALUE)
    return _accepted(
        {
            "healthy": NormalizedReason.RASPBERRY_PI_HEALTHY,
            "degraded": NormalizedReason.RASPBERRY_PI_DEGRADED,
            "unavailable": NormalizedReason.RASPBERRY_PI_UNAVAILABLE,
        }[status]
    )


def _fixed_wled_reason(status: str, details: dict[str, object]) -> ReasonResult | None:
    fixed = {
        "collector_failed": NormalizedReason.WLED_COLLECTOR_FAILED,
        "wled_disabled": NormalizedReason.WLED_DISABLED,
    }
    reason = details.get("reason_code")
    if (
        set(details) != {"reason_code"}
        or not isinstance(reason, str)
        or reason not in fixed
    ):
        return None
    if status != "unavailable":
        return _rejected(RejectionCode.INCONSISTENT_SNAPSHOT)
    return _accepted(fixed[reason])


def _accepted(reason: NormalizedReason) -> ReasonResult:
    return ReasonResult(ReasonDecision.ACCEPTED, (reason,))


def _rejected(code: RejectionCode) -> ReasonResult:
    return ReasonResult(ReasonDecision.REJECTED, rejection=code)
