"""Exhaustive synthetic tests for the sampling-gap reference model."""

from __future__ import annotations

import random
from dataclasses import replace

import pytest

from aurora_core.m18_validation.sampling_gap import (
    MAX_GAP_COUNTER,
    GapEvent,
    GapObservation,
    GapPhase,
    GapState,
    evaluate_sampling_gap,
)


def _step(
    state: GapState,
    sequence: int,
    missed: int,
    **kwargs: bool,
) -> GapState:
    observation = GapObservation(sequence=sequence, missed_intervals=missed, **kwargs)
    return evaluate_sampling_gap(state, observation).state


@pytest.mark.parametrize(
    ("missed", "phase", "event", "reported"),
    [
        (0, GapPhase.CLEAR, GapEvent.NONE, 0),
        (1, GapPhase.CANDIDATE_ONE, GapEvent.CANDIDATE_STARTED, 1),
        (2, GapPhase.ACTIVE, GapEvent.OPENED, 2),
        (100_000, GapPhase.ACTIVE, GapEvent.OPENED, MAX_GAP_COUNTER),
    ],
)
def test_zero_one_two_and_many_missed_intervals(
    missed: int, phase: GapPhase, event: GapEvent, reported: int
) -> None:
    transition = evaluate_sampling_gap(
        GapState(), GapObservation(sequence=1, missed_intervals=missed)
    )
    assert transition.state.phase is phase
    assert transition.event is event
    assert transition.state.largest_missed_interval_report == reported
    assert transition.state.committed_observations == 1


def test_one_delayed_observation_reporting_two_misses_opens_immediately() -> None:
    transition = evaluate_sampling_gap(
        GapState(), GapObservation(sequence=7, missed_intervals=2)
    )
    assert transition.previous.phase is GapPhase.CLEAR
    assert transition.state.phase is GapPhase.ACTIVE
    assert transition.event is GapEvent.OPENED


def test_repeated_misses_keep_gap_open_and_reset_recovery() -> None:
    active = _step(GapState(), 1, 2)
    repeated = evaluate_sampling_gap(
        active, GapObservation(sequence=2, missed_intervals=5)
    )
    assert repeated.state.phase is GapPhase.ACTIVE
    assert repeated.event is GapEvent.MISS_RECORDED

    recovering = _step(repeated.state, 3, 0)
    assert recovering.phase is GapPhase.RECOVERY_ONE
    reset = evaluate_sampling_gap(
        recovering, GapObservation(sequence=4, missed_intervals=1)
    )
    assert reset.state.phase is GapPhase.ACTIVE
    assert reset.state.recovery_streak == 0
    assert reset.event is GapEvent.RECOVERY_RESET


def test_two_consecutive_on_time_commits_are_required_for_recovery() -> None:
    active = _step(GapState(), 1, 2)
    first = evaluate_sampling_gap(
        active, GapObservation(sequence=2, missed_intervals=0)
    )
    assert first.state.phase is GapPhase.RECOVERY_ONE
    assert first.state.recovery_streak == 1
    assert first.event is GapEvent.RECOVERY_STARTED

    second = evaluate_sampling_gap(
        first.state, GapObservation(sequence=3, missed_intervals=0)
    )
    assert second.state.phase is GapPhase.CLEAR
    assert second.state.recovery_streak == 0
    assert second.event is GapEvent.RECOVERED


def test_one_on_time_sample_clears_only_an_unopened_candidate() -> None:
    candidate = _step(GapState(), 1, 1)
    cleared = evaluate_sampling_gap(
        candidate, GapObservation(sequence=2, missed_intervals=0)
    )
    assert cleared.state.phase is GapPhase.CLEAR
    assert cleared.event is GapEvent.CANDIDATE_CLEARED


@pytest.mark.parametrize(
    "initial",
    [
        GapState(),
        GapState(phase=GapPhase.CANDIDATE_ONE, last_committed_sequence=1),
        GapState(phase=GapPhase.ACTIVE, last_committed_sequence=1),
        GapState(phase=GapPhase.RECOVERY_ONE, last_committed_sequence=1),
    ],
)
def test_failed_persistence_never_mutates_gap_state(initial: GapState) -> None:
    transition = evaluate_sampling_gap(
        initial,
        GapObservation(
            sequence=2,
            missed_intervals=10,
            persistence_succeeded=False,
        ),
    )
    assert transition.state is initial
    assert not transition.mutated
    assert transition.event is GapEvent.PERSISTENCE_FAILED


def test_failed_persistence_during_recovery_does_not_advance_or_reset() -> None:
    recovery = GapState(
        phase=GapPhase.RECOVERY_ONE,
        committed_observations=2,
        last_committed_sequence=2,
    )
    failed_on_time = evaluate_sampling_gap(
        recovery,
        GapObservation(
            sequence=3,
            missed_intervals=0,
            persistence_succeeded=False,
        ),
    )
    failed_miss = evaluate_sampling_gap(
        recovery,
        GapObservation(
            sequence=3,
            missed_intervals=1,
            persistence_succeeded=False,
        ),
    )
    assert failed_on_time.state == failed_miss.state == recovery


def test_unsuccessful_health_collection_cannot_recover_or_clear_candidate() -> None:
    for phase in (GapPhase.CANDIDATE_ONE, GapPhase.ACTIVE, GapPhase.RECOVERY_ONE):
        state = GapState(phase=phase, last_committed_sequence=1)
        transition = evaluate_sampling_gap(
            state,
            GapObservation(
                sequence=2,
                missed_intervals=0,
                health_collection_succeeded=False,
            ),
        )
        assert transition.state.phase is phase
        assert transition.event is GapEvent.SAMPLE_UNUSABLE


def test_restart_marker_is_neutral_without_monotonic_miss_evidence() -> None:
    for phase in tuple(GapPhase):
        state = GapState(phase=phase, last_committed_sequence=1)
        transition = evaluate_sampling_gap(
            state,
            GapObservation(
                sequence=2,
                missed_intervals=0,
                restart_gap_marker=True,
            ),
        )
        assert transition.state.phase is phase
        assert transition.event is GapEvent.MARKER_RECORDED

    opened = evaluate_sampling_gap(
        GapState(),
        GapObservation(
            sequence=1,
            missed_intervals=2,
            restart_gap_marker=True,
        ),
    )
    assert opened.state.phase is GapPhase.ACTIVE
    assert opened.event is GapEvent.OPENED


def test_clock_discontinuity_never_opens_or_recovers_and_resets_recovery() -> None:
    neutral = evaluate_sampling_gap(
        GapState(),
        GapObservation(
            sequence=1,
            missed_intervals=100,
            clock_discontinuity_marker=True,
        ),
    )
    assert neutral.state.phase is GapPhase.CLEAR
    assert neutral.event is GapEvent.MARKER_RECORDED

    recovery = GapState(phase=GapPhase.RECOVERY_ONE, last_committed_sequence=1)
    reset = evaluate_sampling_gap(
        recovery,
        GapObservation(
            sequence=2,
            missed_intervals=0,
            clock_discontinuity_marker=True,
        ),
    )
    assert reset.state.phase is GapPhase.ACTIVE
    assert reset.event is GapEvent.RECOVERY_RESET


def test_duplicate_replay_is_idempotent() -> None:
    committed = _step(GapState(), 5, 2)
    duplicate = evaluate_sampling_gap(
        committed, GapObservation(sequence=5, missed_intervals=0)
    )
    older = evaluate_sampling_gap(
        committed, GapObservation(sequence=4, missed_intervals=100)
    )
    assert duplicate.state == older.state == committed
    assert duplicate.event is older.event is GapEvent.DUPLICATE_IGNORED
    assert not duplicate.mutated


def test_gap_counters_saturate_deterministically() -> None:
    state = GapState(
        phase=GapPhase.ACTIVE,
        committed_observations=MAX_GAP_COUNTER,
        largest_missed_interval_report=MAX_GAP_COUNTER,
        last_committed_sequence=1,
    )
    next_state = _step(state, 2, MAX_GAP_COUNTER + 1)
    assert next_state.committed_observations == MAX_GAP_COUNTER
    assert next_state.largest_missed_interval_report == MAX_GAP_COUNTER


@pytest.mark.parametrize(
    "kwargs",
    [
        {"sequence": -1, "missed_intervals": 0},
        {"sequence": 0, "missed_intervals": -1},
        {
            "sequence": 0,
            "missed_intervals": 0,
            "restart_gap_marker": True,
            "clock_discontinuity_marker": True,
        },
    ],
)
def test_invalid_gap_inputs_fail_with_fixed_errors(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        GapObservation(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"phase": "clear"},
        {"committed_observations": True},
        {"largest_missed_interval_report": -1},
        {"last_committed_sequence": True},
    ],
)
def test_invalid_gap_state_fails_closed(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        GapState(**kwargs)  # type: ignore[arg-type]


def _independent_phase_step(
    phase: GapPhase, observation: GapObservation, duplicate: bool
) -> GapPhase:
    if not observation.persistence_succeeded or duplicate:
        return phase
    if observation.clock_discontinuity_marker:
        return GapPhase.ACTIVE if phase is GapPhase.RECOVERY_ONE else phase
    if observation.restart_gap_marker and observation.missed_intervals == 0:
        return phase
    if observation.missed_intervals > 0:
        if phase in {GapPhase.ACTIVE, GapPhase.RECOVERY_ONE}:
            return GapPhase.ACTIVE
        if observation.missed_intervals >= 2:
            return GapPhase.ACTIVE
        return GapPhase.CANDIDATE_ONE
    if not observation.health_collection_succeeded:
        return phase
    return {
        GapPhase.CLEAR: GapPhase.CLEAR,
        GapPhase.CANDIDATE_ONE: GapPhase.CLEAR,
        GapPhase.ACTIVE: GapPhase.RECOVERY_ONE,
        GapPhase.RECOVERY_ONE: GapPhase.CLEAR,
    }[phase]


def test_arbitrary_bounded_sequences_match_independent_reference_table() -> None:
    rng = random.Random(18)
    state = GapState()
    reference_phase = GapPhase.CLEAR
    reference_last: int | None = None
    for index in range(1, 2_001):
        marker = rng.choice(("none", "restart", "clock"))
        sequence = reference_last if index % 17 == 0 and reference_last else index
        observation = GapObservation(
            sequence=sequence,
            missed_intervals=rng.choice((0, 1, 2, 3, MAX_GAP_COUNTER + 10)),
            persistence_succeeded=rng.choice((True, True, True, False)),
            health_collection_succeeded=rng.choice((True, True, False)),
            restart_gap_marker=marker == "restart",
            clock_discontinuity_marker=marker == "clock",
        )
        duplicate = reference_last is not None and sequence <= reference_last
        reference_phase = _independent_phase_step(
            reference_phase, observation, duplicate
        )
        if observation.persistence_succeeded and not duplicate:
            reference_last = sequence
        state = evaluate_sampling_gap(state, observation).state
        assert state.phase is reference_phase
        assert state.last_committed_sequence == reference_last


def test_gap_state_is_immutable() -> None:
    state = GapState()
    with pytest.raises(AttributeError):
        state.phase = GapPhase.ACTIVE  # type: ignore[misc]
    assert replace(state, phase=GapPhase.ACTIVE).phase is GapPhase.ACTIVE
