"""Deterministic sampling-gap reference model for Milestone 18 review."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

MAX_GAP_COUNTER = 65_535
MAX_GAP_SEQUENCE = 2**63 - 1


class GapPhase(StrEnum):
    """Persisted evaluator phases in the schema-version-1 reference model."""

    CLEAR = "clear"
    CANDIDATE_ONE = "candidate_one"
    ACTIVE = "active"
    RECOVERY_ONE = "recovery_one"


class GapEvent(StrEnum):
    """Fixed sanitized outcomes from one reference-model evaluation."""

    NONE = "none"
    CANDIDATE_STARTED = "candidate_started"
    CANDIDATE_CLEARED = "candidate_cleared"
    OPENED = "opened"
    MISS_RECORDED = "miss_recorded"
    RECOVERY_STARTED = "recovery_started"
    RECOVERY_RESET = "recovery_reset"
    RECOVERED = "recovered"
    MARKER_RECORDED = "marker_recorded"
    DUPLICATE_IGNORED = "duplicate_ignored"
    PERSISTENCE_FAILED = "persistence_failed"
    SAMPLE_UNUSABLE = "sample_unusable"


@dataclass(frozen=True, slots=True)
class GapObservation:
    """One bounded scheduler observation supplied to the reference model."""

    sequence: int
    missed_intervals: int
    persistence_succeeded: bool = True
    health_collection_succeeded: bool = True
    restart_gap_marker: bool = False
    clock_discontinuity_marker: bool = False

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or not 0 <= self.sequence <= MAX_GAP_SEQUENCE:
            raise ValueError("invalid_gap_sequence")
        if type(self.missed_intervals) is not int or self.missed_intervals < 0:
            raise ValueError("invalid_missed_intervals")
        for value in (
            self.persistence_succeeded,
            self.health_collection_succeeded,
            self.restart_gap_marker,
            self.clock_discontinuity_marker,
        ):
            if type(value) is not bool:
                raise ValueError("invalid_gap_flag")
        if self.restart_gap_marker and self.clock_discontinuity_marker:
            raise ValueError("conflicting_gap_markers")


@dataclass(frozen=True, slots=True)
class GapState:
    """Minimal bounded persisted state needed by the reference evaluator."""

    phase: GapPhase = GapPhase.CLEAR
    committed_observations: int = 0
    largest_missed_interval_report: int = 0
    last_committed_sequence: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.phase, GapPhase):
            raise ValueError("invalid_gap_phase")
        if type(self.committed_observations) is not int or not (
            0 <= self.committed_observations <= MAX_GAP_COUNTER
        ):
            raise ValueError("invalid_committed_observations")
        if type(self.largest_missed_interval_report) is not int or not (
            0 <= self.largest_missed_interval_report <= MAX_GAP_COUNTER
        ):
            raise ValueError("invalid_largest_missed_report")
        if self.last_committed_sequence is not None and (
            type(self.last_committed_sequence) is not int
            or not 0 <= self.last_committed_sequence <= MAX_GAP_SEQUENCE
        ):
            raise ValueError("invalid_last_committed_sequence")

    @property
    def recovery_streak(self) -> int:
        return 1 if self.phase is GapPhase.RECOVERY_ONE else 0


@dataclass(frozen=True, slots=True)
class GapTransition:
    """Immutable result of evaluating one scheduler observation."""

    previous: GapState
    state: GapState
    event: GapEvent

    @property
    def mutated(self) -> bool:
        return self.state != self.previous


def evaluate_sampling_gap(
    state: GapState, observation: GapObservation
) -> GapTransition:
    """Apply the reviewed gap policy without storage or scheduler integration."""
    if not observation.persistence_succeeded:
        return GapTransition(state, state, GapEvent.PERSISTENCE_FAILED)
    if (
        state.last_committed_sequence is not None
        and observation.sequence <= state.last_committed_sequence
    ):
        return GapTransition(state, state, GapEvent.DUPLICATE_IGNORED)

    committed = replace(
        state,
        committed_observations=min(state.committed_observations + 1, MAX_GAP_COUNTER),
        last_committed_sequence=observation.sequence,
    )

    if observation.clock_discontinuity_marker:
        if state.phase is GapPhase.RECOVERY_ONE:
            return GapTransition(
                state,
                replace(committed, phase=GapPhase.ACTIVE),
                GapEvent.RECOVERY_RESET,
            )
        return GapTransition(state, committed, GapEvent.MARKER_RECORDED)

    if observation.restart_gap_marker and observation.missed_intervals == 0:
        return GapTransition(state, committed, GapEvent.MARKER_RECORDED)

    if observation.missed_intervals > 0:
        return _apply_missed_intervals(state, committed, observation.missed_intervals)

    if not observation.health_collection_succeeded:
        return GapTransition(state, committed, GapEvent.SAMPLE_UNUSABLE)

    if state.phase is GapPhase.CANDIDATE_ONE:
        return GapTransition(
            state,
            replace(committed, phase=GapPhase.CLEAR),
            GapEvent.CANDIDATE_CLEARED,
        )
    if state.phase is GapPhase.ACTIVE:
        return GapTransition(
            state,
            replace(committed, phase=GapPhase.RECOVERY_ONE),
            GapEvent.RECOVERY_STARTED,
        )
    if state.phase is GapPhase.RECOVERY_ONE:
        return GapTransition(
            state,
            replace(committed, phase=GapPhase.CLEAR),
            GapEvent.RECOVERED,
        )
    return GapTransition(state, committed, GapEvent.NONE)


def _apply_missed_intervals(
    previous: GapState, committed: GapState, missed_intervals: int
) -> GapTransition:
    updated = replace(
        committed,
        largest_missed_interval_report=max(
            previous.largest_missed_interval_report,
            min(missed_intervals, MAX_GAP_COUNTER),
        ),
    )
    if previous.phase in {GapPhase.ACTIVE, GapPhase.RECOVERY_ONE}:
        event = (
            GapEvent.RECOVERY_RESET
            if previous.phase is GapPhase.RECOVERY_ONE
            else GapEvent.MISS_RECORDED
        )
        return GapTransition(previous, replace(updated, phase=GapPhase.ACTIVE), event)
    if missed_intervals >= 2:
        return GapTransition(
            previous, replace(updated, phase=GapPhase.ACTIVE), GapEvent.OPENED
        )
    if previous.phase is GapPhase.CANDIDATE_ONE:
        return GapTransition(previous, updated, GapEvent.MISS_RECORDED)
    return GapTransition(
        previous,
        replace(updated, phase=GapPhase.CANDIDATE_ONE),
        GapEvent.CANDIDATE_STARTED,
    )
