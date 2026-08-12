"""Synthetic tests for bounded Milestone 18 retention maintenance."""

from __future__ import annotations

import hashlib
import inspect
import socket
import sqlite3
import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any, cast

import pytest

import aurora_core.health_history.maintenance as maintenance
import aurora_core.health_history.store as store_module
from aurora_core.health_history import schema
from aurora_core.health_history.filesystem import (
    FilesystemBoundaryError,
    FilesystemRejection,
)
from aurora_core.health_history.ingestion import IngestionError, IngestionRejection
from aurora_core.health_history.maintenance import (
    DEFAULT_RETENTION_DAYS,
    INCREMENTAL_VACUUM_PAGES,
    MAX_RETENTION_DAYS,
    MIN_RETENTION_DAYS,
    RETENTION_ROW_BUDGET,
    IncrementalVacuumResult,
    MaintenanceError,
    MaintenanceOutcome,
    MaintenanceRejection,
    MaintenanceStage,
    RetentionCleanupResult,
)
from aurora_core.health_history.models import (
    APPLICATION_ID,
    AUTO_VACUUM_INCREMENTAL,
    COMPONENT_ORDER,
    PAGE_SIZE_BYTES,
    SCHEMA_VERSION,
    AlertKind,
    AlertLifecycle,
    AlertScope,
    ComponentName,
    HealthHistoryStatus,
    SampleKind,
)
from aurora_core.health_history.projection import (
    ComponentProjection,
    HealthProjection,
    _canonical_bytes,
)
from aurora_core.health_history.queries import QueryError, QueryRejection
from aurora_core.health_history.reasons import NormalizedReason
from aurora_core.health_history.store import HealthHistoryStore, StoreError

_NOW = 2_000_000_000_000_000
_DAY_US = 86_400_000_000
_CUTOFF = _NOW - DEFAULT_RETENTION_DAYS * _DAY_US
_TABLES = (
    "schema_migrations",
    "ingestion_checkpoint",
    "accepted_observation_replay",
    "health_samples",
    "component_samples",
    "alerts",
    "evaluation_state",
    "alert_events",
)


@pytest.fixture(autouse=True)
def _block_external_operations(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("external operation is prohibited")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(subprocess, "Popen", blocked)


@pytest.fixture
def store_path(history_test_directory: Path) -> tuple[Path, HealthHistoryStore]:
    path = history_test_directory / "history.db"
    store = HealthHistoryStore.create(path, created_at_utc_us=1)
    try:
        yield path, store
    finally:
        store.close()


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


def _snapshot(path: Path) -> dict[str, list[Any]]:
    return {
        table: _rows(path, f"SELECT * FROM {table} ORDER BY rowid") for table in _TABLES
    }


def _default_reason(
    component: ComponentName, status: HealthHistoryStatus
) -> NormalizedReason:
    return {
        (ComponentName.WLED, HealthHistoryStatus.HEALTHY): (
            NormalizedReason.WLED_HEALTHY
        ),
        (ComponentName.WLED, HealthHistoryStatus.DEGRADED): (
            NormalizedReason.WLED_INFO_LED_COUNT_MISMATCH
        ),
        (ComponentName.WLED, HealthHistoryStatus.UNAVAILABLE): (
            NormalizedReason.WLED_COLLECTOR_FAILED
        ),
        (ComponentName.HYPERHDR, HealthHistoryStatus.HEALTHY): (
            NormalizedReason.HYPERHDR_HEALTHY
        ),
        (ComponentName.HYPERHDR, HealthHistoryStatus.DEGRADED): (
            NormalizedReason.HYPERHDR_VIDEO_GRABBER_INACTIVE
        ),
        (ComponentName.HYPERHDR, HealthHistoryStatus.UNAVAILABLE): (
            NormalizedReason.HYPERHDR_COLLECTOR_FAILED
        ),
        (ComponentName.CAPTURE, HealthHistoryStatus.HEALTHY): (
            NormalizedReason.CAPTURE_HEALTHY
        ),
        (ComponentName.CAPTURE, HealthHistoryStatus.DEGRADED): (
            NormalizedReason.CAPTURE_GRABBER_INACTIVE
        ),
        (ComponentName.CAPTURE, HealthHistoryStatus.UNAVAILABLE): (
            NormalizedReason.CAPTURE_COLLECTOR_FAILED
        ),
        (ComponentName.RASPBERRY_PI, HealthHistoryStatus.HEALTHY): (
            NormalizedReason.RASPBERRY_PI_HEALTHY
        ),
        (ComponentName.RASPBERRY_PI, HealthHistoryStatus.DEGRADED): (
            NormalizedReason.RASPBERRY_PI_DEGRADED
        ),
        (ComponentName.RASPBERRY_PI, HealthHistoryStatus.UNAVAILABLE): (
            NormalizedReason.RASPBERRY_PI_UNAVAILABLE
        ),
    }[(component, status)]


def _projection(
    sequence: int,
    *,
    observed_at: int,
    recorded_at: int | None = None,
    statuses: dict[ComponentName, HealthHistoryStatus] | None = None,
    sample_kind: SampleKind = SampleKind.HEARTBEAT,
) -> HealthProjection:
    selected_statuses = {
        component: HealthHistoryStatus.HEALTHY for component in COMPONENT_ORDER
    }
    selected_statuses.update(statuses or {})
    components = tuple(
        ComponentProjection(
            component=component,
            status=selected_statuses[component],
            reasons=(_default_reason(component, selected_statuses[component]),),
            checked_at_utc_us=observed_at,
            latency_ms=sequence,
            last_successful_at_utc_us=(
                observed_at
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
    overall = max((component.status for component in components), key=ordering.get)
    digest = hashlib.sha256(
        _canonical_bytes(
            observation_sequence=sequence,
            observed_at=observed_at,
            status=overall,
            uptime=1_000,
            sample_kind=sample_kind,
            missed_intervals=0,
            components=components,
        )
    ).digest()
    return HealthProjection(
        schema_version=1,
        observation_sequence=sequence,
        observed_at_utc_us=observed_at,
        recorded_at_utc_us=(observed_at + 1 if recorded_at is None else recorded_at),
        overall_status=overall,
        service_uptime_ms=1_000,
        sample_kind=sample_kind,
        missed_intervals=0,
        components=components,
        digest=digest,
    )


def _insert_projection(
    connection: sqlite3.Connection,
    projection: HealthProjection,
    *,
    stored_kind: SampleKind | None = None,
) -> int:
    selected_stored_kind = stored_kind or (
        projection.sample_kind
        if projection.sample_kind
        in {SampleKind.STARTUP_GAP, SampleKind.CLOCK_DISCONTINUITY}
        else SampleKind.TRANSITION
    )
    cursor = connection.execute(
        "INSERT INTO health_samples("
        "observation_sequence, observed_at_utc_us, recorded_at_utc_us, "
        "overall_status, service_uptime_ms, sample_kind, "
        "accepted_sample_kind, projection_digest, missed_intervals"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            projection.observation_sequence,
            projection.observed_at_utc_us,
            projection.recorded_at_utc_us,
            projection.overall_status.value,
            projection.service_uptime_ms,
            selected_stored_kind.value,
            projection.sample_kind.value,
            projection.digest,
            projection.missed_intervals,
        ),
    )
    assert cursor.lastrowid is not None
    sample_id = cursor.lastrowid
    for component in projection.components:
        reasons = tuple(reason.value for reason in component.reasons)
        padded = (*reasons, None, None)[:3]
        connection.execute(
            "INSERT INTO component_samples("
            "sample_id, component, status, reason_code_1, reason_code_2, "
            "reason_code_3, checked_at_utc_us, latency_ms, "
            "last_successful_at_utc_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                sample_id,
                component.component.value,
                component.status.value,
                *padded,
                component.checked_at_utc_us,
                component.latency_ms,
                component.last_successful_at_utc_us,
            ),
        )
    return sample_id


def _seed_history(path: Path, projections: tuple[HealthProjection, ...]) -> list[int]:
    connection = _connect(path)
    connection.execute("BEGIN")
    ids = [_insert_projection(connection, projection) for projection in projections]
    connection.commit()
    connection.close()
    return ids


def _insert_alert(
    path: Path,
    *,
    lifecycle: AlertLifecycle,
    recovered_at: int | None = None,
    scope: AlertScope = AlertScope.OVERALL,
    kind: AlertKind = AlertKind.DEGRADED,
    first_sample_id: int | None = None,
    latest_sample_id: int | None = None,
) -> int:
    opened_at = (recovered_at - 10) if recovered_at is not None else _CUTOFF - 10
    acknowledged_at = (
        opened_at + 1 if lifecycle is AlertLifecycle.ACKNOWLEDGED else None
    )
    stored_recovered_at = (
        recovered_at
        if lifecycle in {AlertLifecycle.RECOVERED, AlertLifecycle.ARCHIVED}
        else None
    )
    cooldown_until = recovered_at if recovered_at is not None else opened_at + 100
    archived_at = cooldown_until + 1 if lifecycle is AlertLifecycle.ARCHIVED else None
    severity = (
        HealthHistoryStatus.DEGRADED
        if kind is AlertKind.DEGRADED
        else HealthHistoryStatus.UNAVAILABLE
    )
    connection = _connect(path)
    cursor = connection.execute(
        "INSERT INTO alerts("
        "scope, kind, lifecycle, severity, opened_at_utc_us, "
        "acknowledged_at_utc_us, recovered_at_utc_us, archived_at_utc_us, "
        "first_sample_id, latest_sample_id, episode_count, occurrence_count, "
        "cooldown_until_utc_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?)",
        (
            scope.value,
            kind.value,
            lifecycle.value,
            severity.value,
            opened_at,
            acknowledged_at,
            stored_recovered_at,
            archived_at,
            first_sample_id,
            latest_sample_id,
            cooldown_until,
        ),
    )
    connection.close()
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _insert_event(
    path: Path,
    alert_id: int,
    *,
    event_at: int,
    sample_id: int | None = None,
) -> int:
    connection = _connect(path)
    cursor = connection.execute(
        "INSERT INTO alert_events("
        "alert_id, event_type, event_at_utc_us, supporting_sample_id, "
        "resulting_lifecycle) VALUES (?, 'occurrence_updated', ?, ?, 'recovered')",
        (alert_id, event_at, sample_id),
    )
    connection.close()
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _make_freelist(path: Path, *, event_count: int = 3_000) -> int:
    alert_id = _insert_alert(
        path, lifecycle=AlertLifecycle.ARCHIVED, recovered_at=_CUTOFF
    )
    connection = _connect(path)
    connection.execute("BEGIN")
    connection.executemany(
        "INSERT INTO alert_events("
        "alert_id, event_type, event_at_utc_us, resulting_lifecycle) "
        "VALUES (?, 'occurrence_updated', ?, 'recovered')",
        ((alert_id, _CUTOFF + index) for index in range(event_count)),
    )
    connection.execute("DELETE FROM alert_events WHERE alert_id = ?", (alert_id,))
    connection.commit()
    freelist = cast(int, connection.execute("PRAGMA freelist_count").fetchone()[0])
    connection.close()
    assert freelist > 0
    return freelist


def test_schema_v1_creation_enables_incremental_auto_vacuum_and_fixed_indexes(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, _store = store_path
    assert _rows(path, "PRAGMA auto_vacuum") == [(AUTO_VACUUM_INCREMENTAL,)]
    assert _rows(path, "PRAGMA page_size") == [(PAGE_SIZE_BYTES,)]
    assert _rows(path, "PRAGMA application_id") == [(APPLICATION_ID,)]
    assert _rows(path, "PRAGMA user_version") == [(SCHEMA_VERSION,)]
    assert _rows(path, "SELECT version FROM schema_migrations") == [(1,)]
    indexes = {
        row[0]
        for row in _rows(
            path,
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND name NOT LIKE 'sqlite_%'",
        )
    }
    assert {
        "idx_alerts_first_sample_id",
        "idx_alerts_latest_sample_id",
        "idx_alerts_archived_recovered_id",
        "idx_alert_events_supporting_sample_id",
    } <= indexes


def test_schema_creation_rejects_auto_vacuum_none_before_creating_tables(
    history_test_directory: Path,
) -> None:
    path = history_test_directory / "unconfigured.db"
    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA auto_vacuum").fetchone() == (0,)
    with pytest.raises(schema.SchemaVerificationError) as caught:
        schema.create_schema_v1(connection, applied_at_utc_us=1)
    assert caught.value.reason == "auto_vacuum_mismatch"
    assert (
        connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        == []
    )
    connection.execute("PRAGMA auto_vacuum = INCREMENTAL")
    connection.execute("PRAGMA max_page_count = 16384")
    schema.create_schema_v1(connection, applied_at_utc_us=1)
    assert connection.execute("PRAGMA auto_vacuum").fetchone() == (
        AUTO_VACUUM_INCREMENTAL,
    )
    connection.close()


@pytest.mark.parametrize("mode", ["NONE", "FULL"])
def test_open_existing_and_schema_verification_reject_other_auto_vacuum_modes(
    store_path: tuple[Path, HealthHistoryStore], mode: str
) -> None:
    path, store = store_path
    store.close()
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode = DELETE")
    connection.execute(f"PRAGMA auto_vacuum = {mode}")
    connection.execute("VACUUM")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(schema.SchemaVerificationError) as caught:
        schema.verify_schema_v1(connection)
    assert caught.value.reason == "auto_vacuum_mismatch"
    connection.close()
    with pytest.raises(StoreError) as opened:
        HealthHistoryStore.open_existing(path)
    assert opened.value.reason == "open_failed"


@pytest.mark.parametrize(
    "value",
    [
        -1,
        True,
        1.0,
        "1",
        None,
        2**63,
    ],
)
def test_cleanup_rejects_invalid_now_before_sql_without_mutation(
    store_path: tuple[Path, HealthHistoryStore], value: object
) -> None:
    path, store = store_path
    before = _snapshot(path)
    statements: list[str] = []
    store._connection.set_trace_callback(statements.append)  # noqa: SLF001
    try:
        with pytest.raises(MaintenanceError) as caught:
            store.cleanup_retention(now_utc_us=value)  # type: ignore[arg-type]
    finally:
        store._connection.set_trace_callback(None)  # noqa: SLF001
    assert caught.value.reason is MaintenanceRejection.INVALID_MAINTENANCE
    assert statements == []
    assert not store.closed
    assert _snapshot(path) == before


@pytest.mark.parametrize(
    "value",
    [
        0,
        -1,
        True,
        1.0,
        "30",
        MIN_RETENTION_DAYS - 1,
        MAX_RETENTION_DAYS + 1,
    ],
)
def test_cleanup_rejects_invalid_retention_days_without_mutation(
    store_path: tuple[Path, HealthHistoryStore], value: object
) -> None:
    path, store = store_path
    before = _snapshot(path)
    with pytest.raises(MaintenanceError) as caught:
        store.cleanup_retention(
            now_utc_us=_NOW,
            retention_days=value,  # type: ignore[arg-type]
        )
    assert caught.value.reason is MaintenanceRejection.INVALID_MAINTENANCE
    assert not store.closed
    assert _snapshot(path) == before


@pytest.mark.parametrize("retention_days", [MIN_RETENTION_DAYS, MAX_RETENTION_DAYS])
def test_cleanup_accepts_fixed_retention_day_bounds(
    store_path: tuple[Path, HealthHistoryStore], retention_days: int
) -> None:
    _path, store = store_path
    result = store.cleanup_retention(now_utc_us=_NOW, retention_days=retention_days)
    assert result.outcome is MaintenanceOutcome.NO_WORK


def test_cleanup_no_work_and_exact_cutoff_are_read_only(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    _seed_history(path, (_projection(1, observed_at=_CUTOFF),))
    before = _snapshot(path)
    statements: list[str] = []
    store._connection.set_trace_callback(statements.append)  # noqa: SLF001
    try:
        result = store.cleanup_retention(now_utc_us=_NOW)
    finally:
        store._connection.set_trace_callback(None)  # noqa: SLF001
    assert result == RetentionCleanupResult(MaintenanceOutcome.NO_WORK, 0, 0, 0)
    normalized = [" ".join(statement.lower().split()) for statement in statements]
    assert normalized.count("begin immediate") == 1
    assert normalized.count("commit") == 1
    assert _snapshot(path) == before


def test_cleanup_uses_one_transaction_and_preserves_control_tables(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    _seed_history(path, (_projection(1, observed_at=_CUTOFF - 1),))
    protected_before = {
        table: _snapshot(path)[table]
        for table in (
            "schema_migrations",
            "ingestion_checkpoint",
            "accepted_observation_replay",
            "evaluation_state",
        )
    }
    statements: list[str] = []
    store._connection.set_trace_callback(statements.append)  # noqa: SLF001
    try:
        result = store.cleanup_retention(now_utc_us=_NOW)
    finally:
        store._connection.set_trace_callback(None)  # noqa: SLF001
    assert result.logical_rows_deleted == 1
    normalized = [" ".join(statement.lower().split()) for statement in statements]
    assert normalized.count("begin immediate") == 1
    assert normalized.count("commit") == 1
    assert "rollback" not in normalized
    after = _snapshot(path)
    assert {table: after[table] for table in protected_before} == protected_before


def test_one_microsecond_older_history_and_four_components_are_deleted(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    old_id, cutoff_id = _seed_history(
        path,
        (
            _projection(1, observed_at=_CUTOFF - 1),
            _projection(2, observed_at=_CUTOFF),
        ),
    )
    result = store.cleanup_retention(now_utc_us=_NOW)
    assert result == RetentionCleanupResult(MaintenanceOutcome.COMPLETED, 1, 0, 0)
    assert _rows(path, "SELECT id FROM health_samples") == [(cutoff_id,)]
    assert _rows(
        path, "SELECT COUNT(*) FROM component_samples WHERE sample_id = ?", (old_id,)
    ) == [(0,)]
    assert _rows(path, "SELECT COUNT(*) FROM component_samples") == [(4,)]


def test_all_fixed_sample_kinds_are_retained_oldest_first(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    kinds = (
        SampleKind.TRANSITION,
        SampleKind.HEARTBEAT,
        SampleKind.STARTUP_GAP,
        SampleKind.CLOCK_DISCONTINUITY,
    )
    projections = tuple(
        _projection(
            index,
            observed_at=_CUTOFF - len(kinds) - 1 + index,
            sample_kind=kind,
        )
        for index, kind in enumerate(kinds, 1)
    )
    _seed_history(path, projections)
    result = store.cleanup_retention(now_utc_us=_NOW)
    assert result.health_samples_deleted == len(kinds)
    assert result.logical_rows_deleted == len(kinds)
    assert _rows(path, "SELECT COUNT(*) FROM health_samples") == [(0,)]
    assert _rows(path, "SELECT COUNT(*) FROM component_samples") == [(0,)]


@pytest.mark.parametrize("count", [1, 499, 500])
def test_cleanup_deletes_fewer_than_or_exactly_the_fixed_budget(
    store_path: tuple[Path, HealthHistoryStore], count: int
) -> None:
    path, store = store_path
    _seed_history(
        path,
        tuple(
            _projection(sequence, observed_at=_CUTOFF - count + sequence - 1)
            for sequence in range(1, count + 1)
        ),
    )
    result = store.cleanup_retention(now_utc_us=_NOW)
    assert result.health_samples_deleted == count
    assert result.logical_rows_deleted == count
    assert _rows(path, "SELECT COUNT(*) FROM health_samples") == [(0,)]


def test_more_than_budget_uses_id_tiebreaker_and_second_call_progresses(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    ids = _seed_history(
        path,
        tuple(
            _projection(
                sequence,
                observed_at=(_CUTOFF - 2 if sequence <= 250 else _CUTOFF - 1),
            )
            for sequence in range(1, RETENTION_ROW_BUDGET + 2)
        ),
    )
    first = store.cleanup_retention(now_utc_us=_NOW)
    assert first.logical_rows_deleted == RETENTION_ROW_BUDGET
    assert _rows(path, "SELECT id FROM health_samples") == [(ids[-1],)]
    second = store.cleanup_retention(now_utc_us=_NOW)
    assert second.logical_rows_deleted == 1
    assert _rows(path, "SELECT COUNT(*) FROM health_samples") == [(0,)]


def test_health_tie_precedes_archived_alert_at_same_retention_timestamp(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    _seed_history(
        path,
        tuple(
            _projection(sequence, observed_at=_CUTOFF - 1)
            for sequence in range(1, RETENTION_ROW_BUDGET + 1)
        ),
    )
    alert_id = _insert_alert(
        path, lifecycle=AlertLifecycle.ARCHIVED, recovered_at=_CUTOFF - 1
    )
    result = store.cleanup_retention(now_utc_us=_NOW)
    assert result == RetentionCleanupResult(
        MaintenanceOutcome.COMPLETED, RETENTION_ROW_BUDGET, 0, 0
    )
    assert _rows(path, "SELECT id FROM alerts") == [(alert_id,)]


def test_health_retention_sets_all_documented_references_null_only(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    sample_id = _seed_history(path, (_projection(1, observed_at=_CUTOFF - 1),))[0]
    connection = _connect(path)
    connection.execute(
        "UPDATE evaluation_state SET current_status = 'healthy', "
        "last_sample_id = ?, last_heartbeat_at_utc_us = 123 "
        "WHERE scope != 'sampling'",
        (sample_id,),
    )
    connection.execute(
        "UPDATE evaluation_state SET last_sample_id = ?, gap_phase = 'active' "
        "WHERE scope = 'sampling'",
        (sample_id,),
    )
    connection.close()
    alert_id = _insert_alert(
        path,
        lifecycle=AlertLifecycle.OPEN,
        first_sample_id=sample_id,
        latest_sample_id=sample_id,
    )
    event_id = _insert_event(path, alert_id, event_at=_CUTOFF, sample_id=sample_id)
    evaluation_before = _rows(
        path,
        "SELECT scope, current_status, candidate_status, consecutive_count, "
        "last_heartbeat_at_utc_us, gap_phase, cooldown_until_utc_us "
        "FROM evaluation_state ORDER BY scope",
    )
    alert_before = _rows(
        path,
        "SELECT lifecycle, episode_count, occurrence_count, opened_at_utc_us, "
        "acknowledged_at_utc_us, recovered_at_utc_us, archived_at_utc_us, "
        "cooldown_until_utc_us FROM alerts WHERE id = ?",
        (alert_id,),
    )
    event_before = _rows(
        path,
        "SELECT event_type, event_at_utc_us, resulting_lifecycle "
        "FROM alert_events WHERE id = ?",
        (event_id,),
    )

    result = store.cleanup_retention(now_utc_us=_NOW)
    assert result.health_samples_deleted == 1
    assert _rows(path, "SELECT COUNT(*) FROM health_samples") == [(0,)]
    assert _rows(path, "SELECT COUNT(*) FROM component_samples") == [(0,)]
    assert _rows(path, "SELECT DISTINCT last_sample_id FROM evaluation_state") == [
        (None,)
    ]
    assert _rows(
        path,
        "SELECT first_sample_id, latest_sample_id FROM alerts WHERE id = ?",
        (alert_id,),
    ) == [(None, None)]
    assert _rows(
        path, "SELECT supporting_sample_id FROM alert_events WHERE id = ?", (event_id,)
    ) == [(None,)]
    retained_alert = store.get_alert(alert_id)
    assert retained_alert.first_sample_id is None
    assert retained_alert.latest_sample_id is None
    assert store.list_alert_events(alert_id).items[0].supporting_sample_id is None
    assert (
        _rows(
            path,
            "SELECT scope, current_status, candidate_status, consecutive_count, "
            "last_heartbeat_at_utc_us, gap_phase, cooldown_until_utc_us "
            "FROM evaluation_state ORDER BY scope",
        )
        == evaluation_before
    )
    assert (
        _rows(
            path,
            "SELECT lifecycle, episode_count, occurrence_count, opened_at_utc_us, "
            "acknowledged_at_utc_us, recovered_at_utc_us, archived_at_utc_us, "
            "cooldown_until_utc_us FROM alerts WHERE id = ?",
            (alert_id,),
        )
        == alert_before
    )
    assert (
        _rows(
            path,
            "SELECT event_type, event_at_utc_us, resulting_lifecycle "
            "FROM alert_events WHERE id = ?",
            (event_id,),
        )
        == event_before
    )


def test_unrelated_sample_references_remain_unchanged(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    old_id, retained_id = _seed_history(
        path,
        (
            _projection(1, observed_at=_CUTOFF - 1),
            _projection(2, observed_at=_CUTOFF),
        ),
    )
    alert_id = _insert_alert(
        path,
        lifecycle=AlertLifecycle.OPEN,
        first_sample_id=old_id,
        latest_sample_id=retained_id,
    )
    old_event = _insert_event(path, alert_id, event_at=_CUTOFF, sample_id=old_id)
    retained_event = _insert_event(
        path, alert_id, event_at=_CUTOFF + 1, sample_id=retained_id
    )
    store.cleanup_retention(now_utc_us=_NOW)
    assert _rows(
        path,
        "SELECT first_sample_id, latest_sample_id FROM alerts WHERE id = ?",
        (alert_id,),
    ) == [(None, retained_id)]
    assert _rows(
        path,
        "SELECT id, supporting_sample_id FROM alert_events ORDER BY id",
    ) == [(old_event, None), (retained_event, retained_id)]


def test_retention_cleared_evaluator_baseline_recovers_on_next_ingestion(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    first = _projection(1, observed_at=_CUTOFF - 1)
    store.ingest(first)
    sample_id = cast(
        int,
        _rows(
            path,
            "SELECT id FROM health_samples WHERE observation_sequence = 1",
        )[0][0],
    )
    alert_id = _insert_alert(
        path,
        lifecycle=AlertLifecycle.ARCHIVED,
        recovered_at=_CUTOFF,
        first_sample_id=sample_id,
        latest_sample_id=sample_id,
    )
    checkpoint_before = _rows(path, "SELECT * FROM ingestion_checkpoint")
    replay_before = _rows(path, "SELECT * FROM accepted_observation_replay")
    evaluation_before = _rows(
        path,
        "SELECT scope, current_status, candidate_status, consecutive_count, "
        "last_heartbeat_at_utc_us, gap_phase, cooldown_until_utc_us "
        "FROM evaluation_state ORDER BY scope",
    )
    cleanup = store.cleanup_retention(now_utc_us=_NOW)
    assert cleanup.health_samples_deleted == 1
    assert _rows(path, "SELECT * FROM ingestion_checkpoint") == checkpoint_before
    assert _rows(path, "SELECT * FROM accepted_observation_replay") == replay_before
    assert (
        _rows(
            path,
            "SELECT scope, current_status, candidate_status, consecutive_count, "
            "last_heartbeat_at_utc_us, gap_phase, cooldown_until_utc_us "
            "FROM evaluation_state ORDER BY scope",
        )
        == evaluation_before
    )
    assert _rows(path, "SELECT DISTINCT last_sample_id FROM evaluation_state") == [
        (None,)
    ]
    alert_after_cleanup = _rows(path, "SELECT * FROM alerts WHERE id = ?", (alert_id,))
    assert alert_after_cleanup[0][9:11] == (None, None)

    result = store.ingest(_projection(2, observed_at=_CUTOFF + 1))
    assert result.outcome.value == "transition_stored"
    baseline_id = _rows(
        path, "SELECT id FROM health_samples WHERE observation_sequence = 2"
    )[0][0]
    assert _rows(
        path,
        "SELECT COUNT(*) FROM component_samples WHERE sample_id = ?",
        (baseline_id,),
    ) == [(4,)]
    assert _rows(
        path,
        "SELECT last_committed_sequence, accepted_observation_count "
        "FROM ingestion_checkpoint",
    ) == [(2, 2)]
    assert _rows(path, "SELECT * FROM alerts WHERE id = ?", (alert_id,)) == (
        alert_after_cleanup
    )


def test_mixed_retention_cleared_evaluator_references_remain_trust_loss(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    store.ingest(_projection(1, observed_at=_CUTOFF - 1))
    store.cleanup_retention(now_utc_us=_NOW)
    retained_id = _seed_history(path, (_projection(99, observed_at=_CUTOFF),))[0]
    connection = _connect(path)
    connection.execute(
        "UPDATE evaluation_state SET last_sample_id = ? WHERE scope = 'wled'",
        (retained_id,),
    )
    connection.close()
    with pytest.raises(IngestionError) as caught:
        store.ingest(_projection(2, observed_at=_CUTOFF + 1))
    assert caught.value.reason is IngestionRejection.MALFORMED_STATE
    assert store.closed


def test_only_strictly_old_archived_alerts_are_eligible(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    survivors = [
        _insert_alert(path, lifecycle=AlertLifecycle.OPEN, scope=AlertScope.WLED),
        _insert_alert(
            path, lifecycle=AlertLifecycle.ACKNOWLEDGED, scope=AlertScope.HYPERHDR
        ),
        _insert_alert(
            path,
            lifecycle=AlertLifecycle.RECOVERED,
            recovered_at=_CUTOFF - 1,
            scope=AlertScope.CAPTURE,
        ),
        _insert_alert(path, lifecycle=AlertLifecycle.ARCHIVED, recovered_at=_CUTOFF),
        _insert_alert(
            path,
            lifecycle=AlertLifecycle.ARCHIVED,
            recovered_at=_CUTOFF + 1,
            scope=AlertScope.RASPBERRY_PI,
        ),
    ]
    eligible = _insert_alert(
        path,
        lifecycle=AlertLifecycle.ARCHIVED,
        recovered_at=_CUTOFF - 1,
        scope=AlertScope.SAMPLING,
        kind=AlertKind.SAMPLING_GAP,
    )
    survivor_events = [
        _insert_event(path, alert_id, event_at=_CUTOFF + index)
        for index, alert_id in enumerate(survivors)
    ]
    result = store.cleanup_retention(now_utc_us=_NOW)
    assert result == RetentionCleanupResult(MaintenanceOutcome.COMPLETED, 0, 0, 1)
    assert [
        row[0] for row in _rows(path, "SELECT id FROM alerts ORDER BY id")
    ] == survivors
    assert [
        row[0] for row in _rows(path, "SELECT id FROM alert_events ORDER BY id")
    ] == survivor_events
    assert eligible not in survivors


def test_eligible_archived_events_are_deleted_before_parent_without_replacement_event(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    alert_id = _insert_alert(
        path, lifecycle=AlertLifecycle.ARCHIVED, recovered_at=_CUTOFF - 1
    )
    event_ids = [
        _insert_event(path, alert_id, event_at=_CUTOFF + index) for index in range(3)
    ]
    unrelated = _insert_alert(
        path,
        lifecycle=AlertLifecycle.ARCHIVED,
        recovered_at=_CUTOFF,
        scope=AlertScope.WLED,
    )
    unrelated_event = _insert_event(path, unrelated, event_at=_CUTOFF)
    result = store.cleanup_retention(now_utc_us=_NOW)
    assert result == RetentionCleanupResult(MaintenanceOutcome.COMPLETED, 0, 3, 1)
    assert _rows(path, "SELECT id FROM alerts ORDER BY id") == [(unrelated,)]
    assert _rows(path, "SELECT id FROM alert_events") == [(unrelated_event,)]
    assert len(event_ids) == 3


def test_archived_alert_with_more_than_budget_events_retires_across_calls(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    alert_id = _insert_alert(
        path, lifecycle=AlertLifecycle.ARCHIVED, recovered_at=_CUTOFF - 1
    )
    connection = _connect(path)
    connection.execute("BEGIN")
    connection.executemany(
        "INSERT INTO alert_events("
        "alert_id, event_type, event_at_utc_us, resulting_lifecycle) "
        "VALUES (?, 'occurrence_updated', ?, 'recovered')",
        ((alert_id, _CUTOFF + index) for index in range(RETENTION_ROW_BUDGET + 1)),
    )
    connection.commit()
    connection.close()
    event_ids = [
        row[0]
        for row in _rows(
            path,
            "SELECT id FROM alert_events WHERE alert_id = ? "
            "ORDER BY event_at_utc_us, id",
            (alert_id,),
        )
    ]

    first = store.cleanup_retention(now_utc_us=_NOW)
    assert first == RetentionCleanupResult(
        MaintenanceOutcome.COMPLETED, 0, RETENTION_ROW_BUDGET, 0
    )
    assert _rows(path, "SELECT id FROM alerts") == [(alert_id,)]
    assert _rows(path, "SELECT id FROM alert_events") == [(event_ids[-1],)]
    assert store.get_alert(alert_id).lifecycle is AlertLifecycle.ARCHIVED
    assert len(store.list_alert_events(alert_id).items) == 1

    second = store.cleanup_retention(now_utc_us=_NOW)
    assert second == RetentionCleanupResult(MaintenanceOutcome.COMPLETED, 0, 1, 1)
    with pytest.raises(QueryError) as missing:
        store.get_alert(alert_id)
    assert missing.value.reason is QueryRejection.NOT_FOUND
    assert _rows(path, "SELECT COUNT(*) FROM alert_events") == [(0,)]


def test_mixed_health_and_event_work_never_exceeds_total_budget(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    _seed_history(
        path,
        tuple(
            _projection(sequence, observed_at=_CUTOFF - 2) for sequence in range(1, 11)
        ),
    )
    alert_id = _insert_alert(
        path, lifecycle=AlertLifecycle.ARCHIVED, recovered_at=_CUTOFF - 1
    )
    connection = _connect(path)
    connection.executemany(
        "INSERT INTO alert_events("
        "alert_id, event_type, event_at_utc_us, resulting_lifecycle) "
        "VALUES (?, 'occurrence_updated', ?, 'recovered')",
        ((alert_id, _CUTOFF + index) for index in range(500)),
    )
    connection.close()
    first = store.cleanup_retention(now_utc_us=_NOW)
    assert first == RetentionCleanupResult(MaintenanceOutcome.COMPLETED, 10, 490, 0)
    assert first.logical_rows_deleted == RETENTION_ROW_BUDGET
    assert _rows(path, "SELECT COUNT(*) FROM alert_events") == [(10,)]
    second = store.cleanup_retention(now_utc_us=_NOW)
    assert second == RetentionCleanupResult(MaintenanceOutcome.COMPLETED, 0, 10, 1)


def test_multiple_archived_alerts_follow_recovered_time_then_id_order(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    first = _insert_alert(
        path, lifecycle=AlertLifecycle.ARCHIVED, recovered_at=_CUTOFF - 2
    )
    second = _insert_alert(
        path,
        lifecycle=AlertLifecycle.ARCHIVED,
        recovered_at=_CUTOFF - 1,
        scope=AlertScope.WLED,
    )
    connection = _connect(path)
    connection.executemany(
        "INSERT INTO alert_events("
        "alert_id, event_type, event_at_utc_us, resulting_lifecycle) "
        "VALUES (?, 'occurrence_updated', ?, 'recovered')",
        (
            (alert_id, _CUTOFF + index)
            for alert_id in (first, second)
            for index in range(251)
        ),
    )
    connection.close()
    result = store.cleanup_retention(now_utc_us=_NOW)
    assert result == RetentionCleanupResult(MaintenanceOutcome.COMPLETED, 0, 499, 1)
    assert _rows(path, "SELECT id FROM alerts") == [(second,)]
    assert _rows(
        path, "SELECT COUNT(*) FROM alert_events WHERE alert_id = ?", (second,)
    ) == [(3,)]


def test_equal_recovered_archived_alerts_use_id_order(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    first = _insert_alert(
        path, lifecycle=AlertLifecycle.ARCHIVED, recovered_at=_CUTOFF - 1
    )
    second = _insert_alert(
        path,
        lifecycle=AlertLifecycle.ARCHIVED,
        recovered_at=_CUTOFF - 1,
        scope=AlertScope.WLED,
    )
    connection = _connect(path)
    connection.executemany(
        "INSERT INTO alert_events("
        "alert_id, event_type, event_at_utc_us, resulting_lifecycle) "
        "VALUES (?, 'occurrence_updated', ?, 'recovered')",
        ((first, _CUTOFF + index) for index in range(499)),
    )
    connection.close()
    result = store.cleanup_retention(now_utc_us=_NOW)
    assert result == RetentionCleanupResult(MaintenanceOutcome.COMPLETED, 0, 499, 1)
    assert _rows(path, "SELECT id FROM alerts") == [(second,)]


@pytest.mark.parametrize(
    "malformation",
    ["health_digest", "alert_severity", "event_lifecycle"],
)
def test_malformed_retention_candidates_fail_closed_without_mutation(
    store_path: tuple[Path, HealthHistoryStore], malformation: str
) -> None:
    path, store = store_path
    connection = _connect(path)
    connection.execute("PRAGMA ignore_check_constraints = ON")
    if malformation == "health_digest":
        connection.close()
        _seed_history(path, (_projection(1, observed_at=_CUTOFF - 1),))
        connection = _connect(path)
        connection.execute("UPDATE health_samples SET projection_digest = zeroblob(32)")
    else:
        connection.close()
        alert_id = _insert_alert(
            path, lifecycle=AlertLifecycle.ARCHIVED, recovered_at=_CUTOFF - 1
        )
        if malformation == "alert_severity":
            connection = _connect(path)
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute("UPDATE alerts SET severity = 'unavailable'")
        else:
            _insert_event(path, alert_id, event_at=_CUTOFF)
            connection = _connect(path)
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute(
                "UPDATE alert_events SET resulting_lifecycle = 'archived'"
            )
    connection.execute("PRAGMA ignore_check_constraints = OFF")
    connection.close()
    before = _snapshot(path)
    with pytest.raises(MaintenanceError) as caught:
        store.cleanup_retention(now_utc_us=_NOW)
    assert caught.value.reason is MaintenanceRejection.MALFORMED_STATE
    assert caught.value.trust_lost
    assert store.closed
    assert _snapshot(path) == before


def test_retained_rows_remain_queryable_and_pagination_has_no_regression(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    _seed_history(
        path,
        tuple(
            _projection(
                sequence,
                observed_at=(_CUTOFF - 1 if sequence <= 5 else _CUTOFF + sequence),
            )
            for sequence in range(1, 11)
        ),
    )
    store.cleanup_retention(now_utc_us=_NOW)
    seen: list[int] = []
    cursor = None
    while True:
        page = store.list_health_samples(page_size=2, cursor=cursor)
        seen.extend(record.observation_sequence for record in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert seen == [10, 9, 8, 7, 6]
    assert len(seen) == len(set(seen))


def test_incremental_vacuum_zero_freelist_returns_no_work(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    assert _rows(path, "PRAGMA freelist_count") == [(0,)]
    before = _snapshot(path)
    result = store.incremental_vacuum()
    assert result == IncrementalVacuumResult(MaintenanceOutcome.NO_WORK, 0, 0, 0)
    assert _snapshot(path) == before


def test_incremental_vacuum_zero_freelist_checks_deadline_before_no_work(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, store = store_path
    before = _snapshot(path)
    now = 0.0
    freelist_reads = 0
    original_freelist_count = maintenance._freelist_count

    def monotonic() -> float:
        return now

    def freelist_count(connection: sqlite3.Connection) -> int:
        nonlocal freelist_reads, now
        freelist_reads += 1
        result = original_freelist_count(connection)
        assert result == 0
        now = 2.0
        return result

    statements: list[str] = []
    monkeypatch.setattr(store, "_monotonic", monotonic)
    monkeypatch.setattr(maintenance, "_freelist_count", freelist_count)
    store._connection.set_trace_callback(statements.append)  # noqa: SLF001
    try:
        with pytest.raises(MaintenanceError) as caught:
            store.incremental_vacuum()
    finally:
        store._connection.set_trace_callback(None)  # noqa: SLF001
    assert caught.value.reason is MaintenanceRejection.TIMED_OUT
    assert freelist_reads == 1
    assert not any("incremental_vacuum(128)" in item for item in statements)
    assert not store.closed
    assert _snapshot(path) == before


def test_incremental_vacuum_executes_one_fixed_128_page_request(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, store = store_path
    before = _make_freelist(path, event_count=30_000)
    assert before > INCREMENTAL_VACUUM_PAGES
    cursor_consumed = False
    freelist_reads = 0
    original_consume = maintenance._consume_incremental_vacuum_cursor
    original_freelist_count = maintenance._freelist_count

    def consume(cursor: sqlite3.Cursor) -> int:
        nonlocal cursor_consumed
        consumed_rows = original_consume(cursor)
        cursor_consumed = True
        return consumed_rows

    def freelist_count(connection: sqlite3.Connection) -> int:
        nonlocal freelist_reads
        freelist_reads += 1
        if freelist_reads == 2:
            assert cursor_consumed
        return original_freelist_count(connection)

    monkeypatch.setattr(maintenance, "_consume_incremental_vacuum_cursor", consume)
    monkeypatch.setattr(maintenance, "_freelist_count", freelist_count)
    statements: list[str] = []
    store._connection.set_trace_callback(statements.append)  # noqa: SLF001
    try:
        result = store.incremental_vacuum()
    finally:
        store._connection.set_trace_callback(None)  # noqa: SLF001
    assert result.outcome is MaintenanceOutcome.COMPLETED
    assert result.pages_requested == INCREMENTAL_VACUUM_PAGES
    assert result.freelist_before == before
    assert 0 <= result.freelist_after <= before
    reclaimed = result.freelist_before - result.freelist_after
    assert 1 < reclaimed <= INCREMENTAL_VACUUM_PAGES
    assert cursor_consumed
    assert freelist_reads == 2
    normalized = [" ".join(statement.lower().split()) for statement in statements]
    assert normalized.count("pragma incremental_vacuum(128)") == 1
    assert "vacuum" not in normalized


def test_incremental_vacuum_cursor_consumption_is_fixed_and_bounded() -> None:
    class FakeVacuumCursor:
        def __init__(self, row_count: int) -> None:
            self.remaining = [()] * row_count
            self.fetch_sizes: list[int] = []
            self.done = False

        def fetchmany(self, size: int) -> list[tuple[()]]:
            self.fetch_sizes.append(size)
            rows = self.remaining[:size]
            self.remaining = self.remaining[size:]
            self.done = len(rows) < size
            return rows

    cursor = FakeVacuumCursor(INCREMENTAL_VACUUM_PAGES)
    consumed = maintenance._consume_incremental_vacuum_cursor(
        cast(sqlite3.Cursor, cursor)
    )
    assert consumed == INCREMENTAL_VACUUM_PAGES
    assert cursor.fetch_sizes == [INCREMENTAL_VACUUM_PAGES + 1]
    assert cursor.done
    assert cursor.remaining == []

    overflow = FakeVacuumCursor(INCREMENTAL_VACUUM_PAGES + 100)
    with pytest.raises(MaintenanceError) as caught:
        maintenance._consume_incremental_vacuum_cursor(cast(sqlite3.Cursor, overflow))
    assert caught.value.reason is MaintenanceRejection.MALFORMED_STATE
    assert caught.value.trust_lost
    assert overflow.fetch_sizes == [INCREMENTAL_VACUUM_PAGES + 1]


def test_cleanup_timeout_rolls_back_and_clears_progress_handler(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, store = store_path
    _seed_history(path, (_projection(1, observed_at=_CUTOFF - 1),))
    before = _snapshot(path)
    calls = 0

    def elapsed() -> float:
        nonlocal calls
        calls += 1
        return 0.0 if calls == 1 else 2.0

    monkeypatch.setattr(store, "_monotonic", elapsed)
    monkeypatch.setattr(maintenance, "PROGRESS_HANDLER_STEPS", 1)
    with pytest.raises(MaintenanceError) as caught:
        store.cleanup_retention(now_utc_us=_NOW)
    assert caught.value.reason is MaintenanceRejection.TIMED_OUT
    assert not store.closed
    assert _snapshot(path) == before
    assert store.list_health_samples().items


def test_cleanup_checks_deadline_after_python_candidate_validation(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, store = store_path
    _seed_history(path, (_projection(1, observed_at=_CUTOFF - 1),))
    before = _snapshot(path)
    now = 0.0
    validation_calls = 0
    original_record = maintenance._health_sample_record

    def monotonic() -> float:
        return now

    def validate(connection: sqlite3.Connection, row: tuple[object, ...]) -> object:
        nonlocal now, validation_calls
        validation_calls += 1
        result = original_record(connection, row)
        now = 2.0
        return result

    monkeypatch.setattr(store, "_monotonic", monotonic)
    monkeypatch.setattr(maintenance, "_health_sample_record", validate)
    with pytest.raises(MaintenanceError) as caught:
        store.cleanup_retention(now_utc_us=_NOW)
    assert caught.value.reason is MaintenanceRejection.TIMED_OUT
    assert validation_calls == 1
    assert not store.closed
    assert _snapshot(path) == before


def test_cleanup_checks_deadline_before_python_deletion_action(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, store = store_path
    _seed_history(path, (_projection(1, observed_at=_CUTOFF - 1),))
    before = _snapshot(path)
    now = 0.0
    planning_calls = 0
    original_plan = maintenance._retention_plan

    def monotonic() -> float:
        return now

    def plan(
        connection: sqlite3.Connection,
        cutoff: int,
        deadline: maintenance._Deadline,
    ) -> tuple[maintenance._DeletionAction, ...]:
        nonlocal now, planning_calls
        planning_calls += 1
        result = original_plan(connection, cutoff, deadline)
        now = 2.0
        return result

    monkeypatch.setattr(store, "_monotonic", monotonic)
    monkeypatch.setattr(maintenance, "_retention_plan", plan)
    with pytest.raises(MaintenanceError) as caught:
        store.cleanup_retention(now_utc_us=_NOW)
    assert caught.value.reason is MaintenanceRejection.TIMED_OUT
    assert planning_calls == 1
    assert not store.closed
    assert _snapshot(path) == before


def test_cleanup_post_commit_timeout_preserves_committed_deletion_without_rollback(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, store = store_path
    _seed_history(path, (_projection(1, observed_at=_CUTOFF - 1),))
    now = 0.0
    commit_calls = 0
    rollback_calls = 0
    original_commit = maintenance._commit_transaction
    original_rollback = maintenance._rollback_transaction

    def monotonic() -> float:
        return now

    def commit(connection: sqlite3.Connection) -> None:
        nonlocal now, commit_calls
        commit_calls += 1
        original_commit(connection)
        now = 2.0

    def rollback(connection: sqlite3.Connection) -> None:
        nonlocal rollback_calls
        rollback_calls += 1
        original_rollback(connection)

    monkeypatch.setattr(store, "_monotonic", monotonic)
    monkeypatch.setattr(maintenance, "_commit_transaction", commit)
    monkeypatch.setattr(maintenance, "_rollback_transaction", rollback)
    with pytest.raises(MaintenanceError) as caught:
        store.cleanup_retention(now_utc_us=_NOW)
    assert caught.value.reason is MaintenanceRejection.TIMED_OUT
    assert commit_calls == 1
    assert rollback_calls == 0
    assert not store.closed
    assert _rows(path, "SELECT COUNT(*) FROM health_samples") == [(0,)]
    assert _rows(path, "SELECT COUNT(*) FROM component_samples") == [(0,)]


def test_incremental_vacuum_timeout_makes_no_vacuum_call(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, store = store_path
    _make_freelist(path)
    calls = 0

    def elapsed() -> float:
        nonlocal calls
        calls += 1
        return 0.0 if calls == 1 else 2.0

    statements: list[str] = []
    monkeypatch.setattr(store, "_monotonic", elapsed)
    store._connection.set_trace_callback(statements.append)  # noqa: SLF001
    try:
        with pytest.raises(MaintenanceError) as caught:
            store.incremental_vacuum()
    finally:
        store._connection.set_trace_callback(None)  # noqa: SLF001
    assert caught.value.reason is MaintenanceRejection.TIMED_OUT
    assert not store.closed
    assert not any("incremental_vacuum(128)" in item for item in statements)


def test_progress_handler_clear_runs_when_install_reports_failure(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _path, store = store_path
    calls: list[str] = []

    def fail_install(connection: sqlite3.Connection, deadline: object) -> None:
        del connection, deadline
        calls.append("install")
        raise MaintenanceError(MaintenanceRejection.PERSISTENCE_FAILED)

    def clear(connection: sqlite3.Connection) -> None:
        del connection
        calls.append("clear")

    monkeypatch.setattr(maintenance, "_install_progress_handler", fail_install)
    monkeypatch.setattr(maintenance, "_clear_progress_handler", clear)
    with pytest.raises(MaintenanceError) as caught:
        store.incremental_vacuum()
    assert caught.value.reason is MaintenanceRejection.PERSISTENCE_FAILED
    assert calls == ["install", "clear"]
    assert not store.closed


@pytest.mark.parametrize("error_code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED])
def test_busy_and_locked_cleanup_are_non_retrying_non_trust_failures(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
    error_code: int,
) -> None:
    path, store = store_path
    _seed_history(path, (_projection(1, observed_at=_CUTOFF - 1),))
    before = _snapshot(path)
    calls = 0

    def fail(stage: MaintenanceStage) -> None:
        nonlocal calls
        if stage is MaintenanceStage.AFTER_BEGIN:
            calls += 1
            error = sqlite3.OperationalError("private lock detail")
            error.sqlite_errorcode = error_code  # type: ignore[attr-defined]
            raise error

    monkeypatch.setattr(maintenance, "_fault", fail)
    with pytest.raises(MaintenanceError) as caught:
        store.cleanup_retention(now_utc_us=_NOW)
    assert caught.value.reason is MaintenanceRejection.STORAGE_BUSY
    assert str(caught.value) == "storage_busy"
    assert calls == 1
    assert not store.closed
    assert _snapshot(path) == before


@pytest.mark.parametrize("error_code", [sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_SCHEMA])
def test_corruption_and_schema_cleanup_failures_roll_back_and_close(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
    error_code: int,
) -> None:
    path, store = store_path
    _seed_history(path, (_projection(1, observed_at=_CUTOFF - 1),))
    before = _snapshot(path)

    def fail(stage: MaintenanceStage) -> None:
        if stage is MaintenanceStage.AFTER_PLAN:
            error = sqlite3.DatabaseError("private corruption detail")
            error.sqlite_errorcode = error_code  # type: ignore[attr-defined]
            raise error

    monkeypatch.setattr(maintenance, "_fault", fail)
    with pytest.raises(MaintenanceError) as caught:
        store.cleanup_retention(now_utc_us=_NOW)
    assert caught.value.reason is MaintenanceRejection.TRUST_FAILED
    assert caught.value.__cause__ is None
    assert store.closed
    assert _snapshot(path) == before


def test_constraint_failure_after_mutation_rolls_back_every_table(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, store = store_path
    _seed_history(path, (_projection(1, observed_at=_CUTOFF - 1),))
    before = _snapshot(path)

    def fail(stage: MaintenanceStage) -> None:
        if stage is MaintenanceStage.AFTER_MUTATION:
            raise sqlite3.IntegrityError("private constraint detail")

    monkeypatch.setattr(maintenance, "_fault", fail)
    with pytest.raises(MaintenanceError) as caught:
        store.cleanup_retention(now_utc_us=_NOW)
    assert caught.value.reason is MaintenanceRejection.TRUST_FAILED
    assert store.closed
    assert _snapshot(path) == before


def test_generic_persistence_failure_rolls_back_and_keeps_trust(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, store = store_path
    _seed_history(path, (_projection(1, observed_at=_CUTOFF - 1),))
    before = _snapshot(path)

    def fail(stage: MaintenanceStage) -> None:
        if stage is MaintenanceStage.AFTER_MUTATION:
            raise sqlite3.OperationalError("private capacity detail")

    monkeypatch.setattr(maintenance, "_fault", fail)
    with pytest.raises(MaintenanceError) as caught:
        store.cleanup_retention(now_utc_us=_NOW)
    assert caught.value.reason is MaintenanceRejection.PERSISTENCE_FAILED
    assert caught.value.__cause__ is None
    assert not store.closed
    assert _snapshot(path) == before


def test_rollback_failure_is_attempted_once_and_loses_trust(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, store = store_path
    _seed_history(path, (_projection(1, observed_at=_CUTOFF - 1),))
    before = _snapshot(path)
    rollback_calls = 0

    def fail_write(stage: MaintenanceStage) -> None:
        if stage is MaintenanceStage.AFTER_MUTATION:
            raise sqlite3.OperationalError("private failure")

    def fail_rollback(connection: sqlite3.Connection) -> None:
        nonlocal rollback_calls
        del connection
        rollback_calls += 1
        raise sqlite3.DatabaseError("private rollback failure")

    monkeypatch.setattr(maintenance, "_fault", fail_write)
    monkeypatch.setattr(maintenance, "_rollback_transaction", fail_rollback)
    with pytest.raises(MaintenanceError) as caught:
        store.cleanup_retention(now_utc_us=_NOW)
    assert caught.value.reason is MaintenanceRejection.TRUST_FAILED
    assert rollback_calls == 1
    assert store.closed
    assert _snapshot(path) == before


@pytest.mark.parametrize("identity_target", ["main", "sidecar"])
def test_post_commit_identity_loss_reports_trust_failure_without_rollback_claim(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
    identity_target: str,
) -> None:
    path, store = store_path
    _seed_history(path, (_projection(1, observed_at=_CUTOFF - 1),))
    if identity_target == "main":
        original = store_module.validate_database_file
        calls = 0

        def changed(candidate: Path, *, expected: object | None = None) -> Any:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED)
            return original(candidate, expected=expected)  # type: ignore[arg-type]

        monkeypatch.setattr(store_module, "validate_database_file", changed)
    else:
        original_sidecars = store_module._advance_sidecar_snapshot
        sidecar_calls = 0

        def changed_sidecars(candidate: Path, prior: object) -> Any:
            nonlocal sidecar_calls
            sidecar_calls += 1
            if sidecar_calls == 2:
                raise FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED)
            return original_sidecars(candidate, prior)  # type: ignore[arg-type]

        monkeypatch.setattr(store_module, "_advance_sidecar_snapshot", changed_sidecars)
    with pytest.raises(MaintenanceError) as caught:
        store.cleanup_retention(now_utc_us=_NOW)
    assert caught.value.reason is MaintenanceRejection.TRUST_FAILED
    assert store.closed
    assert _rows(path, "SELECT COUNT(*) FROM health_samples") == [(0,)]


@pytest.mark.parametrize("identity_target", ["main", "sidecar"])
def test_identity_loss_after_non_trust_failure_overrides_original_error(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
    identity_target: str,
) -> None:
    path, store = store_path
    before = _snapshot(path)

    def fail(stage: MaintenanceStage) -> None:
        if stage is MaintenanceStage.AFTER_BEGIN:
            error = sqlite3.OperationalError("private busy detail")
            error.sqlite_errorcode = sqlite3.SQLITE_BUSY  # type: ignore[attr-defined]
            raise error

    monkeypatch.setattr(maintenance, "_fault", fail)
    if identity_target == "main":
        original = store_module.validate_database_file
        calls = 0

        def changed(candidate: Path, *, expected: object | None = None) -> Any:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED)
            return original(candidate, expected=expected)  # type: ignore[arg-type]

        monkeypatch.setattr(store_module, "validate_database_file", changed)
    else:
        original_sidecars = store_module._advance_sidecar_snapshot
        sidecar_calls = 0

        def changed_sidecars(candidate: Path, prior: object) -> Any:
            nonlocal sidecar_calls
            sidecar_calls += 1
            if sidecar_calls == 2:
                raise FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED)
            return original_sidecars(candidate, prior)  # type: ignore[arg-type]

        monkeypatch.setattr(store_module, "_advance_sidecar_snapshot", changed_sidecars)
    with pytest.raises(MaintenanceError) as caught:
        store.cleanup_retention(now_utc_us=_NOW)
    assert caught.value.reason is MaintenanceRejection.TRUST_FAILED
    assert store.closed
    assert _snapshot(path) == before


def test_incremental_vacuum_corruption_failure_closes_store(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, store = store_path
    _make_freelist(path)
    before = _snapshot(path)

    def fail(stage: MaintenanceStage) -> None:
        if stage is MaintenanceStage.VACUUM_BEFORE:
            error = sqlite3.DatabaseError("private corruption detail")
            error.sqlite_errorcode = sqlite3.SQLITE_CORRUPT  # type: ignore[attr-defined]
            raise error

    monkeypatch.setattr(maintenance, "_fault", fail)
    with pytest.raises(MaintenanceError) as caught:
        store.incremental_vacuum()
    assert caught.value.reason is MaintenanceRejection.TRUST_FAILED
    assert store.closed
    assert _snapshot(path) == before


@pytest.mark.parametrize("error_code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED])
def test_incremental_vacuum_busy_and_locked_are_non_retrying_non_trust_failures(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
    error_code: int,
) -> None:
    path, store = store_path
    _make_freelist(path)
    before = _snapshot(path)
    calls = 0

    def fail(stage: MaintenanceStage) -> None:
        nonlocal calls
        if stage is MaintenanceStage.VACUUM_BEFORE:
            calls += 1
            error = sqlite3.OperationalError("private lock detail")
            error.sqlite_errorcode = error_code  # type: ignore[attr-defined]
            raise error

    monkeypatch.setattr(maintenance, "_fault", fail)
    with pytest.raises(MaintenanceError) as caught:
        store.incremental_vacuum()
    assert caught.value.reason is MaintenanceRejection.STORAGE_BUSY
    assert calls == 1
    assert not store.closed
    assert _snapshot(path) == before


def test_incremental_vacuum_rechecks_incremental_mode_and_loses_trust(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _path, store = store_path
    monkeypatch.setattr(maintenance, "_auto_vacuum_mode", lambda connection: 0)
    with pytest.raises(MaintenanceError) as caught:
        store.incremental_vacuum()
    assert caught.value.reason is MaintenanceRejection.TRUST_FAILED
    assert store.closed


@pytest.mark.parametrize("identity_target", ["main", "sidecar"])
def test_incremental_vacuum_post_operation_identity_loss_closes_store(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
    identity_target: str,
) -> None:
    path, store = store_path
    before = _snapshot(path)
    if identity_target == "main":
        original = store_module.validate_database_file
        calls = 0

        def changed(candidate: Path, *, expected: object | None = None) -> Any:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED)
            return original(candidate, expected=expected)  # type: ignore[arg-type]

        monkeypatch.setattr(store_module, "validate_database_file", changed)
    else:
        original_sidecars = store_module._advance_sidecar_snapshot
        sidecar_calls = 0

        def changed_sidecars(candidate: Path, prior: object) -> Any:
            nonlocal sidecar_calls
            sidecar_calls += 1
            if sidecar_calls == 2:
                raise FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED)
            return original_sidecars(candidate, prior)  # type: ignore[arg-type]

        monkeypatch.setattr(store_module, "_advance_sidecar_snapshot", changed_sidecars)
    with pytest.raises(MaintenanceError) as caught:
        store.incremental_vacuum()
    assert caught.value.reason is MaintenanceRejection.TRUST_FAILED
    assert store.closed
    assert _snapshot(path) == before


@pytest.mark.parametrize("identity_target", ["main", "sidecar"])
def test_pre_operation_identity_loss_closes_without_sql_or_mutation(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
    identity_target: str,
) -> None:
    path, store = store_path
    before = _snapshot(path)
    if identity_target == "main":

        def changed_main(candidate: Path, *, expected: object | None = None) -> Any:
            del candidate, expected
            raise FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED)

        monkeypatch.setattr(store_module, "validate_database_file", changed_main)
    else:

        def changed_sidecars(candidate: Path, prior: object) -> Any:
            del candidate, prior
            raise FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED)

        monkeypatch.setattr(store_module, "_advance_sidecar_snapshot", changed_sidecars)
    with pytest.raises(MaintenanceError) as caught:
        store.cleanup_retention(now_utc_us=_NOW)
    assert caught.value.reason is MaintenanceRejection.TRUST_FAILED
    assert store.closed
    assert _snapshot(path) == before


def test_query_plans_use_exact_retention_and_foreign_key_indexes(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, _store = store_path
    cases = (
        (
            maintenance._HEALTH_RETENTION_CANDIDATES_SQL,
            (_CUTOFF, RETENTION_ROW_BUDGET),
            "idx_health_samples_observed",
        ),
        (
            maintenance._ARCHIVED_ALERT_CANDIDATES_SQL,
            (_CUTOFF, RETENTION_ROW_BUDGET),
            "idx_alerts_archived_recovered_id",
        ),
        (
            maintenance._ALERT_EVENTS_FOR_RETENTION_SQL,
            (1, RETENTION_ROW_BUDGET + 1),
            "idx_alert_events_alert_time",
        ),
        (
            "SELECT id FROM alerts INDEXED BY idx_alerts_first_sample_id "
            "WHERE first_sample_id = ?",
            (1,),
            "idx_alerts_first_sample_id",
        ),
        (
            "SELECT id FROM alerts INDEXED BY idx_alerts_latest_sample_id "
            "WHERE latest_sample_id = ?",
            (1,),
            "idx_alerts_latest_sample_id",
        ),
        (
            "SELECT id FROM alert_events "
            "INDEXED BY idx_alert_events_supporting_sample_id "
            "WHERE supporting_sample_id = ?",
            (1,),
            "idx_alert_events_supporting_sample_id",
        ),
    )
    for sql, parameters, expected_index in cases:
        plan = _rows(path, f"EXPLAIN QUERY PLAN {sql}", parameters)
        assert expected_index in " ".join(str(row[3]) for row in plan)


def test_public_maintenance_surface_is_fixed_bounded_and_disconnected() -> None:
    assert (
        DEFAULT_RETENTION_DAYS,
        MIN_RETENTION_DAYS,
        MAX_RETENTION_DAYS,
        RETENTION_ROW_BUDGET,
        INCREMENTAL_VACUUM_PAGES,
    ) == (30, 1, 365, 500, 128)
    cleanup_signature = inspect.signature(HealthHistoryStore.cleanup_retention)
    vacuum_signature = inspect.signature(HealthHistoryStore.incremental_vacuum)
    assert tuple(cleanup_signature.parameters) == (
        "self",
        "now_utc_us",
        "retention_days",
    )
    assert tuple(vacuum_signature.parameters) == ("self",)
    source = inspect.getsource(maintenance).lower()
    assert " offset " not in source
    assert "while " not in source
    assert "pragma incremental_vacuum(128)" in source
    assert 'execute("vacuum")' not in source
    assert "insert into alert_events" not in source
    assert "lifecycle = 'expired'" not in source
    assert "wal_checkpoint" not in source
    assert "subprocess" not in source
    assert "socket" not in source
    root = Path(__file__).parents[1] / "src" / "aurora_core"
    runtime_files = (
        root / "__main__.py",
        root / "dashboard" / "server.py",
        *sorted((root / "runtime").glob("*.py")),
    )
    for path in runtime_files:
        runtime_source = path.read_text(encoding="utf-8")
        assert "health_history.maintenance" not in runtime_source
        assert "m18_validation" not in runtime_source


def test_maintenance_result_models_are_immutable_and_validate_budget() -> None:
    result = RetentionCleanupResult(MaintenanceOutcome.COMPLETED, 1, 2, 3)
    assert result.logical_rows_deleted == 6
    with pytest.raises(FrozenInstanceError):
        result.alerts_deleted = 4  # type: ignore[misc]
    with pytest.raises(ValueError):
        RetentionCleanupResult(MaintenanceOutcome.COMPLETED, RETENTION_ROW_BUDGET, 1, 0)
    with pytest.raises(ValueError):
        IncrementalVacuumResult(MaintenanceOutcome.COMPLETED, 129, 1, 0)
    with pytest.raises(ValueError):
        IncrementalVacuumResult(
            MaintenanceOutcome.COMPLETED,
            INCREMENTAL_VACUUM_PAGES,
            INCREMENTAL_VACUUM_PAGES + 1,
            0,
        )
