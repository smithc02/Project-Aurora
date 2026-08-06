"""Exhaustive synthetic tests for the alert-lifecycle reference model."""

from __future__ import annotations

from dataclasses import fields, replace

import pytest

from aurora_core.m18_validation.alert_lifecycle import (
    ALERT_COOLDOWN_SECONDS,
    MAX_ALERT_OCCURRENCES,
    AlertEvent,
    AlertInput,
    AlertKind,
    AlertLifecycle,
    AlertOperation,
    AlertOutcome,
    AlertScope,
    AlertState,
    evaluate_alert_lifecycle,
)


def _state(
    lifecycle: AlertLifecycle,
    *,
    kind: AlertKind = AlertKind.DEGRADED,
    cooldown_until: int = 1_000,
) -> AlertState:
    recovered_at = (
        100
        if lifecycle
        in {
            AlertLifecycle.RECOVERED,
            AlertLifecycle.ARCHIVED,
        }
        else None
    )
    return AlertState(
        scope=AlertScope.WLED,
        kind=kind,
        lifecycle=lifecycle,
        cooldown_until=cooldown_until,
        recovered_at=recovered_at,
    )


def _request(
    operation: AlertOperation,
    *,
    kind: AlertKind = AlertKind.DEGRADED,
    now: int = 1_000,
    persistence_succeeded: bool = True,
    escalation_kind: AlertKind | None = None,
) -> AlertInput:
    return AlertInput(
        operation=operation,
        scope=AlertScope.WLED,
        kind=kind,
        now=now,
        persistence_succeeded=persistence_succeeded,
        escalation_kind=escalation_kind,
    )


def test_no_alert_opens_one_fixed_alert() -> None:
    transition = evaluate_alert_lifecycle(None, _request(AlertOperation.OPEN, now=10))
    assert transition.outcome is AlertOutcome.APPLIED
    assert transition.event is AlertEvent.OPENED
    assert transition.created_alert is None
    assert transition.state == AlertState(
        scope=AlertScope.WLED,
        kind=AlertKind.DEGRADED,
        lifecycle=AlertLifecycle.OPEN,
        cooldown_until=10 + ALERT_COOLDOWN_SECONDS,
    )


@pytest.mark.parametrize(
    ("lifecycle", "operation", "expected", "outcome", "event"),
    [
        (
            AlertLifecycle.OPEN,
            AlertOperation.ACKNOWLEDGE,
            AlertLifecycle.ACKNOWLEDGED,
            AlertOutcome.APPLIED,
            AlertEvent.ACKNOWLEDGED,
        ),
        (
            AlertLifecycle.OPEN,
            AlertOperation.RECOVER,
            AlertLifecycle.RECOVERED,
            AlertOutcome.APPLIED,
            AlertEvent.RECOVERED,
        ),
        (
            AlertLifecycle.OPEN,
            AlertOperation.ARCHIVE,
            AlertLifecycle.OPEN,
            AlertOutcome.REJECTED,
            None,
        ),
        (
            AlertLifecycle.ACKNOWLEDGED,
            AlertOperation.ACKNOWLEDGE,
            AlertLifecycle.ACKNOWLEDGED,
            AlertOutcome.IDEMPOTENT,
            None,
        ),
        (
            AlertLifecycle.ACKNOWLEDGED,
            AlertOperation.RECOVER,
            AlertLifecycle.RECOVERED,
            AlertOutcome.APPLIED,
            AlertEvent.RECOVERED,
        ),
        (
            AlertLifecycle.ACKNOWLEDGED,
            AlertOperation.ARCHIVE,
            AlertLifecycle.ACKNOWLEDGED,
            AlertOutcome.REJECTED,
            None,
        ),
        (
            AlertLifecycle.RECOVERED,
            AlertOperation.ACKNOWLEDGE,
            AlertLifecycle.RECOVERED,
            AlertOutcome.REJECTED,
            None,
        ),
        (
            AlertLifecycle.RECOVERED,
            AlertOperation.RECOVER,
            AlertLifecycle.RECOVERED,
            AlertOutcome.IDEMPOTENT,
            None,
        ),
        (
            AlertLifecycle.RECOVERED,
            AlertOperation.ARCHIVE,
            AlertLifecycle.ARCHIVED,
            AlertOutcome.APPLIED,
            AlertEvent.ARCHIVED,
        ),
        (
            AlertLifecycle.ARCHIVED,
            AlertOperation.ACKNOWLEDGE,
            AlertLifecycle.ARCHIVED,
            AlertOutcome.REJECTED,
            None,
        ),
        (
            AlertLifecycle.ARCHIVED,
            AlertOperation.RECOVER,
            AlertLifecycle.ARCHIVED,
            AlertOutcome.IDEMPOTENT,
            None,
        ),
        (
            AlertLifecycle.ARCHIVED,
            AlertOperation.ARCHIVE,
            AlertLifecycle.ARCHIVED,
            AlertOutcome.IDEMPOTENT,
            None,
        ),
    ],
)
def test_every_acknowledge_recover_and_archive_state_pair(
    lifecycle: AlertLifecycle,
    operation: AlertOperation,
    expected: AlertLifecycle,
    outcome: AlertOutcome,
    event: AlertEvent | None,
) -> None:
    state = _state(lifecycle)
    transition = evaluate_alert_lifecycle(state, _request(operation))
    assert transition.state is not None
    assert transition.state.lifecycle is expected
    assert transition.outcome is outcome
    assert transition.event is event
    if outcome in {AlertOutcome.REJECTED, AlertOutcome.IDEMPOTENT}:
        assert transition.state is state
        assert not transition.mutated


@pytest.mark.parametrize(
    "lifecycle", [AlertLifecycle.OPEN, AlertLifecycle.ACKNOWLEDGED]
)
def test_matching_active_alert_updates_only_bounded_occurrence_metadata(
    lifecycle: AlertLifecycle,
) -> None:
    state = _state(lifecycle, cooldown_until=500)
    transition = evaluate_alert_lifecycle(state, _request(AlertOperation.OPEN, now=100))
    assert transition.state is not None
    assert transition.state.lifecycle is lifecycle
    assert transition.state.occurrence_count == 2
    assert transition.created_alert is None
    assert transition.event is AlertEvent.OCCURRENCE_UPDATED


def test_acknowledged_alert_never_reopens_in_place() -> None:
    state = _state(AlertLifecycle.ACKNOWLEDGED)
    repeated = evaluate_alert_lifecycle(state, _request(AlertOperation.OPEN, now=1_000))
    assert repeated.state is not None
    assert repeated.state.lifecycle is AlertLifecycle.ACKNOWLEDGED
    assert repeated.created_alert is None

    escalated = evaluate_alert_lifecycle(
        state,
        _request(
            AlertOperation.ESCALATE,
            now=1_000,
            escalation_kind=AlertKind.UNAVAILABLE,
        ),
    )
    assert escalated.state is state
    assert escalated.created_alert is not None
    assert escalated.created_alert.kind is AlertKind.UNAVAILABLE
    assert escalated.created_alert.lifecycle is AlertLifecycle.OPEN


def test_escalation_is_code_owned_and_creates_a_distinct_alert_kind() -> None:
    state = _state(AlertLifecycle.OPEN)
    transition = evaluate_alert_lifecycle(
        state,
        _request(
            AlertOperation.ESCALATE,
            escalation_kind=AlertKind.UNAVAILABLE,
        ),
    )
    assert transition.state is state
    assert transition.created_alert == AlertState(
        scope=AlertScope.WLED,
        kind=AlertKind.UNAVAILABLE,
        lifecycle=AlertLifecycle.OPEN,
        generation=2,
        cooldown_until=1_000 + ALERT_COOLDOWN_SECONDS,
    )
    assert transition.event is AlertEvent.OPENED

    for invalid in (
        _state(AlertLifecycle.RECOVERED),
        _state(AlertLifecycle.ARCHIVED),
        _state(AlertLifecycle.OPEN, kind=AlertKind.UNAVAILABLE),
        _state(AlertLifecycle.OPEN, kind=AlertKind.SAMPLING_GAP),
    ):
        rejected = evaluate_alert_lifecycle(
            invalid,
            AlertInput(
                operation=AlertOperation.ESCALATE,
                scope=invalid.scope,
                kind=invalid.kind,
                now=1_000,
                escalation_kind=AlertKind.UNAVAILABLE,
            ),
        )
        assert rejected.outcome is AlertOutcome.REJECTED
        assert rejected.state is invalid
        assert rejected.event is None


def test_recovery_records_one_fixed_event_and_duplicates_are_idempotent() -> None:
    open_state = _state(AlertLifecycle.OPEN)
    first = evaluate_alert_lifecycle(
        open_state, _request(AlertOperation.RECOVER, now=50)
    )
    assert first.state is not None
    assert first.state.recovered_at == 50
    assert first.event is AlertEvent.RECOVERED
    duplicate = evaluate_alert_lifecycle(
        first.state, _request(AlertOperation.RECOVER, now=60)
    )
    assert duplicate.state is first.state
    assert duplicate.event is None
    assert duplicate.outcome is AlertOutcome.IDEMPOTENT


def test_archive_requires_recovery_and_exact_cooldown_eligibility() -> None:
    recovered = _state(AlertLifecycle.RECOVERED, cooldown_until=1_000)
    before = evaluate_alert_lifecycle(
        recovered, _request(AlertOperation.ARCHIVE, now=999)
    )
    boundary = evaluate_alert_lifecycle(
        recovered, _request(AlertOperation.ARCHIVE, now=1_000)
    )
    assert before.outcome is AlertOutcome.REJECTED
    assert before.state is recovered
    assert before.event is None
    assert boundary.state is not None
    assert boundary.state.lifecycle is AlertLifecycle.ARCHIVED
    assert boundary.event is AlertEvent.ARCHIVED


def test_recovered_cooldown_updates_then_new_occurrence_is_distinct() -> None:
    recovered = _state(AlertLifecycle.RECOVERED, cooldown_until=1_000)
    within = evaluate_alert_lifecycle(recovered, _request(AlertOperation.OPEN, now=999))
    assert within.state is not None
    assert within.state.lifecycle is AlertLifecycle.RECOVERED
    assert within.state.occurrence_count == 2
    assert within.created_alert is None
    assert within.state.cooldown_until == 999 + ALERT_COOLDOWN_SECONDS

    boundary = evaluate_alert_lifecycle(
        recovered, _request(AlertOperation.OPEN, now=1_000)
    )
    assert boundary.state is recovered
    assert boundary.created_alert is not None
    assert boundary.created_alert.lifecycle is AlertLifecycle.OPEN
    assert boundary.created_alert.generation == 2


def test_archived_record_is_terminal_and_later_opening_creates_new_record() -> None:
    archived = _state(AlertLifecycle.ARCHIVED)
    transition = evaluate_alert_lifecycle(
        archived, _request(AlertOperation.OPEN, now=2_000)
    )
    assert transition.state is archived
    assert archived.lifecycle is AlertLifecycle.ARCHIVED
    assert transition.created_alert is not None
    assert transition.created_alert.lifecycle is AlertLifecycle.OPEN
    assert transition.created_alert.generation == 2
    assert transition.event is AlertEvent.OPENED


def test_occurrence_count_saturates() -> None:
    state = replace(_state(AlertLifecycle.OPEN), occurrence_count=MAX_ALERT_OCCURRENCES)
    transition = evaluate_alert_lifecycle(
        state, _request(AlertOperation.OPEN, now=1_000)
    )
    assert transition.state is not None
    assert transition.state.occurrence_count == MAX_ALERT_OCCURRENCES
    assert transition.event is AlertEvent.OCCURRENCE_UPDATED


@pytest.mark.parametrize("lifecycle", tuple(AlertLifecycle))
@pytest.mark.parametrize("operation", tuple(AlertOperation))
def test_failed_persistence_cannot_mutate_any_alert_transition(
    lifecycle: AlertLifecycle, operation: AlertOperation
) -> None:
    state = _state(lifecycle)
    escalation_kind = (
        AlertKind.UNAVAILABLE if operation is AlertOperation.ESCALATE else None
    )
    transition = evaluate_alert_lifecycle(
        state,
        _request(
            operation,
            persistence_succeeded=False,
            escalation_kind=escalation_kind,
        ),
    )
    assert transition.state is state
    assert transition.created_alert is None
    assert transition.event is None
    assert transition.outcome is AlertOutcome.NOT_PERSISTED
    assert not transition.mutated


def test_failed_initial_open_creates_nothing() -> None:
    transition = evaluate_alert_lifecycle(
        None,
        _request(AlertOperation.OPEN, persistence_succeeded=False),
    )
    assert transition.state is None
    assert transition.created_alert is None
    assert transition.outcome is AlertOutcome.NOT_PERSISTED


def test_scope_or_kind_mismatch_fails_closed() -> None:
    state = _state(AlertLifecycle.OPEN)
    wrong_scope = AlertInput(
        AlertOperation.ACKNOWLEDGE,
        AlertScope.HYPERHDR,
        AlertKind.DEGRADED,
        1_000,
    )
    wrong_kind = _request(AlertOperation.ACKNOWLEDGE, kind=AlertKind.UNAVAILABLE)
    for request in (wrong_scope, wrong_kind):
        transition = evaluate_alert_lifecycle(state, request)
        assert transition.state is state
        assert transition.event is None
        assert transition.outcome is AlertOutcome.REJECTED


def test_no_alert_rejects_every_operation_except_open() -> None:
    for operation in (
        AlertOperation.ACKNOWLEDGE,
        AlertOperation.RECOVER,
        AlertOperation.ARCHIVE,
        AlertOperation.ESCALATE,
    ):
        transition = evaluate_alert_lifecycle(
            None,
            _request(
                operation,
                escalation_kind=AlertKind.UNAVAILABLE
                if operation is AlertOperation.ESCALATE
                else None,
            ),
        )
        assert transition.state is None
        assert transition.event is None
        assert transition.outcome is AlertOutcome.REJECTED


def test_event_and_lifecycle_registries_are_exact() -> None:
    assert {event.value for event in AlertEvent} == {
        "opened",
        "occurrence_updated",
        "acknowledged",
        "recovered",
        "archived",
    }
    assert {lifecycle.value for lifecycle in AlertLifecycle} == {
        "open",
        "acknowledged",
        "recovered",
        "archived",
    }
    assert "expired" not in {lifecycle.value for lifecycle in AlertLifecycle}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"scope": "wled"},
        {"kind": "degraded"},
        {"lifecycle": "open"},
        {"generation": True},
        {"occurrence_count": 0},
        {"cooldown_until": -1},
    ],
)
def test_invalid_alert_state_values_fail_closed(kwargs: dict[str, object]) -> None:
    arguments: dict[str, object] = {
        "scope": AlertScope.WLED,
        "kind": AlertKind.DEGRADED,
        "lifecycle": AlertLifecycle.OPEN,
    }
    arguments.update(kwargs)
    with pytest.raises(ValueError):
        AlertState(**arguments)  # type: ignore[arg-type]


def test_alert_input_accepts_only_code_owned_types() -> None:
    with pytest.raises(ValueError, match="invalid_alert_operation"):
        AlertInput(  # type: ignore[arg-type]
            "open", AlertScope.WLED, AlertKind.DEGRADED, 0
        )
    with pytest.raises(ValueError, match="invalid_alert_scope"):
        AlertInput(  # type: ignore[arg-type]
            AlertOperation.OPEN, "wled", AlertKind.DEGRADED, 0
        )
    with pytest.raises(ValueError, match="invalid_alert_kind"):
        AlertInput(  # type: ignore[arg-type]
            AlertOperation.OPEN, AlertScope.WLED, "degraded", 0
        )


def test_alert_state_contains_only_fixed_sanitized_fields() -> None:
    assert {field.name for field in fields(AlertState)} == {
        "scope",
        "kind",
        "lifecycle",
        "generation",
        "occurrence_count",
        "cooldown_until",
        "recovered_at",
    }


def test_alert_state_and_transition_are_immutable() -> None:
    state = _state(AlertLifecycle.OPEN)
    transition = evaluate_alert_lifecycle(state, _request(AlertOperation.ACKNOWLEDGE))
    with pytest.raises(AttributeError):
        state.lifecycle = AlertLifecycle.RECOVERED  # type: ignore[misc]
    with pytest.raises(AttributeError):
        transition.outcome = AlertOutcome.REJECTED  # type: ignore[misc]
