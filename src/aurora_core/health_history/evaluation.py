"""Pure deterministic health-state evaluation for accepted observations."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from aurora_core.health_history.models import (
    MAX_BOUNDED_COUNTER,
    HealthHistoryStatus,
)

DEGRADED_OPEN_THRESHOLD: Final = 3
UNAVAILABLE_OPEN_THRESHOLD: Final = 2
HEALTHY_RECOVERY_THRESHOLD: Final = 2


class HealthEvaluationEvent(StrEnum):
    NONE = "none"
    CONDITION_CONFIRMED = "condition_confirmed"
    RECOVERY_CONFIRMED = "recovery_confirmed"


@dataclass(frozen=True, slots=True)
class HealthEvaluationState:
    current_status: HealthHistoryStatus | None = None
    candidate_status: HealthHistoryStatus | None = None
    consecutive_count: int = 0

    def __post_init__(self) -> None:
        if self.current_status is not None and not isinstance(
            self.current_status, HealthHistoryStatus
        ):
            raise ValueError("invalid_current_status")
        if self.candidate_status is not None and not isinstance(
            self.candidate_status, HealthHistoryStatus
        ):
            raise ValueError("invalid_candidate_status")
        if type(self.consecutive_count) is not int or not (
            0 <= self.consecutive_count <= MAX_BOUNDED_COUNTER
        ):
            raise ValueError("invalid_consecutive_count")
        if (self.candidate_status is None) != (self.consecutive_count == 0):
            raise ValueError("inconsistent_candidate_count")


@dataclass(frozen=True, slots=True)
class HealthEvaluationInput:
    status: HealthHistoryStatus
    intentional_disabled: bool = False
    active_health_alert: bool = False
    persistence_succeeded: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.status, HealthHistoryStatus):
            raise ValueError("invalid_observed_status")
        if any(
            type(value) is not bool
            for value in (
                self.intentional_disabled,
                self.active_health_alert,
                self.persistence_succeeded,
            )
        ):
            raise ValueError("invalid_evaluation_flag")
        if self.intentional_disabled and self.status is HealthHistoryStatus.HEALTHY:
            raise ValueError("inconsistent_disabled_status")


@dataclass(frozen=True, slots=True)
class HealthEvaluationTransition:
    previous: HealthEvaluationState
    state: HealthEvaluationState
    event: HealthEvaluationEvent

    @property
    def mutated(self) -> bool:
        return self.state != self.previous


def evaluate_health_state(
    state: HealthEvaluationState,
    observation: HealthEvaluationInput,
) -> HealthEvaluationTransition:
    """Advance one fixed health scope without I/O or wall-clock behavior."""
    if not observation.persistence_succeeded:
        return HealthEvaluationTransition(state, state, HealthEvaluationEvent.NONE)

    if observation.intentional_disabled:
        updated = HealthEvaluationState(current_status=observation.status)
        return HealthEvaluationTransition(state, updated, HealthEvaluationEvent.NONE)

    if observation.status is HealthHistoryStatus.HEALTHY:
        return _evaluate_healthy(state, observation.active_health_alert)

    count = (
        min(state.consecutive_count + 1, MAX_BOUNDED_COUNTER)
        if state.candidate_status is observation.status
        else 1
    )
    updated = HealthEvaluationState(
        current_status=observation.status,
        candidate_status=observation.status,
        consecutive_count=count,
    )
    threshold = (
        DEGRADED_OPEN_THRESHOLD
        if observation.status is HealthHistoryStatus.DEGRADED
        else UNAVAILABLE_OPEN_THRESHOLD
    )
    event = (
        HealthEvaluationEvent.CONDITION_CONFIRMED
        if count >= threshold
        else HealthEvaluationEvent.NONE
    )
    return HealthEvaluationTransition(state, updated, event)


def _evaluate_healthy(
    state: HealthEvaluationState, active_health_alert: bool
) -> HealthEvaluationTransition:
    if not active_health_alert:
        updated = HealthEvaluationState(current_status=HealthHistoryStatus.HEALTHY)
        return HealthEvaluationTransition(state, updated, HealthEvaluationEvent.NONE)
    count = (
        min(state.consecutive_count + 1, MAX_BOUNDED_COUNTER)
        if state.candidate_status is HealthHistoryStatus.HEALTHY
        else 1
    )
    if count >= HEALTHY_RECOVERY_THRESHOLD:
        updated = HealthEvaluationState(current_status=HealthHistoryStatus.HEALTHY)
        event = HealthEvaluationEvent.RECOVERY_CONFIRMED
    else:
        updated = HealthEvaluationState(
            current_status=HealthHistoryStatus.HEALTHY,
            candidate_status=HealthHistoryStatus.HEALTHY,
            consecutive_count=count,
        )
        event = HealthEvaluationEvent.NONE
    return HealthEvaluationTransition(state, updated, event)
