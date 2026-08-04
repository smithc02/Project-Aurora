"""Synthetic tests for the isolated Milestone 18 reason registry."""

from __future__ import annotations

from collections import OrderedDict
from itertools import product

import pytest

from aurora_core.m18_validation.reasons import (
    NormalizedReason,
    ReasonDecision,
    RejectionCode,
    normalize_component_reason,
)


def _wled_details(
    *, info: str = "validated", state: str = "validated"
) -> dict[str, object]:
    return {
        "info_reason_code": info,
        "state_reason_code": state,
        "firmware_version": "synthetic",
        "uptime_seconds": 1,
        "reported_led_count": 10,
        "expected_led_count": 10,
        "expected_active_led_count": 8,
        "expected_skipped_leds": 2,
        "led_count_matches": True,
        "estimated_current_milliamps": 10,
        "current_limit_milliamps": 20,
        "brightness": 1,
        "output_on": True,
    }


def _hyperhdr_details(
    reason: str = "validated",
    *,
    instance_running: bool = True,
    grabber_active: bool = True,
    led_output_active: bool = True,
) -> dict[str, object]:
    validated = reason == "validated"
    return {
        "reason_code": reason,
        "server_info_received": validated,
        "hdr_mode_enabled": None,
        "instance_running": instance_running if validated else None,
        "grabber_active": grabber_active if validated else None,
        "led_output_active": led_output_active if validated else None,
    }


def _capture_details(reason: str = "validated") -> dict[str, object]:
    return {
        "reason_code": reason,
        "device_node_present": reason == "validated",
        "character_device": reason == "validated",
        "v4l2_registered": reason == "validated",
        "process_read_access": reason == "validated",
        "device_name": "synthetic ignored name" if reason == "validated" else None,
    }


def _pi_details() -> dict[str, object]:
    return {
        "cpu_temperature_c": 40.0,
        "cpu_temperature_warning_c": 80.0,
        "load_average_1m": 0.1,
        "load_average_5m": 0.2,
        "load_average_15m": 0.3,
        "logical_cpu_count": 4,
        "memory_used_percent": 20.0,
        "memory_warning_percent": 90.0,
        "root_storage_used_percent": 30.0,
        "storage_warning_percent": 90.0,
        "host_uptime_seconds": 100.0,
    }


def _normalize(
    component: str,
    status: str,
    details: dict[str, object],
    *,
    schema_version: object = 1,
    message: object = "ignored free-form text",
):
    return normalize_component_reason(
        schema_version=schema_version,
        component=component,
        status=status,
        details=details,
        message=message,
    )


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("led_count_mismatch", NormalizedReason.WLED_INFO_LED_COUNT_MISMATCH),
        ("connection_failed", NormalizedReason.WLED_INFO_CONNECTION_FAILED),
        ("timeout", NormalizedReason.WLED_INFO_TIMEOUT),
        ("redirect_rejected", NormalizedReason.WLED_INFO_REDIRECT_REJECTED),
        ("http_error", NormalizedReason.WLED_INFO_HTTP_ERROR),
        ("response_too_large", NormalizedReason.WLED_INFO_RESPONSE_TOO_LARGE),
        ("invalid_json", NormalizedReason.WLED_INFO_INVALID_JSON),
        ("invalid_response", NormalizedReason.WLED_INFO_INVALID_RESPONSE),
    ],
)
def test_all_wled_information_reasons(reason: str, expected: NormalizedReason) -> None:
    result = _normalize("wled", "degraded", _wled_details(info=reason))
    assert result.reasons == (expected,)


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("connection_failed", NormalizedReason.WLED_STATE_CONNECTION_FAILED),
        ("timeout", NormalizedReason.WLED_STATE_TIMEOUT),
        ("redirect_rejected", NormalizedReason.WLED_STATE_REDIRECT_REJECTED),
        ("http_error", NormalizedReason.WLED_STATE_HTTP_ERROR),
        ("response_too_large", NormalizedReason.WLED_STATE_RESPONSE_TOO_LARGE),
        ("invalid_json", NormalizedReason.WLED_STATE_INVALID_JSON),
        ("invalid_response", NormalizedReason.WLED_STATE_INVALID_RESPONSE),
    ],
)
def test_all_wled_state_reasons(reason: str, expected: NormalizedReason) -> None:
    result = _normalize("wled", "degraded", _wled_details(state=reason))
    assert result.reasons == (expected,)


def test_wled_healthy_disabled_failed_and_two_failures_are_bounded() -> None:
    assert _normalize("wled", "healthy", _wled_details()).reasons == (
        NormalizedReason.WLED_HEALTHY,
    )
    assert _normalize(
        "wled", "unavailable", {"reason_code": "wled_disabled"}
    ).reasons == (NormalizedReason.WLED_DISABLED,)
    assert _normalize(
        "wled", "unavailable", {"reason_code": "collector_failed"}
    ).reasons == (NormalizedReason.WLED_COLLECTOR_FAILED,)
    result = _normalize(
        "wled",
        "unavailable",
        _wled_details(info="timeout", state="connection_failed"),
    )
    assert result.reasons == (
        NormalizedReason.WLED_INFO_TIMEOUT,
        NormalizedReason.WLED_STATE_CONNECTION_FAILED,
    )


@pytest.mark.parametrize(
    ("reason", "status", "expected"),
    [
        (
            "connection_failed",
            "unavailable",
            NormalizedReason.HYPERHDR_CONNECTION_FAILED,
        ),
        ("timeout", "unavailable", NormalizedReason.HYPERHDR_TIMEOUT),
        ("redirect_rejected", "degraded", NormalizedReason.HYPERHDR_REDIRECT_REJECTED),
        (
            "authorization_required",
            "degraded",
            NormalizedReason.HYPERHDR_AUTHORIZATION_REQUIRED,
        ),
        ("http_error", "degraded", NormalizedReason.HYPERHDR_HTTP_ERROR),
        (
            "response_too_large",
            "degraded",
            NormalizedReason.HYPERHDR_RESPONSE_TOO_LARGE,
        ),
        ("invalid_json", "degraded", NormalizedReason.HYPERHDR_INVALID_JSON),
        ("invalid_response", "degraded", NormalizedReason.HYPERHDR_INVALID_RESPONSE),
        (
            "server_reported_failure",
            "degraded",
            NormalizedReason.HYPERHDR_SERVER_REPORTED_FAILURE,
        ),
    ],
)
def test_all_hyperhdr_failure_reasons(
    reason: str, status: str, expected: NormalizedReason
) -> None:
    assert _normalize("hyperhdr", status, _hyperhdr_details(reason)).reasons == (
        expected,
    )


def test_hyperhdr_fixed_states_and_inactive_components() -> None:
    assert _normalize("hyperhdr", "healthy", _hyperhdr_details()).reasons == (
        NormalizedReason.HYPERHDR_HEALTHY,
    )
    disabled = _hyperhdr_details("hyperhdr_disabled")
    assert _normalize("hyperhdr", "unavailable", disabled).reasons == (
        NormalizedReason.HYPERHDR_DISABLED,
    )
    failed = _normalize("hyperhdr", "unavailable", {"reason_code": "collector_failed"})
    assert failed.reasons == (NormalizedReason.HYPERHDR_COLLECTOR_FAILED,)
    inactive = _hyperhdr_details()
    inactive["instance_running"] = False
    inactive["grabber_active"] = False
    inactive["led_output_active"] = False
    assert _normalize("hyperhdr", "degraded", inactive).reasons == (
        NormalizedReason.HYPERHDR_INSTANCE_INACTIVE,
        NormalizedReason.HYPERHDR_VIDEO_GRABBER_INACTIVE,
        NormalizedReason.HYPERHDR_LED_OUTPUT_INACTIVE,
    )


@pytest.mark.parametrize(
    ("instance_running", "grabber_active", "led_output_active"),
    tuple(product((True, False), repeat=3)),
)
def test_every_complete_validated_hyperhdr_component_shape(
    instance_running: bool,
    grabber_active: bool,
    led_output_active: bool,
) -> None:
    details = _hyperhdr_details(
        instance_running=instance_running,
        grabber_active=grabber_active,
        led_output_active=led_output_active,
    )
    expected = tuple(
        reason
        for active, reason in (
            (instance_running, NormalizedReason.HYPERHDR_INSTANCE_INACTIVE),
            (grabber_active, NormalizedReason.HYPERHDR_VIDEO_GRABBER_INACTIVE),
            (led_output_active, NormalizedReason.HYPERHDR_LED_OUTPUT_INACTIVE),
        )
        if not active
    ) or (NormalizedReason.HYPERHDR_HEALTHY,)
    status = (
        "healthy"
        if all((instance_running, grabber_active, led_output_active))
        else "degraded"
    )
    assert _normalize("hyperhdr", status, details).reasons == expected


@pytest.mark.parametrize("hdr_mode_enabled", [None, True, False])
def test_validated_hyperhdr_accepts_current_hdr_observation_shapes(
    hdr_mode_enabled: bool | None,
) -> None:
    details = _hyperhdr_details()
    details["hdr_mode_enabled"] = hdr_mode_enabled
    assert _normalize("hyperhdr", "healthy", details).reasons == (
        NormalizedReason.HYPERHDR_HEALTHY,
    )


@pytest.mark.parametrize(
    ("status", "details"),
    [
        (
            "healthy",
            dict(_hyperhdr_details(), server_info_received=False),
        ),
        (
            "healthy",
            dict(_hyperhdr_details(), instance_running=None),
        ),
        (
            "healthy",
            dict(_hyperhdr_details(), grabber_active=None),
        ),
        (
            "healthy",
            dict(_hyperhdr_details(), led_output_active=None),
        ),
        (
            "unavailable",
            dict(_hyperhdr_details("connection_failed"), server_info_received=True),
        ),
        (
            "degraded",
            dict(
                _hyperhdr_details("http_error"),
                instance_running=True,
                grabber_active=True,
                led_output_active=True,
            ),
        ),
        (
            "unavailable",
            dict(
                _hyperhdr_details("hyperhdr_disabled"),
                grabber_active=False,
            ),
        ),
        (
            "healthy",
            _hyperhdr_details(grabber_active=False),
        ),
        (
            "unavailable",
            _hyperhdr_details(),
        ),
    ],
)
def test_contradictory_hyperhdr_shapes_are_inconsistent(
    status: str, details: dict[str, object]
) -> None:
    result = _normalize("hyperhdr", status, details)
    assert result.decision is ReasonDecision.REJECTED
    assert result.reasons == ()
    assert result.rejection is RejectionCode.INCONSISTENT_SNAPSHOT


def test_unknown_hyperhdr_value_rejects_without_copying_value() -> None:
    unknown = "free-form-value-must-not-appear"
    details = _hyperhdr_details()
    details["server_info_received"] = unknown
    result = _normalize("hyperhdr", "healthy", details)
    assert result.rejection is RejectionCode.UNKNOWN_VALUE
    assert unknown not in repr(result)


@pytest.mark.parametrize(
    ("reason", "status", "expected"),
    [
        ("capture_device_disabled", "unavailable", NormalizedReason.CAPTURE_DISABLED),
        (
            "unsupported_platform",
            "unavailable",
            NormalizedReason.CAPTURE_UNSUPPORTED_PLATFORM,
        ),
        ("device_not_found", "unavailable", NormalizedReason.CAPTURE_DEVICE_NOT_FOUND),
        ("probe_failed", "unavailable", NormalizedReason.CAPTURE_PROBE_FAILED),
        (
            "symlink_resolution_failed",
            "unavailable",
            NormalizedReason.CAPTURE_SYMLINK_RESOLUTION_FAILED,
        ),
        (
            "invalid_device_target",
            "degraded",
            NormalizedReason.CAPTURE_INVALID_DEVICE_TARGET,
        ),
        (
            "not_character_device",
            "degraded",
            NormalizedReason.CAPTURE_NOT_CHARACTER_DEVICE,
        ),
        (
            "v4l2_registration_missing",
            "degraded",
            NormalizedReason.CAPTURE_V4L2_REGISTRATION_MISSING,
        ),
        (
            "metadata_unavailable",
            "degraded",
            NormalizedReason.CAPTURE_METADATA_UNAVAILABLE,
        ),
        (
            "invalid_device_name",
            "degraded",
            NormalizedReason.CAPTURE_INVALID_DEVICE_NAME,
        ),
        ("permission_denied", "degraded", NormalizedReason.CAPTURE_PERMISSION_DENIED),
    ],
)
def test_all_capture_probe_reasons(
    reason: str, status: str, expected: NormalizedReason
) -> None:
    assert _normalize("capture", status, _capture_details(reason)).reasons == (
        expected,
    )


def test_capture_health_collector_failure_and_activity_reasons() -> None:
    assert _normalize("capture", "healthy", _capture_details()).reasons == (
        NormalizedReason.CAPTURE_HEALTHY,
    )
    assert _normalize(
        "capture", "unavailable", {"reason_code": "collector_failed"}
    ).reasons == (NormalizedReason.CAPTURE_COLLECTOR_FAILED,)
    inactive = _capture_details()
    inactive.update({"activity_source": "HyperHDR serverinfo", "grabber_active": False})
    assert _normalize("capture", "degraded", inactive).reasons == (
        NormalizedReason.CAPTURE_HEALTHY,
        NormalizedReason.CAPTURE_GRABBER_INACTIVE,
    )
    unknown = dict(inactive, grabber_active=None)
    assert _normalize("capture", "degraded", unknown).reasons == (
        NormalizedReason.CAPTURE_HEALTHY,
        NormalizedReason.CAPTURE_ACTIVITY_UNREPORTED,
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("healthy", NormalizedReason.RASPBERRY_PI_HEALTHY),
        ("degraded", NormalizedReason.RASPBERRY_PI_DEGRADED),
        ("unavailable", NormalizedReason.RASPBERRY_PI_UNAVAILABLE),
    ],
)
def test_raspberry_pi_status_only_mapping(
    status: str, expected: NormalizedReason
) -> None:
    assert _normalize("raspberry_pi", status, _pi_details()).reasons == (expected,)
    assert _normalize(
        "raspberry_pi", "unavailable", {"reason_code": "collector_failed"}
    ).reasons == (NormalizedReason.RASPBERRY_PI_COLLECTOR_FAILED,)


@pytest.mark.parametrize(
    ("overrides", "rejection"),
    [
        ({"schema_version": 2}, RejectionCode.UNKNOWN_SCHEMA),
        ({"schema_version": True}, RejectionCode.UNKNOWN_SCHEMA),
        ({"component": "other"}, RejectionCode.UNKNOWN_COMPONENT),
        ({"component": "WLED"}, RejectionCode.UNKNOWN_COMPONENT),
        ({"status": "disabled"}, RejectionCode.UNKNOWN_STATUS),
        ({"details": {"unexpected": "value"}}, RejectionCode.UNKNOWN_DETAILS),
    ],
)
def test_unknown_schema_component_status_and_details_reject(
    overrides: dict[str, object], rejection: RejectionCode
) -> None:
    arguments: dict[str, object] = {
        "schema_version": 1,
        "component": "wled",
        "status": "healthy",
        "details": _wled_details(),
    }
    arguments.update(overrides)
    result = normalize_component_reason(**arguments)
    assert result.decision is ReasonDecision.REJECTED
    assert result.reasons == ()
    assert result.rejection is rejection


def test_unknown_values_and_inconsistent_status_reject_without_echo() -> None:
    secret = "credential-value-must-not-appear"
    unknown = _wled_details(info=secret)
    result = _normalize("wled", "degraded", unknown)
    assert result.rejection is RejectionCode.UNKNOWN_VALUE
    assert secret not in repr(result)
    inconsistent = _normalize("wled", "healthy", _wled_details(state="timeout"))
    assert inconsistent.rejection is RejectionCode.INCONSISTENT_SNAPSHOT
    pi = _pi_details()
    pi["cpu_temperature_c"] = secret
    assert _normalize("raspberry_pi", "healthy", pi).rejection is (
        RejectionCode.UNKNOWN_VALUE
    )


@pytest.mark.parametrize(
    ("component", "status", "details"),
    [
        ("wled", "degraded", _wled_details(info="future_reason")),
        ("hyperhdr", "degraded", _hyperhdr_details("future_reason")),
        ("capture", "degraded", _capture_details("future_reason")),
        (
            "capture",
            "degraded",
            dict(
                _capture_details(),
                activity_source="unapproved source",
                grabber_active=False,
            ),
        ),
    ],
)
def test_unknown_contributing_value_rejected_for_each_registry_shape(
    component: str, status: str, details: dict[str, object]
) -> None:
    result = _normalize(component, status, details)
    assert result.reasons == ()
    assert result.rejection is RejectionCode.UNKNOWN_VALUE


@pytest.mark.parametrize(
    ("component", "status", "details"),
    [
        ("wled", "healthy", _wled_details()),
        ("hyperhdr", "healthy", _hyperhdr_details()),
        ("capture", "healthy", _capture_details()),
        ("raspberry_pi", "healthy", _pi_details()),
    ],
)
def test_unknown_detail_key_rejected_for_every_component(
    component: str, status: str, details: dict[str, object]
) -> None:
    details["new_detail"] = "must not persist"
    assert _normalize(component, status, details).rejection is (
        RejectionCode.UNKNOWN_DETAILS
    )


def test_message_and_ignored_prohibited_values_never_affect_output() -> None:
    first = _wled_details()
    first["firmware_version"] = "private-host secret brightness=255"
    first["brightness"] = 255
    expected = _normalize("wled", "healthy", first, message="one raw exception")
    second = _wled_details()
    second["firmware_version"] = "different"
    second["brightness"] = 1
    actual = _normalize("wled", "healthy", second, message="different message")
    assert actual == expected
    serialized = repr(actual)
    for prohibited in ("private-host", "brightness", "raw exception", "255"):
        assert prohibited not in serialized


def test_details_order_is_irrelevant_and_output_is_bounded() -> None:
    details = _wled_details(info="timeout", state="connection_failed")
    reversed_details = OrderedDict(reversed(tuple(details.items())))
    first = _normalize("wled", "unavailable", details)
    second = normalize_component_reason(
        schema_version=1,
        component="wled",
        status="unavailable",
        details=reversed_details,
    )
    assert first == second
    assert len(first.reasons) <= 3


def test_only_four_fixed_component_names_can_be_accepted() -> None:
    accepted = {
        "wled": ("healthy", _wled_details()),
        "hyperhdr": ("healthy", _hyperhdr_details()),
        "capture": ("healthy", _capture_details()),
        "raspberry_pi": ("healthy", _pi_details()),
    }
    for component, (status, details) in accepted.items():
        assert (
            _normalize(component, status, details).decision is ReasonDecision.ACCEPTED
        )
    for component in ("ddp", "mqtt", "service", "", "raspberry-pi"):
        assert _normalize(component, "healthy", {}).decision is ReasonDecision.REJECTED
