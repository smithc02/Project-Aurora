"""One-transaction ingestion for validated production health projections."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, cast

from aurora_core.health_history.evaluation import (
    HealthEvaluationEvent,
    HealthEvaluationInput,
    HealthEvaluationState,
    HealthEvaluationTransition,
    evaluate_health_state,
)
from aurora_core.health_history.lifecycle import (
    AutomaticAlertInput,
    AutomaticAlertOperation,
    AutomaticAlertOutcome,
    AutomaticAlertState,
    AutomaticAlertTransition,
    evaluate_automatic_alert,
)
from aurora_core.health_history.models import (
    COMPONENT_ORDER,
    MAX_BOUNDED_COUNTER,
    MAX_OBSERVATION_SEQUENCE,
    MAX_SERVICE_UPTIME_MS,
    MAX_TIMESTAMP_US,
    PROJECTION_DIGEST_BYTES,
    REPLAY_LEDGER_CAPACITY,
    AlertKind,
    AlertLifecycle,
    AlertScope,
    ComponentName,
    HealthHistoryStatus,
    LifecycleEvent,
    SampleKind,
    SamplingGapPhase,
)
from aurora_core.health_history.projection import (
    ComponentProjection,
    HealthProjection,
    ProjectionError,
    validate_health_projection,
)
from aurora_core.health_history.reasons import NormalizedReason
from aurora_core.health_history.sampling_gap import (
    SamplingGapEvent,
    SamplingGapObservation,
    SamplingGapState,
    SamplingGapTransition,
    evaluate_sampling_gap,
)

HEARTBEAT_INTERVAL_US: Final = 15 * 60 * 1_000_000
MAX_RELEVANT_ALERTS: Final = 11

_ALERT_COLUMNS: Final = (
    "id, scope, kind, lifecycle, episode_count, occurrence_count, "
    "cooldown_until_utc_us, recovered_at_utc_us"
)
ACTIVE_HEALTH_ALERTS_SQL: Final = (
    f"SELECT {_ALERT_COLUMNS} FROM alerts "
    "WHERE scope = ? AND kind IN ('degraded', 'unavailable') "
    "AND lifecycle IN ('open', 'acknowledged') ORDER BY kind, id LIMIT 3"
)
ACTIVE_PAIR_ALERT_SQL: Final = (
    f"SELECT {_ALERT_COLUMNS} FROM alerts "
    "WHERE scope = ? AND kind = ? "
    "AND lifecycle IN ('open', 'acknowledged') ORDER BY id LIMIT 2"
)
CURRENT_ACTIVE_ALERT_SQL: Final = (
    f"SELECT {_ALERT_COLUMNS} FROM alerts "
    "WHERE scope = ? AND kind = ? "
    "AND lifecycle IN ('open', 'acknowledged') ORDER BY id DESC LIMIT 2"
)
LATEST_TERMINAL_ALERT_SQL: Final = (
    f"SELECT {_ALERT_COLUMNS} FROM alerts "
    "WHERE scope = ? AND kind = ? AND lifecycle IN ('recovered', 'archived') "
    "ORDER BY id DESC LIMIT 1"
)
ELIGIBLE_RECOVERED_ALERTS_SQL: Final = (
    f"SELECT {_ALERT_COLUMNS} FROM alerts "
    "WHERE lifecycle = 'recovered' AND cooldown_until_utc_us <= ? "
    "ORDER BY cooldown_until_utc_us, id LIMIT ?"
)


class IngestionOutcome(StrEnum):
    REPLAYED = "replayed"
    STATE_ONLY = "state_only"
    TRANSITION_STORED = "transition_stored"
    HEARTBEAT_STORED = "heartbeat_stored"
    STARTUP_MARKER_STORED = "startup_marker_stored"
    CLOCK_MARKER_STORED = "clock_marker_stored"


class IngestionRejection(StrEnum):
    INVALID_PROJECTION = "invalid_projection"
    STALE_SEQUENCE = "stale_sequence"
    SEQUENCE_CONFLICT = "sequence_conflict"
    GENERATION_EXHAUSTED = "generation_exhausted"
    STORAGE_BUSY = "storage_busy"
    PERSISTENCE_FAILED = "persistence_failed"
    MALFORMED_STATE = "malformed_state"
    TRUST_FAILED = "trust_failed"


class IngestionStage(StrEnum):
    ARCHIVAL = "archival"
    HISTORY = "history"
    COMPONENTS = "components"
    EVALUATION = "evaluation"
    ALERTS = "alerts"
    CHECKPOINT = "checkpoint"
    BEFORE_COMMIT = "before_commit"


class IngestionError(Exception):
    """Fixed failure with no SQL, path, exception, or submitted value."""

    def __init__(self, reason: IngestionRejection, *, trust_lost: bool = False) -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.trust_lost = trust_lost


@dataclass(frozen=True, slots=True)
class IngestionResult:
    outcome: IngestionOutcome

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, IngestionOutcome):
            raise ValueError("invalid_ingestion_outcome")


@dataclass(frozen=True, slots=True)
class _Checkpoint:
    sequence: int | None
    observed_at: int | None
    digest: bytes | None
    sample_kind: SampleKind | None
    accepted_count: int


@dataclass(frozen=True, slots=True)
class _ReplayEntry:
    sequence: int
    observed_at: int
    digest: bytes
    sample_kind: SampleKind


@dataclass(frozen=True, slots=True)
class _EvaluationRow:
    scope: AlertScope
    current_status: HealthHistoryStatus | None
    candidate_status: HealthHistoryStatus | None
    consecutive_count: int
    last_sample_id: int | None
    last_heartbeat_at: int | None
    gap_phase: SamplingGapPhase
    cooldown_until: int | None


@dataclass(frozen=True, slots=True)
class _StoredAlert:
    alert_id: int
    state: AutomaticAlertState


@dataclass(frozen=True, slots=True)
class _AlertPlan:
    existing_alert_id: int | None
    transition: AutomaticAlertTransition


@dataclass(frozen=True, slots=True)
class _StorageDecision:
    outcome: IngestionOutcome
    stored_kind: SampleKind | None
    updates_evaluator_baseline: bool = True


def ingest_projection(
    connection: sqlite3.Connection, projection: HealthProjection
) -> IngestionResult:
    """Persist one non-replayed observation in exactly one write transaction."""
    try:
        validate_health_projection(projection)
    except ProjectionError:
        raise IngestionError(IngestionRejection.INVALID_PROJECTION) from None

    transaction_started = False
    try:
        connection.execute("BEGIN IMMEDIATE")
        transaction_started = True
        checkpoint = _read_checkpoint(connection)
        _validate_replay_anchor(connection, checkpoint)
        if _is_replay_or_reject_stale(connection, checkpoint, projection):
            _rollback_after_failure(connection)
            transaction_started = False
            return IngestionResult(IngestionOutcome.REPLAYED)

        evaluation_rows = _read_evaluation_rows(connection)
        _archive_eligible_alerts(connection, projection.recorded_at_utc_us)
        _fault(IngestionStage.ARCHIVAL)

        health_transitions = _evaluate_health_scopes(
            connection, evaluation_rows, projection
        )
        gap_transition = _evaluate_gap(evaluation_rows, checkpoint, projection)
        decision = _storage_decision(
            connection, evaluation_rows, checkpoint, projection
        )

        alert_plans = _plan_automatic_alerts(
            connection,
            health_transitions,
            gap_transition,
            projection.recorded_at_utc_us,
            gap_observation_has_miss=(
                projection.missed_intervals > 0
                and projection.sample_kind is not SampleKind.CLOCK_DISCONTINUITY
            ),
        )
        if _requires_supporting_sample(alert_plans):
            decision = _force_transition_storage(decision)

        sample_id: int | None = None
        if decision.stored_kind is not None:
            sample_id = _insert_health_sample(
                connection, projection, decision.stored_kind
            )
        _fault(IngestionStage.HISTORY)
        if sample_id is not None:
            _insert_component_samples(connection, sample_id, projection.components)
        _fault(IngestionStage.COMPONENTS)

        _write_evaluation_rows(
            connection,
            evaluation_rows,
            health_transitions,
            gap_transition,
            sample_id=(sample_id if decision.updates_evaluator_baseline else None),
            heartbeat_at=(
                projection.recorded_at_utc_us if sample_id is not None else None
            ),
        )
        _fault(IngestionStage.EVALUATION)
        for plan in alert_plans:
            _persist_alert_plan(
                connection,
                plan,
                now_utc_us=projection.recorded_at_utc_us,
                supporting_sample_id=sample_id,
            )
        _fault(IngestionStage.ALERTS)

        _write_checkpoint_and_replay_ledger(connection, checkpoint, projection)
        _fault(IngestionStage.CHECKPOINT)
        _fault(IngestionStage.BEFORE_COMMIT)
        connection.commit()
        transaction_started = False
        return IngestionResult(decision.outcome)
    except IngestionError:
        if transaction_started:
            _rollback_after_failure(connection)
        raise
    except sqlite3.Error as error:
        if transaction_started:
            _rollback_after_failure(connection)
        raise _classified_sqlite_error(error) from None
    except (TypeError, ValueError):
        if transaction_started:
            _rollback_after_failure(connection)
        raise IngestionError(
            IngestionRejection.MALFORMED_STATE, trust_lost=True
        ) from None


def _read_checkpoint(connection: sqlite3.Connection) -> _Checkpoint:
    rows = connection.execute(
        "SELECT singleton_id, last_committed_sequence, "
        "last_accepted_observed_at_utc_us, "
        "last_accepted_projection_digest, last_accepted_sample_kind, "
        "accepted_observation_count FROM ingestion_checkpoint LIMIT 2"
    ).fetchall()
    if len(rows) != 1 or rows[0][0] != 1:
        raise IngestionError(IngestionRejection.MALFORMED_STATE, trust_lost=True)
    sequence, observed, digest, raw_kind, count = rows[0][1:]
    if type(count) is not int or not 0 <= count <= MAX_BOUNDED_COUNTER:
        raise IngestionError(IngestionRejection.MALFORMED_STATE, trust_lost=True)
    empty = (
        sequence is None
        and observed is None
        and digest is None
        and raw_kind is None
        and count == 0
    )
    if empty:
        return _Checkpoint(None, None, None, None, 0)
    if (
        type(sequence) is not int
        or not 0 <= sequence <= MAX_OBSERVATION_SEQUENCE
        or type(observed) is not int
        or not 0 <= observed <= MAX_TIMESTAMP_US
        or type(digest) is not bytes
        or len(digest) != PROJECTION_DIGEST_BYTES
        or count < 1
    ):
        raise IngestionError(IngestionRejection.MALFORMED_STATE, trust_lost=True)
    try:
        sample_kind = SampleKind(raw_kind)
    except (TypeError, ValueError):
        raise IngestionError(
            IngestionRejection.MALFORMED_STATE, trust_lost=True
        ) from None
    return _Checkpoint(sequence, observed, digest, sample_kind, count)


def _validate_replay_anchor(
    connection: sqlite3.Connection, checkpoint: _Checkpoint
) -> None:
    rows = connection.execute(
        "SELECT observation_sequence, observed_at_utc_us, projection_digest, "
        "accepted_sample_kind FROM accepted_observation_replay "
        "ORDER BY observation_sequence DESC LIMIT ?",
        (REPLAY_LEDGER_CAPACITY + 1,),
    ).fetchall()
    expected_rows = min(checkpoint.accepted_count, REPLAY_LEDGER_CAPACITY)
    if len(rows) != expected_rows:
        raise IngestionError(IngestionRejection.MALFORMED_STATE, trust_lost=True)
    if checkpoint.sequence is None:
        return
    retained = tuple(_replay_entry(row) for row in rows)
    latest = retained[0]
    if (
        latest.sequence != checkpoint.sequence
        or latest.observed_at != checkpoint.observed_at
        or latest.digest != checkpoint.digest
        or latest.sample_kind is not checkpoint.sample_kind
    ):
        raise IngestionError(IngestionRejection.MALFORMED_STATE, trust_lost=True)


def _is_replay_or_reject_stale(
    connection: sqlite3.Connection,
    checkpoint: _Checkpoint,
    projection: HealthProjection,
) -> bool:
    if (
        checkpoint.sequence is None
        or projection.observation_sequence > checkpoint.sequence
    ):
        return False
    row = connection.execute(
        "SELECT observation_sequence, observed_at_utc_us, projection_digest, "
        "accepted_sample_kind FROM accepted_observation_replay "
        "WHERE observation_sequence = ? LIMIT 1",
        (projection.observation_sequence,),
    ).fetchone()
    if row is None:
        raise IngestionError(IngestionRejection.STALE_SEQUENCE)
    accepted = _replay_entry(row)
    if (
        projection.observed_at_utc_us == accepted.observed_at
        and projection.digest == accepted.digest
        and projection.sample_kind is accepted.sample_kind
    ):
        return True
    raise IngestionError(IngestionRejection.SEQUENCE_CONFLICT)


def _replay_entry(row: tuple[object, ...]) -> _ReplayEntry:
    try:
        if (
            type(row[0]) is not int
            or not 0 <= row[0] <= MAX_OBSERVATION_SEQUENCE
            or type(row[1]) is not int
            or not 0 <= row[1] <= MAX_TIMESTAMP_US
            or type(row[2]) is not bytes
            or len(row[2]) != PROJECTION_DIGEST_BYTES
            or type(row[3]) is not str
        ):
            raise ValueError("invalid_replay_entry")
        return _ReplayEntry(row[0], row[1], row[2], SampleKind(row[3]))
    except (TypeError, ValueError):
        raise IngestionError(
            IngestionRejection.MALFORMED_STATE, trust_lost=True
        ) from None


def _read_evaluation_rows(
    connection: sqlite3.Connection,
) -> dict[AlertScope, _EvaluationRow]:
    rows = connection.execute(
        "SELECT scope, current_status, candidate_status, consecutive_count, "
        "last_sample_id, last_heartbeat_at_utc_us, gap_phase, "
        "cooldown_until_utc_us FROM evaluation_state ORDER BY scope LIMIT 7"
    ).fetchall()
    if len(rows) != len(AlertScope):
        raise IngestionError(IngestionRejection.MALFORMED_STATE, trust_lost=True)
    result: dict[AlertScope, _EvaluationRow] = {}
    for row in rows:
        try:
            scope = AlertScope(row[0])
            current = None if row[1] is None else HealthHistoryStatus(row[1])
            candidate = None if row[2] is None else HealthHistoryStatus(row[2])
            gap_phase = SamplingGapPhase(row[6])
        except (TypeError, ValueError):
            raise IngestionError(
                IngestionRejection.MALFORMED_STATE, trust_lost=True
            ) from None
        for value in (row[3],):
            if type(value) is not int or not 0 <= value <= MAX_BOUNDED_COUNTER:
                raise IngestionError(
                    IngestionRejection.MALFORMED_STATE, trust_lost=True
                )
        if row[4] is not None and (type(row[4]) is not int or row[4] < 1):
            raise IngestionError(IngestionRejection.MALFORMED_STATE, trust_lost=True)
        for value in (row[5], row[7]):
            if value is not None and (
                type(value) is not int or not 0 <= value <= MAX_TIMESTAMP_US
            ):
                raise IngestionError(
                    IngestionRejection.MALFORMED_STATE, trust_lost=True
                )
        if scope in result:
            raise IngestionError(IngestionRejection.MALFORMED_STATE, trust_lost=True)
        if scope is not AlertScope.SAMPLING:
            HealthEvaluationState(current, candidate, row[3])
            if gap_phase is not SamplingGapPhase.CLEAR:
                raise IngestionError(
                    IngestionRejection.MALFORMED_STATE, trust_lost=True
                )
        elif current is not None or candidate is not None:
            raise IngestionError(IngestionRejection.MALFORMED_STATE, trust_lost=True)
        result[scope] = _EvaluationRow(
            scope,
            current,
            candidate,
            row[3],
            row[4],
            row[5],
            gap_phase,
            row[7],
        )
    if set(result) != set(AlertScope):
        raise IngestionError(IngestionRejection.MALFORMED_STATE, trust_lost=True)
    return result


def _evaluate_health_scopes(
    connection: sqlite3.Connection,
    rows: dict[AlertScope, _EvaluationRow],
    projection: HealthProjection,
) -> dict[AlertScope, HealthEvaluationTransition]:
    component_by_name = {
        component.component: component for component in projection.components
    }
    results: dict[AlertScope, HealthEvaluationTransition] = {}
    for scope in (
        AlertScope.OVERALL,
        AlertScope.WLED,
        AlertScope.HYPERHDR,
        AlertScope.CAPTURE,
        AlertScope.RASPBERRY_PI,
    ):
        row = rows[scope]
        component = (
            None
            if scope is AlertScope.OVERALL
            else component_by_name[ComponentName(scope.value)]
        )
        status = projection.overall_status if component is None else component.status
        results[scope] = evaluate_health_state(
            HealthEvaluationState(
                row.current_status, row.candidate_status, row.consecutive_count
            ),
            HealthEvaluationInput(
                status=status,
                intentional_disabled=_intentional_disabled(
                    scope, projection, component
                ),
                active_health_alert=bool(_active_health_alerts(connection, scope)),
            ),
        )
    return results


def _intentional_disabled(
    scope: AlertScope,
    projection: HealthProjection,
    component: ComponentProjection | None,
) -> bool:
    disabled = {
        NormalizedReason.WLED_DISABLED,
        NormalizedReason.HYPERHDR_DISABLED,
        NormalizedReason.CAPTURE_DISABLED,
    }
    if component is not None:
        return len(component.reasons) == 1 and component.reasons[0] in disabled
    if projection.overall_status is HealthHistoryStatus.HEALTHY:
        return False
    worst = tuple(
        item
        for item in projection.components
        if item.status is projection.overall_status
    )
    return bool(worst) and all(
        len(item.reasons) == 1 and item.reasons[0] in disabled for item in worst
    )


def _evaluate_gap(
    rows: dict[AlertScope, _EvaluationRow],
    checkpoint: _Checkpoint,
    projection: HealthProjection,
) -> SamplingGapTransition:
    row = rows[AlertScope.SAMPLING]
    return evaluate_sampling_gap(
        SamplingGapState(
            phase=row.gap_phase,
            committed_observations=checkpoint.accepted_count,
            largest_missed_interval_report=row.consecutive_count,
            last_committed_sequence=checkpoint.sequence,
        ),
        SamplingGapObservation(
            sequence=projection.observation_sequence,
            missed_intervals=projection.missed_intervals,
            health_collection_succeeded=True,
            startup_gap_marker=projection.sample_kind is SampleKind.STARTUP_GAP,
            clock_discontinuity_marker=(
                projection.sample_kind is SampleKind.CLOCK_DISCONTINUITY
            ),
        ),
    )


def _storage_decision(
    connection: sqlite3.Connection,
    evaluation_rows: dict[AlertScope, _EvaluationRow],
    checkpoint: _Checkpoint,
    projection: HealthProjection,
) -> _StorageDecision:
    baseline_sample_id = _baseline_sample_reference(
        connection,
        evaluation_rows,
        last_committed_sequence=checkpoint.sequence,
    )
    if checkpoint.sequence is None and baseline_sample_id is not None:
        raise IngestionError(IngestionRejection.MALFORMED_STATE, trust_lost=True)
    if projection.sample_kind is SampleKind.STARTUP_GAP:
        return _StorageDecision(
            IngestionOutcome.STARTUP_MARKER_STORED,
            SampleKind.STARTUP_GAP,
            updates_evaluator_baseline=not (
                checkpoint.sequence is not None and baseline_sample_id is None
            ),
        )
    if projection.sample_kind is SampleKind.CLOCK_DISCONTINUITY:
        return _StorageDecision(
            IngestionOutcome.CLOCK_MARKER_STORED,
            SampleKind.CLOCK_DISCONTINUITY,
            updates_evaluator_baseline=not (
                checkpoint.sequence is not None and baseline_sample_id is None
            ),
        )
    if checkpoint.sequence is None or baseline_sample_id is None:
        return _StorageDecision(
            IngestionOutcome.TRANSITION_STORED, SampleKind.TRANSITION
        )
    if _projection_changed(
        connection,
        baseline_sample_id,
        projection,
        last_committed_sequence=checkpoint.sequence,
    ):
        return _StorageDecision(
            IngestionOutcome.TRANSITION_STORED, SampleKind.TRANSITION
        )
    last_heartbeat = evaluation_rows[AlertScope.OVERALL].last_heartbeat_at
    if last_heartbeat is None:
        raise IngestionError(IngestionRejection.MALFORMED_STATE, trust_lost=True)
    if (
        projection.recorded_at_utc_us >= last_heartbeat
        and projection.recorded_at_utc_us - last_heartbeat >= HEARTBEAT_INTERVAL_US
    ):
        return _StorageDecision(IngestionOutcome.HEARTBEAT_STORED, SampleKind.HEARTBEAT)
    return _StorageDecision(IngestionOutcome.STATE_ONLY, None)


def _baseline_sample_reference(
    connection: sqlite3.Connection,
    rows: dict[AlertScope, _EvaluationRow],
    *,
    last_committed_sequence: int | None,
) -> int | None:
    references = {row.last_sample_id for row in rows.values()}
    if references == {None}:
        return None
    if None in references or len(references) != 1:
        raise IngestionError(IngestionRejection.MALFORMED_STATE, trust_lost=True)
    sample_id = next(iter(references))
    if sample_id is None:
        raise IngestionError(IngestionRejection.MALFORMED_STATE, trust_lost=True)
    _read_baseline_sample(
        connection,
        sample_id,
        last_committed_sequence=last_committed_sequence,
    )
    return sample_id


def _projection_changed(
    connection: sqlite3.Connection,
    sample_id: int,
    projection: HealthProjection,
    *,
    last_committed_sequence: int | None,
) -> bool:
    previous_status, previous = _read_baseline_sample(
        connection,
        sample_id,
        last_committed_sequence=last_committed_sequence,
    )
    if previous_status is not projection.overall_status:
        return True

    return any(
        previous[component.component] != (component.status, component.reasons)
        for component in projection.components
    )


def _read_baseline_sample(
    connection: sqlite3.Connection,
    sample_id: int,
    *,
    last_committed_sequence: int | None,
) -> tuple[
    HealthHistoryStatus,
    dict[ComponentName, tuple[HealthHistoryStatus, tuple[NormalizedReason, ...]]],
]:
    sample = connection.execute(
        "SELECT observation_sequence, observed_at_utc_us, recorded_at_utc_us, "
        "overall_status, service_uptime_ms, sample_kind, accepted_sample_kind, "
        "projection_digest, missed_intervals FROM health_samples "
        "WHERE id = ? LIMIT 1",
        (sample_id,),
    ).fetchone()
    try:
        if sample is None:
            raise ValueError("missing_sample")
        if (
            type(sample[0]) is not int
            or not 0 <= sample[0] <= MAX_OBSERVATION_SEQUENCE
            or type(sample[1]) is not int
            or not 0 <= sample[1] <= MAX_TIMESTAMP_US
            or type(sample[2]) is not int
            or not 0 <= sample[2] <= MAX_TIMESTAMP_US
            or type(sample[4]) is not int
            or not 0 <= sample[4] <= MAX_SERVICE_UPTIME_MS
            or type(sample[7]) is not bytes
            or len(sample[7]) != PROJECTION_DIGEST_BYTES
            or type(sample[8]) is not int
            or not 0 <= sample[8] <= MAX_BOUNDED_COUNTER
            or last_committed_sequence is None
            or sample[0] > last_committed_sequence
        ):
            raise ValueError("invalid_sample")
        SampleKind(sample[5])
        accepted_sample_kind = SampleKind(sample[6])
        overall_status = HealthHistoryStatus(sample[3])
    except (TypeError, ValueError):
        raise IngestionError(
            IngestionRejection.MALFORMED_STATE, trust_lost=True
        ) from None
    rows = connection.execute(
        "SELECT component, status, reason_code_1, reason_code_2, reason_code_3, "
        "checked_at_utc_us, latency_ms, last_successful_at_utc_us "
        "FROM component_samples WHERE sample_id = ? LIMIT 5",
        (sample_id,),
    ).fetchall()
    if len(rows) != len(COMPONENT_ORDER):
        raise IngestionError(IngestionRejection.MALFORMED_STATE, trust_lost=True)
    components: dict[ComponentName, ComponentProjection] = {}
    try:
        for row in rows:
            component = ComponentName(row[0])
            status = HealthHistoryStatus(row[1])
            reasons = tuple(
                NormalizedReason(value) for value in row[2:5] if value is not None
            )
            projected = ComponentProjection(
                component=component,
                status=status,
                reasons=reasons,
                checked_at_utc_us=row[5],
                latency_ms=row[6],
                last_successful_at_utc_us=row[7],
            )
            if component in components:
                raise ValueError("duplicate_component")
            components[component] = projected
    except (TypeError, ValueError):
        raise IngestionError(
            IngestionRejection.MALFORMED_STATE, trust_lost=True
        ) from None
    if set(components) != set(COMPONENT_ORDER):
        raise IngestionError(IngestionRejection.MALFORMED_STATE, trust_lost=True)
    try:
        accepted_projection = HealthProjection(
            schema_version=1,
            observation_sequence=sample[0],
            observed_at_utc_us=sample[1],
            recorded_at_utc_us=sample[2],
            overall_status=overall_status,
            service_uptime_ms=sample[4],
            sample_kind=accepted_sample_kind,
            missed_intervals=sample[8],
            components=tuple(components[name] for name in COMPONENT_ORDER),
            digest=sample[7],
        )
        validate_health_projection(accepted_projection)
    except (ProjectionError, TypeError, ValueError):
        raise IngestionError(
            IngestionRejection.MALFORMED_STATE, trust_lost=True
        ) from None
    return overall_status, {
        component.component: (component.status, component.reasons)
        for component in accepted_projection.components
    }


def _plan_automatic_alerts(
    connection: sqlite3.Connection,
    health: dict[AlertScope, HealthEvaluationTransition],
    gap: SamplingGapTransition,
    now_utc_us: int,
    *,
    gap_observation_has_miss: bool,
) -> list[_AlertPlan]:
    plans: list[_AlertPlan] = []
    for scope, transition in health.items():
        if transition.event is HealthEvaluationEvent.CONDITION_CONFIRMED:
            current = transition.state.current_status
            if current not in {
                HealthHistoryStatus.DEGRADED,
                HealthHistoryStatus.UNAVAILABLE,
            }:
                raise IngestionError(
                    IngestionRejection.MALFORMED_STATE, trust_lost=True
                )
            plans.append(
                _plan_health_condition(
                    connection, scope, AlertKind(current.value), now_utc_us
                )
            )
        elif transition.event is HealthEvaluationEvent.RECOVERY_CONFIRMED:
            plans.extend(_plan_health_recovery(connection, scope, now_utc_us))

    gap_condition = gap.event is SamplingGapEvent.OPENED or (
        gap.state.phase is SamplingGapPhase.ACTIVE
        and gap_observation_has_miss
        and gap.event
        in {SamplingGapEvent.MISS_RECORDED, SamplingGapEvent.RECOVERY_RESET}
    )
    if gap_condition:
        plans.append(
            _plan_open_alert(
                connection,
                AlertScope.SAMPLING,
                AlertKind.SAMPLING_GAP,
                now_utc_us,
            )
        )
    elif gap.event is SamplingGapEvent.RECOVERED:
        plans.extend(
            _plan_pair_recovery(
                connection,
                AlertScope.SAMPLING,
                AlertKind.SAMPLING_GAP,
                now_utc_us,
            )
        )
    return plans


def _plan_health_condition(
    connection: sqlite3.Connection,
    scope: AlertScope,
    kind: AlertKind,
    now_utc_us: int,
) -> _AlertPlan:
    if kind is not AlertKind.UNAVAILABLE:
        return _plan_open_alert(connection, scope, kind, now_utc_us)
    unavailable = _current_or_latest_alert(connection, scope, AlertKind.UNAVAILABLE)
    if unavailable is not None:
        return _plan_open_alert(connection, scope, kind, now_utc_us)
    degraded = next(
        (
            stored
            for stored in _active_health_alerts(connection, scope)
            if stored.state.kind is AlertKind.DEGRADED
        ),
        None,
    )
    if degraded is None:
        return _plan_open_alert(connection, scope, kind, now_utc_us)
    transition = evaluate_automatic_alert(
        degraded.state,
        AutomaticAlertInput(
            operation=AutomaticAlertOperation.ESCALATE,
            scope=scope,
            kind=AlertKind.DEGRADED,
            now_utc_us=now_utc_us,
            escalation_kind=AlertKind.UNAVAILABLE,
        ),
    )
    if transition.outcome is AutomaticAlertOutcome.REJECTED:
        raise IngestionError(IngestionRejection.GENERATION_EXHAUSTED)
    if transition.event is not LifecycleEvent.OPENED:
        raise IngestionError(IngestionRejection.MALFORMED_STATE, trust_lost=True)
    return _AlertPlan(degraded.alert_id, transition)


def _active_health_alerts(
    connection: sqlite3.Connection, scope: AlertScope
) -> list[_StoredAlert]:
    rows = connection.execute(
        ACTIVE_HEALTH_ALERTS_SQL,
        (scope.value,),
    ).fetchall()
    if len(rows) > 2:
        raise IngestionError(IngestionRejection.MALFORMED_STATE, trust_lost=True)
    return [_stored_alert(row) for row in rows]


def _plan_open_alert(
    connection: sqlite3.Connection,
    scope: AlertScope,
    kind: AlertKind,
    now_utc_us: int,
) -> _AlertPlan:
    stored = _current_or_latest_alert(connection, scope, kind)
    transition = evaluate_automatic_alert(
        None if stored is None else stored.state,
        AutomaticAlertInput(
            operation=AutomaticAlertOperation.OPEN,
            scope=scope,
            kind=kind,
            now_utc_us=now_utc_us,
        ),
    )
    if transition.outcome is AutomaticAlertOutcome.REJECTED:
        raise IngestionError(IngestionRejection.GENERATION_EXHAUSTED)
    if transition.outcome not in {
        AutomaticAlertOutcome.APPLIED,
        AutomaticAlertOutcome.IDEMPOTENT,
    }:
        raise IngestionError(IngestionRejection.MALFORMED_STATE, trust_lost=True)
    return _AlertPlan(None if stored is None else stored.alert_id, transition)


def _plan_health_recovery(
    connection: sqlite3.Connection, scope: AlertScope, now_utc_us: int
) -> list[_AlertPlan]:
    return [
        _recovery_plan(stored, now_utc_us)
        for stored in _active_health_alerts(connection, scope)
    ]


def _plan_pair_recovery(
    connection: sqlite3.Connection,
    scope: AlertScope,
    kind: AlertKind,
    now_utc_us: int,
) -> list[_AlertPlan]:
    rows = connection.execute(
        ACTIVE_PAIR_ALERT_SQL,
        (scope.value, kind.value),
    ).fetchall()
    if len(rows) > 1:
        raise IngestionError(IngestionRejection.MALFORMED_STATE, trust_lost=True)
    return [] if not rows else [_recovery_plan(_stored_alert(rows[0]), now_utc_us)]


def _recovery_plan(stored: _StoredAlert, now_utc_us: int) -> _AlertPlan:
    transition = evaluate_automatic_alert(
        stored.state,
        AutomaticAlertInput(
            operation=AutomaticAlertOperation.RECOVER,
            scope=stored.state.scope,
            kind=stored.state.kind,
            now_utc_us=now_utc_us,
        ),
    )
    if transition.event is not LifecycleEvent.RECOVERED:
        raise IngestionError(IngestionRejection.MALFORMED_STATE, trust_lost=True)
    return _AlertPlan(stored.alert_id, transition)


def _current_or_latest_alert(
    connection: sqlite3.Connection, scope: AlertScope, kind: AlertKind
) -> _StoredAlert | None:
    active = connection.execute(
        CURRENT_ACTIVE_ALERT_SQL,
        (scope.value, kind.value),
    ).fetchall()
    if len(active) > 1:
        raise IngestionError(IngestionRejection.MALFORMED_STATE, trust_lost=True)
    if active:
        return _stored_alert(active[0])
    terminal = connection.execute(
        LATEST_TERMINAL_ALERT_SQL,
        (scope.value, kind.value),
    ).fetchone()
    return None if terminal is None else _stored_alert(terminal)


def _stored_alert(row: tuple[object, ...]) -> _StoredAlert:
    try:
        if type(row[0]) is not int or row[0] < 1:
            raise ValueError("invalid_alert_id")
        if any(type(row[index]) is not str for index in (1, 2, 3)):
            raise ValueError("invalid_alert_enum")
        if any(type(row[index]) is not int for index in (4, 5, 6)):
            raise ValueError("invalid_alert_counter")
        if row[7] is not None and type(row[7]) is not int:
            raise ValueError("invalid_alert_recovery")
        alert_id = row[0]
        scope = cast(str, row[1])
        kind = cast(str, row[2])
        lifecycle = cast(str, row[3])
        generation = cast(int, row[4])
        occurrence_count = cast(int, row[5])
        cooldown_until = cast(int, row[6])
        recovered_at = row[7]
        state = AutomaticAlertState(
            scope=AlertScope(scope),
            kind=AlertKind(kind),
            lifecycle=AlertLifecycle(lifecycle),
            generation=generation,
            occurrence_count=occurrence_count,
            cooldown_until_utc_us=cooldown_until,
            recovered_at_utc_us=recovered_at,
        )
    except (TypeError, ValueError):
        raise IngestionError(
            IngestionRejection.MALFORMED_STATE, trust_lost=True
        ) from None
    return _StoredAlert(alert_id, state)


def _archive_eligible_alerts(connection: sqlite3.Connection, now_utc_us: int) -> None:
    rows = connection.execute(
        ELIGIBLE_RECOVERED_ALERTS_SQL,
        (now_utc_us, MAX_RELEVANT_ALERTS + 1),
    ).fetchall()
    if len(rows) > MAX_RELEVANT_ALERTS:
        raise IngestionError(IngestionRejection.MALFORMED_STATE, trust_lost=True)
    seen: set[tuple[AlertScope, AlertKind]] = set()
    for row in rows:
        stored = _stored_alert(row)
        key = (stored.state.scope, stored.state.kind)
        if key in seen:
            raise IngestionError(IngestionRejection.MALFORMED_STATE, trust_lost=True)
        seen.add(key)
        transition = evaluate_automatic_alert(
            stored.state,
            AutomaticAlertInput(
                operation=AutomaticAlertOperation.ARCHIVE,
                scope=stored.state.scope,
                kind=stored.state.kind,
                now_utc_us=now_utc_us,
            ),
        )
        _persist_alert_plan(
            connection,
            _AlertPlan(stored.alert_id, transition),
            now_utc_us=now_utc_us,
            supporting_sample_id=None,
        )


def _requires_supporting_sample(plans: list[_AlertPlan]) -> bool:
    return any(
        plan.transition.event in {LifecycleEvent.OPENED, LifecycleEvent.RECOVERED}
        for plan in plans
    )


def _force_transition_storage(decision: _StorageDecision) -> _StorageDecision:
    if decision.stored_kind in {
        SampleKind.TRANSITION,
        SampleKind.STARTUP_GAP,
        SampleKind.CLOCK_DISCONTINUITY,
    }:
        return decision
    return _StorageDecision(IngestionOutcome.TRANSITION_STORED, SampleKind.TRANSITION)


def _insert_health_sample(
    connection: sqlite3.Connection,
    projection: HealthProjection,
    stored_kind: SampleKind,
) -> int:
    cursor = connection.execute(
        "INSERT INTO health_samples("
        "observation_sequence, observed_at_utc_us, recorded_at_utc_us, "
        "overall_status, service_uptime_ms, sample_kind, accepted_sample_kind, "
        "projection_digest, missed_intervals"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            projection.observation_sequence,
            projection.observed_at_utc_us,
            projection.recorded_at_utc_us,
            projection.overall_status.value,
            projection.service_uptime_ms,
            stored_kind.value,
            projection.sample_kind.value,
            projection.digest,
            projection.missed_intervals,
        ),
    )
    if type(cursor.lastrowid) is not int or cursor.lastrowid < 1:
        raise IngestionError(IngestionRejection.PERSISTENCE_FAILED)
    return cursor.lastrowid


def _insert_component_samples(
    connection: sqlite3.Connection,
    sample_id: int,
    components: tuple[ComponentProjection, ...],
) -> None:
    for component in components:
        reasons: tuple[str | None, str | None, str | None] = (
            component.reasons[0].value,
            component.reasons[1].value if len(component.reasons) > 1 else None,
            component.reasons[2].value if len(component.reasons) > 2 else None,
        )
        connection.execute(
            "INSERT INTO component_samples("
            "sample_id, component, status, reason_code_1, reason_code_2, "
            "reason_code_3, checked_at_utc_us, latency_ms, "
            "last_successful_at_utc_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sample_id,
                component.component.value,
                component.status.value,
                *reasons,
                component.checked_at_utc_us,
                component.latency_ms,
                component.last_successful_at_utc_us,
            ),
        )


def _write_evaluation_rows(
    connection: sqlite3.Connection,
    previous: dict[AlertScope, _EvaluationRow],
    health: dict[AlertScope, HealthEvaluationTransition],
    gap: SamplingGapTransition,
    *,
    sample_id: int | None,
    heartbeat_at: int | None,
) -> None:
    for scope, transition in health.items():
        row = previous[scope]
        cursor = connection.execute(
            "UPDATE evaluation_state SET current_status = ?, candidate_status = ?, "
            "consecutive_count = ?, last_sample_id = ?, "
            "last_heartbeat_at_utc_us = ? WHERE scope = ?",
            (
                transition.state.current_status.value
                if transition.state.current_status is not None
                else None,
                transition.state.candidate_status.value
                if transition.state.candidate_status is not None
                else None,
                transition.state.consecutive_count,
                sample_id if sample_id is not None else row.last_sample_id,
                heartbeat_at if heartbeat_at is not None else row.last_heartbeat_at,
                scope.value,
            ),
        )
        if cursor.rowcount != 1:
            raise IngestionError(IngestionRejection.MALFORMED_STATE, trust_lost=True)
    sampling = previous[AlertScope.SAMPLING]
    cursor = connection.execute(
        "UPDATE evaluation_state SET consecutive_count = ?, last_sample_id = ?, "
        "last_heartbeat_at_utc_us = ?, gap_phase = ? WHERE scope = 'sampling'",
        (
            gap.state.largest_missed_interval_report,
            sample_id if sample_id is not None else sampling.last_sample_id,
            heartbeat_at if heartbeat_at is not None else sampling.last_heartbeat_at,
            gap.state.phase.value,
        ),
    )
    if cursor.rowcount != 1:
        raise IngestionError(IngestionRejection.MALFORMED_STATE, trust_lost=True)


def _persist_alert_plan(
    connection: sqlite3.Connection,
    plan: _AlertPlan,
    *,
    now_utc_us: int,
    supporting_sample_id: int | None,
) -> None:
    transition = plan.transition
    if transition.outcome is AutomaticAlertOutcome.IDEMPOTENT:
        return
    if transition.outcome is not AutomaticAlertOutcome.APPLIED:
        raise IngestionError(IngestionRejection.MALFORMED_STATE, trust_lost=True)
    created = transition.created_alert
    if transition.previous is None and transition.state is not None:
        created = transition.state
    if created is not None:
        if supporting_sample_id is None:
            raise IngestionError(IngestionRejection.MALFORMED_STATE, trust_lost=True)
        alert_id = _insert_alert(connection, created, now_utc_us, supporting_sample_id)
        _insert_alert_event(
            connection,
            alert_id,
            LifecycleEvent.OPENED,
            now_utc_us,
            supporting_sample_id,
            AlertLifecycle.OPEN,
        )
        return
    if plan.existing_alert_id is None or transition.state is None:
        raise IngestionError(IngestionRejection.MALFORMED_STATE, trust_lost=True)
    state = transition.state
    if transition.event is LifecycleEvent.OCCURRENCE_UPDATED:
        cursor = connection.execute(
            "UPDATE alerts SET occurrence_count = ?, cooldown_until_utc_us = ?, "
            "latest_sample_id = COALESCE(?, latest_sample_id) WHERE id = ?",
            (
                state.occurrence_count,
                state.cooldown_until_utc_us,
                supporting_sample_id,
                plan.existing_alert_id,
            ),
        )
    elif transition.event is LifecycleEvent.RECOVERED:
        cursor = connection.execute(
            "UPDATE alerts SET lifecycle = 'recovered', recovered_at_utc_us = ?, "
            "cooldown_until_utc_us = ?, latest_sample_id = ? WHERE id = ?",
            (
                state.recovered_at_utc_us,
                state.cooldown_until_utc_us,
                supporting_sample_id,
                plan.existing_alert_id,
            ),
        )
    elif transition.event is LifecycleEvent.ARCHIVED:
        cursor = connection.execute(
            "UPDATE alerts SET lifecycle = 'archived', archived_at_utc_us = ? "
            "WHERE id = ?",
            (now_utc_us, plan.existing_alert_id),
        )
    else:
        raise IngestionError(IngestionRejection.MALFORMED_STATE, trust_lost=True)
    if cursor.rowcount != 1:
        raise IngestionError(IngestionRejection.MALFORMED_STATE, trust_lost=True)
    _insert_alert_event(
        connection,
        plan.existing_alert_id,
        transition.event,
        now_utc_us,
        supporting_sample_id,
        state.lifecycle,
    )


def _insert_alert(
    connection: sqlite3.Connection,
    state: AutomaticAlertState,
    now_utc_us: int,
    sample_id: int,
) -> int:
    severity = (
        HealthHistoryStatus.DEGRADED.value
        if state.kind is AlertKind.DEGRADED
        else HealthHistoryStatus.UNAVAILABLE.value
    )
    cursor = connection.execute(
        "INSERT INTO alerts("
        "scope, kind, lifecycle, severity, opened_at_utc_us, first_sample_id, "
        "latest_sample_id, episode_count, occurrence_count, cooldown_until_utc_us"
        ") VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?, ?)",
        (
            state.scope.value,
            state.kind.value,
            severity,
            now_utc_us,
            sample_id,
            sample_id,
            state.generation,
            state.occurrence_count,
            state.cooldown_until_utc_us,
        ),
    )
    if type(cursor.lastrowid) is not int or cursor.lastrowid < 1:
        raise IngestionError(IngestionRejection.PERSISTENCE_FAILED)
    return cursor.lastrowid


def _insert_alert_event(
    connection: sqlite3.Connection,
    alert_id: int,
    event: LifecycleEvent,
    now_utc_us: int,
    sample_id: int | None,
    lifecycle: AlertLifecycle,
) -> None:
    cursor = connection.execute(
        "INSERT INTO alert_events("
        "alert_id, event_type, event_at_utc_us, supporting_sample_id, "
        "resulting_lifecycle) VALUES (?, ?, ?, ?, ?)",
        (alert_id, event.value, now_utc_us, sample_id, lifecycle.value),
    )
    if cursor.rowcount != 1:
        raise IngestionError(IngestionRejection.PERSISTENCE_FAILED)


def _write_checkpoint_and_replay_ledger(
    connection: sqlite3.Connection,
    previous: _Checkpoint,
    projection: HealthProjection,
) -> None:
    if (
        previous.sequence is not None
        and projection.observation_sequence <= previous.sequence
    ):
        raise IngestionError(IngestionRejection.MALFORMED_STATE, trust_lost=True)
    connection.execute(
        "INSERT INTO accepted_observation_replay("
        "observation_sequence, observed_at_utc_us, projection_digest, "
        "accepted_sample_kind) VALUES (?, ?, ?, ?)",
        (
            projection.observation_sequence,
            projection.observed_at_utc_us,
            projection.digest,
            projection.sample_kind.value,
        ),
    )
    connection.execute(
        "DELETE FROM accepted_observation_replay "
        "WHERE observation_sequence < ("
        "SELECT observation_sequence FROM accepted_observation_replay "
        "ORDER BY observation_sequence DESC LIMIT 1 OFFSET ?)",
        (REPLAY_LEDGER_CAPACITY - 1,),
    )
    accepted_count = min(previous.accepted_count + 1, MAX_BOUNDED_COUNTER)
    cursor = connection.execute(
        "UPDATE ingestion_checkpoint SET last_committed_sequence = ?, "
        "last_accepted_observed_at_utc_us = ?, "
        "last_accepted_projection_digest = ?, last_accepted_sample_kind = ?, "
        "accepted_observation_count = ? WHERE singleton_id = 1 "
        "AND last_committed_sequence IS ? "
        "AND accepted_observation_count = ?",
        (
            projection.observation_sequence,
            projection.observed_at_utc_us,
            projection.digest,
            projection.sample_kind.value,
            accepted_count,
            previous.sequence,
            previous.accepted_count,
        ),
    )
    if cursor.rowcount != 1:
        raise IngestionError(IngestionRejection.MALFORMED_STATE, trust_lost=True)


def _classified_sqlite_error(error: sqlite3.Error) -> IngestionError:
    raw_code = getattr(error, "sqlite_errorcode", None)
    primary_code = raw_code & 0xFF if type(raw_code) is int else None
    if primary_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        return IngestionError(IngestionRejection.STORAGE_BUSY)
    if isinstance(error, sqlite3.IntegrityError) or primary_code in {
        sqlite3.SQLITE_CORRUPT,
        sqlite3.SQLITE_NOTADB,
        sqlite3.SQLITE_SCHEMA,
        sqlite3.SQLITE_CONSTRAINT,
    }:
        return IngestionError(IngestionRejection.TRUST_FAILED, trust_lost=True)
    return IngestionError(IngestionRejection.PERSISTENCE_FAILED)


def _rollback_after_failure(connection: sqlite3.Connection) -> None:
    try:
        _rollback_transaction(connection)
    except sqlite3.Error:
        raise IngestionError(IngestionRejection.TRUST_FAILED, trust_lost=True) from None


def _rollback_transaction(connection: sqlite3.Connection) -> None:
    connection.rollback()


def _fault(stage: IngestionStage) -> None:
    """No-op injection seam used only by synthetic rollback tests."""
    del stage
