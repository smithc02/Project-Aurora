"""Safe exceptions for runtime orchestration."""

from __future__ import annotations

from aurora_core.runtime.models import ComponentId


class AuroraRuntimeError(Exception):
    """Base error with messages suitable for concise user-facing handling."""


class ComponentRegistryError(AuroraRuntimeError):
    """The injected component registry does not match the plan safely."""


class MissingComponentError(ComponentRegistryError):
    def __init__(self, component_id: ComponentId) -> None:
        super().__init__(
            f"Enabled runtime component is not injected: {component_id.value}"
        )


class ComponentStartupError(AuroraRuntimeError):
    def __init__(self, component_id: ComponentId) -> None:
        super().__init__(f"Runtime component failed to start: {component_id.value}")


class ComponentShutdownError(AuroraRuntimeError):
    def __init__(self, component_id: ComponentId) -> None:
        super().__init__(f"Runtime component failed to stop: {component_id.value}")
