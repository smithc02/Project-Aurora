"""Unit tests for the hardware-free runtime planning and lifecycle contracts."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from aurora_core.config import load_settings
from aurora_core.runtime import (
    COMPONENT_ORDER,
    ComponentHealthReport,
    ComponentHealthState,
    ComponentId,
    ComponentLifecycleState,
    OverallHealthState,
    RuntimeComponent,
    RuntimeController,
    build_runtime_plan,
    derive_overall_health,
)
from aurora_core.runtime.errors import (
    ComponentRegistryError,
    ComponentStartupError,
    MissingComponentError,
)


class FakeComponent:
    def __init__(
        self,
        component_id: ComponentId,
        events: list[str],
        *,
        fail_start: bool = False,
        fail_health: bool = False,
    ) -> None:
        self.component_id = component_id
        self.events = events
        self.fail_start = fail_start
        self.fail_health = fail_health

    def start(self) -> None:
        self.events.append(f"start:{self.component_id.value}")
        if self.fail_start:
            raise RuntimeError("do-not-print-this")

    def stop(self) -> None:
        self.events.append(f"stop:{self.component_id.value}")

    def health(self) -> ComponentHealthReport:
        if self.fail_health:
            raise RuntimeError("do-not-print-this")
        return ComponentHealthReport(self.component_id, ComponentHealthState.HEALTHY)


def test_default_plan_is_immutable_ordered_and_disabled() -> None:
    plan = build_runtime_plan(load_settings(environment={}))
    assert tuple(item.component_id for item in plan.components) == COMPONENT_ORDER
    assert all(not item.enabled for item in plan.components)
    assert all(
        item.initial_health == ComponentHealthState.DISABLED for item in plan.components
    )
    with pytest.raises(FrozenInstanceError):
        plan.lighting_zone_count = 2  # type: ignore[misc]


@pytest.mark.parametrize("section", ["wled", "hyperhdr", "capture_device"])
def test_enabled_settings_are_configured_but_unknown(section: str) -> None:
    value = (
        {"enabled": True, "identifier": "/dev/video0"}
        if section == "capture_device"
        else {"enabled": True, "host": "example.invalid"}
    )
    plan = build_runtime_plan(
        load_settings(environment={}, cli_overrides={section: value})
    )
    component = next(
        item for item in plan.components if item.component_id.value == section
    )
    assert (
        component.configured
        and component.initial_health == ComponentHealthState.UNKNOWN
    )


def test_plan_summarizes_resources_and_excludes_secret() -> None:
    secret = "do-not-print-this"
    settings = load_settings(
        environment={},
        cli_overrides={
            "mqtt": {"password": secret},
            "lighting_zones": [
                {"name": "rear", "enabled": True, "led_count": 10},
                {"name": "other", "enabled": False},
            ],
            "led_layout": {"orientation": "up"},
        },
    )
    plan = build_runtime_plan(settings)
    assert plan.lighting_zone_count == 1 and plan.led_layout_configured
    assert secret not in repr(plan)
    assert plan == build_runtime_plan(settings)


def test_fake_satisfies_contract() -> None:
    assert isinstance(FakeComponent(ComponentId.WLED, []), RuntimeComponent)


def test_controller_order_idempotence_and_disabled_skip() -> None:
    plan = build_runtime_plan(
        load_settings(
            environment={},
            cli_overrides={
                "wled": {"enabled": True, "host": "x"},
                "mqtt": {"enabled": True, "host": "x"},
            },
        )
    )
    events: list[str] = []
    controller = RuntimeController(
        plan,
        [
            FakeComponent(ComponentId.WLED, events),
            FakeComponent(ComponentId.MQTT, events),
            FakeComponent(ComponentId.DDP, events),
        ],
    )
    controller.start()
    controller.start()
    controller.stop()
    controller.stop()
    assert events == ["start:wled", "start:mqtt", "stop:mqtt", "stop:wled"]


def test_duplicate_start_failure_rolls_back_safely() -> None:
    plan = build_runtime_plan(
        load_settings(
            environment={},
            cli_overrides={
                "hyperhdr": {"enabled": True, "host": "x"},
                "wled": {"enabled": True, "host": "x"},
            },
        )
    )
    events: list[str] = []
    controller = RuntimeController(
        plan,
        [
            FakeComponent(ComponentId.HYPERHDR, events),
            FakeComponent(ComponentId.WLED, events, fail_start=True),
        ],
    )
    with pytest.raises(ComponentStartupError) as error:
        controller.start()
    assert "do-not-print-this" not in str(error.value)
    assert events == ["start:hyperhdr", "start:wled", "stop:hyperhdr"]
    assert (
        dict(controller.snapshot().lifecycle)[ComponentId.WLED]
        == ComponentLifecycleState.FAILED
    )


def test_duplicate_identity_rejected_and_health_failures_safe() -> None:
    plan = build_runtime_plan(
        load_settings(
            environment={}, cli_overrides={"wled": {"enabled": True, "host": "x"}}
        )
    )
    with pytest.raises(ComponentRegistryError):
        RuntimeController(
            plan,
            [FakeComponent(ComponentId.WLED, []), FakeComponent(ComponentId.WLED, [])],
        )
    controller = RuntimeController(
        plan, [FakeComponent(ComponentId.WLED, [], fail_health=True)]
    )
    report = controller.snapshot().health_reports[2]
    assert report.state == ComponentHealthState.UNHEALTHY
    assert "do-not-print-this" not in repr(report)


def test_missing_enabled_component_is_a_safe_error() -> None:
    plan = build_runtime_plan(
        load_settings(
            environment={},
            cli_overrides={"wled": {"enabled": True, "host": "example.invalid"}},
        )
    )
    controller = RuntimeController(plan, [])
    with pytest.raises(MissingComponentError) as error:
        controller.start()
    assert "example.invalid" not in str(error.value)
    assert dict(controller.snapshot().lifecycle)[ComponentId.WLED] == (
        ComponentLifecycleState.FAILED
    )


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ([], OverallHealthState.DISABLED),
        ([ComponentHealthState.HEALTHY], OverallHealthState.HEALTHY),
        ([ComponentHealthState.UNKNOWN], OverallHealthState.UNKNOWN),
        (
            [ComponentHealthState.DEGRADED, ComponentHealthState.HEALTHY],
            OverallHealthState.DEGRADED,
        ),
        (
            [ComponentHealthState.UNHEALTHY, ComponentHealthState.HEALTHY],
            OverallHealthState.UNHEALTHY,
        ),
    ],
)
def test_health_aggregation(
    states: list[ComponentHealthState], expected: OverallHealthState
) -> None:
    base = build_runtime_plan(load_settings(environment={}))
    plans = tuple(
        replace(base.components[index], enabled=True) for index in range(len(states))
    )
    reports = {
        plans[index].component_id: ComponentHealthReport(
            plans[index].component_id, state
        )
        for index, state in enumerate(states)
    }
    lifecycle = {plan.component_id: ComponentLifecycleState.RUNNING for plan in plans}
    assert derive_overall_health(plans, reports, lifecycle) == expected
    assert derive_overall_health(tuple(reversed(plans)), reports, lifecycle) == expected


def test_missing_report_and_failed_lifecycle_are_not_healthy() -> None:
    base = build_runtime_plan(load_settings(environment={}))
    plan = replace(base.components[0], enabled=True)
    assert derive_overall_health((plan,), {}, {}) == OverallHealthState.UNKNOWN
    assert (
        derive_overall_health(
            (plan,),
            {
                plan.component_id: ComponentHealthReport(
                    plan.component_id, ComponentHealthState.HEALTHY
                )
            },
            {plan.component_id: ComponentLifecycleState.FAILED},
        )
        == OverallHealthState.UNHEALTHY
    )
