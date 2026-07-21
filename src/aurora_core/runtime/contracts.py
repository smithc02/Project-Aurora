"""Narrow synchronous contracts for future adapter-boundary components."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from aurora_core.runtime.models import ComponentHealthReport, ComponentId


@runtime_checkable
class RuntimeComponent(Protocol):
    """A future adapter contract; implementations are injected, never constructed here.

    The synchronous form intentionally avoids lifecycle infrastructure. Future adapters
    may bridge asynchronous implementation details at their own boundary.
    """

    @property
    def component_id(self) -> ComponentId: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def health(self) -> ComponentHealthReport: ...
