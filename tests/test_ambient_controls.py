"""Tests for Milestone 19 bounded combined ambient-mode controls."""

from __future__ import annotations

import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from functools import cache
from http import HTTPStatus
from types import SimpleNamespace
from typing import cast
from urllib.parse import urlencode

import pytest

from aurora_core.config import AuroraConfigurationError, load_settings
from aurora_core.config.models import (
    AmbientControlSettings,
    AuroraOperation,
    HyperHDROperation,
    WLEDOperation,
)
from aurora_core.control_plane.ambient_service import (
    AmbientControlAvailability,
    AmbientControlResult,
    AmbientControlService,
    AmbientControlStatus,
    AmbientControlStep,
)
from aurora_core.control_plane.audit import AuditReason, SecurityAudit
from aurora_core.control_plane.contracts import (
    AMBIENT_IMPLEMENTED_OPERATION_ORDER,
    AURORA_AMBIENT_OFF_CONFIRMATION_VALUE,
    LED_OUTPUT_DISABLE_CONFIRMATION_VALUE,
    POWER_OFF_CONFIRMATION_VALUE,
    VIDEO_GRABBER_DISABLE_CONFIRMATION_VALUE,
)
from aurora_core.control_plane.cookies import SESSION_COOKIE_NAME
from aurora_core.control_plane.hyperhdr_adapter import (
    HyperHDRAdapterReason,
    HyperHDRAdapterResult,
)
from aurora_core.control_plane.hyperhdr_service import (
    HyperHDRControlResult,
    HyperHDRControlService,
    HyperHDRControlStatus,
)
from aurora_core.control_plane.mutation_gate import ControlMutationGate
from aurora_core.control_plane.rendering import render_controls
from aurora_core.control_plane.service import ControlPlaneService
from aurora_core.control_plane.sessions import SessionContext
from aurora_core.control_plane.wled_adapter import AdapterReason, AdapterResult
from aurora_core.control_plane.wled_service import (
    WLEDControlResult,
    WLEDControlService,
    WLEDControlStatus,
)
from aurora_core.dashboard import server as dashboard_server
from aurora_core.dashboard.models import ComponentHealth, HealthReport, HealthStatus
from aurora_core.dashboard.server import DashboardHandler, build_server
from aurora_core.security.passwords import hash_password

ALL_WLED = tuple(WLEDOperation)
ALL_HYPERHDR = tuple(HyperHDROperation)
ALL_AMBIENT = tuple(AuroraOperation)
_CSRF = "c" * 43
_SESSION = SessionContext("operator", _CSRF, 300.0)


class AuditCapture:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, str | int]]] = []

    def __call__(self, event: str, fields: object) -> None:
        self.events.append((event, dict(fields)))  # type: ignore[arg-type]


def _verified_wled() -> WLEDControlResult:
    return WLEDControlResult(WLEDControlStatus.VERIFIED, AuditReason.VERIFIED)


def _verified_hyperhdr() -> HyperHDRControlResult:
    return HyperHDRControlResult(
        HyperHDRControlStatus.VERIFIED,
        AuditReason.VERIFIED,
    )


class ScriptedWLED:
    def __init__(
        self,
        gate: ControlMutationGate,
        log: list[str],
        *,
        operations: tuple[WLEDOperation, ...] = ALL_WLED,
        results: dict[WLEDOperation, WLEDControlResult] | None = None,
    ) -> None:
        self.mutation_gate = gate
        self.available_operations = operations
        self._log = log
        self._results = {} if results is None else results
        self.confirmations: list[str | None] = []

    def power_on(
        self,
        session: SessionContext | None,
        submitted_csrf: str | None,
        client_identifier: str,
    ) -> WLEDControlResult:
        self._log.append(WLEDOperation.POWER_ON.value)
        return self._results.get(WLEDOperation.POWER_ON, _verified_wled())

    def power_off(
        self,
        session: SessionContext | None,
        submitted_csrf: str | None,
        confirmation: str | None,
        client_identifier: str,
    ) -> WLEDControlResult:
        self._log.append(WLEDOperation.POWER_OFF.value)
        self.confirmations.append(confirmation)
        return self._results.get(WLEDOperation.POWER_OFF, _verified_wled())


class ScriptedHyperHDR:
    def __init__(
        self,
        gate: ControlMutationGate,
        log: list[str],
        *,
        operations: tuple[HyperHDROperation, ...] = ALL_HYPERHDR,
        results: dict[HyperHDROperation, HyperHDRControlResult] | None = None,
    ) -> None:
        self.mutation_gate = gate
        self.available_operations = operations
        self._log = log
        self._results = {} if results is None else results
        self.confirmations: list[tuple[HyperHDROperation, str | None]] = []

    def video_grabber_enable(
        self,
        session: SessionContext | None,
        submitted_csrf: str | None,
        client_identifier: str,
    ) -> HyperHDRControlResult:
        operation = HyperHDROperation.VIDEO_GRABBER_ENABLE
        self._log.append(operation.value)
        return self._results.get(operation, _verified_hyperhdr())

    def video_grabber_disable(
        self,
        session: SessionContext | None,
        submitted_csrf: str | None,
        confirmation: str | None,
        client_identifier: str,
    ) -> HyperHDRControlResult:
        operation = HyperHDROperation.VIDEO_GRABBER_DISABLE
        self._log.append(operation.value)
        self.confirmations.append((operation, confirmation))
        return self._results.get(operation, _verified_hyperhdr())

    def led_output_enable(
        self,
        session: SessionContext | None,
        submitted_csrf: str | None,
        client_identifier: str,
    ) -> HyperHDRControlResult:
        operation = HyperHDROperation.LED_OUTPUT_ENABLE
        self._log.append(operation.value)
        return self._results.get(operation, _verified_hyperhdr())

    def led_output_disable(
        self,
        session: SessionContext | None,
        submitted_csrf: str | None,
        confirmation: str | None,
        client_identifier: str,
    ) -> HyperHDRControlResult:
        operation = HyperHDROperation.LED_OUTPUT_DISABLE
        self._log.append(operation.value)
        self.confirmations.append((operation, confirmation))
        return self._results.get(operation, _verified_hyperhdr())


def _ambient_settings(
    *,
    enabled: bool = True,
    operations: tuple[AuroraOperation, ...] = ALL_AMBIENT,
    operation_limit: int = 20,
) -> AmbientControlSettings:
    return AmbientControlSettings.model_validate(
        {
            "enabled": enabled,
            "allowed_operations": list(operations),
            "operation_limit": operation_limit,
            "operation_window_seconds": 60,
        }
    )


def _scripted_service(
    *,
    wled_results: dict[WLEDOperation, WLEDControlResult] | None = None,
    hyperhdr_results: dict[HyperHDROperation, HyperHDRControlResult] | None = None,
    wled_operations: tuple[WLEDOperation, ...] = ALL_WLED,
    hyperhdr_operations: tuple[HyperHDROperation, ...] = ALL_HYPERHDR,
    settings: AmbientControlSettings | None = None,
    audit_capture: AuditCapture | None = None,
) -> tuple[AmbientControlService, ScriptedWLED, ScriptedHyperHDR, list[str]]:
    gate = ControlMutationGate()
    log: list[str] = []
    wled = ScriptedWLED(
        gate,
        log,
        operations=wled_operations,
        results=wled_results,
    )
    hyperhdr = ScriptedHyperHDR(
        gate,
        log,
        operations=hyperhdr_operations,
        results=hyperhdr_results,
    )
    audit = None if audit_capture is None else SecurityAudit(audit_capture)
    service = AmbientControlService(
        _ambient_settings() if settings is None else settings,
        authentication_enabled=True,
        wled_controls=cast(WLEDControlService, wled),
        hyperhdr_controls=cast(HyperHDRControlService, hyperhdr),
        mutation_gate=gate,
        limiter_digest_key=b"ambient-limiter-key",
        audit=audit,
    )
    return service, wled, hyperhdr, log


def test_configuration_defaults_registry_and_environment_are_fail_closed() -> None:
    defaults = load_settings(environment={})
    assert not defaults.ambient_controls.enabled
    assert defaults.ambient_controls.allowed_operations == ()
    assert defaults.ambient_controls.operation_limit == 20
    assert defaults.ambient_controls.operation_window_seconds == 60
    assert AMBIENT_IMPLEMENTED_OPERATION_ORDER == ALL_AMBIENT

    configured = load_settings(
        environment={
            "AURORA_AMBIENT_CONTROLS__ENABLED": "true",
            "AURORA_AMBIENT_CONTROLS__ALLOWED_OPERATIONS": (
                "aurora.ambient_on,aurora.ambient_off"
            ),
            "AURORA_AMBIENT_CONTROLS__OPERATION_LIMIT": "7",
            "AURORA_AMBIENT_CONTROLS__OPERATION_WINDOW_SECONDS": "30",
        }
    )
    assert configured.ambient_controls.enabled
    assert configured.ambient_controls.allowed_operations == ALL_AMBIENT
    assert configured.ambient_controls.operation_limit == 7
    assert configured.ambient_controls.operation_window_seconds == 30


@pytest.mark.parametrize(
    "override",
    [
        {"enabled": 1},
        {"allowed_operations": ["aurora.unknown"]},
        {"allowed_operations": ["aurora.ambient_on", "aurora.ambient_on"]},
        {"allowed_operations": "aurora.ambient_on"},
        {"operation_limit": 0},
        {"operation_limit": 121},
        {"operation_window_seconds": 0},
        {"operation_window_seconds": 3601},
    ],
)
def test_invalid_parent_configuration_is_rejected(override: dict[str, object]) -> None:
    with pytest.raises(AuroraConfigurationError):
        load_settings(
            environment={},
            cli_overrides={"ambient_controls": override},
        )


def test_parent_requires_explicit_allowlist_and_every_required_child() -> None:
    service, _, _, _ = _scripted_service(
        settings=_ambient_settings(operations=()),
    )
    assert service.availability is AmbientControlAvailability.NO_OPERATIONS
    assert service.available_operations == ()

    service, _, _, _ = _scripted_service(
        wled_operations=(),
    )
    assert (
        service.availability is AmbientControlAvailability.REQUIRED_CHILDREN_UNAVAILABLE
    )
    assert service.available_operations == ()

    service, _, _, _ = _scripted_service(
        settings=_ambient_settings(operations=(AuroraOperation.AMBIENT_ON,)),
        wled_operations=(WLEDOperation.POWER_ON,),
        hyperhdr_operations=(
            HyperHDROperation.VIDEO_GRABBER_ENABLE,
            HyperHDROperation.LED_OUTPUT_ENABLE,
        ),
    )
    assert service.available_operations == (AuroraOperation.AMBIENT_ON,)


def test_parent_policy_rejections_and_metadata_are_fixed_and_precede_children() -> None:
    gate = ControlMutationGate()
    log: list[str] = []
    wled = ScriptedWLED(gate, log)
    hyperhdr = ScriptedHyperHDR(gate, log)
    unauthenticated = AmbientControlService(
        _ambient_settings(),
        authentication_enabled=False,
        wled_controls=cast(WLEDControlService, wled),
        hyperhdr_controls=cast(HyperHDRControlService, hyperhdr),
        mutation_gate=gate,
    )
    assert (
        unauthenticated.availability
        is AmbientControlAvailability.AUTHENTICATION_UNAVAILABLE
    )
    assert unauthenticated.available_operations == ()

    disabled, _, _, _ = _scripted_service(
        settings=_ambient_settings(enabled=False),
    )
    assert disabled.availability is AmbientControlAvailability.CONTROLS_DISABLED
    assert disabled.ambient_on(_SESSION, _CSRF, "disabled").status is (
        AmbientControlStatus.DENIED
    )

    on_only, _, _, _ = _scripted_service(
        settings=_ambient_settings(operations=(AuroraOperation.AMBIENT_ON,)),
    )
    assert on_only.operation_contracts == tuple(on_only.operation_contracts)
    assert on_only.tracked_client_count == 0
    assert (
        on_only.ambient_off(
            _SESSION,
            _CSRF,
            AURORA_AMBIENT_OFF_CONFIRMATION_VALUE,
            "not-allowlisted",
        ).status
        is AmbientControlStatus.DENIED
    )

    missing_child, _, _, _ = _scripted_service(
        wled_operations=(),
    )
    assert (
        missing_child.ambient_on(_SESSION, _CSRF, "missing-child").status
        is AmbientControlStatus.DENIED
    )
    assert log == []


def test_ambient_on_runs_exact_verified_sequence_without_cached_shortcuts() -> None:
    service, _, _, log = _scripted_service()
    result = service.ambient_on(_SESSION, _CSRF, "client")
    assert result == AmbientControlResult(
        AmbientControlStatus.COMPLETED,
        (
            AmbientControlStep.VIDEO_GRABBER_ENABLE,
            AmbientControlStep.WLED_POWER_ON,
            AmbientControlStep.LED_OUTPUT_ENABLE,
        ),
        (
            AmbientControlStep.VIDEO_GRABBER_ENABLE,
            AmbientControlStep.WLED_POWER_ON,
            AmbientControlStep.LED_OUTPUT_ENABLE,
        ),
    )
    assert log == [
        "hyperhdr.video_grabber_enable",
        "wled.power_on",
        "hyperhdr.led_output_enable",
    ]


@pytest.mark.parametrize(
    ("child_status", "expected"),
    [
        (HyperHDRControlStatus.FAILED, AmbientControlStatus.FAILED),
        (HyperHDRControlStatus.UNVERIFIED, AmbientControlStatus.UNVERIFIED),
        (HyperHDRControlStatus.DENIED, AmbientControlStatus.DENIED),
        (HyperHDRControlStatus.BUSY, AmbientControlStatus.BUSY),
        (HyperHDRControlStatus.RATE_LIMITED, AmbientControlStatus.RATE_LIMITED),
    ],
)
def test_ambient_on_stops_at_first_grabber_outcome(
    child_status: HyperHDRControlStatus,
    expected: AmbientControlStatus,
) -> None:
    reason = {
        HyperHDRControlStatus.FAILED: AuditReason.CONNECTION_FAILURE,
        HyperHDRControlStatus.UNVERIFIED: AuditReason.STATE_VERIFICATION_MISMATCH,
        HyperHDRControlStatus.DENIED: AuditReason.OPERATION_NOT_ALLOWLISTED,
        HyperHDRControlStatus.BUSY: AuditReason.OPERATION_IN_PROGRESS,
        HyperHDRControlStatus.RATE_LIMITED: AuditReason.OPERATION_LIMIT,
    }[child_status]
    service, _, _, log = _scripted_service(
        hyperhdr_results={
            HyperHDROperation.VIDEO_GRABBER_ENABLE: HyperHDRControlResult(
                child_status,
                reason,
            )
        }
    )
    result = service.ambient_on(_SESSION, _CSRF, "client")
    assert result.status is expected
    assert result.attempted_steps == (AmbientControlStep.VIDEO_GRABBER_ENABLE,)
    assert result.verified_steps == ()
    assert log == ["hyperhdr.video_grabber_enable"]


@pytest.mark.parametrize(
    "status",
    [
        WLEDControlStatus.FAILED,
        WLEDControlStatus.UNVERIFIED,
        WLEDControlStatus.DENIED,
        WLEDControlStatus.BUSY,
        WLEDControlStatus.RATE_LIMITED,
    ],
)
def test_ambient_on_reports_partial_after_verified_grabber(
    status: WLEDControlStatus,
) -> None:
    service, _, _, log = _scripted_service(
        wled_results={
            WLEDOperation.POWER_ON: WLEDControlResult(
                status,
                AuditReason.CONNECTION_FAILURE,
            )
        }
    )
    result = service.ambient_on(_SESSION, _CSRF, "client")
    assert result.status is AmbientControlStatus.PARTIALLY_COMPLETED
    assert result.verified_steps == (AmbientControlStep.VIDEO_GRABBER_ENABLE,)
    assert log == ["hyperhdr.video_grabber_enable", "wled.power_on"]


def test_ambient_on_led_failure_is_partial_without_rollback_or_retry() -> None:
    service, _, _, log = _scripted_service(
        hyperhdr_results={
            HyperHDROperation.LED_OUTPUT_ENABLE: HyperHDRControlResult(
                HyperHDRControlStatus.FAILED,
                AuditReason.TIMEOUT,
            )
        }
    )
    result = service.ambient_on(_SESSION, _CSRF, "client")
    assert result.status is AmbientControlStatus.PARTIALLY_COMPLETED
    assert result.verified_steps == (
        AmbientControlStep.VIDEO_GRABBER_ENABLE,
        AmbientControlStep.WLED_POWER_ON,
    )
    assert log == [
        "hyperhdr.video_grabber_enable",
        "wled.power_on",
        "hyperhdr.led_output_enable",
    ]


def test_ambient_off_runs_exact_sequence_and_fixed_child_confirmations() -> None:
    service, wled, hyperhdr, log = _scripted_service()
    result = service.ambient_off(
        _SESSION,
        _CSRF,
        AURORA_AMBIENT_OFF_CONFIRMATION_VALUE,
        "client",
    )
    assert result.status is AmbientControlStatus.COMPLETED
    assert result.attempted_steps == (
        AmbientControlStep.LED_OUTPUT_DISABLE,
        AmbientControlStep.VIDEO_GRABBER_DISABLE,
        AmbientControlStep.WLED_POWER_OFF,
    )
    assert result.verified_steps == result.attempted_steps
    assert log == [
        "hyperhdr.led_output_disable",
        "hyperhdr.video_grabber_disable",
        "wled.power_off",
    ]
    assert hyperhdr.confirmations == [
        (
            HyperHDROperation.LED_OUTPUT_DISABLE,
            LED_OUTPUT_DISABLE_CONFIRMATION_VALUE,
        ),
        (
            HyperHDROperation.VIDEO_GRABBER_DISABLE,
            VIDEO_GRABBER_DISABLE_CONFIRMATION_VALUE,
        ),
    ]
    assert wled.confirmations == [POWER_OFF_CONFIRMATION_VALUE]


@pytest.mark.parametrize(
    ("child_status", "expected"),
    [
        (HyperHDRControlStatus.FAILED, AmbientControlStatus.FAILED),
        (HyperHDRControlStatus.UNVERIFIED, AmbientControlStatus.UNVERIFIED),
        (HyperHDRControlStatus.DENIED, AmbientControlStatus.DENIED),
        (HyperHDRControlStatus.BUSY, AmbientControlStatus.BUSY),
        (HyperHDRControlStatus.RATE_LIMITED, AmbientControlStatus.RATE_LIMITED),
    ],
)
def test_ambient_off_stops_when_led_output_is_not_verified(
    child_status: HyperHDRControlStatus,
    expected: AmbientControlStatus,
) -> None:
    service, _, _, log = _scripted_service(
        hyperhdr_results={
            HyperHDROperation.LED_OUTPUT_DISABLE: HyperHDRControlResult(
                child_status,
                AuditReason.CONNECTION_FAILURE,
            )
        }
    )
    result = service.ambient_off(
        _SESSION,
        _CSRF,
        AURORA_AMBIENT_OFF_CONFIRMATION_VALUE,
        "client",
    )
    assert result.status is expected
    assert log == ["hyperhdr.led_output_disable"]


@pytest.mark.parametrize(
    "child_status",
    [
        HyperHDRControlStatus.FAILED,
        HyperHDRControlStatus.UNVERIFIED,
        HyperHDRControlStatus.DENIED,
        HyperHDRControlStatus.BUSY,
        HyperHDRControlStatus.RATE_LIMITED,
    ],
)
def test_ambient_off_continues_to_wled_after_grabber_failure(
    child_status: HyperHDRControlStatus,
) -> None:
    service, _, _, log = _scripted_service(
        hyperhdr_results={
            HyperHDROperation.VIDEO_GRABBER_DISABLE: HyperHDRControlResult(
                child_status,
                AuditReason.CONNECTION_FAILURE,
            )
        }
    )
    result = service.ambient_off(
        _SESSION,
        _CSRF,
        AURORA_AMBIENT_OFF_CONFIRMATION_VALUE,
        "client",
    )
    assert result.status is AmbientControlStatus.PARTIALLY_COMPLETED
    assert result.verified_steps == (
        AmbientControlStep.LED_OUTPUT_DISABLE,
        AmbientControlStep.WLED_POWER_OFF,
    )
    assert log == [
        "hyperhdr.led_output_disable",
        "hyperhdr.video_grabber_disable",
        "wled.power_off",
    ]


def test_ambient_off_wled_failure_is_partial_without_retry() -> None:
    service, _, _, log = _scripted_service(
        wled_results={
            WLEDOperation.POWER_OFF: WLEDControlResult(
                WLEDControlStatus.UNVERIFIED,
                AuditReason.STATE_VERIFICATION_MISMATCH,
            )
        }
    )
    result = service.ambient_off(
        _SESSION,
        _CSRF,
        AURORA_AMBIENT_OFF_CONFIRMATION_VALUE,
        "client",
    )
    assert result.status is AmbientControlStatus.PARTIALLY_COMPLETED
    assert log.count("wled.power_off") == 1


def test_parent_authentication_csrf_confirmation_and_limiter_precede_children() -> None:
    capture = AuditCapture()
    service, _, _, log = _scripted_service(
        settings=_ambient_settings(operation_limit=1),
        audit_capture=capture,
    )
    assert service.ambient_on(None, _CSRF, "a").status is AmbientControlStatus.DENIED
    assert service.ambient_on(_SESSION, None, "b").status is AmbientControlStatus.DENIED
    assert (
        service.ambient_on(_SESSION, "x" * 43, "c").status
        is AmbientControlStatus.DENIED
    )
    assert (
        service.ambient_off(_SESSION, _CSRF, None, "d").status
        is AmbientControlStatus.DENIED
    )
    assert (
        service.ambient_off(_SESSION, _CSRF, "wrong", "e").status
        is AmbientControlStatus.DENIED
    )
    assert log == []

    assert (
        service.ambient_on(_SESSION, _CSRF, "limited").status
        is AmbientControlStatus.COMPLETED
    )
    prior = list(log)
    assert (
        service.ambient_on(_SESSION, _CSRF, "limited").status
        is AmbientControlStatus.RATE_LIMITED
    )
    assert log == prior
    parent_events = [
        fields
        for _, fields in capture.events
        if str(fields.get("security_event", "")).startswith("aurora_operation_")
    ]
    assert len(parent_events) == 7


def test_gate_is_reentrant_nonblocking_and_rejects_other_threads() -> None:
    gate = ControlMutationGate()
    assert gate.acquire()
    assert gate.acquire()
    gate.release()
    with ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(gate.acquire).result(timeout=1) is False
    gate.release()
    with ThreadPoolExecutor(max_workers=1) as executor:
        assert executor.submit(gate.acquire).result(timeout=1) is True


def test_ambient_service_requires_the_exact_shared_gate() -> None:
    shared = ControlMutationGate()
    other = ControlMutationGate()
    wled = ScriptedWLED(shared, [])
    hyperhdr = ScriptedHyperHDR(other, [])
    with pytest.raises(ValueError, match="^shared_mutation_gate_required$"):
        AmbientControlService(
            _ambient_settings(),
            authentication_enabled=True,
            wled_controls=cast(WLEDControlService, wled),
            hyperhdr_controls=cast(HyperHDRControlService, hyperhdr),
            mutation_gate=shared,
        )


class FakeWLEDAdapter:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    def execute(
        self, operation: WLEDOperation, operation_input: object
    ) -> AdapterResult:
        self.log.append(operation.value)
        return AdapterResult(True, AdapterReason.VERIFIED)


class FakeHyperHDRAdapter:
    def __init__(self, log: list[str]) -> None:
        self.log = log

    def execute(self, operation: HyperHDROperation) -> HyperHDRAdapterResult:
        self.log.append(operation.value)
        return HyperHDRAdapterResult(True, True, HyperHDRAdapterReason.VERIFIED)


class BlockingWLEDAdapter(FakeWLEDAdapter):
    def __init__(self, log: list[str]) -> None:
        super().__init__(log)
        self.entered = threading.Event()
        self.release = threading.Event()

    def execute(
        self, operation: WLEDOperation, operation_input: object
    ) -> AdapterResult:
        self.log.append(operation.value)
        self.entered.set()
        assert self.release.wait(timeout=2)
        return AdapterResult(True, AdapterReason.VERIFIED)


class BlockingHyperHDRAdapter(FakeHyperHDRAdapter):
    def __init__(self, log: list[str]) -> None:
        super().__init__(log)
        self.entered = threading.Event()
        self.release = threading.Event()

    def execute(self, operation: HyperHDROperation) -> HyperHDRAdapterResult:
        self.log.append(operation.value)
        self.entered.set()
        assert self.release.wait(timeout=2)
        return HyperHDRAdapterResult(True, True, HyperHDRAdapterReason.VERIFIED)


class HealthStub:
    def __init__(self) -> None:
        self.calls = 0
        self.invalidations = 0
        self.report = _health_report()

    def get_health(self) -> HealthReport:
        self.calls += 1
        return self.report

    def invalidate(self) -> None:
        self.invalidations += 1


def _full_settings(
    *,
    parent_limit: int = 20,
    hyperhdr_limit: int = 20,
):
    return load_settings(
        environment={},
        cli_overrides={
            "dashboard": {
                "authentication": {
                    "enabled": True,
                    "username": "operator",
                    "password_hash": _password_hash(),
                    "session_ttl_minutes": 5,
                }
            },
            "wled": {
                "enabled": True,
                "host": "wled.invalid",
                "controls": {
                    "enabled": True,
                    "allowed_operations": [item.value for item in ALL_WLED],
                },
            },
            "hyperhdr": {
                "enabled": True,
                "host": "hyperhdr.invalid",
                "port": 8090,
                "controls": {
                    "enabled": True,
                    "allowed_operations": [item.value for item in ALL_HYPERHDR],
                    "operation_limit": hyperhdr_limit,
                },
            },
            "ambient_controls": {
                "enabled": True,
                "allowed_operations": [item.value for item in ALL_AMBIENT],
                "operation_limit": parent_limit,
            },
        },
    )


def _actual_services(
    *,
    health: HealthStub | None = None,
    capture: AuditCapture | None = None,
    wled_adapter: FakeWLEDAdapter | None = None,
    parent_limit: int = 20,
    hyperhdr_limit: int = 20,
) -> tuple[
    AmbientControlService,
    WLEDControlService,
    HyperHDRControlService,
    ControlMutationGate,
    list[str],
    HealthStub,
    AuditCapture,
]:
    settings = _full_settings(
        parent_limit=parent_limit,
        hyperhdr_limit=hyperhdr_limit,
    )
    gate = ControlMutationGate()
    log: list[str] = []
    active_health = HealthStub() if health is None else health
    active_capture = AuditCapture() if capture is None else capture
    audit = SecurityAudit(active_capture)
    wled = WLEDControlService(
        settings.wled,
        authentication_enabled=True,
        adapter=FakeWLEDAdapter(log) if wled_adapter is None else wled_adapter,
        limiter_digest_key=b"wled-limiter-key",
        audit=audit,
        cache_invalidator=active_health.invalidate,
        mutation_gate=gate,
    )
    hyperhdr = HyperHDRControlService(
        settings.hyperhdr,
        authentication_enabled=True,
        adapter=FakeHyperHDRAdapter(log),
        limiter_digest_key=b"hyperhdr-limiter-key",
        audit=audit,
        cache_invalidator=active_health.invalidate,
        mutation_gate=gate,
    )
    ambient = AmbientControlService(
        settings.ambient_controls,
        authentication_enabled=True,
        wled_controls=wled,
        hyperhdr_controls=hyperhdr,
        mutation_gate=gate,
        limiter_digest_key=b"ambient-limiter-key",
        audit=audit,
    )
    return ambient, wled, hyperhdr, gate, log, active_health, active_capture


def test_child_limiters_audits_and_cache_invalidation_remain_active() -> None:
    ambient, _, _, _, log, health, capture = _actual_services(hyperhdr_limit=1)
    result = ambient.ambient_on(_SESSION, _CSRF, "client")
    assert result.status is AmbientControlStatus.PARTIALLY_COMPLETED
    assert result.attempted_steps[-1] is AmbientControlStep.LED_OUTPUT_ENABLE
    assert result.verified_steps == (
        AmbientControlStep.VIDEO_GRABBER_ENABLE,
        AmbientControlStep.WLED_POWER_ON,
    )
    assert log == ["hyperhdr.video_grabber_enable", "wled.power_on"]
    assert health.invalidations == 2
    events = [fields["security_event"] for _, fields in capture.events]
    assert events == [
        "hyperhdr_operation_succeeded",
        "wled_operation_succeeded",
        "hyperhdr_operation_rate_limited",
        "aurora_operation_partially_completed",
    ]
    assert capture.events[-1][1] == {
        "schema_version": 1,
        "security_event": "aurora_operation_partially_completed",
        "reason_code": "partial_completion",
        "operation_id": "aurora.ambient_on",
    }


def test_shared_gate_blocks_standalone_and_combined_mutations_without_waiting() -> None:
    settings = _full_settings()
    gate = ControlMutationGate()
    log: list[str] = []
    blocker = BlockingWLEDAdapter(log)
    health = HealthStub()
    wled = WLEDControlService(
        settings.wled,
        authentication_enabled=True,
        adapter=blocker,
        mutation_gate=gate,
    )
    hyperhdr = HyperHDRControlService(
        settings.hyperhdr,
        authentication_enabled=True,
        adapter=FakeHyperHDRAdapter(log),
        mutation_gate=gate,
    )
    ambient = AmbientControlService(
        settings.ambient_controls,
        authentication_enabled=True,
        wled_controls=wled,
        hyperhdr_controls=hyperhdr,
        mutation_gate=gate,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        standalone = executor.submit(wled.power_on, _SESSION, _CSRF, "standalone")
        assert blocker.entered.wait(timeout=1)
        combined = ambient.ambient_on(_SESSION, _CSRF, "combined")
        hyper_result = executor.submit(
            hyperhdr.led_output_enable,
            _SESSION,
            _CSRF,
            "hyperhdr",
        ).result(timeout=1)
        assert combined.status is AmbientControlStatus.BUSY
        assert combined.attempted_steps == ()
        assert hyper_result.status is HyperHDRControlStatus.BUSY
        blocker.release.set()
        assert standalone.result(timeout=1).status is WLEDControlStatus.VERIFIED
    assert health.calls == 0


def test_combined_owner_blocks_every_competing_mutation_without_interleaving() -> None:
    settings = _full_settings()
    gate = ControlMutationGate()
    log: list[str] = []
    blocker = BlockingHyperHDRAdapter(log)
    wled = WLEDControlService(
        settings.wled,
        authentication_enabled=True,
        adapter=FakeWLEDAdapter(log),
        mutation_gate=gate,
    )
    hyperhdr = HyperHDRControlService(
        settings.hyperhdr,
        authentication_enabled=True,
        adapter=blocker,
        mutation_gate=gate,
    )
    ambient = AmbientControlService(
        settings.ambient_controls,
        authentication_enabled=True,
        wled_controls=wled,
        hyperhdr_controls=hyperhdr,
        mutation_gate=gate,
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        owner = executor.submit(ambient.ambient_on, _SESSION, _CSRF, "owner")
        assert blocker.entered.wait(timeout=1)
        assert (
            ambient.ambient_off(
                _SESSION,
                _CSRF,
                AURORA_AMBIENT_OFF_CONFIRMATION_VALUE,
                "competing-composite",
            ).status
            is AmbientControlStatus.BUSY
        )
        assert (
            wled.power_on(_SESSION, _CSRF, "competing-wled").status
            is WLEDControlStatus.BUSY
        )
        assert (
            hyperhdr.led_output_enable(
                _SESSION,
                _CSRF,
                "competing-hyperhdr",
            ).status
            is HyperHDRControlStatus.BUSY
        )
        assert log == ["hyperhdr.video_grabber_enable"]
        blocker.release.set()
        assert owner.result(timeout=2).status is AmbientControlStatus.COMPLETED
    assert log == [
        "hyperhdr.video_grabber_enable",
        "wled.power_on",
        "hyperhdr.led_output_enable",
    ]


def test_dashboard_default_construction_injects_one_gate_into_all_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeServer:
        def __init__(
            self,
            address: tuple[str, int],
            service: object,
            refresh_seconds: int,
            control_plane: object,
            wled_controls: WLEDControlService,
            hyperhdr_controls: HyperHDRControlService,
            ambient_controls: AmbientControlService,
            configuration_profile: object,
        ) -> None:
            self.wled_controls = wled_controls
            self.hyperhdr_controls = hyperhdr_controls
            self.ambient_controls = ambient_controls

    monkeypatch.setattr(dashboard_server, "DashboardHTTPServer", FakeServer)
    settings = _full_settings()
    server = build_server(settings, service=HealthStub(), port=0)
    assert server.ambient_controls.mutation_gate is server.wled_controls.mutation_gate
    assert (
        server.ambient_controls.mutation_gate is server.hyperhdr_controls.mutation_gate
    )


def test_parent_audit_and_result_never_expose_exception_canary() -> None:
    canary = "private-host-token-response"

    class RaisingHyperHDR(ScriptedHyperHDR):
        def video_grabber_enable(
            self,
            session: SessionContext | None,
            submitted_csrf: str | None,
            client_identifier: str,
        ) -> HyperHDRControlResult:
            self._log.append(HyperHDROperation.VIDEO_GRABBER_ENABLE.value)
            raise RuntimeError(canary)

    gate = ControlMutationGate()
    log: list[str] = []
    capture = AuditCapture()
    wled = ScriptedWLED(gate, log)
    hyperhdr = RaisingHyperHDR(gate, log)
    service = AmbientControlService(
        _ambient_settings(),
        authentication_enabled=True,
        wled_controls=cast(WLEDControlService, wled),
        hyperhdr_controls=cast(HyperHDRControlService, hyperhdr),
        mutation_gate=gate,
        audit=SecurityAudit(capture),
    )
    result = service.ambient_on(_SESSION, _CSRF, "client")
    rendered = repr(result) + json.dumps(capture.events)
    assert result.status is AmbientControlStatus.FAILED
    assert canary not in rendered
    assert len(capture.events) == 1


@cache
def _password_hash() -> str:
    return hash_password("ambient-test-password", salt=bytes(range(16)))


def _health_report() -> HealthReport:
    return HealthReport(
        status=HealthStatus.HEALTHY,
        checked_at="2026-01-01T00:00:00+00:00",
        service_uptime_seconds=5.0,
        components=(
            ComponentHealth(
                "wled",
                HealthStatus.HEALTHY,
                "available",
                "2026-01-01T00:00:00+00:00",
                1.0,
                {"output_on": True, "brightness": 128},
            ),
            ComponentHealth(
                "hyperhdr",
                HealthStatus.HEALTHY,
                "available",
                "2026-01-01T00:00:00+00:00",
                1.0,
                {
                    "instance_running": True,
                    "grabber_active": True,
                    "led_output_active": True,
                },
            ),
            ComponentHealth(
                "capture",
                HealthStatus.HEALTHY,
                "available",
                "2026-01-01T00:00:00+00:00",
                1.0,
                {"present": True},
            ),
        ),
    )


def _login(control: ControlPlaneService) -> tuple[str, SessionContext]:
    result = control.authenticate("operator", "ambient-test-password", "login")
    assert result.created_session is not None
    return result.created_session.token, result.created_session.session


def _headers(*, body: bytes = b"", cookie: str | None = None) -> Message:
    headers = Message()
    if body:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["Content-Length"] = str(len(body))
    if cookie is not None:
        headers["Cookie"] = cookie
    return headers


def _request(
    health: HealthStub,
    control: ControlPlaneService,
    wled: WLEDControlService,
    hyperhdr: HyperHDRControlService,
    ambient: AmbientControlService,
    path: str,
    *,
    method: str = "GET",
    body: bytes = b"",
    headers: Message | None = None,
) -> tuple[bytes, str, HTTPStatus, dict[str, str]]:
    handler = DashboardHandler.__new__(DashboardHandler)
    handler.server = SimpleNamespace(
        health_service=health,
        refresh_seconds=5,
        control_plane=control,
        wled_controls=wled,
        hyperhdr_controls=hyperhdr,
        ambient_controls=ambient,
        configuration_profile="test-profile",
    )
    handler.path = path
    handler.headers = Message() if headers is None else headers
    handler.rfile = io.BytesIO(body)
    handler.client_address = ("test-client", 0)
    responses: list[tuple[bytes, str, HTTPStatus, dict[str, str]]] = []

    def capture(
        response_body: bytes,
        content_type: str,
        status: HTTPStatus = HTTPStatus.OK,
        response_headers: dict[str, str] | None = None,
    ) -> None:
        responses.append(
            (
                response_body,
                content_type,
                status,
                {} if response_headers is None else dict(response_headers),
            )
        )

    handler._send = capture  # type: ignore[method-assign]
    getattr(handler, f"do_{method}")()
    return responses[-1]


def test_protected_routes_ui_notices_and_cached_health_boundary() -> None:
    settings = _full_settings()
    health = HealthStub()
    control = ControlPlaneService(
        settings.dashboard.authentication,
        limiter_digest_key=b"login-key",
    )
    token, session = _login(control)
    ambient, wled, hyperhdr, _, log, _, _ = _actual_services(health=health)
    cookie = f"{SESSION_COOKIE_NAME}={token}"

    page, content_type, status, _ = _request(
        health,
        control,
        wled,
        hyperhdr,
        ambient,
        "/controls",
        headers=_headers(cookie=cookie),
    )
    assert status is HTTPStatus.OK
    assert content_type == "text/html; charset=utf-8"
    assert b"Ambient Mode" in page
    assert b'action="/controls/ambient/on"' in page
    assert b'action="/controls/ambient/off"' in page
    assert AURORA_AMBIENT_OFF_CONFIRMATION_VALUE.encode() in page
    assert b"destructive-control" in page
    assert b'action="/controls/wled/power-on"' in page
    assert b'action="/controls/hyperhdr/led-output/disable"' in page
    assert health.calls == 1
    assert log == []

    public_api, _, public_status, _ = _request(
        health,
        control,
        wled,
        hyperhdr,
        ambient,
        "/api/health",
    )
    public_payload = json.loads(public_api)
    assert public_status is HTTPStatus.OK
    assert public_payload["schema_version"] == 1
    assert "ambient" not in public_payload
    assert health.calls == 2

    api, _, api_status, _ = _request(
        health,
        control,
        wled,
        hyperhdr,
        ambient,
        "/api/control/status",
        headers=_headers(cookie=cookie),
    )
    assert api_status is HTTPStatus.OK
    assert json.loads(api) == {
        "schema_version": 1,
        "authenticated": True,
        "mutations_enabled": True,
        "available_operations": [
            "aurora.ambient_on",
            "aurora.ambient_off",
            "wled.power_on",
            "wled.power_off",
            "wled.brightness_set",
            "hyperhdr.video_grabber_enable",
            "hyperhdr.video_grabber_disable",
            "hyperhdr.led_output_enable",
            "hyperhdr.led_output_disable",
        ],
    }
    assert health.calls == 2

    body = urlencode({"csrf_token": session.csrf_token}).encode()
    _, _, post_status, post_headers = _request(
        health,
        control,
        wled,
        hyperhdr,
        ambient,
        "/controls/ambient/on",
        method="POST",
        body=body,
        headers=_headers(body=body, cookie=cookie),
    )
    assert post_status is HTTPStatus.SEE_OTHER
    assert post_headers["Location"] == "/controls?notice=ambient_completed"
    assert health.calls == 2
    assert log == [
        "hyperhdr.video_grabber_enable",
        "wled.power_on",
        "hyperhdr.led_output_enable",
    ]

    notice, _, _, _ = _request(
        health,
        control,
        wled,
        hyperhdr,
        ambient,
        "/controls?notice=ambient_partial",
        headers=_headers(cookie=cookie),
    )
    assert b"completed only partially" in notice
    assert b"individual controls" in notice
    assert health.calls == 3


def test_ambient_http_auth_csrf_confirmation_and_form_bounds_fail_closed() -> None:
    settings = _full_settings()
    health = HealthStub()
    control = ControlPlaneService(settings.dashboard.authentication)
    token, session = _login(control)
    ambient, wled, hyperhdr, _, log, _, _ = _actual_services(health=health)
    cookie = f"{SESSION_COOKIE_NAME}={token}"

    _, _, status, headers = _request(
        health,
        control,
        wled,
        hyperhdr,
        ambient,
        "/controls/ambient/on",
        method="POST",
        body=b"csrf_token=x",
        headers=_headers(body=b"csrf_token=x"),
    )
    assert status is HTTPStatus.SEE_OTHER
    assert headers["Location"] == "/login?next=%2Fcontrols"

    missing_csrf = b""
    missing_csrf_headers = Message()
    missing_csrf_headers["Content-Type"] = "application/x-www-form-urlencoded"
    missing_csrf_headers["Content-Length"] = "0"
    missing_csrf_headers["Cookie"] = cookie
    _, _, status, headers = _request(
        health,
        control,
        wled,
        hyperhdr,
        ambient,
        "/controls/ambient/on",
        method="POST",
        body=missing_csrf,
        headers=missing_csrf_headers,
    )
    assert status is HTTPStatus.SEE_OTHER
    assert headers["Location"] == "/controls?notice=ambient_denied"

    wrong_confirmation = urlencode(
        {"csrf_token": session.csrf_token, "confirmation": "wrong"}
    ).encode()
    _, _, status, headers = _request(
        health,
        control,
        wled,
        hyperhdr,
        ambient,
        "/controls/ambient/off",
        method="POST",
        body=wrong_confirmation,
        headers=_headers(body=wrong_confirmation, cookie=cookie),
    )
    assert status is HTTPStatus.SEE_OTHER
    assert headers["Location"] == "/controls?notice=ambient_denied"

    oversized_headers = Message()
    oversized_headers["Content-Type"] = "application/x-www-form-urlencoded"
    oversized_headers["Content-Length"] = "1025"
    oversized_headers["Cookie"] = cookie
    body, _, status, _ = _request(
        health,
        control,
        wled,
        hyperhdr,
        ambient,
        "/controls/ambient/on",
        method="POST",
        headers=oversized_headers,
    )
    assert status is HTTPStatus.REQUEST_ENTITY_TOO_LARGE
    assert body == b"Unable to process the ambient operation request.\n"
    assert log == []


def test_rendering_exposes_only_available_parent_forms_and_fixed_notice() -> None:
    page = render_controls(
        _SESSION,
        report=_health_report(),
        ambient_availability=AmbientControlAvailability.AVAILABLE,
        ambient_operations=(AuroraOperation.AMBIENT_ON,),
        ambient_notice="ambient_unverified",
    )
    assert 'action="/controls/ambient/on"' in page
    assert 'action="/controls/ambient/off"' not in page
    assert "could not verify" in page
    assert "<script" not in page
    assert "health_history" not in page
