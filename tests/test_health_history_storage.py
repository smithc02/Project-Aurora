"""Hardware-free tests for the Milestone 18 production storage foundation."""

from __future__ import annotations

import os
import socket
import sqlite3
import stat
import subprocess
from collections import OrderedDict
from datetime import UTC, datetime
from itertools import product
from pathlib import Path
from typing import Any

import pytest

from aurora_core.dashboard.models import ComponentHealth, HealthReport, HealthStatus
from aurora_core.health_history import schema
from aurora_core.health_history.filesystem import (
    FilesystemBoundaryError,
    FilesystemRejection,
    remove_created_artifacts,
    validate_database_file,
)
from aurora_core.health_history.models import (
    APPLICATION_ID,
    COMPONENT_ORDER,
    MAX_BOUNDED_COUNTER,
    MAX_COMPONENT_LATENCY_MS,
    MAX_SCHEMA_VERSION,
    MAX_SERVICE_UPTIME_MS,
    PAGE_SIZE_BYTES,
    PROJECTION_DIGEST_BYTES,
    SCHEMA_VERSION,
    AlertKind,
    AlertLifecycle,
    AlertScope,
    ComponentName,
    DatabaseIdentity,
    HealthHistoryStatus,
    LifecycleEvent,
    SampleKind,
    SamplingGapPhase,
)
from aurora_core.health_history.projection import (
    ComponentProjection,
    HealthProjection,
    ProjectionError,
    ProjectionRejection,
    project_health_report,
)
from aurora_core.health_history.reasons import (
    NormalizedReason,
    ReasonDecision,
    RejectionCode,
    normalize_component_reason,
)
from aurora_core.health_history.store import HealthHistoryStore, StoreError
from aurora_core.m18_validation.reasons import (
    NormalizedReason as ReferenceNormalizedReason,
)
from aurora_core.m18_validation.reasons import (
    normalize_component_reason as normalize_reference_reason,
)

_OBSERVED = "2026-08-05T12:00:00+00:00"
_RECORDED = datetime(2026, 8, 5, 12, 0, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _block_external_operations(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("external operation is prohibited")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(subprocess, "Popen", blocked)


@pytest.fixture
def protected_directory(tmp_path: Path) -> Path:
    tmp_path.chmod(0o700)
    return tmp_path


def _wled_details(
    *, info: str = "validated", state: str = "validated"
) -> dict[str, object]:
    return {
        "info_reason_code": info,
        "state_reason_code": state,
        "firmware_version": "excluded",
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
    instance: bool = True,
    grabber: bool = True,
    output: bool = True,
) -> dict[str, object]:
    validated = reason == "validated"
    return {
        "reason_code": reason,
        "server_info_received": validated,
        "hdr_mode_enabled": None,
        "instance_running": instance if validated else None,
        "grabber_active": grabber if validated else None,
        "led_output_active": output if validated else None,
    }


def _capture_details(reason: str = "validated") -> dict[str, object]:
    validated = reason == "validated"
    return {
        "reason_code": reason,
        "device_node_present": validated,
        "character_device": validated,
        "v4l2_registered": validated,
        "process_read_access": validated,
        "device_name": "excluded" if validated else None,
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


def _component(
    name: str,
    details: dict[str, object],
    *,
    status: HealthStatus = HealthStatus.HEALTHY,
    message: str = "ignored message",
    checked_at: str = _OBSERVED,
    latency_ms: float = 1.25,
    last_successful_at: str | None = _OBSERVED,
) -> ComponentHealth:
    return ComponentHealth(
        name=name,
        status=status,
        message=message,
        checked_at=checked_at,
        latency_ms=latency_ms,
        details=details,
        last_successful_at=last_successful_at,
    )


def _report(
    *,
    status: HealthStatus = HealthStatus.HEALTHY,
    components: tuple[ComponentHealth, ...] | None = None,
) -> HealthReport:
    return HealthReport(
        status=status,
        checked_at=_OBSERVED,
        service_uptime_seconds=12.5,
        components=components
        or (
            _component("wled", _wled_details()),
            _component("hyperhdr", _hyperhdr_details()),
            _component("capture", _capture_details()),
            _component("raspberry_pi", _pi_details()),
        ),
    )


def _normalize_pair(
    component: str, status: str, details: dict[str, object], message: object = "ignored"
) -> tuple[object, object]:
    arguments = {
        "schema_version": 1,
        "component": component,
        "status": status,
        "details": details,
        "message": message,
    }
    return (
        normalize_component_reason(**arguments),
        normalize_reference_reason(**arguments),
    )


@pytest.mark.parametrize(
    ("component", "status", "details"),
    [
        ("wled", "healthy", _wled_details()),
        ("wled", "unavailable", {"reason_code": "wled_disabled"}),
        ("wled", "unavailable", {"reason_code": "collector_failed"}),
        *[
            ("wled", "degraded", _wled_details(info=reason))
            for reason in (
                "led_count_mismatch",
                "redirect_rejected",
                "http_error",
                "response_too_large",
                "invalid_json",
                "invalid_response",
            )
        ],
        *[
            ("wled", "unavailable", _wled_details(info=reason, state=reason))
            for reason in ("connection_failed", "timeout")
        ],
        *[
            ("wled", "degraded", _wled_details(state=reason))
            for reason in (
                "redirect_rejected",
                "http_error",
                "response_too_large",
                "invalid_json",
                "invalid_response",
            )
        ],
        ("hyperhdr", "healthy", _hyperhdr_details()),
        (
            "hyperhdr",
            "unavailable",
            _hyperhdr_details("hyperhdr_disabled"),
        ),
        ("hyperhdr", "unavailable", {"reason_code": "collector_failed"}),
        *[
            (
                "hyperhdr",
                "unavailable"
                if reason in {"connection_failed", "timeout"}
                else "degraded",
                _hyperhdr_details(reason),
            )
            for reason in (
                "connection_failed",
                "timeout",
                "redirect_rejected",
                "authorization_required",
                "http_error",
                "response_too_large",
                "invalid_json",
                "invalid_response",
                "server_reported_failure",
            )
        ],
        *[
            (
                "hyperhdr",
                "healthy" if all(states) else "degraded",
                _hyperhdr_details(
                    instance=states[0], grabber=states[1], output=states[2]
                ),
            )
            for states in product((False, True), repeat=3)
        ],
        ("capture", "healthy", _capture_details()),
        ("capture", "unavailable", {"reason_code": "collector_failed"}),
        *[
            (
                "capture",
                "unavailable"
                if reason
                in {
                    "capture_device_disabled",
                    "unsupported_platform",
                    "device_not_found",
                    "probe_failed",
                    "symlink_resolution_failed",
                }
                else "degraded",
                _capture_details(reason),
            )
            for reason in (
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
            )
        ],
        *[
            (
                "raspberry_pi",
                status,
                _pi_details(),
            )
            for status in ("healthy", "degraded", "unavailable")
        ],
        ("raspberry_pi", "unavailable", {"reason_code": "collector_failed"}),
    ],
)
def test_production_reason_registry_matches_accepted_reference(
    component: str, status: str, details: dict[str, object]
) -> None:
    production, reference = _normalize_pair(component, status, details)
    assert production.decision.value == reference.decision.value
    assert tuple(reason.value for reason in production.reasons) == tuple(
        reason.value for reason in reference.reasons
    )
    assert production.rejection is None or reference.rejection is not None
    if production.rejection is not None:
        assert production.rejection.value == reference.rejection.value


def test_production_and_reference_reason_code_sets_are_exactly_equal() -> None:
    assert {reason.value for reason in NormalizedReason} == {
        reason.value for reason in ReferenceNormalizedReason
    }


@pytest.mark.parametrize(
    ("info", "state"),
    tuple(
        product(
            (
                "validated",
                "led_count_mismatch",
                "connection_failed",
                "timeout",
                "redirect_rejected",
                "http_error",
                "response_too_large",
                "invalid_json",
                "invalid_response",
            ),
            (
                "validated",
                "connection_failed",
                "timeout",
                "redirect_rejected",
                "http_error",
                "response_too_large",
                "invalid_json",
                "invalid_response",
            ),
        )
    ),
)
def test_every_accepted_wled_reason_combination_matches_reference(
    info: str, state: str
) -> None:
    info_status = (
        "healthy"
        if info == "validated"
        else "unavailable"
        if info in {"connection_failed", "timeout"}
        else "degraded"
    )
    state_status = (
        "healthy"
        if state == "validated"
        else "unavailable"
        if state in {"connection_failed", "timeout"}
        else "degraded"
    )
    status = (
        "healthy"
        if info_status == state_status == "healthy"
        else "unavailable"
        if info_status == state_status == "unavailable"
        else "degraded"
    )
    production, reference = _normalize_pair(
        "wled", status, _wled_details(info=info, state=state)
    )
    assert production.decision.value == reference.decision.value == "accepted"
    assert tuple(reason.value for reason in production.reasons) == tuple(
        reason.value for reason in reference.reasons
    )


@pytest.mark.parametrize("activity", [True, False, None])
@pytest.mark.parametrize(
    "reason",
    (
        "validated",
        "invalid_device_target",
        "not_character_device",
        "v4l2_registration_missing",
        "metadata_unavailable",
        "invalid_device_name",
        "permission_denied",
    ),
)
def test_every_current_capture_activity_shape_matches_reference(
    reason: str, activity: bool | None
) -> None:
    details = _capture_details(reason)
    details.update(
        {"activity_source": "HyperHDR serverinfo", "grabber_active": activity}
    )
    status = "healthy" if reason == "validated" and activity is True else "degraded"
    production, reference = _normalize_pair("capture", status, details)
    assert production.decision.value == reference.decision.value == "accepted"
    assert tuple(item.value for item in production.reasons) == tuple(
        item.value for item in reference.reasons
    )


@pytest.mark.parametrize("hdr_mode", [True, False, None])
def test_every_current_hyperhdr_hdr_shape_matches_reference(
    hdr_mode: bool | None,
) -> None:
    details = _hyperhdr_details()
    details["hdr_mode_enabled"] = hdr_mode
    production, reference = _normalize_pair("hyperhdr", "healthy", details)
    assert production.decision.value == reference.decision.value == "accepted"
    assert tuple(item.value for item in production.reasons) == tuple(
        item.value for item in reference.reasons
    )


@pytest.mark.parametrize(
    ("component", "status", "details"),
    [
        ("other", "healthy", {}),
        ("wled", "future", _wled_details()),
        ("wled", "healthy", dict(_wled_details(), unknown="excluded")),
        ("wled", "healthy", _wled_details(info="future")),
        (
            "hyperhdr",
            "healthy",
            dict(_hyperhdr_details(), server_info_received=False),
        ),
        (
            "hyperhdr",
            "unavailable",
            dict(_hyperhdr_details("connection_failed"), instance_running=True),
        ),
        ("capture", "healthy", dict(_capture_details(), unexpected=True)),
        ("capture", "degraded", _capture_details("future")),
        ("raspberry_pi", "healthy", dict(_pi_details(), logical_cpu_count=True)),
    ],
)
def test_rejected_registry_results_match_reference_without_free_form_values(
    component: str, status: str, details: dict[str, object]
) -> None:
    production, reference = _normalize_pair(
        component, status, details, "credential endpoint raw exception"
    )
    assert production.decision is ReasonDecision.REJECTED
    assert reference.decision.value == production.decision.value
    assert production.rejection is not None
    assert reference.rejection.value == production.rejection.value
    rendered = repr(production)
    for prohibited in ("credential", "endpoint", "raw exception"):
        assert prohibited not in rendered


@pytest.mark.parametrize(
    ("component", "status", "details", "rejection"),
    [
        ([], "healthy", {}, RejectionCode.UNKNOWN_COMPONENT),
        ("wled", [], _wled_details(), RejectionCode.UNKNOWN_STATUS),
        (
            "wled",
            "degraded",
            _wled_details(info=[]),  # type: ignore[arg-type]
            RejectionCode.UNKNOWN_VALUE,
        ),
        (
            "capture",
            "degraded",
            _capture_details([]),  # type: ignore[arg-type]
            RejectionCode.UNKNOWN_VALUE,
        ),
    ],
)
def test_unhashable_unknown_values_fail_closed(
    component: object,
    status: object,
    details: dict[str, object],
    rejection: RejectionCode,
) -> None:
    result = normalize_component_reason(
        schema_version=1,
        component=component,
        status=status,
        details=details,
    )
    assert result.rejection is rejection


def test_registry_is_message_and_mapping_order_independent() -> None:
    details = _wled_details(info="timeout", state="connection_failed")
    first, _ = _normalize_pair("wled", "unavailable", details, "first")
    second, _ = _normalize_pair(
        "wled",
        "unavailable",
        OrderedDict(reversed(tuple(details.items()))),
        "second private value",
    )
    assert first == second


def test_projection_is_complete_bounded_and_deterministic() -> None:
    first = project_health_report(
        _report(), observation_sequence=1, recorded_at=_RECORDED
    )
    reordered = _report(
        components=tuple(
            _component(
                component.name,
                dict(reversed(tuple(component.details.items()))),
                status=component.status,
                message=component.message,
                checked_at=component.checked_at,
                latency_ms=component.latency_ms,
                last_successful_at=component.last_successful_at,
            )
            for component in reversed(_report().components)
        )
    )
    second = project_health_report(
        reordered, observation_sequence=1, recorded_at=_RECORDED
    )
    assert first == second
    assert tuple(item.component for item in first.components) == COMPONENT_ORDER
    assert first.digest == second.digest
    assert len(first.digest) == PROJECTION_DIGEST_BYTES
    assert first.service_uptime_ms == 12_500
    assert all(component.latency_ms == 1 for component in first.components)
    replay = project_health_report(
        _report(),
        observation_sequence=1,
        recorded_at=datetime(2026, 8, 5, 12, 1, tzinfo=UTC),
    )
    assert replay.digest == first.digest


def test_projection_digest_includes_scheduler_evidence_but_not_recording_time() -> None:
    base = project_health_report(
        _report(), observation_sequence=1, recorded_at=_RECORDED
    )
    retried = project_health_report(
        _report(),
        observation_sequence=1,
        recorded_at=datetime(2026, 8, 5, 12, 1, tzinfo=UTC),
    )
    transition = project_health_report(
        _report(),
        observation_sequence=1,
        recorded_at=_RECORDED,
        sample_kind=SampleKind.TRANSITION,
    )
    missed = project_health_report(
        _report(),
        observation_sequence=1,
        recorded_at=_RECORDED,
        missed_intervals=1,
    )
    assert retried.recorded_at_utc_us != base.recorded_at_utc_us
    assert retried.digest == base.digest
    assert transition.digest != base.digest
    assert missed.digest != base.digest


def test_replay_key_distinguishes_same_observation_with_distinct_scheduler_evidence(
    protected_directory: Path,
) -> None:
    base = project_health_report(
        _report(), observation_sequence=1, recorded_at=_RECORDED
    )
    missed = project_health_report(
        _report(), observation_sequence=2, recorded_at=_RECORDED, missed_intervals=1
    )
    path, store = _create_store(protected_directory)
    store.close()
    connection = _rw(path)
    _insert_health_sample(
        connection,
        sequence=1,
        observed=base.observed_at_utc_us,
        digest=base.digest,
    )
    _insert_health_sample(
        connection,
        sequence=2,
        observed=missed.observed_at_utc_us,
        digest=missed.digest,
    )
    assert connection.execute("SELECT COUNT(*) FROM health_samples").fetchone() == (2,)
    connection.close()


def test_projection_ignores_messages_and_excluded_detail_values() -> None:
    original = _report()
    changed_components = tuple(
        _component(
            component.name,
            {
                **component.details,
                **(
                    {"firmware_version": "different", "brightness": 255}
                    if component.name == "wled"
                    else {}
                ),
            },
            message="host credential raw exception path",
        )
        for component in original.components
    )
    changed = _report(components=changed_components)
    assert project_health_report(
        original, observation_sequence=1, recorded_at=_RECORDED
    ).digest == (
        project_health_report(
            changed, observation_sequence=1, recorded_at=_RECORDED
        ).digest
    )


@pytest.mark.parametrize(
    ("report", "recorded_at", "reason"),
    [
        (
            HealthReport(
                HealthStatus.HEALTHY,
                _OBSERVED,
                1,
                _report().components,
                schema_version=2,
            ),
            _RECORDED,
            ProjectionRejection.UNKNOWN_SCHEMA,
        ),
        (
            HealthReport(
                HealthStatus.HEALTHY,
                "not-a-time",
                1,
                _report().components,
            ),
            _RECORDED,
            ProjectionRejection.INVALID_TIMESTAMP,
        ),
        (
            HealthReport(
                HealthStatus.HEALTHY,
                _OBSERVED,
                float("inf"),
                _report().components,
            ),
            _RECORDED,
            ProjectionRejection.INVALID_DURATION,
        ),
        (
            HealthReport(
                HealthStatus.HEALTHY,
                _OBSERVED,
                -1,
                _report().components,
            ),
            _RECORDED,
            ProjectionRejection.INVALID_DURATION,
        ),
        (
            _report(components=_report().components[:-1]),
            _RECORDED,
            ProjectionRejection.INVALID_COMPONENTS,
        ),
    ],
)
def test_projection_rejects_invalid_input_without_partial_result(
    report: HealthReport, recorded_at: datetime, reason: ProjectionRejection
) -> None:
    with pytest.raises(ProjectionError) as caught:
        project_health_report(report, observation_sequence=1, recorded_at=recorded_at)
    assert caught.value.reason is reason


@pytest.mark.parametrize("invalid", [True, -1, MAX_BOUNDED_COUNTER + 1])
def test_projection_rejects_invalid_missed_interval_counts(invalid: object) -> None:
    with pytest.raises(ProjectionError) as caught:
        project_health_report(
            _report(),
            observation_sequence=1,
            recorded_at=_RECORDED,
            missed_intervals=invalid,  # type: ignore[arg-type]
        )
    assert caught.value.reason is ProjectionRejection.INVALID_DURATION


def _component_at_status(name: str, status: HealthStatus) -> ComponentHealth:
    if name == "wled":
        details = (
            _wled_details()
            if status is HealthStatus.HEALTHY
            else _wled_details(info="http_error")
            if status is HealthStatus.DEGRADED
            else _wled_details(info="timeout", state="timeout")
        )
    elif name == "hyperhdr":
        details = (
            _hyperhdr_details()
            if status is HealthStatus.HEALTHY
            else _hyperhdr_details(grabber=False)
            if status is HealthStatus.DEGRADED
            else _hyperhdr_details("timeout")
        )
    elif name == "capture":
        details = _capture_details()
        if status is HealthStatus.DEGRADED:
            details.update(
                {"activity_source": "HyperHDR serverinfo", "grabber_active": False}
            )
        elif status is HealthStatus.UNAVAILABLE:
            details = _capture_details("device_not_found")
    else:
        details = _pi_details()
    return _component(name, details, status=status)


def test_projection_requires_overall_status_to_equal_worst_component_exhaustively() -> (
    None
):
    order = {
        HealthStatus.HEALTHY: 0,
        HealthStatus.DEGRADED: 1,
        HealthStatus.UNAVAILABLE: 2,
    }
    statuses = tuple(HealthStatus)
    for combination in product(statuses, repeat=len(COMPONENT_ORDER)):
        components = tuple(
            _component_at_status(name.value, status)
            for name, status in zip(COMPONENT_ORDER, combination, strict=True)
        )
        expected = max(combination, key=order.__getitem__)
        projection = project_health_report(
            _report(status=expected, components=components),
            observation_sequence=1,
            recorded_at=_RECORDED,
        )
        assert projection.overall_status.value == expected.value
        for inconsistent in set(statuses) - {expected}:
            with pytest.raises(ProjectionError) as caught:
                project_health_report(
                    _report(status=inconsistent, components=components),
                    observation_sequence=1,
                    recorded_at=_RECORDED,
                )
            assert caught.value.reason is ProjectionRejection.INCONSISTENT_STATUS


def test_projection_sanitizes_extreme_integer_duration_overflow() -> None:
    huge = 10**10_000
    uptime_report = HealthReport(
        status=HealthStatus.HEALTHY,
        checked_at=_OBSERVED,
        service_uptime_seconds=huge,  # type: ignore[arg-type]
        components=_report().components,
    )
    latency_components = (
        _component(
            "wled",
            _wled_details(),
            latency_ms=huge,  # type: ignore[arg-type]
        ),
        *_report().components[1:],
    )
    for report in (uptime_report, _report(components=latency_components)):
        with pytest.raises(ProjectionError) as caught:
            project_health_report(report, observation_sequence=1, recorded_at=_RECORDED)
        assert caught.value.reason is ProjectionRejection.INVALID_DURATION
        assert repr(caught.value) == "ProjectionError('invalid_duration')"


def test_strict_production_models_reject_open_ended_values() -> None:
    component = ComponentProjection(
        ComponentName.WLED,
        HealthHistoryStatus.HEALTHY,
        (NormalizedReason.WLED_HEALTHY,),
        1,
        1,
        None,
    )
    with pytest.raises(ValueError):
        ComponentProjection(  # type: ignore[arg-type]
            "wled",
            HealthHistoryStatus.HEALTHY,
            (NormalizedReason.WLED_HEALTHY,),
            1,
            1,
            None,
        )
    with pytest.raises(ValueError):
        HealthProjection(
            1,
            1,
            1,
            1,
            HealthHistoryStatus.HEALTHY,
            1,
            SampleKind.HEARTBEAT,
            0,
            (component, component, component, component),
            bytes(PROJECTION_DIGEST_BYTES),
        )


@pytest.mark.parametrize(
    ("checked_at", "latency", "last_successful"),
    [
        (True, 1, None),
        (-1, 1, None),
        (1, True, None),
        (1, -1, None),
        (1, MAX_COMPONENT_LATENCY_MS + 1, None),
        (1, 1, True),
        (1, 1, -1),
    ],
)
def test_component_projection_rejects_boolean_negative_and_oversized_integers(
    checked_at: object, latency: object, last_successful: object
) -> None:
    with pytest.raises(ValueError):
        ComponentProjection(
            component=ComponentName.WLED,
            status=HealthHistoryStatus.HEALTHY,
            reasons=(NormalizedReason.WLED_HEALTHY,),
            checked_at_utc_us=checked_at,  # type: ignore[arg-type]
            latency_ms=latency,  # type: ignore[arg-type]
            last_successful_at_utc_us=last_successful,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("component", "reasons"),
    [
        (ComponentName.WLED, (NormalizedReason.HYPERHDR_HEALTHY,)),
        (
            ComponentName.WLED,
            (
                NormalizedReason.WLED_HEALTHY,
                NormalizedReason.WLED_INFO_HTTP_ERROR,
                NormalizedReason.WLED_STATE_HTTP_ERROR,
            ),
        ),
        (
            ComponentName.HYPERHDR,
            (
                NormalizedReason.HYPERHDR_INSTANCE_INACTIVE,
                NormalizedReason.HYPERHDR_VIDEO_GRABBER_INACTIVE,
                NormalizedReason.HYPERHDR_LED_OUTPUT_INACTIVE,
                NormalizedReason.HYPERHDR_TIMEOUT,
            ),
        ),
        (
            ComponentName.CAPTURE,
            (
                NormalizedReason.CAPTURE_PROBE_FAILED,
                NormalizedReason.CAPTURE_GRABBER_INACTIVE,
                NormalizedReason.CAPTURE_ACTIVITY_UNREPORTED,
            ),
        ),
        (
            ComponentName.RASPBERRY_PI,
            (
                NormalizedReason.RASPBERRY_PI_HEALTHY,
                NormalizedReason.RASPBERRY_PI_DEGRADED,
            ),
        ),
        (
            ComponentName.WLED,
            (NormalizedReason.WLED_HEALTHY, NormalizedReason.WLED_HEALTHY),
        ),
    ],
)
def test_component_projection_enforces_component_reason_invariants(
    component: ComponentName, reasons: tuple[NormalizedReason, ...]
) -> None:
    with pytest.raises(ValueError):
        ComponentProjection(
            component=component,
            status=HealthHistoryStatus.HEALTHY,
            reasons=reasons,
            checked_at_utc_us=1,
            latency_ms=1,
            last_successful_at_utc_us=None,
        )


def _database_path(directory: Path) -> Path:
    return directory / "history.sqlite3"


def _create_store(directory: Path) -> tuple[Path, HealthHistoryStore]:
    path = _database_path(directory)
    return path, HealthHistoryStore.create(path, created_at_utc_us=1)


def _rw(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.absolute().as_uri()}?mode=rw", uri=True)


def test_create_sets_exact_identity_schema_pragmas_and_permissions(
    protected_directory: Path,
) -> None:
    path, store = _create_store(protected_directory)
    try:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        with _rw(path) as connection:
            assert connection.execute("PRAGMA application_id").fetchone() == (
                APPLICATION_ID,
            )
            assert connection.execute("PRAGMA user_version").fetchone() == (
                SCHEMA_VERSION,
            )
            assert connection.execute("PRAGMA page_size").fetchone() == (
                PAGE_SIZE_BYTES,
            )
            assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
            assert set(
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            ) == set(schema.EXPECTED_TABLES)
            assert set(
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='index' AND name NOT LIKE 'sqlite_%'"
                )
            ) == set(schema.EXPECTED_INDEXES)
            assert set(
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger'"
                )
            ) == set(schema.EXPECTED_TRIGGERS)
            assert connection.execute(
                "SELECT version FROM schema_migrations"
            ).fetchall() == [(1,)]
            assert connection.execute(
                "SELECT COUNT(*) FROM evaluation_state"
            ).fetchone() == (len(AlertScope),)
        for suffix in ("-wal", "-shm"):
            sidecar = path.with_name(f"{path.name}{suffix}")
            if sidecar.exists():
                assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
    finally:
        store.close()


def test_migration_ledger_constraint_allows_only_bounded_positive_versions(
    protected_directory: Path,
) -> None:
    path, store = _create_store(protected_directory)
    store.close()
    connection = _rw(path)
    for invalid in (0, -1):
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at_utc_us) "
                "VALUES (?, 1)",
                (invalid,),
            )
        connection.rollback()
    connection.execute("BEGIN")
    connection.execute(
        "INSERT INTO schema_migrations(version, applied_at_utc_us) VALUES (?, 2)",
        (min(2, MAX_SCHEMA_VERSION),),
    )
    assert connection.execute(
        "SELECT version FROM schema_migrations ORDER BY version"
    ).fetchall() == [(1,), (2,)]
    connection.rollback()
    connection.close()


def test_schema_v1_verification_rejects_a_second_positive_migration_row(
    protected_directory: Path,
) -> None:
    path, store = _create_store(protected_directory)
    store.close()
    connection = _rw(path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        "INSERT INTO schema_migrations(version, applied_at_utc_us) VALUES (2, 2)"
    )
    with pytest.raises(schema.SchemaVerificationError) as caught:
        schema.verify_schema_v1(connection)
    assert caught.value.reason == "migration_ledger_mismatch"
    connection.rollback()
    connection.close()
    assert not hasattr(HealthHistoryStore, "migrate")


def test_open_existing_uses_no_create_and_missing_file_remains_missing(
    protected_directory: Path,
) -> None:
    missing = _database_path(protected_directory)
    with pytest.raises(StoreError):
        HealthHistoryStore.open_existing(missing)
    assert not missing.exists()


def test_exclusive_creation_never_reuses_an_existing_path(
    protected_directory: Path,
) -> None:
    path = _database_path(protected_directory)
    path.touch(mode=0o600)
    before = path.stat()
    with pytest.raises(StoreError) as caught:
        HealthHistoryStore.create(path, created_at_utc_us=1)
    assert caught.value.reason == "already_exists"
    after = path.stat()
    assert (after.st_dev, after.st_ino, after.st_size) == (
        before.st_dev,
        before.st_ino,
        before.st_size,
    )


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
@pytest.mark.parametrize("object_kind", ["file", "directory", "symlink"])
def test_creation_refuses_every_preexisting_reserved_sidecar_without_modification(
    protected_directory: Path, suffix: str, object_kind: str
) -> None:
    path = _database_path(protected_directory)
    reserved = path.with_name(f"{path.name}{suffix}")
    target = protected_directory / f"target{suffix}"
    if object_kind == "file":
        reserved.write_bytes(b"existing evidence")
        reserved.chmod(0o600)
    elif object_kind == "directory":
        reserved.mkdir(mode=0o700)
    else:
        target.write_bytes(b"target evidence")
        target.chmod(0o600)
        reserved.symlink_to(target)
    before = reserved.lstat()
    with pytest.raises(StoreError) as caught:
        HealthHistoryStore.create(path, created_at_utc_us=1)
    assert caught.value.reason == "already_exists"
    after = reserved.lstat()
    assert (after.st_dev, after.st_ino, stat.S_IFMT(after.st_mode)) == (
        before.st_dev,
        before.st_ino,
        stat.S_IFMT(before.st_mode),
    )
    assert not path.exists()
    if object_kind == "file":
        assert reserved.read_bytes() == b"existing evidence"
    elif object_kind == "symlink":
        assert reserved.readlink() == target
        assert target.read_bytes() == b"target evidence"


@pytest.mark.parametrize("mode", [0o755, 0o750, 0o777])
def test_creation_rejects_insecure_parent_modes(tmp_path: Path, mode: int) -> None:
    tmp_path.chmod(mode)
    with pytest.raises(StoreError):
        HealthHistoryStore.create(_database_path(tmp_path), created_at_utc_us=1)


def test_creation_rejects_missing_and_symlinked_parents(tmp_path: Path) -> None:
    missing = tmp_path / "missing" / "history.sqlite3"
    with pytest.raises(StoreError):
        HealthHistoryStore.create(missing, created_at_utc_us=1)
    actual = tmp_path / "actual"
    actual.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(actual, target_is_directory=True)
    with pytest.raises(StoreError):
        HealthHistoryStore.create(linked / "history.sqlite3", created_at_utc_us=1)


def test_existing_main_file_rejects_symlink_hard_link_and_nonregular(
    protected_directory: Path,
) -> None:
    target = protected_directory / "target"
    target.touch(mode=0o600)
    symlink = _database_path(protected_directory)
    symlink.symlink_to(target)
    with pytest.raises(StoreError):
        HealthHistoryStore.open_existing(symlink)
    symlink.unlink()
    hard_link = _database_path(protected_directory)
    os.link(target, hard_link)
    with pytest.raises(StoreError):
        HealthHistoryStore.open_existing(hard_link)
    hard_link.unlink()
    directory = _database_path(protected_directory)
    directory.mkdir(mode=0o700)
    with pytest.raises(StoreError):
        HealthHistoryStore.open_existing(directory)


def test_symlinked_sqlite_sidecar_is_rejected(protected_directory: Path) -> None:
    path, store = _create_store(protected_directory)
    store.close()
    target = protected_directory / "target"
    target.touch(mode=0o600)
    sidecar = path.with_name(f"{path.name}-wal")
    sidecar.symlink_to(target)
    with pytest.raises(StoreError):
        HealthHistoryStore.open_existing(path)


def test_insecure_database_mode_is_rejected(protected_directory: Path) -> None:
    path, store = _create_store(protected_directory)
    store.close()
    path.chmod(0o640)
    with pytest.raises(StoreError):
        HealthHistoryStore.open_existing(path)


def test_foreign_owner_rejected_when_test_has_privilege(
    protected_directory: Path,
) -> None:
    if os.geteuid() != 0:
        pytest.skip("ordinary test user cannot safely manufacture foreign ownership")
    path, store = _create_store(protected_directory)
    store.close()
    os.chown(path, 1, -1)
    try:
        with pytest.raises(StoreError):
            HealthHistoryStore.open_existing(path)
    finally:
        os.chown(path, 0, -1)


def test_identity_change_during_open_fails_closed(
    protected_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, store = _create_store(protected_directory)
    store.close()
    import aurora_core.health_history.store as store_module

    original = store_module.validate_database_file
    calls = 0

    def changed(candidate: Path, *, expected: object | None = None) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED)
        return original(candidate, expected=expected)  # type: ignore[arg-type]

    monkeypatch.setattr(store_module, "validate_database_file", changed)
    with pytest.raises(StoreError):
        HealthHistoryStore.open_existing(path)
    assert path.exists()


@pytest.mark.parametrize(
    ("pragma", "value"),
    [
        ("application_id", 1),
        ("application_id", int(DatabaseIdentity.M18_SYNTHETIC_BENCHMARK)),
        ("user_version", 2),
        ("user_version", 0),
    ],
)
def test_identity_and_schema_version_mismatches_fail_closed(
    protected_directory: Path, pragma: str, value: int
) -> None:
    path, store = _create_store(protected_directory)
    store.close()
    connection = _rw(path)
    connection.execute(f"PRAGMA {pragma} = {value}")
    connection.close()
    with pytest.raises(StoreError):
        HealthHistoryStore.open_existing(path)
    assert path.exists()


@pytest.mark.parametrize(
    "mutation",
    [
        "DROP INDEX idx_health_samples_observed",
        "DROP TABLE alert_events",
        "CREATE TABLE unexpected_table (id INTEGER PRIMARY KEY)",
        "CREATE INDEX unexpected_index ON health_samples(recorded_at_utc_us)",
    ],
)
def test_missing_altered_and_unexpected_schema_objects_fail_closed(
    protected_directory: Path, mutation: str
) -> None:
    path, store = _create_store(protected_directory)
    store.close()
    connection = _rw(path)
    connection.execute(mutation)
    connection.commit()
    connection.close()
    with pytest.raises(StoreError):
        HealthHistoryStore.open_existing(path)


def test_altered_required_columns_fail_closed(protected_directory: Path) -> None:
    path, store = _create_store(protected_directory)
    store.close()
    connection = _rw(path)
    connection.execute(
        "ALTER TABLE schema_migrations RENAME TO replaced_schema_migrations"
    )
    connection.execute(
        "CREATE TABLE schema_migrations ("
        "version TEXT PRIMARY KEY, applied_at_utc_us INTEGER NOT NULL)"
    )
    connection.execute(
        "INSERT INTO schema_migrations(version, applied_at_utc_us) VALUES ('1', 1)"
    )
    connection.execute("DROP TABLE replaced_schema_migrations")
    connection.commit()
    connection.close()
    with pytest.raises(StoreError):
        HealthHistoryStore.open_existing(path)


def test_schema_object_count_cap_fails_before_unbounded_inspection(
    protected_directory: Path,
) -> None:
    path, store = _create_store(protected_directory)
    store.close()
    connection = _rw(path)
    connection.execute("PRAGMA foreign_keys = ON")
    for index in range(schema.MAX_SCHEMA_OBJECTS):
        connection.execute(f"CREATE TABLE extra_{index} (id INTEGER PRIMARY KEY)")
    with pytest.raises(schema.SchemaVerificationError) as caught:
        schema.verify_schema_v1(connection)
    assert caught.value.reason == "schema_object_limit"
    connection.close()


def _insert_health_sample(
    connection: sqlite3.Connection,
    *,
    sequence: int = 1,
    observed: int = 1,
    digest: bytes = bytes(PROJECTION_DIGEST_BYTES),
    status: str = "healthy",
    sample_kind: str = "heartbeat",
) -> int:
    cursor = connection.execute(
        "INSERT INTO health_samples("
        "observation_sequence, observed_at_utc_us, recorded_at_utc_us, "
        "overall_status, service_uptime_ms, sample_kind, accepted_sample_kind, "
        "projection_digest, missed_intervals"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (sequence, observed, 2, status, 3, sample_kind, sample_kind, digest, 0),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def test_replay_and_component_uniqueness_and_foreign_keys(
    protected_directory: Path,
) -> None:
    path, store = _create_store(protected_directory)
    store.close()
    connection = _rw(path)
    connection.execute("PRAGMA foreign_keys = ON")
    sample_id = _insert_health_sample(connection)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_health_sample(connection)
    connection.execute(
        "INSERT INTO component_samples("
        "sample_id, component, status, reason_code_1, checked_at_utc_us, latency_ms"
        ") VALUES (?, ?, ?, ?, ?, ?)",
        (sample_id, "wled", "healthy", "wled.healthy", 1, 1),
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO component_samples("
            "sample_id, component, status, reason_code_1, checked_at_utc_us, latency_ms"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (sample_id, "wled", "healthy", "wled.healthy", 1, 1),
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO component_samples("
            "sample_id, component, status, reason_code_1, checked_at_utc_us, latency_ms"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (999, "wled", "healthy", "wled.healthy", 1, 1),
        )
    connection.close()


@pytest.mark.parametrize(
    ("component", "reasons"),
    [
        (
            "wled",
            (
                "wled.healthy",
                "wled.info.http_error",
                "wled.state.http_error",
            ),
        ),
        (
            "capture",
            (
                "capture.probe_failed",
                "capture.grabber_inactive",
                "capture.activity_unreported",
            ),
        ),
        (
            "raspberry_pi",
            ("raspberry_pi.healthy", "raspberry_pi.degraded", None),
        ),
        ("wled", ("hyperhdr.healthy", None, None)),
    ],
)
def test_component_reason_count_and_prefix_checks_are_enforced_by_schema(
    protected_directory: Path,
    component: str,
    reasons: tuple[str | None, str | None, str | None],
) -> None:
    path, store = _create_store(protected_directory)
    store.close()
    connection = _rw(path)
    connection.execute("PRAGMA foreign_keys = ON")
    sample_id = _insert_health_sample(connection)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO component_samples("
            "sample_id, component, status, reason_code_1, reason_code_2, "
            "reason_code_3, checked_at_utc_us, latency_ms"
            ") VALUES (?, ?, 'degraded', ?, ?, ?, 1, 1)",
            (sample_id, component, *reasons),
        )
    connection.close()


def _insert_alert(
    connection: sqlite3.Connection,
    *,
    scope: str = "wled",
    kind: str = "degraded",
    lifecycle: str = "open",
) -> None:
    acknowledged = 1 if lifecycle == "acknowledged" else None
    recovered = 1 if lifecycle in {"recovered", "archived"} else None
    archived = 1 if lifecycle == "archived" else None
    connection.execute(
        "INSERT INTO alerts("
        "scope, kind, lifecycle, severity, opened_at_utc_us, "
        "acknowledged_at_utc_us, recovered_at_utc_us, archived_at_utc_us, "
        "episode_count, "
        "occurrence_count, cooldown_until_utc_us"
        ") VALUES (?, ?, ?, 'degraded', 1, ?, ?, ?, 1, 1, 1)",
        (scope, kind, lifecycle, acknowledged, recovered, archived),
    )


def test_only_one_active_alert_per_scope_and_kind(protected_directory: Path) -> None:
    path, store = _create_store(protected_directory)
    store.close()
    connection = _rw(path)
    _insert_alert(connection)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_alert(connection, lifecycle="acknowledged")
    _insert_alert(connection, lifecycle="recovered")
    connection.close()


def test_only_attached_valid_lifecycle_events_can_be_persisted(
    protected_directory: Path,
) -> None:
    path, store = _create_store(protected_directory)
    store.close()
    connection = _rw(path)
    connection.execute("PRAGMA foreign_keys = ON")
    _insert_alert(connection)
    alert_id = connection.execute("SELECT id FROM alerts").fetchone()[0]
    valid_events = (
        ("opened", "open"),
        ("occurrence_updated", "open"),
        ("acknowledged", "acknowledged"),
        ("recovered", "recovered"),
        ("archived", "archived"),
    )
    for event_at, (event, lifecycle) in enumerate(valid_events, start=1):
        connection.execute(
            "INSERT INTO alert_events("
            "alert_id, event_type, event_at_utc_us, resulting_lifecycle"
            ") VALUES (?, ?, ?, ?)",
            (alert_id, event, event_at, lifecycle),
        )
    for values in (
        (None, "opened", "open"),
        (alert_id, "rejected_transition", "open"),
        (alert_id, "opened", "recovered"),
        (alert_id, "occurrence_updated", "archived"),
    ):
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO alert_events("
                "alert_id, event_type, event_at_utc_us, resulting_lifecycle"
                ") VALUES (?, ?, 10, ?)",
                values,
            )
    assert connection.execute("SELECT COUNT(*) FROM alert_events").fetchone() == (
        len(valid_events),
    )
    connection.close()


@pytest.mark.parametrize(
    ("table", "column", "invalid"),
    [
        ("health_samples", "overall_status", "unknown"),
        ("health_samples", "sample_kind", "unknown"),
        ("component_samples", "component", "unknown"),
        ("component_samples", "status", "unknown"),
        ("component_samples", "reason_code_1", "unknown"),
        ("alerts", "scope", "unknown"),
        ("alerts", "kind", "unknown"),
        ("alerts", "lifecycle", "expired"),
        ("alert_events", "event_type", "expired"),
        ("alert_events", "resulting_lifecycle", "expired"),
        ("evaluation_state", "gap_phase", "unknown"),
    ],
)
def test_every_fixed_enum_has_a_database_check_constraint(
    table: str, column: str, invalid: str
) -> None:
    assert f"{column} IN" in " ".join(schema.TABLE_DDL[table].split())
    assert invalid not in {
        item.value
        for enum_type in (
            HealthHistoryStatus,
            SampleKind,
            ComponentName,
            NormalizedReason,
            AlertScope,
            AlertKind,
            AlertLifecycle,
            LifecycleEvent,
            SamplingGapPhase,
        )
        for item in enum_type
    }


def test_bounded_numeric_and_digest_constraints_are_fixed_in_schema() -> None:
    combined = " ".join(schema.TABLE_DDL.values())
    for bound in (
        MAX_BOUNDED_COUNTER,
        MAX_COMPONENT_LATENCY_MS,
        MAX_SERVICE_UPTIME_MS,
        PROJECTION_DIGEST_BYTES,
    ):
        assert str(bound) in combined


def test_database_check_constraints_reject_unknown_enums_and_bounds(
    protected_directory: Path,
) -> None:
    path, store = _create_store(protected_directory)
    store.close()
    connection = _rw(path)
    connection.execute("PRAGMA foreign_keys = ON")
    invalid_samples = (
        (1, 1, "unknown", 1, "heartbeat", bytes(32), 0),
        (1, 1, "healthy", 1, "unknown", bytes(32), 0),
        (1, 1, "healthy", MAX_SERVICE_UPTIME_MS + 1, "heartbeat", bytes(32), 0),
        (1, 1, "healthy", 1, "heartbeat", bytes(31), 0),
        (1, 1, "healthy", 1, "heartbeat", bytes(32), MAX_BOUNDED_COUNTER + 1),
    )
    for values in invalid_samples:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO health_samples("
                "observed_at_utc_us, recorded_at_utc_us, overall_status, "
                "service_uptime_ms, sample_kind, projection_digest, missed_intervals"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                values,
            )
    sample_id = _insert_health_sample(connection)
    for column, value in (
        ("component", "unknown"),
        ("status", "unknown"),
        ("reason_code_1", "unknown"),
    ):
        values = {
            "component": "wled",
            "status": "healthy",
            "reason_code_1": "wled.healthy",
        }
        values[column] = value
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO component_samples("
                "sample_id, component, status, reason_code_1, "
                "checked_at_utc_us, latency_ms"
                ") VALUES (?, ?, ?, ?, 1, 1)",
                (
                    sample_id,
                    values["component"],
                    values["status"],
                    values["reason_code_1"],
                ),
            )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO component_samples("
            "sample_id, component, status, reason_code_1, checked_at_utc_us, latency_ms"
            ") VALUES (?, 'wled', 'healthy', 'wled.healthy', 1, ?)",
            (sample_id, MAX_COMPONENT_LATENCY_MS + 1),
        )
    for column, value in (
        ("scope", "unknown"),
        ("kind", "unknown"),
        ("lifecycle", "expired"),
    ):
        values = {"scope": "wled", "kind": "degraded", "lifecycle": "open"}
        values[column] = value
        with pytest.raises(sqlite3.IntegrityError):
            _insert_alert(connection, **values)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE evaluation_state SET gap_phase = 'unknown' WHERE scope = 'sampling'"
        )
    _insert_alert(connection)
    alert_id = connection.execute("SELECT MAX(id) FROM alerts").fetchone()[0]
    for event, lifecycle in (("unknown", "open"), ("opened", "expired")):
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO alert_events("
                "alert_id, event_type, event_at_utc_us, resulting_lifecycle"
                ") VALUES (?, ?, 1, ?)",
                (alert_id, event, lifecycle),
            )
    connection.close()


def test_quick_check_progress_handler_cancellation_fails_closed(
    protected_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, store = _create_store(protected_directory)
    store.close()
    monkeypatch.setattr(schema, "PROGRESS_HANDLER_STEPS", 1)
    ticks = iter((0.0, 3.0, 3.0, 3.0, 3.0))
    connection = _rw(path)
    connection.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(schema.SchemaVerificationError):
        schema.verify_schema_v1(connection, monotonic=lambda: next(ticks, 3.0))
    connection.close()


class _QuickCheckCursor:
    def __init__(self, rows: list[tuple[str]]) -> None:
        self._rows = rows

    def fetchmany(self, count: int) -> list[tuple[str]]:
        assert count == 2
        return self._rows


class _QuickCheckConnection:
    def __init__(
        self,
        *,
        rows: list[tuple[str]] | None = None,
        error: sqlite3.Error | None = None,
    ) -> None:
        self.rows = rows or [("ok",)]
        self.error = error
        self.handlers: list[tuple[object, int]] = []

    def set_progress_handler(self, handler: object, steps: int) -> None:
        self.handlers.append((handler, steps))

    def execute(self, statement: str) -> _QuickCheckCursor:
        assert statement == "PRAGMA quick_check(1)"
        if self.error is not None:
            raise self.error
        return _QuickCheckCursor(self.rows)


def test_quick_check_rejects_successful_result_completed_after_deadline() -> None:
    connection = _QuickCheckConnection()
    ticks = iter((0.0, schema.QUICK_CHECK_SECONDS + 0.001))
    with pytest.raises(schema.SchemaVerificationError) as caught:
        schema._bounded_quick_check(
            connection,  # type: ignore[arg-type]
            monotonic=lambda: next(ticks),
        )
    assert caught.value.reason == "quick_check_failed"
    assert connection.handlers[-1] == (None, 0)


def test_quick_check_accepts_completion_within_deadline_and_cleans_handler() -> None:
    connection = _QuickCheckConnection()
    ticks = iter((10.0, 10.0 + schema.QUICK_CHECK_SECONDS - 0.001))
    schema._bounded_quick_check(
        connection,  # type: ignore[arg-type]
        monotonic=lambda: next(ticks),
    )
    assert connection.handlers[0][0] is not None
    assert connection.handlers[-1] == (None, 0)


def test_quick_check_cleans_handler_after_sqlite_failure() -> None:
    connection = _QuickCheckConnection(error=sqlite3.DatabaseError("synthetic"))
    with pytest.raises(schema.SchemaVerificationError) as caught:
        schema._bounded_quick_check(
            connection,  # type: ignore[arg-type]
            monotonic=lambda: 0.0,
        )
    assert caught.value.reason == "quick_check_failed"
    assert connection.handlers[-1] == (None, 0)


def test_failed_creation_removes_only_its_incomplete_file(
    protected_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aurora_core.health_history.store as store_module

    unrelated = protected_directory / "unrelated"
    unrelated.write_bytes(b"evidence")
    unrelated.chmod(0o600)
    path = _database_path(protected_directory)

    def fail(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise schema.SchemaVerificationError("synthetic_failure")

    monkeypatch.setattr(store_module, "create_schema_v1", fail)
    with pytest.raises(StoreError):
        HealthHistoryStore.create(path, created_at_utc_us=1)
    assert not path.exists()
    assert unrelated.read_bytes() == b"evidence"


def test_creation_detects_a_newly_created_sidecar_identity_change_during_verification(
    protected_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aurora_core.health_history.store as store_module

    path = _database_path(protected_directory)
    original_verify = store_module.verify_schema_v1
    original_sidecars = store_module.validate_sidecars
    verification_complete = False

    def verify(*args: object, **kwargs: object) -> None:
        nonlocal verification_complete
        original_verify(*args, **kwargs)  # type: ignore[arg-type]
        verification_complete = True

    def changed_sidecars(candidate: Path) -> dict[str, object]:
        result = original_sidecars(candidate)
        if verification_complete and result:
            suffix = next(iter(result))
            identity = result[suffix]
            result[suffix] = type(identity)(
                identity.device,
                identity.inode + 1,
                identity.mode,
                identity.owner,
                identity.links,
                identity.size,
            )
        return result  # type: ignore[return-value]

    monkeypatch.setattr(store_module, "verify_schema_v1", verify)
    monkeypatch.setattr(store_module, "validate_sidecars", changed_sidecars)
    with pytest.raises(StoreError):
        HealthHistoryStore.create(path, created_at_utc_us=1)
    assert not path.exists()


def test_failed_creation_cleanup_removes_only_exact_captured_identities(
    protected_directory: Path,
) -> None:
    main = _database_path(protected_directory)
    wal = main.with_name(f"{main.name}-wal")
    shm = main.with_name(f"{main.name}-shm")
    for candidate, content in ((main, b"main"), (wal, b"wal")):
        candidate.write_bytes(content)
        candidate.chmod(0o600)
    main_identity = validate_database_file(main)
    wal_identity = validate_database_file(wal)
    remove_created_artifacts(main, main_identity, {"-wal": wal_identity})
    assert not main.exists()
    assert not wal.exists()

    for candidate, content in (
        (main, b"second main"),
        (wal, b"captured wal"),
        (shm, b"uncertain shm"),
    ):
        candidate.write_bytes(content)
        candidate.chmod(0o600)
    main_identity = validate_database_file(main)
    replaced_identity = validate_database_file(wal)
    replacement = protected_directory / "replacement-wal"
    replacement.write_bytes(b"replacement evidence")
    replacement.chmod(0o600)
    os.replace(replacement, wal)
    remove_created_artifacts(main, main_identity, {"-wal": replaced_identity})
    assert not main.exists()
    assert wal.read_bytes() == b"replacement evidence"
    assert shm.read_bytes() == b"uncertain shm"


def test_store_context_manager_and_explicit_close(protected_directory: Path) -> None:
    path = _database_path(protected_directory)
    with HealthHistoryStore.create(path, created_at_utc_us=1) as store:
        assert not store.closed
        store.verify()
    assert store.closed
    store.close()
    with pytest.raises(StoreError):
        store.verify()
    reopened = HealthHistoryStore.open_existing(path)
    reopened.close()
    assert reopened.closed


def test_runtime_entry_points_do_not_import_validation_or_history() -> None:
    repository = Path(__file__).resolve().parents[1]
    entry_points = (
        repository / "src/aurora_core/__main__.py",
        repository / "src/aurora_core/dashboard/server.py",
    )
    for path in entry_points:
        source = path.read_text(encoding="utf-8")
        assert "health_history" not in source
        assert "m18_validation" not in source


def test_public_health_schema_remains_version_one() -> None:
    assert _report().schema_version == 1


def test_no_expired_production_alert_lifecycle_exists() -> None:
    assert {state.value for state in AlertLifecycle} == {
        "open",
        "acknowledged",
        "recovered",
        "archived",
    }
    assert {event.value for event in LifecycleEvent} == {
        "opened",
        "occurrence_updated",
        "acknowledged",
        "recovered",
        "archived",
    }


def test_reason_rejection_is_finite_and_does_not_echo_unknown_value() -> None:
    value = "endpoint credential prohibited"
    result = normalize_component_reason(
        schema_version=1,
        component="wled",
        status="degraded",
        details=_wled_details(info=value),
    )
    assert result.rejection is RejectionCode.UNKNOWN_VALUE
    assert value not in repr(result)
