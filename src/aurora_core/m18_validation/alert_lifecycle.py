"""Schema-version-1 alert-lifecycle reference model for Milestone 18 review."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

ALERT_COOLDOWN_SECONDS = 15 * 60
MAX_ALERT_OCCURRENCES = 65_535
MAX_ALERT_GENERATION = 2**63 - 1
MAX_ALERT_TIME = 2**63 - 1


class AlertScope(StrEnum):
    OVERALL = "overall"
    WLED = "wled"
    HYPERHDR = "hyperhdr"
    CAPTURE = "capture"
    RASPBERRY_PI = "raspberry_pi"
    SAMPLING = "sampling"


class AlertKind(StrEnum):
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    SAMPLING_GAP = "sampling_gap"


class AlertLifecycle(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RECOVERED = "recovered"
    ARCHIVED = "archived"


class AlertOperation(StrEnum):
    OPEN = "open"
    ACKNOWLEDGE = "acknowledge"
    RECOVER = "recover"
    ARCHIVE = "archive"
    ESCALATE = "escalate"


class AlertEvent(StrEnum):
    OPENED = "opened"
    OCCURRENCE_UPDATED = "occurrence_updated"
    ACKNOWLEDGED = "acknowledged"
    RECOVERED = "recovered"
    ARCHIVED = "archived"


class AlertOutcome(StrEnum):
    APPLIED = "applied"
    IDEMPOTENT = "idempotent"
    REJECTED = "rejected"
    NOT_PERSISTED = "not_persisted"


@dataclass(frozen=True, slots=True)
class AlertState:
    """One immutable sanitized alert record; it contains no operator data."""

    scope: AlertScope
    kind: AlertKind
    lifecycle: AlertLifecycle
    generation: int = 1
    occurrence_count: int = 1
    cooldown_until: int = ALERT_COOLDOWN_SECONDS
    recovered_at: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, AlertScope):
            raise ValueError("invalid_alert_scope")
        if not isinstance(self.kind, AlertKind):
            raise ValueError("invalid_alert_kind")
        if not isinstance(self.lifecycle, AlertLifecycle):
            raise ValueError("invalid_alert_lifecycle")
        if type(self.generation) is not int or not (
            1 <= self.generation <= MAX_ALERT_GENERATION
        ):
            raise ValueError("invalid_alert_generation")
        if type(self.occurrence_count) is not int or not (
            1 <= self.occurrence_count <= MAX_ALERT_OCCURRENCES
        ):
            raise ValueError("invalid_alert_occurrence_count")
        if type(self.cooldown_until) is not int or not (
            0 <= self.cooldown_until <= MAX_ALERT_TIME
        ):
            raise ValueError("invalid_alert_cooldown")
        if self.recovered_at is not None and (
            type(self.recovered_at) is not int
            or not 0 <= self.recovered_at <= MAX_ALERT_TIME
        ):
            raise ValueError("invalid_alert_recovery_time")
        if self.lifecycle in {AlertLifecycle.RECOVERED, AlertLifecycle.ARCHIVED}:
            if self.recovered_at is None:
                raise ValueError("missing_alert_recovery_time")
        elif self.recovered_at is not None:
            raise ValueError("unexpected_alert_recovery_time")


@dataclass(frozen=True, slots=True)
class AlertInput:
    """One code-owned lifecycle request with no free-form fields."""

    operation: AlertOperation
    scope: AlertScope
    kind: AlertKind
    now: int
    persistence_succeeded: bool = True
    escalation_kind: AlertKind | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.operation, AlertOperation):
            raise ValueError("invalid_alert_operation")
        if not isinstance(self.scope, AlertScope):
            raise ValueError("invalid_alert_scope")
        if not isinstance(self.kind, AlertKind):
            raise ValueError("invalid_alert_kind")
        if type(self.now) is not int or not 0 <= self.now <= MAX_ALERT_TIME:
            raise ValueError("invalid_alert_time")
        if type(self.persistence_succeeded) is not bool:
            raise ValueError("invalid_alert_persistence_flag")
        if self.operation is AlertOperation.ESCALATE:
            if not isinstance(self.escalation_kind, AlertKind):
                raise ValueError("missing_escalation_kind")
        elif self.escalation_kind is not None:
            raise ValueError("unexpected_escalation_kind")


@dataclass(frozen=True, slots=True)
class AlertTransition:
    """Immutable result; only an applied persisted lifecycle change has an event."""

    previous: AlertState | None
    state: AlertState | None
    created_alert: AlertState | None
    event: AlertEvent | None
    outcome: AlertOutcome

    @property
    def mutated(self) -> bool:
        return self.state != self.previous or self.created_alert is not None


def evaluate_alert_lifecycle(
    state: AlertState | None, request: AlertInput
) -> AlertTransition:
    """Apply the reviewed lifecycle without SQL, routes, auth, or persistence."""
    if not request.persistence_succeeded:
        return AlertTransition(state, state, None, None, AlertOutcome.NOT_PERSISTED)
    if state is not None and (state.scope, state.kind) != (
        request.scope,
        request.kind,
    ):
        return _rejected(state)

    if request.operation is AlertOperation.OPEN:
        return _open_or_update(state, request)
    if state is None:
        return _rejected(state)
    if request.operation is AlertOperation.ACKNOWLEDGE:
        return _acknowledge(state)
    if request.operation is AlertOperation.RECOVER:
        return _recover(state, request.now)
    if request.operation is AlertOperation.ARCHIVE:
        return _archive(state, request.now)
    return _escalate(state, request)


def _open_or_update(state: AlertState | None, request: AlertInput) -> AlertTransition:
    if state is None:
        opened = _new_alert(request.scope, request.kind, request.now, 1)
        return AlertTransition(
            None, opened, None, AlertEvent.OPENED, AlertOutcome.APPLIED
        )
    if state.lifecycle in {AlertLifecycle.OPEN, AlertLifecycle.ACKNOWLEDGED}:
        updated = _update_occurrence(state, request.now)
        return AlertTransition(
            state,
            updated,
            None,
            AlertEvent.OCCURRENCE_UPDATED,
            AlertOutcome.APPLIED,
        )
    if (
        state.lifecycle is AlertLifecycle.RECOVERED
        and request.now < state.cooldown_until
    ):
        updated = _update_occurrence(state, request.now)
        return AlertTransition(
            state,
            updated,
            None,
            AlertEvent.OCCURRENCE_UPDATED,
            AlertOutcome.APPLIED,
        )
    if state.generation == MAX_ALERT_GENERATION:
        return _rejected(state)
    created = _new_alert(request.scope, request.kind, request.now, state.generation + 1)
    return AlertTransition(
        state, state, created, AlertEvent.OPENED, AlertOutcome.APPLIED
    )


def _acknowledge(state: AlertState) -> AlertTransition:
    if state.lifecycle is AlertLifecycle.OPEN:
        acknowledged = replace(state, lifecycle=AlertLifecycle.ACKNOWLEDGED)
        return AlertTransition(
            state,
            acknowledged,
            None,
            AlertEvent.ACKNOWLEDGED,
            AlertOutcome.APPLIED,
        )
    if state.lifecycle is AlertLifecycle.ACKNOWLEDGED:
        return _idempotent(state)
    return _rejected(state)


def _recover(state: AlertState, now: int) -> AlertTransition:
    if state.lifecycle in {AlertLifecycle.OPEN, AlertLifecycle.ACKNOWLEDGED}:
        recovered = replace(
            state,
            lifecycle=AlertLifecycle.RECOVERED,
            recovered_at=now,
            cooldown_until=max(state.cooldown_until, _bounded_deadline(now)),
        )
        return AlertTransition(
            state,
            recovered,
            None,
            AlertEvent.RECOVERED,
            AlertOutcome.APPLIED,
        )
    if state.lifecycle in {AlertLifecycle.RECOVERED, AlertLifecycle.ARCHIVED}:
        return _idempotent(state)
    return _rejected(state)


def _archive(state: AlertState, now: int) -> AlertTransition:
    if state.lifecycle is AlertLifecycle.ARCHIVED:
        return _idempotent(state)
    if state.lifecycle is not AlertLifecycle.RECOVERED or now < state.cooldown_until:
        return _rejected(state)
    archived = replace(state, lifecycle=AlertLifecycle.ARCHIVED)
    return AlertTransition(
        state,
        archived,
        None,
        AlertEvent.ARCHIVED,
        AlertOutcome.APPLIED,
    )


def _escalate(state: AlertState, request: AlertInput) -> AlertTransition:
    if (
        state.lifecycle not in {AlertLifecycle.OPEN, AlertLifecycle.ACKNOWLEDGED}
        or request.escalation_kind is not AlertKind.UNAVAILABLE
        or state.kind is not AlertKind.DEGRADED
        or state.generation == MAX_ALERT_GENERATION
    ):
        return _rejected(state)
    created = _new_alert(
        state.scope,
        AlertKind.UNAVAILABLE,
        request.now,
        state.generation + 1,
    )
    return AlertTransition(
        state, state, created, AlertEvent.OPENED, AlertOutcome.APPLIED
    )


def _new_alert(
    scope: AlertScope, kind: AlertKind, now: int, generation: int
) -> AlertState:
    return AlertState(
        scope=scope,
        kind=kind,
        lifecycle=AlertLifecycle.OPEN,
        generation=generation,
        cooldown_until=_bounded_deadline(now),
    )


def _update_occurrence(state: AlertState, now: int) -> AlertState:
    return replace(
        state,
        occurrence_count=min(state.occurrence_count + 1, MAX_ALERT_OCCURRENCES),
        cooldown_until=max(state.cooldown_until, _bounded_deadline(now)),
    )


def _bounded_deadline(now: int) -> int:
    return min(now + ALERT_COOLDOWN_SECONDS, MAX_ALERT_TIME)


def _idempotent(state: AlertState) -> AlertTransition:
    return AlertTransition(state, state, None, None, AlertOutcome.IDEMPOTENT)


def _rejected(state: AlertState | None) -> AlertTransition:
    return AlertTransition(state, state, None, None, AlertOutcome.REJECTED)
