"""Small deterministic orchestration over injected, synchronous components."""

from __future__ import annotations

from collections.abc import Iterable

from aurora_core.runtime.contracts import RuntimeComponent
from aurora_core.runtime.errors import (
    ComponentRegistryError,
    ComponentShutdownError,
    ComponentStartupError,
    MissingComponentError,
)
from aurora_core.runtime.health import derive_overall_health
from aurora_core.runtime.models import (
    ComponentHealthReport,
    ComponentHealthState,
    ComponentId,
    ComponentLifecycleState,
    RuntimePlan,
    RuntimeSnapshot,
)


class RuntimeController:
    """Own one plan and coordinate injected components in plan order only."""

    def __init__(
        self, plan: RuntimePlan, components: Iterable[RuntimeComponent]
    ) -> None:
        self._plan = plan
        self._components: dict[ComponentId, RuntimeComponent] = {}
        for component in components:
            if component.component_id in self._components:
                raise ComponentRegistryError(
                    "Duplicate runtime component identity: "
                    f"{component.component_id.value}"
                )
            self._components[component.component_id] = component
        self._lifecycle = {
            item.component_id: item.initial_lifecycle for item in plan.components
        }
        self._started: list[ComponentId] = []

    def start(self) -> None:
        """Start once in plan order; a repeated call after success is a no-op."""
        if self._started:
            return
        for item in self._plan.components:
            if not item.enabled:
                continue
            component = self._components.get(item.component_id)
            if component is None:
                self._lifecycle[item.component_id] = ComponentLifecycleState.FAILED
                self._rollback()
                raise MissingComponentError(item.component_id)
            self._lifecycle[item.component_id] = ComponentLifecycleState.STARTING
            try:
                component.start()
            except Exception as error:
                self._lifecycle[item.component_id] = ComponentLifecycleState.FAILED
                self._rollback()
                raise ComponentStartupError(item.component_id) from error
            self._lifecycle[item.component_id] = ComponentLifecycleState.RUNNING
            self._started.append(item.component_id)

    def stop(self) -> None:
        """Stop started components in reverse order; a repeated call is a no-op."""
        self._stop_started(raise_on_failure=True)

    def _rollback(self) -> None:
        self._stop_started(raise_on_failure=False)

    def _stop_started(self, *, raise_on_failure: bool) -> None:
        failure: tuple[ComponentId, Exception] | None = None
        while self._started:
            component_id = self._started.pop()
            component = self._components[component_id]
            self._lifecycle[component_id] = ComponentLifecycleState.STOPPING
            try:
                component.stop()
            except Exception as error:
                self._lifecycle[component_id] = ComponentLifecycleState.FAILED
                if failure is None:
                    failure = (component_id, error)
            else:
                self._lifecycle[component_id] = ComponentLifecycleState.STOPPED
        if failure is not None and raise_on_failure:
            raise ComponentShutdownError(failure[0]) from failure[1]

    def snapshot(self) -> RuntimeSnapshot:
        """Return an immutable snapshot with safe health-failure reports."""
        reports: dict[ComponentId, ComponentHealthReport] = {}
        for item in self._plan.components:
            if not item.enabled:
                reports[item.component_id] = ComponentHealthReport(
                    item.component_id, ComponentHealthState.DISABLED
                )
                continue
            component = self._components.get(item.component_id)
            if component is None:
                reports[item.component_id] = ComponentHealthReport(
                    item.component_id,
                    ComponentHealthState.UNHEALTHY,
                    "missing_component",
                    "Component is not injected.",
                )
                continue
            try:
                report = component.health()
            except Exception:
                report = ComponentHealthReport(
                    item.component_id,
                    ComponentHealthState.UNHEALTHY,
                    "health_check_failed",
                    "Component health check failed.",
                )
            reports[item.component_id] = report
        return RuntimeSnapshot(
            lifecycle=tuple(
                (item.component_id, self._lifecycle[item.component_id])
                for item in self._plan.components
            ),
            health_reports=tuple(
                reports[item.component_id] for item in self._plan.components
            ),
            overall_health=derive_overall_health(
                self._plan.components, reports, self._lifecycle
            ),
        )
