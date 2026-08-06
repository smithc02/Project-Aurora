"""Pure automatic alert lifecycle used by accepted-sample ingestion."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final

from aurora_core.health_history.models import (
    MAX_BOUNDED_COUNTER,
    MAX_TIMESTAMP_US,
    AlertKind,
    AlertLifecycle,
    AlertScope,
    LifecycleEvent,
)

ALERT_COOLDOWN_US: Final = 15 * 60 * 1_000_000


class AutomaticAlertOperation(StrEnum):
    OPEN = "open"
    RECOVER = "recover"
    ARCHIVE = "archive"
    ESCALATE = "escalate"


class AutomaticAlertOutcome(StrEnum):
    APPLIED = "applied"
    IDEMPOTENT = "idempotent"
    REJECTED = "rejected"
    NOT_PERSISTED = "not_persisted"


@dataclass(frozen=True, slots=True)
class AutomaticAlertState:
    scope: AlertScope
    kind: AlertKind
    lifecycle: AlertLifecycle
    generation: int = 1
    occurrence_count: int = 1
    cooldown_until_utc_us: int = ALERT_COOLDOWN_US
    recovered_at_utc_us: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, AlertScope):
            raise ValueError("invalid_alert_scope")
        if not isinstance(self.kind, AlertKind):
            raise ValueError("invalid_alert_kind")
        if not isinstance(self.lifecycle, AlertLifecycle):
            raise ValueError("invalid_alert_lifecycle")
        if type(self.generation) is not int or not (
            1 <= self.generation <= MAX_BOUNDED_COUNTER
        ):
            raise ValueError("invalid_alert_generation")
        if type(self.occurrence_count) is not int or not (
            1 <= self.occurrence_count <= MAX_BOUNDED_COUNTER
        ):
            raise ValueError("invalid_alert_occurrences")
        if type(self.cooldown_until_utc_us) is not int or not (
            0 <= self.cooldown_until_utc_us <= MAX_TIMESTAMP_US
        ):
            raise ValueError("invalid_alert_cooldown")
        if self.recovered_at_utc_us is not None and (
            type(self.recovered_at_utc_us) is not int
            or not 0 <= self.recovered_at_utc_us <= MAX_TIMESTAMP_US
        ):
            raise ValueError("invalid_alert_recovery_time")
        terminal = self.lifecycle in {
            AlertLifecycle.RECOVERED,
            AlertLifecycle.ARCHIVED,
        }
        if terminal != (self.recovered_at_utc_us is not None):
            raise ValueError("inconsistent_alert_recovery")
        if (self.scope is AlertScope.SAMPLING) != (self.kind is AlertKind.SAMPLING_GAP):
            raise ValueError("inconsistent_alert_scope_kind")


@dataclass(frozen=True, slots=True)
class AutomaticAlertInput:
    operation: AutomaticAlertOperation
    scope: AlertScope
    kind: AlertKind
    now_utc_us: int
    persistence_succeeded: bool = True
    escalation_kind: AlertKind | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.operation, AutomaticAlertOperation):
            raise ValueError("invalid_alert_operation")
        if not isinstance(self.scope, AlertScope):
            raise ValueError("invalid_alert_scope")
        if not isinstance(self.kind, AlertKind):
            raise ValueError("invalid_alert_kind")
        if (self.scope is AlertScope.SAMPLING) != (self.kind is AlertKind.SAMPLING_GAP):
            raise ValueError("inconsistent_alert_scope_kind")
        if type(self.now_utc_us) is not int or not (
            0 <= self.now_utc_us <= MAX_TIMESTAMP_US
        ):
            raise ValueError("invalid_alert_time")
        if type(self.persistence_succeeded) is not bool:
            raise ValueError("invalid_persistence_flag")
        if self.operation is AutomaticAlertOperation.ESCALATE:
            if self.escalation_kind is not AlertKind.UNAVAILABLE:
                raise ValueError("invalid_escalation_kind")
        elif self.escalation_kind is not None:
            raise ValueError("unexpected_escalation_kind")


@dataclass(frozen=True, slots=True)
class AutomaticAlertTransition:
    previous: AutomaticAlertState | None
    state: AutomaticAlertState | None
    created_alert: AutomaticAlertState | None
    event: LifecycleEvent | None
    outcome: AutomaticAlertOutcome

    @property
    def mutated(self) -> bool:
        return self.state != self.previous or self.created_alert is not None


def evaluate_automatic_alert(
    state: AutomaticAlertState | None,
    request: AutomaticAlertInput,
) -> AutomaticAlertTransition:
    """Evaluate only ingestion-authorized automatic lifecycle operations."""
    if not request.persistence_succeeded:
        return AutomaticAlertTransition(
            state, state, None, None, AutomaticAlertOutcome.NOT_PERSISTED
        )
    if state is not None and (state.scope, state.kind) != (
        request.scope,
        request.kind,
    ):
        return _rejected(state)
    if request.operation is AutomaticAlertOperation.OPEN:
        return _open_or_update(state, request)
    if state is None:
        return _rejected(state)
    if request.operation is AutomaticAlertOperation.RECOVER:
        return _recover(state, request.now_utc_us)
    if request.operation is AutomaticAlertOperation.ARCHIVE:
        return _archive(state, request.now_utc_us)
    return _escalate(state, request)


def _open_or_update(
    state: AutomaticAlertState | None,
    request: AutomaticAlertInput,
) -> AutomaticAlertTransition:
    if state is None:
        opened = _new_alert(request.scope, request.kind, request.now_utc_us, 1)
        return AutomaticAlertTransition(
            None,
            opened,
            None,
            LifecycleEvent.OPENED,
            AutomaticAlertOutcome.APPLIED,
        )
    if state.lifecycle in {AlertLifecycle.OPEN, AlertLifecycle.ACKNOWLEDGED}:
        return _occurrence_transition(state, request.now_utc_us)
    if (
        state.lifecycle is AlertLifecycle.RECOVERED
        and request.now_utc_us < state.cooldown_until_utc_us
    ):
        return _occurrence_transition(state, request.now_utc_us)
    generation = min(state.generation + 1, MAX_BOUNDED_COUNTER)
    created = _new_alert(request.scope, request.kind, request.now_utc_us, generation)
    return AutomaticAlertTransition(
        state,
        state,
        created,
        LifecycleEvent.OPENED,
        AutomaticAlertOutcome.APPLIED,
    )


def _recover(state: AutomaticAlertState, now_utc_us: int) -> AutomaticAlertTransition:
    if state.lifecycle in {AlertLifecycle.OPEN, AlertLifecycle.ACKNOWLEDGED}:
        recovered = replace(
            state,
            lifecycle=AlertLifecycle.RECOVERED,
            recovered_at_utc_us=now_utc_us,
            cooldown_until_utc_us=max(
                state.cooldown_until_utc_us, _deadline(now_utc_us)
            ),
        )
        return AutomaticAlertTransition(
            state,
            recovered,
            None,
            LifecycleEvent.RECOVERED,
            AutomaticAlertOutcome.APPLIED,
        )
    if state.lifecycle in {AlertLifecycle.RECOVERED, AlertLifecycle.ARCHIVED}:
        return _idempotent(state)
    return _rejected(state)


def _archive(state: AutomaticAlertState, now_utc_us: int) -> AutomaticAlertTransition:
    if state.lifecycle is AlertLifecycle.ARCHIVED:
        return _idempotent(state)
    if (
        state.lifecycle is not AlertLifecycle.RECOVERED
        or now_utc_us < state.cooldown_until_utc_us
    ):
        return _rejected(state)
    archived = replace(state, lifecycle=AlertLifecycle.ARCHIVED)
    return AutomaticAlertTransition(
        state,
        archived,
        None,
        LifecycleEvent.ARCHIVED,
        AutomaticAlertOutcome.APPLIED,
    )


def _escalate(
    state: AutomaticAlertState, request: AutomaticAlertInput
) -> AutomaticAlertTransition:
    if (
        state.lifecycle not in {AlertLifecycle.OPEN, AlertLifecycle.ACKNOWLEDGED}
        or state.kind is not AlertKind.DEGRADED
        or request.escalation_kind is not AlertKind.UNAVAILABLE
    ):
        return _rejected(state)
    created = _new_alert(
        state.scope,
        AlertKind.UNAVAILABLE,
        request.now_utc_us,
        min(state.generation + 1, MAX_BOUNDED_COUNTER),
    )
    return AutomaticAlertTransition(
        state,
        state,
        created,
        LifecycleEvent.OPENED,
        AutomaticAlertOutcome.APPLIED,
    )


def _occurrence_transition(
    state: AutomaticAlertState, now_utc_us: int
) -> AutomaticAlertTransition:
    updated = replace(
        state,
        occurrence_count=min(state.occurrence_count + 1, MAX_BOUNDED_COUNTER),
        cooldown_until_utc_us=max(state.cooldown_until_utc_us, _deadline(now_utc_us)),
    )
    if updated == state:
        return _idempotent(state)
    return AutomaticAlertTransition(
        state,
        updated,
        None,
        LifecycleEvent.OCCURRENCE_UPDATED,
        AutomaticAlertOutcome.APPLIED,
    )


def _new_alert(
    scope: AlertScope, kind: AlertKind, now_utc_us: int, generation: int
) -> AutomaticAlertState:
    return AutomaticAlertState(
        scope=scope,
        kind=kind,
        lifecycle=AlertLifecycle.OPEN,
        generation=generation,
        cooldown_until_utc_us=_deadline(now_utc_us),
    )


def _deadline(now_utc_us: int) -> int:
    return min(now_utc_us + ALERT_COOLDOWN_US, MAX_TIMESTAMP_US)


def _idempotent(state: AutomaticAlertState) -> AutomaticAlertTransition:
    return AutomaticAlertTransition(
        state, state, None, None, AutomaticAlertOutcome.IDEMPOTENT
    )


def _rejected(state: AutomaticAlertState | None) -> AutomaticAlertTransition:
    return AutomaticAlertTransition(
        state, state, None, None, AutomaticAlertOutcome.REJECTED
    )
