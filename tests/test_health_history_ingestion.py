"""Synthetic tests for atomic Milestone 18 ingestion and evaluator state."""

from __future__ import annotations

import hashlib
import inspect
import socket
import sqlite3
import subprocess
from dataclasses import replace
from itertools import product
from pathlib import Path
from typing import Any

import pytest

import aurora_core.health_history.store as store_module
from aurora_core.health_history import ingestion, schema
from aurora_core.health_history.evaluation import (
    HealthEvaluationEvent,
    HealthEvaluationInput,
    HealthEvaluationState,
    evaluate_health_state,
)
from aurora_core.health_history.filesystem import (
    FilesystemBoundaryError,
    FilesystemRejection,
)
from aurora_core.health_history.ingestion import (
    HEARTBEAT_INTERVAL_US,
    IngestionError,
    IngestionOutcome,
    IngestionRejection,
    IngestionStage,
)
from aurora_core.health_history.lifecycle import (
    AutomaticAlertInput,
    AutomaticAlertOperation,
    AutomaticAlertState,
    evaluate_automatic_alert,
)
from aurora_core.health_history.models import (
    COMPONENT_ORDER,
    MAX_BOUNDED_COUNTER,
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
    _canonical_bytes,
)
from aurora_core.health_history.reasons import NormalizedReason
from aurora_core.health_history.sampling_gap import (
    SamplingGapObservation,
    SamplingGapState,
    evaluate_sampling_gap,
)
from aurora_core.health_history.store import HealthHistoryStore
from aurora_core.m18_validation import alert_lifecycle as reference_alerts
from aurora_core.m18_validation import sampling_gap as reference_gap

_BASE_TIME = 1_786_000_000_000_000


@pytest.fixture(autouse=True)
def _block_external_operations(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("external operation is prohibited")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(subprocess, "Popen", blocked)


@pytest.fixture
def store_path(tmp_path: Path) -> tuple[Path, HealthHistoryStore]:
    tmp_path.chmod(0o700)
    path = tmp_path / "history.db"
    store = HealthHistoryStore.create(path, created_at_utc_us=1)
    try:
        yield path, store
    finally:
        store.close()


def _default_reason(
    component: ComponentName, status: HealthHistoryStatus
) -> NormalizedReason:
    return {
        (
            ComponentName.WLED,
            HealthHistoryStatus.HEALTHY,
        ): NormalizedReason.WLED_HEALTHY,
        (
            ComponentName.WLED,
            HealthHistoryStatus.DEGRADED,
        ): NormalizedReason.WLED_INFO_LED_COUNT_MISMATCH,
        (
            ComponentName.WLED,
            HealthHistoryStatus.UNAVAILABLE,
        ): NormalizedReason.WLED_COLLECTOR_FAILED,
        (
            ComponentName.HYPERHDR,
            HealthHistoryStatus.HEALTHY,
        ): NormalizedReason.HYPERHDR_HEALTHY,
        (
            ComponentName.HYPERHDR,
            HealthHistoryStatus.DEGRADED,
        ): NormalizedReason.HYPERHDR_VIDEO_GRABBER_INACTIVE,
        (
            ComponentName.HYPERHDR,
            HealthHistoryStatus.UNAVAILABLE,
        ): NormalizedReason.HYPERHDR_COLLECTOR_FAILED,
        (
            ComponentName.CAPTURE,
            HealthHistoryStatus.HEALTHY,
        ): NormalizedReason.CAPTURE_HEALTHY,
        (
            ComponentName.CAPTURE,
            HealthHistoryStatus.DEGRADED,
        ): NormalizedReason.CAPTURE_GRABBER_INACTIVE,
        (
            ComponentName.CAPTURE,
            HealthHistoryStatus.UNAVAILABLE,
        ): NormalizedReason.CAPTURE_COLLECTOR_FAILED,
        (
            ComponentName.RASPBERRY_PI,
            HealthHistoryStatus.HEALTHY,
        ): NormalizedReason.RASPBERRY_PI_HEALTHY,
        (
            ComponentName.RASPBERRY_PI,
            HealthHistoryStatus.DEGRADED,
        ): NormalizedReason.RASPBERRY_PI_DEGRADED,
        (
            ComponentName.RASPBERRY_PI,
            HealthHistoryStatus.UNAVAILABLE,
        ): NormalizedReason.RASPBERRY_PI_UNAVAILABLE,
    }[(component, status)]


def _projection(
    sequence: int,
    *,
    recorded_at: int | None = None,
    observed_at: int | None = None,
    statuses: dict[ComponentName, HealthHistoryStatus] | None = None,
    reasons: dict[ComponentName, tuple[NormalizedReason, ...]] | None = None,
    sample_kind: SampleKind = SampleKind.HEARTBEAT,
    missed_intervals: int = 0,
) -> HealthProjection:
    observed = observed_at or _BASE_TIME + sequence * 30_000_000
    recorded = recorded_at if recorded_at is not None else observed + 1_000_000
    selected_statuses = {
        component: HealthHistoryStatus.HEALTHY for component in COMPONENT_ORDER
    }
    selected_statuses.update(statuses or {})
    components = tuple(
        ComponentProjection(
            component=component,
            status=selected_statuses[component],
            reasons=(reasons or {}).get(
                component, (_default_reason(component, selected_statuses[component]),)
            ),
            checked_at_utc_us=observed,
            latency_ms=1,
            last_successful_at_utc_us=(
                observed
                if selected_statuses[component] is not HealthHistoryStatus.UNAVAILABLE
                else None
            ),
        )
        for component in COMPONENT_ORDER
    )
    ordering = {
        HealthHistoryStatus.HEALTHY: 0,
        HealthHistoryStatus.DEGRADED: 1,
        HealthHistoryStatus.UNAVAILABLE: 2,
    }
    overall = max((item.status for item in components), key=ordering.__getitem__)
    digest = hashlib.sha256(
        _canonical_bytes(
            observed_at=observed,
            status=overall,
            uptime=1_000,
            sample_kind=sample_kind,
            missed_intervals=missed_intervals,
            components=components,
        )
    ).digest()
    return HealthProjection(
        schema_version=1,
        observed_at_utc_us=observed,
        recorded_at_utc_us=recorded,
        overall_status=overall,
        service_uptime_ms=1_000,
        sample_kind=sample_kind,
        missed_intervals=missed_intervals,
        components=components,
        digest=digest,
    )


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{path.absolute().as_uri()}?mode=rw", uri=True, isolation_level=None
    )
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _rows(path: Path, sql: str, parameters: tuple[object, ...] = ()) -> list[Any]:
    connection = _connect(path)
    try:
        return connection.execute(sql, parameters).fetchall()
    finally:
        connection.close()


def _table_snapshot(path: Path) -> dict[str, list[Any]]:
    return {
        table: _rows(path, f"SELECT * FROM {table} ORDER BY rowid")
        for table in (
            "ingestion_checkpoint",
            "health_samples",
            "component_samples",
            "evaluation_state",
            "alerts",
            "alert_events",
        )
    }


def _alert_rows(path: Path) -> list[Any]:
    return _rows(
        path,
        "SELECT id, scope, kind, lifecycle, episode_count, occurrence_count "
        "FROM alerts ORDER BY id",
    )


def test_schema_v1_has_one_global_checkpoint_and_no_singular_alert_reference(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, _store = store_path
    assert _rows(path, "SELECT * FROM ingestion_checkpoint") == [
        (1, None, None, None, 0)
    ]
    columns = {row[1] for row in _rows(path, "PRAGMA table_info(evaluation_state)")}
    assert "current_alert_id" not in columns
    assert _rows(path, "SELECT COUNT(*) FROM evaluation_state") == [(6,)]


def test_checkpoint_cardinality_is_verified(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    store.close()
    connection = _connect(path)
    connection.execute("DELETE FROM ingestion_checkpoint")
    with pytest.raises(schema.SchemaVerificationError) as caught:
        schema.verify_schema_v1(connection)
    assert caught.value.reason == "ingestion_checkpoint_mismatch"
    connection.close()


def test_checkpoint_constraints_require_one_complete_bounded_identity(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    store.close()
    connection = _connect(path)
    invalid_updates = (
        "last_accepted_observed_at_utc_us = 1",
        "last_accepted_projection_digest = x'00'",
        "last_accepted_sample_kind = 'unknown'",
        f"accepted_observation_count = {MAX_BOUNDED_COUNTER + 1}",
    )
    for assignment in invalid_updates:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                f"UPDATE ingestion_checkpoint SET {assignment} WHERE singleton_id = 1"
            )
        connection.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO ingestion_checkpoint(singleton_id) VALUES (2)")
    connection.close()


def test_first_observation_is_transition_with_four_components_and_checkpoint(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    projection = _projection(1)
    result = store.ingest(projection)
    assert result.outcome is IngestionOutcome.TRANSITION_STORED
    assert _rows(path, "SELECT sample_kind FROM health_samples") == [("transition",)]
    assert _rows(path, "SELECT COUNT(*) FROM component_samples") == [(4,)]
    checkpoint = _rows(
        path,
        "SELECT last_accepted_observed_at_utc_us, "
        "last_accepted_projection_digest, last_accepted_sample_kind, "
        "accepted_observation_count FROM ingestion_checkpoint",
    )[0]
    assert checkpoint == (
        projection.observed_at_utc_us,
        projection.digest,
        projection.sample_kind.value,
        1,
    )


def test_replay_after_stored_and_state_only_observations_mutates_nothing(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    stored = _projection(1)
    assert store.ingest(stored).outcome is IngestionOutcome.TRANSITION_STORED
    before = _table_snapshot(path)
    assert (
        store.ingest(
            replace(stored, recorded_at_utc_us=stored.recorded_at_utc_us + 7)
        ).outcome
        is IngestionOutcome.REPLAYED
    )
    assert _table_snapshot(path) == before

    compacted = _projection(2)
    assert store.ingest(compacted).outcome is IngestionOutcome.STATE_ONLY
    before = _table_snapshot(path)
    assert store.ingest(compacted).outcome is IngestionOutcome.REPLAYED
    assert _table_snapshot(path) == before


def test_replay_defense_recognizes_an_older_stored_observation(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    first = _projection(1)
    store.ingest(first)
    changed = _projection(
        2,
        statuses={ComponentName.WLED: HealthHistoryStatus.DEGRADED},
    )
    store.ingest(changed)
    before = _table_snapshot(path)
    assert store.ingest(first).outcome is IngestionOutcome.REPLAYED
    assert _table_snapshot(path) == before


def test_same_observed_time_with_distinct_scheduler_evidence_is_not_replay(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    observed = _BASE_TIME
    first = _projection(1, observed_at=observed)
    delayed = _projection(2, observed_at=observed, missed_intervals=1)
    assert first.digest != delayed.digest
    assert store.ingest(first).outcome is IngestionOutcome.TRANSITION_STORED
    assert store.ingest(delayed).outcome is IngestionOutcome.STATE_ONLY
    assert _rows(
        path,
        "SELECT last_accepted_projection_digest FROM ingestion_checkpoint",
    ) == [(delayed.digest,)]
    before = _table_snapshot(path)
    assert store.ingest(delayed).outcome is IngestionOutcome.REPLAYED
    assert _table_snapshot(path) == before


def test_unchanged_compaction_and_exact_heartbeat_boundary(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    first = _projection(1, recorded_at=_BASE_TIME)
    store.ingest(first)
    just_before = _projection(2, recorded_at=_BASE_TIME + HEARTBEAT_INTERVAL_US - 1)
    assert store.ingest(just_before).outcome is IngestionOutcome.STATE_ONLY
    assert _rows(path, "SELECT COUNT(*) FROM component_samples") == [(4,)]
    at_boundary = _projection(3, recorded_at=_BASE_TIME + HEARTBEAT_INTERVAL_US)
    assert store.ingest(at_boundary).outcome is IngestionOutcome.HEARTBEAT_STORED
    assert _rows(path, "SELECT sample_kind FROM health_samples ORDER BY id") == [
        ("transition",),
        ("heartbeat",),
    ]
    assert _rows(path, "SELECT COUNT(*) FROM component_samples") == [(8,)]


def test_replay_after_stored_heartbeat_mutates_nothing(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    store.ingest(_projection(1, recorded_at=_BASE_TIME))
    heartbeat = _projection(2, recorded_at=_BASE_TIME + HEARTBEAT_INTERVAL_US)
    assert store.ingest(heartbeat).outcome is IngestionOutcome.HEARTBEAT_STORED
    before = _table_snapshot(path)
    assert store.ingest(heartbeat).outcome is IngestionOutcome.REPLAYED
    assert _table_snapshot(path) == before


def test_clock_marker_accepts_backward_utc_observation_without_false_replay(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    first = _projection(1)
    store.ingest(first)
    marker = _projection(
        2,
        observed_at=first.observed_at_utc_us - 10_000_000,
        sample_kind=SampleKind.CLOCK_DISCONTINUITY,
    )
    assert store.ingest(marker).outcome is IngestionOutcome.CLOCK_MARKER_STORED
    assert _rows(path, "SELECT sample_kind FROM health_samples ORDER BY id") == [
        ("transition",),
        ("clock_discontinuity",),
    ]


@pytest.mark.parametrize(
    ("kind", "outcome"),
    [
        (SampleKind.STARTUP_GAP, IngestionOutcome.STARTUP_MARKER_STORED),
        (
            SampleKind.CLOCK_DISCONTINUITY,
            IngestionOutcome.CLOCK_MARKER_STORED,
        ),
    ],
)
def test_fixed_markers_are_always_stored(
    store_path: tuple[Path, HealthHistoryStore],
    kind: SampleKind,
    outcome: IngestionOutcome,
) -> None:
    path, store = store_path
    store.ingest(_projection(1))
    result = store.ingest(_projection(2, sample_kind=kind))
    assert result.outcome is outcome
    assert _rows(path, "SELECT sample_kind FROM health_samples ORDER BY id")[-1] == (
        kind.value,
    )


@pytest.mark.parametrize(
    ("kind", "outcome"),
    [
        (SampleKind.STARTUP_GAP, IngestionOutcome.STARTUP_MARKER_STORED),
        (
            SampleKind.CLOCK_DISCONTINUITY,
            IngestionOutcome.CLOCK_MARKER_STORED,
        ),
    ],
)
def test_first_accepted_marker_retains_its_exact_kind(
    store_path: tuple[Path, HealthHistoryStore],
    kind: SampleKind,
    outcome: IngestionOutcome,
) -> None:
    path, store = store_path
    assert store.ingest(_projection(1, sample_kind=kind)).outcome is outcome
    assert _rows(path, "SELECT sample_kind FROM health_samples") == [(kind.value,)]


def test_status_and_reason_only_changes_are_transitions(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    store.ingest(_projection(1))
    degraded = _projection(
        2,
        statuses={ComponentName.WLED: HealthHistoryStatus.DEGRADED},
    )
    assert store.ingest(degraded).outcome is IngestionOutcome.TRANSITION_STORED
    reason_change = _projection(
        3,
        statuses={ComponentName.WLED: HealthHistoryStatus.DEGRADED},
        reasons={ComponentName.WLED: (NormalizedReason.WLED_INFO_HTTP_ERROR,)},
    )
    assert store.ingest(reason_change).outcome is IngestionOutcome.TRANSITION_STORED
    assert _rows(path, "SELECT COUNT(*) FROM health_samples") == [(3,)]


@pytest.mark.parametrize(
    ("status", "threshold", "kind"),
    [
        (HealthHistoryStatus.DEGRADED, 3, AlertKind.DEGRADED),
        (HealthHistoryStatus.UNAVAILABLE, 2, AlertKind.UNAVAILABLE),
    ],
)
def test_health_thresholds_open_overall_and_component_alerts(
    store_path: tuple[Path, HealthHistoryStore],
    status: HealthHistoryStatus,
    threshold: int,
    kind: AlertKind,
) -> None:
    path, store = store_path
    for sequence in range(1, threshold + 1):
        result = store.ingest(
            _projection(sequence, statuses={ComponentName.WLED: status})
        )
    assert result.outcome is IngestionOutcome.TRANSITION_STORED
    alerts = _alert_rows(path)
    assert [(row[1], row[2], row[3]) for row in alerts] == [
        ("overall", kind.value, "open"),
        ("wled", kind.value, "open"),
    ]
    assert _rows(path, "SELECT event_type FROM alert_events ORDER BY id") == [
        ("opened",),
        ("opened",),
    ]


@pytest.mark.parametrize("component", tuple(COMPONENT_ORDER))
def test_every_fixed_component_scope_is_evaluated(
    store_path: tuple[Path, HealthHistoryStore], component: ComponentName
) -> None:
    path, store = store_path
    for sequence in range(1, 4):
        store.ingest(
            _projection(
                sequence,
                statuses={component: HealthHistoryStatus.DEGRADED},
            )
        )
    assert _rows(
        path,
        "SELECT lifecycle FROM alerts WHERE scope = ? AND kind = 'degraded'",
        (component.value,),
    ) == [("open",)]


def test_candidate_switching_is_deterministic(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    sequence = (
        HealthHistoryStatus.DEGRADED,
        HealthHistoryStatus.DEGRADED,
        HealthHistoryStatus.UNAVAILABLE,
        HealthHistoryStatus.DEGRADED,
    )
    expected = (("degraded", 1), ("degraded", 2), ("unavailable", 1), ("degraded", 1))
    for index, (status, state) in enumerate(zip(sequence, expected, strict=True), 1):
        store.ingest(_projection(index, statuses={ComponentName.WLED: status}))
        assert _rows(
            path,
            "SELECT candidate_status, consecutive_count FROM evaluation_state "
            "WHERE scope = 'wled'",
        ) == [state]


@pytest.mark.parametrize(
    ("component", "reason", "expected_alerts"),
    [
        (ComponentName.WLED, NormalizedReason.WLED_DISABLED, 0),
        (ComponentName.HYPERHDR, NormalizedReason.HYPERHDR_DISABLED, 0),
        (ComponentName.CAPTURE, NormalizedReason.CAPTURE_DISABLED, 0),
        (ComponentName.WLED, NormalizedReason.WLED_COLLECTOR_FAILED, 2),
    ],
)
def test_only_exact_intentional_disable_is_suppressed(
    store_path: tuple[Path, HealthHistoryStore],
    component: ComponentName,
    reason: NormalizedReason,
    expected_alerts: int,
) -> None:
    path, store = store_path
    for sequence in range(1, 4):
        store.ingest(
            _projection(
                sequence,
                statuses={component: HealthHistoryStatus.UNAVAILABLE},
                reasons={component: (reason,)},
            )
        )
    assert _rows(path, "SELECT COUNT(*) FROM alerts") == [(expected_alerts,)]


def test_degraded_to_unavailable_escalation_preserves_distinct_active_alerts(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    for sequence in range(1, 4):
        store.ingest(
            _projection(
                sequence,
                statuses={ComponentName.WLED: HealthHistoryStatus.DEGRADED},
            )
        )
    for sequence in range(4, 6):
        store.ingest(
            _projection(
                sequence,
                statuses={ComponentName.WLED: HealthHistoryStatus.UNAVAILABLE},
            )
        )
    active = _rows(
        path,
        "SELECT scope, kind, lifecycle FROM alerts WHERE scope = 'wled' ORDER BY id",
    )
    assert active == [
        ("wled", "degraded", "open"),
        ("wled", "unavailable", "open"),
    ]


def test_matching_condition_updates_occurrence_without_replay_inflation(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    projections = [
        _projection(
            sequence,
            statuses={ComponentName.WLED: HealthHistoryStatus.DEGRADED},
        )
        for sequence in range(1, 5)
    ]
    for projection in projections:
        store.ingest(projection)
    before = _table_snapshot(path)
    assert _rows(
        path,
        "SELECT occurrence_count FROM alerts WHERE scope = 'wled'",
    ) == [(2,)]
    assert store.ingest(projections[-1]).outcome is IngestionOutcome.REPLAYED
    assert _table_snapshot(path) == before


def test_two_healthy_samples_recover_all_active_health_alerts_for_scope(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    for sequence in range(1, 4):
        store.ingest(
            _projection(
                sequence,
                statuses={ComponentName.WLED: HealthHistoryStatus.DEGRADED},
            )
        )
    for sequence in range(4, 6):
        store.ingest(
            _projection(
                sequence,
                statuses={ComponentName.WLED: HealthHistoryStatus.UNAVAILABLE},
            )
        )
    store.ingest(_projection(6))
    assert _rows(
        path, "SELECT lifecycle FROM alerts WHERE scope = 'wled' ORDER BY id"
    ) == [("open",), ("open",)]
    result = store.ingest(_projection(7))
    assert result.outcome is IngestionOutcome.TRANSITION_STORED
    assert _rows(
        path, "SELECT lifecycle FROM alerts WHERE scope = 'wled' ORDER BY id"
    ) == [("recovered",), ("recovered",)]
    assert _rows(
        path,
        "SELECT COUNT(*) FROM alert_events WHERE event_type = 'recovered'",
    ) == [(4,)]


def test_replay_after_alert_opening_and_recovery_is_fully_idempotent(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    first = _projection(
        1, statuses={ComponentName.WLED: HealthHistoryStatus.UNAVAILABLE}
    )
    opening = _projection(
        2, statuses={ComponentName.WLED: HealthHistoryStatus.UNAVAILABLE}
    )
    store.ingest(first)
    store.ingest(opening)
    before_open_replay = _table_snapshot(path)
    assert store.ingest(opening).outcome is IngestionOutcome.REPLAYED
    assert _table_snapshot(path) == before_open_replay
    store.ingest(_projection(3))
    recovery = _projection(4)
    store.ingest(recovery)
    before_recovery_replay = _table_snapshot(path)
    assert store.ingest(recovery).outcome is IngestionOutcome.REPLAYED
    assert _table_snapshot(path) == before_recovery_replay


def test_acknowledged_alert_is_preserved_by_occurrence_and_automatically_recovered(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    for sequence in range(1, 4):
        store.ingest(
            _projection(
                sequence,
                statuses={ComponentName.WLED: HealthHistoryStatus.DEGRADED},
            )
        )
    connection = _connect(path)
    connection.execute(
        "UPDATE alerts SET lifecycle = 'acknowledged', acknowledged_at_utc_us = 1 "
        "WHERE scope = 'wled'"
    )
    connection.close()
    store.ingest(
        _projection(4, statuses={ComponentName.WLED: HealthHistoryStatus.DEGRADED})
    )
    assert _rows(
        path,
        "SELECT lifecycle, occurrence_count FROM alerts WHERE scope = 'wled'",
    ) == [("acknowledged", 2)]
    store.ingest(_projection(5))
    store.ingest(_projection(6))
    assert _rows(path, "SELECT lifecycle FROM alerts WHERE scope = 'wled'") == [
        ("recovered",)
    ]
    assert _rows(
        path,
        "SELECT COUNT(*) FROM alert_events WHERE event_type = 'acknowledged'",
    ) == [(0,)]


def test_sampling_gap_open_recovery_and_marker_rules(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    store.ingest(_projection(1))
    store.ingest(_projection(2, missed_intervals=1))
    assert _rows(
        path, "SELECT gap_phase FROM evaluation_state WHERE scope = 'sampling'"
    ) == [("candidate_one",)]
    opened = store.ingest(_projection(3, missed_intervals=2))
    assert opened.outcome is IngestionOutcome.TRANSITION_STORED
    assert _rows(
        path,
        "SELECT lifecycle FROM alerts WHERE scope = 'sampling'",
    ) == [("open",)]
    store.ingest(_projection(4, sample_kind=SampleKind.STARTUP_GAP))
    assert _rows(
        path, "SELECT gap_phase FROM evaluation_state WHERE scope = 'sampling'"
    ) == [("active",)]
    store.ingest(_projection(5))
    assert _rows(
        path, "SELECT gap_phase FROM evaluation_state WHERE scope = 'sampling'"
    ) == [("recovery_one",)]
    store.ingest(_projection(6, sample_kind=SampleKind.CLOCK_DISCONTINUITY))
    assert _rows(
        path, "SELECT gap_phase FROM evaluation_state WHERE scope = 'sampling'"
    ) == [("active",)]
    store.ingest(_projection(7))
    recovered = store.ingest(_projection(8))
    assert recovered.outcome is IngestionOutcome.TRANSITION_STORED
    assert _rows(path, "SELECT lifecycle FROM alerts WHERE scope = 'sampling'") == [
        ("recovered",)
    ]


def test_sampling_gap_recovery_requires_on_time_healthy_observations(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    store.ingest(_projection(1))
    store.ingest(_projection(2, missed_intervals=2))
    store.ingest(
        _projection(3, statuses={ComponentName.WLED: HealthHistoryStatus.DEGRADED})
    )
    assert _rows(
        path, "SELECT gap_phase FROM evaluation_state WHERE scope = 'sampling'"
    ) == [("active",)]
    store.ingest(_projection(4))
    store.ingest(_projection(5))
    assert _rows(path, "SELECT lifecycle FROM alerts WHERE scope = 'sampling'") == [
        ("recovered",)
    ]


def test_recovered_alert_cooldown_then_archive_and_distinct_later_episode(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    for sequence in range(1, 4):
        store.ingest(
            _projection(
                sequence,
                statuses={ComponentName.WLED: HealthHistoryStatus.DEGRADED},
            )
        )
    store.ingest(_projection(4))
    recovered = _projection(5)
    store.ingest(recovered)
    archive_time = recovered.recorded_at_utc_us + 15 * 60 * 1_000_000
    store.ingest(_projection(6, recorded_at=archive_time))
    assert _rows(path, "SELECT lifecycle FROM alerts WHERE scope = 'wled'") == [
        ("archived",)
    ]
    for sequence in range(7, 10):
        store.ingest(
            _projection(
                sequence,
                recorded_at=archive_time + sequence * 30_000_000,
                statuses={ComponentName.WLED: HealthHistoryStatus.DEGRADED},
            )
        )
    assert _rows(
        path,
        "SELECT lifecycle, episode_count FROM alerts WHERE scope = 'wled' ORDER BY id",
    ) == [("archived", 1), ("open", 2)]


def test_matching_recurrence_during_cooldown_updates_recovered_record(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    for sequence in range(1, 4):
        store.ingest(
            _projection(
                sequence,
                statuses={ComponentName.WLED: HealthHistoryStatus.DEGRADED},
            )
        )
    store.ingest(_projection(4))
    store.ingest(_projection(5))
    for sequence in range(6, 9):
        store.ingest(
            _projection(
                sequence,
                statuses={ComponentName.WLED: HealthHistoryStatus.DEGRADED},
            )
        )
    assert _rows(
        path,
        "SELECT lifecycle, occurrence_count FROM alerts WHERE scope = 'wled'",
    ) == [("recovered", 2)]


def test_archival_uses_only_fixed_events_and_no_expired_or_rejected_row(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    for sequence in range(1, 3):
        store.ingest(
            _projection(
                sequence,
                statuses={ComponentName.WLED: HealthHistoryStatus.UNAVAILABLE},
            )
        )
    store.ingest(_projection(3))
    recovered = _projection(4)
    store.ingest(recovered)
    store.ingest(
        _projection(
            5,
            recorded_at=recovered.recorded_at_utc_us + 15 * 60 * 1_000_000,
        )
    )
    events = {row[0] for row in _rows(path, "SELECT event_type FROM alert_events")}
    assert events <= {event.value for event in LifecycleEvent}
    assert "archived" in events
    assert "expired" not in events
    assert "rejected_transition" not in events


@pytest.mark.parametrize("stage", tuple(IngestionStage))
def test_fault_after_each_mutation_stage_rolls_back_every_table(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
    stage: IngestionStage,
) -> None:
    path, store = store_path
    before = _table_snapshot(path)

    def fail(current: IngestionStage) -> None:
        if current is stage:
            raise sqlite3.OperationalError("synthetic private detail")

    monkeypatch.setattr(ingestion, "_fault", fail)
    with pytest.raises(IngestionError) as caught:
        store.ingest(_projection(1))
    assert caught.value.reason is IngestionRejection.PERSISTENCE_FAILED
    assert str(caught.value) == "persistence_failed"
    assert _table_snapshot(path) == before


def test_fault_after_alert_mutation_rolls_back_open_and_checkpoint(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, store = store_path
    store.ingest(
        _projection(1, statuses={ComponentName.WLED: HealthHistoryStatus.UNAVAILABLE})
    )
    before = _table_snapshot(path)

    def fail(stage: IngestionStage) -> None:
        if stage is IngestionStage.ALERTS:
            raise sqlite3.OperationalError("must not escape")

    monkeypatch.setattr(ingestion, "_fault", fail)
    with pytest.raises(IngestionError):
        store.ingest(
            _projection(
                2, statuses={ComponentName.WLED: HealthHistoryStatus.UNAVAILABLE}
            )
        )
    assert _table_snapshot(path) == before


def test_fault_after_archive_mutation_rolls_back_terminal_transition(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, store = store_path
    for sequence in range(1, 3):
        store.ingest(
            _projection(
                sequence,
                statuses={ComponentName.WLED: HealthHistoryStatus.UNAVAILABLE},
            )
        )
    store.ingest(_projection(3))
    recovered = _projection(4)
    store.ingest(recovered)
    before = _table_snapshot(path)

    def fail(stage: IngestionStage) -> None:
        if stage is IngestionStage.ARCHIVAL:
            raise sqlite3.OperationalError("must not escape")

    monkeypatch.setattr(ingestion, "_fault", fail)
    with pytest.raises(IngestionError):
        store.ingest(
            _projection(
                5,
                recorded_at=recovered.recorded_at_utc_us + 15 * 60 * 1_000_000,
            )
        )
    assert _table_snapshot(path) == before


def test_busy_database_fails_without_retry_or_checkpoint_change(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    before = _table_snapshot(path)
    locker = _connect(path)
    locker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(IngestionError) as caught:
            store.ingest(_projection(1))
        assert caught.value.reason is IngestionRejection.STORAGE_BUSY
    finally:
        locker.rollback()
        locker.close()
    assert _table_snapshot(path) == before


def test_malformed_persisted_evaluator_state_fails_closed_and_closes_store(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    connection = _connect(path)
    connection.execute(
        "UPDATE evaluation_state SET consecutive_count = 1 WHERE scope = 'wled'"
    )
    connection.close()
    with pytest.raises(IngestionError) as caught:
        store.ingest(_projection(1))
    assert caught.value.reason is IngestionRejection.MALFORMED_STATE
    assert caught.value.__cause__ is None
    assert store.closed


@pytest.mark.parametrize("failure_call", [1, 2])
def test_main_identity_rejection_before_or_after_write_is_sanitized(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
    failure_call: int,
) -> None:
    _path, store = store_path
    original = store_module.validate_database_file
    calls = 0

    def changed(candidate: Path, *, expected: object | None = None) -> Any:
        nonlocal calls
        calls += 1
        if calls == failure_call:
            raise FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED)
        return original(candidate, expected=expected)  # type: ignore[arg-type]

    monkeypatch.setattr(store_module, "validate_database_file", changed)
    with pytest.raises(IngestionError) as caught:
        store.ingest(_projection(1))
    assert caught.value.reason is IngestionRejection.TRUST_FAILED
    assert caught.value.__cause__ is None
    assert store.closed


@pytest.mark.parametrize("failure_call", [1, 2, 3])
def test_sidecar_identity_rejection_before_and_after_write_closes_store(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
    failure_call: int,
) -> None:
    _path, store = store_path
    original = store_module._advance_sidecar_snapshot
    calls = 0

    def changed(candidate: Path, before: object) -> Any:
        nonlocal calls
        calls += 1
        if calls == failure_call:
            raise FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED)
        return original(candidate, before)  # type: ignore[arg-type]

    monkeypatch.setattr(store_module, "_advance_sidecar_snapshot", changed)
    with pytest.raises(IngestionError) as caught:
        store.ingest(_projection(1))
    assert caught.value.reason is IngestionRejection.TRUST_FAILED
    assert store.closed


def test_invalid_projection_is_rejected_before_database_mutation(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    forged = replace(_projection(1), digest=b"x" * 32)
    before = _table_snapshot(path)
    with pytest.raises(IngestionError) as caught:
        store.ingest(forged)
    assert caught.value.reason is IngestionRejection.INVALID_PROJECTION
    assert _table_snapshot(path) == before


def test_accepted_checkpoint_counter_saturates_without_blocking_ingestion(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    store.ingest(_projection(1))
    connection = _connect(path)
    connection.execute(
        "UPDATE ingestion_checkpoint SET accepted_observation_count = ?",
        (MAX_BOUNDED_COUNTER,),
    )
    connection.close()
    assert store.ingest(_projection(2)).outcome is IngestionOutcome.STATE_ONLY
    assert _rows(
        path, "SELECT accepted_observation_count FROM ingestion_checkpoint"
    ) == [(MAX_BOUNDED_COUNTER,)]


def test_no_generic_sql_or_acknowledgment_surface_is_exposed() -> None:
    for name in ("execute", "query", "acknowledge", "archive", "migrate"):
        assert not hasattr(HealthHistoryStore, name)
    source = inspect.getsource(ingestion)
    assert " LIMIT " in source
    assert "m18_validation" not in source


def test_health_evaluator_threshold_recovery_and_failure_invariants() -> None:
    state = HealthEvaluationState()
    for count in range(1, 4):
        transition = evaluate_health_state(
            state,
            HealthEvaluationInput(status=HealthHistoryStatus.DEGRADED),
        )
        state = transition.state
        assert transition.event is (
            HealthEvaluationEvent.CONDITION_CONFIRMED
            if count == 3
            else HealthEvaluationEvent.NONE
        )
    failed = evaluate_health_state(
        state,
        HealthEvaluationInput(
            status=HealthHistoryStatus.HEALTHY,
            active_health_alert=True,
            persistence_succeeded=False,
        ),
    )
    assert failed.state == state and not failed.mutated
    first = evaluate_health_state(
        state,
        HealthEvaluationInput(
            status=HealthHistoryStatus.HEALTHY, active_health_alert=True
        ),
    )
    second = evaluate_health_state(
        first.state,
        HealthEvaluationInput(
            status=HealthHistoryStatus.HEALTHY, active_health_alert=True
        ),
    )
    assert second.event is HealthEvaluationEvent.RECOVERY_CONFIRMED


def test_sampling_gap_production_model_matches_reference_sequences() -> None:
    for missed_values in product((0, 1, 2, 7), repeat=3):
        production_state = SamplingGapState()
        reference_state = reference_gap.GapState()
        for sequence, missed in enumerate(missed_values, 1):
            production = evaluate_sampling_gap(
                production_state,
                SamplingGapObservation(sequence=sequence, missed_intervals=missed),
            )
            reference = reference_gap.evaluate_sampling_gap(
                reference_state,
                reference_gap.GapObservation(
                    sequence=sequence, missed_intervals=missed
                ),
            )
            assert production.event.value == reference.event.value
            assert production.state.phase.value == reference.state.phase.value
            assert (
                production.state.committed_observations
                == reference.state.committed_observations
            )
            assert (
                production.state.largest_missed_interval_report
                == reference.state.largest_missed_interval_report
            )
            production_state = production.state
            reference_state = reference.state


@pytest.mark.parametrize(
    ("phase", "missed", "healthy", "startup", "clock", "persisted"),
    [
        (SamplingGapPhase.CLEAR, 0, True, True, False, True),
        (SamplingGapPhase.RECOVERY_ONE, 0, True, False, True, True),
        (SamplingGapPhase.ACTIVE, 3, True, False, False, True),
        (SamplingGapPhase.ACTIVE, 0, False, False, False, True),
        (SamplingGapPhase.CANDIDATE_ONE, 0, True, False, False, False),
    ],
)
def test_sampling_gap_marker_failure_and_health_parity(
    phase: SamplingGapPhase,
    missed: int,
    healthy: bool,
    startup: bool,
    clock: bool,
    persisted: bool,
) -> None:
    production = evaluate_sampling_gap(
        SamplingGapState(phase=phase),
        SamplingGapObservation(
            sequence=1,
            missed_intervals=missed,
            health_collection_succeeded=healthy,
            persistence_succeeded=persisted,
            startup_gap_marker=startup,
            clock_discontinuity_marker=clock,
        ),
    )
    reference = reference_gap.evaluate_sampling_gap(
        reference_gap.GapState(phase=reference_gap.GapPhase(phase.value)),
        reference_gap.GapObservation(
            sequence=1,
            missed_intervals=missed,
            health_collection_succeeded=healthy,
            persistence_succeeded=persisted,
            restart_gap_marker=startup,
            clock_discontinuity_marker=clock,
        ),
    )
    assert production.event.value == reference.event.value
    assert production.state.phase.value == reference.state.phase.value


def test_sampling_gap_duplicate_sequence_parity() -> None:
    production_state = SamplingGapState(
        phase=SamplingGapPhase.ACTIVE,
        committed_observations=4,
        largest_missed_interval_report=2,
        last_committed_sequence=10,
    )
    reference_state = reference_gap.GapState(
        phase=reference_gap.GapPhase.ACTIVE,
        committed_observations=4,
        largest_missed_interval_report=2,
        last_committed_sequence=10,
    )
    for sequence in (9, 10):
        production = evaluate_sampling_gap(
            production_state,
            SamplingGapObservation(sequence=sequence, missed_intervals=20),
        )
        reference = reference_gap.evaluate_sampling_gap(
            reference_state,
            reference_gap.GapObservation(sequence=sequence, missed_intervals=20),
        )
        assert production.event.value == reference.event.value
        assert production.state == production_state
        assert reference.state == reference_state


@pytest.mark.parametrize(
    ("operation", "lifecycle", "now"),
    [
        ("open", None, 1),
        ("open", "open", 2),
        ("open", "acknowledged", 2),
        ("open", "recovered", 2),
        ("open", "archived", 2),
        ("recover", None, 2),
        ("recover", "open", 2),
        ("recover", "acknowledged", 2),
        ("recover", "recovered", 2),
        ("recover", "archived", 2),
        ("archive", None, 901),
        ("archive", "open", 901),
        ("archive", "acknowledged", 901),
        ("archive", "recovered", 901),
        ("archive", "recovered", 899),
        ("archive", "archived", 901),
        ("escalate", None, 2),
        ("escalate", "open", 2),
        ("escalate", "acknowledged", 2),
        ("escalate", "recovered", 2),
        ("escalate", "archived", 2),
    ],
)
def test_automatic_lifecycle_matches_accepted_reference_model(
    operation: str, lifecycle: str | None, now: int
) -> None:
    reference_state = (
        None
        if lifecycle is None
        else reference_alerts.AlertState(
            scope=reference_alerts.AlertScope.WLED,
            kind=reference_alerts.AlertKind.DEGRADED,
            lifecycle=reference_alerts.AlertLifecycle(lifecycle),
            cooldown_until=900,
            recovered_at=1 if lifecycle in {"recovered", "archived"} else None,
        )
    )
    production_state = (
        None
        if lifecycle is None
        else AutomaticAlertState(
            scope=AlertScope.WLED,
            kind=AlertKind.DEGRADED,
            lifecycle=AlertLifecycle(lifecycle),
            cooldown_until_utc_us=900 * 1_000_000,
            recovered_at_utc_us=(
                1_000_000 if lifecycle in {"recovered", "archived"} else None
            ),
        )
    )
    reference_operation = reference_alerts.AlertOperation(operation)
    production_operation = AutomaticAlertOperation(operation)
    reference = reference_alerts.evaluate_alert_lifecycle(
        reference_state,
        reference_alerts.AlertInput(
            operation=reference_operation,
            scope=reference_alerts.AlertScope.WLED,
            kind=reference_alerts.AlertKind.DEGRADED,
            now=now,
            escalation_kind=(
                reference_alerts.AlertKind.UNAVAILABLE
                if operation == "escalate"
                else None
            ),
        ),
    )
    production = evaluate_automatic_alert(
        production_state,
        AutomaticAlertInput(
            operation=production_operation,
            scope=AlertScope.WLED,
            kind=AlertKind.DEGRADED,
            now_utc_us=now * 1_000_000,
            escalation_kind=(
                AlertKind.UNAVAILABLE if operation == "escalate" else None
            ),
        ),
    )
    assert production.outcome.value == reference.outcome.value
    assert (None if production.event is None else production.event.value) == (
        None if reference.event is None else reference.event.value
    )
    assert (None if production.state is None else production.state.lifecycle.value) == (
        None if reference.state is None else reference.state.lifecycle.value
    )
    assert (
        None
        if production.created_alert is None
        else production.created_alert.kind.value
    ) == (
        None if reference.created_alert is None else reference.created_alert.kind.value
    )


def test_automatic_lifecycle_persistence_failure_and_occurrence_saturation() -> None:
    state = AutomaticAlertState(
        scope=AlertScope.WLED,
        kind=AlertKind.DEGRADED,
        lifecycle=AlertLifecycle.OPEN,
        occurrence_count=MAX_BOUNDED_COUNTER,
        cooldown_until_utc_us=1,
    )
    failed = evaluate_automatic_alert(
        state,
        AutomaticAlertInput(
            operation=AutomaticAlertOperation.OPEN,
            scope=AlertScope.WLED,
            kind=AlertKind.DEGRADED,
            now_utc_us=2,
            persistence_succeeded=False,
        ),
    )
    assert failed.state == state
    assert failed.event is None
    updated = evaluate_automatic_alert(
        state,
        AutomaticAlertInput(
            operation=AutomaticAlertOperation.OPEN,
            scope=AlertScope.WLED,
            kind=AlertKind.DEGRADED,
            now_utc_us=2,
        ),
    )
    assert updated.state is not None
    assert updated.state.occurrence_count == MAX_BOUNDED_COUNTER
    assert updated.event is LifecycleEvent.OCCURRENCE_UPDATED


def test_counters_saturate_without_overflow() -> None:
    health = evaluate_health_state(
        HealthEvaluationState(
            current_status=HealthHistoryStatus.DEGRADED,
            candidate_status=HealthHistoryStatus.DEGRADED,
            consecutive_count=MAX_BOUNDED_COUNTER,
        ),
        HealthEvaluationInput(status=HealthHistoryStatus.DEGRADED),
    )
    assert health.state.consecutive_count == MAX_BOUNDED_COUNTER
    gap = evaluate_sampling_gap(
        SamplingGapState(
            phase=SamplingGapPhase.ACTIVE,
            committed_observations=MAX_BOUNDED_COUNTER,
            largest_missed_interval_report=MAX_BOUNDED_COUNTER,
        ),
        SamplingGapObservation(sequence=1, missed_intervals=MAX_BOUNDED_COUNTER + 1),
    )
    assert gap.state.committed_observations == MAX_BOUNDED_COUNTER
    assert gap.state.largest_missed_interval_report == MAX_BOUNDED_COUNTER
