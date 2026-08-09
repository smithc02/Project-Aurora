"""Independent production sampling-gap evaluator with reviewed semantics."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum

from aurora_core.health_history.models import (
    MAX_BOUNDED_COUNTER,
    MAX_TIMESTAMP_US,
    SamplingGapPhase,
)


class SamplingGapEvent(StrEnum):
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
class SamplingGapObservation:
    sequence: int
    missed_intervals: int
    persistence_succeeded: bool = True
    health_collection_succeeded: bool = True
    startup_gap_marker: bool = False
    clock_discontinuity_marker: bool = False

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or not 0 <= self.sequence <= MAX_TIMESTAMP_US:
            raise ValueError("invalid_gap_sequence")
        if type(self.missed_intervals) is not int or self.missed_intervals < 0:
            raise ValueError("invalid_missed_intervals")
        if any(
            type(value) is not bool
            for value in (
                self.persistence_succeeded,
                self.health_collection_succeeded,
                self.startup_gap_marker,
                self.clock_discontinuity_marker,
            )
        ):
            raise ValueError("invalid_gap_flag")
        if self.startup_gap_marker and self.clock_discontinuity_marker:
            raise ValueError("conflicting_gap_markers")


@dataclass(frozen=True, slots=True)
class SamplingGapState:
    phase: SamplingGapPhase = SamplingGapPhase.CLEAR
    committed_observations: int = 0
    largest_missed_interval_report: int = 0
    last_committed_sequence: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.phase, SamplingGapPhase):
            raise ValueError("invalid_gap_phase")
        for name, value in (
            ("committed_observations", self.committed_observations),
            ("largest_missed_interval_report", self.largest_missed_interval_report),
        ):
            if type(value) is not int or not 0 <= value <= MAX_BOUNDED_COUNTER:
                raise ValueError(f"invalid_{name}")
        if self.last_committed_sequence is not None and (
            type(self.last_committed_sequence) is not int
            or not 0 <= self.last_committed_sequence <= MAX_TIMESTAMP_US
        ):
            raise ValueError("invalid_last_committed_sequence")


@dataclass(frozen=True, slots=True)
class SamplingGapTransition:
    previous: SamplingGapState
    state: SamplingGapState
    event: SamplingGapEvent

    @property
    def mutated(self) -> bool:
        return self.state != self.previous


def evaluate_sampling_gap(
    state: SamplingGapState, observation: SamplingGapObservation
) -> SamplingGapTransition:
    """Apply one persisted scheduler observation without storage integration."""
    if not observation.persistence_succeeded:
        return SamplingGapTransition(state, state, SamplingGapEvent.PERSISTENCE_FAILED)
    if (
        state.last_committed_sequence is not None
        and observation.sequence <= state.last_committed_sequence
    ):
        return SamplingGapTransition(state, state, SamplingGapEvent.DUPLICATE_IGNORED)

    committed = replace(
        state,
        committed_observations=min(
            state.committed_observations + 1, MAX_BOUNDED_COUNTER
        ),
        last_committed_sequence=observation.sequence,
    )
    if observation.clock_discontinuity_marker:
        if state.phase is SamplingGapPhase.RECOVERY_ONE:
            return SamplingGapTransition(
                state,
                replace(committed, phase=SamplingGapPhase.ACTIVE),
                SamplingGapEvent.RECOVERY_RESET,
            )
        return SamplingGapTransition(state, committed, SamplingGapEvent.MARKER_RECORDED)
    if observation.startup_gap_marker and observation.missed_intervals == 0:
        return SamplingGapTransition(state, committed, SamplingGapEvent.MARKER_RECORDED)
    if observation.missed_intervals > 0:
        return _apply_misses(state, committed, observation.missed_intervals)
    if not observation.health_collection_succeeded:
        return SamplingGapTransition(state, committed, SamplingGapEvent.SAMPLE_UNUSABLE)
    if state.phase is SamplingGapPhase.CANDIDATE_ONE:
        return SamplingGapTransition(
            state,
            replace(committed, phase=SamplingGapPhase.CLEAR),
            SamplingGapEvent.CANDIDATE_CLEARED,
        )
    if state.phase is SamplingGapPhase.ACTIVE:
        return SamplingGapTransition(
            state,
            replace(committed, phase=SamplingGapPhase.RECOVERY_ONE),
            SamplingGapEvent.RECOVERY_STARTED,
        )
    if state.phase is SamplingGapPhase.RECOVERY_ONE:
        return SamplingGapTransition(
            state,
            replace(committed, phase=SamplingGapPhase.CLEAR),
            SamplingGapEvent.RECOVERED,
        )
    return SamplingGapTransition(state, committed, SamplingGapEvent.NONE)


def _apply_misses(
    previous: SamplingGapState,
    committed: SamplingGapState,
    missed_intervals: int,
) -> SamplingGapTransition:
    updated = replace(
        committed,
        largest_missed_interval_report=max(
            previous.largest_missed_interval_report,
            min(missed_intervals, MAX_BOUNDED_COUNTER),
        ),
    )
    if previous.phase in {SamplingGapPhase.ACTIVE, SamplingGapPhase.RECOVERY_ONE}:
        event = (
            SamplingGapEvent.RECOVERY_RESET
            if previous.phase is SamplingGapPhase.RECOVERY_ONE
            else SamplingGapEvent.MISS_RECORDED
        )
        return SamplingGapTransition(
            previous, replace(updated, phase=SamplingGapPhase.ACTIVE), event
        )
    if missed_intervals >= 2:
        return SamplingGapTransition(
            previous,
            replace(updated, phase=SamplingGapPhase.ACTIVE),
            SamplingGapEvent.OPENED,
        )
    if previous.phase is SamplingGapPhase.CANDIDATE_ONE:
        return SamplingGapTransition(previous, updated, SamplingGapEvent.MISS_RECORDED)
    return SamplingGapTransition(
        previous,
        replace(updated, phase=SamplingGapPhase.CANDIDATE_ONE),
        SamplingGapEvent.CANDIDATE_STARTED,
    )
