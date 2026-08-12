"""Synthetic tests for bounded read-only Milestone 18 queries."""

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

import aurora_core.health_history.queries as queries
import aurora_core.health_history.store as store_module
from aurora_core.health_history import schema
from aurora_core.health_history.filesystem import (
    FilesystemBoundaryError,
    FilesystemRejection,
)
from aurora_core.health_history.lifecycle import ALERT_COOLDOWN_US
from aurora_core.health_history.models import (
    COMPONENT_ORDER,
    MAX_TIMESTAMP_US,
    AlertKind,
    AlertLifecycle,
    AlertScope,
    ComponentName,
    HealthHistoryStatus,
    LifecycleEvent,
    SampleKind,
)
from aurora_core.health_history.projection import (
    ComponentProjection,
    HealthProjection,
    _canonical_bytes,
)
from aurora_core.health_history.queries import (
    DEFAULT_ALERT_EVENT_PAGE_SIZE,
    DEFAULT_ALERT_PAGE_SIZE,
    DEFAULT_HEALTH_SAMPLE_PAGE_SIZE,
    MAX_ALERT_EVENT_PAGE_SIZE,
    MAX_ALERT_PAGE_SIZE,
    MAX_HEALTH_SAMPLE_PAGE_SIZE,
    AlertCursor,
    AlertEventCursor,
    HealthSampleCursor,
    QueryError,
    QueryRejection,
    QueryStage,
)
from aurora_core.health_history.reasons import NormalizedReason
from aurora_core.health_history.store import HealthHistoryStore, StoreError

_BASE_TIME = 1_786_000_000_000_000
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
    observed_at: int | None = None,
    recorded_at: int | None = None,
    statuses: dict[ComponentName, HealthHistoryStatus] | None = None,
    reasons: dict[ComponentName, tuple[NormalizedReason, ...]] | None = None,
    sample_kind: SampleKind = SampleKind.HEARTBEAT,
    missed_intervals: int = 0,
) -> HealthProjection:
    observed = observed_at if observed_at is not None else _BASE_TIME + sequence
    recorded = recorded_at if recorded_at is not None else observed + 1
    selected_statuses = {
        component: HealthHistoryStatus.HEALTHY for component in COMPONENT_ORDER
    }
    selected_statuses.update(statuses or {})
    components = tuple(
        ComponentProjection(
            component=component,
            status=selected_statuses[component],
            reasons=(reasons or {}).get(
                component,
                (_default_reason(component, selected_statuses[component]),),
            ),
            checked_at_utc_us=observed,
            latency_ms=sequence,
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
    overall = max((component.status for component in components), key=ordering.get)
    digest = hashlib.sha256(
        _canonical_bytes(
            observation_sequence=sequence,
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
        observation_sequence=sequence,
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


def _snapshot(path: Path) -> dict[str, list[Any]]:
    return {
        table: _rows(path, f"SELECT * FROM {table} ORDER BY rowid") for table in _TABLES
    }


def _insert_alert(
    path: Path,
    *,
    scope: AlertScope = AlertScope.OVERALL,
    kind: AlertKind = AlertKind.DEGRADED,
    lifecycle: AlertLifecycle = AlertLifecycle.OPEN,
    opened_at: int = _BASE_TIME,
    sample_id: int | None = None,
    acknowledged: bool = False,
    episode_count: int = 1,
    occurrence_count: int = 1,
) -> int:
    severity = (
        HealthHistoryStatus.DEGRADED
        if kind is AlertKind.DEGRADED
        else HealthHistoryStatus.UNAVAILABLE
    )
    acknowledged_at = (
        opened_at + 1
        if acknowledged or lifecycle is AlertLifecycle.ACKNOWLEDGED
        else None
    )
    recovered_at = (
        opened_at + 2
        if lifecycle in {AlertLifecycle.RECOVERED, AlertLifecycle.ARCHIVED}
        else None
    )
    cooldown_until = opened_at + ALERT_COOLDOWN_US
    archived_at = cooldown_until if lifecycle is AlertLifecycle.ARCHIVED else None
    connection = _connect(path)
    cursor = connection.execute(
        "INSERT INTO alerts("
        "scope, kind, lifecycle, severity, opened_at_utc_us, "
        "acknowledged_at_utc_us, recovered_at_utc_us, archived_at_utc_us, "
        "first_sample_id, latest_sample_id, episode_count, occurrence_count, "
        "cooldown_until_utc_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            scope.value,
            kind.value,
            lifecycle.value,
            severity.value,
            opened_at,
            acknowledged_at,
            recovered_at,
            archived_at,
            sample_id,
            sample_id,
            episode_count,
            occurrence_count,
            cooldown_until,
        ),
    )
    connection.close()
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _insert_event(
    path: Path,
    alert_id: int,
    event_type: LifecycleEvent,
    lifecycle: AlertLifecycle,
    *,
    event_at: int,
    sample_id: int | None = None,
) -> int:
    connection = _connect(path)
    cursor = connection.execute(
        "INSERT INTO alert_events("
        "alert_id, event_type, event_at_utc_us, supporting_sample_id, "
        "resulting_lifecycle) VALUES (?, ?, ?, ?, ?)",
        (alert_id, event_type.value, event_at, sample_id, lifecycle.value),
    )
    connection.close()
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _seed_archived_alerts(path: Path, count: int) -> list[int]:
    connection = _connect(path)
    connection.execute("BEGIN")
    ids: list[int] = []
    for index in range(count):
        opened_at = _BASE_TIME + index
        cooldown_until = opened_at + ALERT_COOLDOWN_US
        cursor = connection.execute(
            "INSERT INTO alerts("
            "scope, kind, lifecycle, severity, opened_at_utc_us, "
            "recovered_at_utc_us, archived_at_utc_us, episode_count, "
            "occurrence_count, cooldown_until_utc_us) "
            "VALUES ('overall', 'degraded', 'archived', 'degraded', "
            "?, ?, ?, 1, 1, ?)",
            (opened_at, opened_at + 1, cooldown_until, cooldown_until),
        )
        assert cursor.lastrowid is not None
        ids.append(cursor.lastrowid)
    connection.commit()
    connection.close()
    return ids


def _seed_open_occurrence_events(path: Path, alert_id: int, count: int) -> list[int]:
    connection = _connect(path)
    connection.execute("BEGIN")
    ids: list[int] = []
    for index in range(count):
        cursor = connection.execute(
            "INSERT INTO alert_events("
            "alert_id, event_type, event_at_utc_us, resulting_lifecycle) "
            "VALUES (?, 'occurrence_updated', ?, 'open')",
            (alert_id, _BASE_TIME + index),
        )
        assert cursor.lastrowid is not None
        ids.append(cursor.lastrowid)
    connection.commit()
    connection.close()
    return ids


def _stored_sample_id(path: Path, sequence: int) -> int:
    return cast(
        int,
        _rows(
            path,
            "SELECT id FROM health_samples WHERE observation_sequence = ?",
            (sequence,),
        )[0][0],
    )


def _seed_history(path: Path, projections: tuple[HealthProjection, ...]) -> None:
    connection = _connect(path)
    connection.execute("BEGIN")
    for projection in projections:
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
                SampleKind.TRANSITION.value,
                projection.sample_kind.value,
                projection.digest,
                projection.missed_intervals,
            ),
        )
        assert cursor.lastrowid is not None
        sample_id = cursor.lastrowid
        for component in projection.components:
            reason_values = tuple(reason.value for reason in component.reasons)
            padded_reasons = (*reason_values, None, None)[:3]
            connection.execute(
                "INSERT INTO component_samples("
                "sample_id, component, status, reason_code_1, reason_code_2, "
                "reason_code_3, checked_at_utc_us, latency_ms, "
                "last_successful_at_utc_us) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sample_id,
                    component.component.value,
                    component.status.value,
                    *padded_reasons,
                    component.checked_at_utc_us,
                    component.latency_ms,
                    component.last_successful_at_utc_us,
                ),
            )
    connection.commit()
    connection.close()


def _assert_malformed_query_closes(
    path: Path, store: HealthHistoryStore, operation: Any
) -> None:
    before = _snapshot(path)
    with pytest.raises(QueryError) as caught:
        operation()
    assert caught.value.reason is QueryRejection.MALFORMED_STATE
    assert str(caught.value) == "malformed_state"
    assert caught.value.__cause__ is None
    assert store.closed
    assert _snapshot(path) == before


def test_empty_query_pages_are_immutable_and_mutate_nothing(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    before = _snapshot(path)
    samples = store.list_health_samples()
    alerts = store.list_alerts()
    assert samples.items == () and samples.next_cursor is None
    assert alerts.items == () and alerts.next_cursor is None
    assert _snapshot(path) == before
    with pytest.raises(FrozenInstanceError):
        samples.items = ()  # type: ignore[misc]


def test_every_public_query_requires_an_open_verified_store(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    _path, store = store_path
    store.close()
    operations = (
        store.list_health_samples,
        store.list_alerts,
        lambda: store.get_alert(1),
        lambda: store.list_alert_events(1),
    )
    for operation in operations:
        with pytest.raises(StoreError) as caught:
            operation()
        assert caught.value.reason == "store_closed"


def test_health_sample_query_returns_approved_fields_and_four_ordered_components(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    projection = _projection(
        1,
        reasons={
            ComponentName.WLED: (
                NormalizedReason.WLED_INFO_LED_COUNT_MISMATCH,
                NormalizedReason.WLED_STATE_HTTP_ERROR,
            )
        },
        statuses={ComponentName.WLED: HealthHistoryStatus.DEGRADED},
    )
    store.ingest(projection)
    before = _snapshot(path)
    page = store.list_health_samples()
    assert len(page.items) == 1
    sample = page.items[0]
    assert sample.id == 1
    assert sample.observation_sequence == 1
    assert sample.observed_at_utc_us == projection.observed_at_utc_us
    assert sample.recorded_at_utc_us == projection.recorded_at_utc_us
    assert sample.overall_status is HealthHistoryStatus.DEGRADED
    assert sample.service_uptime_ms == 1_000
    assert sample.sample_kind is SampleKind.TRANSITION
    assert sample.accepted_sample_kind is SampleKind.HEARTBEAT
    assert sample.missed_intervals == 0
    assert tuple(component.component for component in sample.components) == (
        COMPONENT_ORDER
    )
    assert sample.components[0].reasons == projection.components[0].reasons
    assert not hasattr(sample, "projection_digest")
    assert _snapshot(path) == before


def test_health_history_newest_first_and_identical_times_use_descending_id(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    _path, store = store_path
    for sequence in range(1, 4):
        store.ingest(
            _projection(
                sequence,
                observed_at=_BASE_TIME,
                sample_kind=SampleKind.STARTUP_GAP,
            )
        )
    page = store.list_health_samples()
    assert [item.observation_sequence for item in page.items] == [3, 2, 1]


def test_health_history_page_sizes_and_keyset_traversal_have_no_gaps_or_duplicates(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    _seed_history(
        path,
        tuple(
            _projection(sequence, sample_kind=SampleKind.STARTUP_GAP)
            for sequence in range(1, 106)
        ),
    )
    assert len(store.list_health_samples().items) == DEFAULT_HEALTH_SAMPLE_PAGE_SIZE
    assert len(store.list_health_samples(page_size=1).items) == 1
    maximum = store.list_health_samples(page_size=MAX_HEALTH_SAMPLE_PAGE_SIZE)
    assert len(maximum.items) == MAX_HEALTH_SAMPLE_PAGE_SIZE
    assert maximum.next_cursor is not None

    seen: list[int] = []
    cursor = None
    while True:
        page = store.list_health_samples(page_size=17, cursor=cursor)
        seen.extend(item.observation_sequence for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert seen == list(range(105, 0, -1))
    assert len(seen) == len(set(seen))


@pytest.mark.parametrize(
    "value", [0, -1, True, 1.0, "1", MAX_HEALTH_SAMPLE_PAGE_SIZE + 1]
)
def test_health_history_rejects_invalid_page_sizes_without_mutation(
    store_path: tuple[Path, HealthHistoryStore], value: object
) -> None:
    path, store = store_path
    before = _snapshot(path)
    with pytest.raises(QueryError) as caught:
        store.list_health_samples(page_size=value)  # type: ignore[arg-type]
    assert caught.value.reason is QueryRejection.INVALID_QUERY
    assert not store.closed
    assert _snapshot(path) == before


def test_health_history_exact_status_filter_uses_same_cursor_contract(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    _path, store = store_path
    statuses = (
        HealthHistoryStatus.HEALTHY,
        HealthHistoryStatus.DEGRADED,
        HealthHistoryStatus.HEALTHY,
        HealthHistoryStatus.UNAVAILABLE,
        HealthHistoryStatus.HEALTHY,
    )
    for sequence, status in enumerate(statuses, 1):
        store.ingest(
            _projection(
                sequence,
                statuses={ComponentName.WLED: status},
                sample_kind=SampleKind.STARTUP_GAP,
            )
        )
    first = store.list_health_samples(
        page_size=2, overall_status=HealthHistoryStatus.HEALTHY
    )
    assert [item.observation_sequence for item in first.items] == [5, 3]
    assert first.next_cursor is not None
    second = store.list_health_samples(
        page_size=2,
        cursor=first.next_cursor,
        overall_status=HealthHistoryStatus.HEALTHY,
    )
    assert [item.observation_sequence for item in second.items] == [1]
    assert second.next_cursor is None
    with pytest.raises(QueryError) as caught:
        store.list_health_samples(overall_status="healthy")  # type: ignore[arg-type]
    assert caught.value.reason is QueryRejection.INVALID_QUERY


def test_marker_and_accepted_vs_stored_kinds_are_preserved(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    _path, store = store_path
    store.ingest(_projection(1))
    store.ingest(_projection(2, sample_kind=SampleKind.STARTUP_GAP))
    store.ingest(_projection(3, sample_kind=SampleKind.CLOCK_DISCONTINUITY))
    records = {
        item.observation_sequence: item for item in store.list_health_samples().items
    }
    assert records[1].sample_kind is SampleKind.TRANSITION
    assert records[1].accepted_sample_kind is SampleKind.HEARTBEAT
    assert records[2].sample_kind is SampleKind.STARTUP_GAP
    assert records[3].sample_kind is SampleKind.CLOCK_DISCONTINUITY


@pytest.mark.parametrize(
    "mutation",
    [
        "DELETE FROM component_samples WHERE component = 'wled'",
        "UPDATE component_samples SET component = 'invalid' WHERE component = 'wled'",
        "UPDATE component_samples SET reason_code_1 = 'invalid' "
        "WHERE component = 'wled'",
        "UPDATE component_samples SET reason_code_1 = 'wled.healthy' "
        "WHERE component = 'hyperhdr'",
        "UPDATE component_samples SET reason_code_2 = reason_code_1 "
        "WHERE component = 'wled'",
        "UPDATE component_samples SET reason_code_2 = 'wled.info.http_error', "
        "reason_code_3 = 'wled.state.http_error' WHERE component = 'wled'",
        "UPDATE component_samples SET status = 'invalid' WHERE component = 'wled'",
        "UPDATE health_samples SET overall_status = 'degraded'",
        "UPDATE health_samples SET sample_kind = 'invalid'",
        "UPDATE health_samples SET accepted_sample_kind = 'invalid'",
        "UPDATE health_samples SET missed_intervals = -1",
        "UPDATE health_samples SET observed_at_utc_us = -1",
        "UPDATE health_samples SET projection_digest = zeroblob(32)",
    ],
)
def test_malformed_history_rows_are_trust_loss_and_never_skipped(
    store_path: tuple[Path, HealthHistoryStore], mutation: str
) -> None:
    path, store = store_path
    store.ingest(_projection(1))
    connection = _connect(path)
    connection.execute("PRAGMA ignore_check_constraints = ON")
    connection.execute(mutation)
    connection.execute("PRAGMA ignore_check_constraints = OFF")
    connection.close()
    _assert_malformed_query_closes(path, store, store.list_health_samples)


def test_health_history_validates_malformed_digest_lookahead_before_returning_page(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    _seed_history(
        path,
        (
            _projection(1, sample_kind=SampleKind.STARTUP_GAP),
            _projection(2, sample_kind=SampleKind.STARTUP_GAP),
        ),
    )
    connection = _connect(path)
    connection.execute(
        "UPDATE health_samples SET projection_digest = zeroblob(32) "
        "WHERE observation_sequence = 1"
    )
    connection.close()

    _assert_malformed_query_closes(
        path, store, lambda: store.list_health_samples(page_size=1)
    )


def test_health_status_cursor_validates_malformed_digest_lookahead(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    _seed_history(
        path,
        tuple(
            _projection(sequence, sample_kind=SampleKind.STARTUP_GAP)
            for sequence in range(1, 4)
        ),
    )
    first = store.list_health_samples(
        page_size=1, overall_status=HealthHistoryStatus.HEALTHY
    )
    assert [item.observation_sequence for item in first.items] == [3]
    assert first.next_cursor is not None
    connection = _connect(path)
    connection.execute(
        "UPDATE health_samples SET projection_digest = zeroblob(32) "
        "WHERE observation_sequence = 1"
    )
    connection.close()

    _assert_malformed_query_closes(
        path,
        store,
        lambda: store.list_health_samples(
            page_size=1,
            cursor=first.next_cursor,
            overall_status=HealthHistoryStatus.HEALTHY,
        ),
    )


def test_health_history_invalid_cursor_is_sanitized_and_read_only(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    before = _snapshot(path)
    with pytest.raises(QueryError) as constructed:
        HealthSampleCursor(-1, 1)
    assert constructed.value.reason is QueryRejection.INVALID_CURSOR
    with pytest.raises(QueryError) as caught:
        store.list_health_samples(cursor=object())  # type: ignore[arg-type]
    assert caught.value.reason is QueryRejection.INVALID_CURSOR
    assert not store.closed
    assert _snapshot(path) == before


def test_alert_listing_validates_every_scope_kind_and_lifecycle(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    scopes = tuple(AlertScope)
    for index, scope in enumerate(scopes):
        kind = (
            AlertKind.SAMPLING_GAP
            if scope is AlertScope.SAMPLING
            else AlertKind.DEGRADED
        )
        _insert_alert(
            path,
            scope=scope,
            kind=kind,
            lifecycle=AlertLifecycle.ARCHIVED,
            opened_at=_BASE_TIME + index,
        )
    records = store.list_alerts().items
    assert {record.scope for record in records} == set(AlertScope)
    assert {record.kind for record in records} >= {
        AlertKind.DEGRADED,
        AlertKind.SAMPLING_GAP,
    }

    other_path = path.parent / "kinds.db"
    other = HealthHistoryStore.create(other_path, created_at_utc_us=1)
    try:
        _insert_alert(other_path, kind=AlertKind.DEGRADED)
        _insert_alert(other_path, kind=AlertKind.UNAVAILABLE)
        _insert_alert(
            other_path,
            scope=AlertScope.SAMPLING,
            kind=AlertKind.SAMPLING_GAP,
        )
        assert {record.kind for record in other.list_alerts().items} == set(AlertKind)
    finally:
        other.close()


def test_alert_listing_supports_all_lifecycles_and_safe_acknowledged_rows(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    combinations = (
        (AlertScope.WLED, AlertKind.DEGRADED, AlertLifecycle.OPEN),
        (AlertScope.HYPERHDR, AlertKind.DEGRADED, AlertLifecycle.ACKNOWLEDGED),
        (AlertScope.CAPTURE, AlertKind.DEGRADED, AlertLifecycle.RECOVERED),
        (AlertScope.RASPBERRY_PI, AlertKind.DEGRADED, AlertLifecycle.ARCHIVED),
    )
    for index, (scope, kind, lifecycle) in enumerate(combinations):
        _insert_alert(
            path,
            scope=scope,
            kind=kind,
            lifecycle=lifecycle,
            opened_at=_BASE_TIME + index,
        )
    records = store.list_alerts().items
    assert {record.lifecycle for record in records} == set(AlertLifecycle)
    acknowledged = next(
        record for record in records if record.lifecycle is AlertLifecycle.ACKNOWLEDGED
    )
    assert acknowledged.acknowledged_at_utc_us is not None


def test_alerts_are_newest_first_and_keyset_pages_use_descending_id_tiebreaker(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    ids = [
        _insert_alert(
            path,
            scope=scope,
            lifecycle=AlertLifecycle.ARCHIVED,
            opened_at=_BASE_TIME,
        )
        for scope in (AlertScope.WLED, AlertScope.HYPERHDR, AlertScope.CAPTURE)
    ]
    first = store.list_alerts(page_size=2)
    assert [record.id for record in first.items] == list(reversed(ids[-2:]))
    assert first.next_cursor is not None
    second = store.list_alerts(page_size=2, cursor=first.next_cursor)
    assert [record.id for record in second.items] == [ids[0]]
    assert second.next_cursor is None


def test_alert_default_and_maximum_page_sizes_remain_bounded(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    ids = _seed_archived_alerts(path, 105)
    default = store.list_alerts()
    maximum = store.list_alerts(page_size=MAX_ALERT_PAGE_SIZE)
    assert len(default.items) == DEFAULT_ALERT_PAGE_SIZE
    assert len(maximum.items) == MAX_ALERT_PAGE_SIZE
    assert [record.id for record in maximum.items] == list(reversed(ids[-100:]))
    assert maximum.next_cursor is not None


def test_alert_lifecycle_filter_and_multiple_simultaneous_kinds(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    degraded = _insert_alert(path, kind=AlertKind.DEGRADED)
    unavailable = _insert_alert(
        path, kind=AlertKind.UNAVAILABLE, opened_at=_BASE_TIME + 1
    )
    archived = _insert_alert(
        path,
        scope=AlertScope.WLED,
        kind=AlertKind.DEGRADED,
        lifecycle=AlertLifecycle.ARCHIVED,
        opened_at=_BASE_TIME + 2,
    )
    active = store.list_alerts(lifecycle=AlertLifecycle.OPEN)
    assert [record.id for record in active.items] == [unavailable, degraded]
    terminal = store.list_alerts(lifecycle=AlertLifecycle.ARCHIVED)
    assert [record.id for record in terminal.items] == [archived]
    with pytest.raises(QueryError) as caught:
        store.list_alerts(lifecycle="open")  # type: ignore[arg-type]
    assert caught.value.reason is QueryRejection.INVALID_QUERY


@pytest.mark.parametrize("value", [0, -1, True, 1.0, "1", MAX_ALERT_PAGE_SIZE + 1])
def test_alert_listing_rejects_invalid_page_sizes_without_mutation(
    store_path: tuple[Path, HealthHistoryStore], value: object
) -> None:
    path, store = store_path
    before = _snapshot(path)
    with pytest.raises(QueryError) as caught:
        store.list_alerts(page_size=value)  # type: ignore[arg-type]
    assert caught.value.reason is QueryRejection.INVALID_QUERY
    assert not store.closed
    assert _snapshot(path) == before


def test_get_alert_is_read_only_and_not_found_is_fixed(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    alert_id = _insert_alert(path)
    before = _snapshot(path)
    assert store.get_alert(alert_id).id == alert_id
    assert _snapshot(path) == before
    with pytest.raises(QueryError) as caught:
        store.get_alert(alert_id + 100)
    assert caught.value.reason is QueryRejection.NOT_FOUND
    assert str(caught.value) == "not_found"
    assert caught.value.__cause__ is None
    assert not store.closed
    assert _snapshot(path) == before


def test_get_alert_and_not_found_do_not_scan_or_expose_malformed_neighbors(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    valid_id = _insert_alert(path, scope=AlertScope.WLED)
    malformed_id = _insert_alert(path, scope=AlertScope.HYPERHDR)
    connection = _connect(path)
    connection.execute("PRAGMA ignore_check_constraints = ON")
    connection.execute(
        "UPDATE alerts SET severity = 'unavailable' WHERE id = ?",
        (malformed_id,),
    )
    connection.execute("PRAGMA ignore_check_constraints = OFF")
    connection.close()
    assert store.get_alert(valid_id).id == valid_id
    with pytest.raises(QueryError) as missing:
        store.get_alert(999)
    assert missing.value.reason is QueryRejection.NOT_FOUND
    assert not store.closed
    with pytest.raises(QueryError) as malformed:
        store.get_alert(malformed_id)
    assert malformed.value.reason is QueryRejection.MALFORMED_STATE
    assert store.closed


@pytest.mark.parametrize("value", [0, -1, True, 1.0, "1", MAX_TIMESTAMP_US + 1])
def test_get_alert_rejects_invalid_ids_without_mutation(
    store_path: tuple[Path, HealthHistoryStore], value: object
) -> None:
    path, store = store_path
    before = _snapshot(path)
    with pytest.raises(QueryError) as caught:
        store.get_alert(value)  # type: ignore[arg-type]
    assert caught.value.reason is QueryRejection.INVALID_QUERY
    assert not store.closed
    assert _snapshot(path) == before


def test_retention_cleared_alert_and_event_sample_references_are_valid(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    store.ingest(_projection(1))
    sample_id = _stored_sample_id(path, 1)
    alert_id = _insert_alert(path, sample_id=sample_id)
    _insert_event(
        path,
        alert_id,
        LifecycleEvent.OPENED,
        AlertLifecycle.OPEN,
        event_at=_BASE_TIME,
        sample_id=sample_id,
    )
    connection = _connect(path)
    connection.execute("DELETE FROM health_samples WHERE id = ?", (sample_id,))
    connection.close()
    alert = store.get_alert(alert_id)
    event = store.list_alert_events(alert_id).items[0]
    assert alert.first_sample_id is None and alert.latest_sample_id is None
    assert event.supporting_sample_id is None


@pytest.mark.parametrize(
    "assignment",
    [
        "lifecycle = 'acknowledged', acknowledged_at_utc_us = NULL",
        "severity = 'unavailable'",
        "scope = 'sampling'",
        "episode_count = 0",
        "occurrence_count = 0",
        "opened_at_utc_us = -1",
        "kind = 'invalid'",
        "lifecycle = 'archived', recovered_at_utc_us = 2, "
        "archived_at_utc_us = 3, cooldown_until_utc_us = 4",
    ],
)
def test_malformed_alert_rows_are_trust_loss(
    store_path: tuple[Path, HealthHistoryStore], assignment: str
) -> None:
    path, store = store_path
    _insert_alert(path)
    connection = _connect(path)
    connection.execute("PRAGMA ignore_check_constraints = ON")
    connection.execute(f"UPDATE alerts SET {assignment}")
    connection.execute("PRAGMA ignore_check_constraints = OFF")
    connection.close()
    _assert_malformed_query_closes(path, store, store.list_alerts)


def test_non_null_missing_alert_sample_reference_is_trust_loss(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    _insert_alert(path)
    connection = _connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("UPDATE alerts SET first_sample_id = 999")
    connection.close()
    _assert_malformed_query_closes(path, store, store.list_alerts)


def test_alert_listing_validates_malformed_lookahead_before_returning_page(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    lookahead_id = _insert_alert(
        path, lifecycle=AlertLifecycle.ARCHIVED, opened_at=_BASE_TIME
    )
    _insert_alert(path, lifecycle=AlertLifecycle.ARCHIVED, opened_at=_BASE_TIME + 1)
    connection = _connect(path)
    connection.execute("PRAGMA ignore_check_constraints = ON")
    connection.execute(
        "UPDATE alerts SET severity = 'unavailable' WHERE id = ?",
        (lookahead_id,),
    )
    connection.execute("PRAGMA ignore_check_constraints = OFF")
    connection.close()

    _assert_malformed_query_closes(path, store, lambda: store.list_alerts(page_size=1))


def test_alert_lifecycle_cursor_validates_malformed_lookahead(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    lookahead_id = _insert_alert(
        path, lifecycle=AlertLifecycle.ARCHIVED, opened_at=_BASE_TIME
    )
    _insert_alert(path, lifecycle=AlertLifecycle.ARCHIVED, opened_at=_BASE_TIME + 1)
    _insert_alert(path, lifecycle=AlertLifecycle.ARCHIVED, opened_at=_BASE_TIME + 2)
    first = store.list_alerts(page_size=1, lifecycle=AlertLifecycle.ARCHIVED)
    assert first.next_cursor is not None
    connection = _connect(path)
    connection.execute("PRAGMA ignore_check_constraints = ON")
    connection.execute(
        "UPDATE alerts SET severity = 'unavailable' WHERE id = ?",
        (lookahead_id,),
    )
    connection.execute("PRAGMA ignore_check_constraints = OFF")
    connection.close()

    _assert_malformed_query_closes(
        path,
        store,
        lambda: store.list_alerts(
            page_size=1,
            cursor=first.next_cursor,
            lifecycle=AlertLifecycle.ARCHIVED,
        ),
    )


def test_event_timeline_validates_fixed_events_and_orders_equal_times_by_id(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    alert_id = _insert_alert(path, lifecycle=AlertLifecycle.ARCHIVED)
    pairs = (
        (LifecycleEvent.OPENED, AlertLifecycle.OPEN),
        (LifecycleEvent.OCCURRENCE_UPDATED, AlertLifecycle.OPEN),
        (LifecycleEvent.ACKNOWLEDGED, AlertLifecycle.ACKNOWLEDGED),
        (LifecycleEvent.RECOVERED, AlertLifecycle.RECOVERED),
        (LifecycleEvent.ARCHIVED, AlertLifecycle.ARCHIVED),
    )
    ids = [
        _insert_event(
            path,
            alert_id,
            event,
            lifecycle,
            event_at=_BASE_TIME if index < 2 else _BASE_TIME + index,
        )
        for index, (event, lifecycle) in enumerate(pairs)
    ]
    before = _snapshot(path)
    page = store.list_alert_events(alert_id)
    assert [event.id for event in page.items] == ids
    assert [event.event_type for event in page.items] == [pair[0] for pair in pairs]
    assert _snapshot(path) == before


def test_event_timeline_keyset_pagination_has_no_gaps_or_duplicates(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    alert_id = _insert_alert(path)
    ids = [
        _insert_event(
            path,
            alert_id,
            LifecycleEvent.OCCURRENCE_UPDATED,
            AlertLifecycle.OPEN,
            event_at=_BASE_TIME + index // 2,
        )
        for index in range(7)
    ]
    seen: list[int] = []
    cursor = None
    while True:
        page = store.list_alert_events(alert_id, page_size=2, cursor=cursor)
        seen.extend(event.id for event in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert seen == ids
    assert len(seen) == len(set(seen))


def test_event_default_and_maximum_page_sizes_remain_bounded(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    alert_id = _insert_alert(path)
    ids = _seed_open_occurrence_events(path, alert_id, 105)
    default = store.list_alert_events(alert_id)
    maximum = store.list_alert_events(alert_id, page_size=MAX_ALERT_EVENT_PAGE_SIZE)
    assert len(default.items) == DEFAULT_ALERT_EVENT_PAGE_SIZE
    assert len(maximum.items) == MAX_ALERT_EVENT_PAGE_SIZE
    assert [event.id for event in maximum.items] == ids[:100]
    assert maximum.next_cursor is not None


@pytest.mark.parametrize(
    "lifecycle",
    [AlertLifecycle.OPEN, AlertLifecycle.ACKNOWLEDGED, AlertLifecycle.RECOVERED],
)
def test_occurrence_updated_event_allows_each_fixed_resulting_lifecycle(
    store_path: tuple[Path, HealthHistoryStore], lifecycle: AlertLifecycle
) -> None:
    path, store = store_path
    alert_id = _insert_alert(path, lifecycle=lifecycle)
    _insert_event(
        path,
        alert_id,
        LifecycleEvent.OCCURRENCE_UPDATED,
        lifecycle,
        event_at=_BASE_TIME,
    )
    assert store.list_alert_events(alert_id).items[0].resulting_lifecycle is lifecycle


@pytest.mark.parametrize(
    "value", [0, -1, True, 1.0, "1", MAX_ALERT_EVENT_PAGE_SIZE + 1]
)
def test_event_timeline_rejects_invalid_page_sizes_without_mutation(
    store_path: tuple[Path, HealthHistoryStore], value: object
) -> None:
    path, store = store_path
    before = _snapshot(path)
    with pytest.raises(QueryError) as caught:
        store.list_alert_events(1, page_size=value)  # type: ignore[arg-type]
    assert caught.value.reason is QueryRejection.INVALID_QUERY
    assert not store.closed
    assert _snapshot(path) == before


def test_event_timeline_missing_alert_is_fixed_not_found_and_read_only(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    before = _snapshot(path)
    with pytest.raises(QueryError) as caught:
        store.list_alert_events(999)
    assert caught.value.reason is QueryRejection.NOT_FOUND
    assert not store.closed
    assert _snapshot(path) == before


@pytest.mark.parametrize("value", [0, -1, True, 1.0, "1", MAX_TIMESTAMP_US + 1])
def test_event_timeline_rejects_invalid_alert_ids_without_mutation(
    store_path: tuple[Path, HealthHistoryStore], value: object
) -> None:
    path, store = store_path
    before = _snapshot(path)
    with pytest.raises(QueryError) as caught:
        store.list_alert_events(value)  # type: ignore[arg-type]
    assert caught.value.reason is QueryRejection.INVALID_QUERY
    assert not store.closed
    assert _snapshot(path) == before


def test_event_cursor_validation_is_fixed_and_read_only(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    alert_id = _insert_alert(path)
    before = _snapshot(path)
    with pytest.raises(QueryError) as constructed:
        AlertEventCursor(_BASE_TIME, 0)
    assert constructed.value.reason is QueryRejection.INVALID_CURSOR
    with pytest.raises(QueryError) as caught:
        store.list_alert_events(alert_id, cursor=object())  # type: ignore[arg-type]
    assert caught.value.reason is QueryRejection.INVALID_CURSOR
    assert not store.closed
    assert _snapshot(path) == before


def test_alert_cursor_validation_is_fixed_and_read_only(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    before = _snapshot(path)
    with pytest.raises(QueryError) as constructed:
        AlertCursor(-1, 1)
    assert constructed.value.reason is QueryRejection.INVALID_CURSOR
    with pytest.raises(QueryError) as caught:
        store.list_alerts(cursor=object())  # type: ignore[arg-type]
    assert caught.value.reason is QueryRejection.INVALID_CURSOR
    assert not store.closed
    assert _snapshot(path) == before


def test_malformed_event_lifecycle_pair_is_trust_loss(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    alert_id = _insert_alert(path)
    _insert_event(
        path,
        alert_id,
        LifecycleEvent.OPENED,
        AlertLifecycle.OPEN,
        event_at=_BASE_TIME,
    )
    connection = _connect(path)
    connection.execute("PRAGMA ignore_check_constraints = ON")
    connection.execute("UPDATE alert_events SET resulting_lifecycle = 'archived'")
    connection.execute("PRAGMA ignore_check_constraints = OFF")
    connection.close()
    _assert_malformed_query_closes(
        path, store, lambda: store.list_alert_events(alert_id)
    )


def test_non_null_missing_event_sample_reference_is_trust_loss(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    alert_id = _insert_alert(path)
    _insert_event(
        path,
        alert_id,
        LifecycleEvent.OPENED,
        AlertLifecycle.OPEN,
        event_at=_BASE_TIME,
    )
    connection = _connect(path)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("UPDATE alert_events SET supporting_sample_id = 999")
    connection.close()
    _assert_malformed_query_closes(
        path, store, lambda: store.list_alert_events(alert_id)
    )


def test_event_timeline_validates_malformed_lookahead_before_returning_page(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    alert_id = _insert_alert(path)
    _insert_event(
        path,
        alert_id,
        LifecycleEvent.OCCURRENCE_UPDATED,
        AlertLifecycle.OPEN,
        event_at=_BASE_TIME,
    )
    lookahead_id = _insert_event(
        path,
        alert_id,
        LifecycleEvent.OPENED,
        AlertLifecycle.OPEN,
        event_at=_BASE_TIME + 1,
    )
    connection = _connect(path)
    connection.execute("PRAGMA ignore_check_constraints = ON")
    connection.execute(
        "UPDATE alert_events SET resulting_lifecycle = 'archived' WHERE id = ?",
        (lookahead_id,),
    )
    connection.execute("PRAGMA ignore_check_constraints = OFF")
    connection.close()

    _assert_malformed_query_closes(
        path, store, lambda: store.list_alert_events(alert_id, page_size=1)
    )


def test_event_cursor_validates_malformed_lookahead(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    alert_id = _insert_alert(path)
    event_ids = [
        _insert_event(
            path,
            alert_id,
            LifecycleEvent.OCCURRENCE_UPDATED,
            AlertLifecycle.OPEN,
            event_at=_BASE_TIME + index,
        )
        for index in range(3)
    ]
    first = store.list_alert_events(alert_id, page_size=1)
    assert first.next_cursor is not None
    connection = _connect(path)
    connection.execute("PRAGMA ignore_check_constraints = ON")
    connection.execute(
        "UPDATE alert_events SET resulting_lifecycle = 'archived' WHERE id = ?",
        (event_ids[-1],),
    )
    connection.execute("PRAGMA ignore_check_constraints = OFF")
    connection.close()

    _assert_malformed_query_closes(
        path,
        store,
        lambda: store.list_alert_events(
            alert_id, page_size=1, cursor=first.next_cursor
        ),
    )


def test_mismatched_event_alert_id_is_not_returned_as_valid(
    store_path: tuple[Path, HealthHistoryStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, store = store_path
    requested = _insert_alert(path, scope=AlertScope.WLED)
    other = _insert_alert(path, scope=AlertScope.HYPERHDR)
    _insert_event(
        path,
        other,
        LifecycleEvent.OPENED,
        AlertLifecycle.OPEN,
        event_at=_BASE_TIME,
    )
    monkeypatch.setattr(
        queries,
        "_ALERT_EVENTS_SQL",
        f"SELECT {queries._ALERT_EVENT_COLUMNS} FROM alert_events "
        "WHERE ? IS NOT NULL ORDER BY event_at_utc_us, id LIMIT ?",
    )
    _assert_malformed_query_closes(
        path, store, lambda: store.list_alert_events(requested)
    )


@pytest.mark.parametrize("error_code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED])
def test_busy_and_locked_queries_are_sanitized_non_trust_failures(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
    error_code: int,
) -> None:
    path, store = store_path
    before = _snapshot(path)
    calls = 0

    def fail(stage: QueryStage) -> None:
        nonlocal calls
        calls += 1
        assert stage is QueryStage.HEALTH_SAMPLES
        error = sqlite3.OperationalError("private sqlite lock detail")
        error.sqlite_errorcode = error_code  # type: ignore[attr-defined]
        raise error

    monkeypatch.setattr(queries, "_fault", fail)
    with pytest.raises(QueryError) as caught:
        store.list_health_samples()
    assert caught.value.reason is QueryRejection.STORAGE_BUSY
    assert str(caught.value) == "storage_busy"
    assert caught.value.__cause__ is None
    assert calls == 1
    assert not store.closed
    assert _snapshot(path) == before


@pytest.mark.parametrize("error_code", [sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_SCHEMA])
def test_corruption_and_schema_query_errors_close_the_store(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
    error_code: int,
) -> None:
    path, store = store_path
    before = _snapshot(path)

    def fail(stage: QueryStage) -> None:
        del stage
        error = sqlite3.DatabaseError("private sqlite corruption detail")
        error.sqlite_errorcode = error_code  # type: ignore[attr-defined]
        raise error

    monkeypatch.setattr(queries, "_fault", fail)
    with pytest.raises(QueryError) as caught:
        store.list_alerts()
    assert caught.value.reason is QueryRejection.TRUST_FAILED
    assert str(caught.value) == "trust_failed"
    assert caught.value.__cause__ is None
    assert store.closed
    assert _snapshot(path) == before


def test_trust_losing_reader_skips_post_operation_identity_work(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, store = store_path
    before = _snapshot(path)
    original_main = store_module.validate_database_file
    original_sidecar = store_module._advance_sidecar_snapshot
    main_calls = 0
    sidecar_calls = 0

    def count_main(candidate: Path, *, expected: object | None = None) -> Any:
        nonlocal main_calls
        main_calls += 1
        return original_main(candidate, expected=expected)  # type: ignore[arg-type]

    def count_sidecar(candidate: Path, prior: object) -> Any:
        nonlocal sidecar_calls
        sidecar_calls += 1
        return original_sidecar(candidate, prior)  # type: ignore[arg-type]

    def fail(stage: QueryStage) -> None:
        assert stage is QueryStage.HEALTH_SAMPLES
        error = sqlite3.DatabaseError("private sqlite corruption detail")
        error.sqlite_errorcode = sqlite3.SQLITE_CORRUPT  # type: ignore[attr-defined]
        raise error

    monkeypatch.setattr(store_module, "validate_database_file", count_main)
    monkeypatch.setattr(store_module, "_advance_sidecar_snapshot", count_sidecar)
    monkeypatch.setattr(queries, "_fault", fail)
    with pytest.raises(QueryError) as caught:
        store.list_health_samples()
    assert caught.value.reason is QueryRejection.TRUST_FAILED
    assert main_calls == 1
    assert sidecar_calls == 1
    assert store.closed
    assert _snapshot(path) == before


def test_generic_sqlite_query_error_is_sanitized_without_raw_text(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, store = store_path
    before = _snapshot(path)

    def fail(stage: QueryStage) -> None:
        del stage
        raise sqlite3.OperationalError("private database path and SQL detail")

    monkeypatch.setattr(queries, "_fault", fail)
    with pytest.raises(QueryError) as caught:
        store.list_health_samples()
    assert caught.value.reason is QueryRejection.PERSISTENCE_FAILED
    assert str(caught.value) == "persistence_failed"
    assert caught.value.__cause__ is None
    assert not store.closed
    assert _snapshot(path) == before


@pytest.mark.parametrize("identity_target", ["main", "sidecar"])
@pytest.mark.parametrize(
    "operation_case",
    [
        "successful_health",
        "get_alert_not_found",
        "alert_events_not_found",
        "busy",
        "locked",
        "persistence_failed",
        "invalid_query",
        "invalid_cursor",
    ],
)
def test_post_operation_identity_replacement_overrides_non_trust_query_result(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
    identity_target: str,
    operation_case: str,
) -> None:
    path, store = store_path
    before = _snapshot(path)

    if operation_case in {"busy", "locked", "persistence_failed"}:

        def fail(stage: QueryStage) -> None:
            assert stage is QueryStage.HEALTH_SAMPLES
            error = sqlite3.OperationalError("private sqlite query detail")
            if operation_case == "busy":
                error.sqlite_errorcode = sqlite3.SQLITE_BUSY  # type: ignore[attr-defined]
            elif operation_case == "locked":
                error.sqlite_errorcode = sqlite3.SQLITE_LOCKED  # type: ignore[attr-defined]
            raise error

        monkeypatch.setattr(queries, "_fault", fail)

    if identity_target == "main":
        original_main = store_module.validate_database_file
        main_calls = 0

        def changed_main(candidate: Path, *, expected: object | None = None) -> Any:
            nonlocal main_calls
            main_calls += 1
            if main_calls == 2:
                raise FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED)
            return original_main(candidate, expected=expected)  # type: ignore[arg-type]

        monkeypatch.setattr(store_module, "validate_database_file", changed_main)
    else:
        original_sidecar = store_module._advance_sidecar_snapshot
        sidecar_calls = 0

        def changed_sidecar(candidate: Path, prior: object) -> Any:
            nonlocal sidecar_calls
            sidecar_calls += 1
            if sidecar_calls == 2:
                raise FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED)
            return original_sidecar(candidate, prior)  # type: ignore[arg-type]

        monkeypatch.setattr(store_module, "_advance_sidecar_snapshot", changed_sidecar)

    operation = {
        "successful_health": store.list_health_samples,
        "get_alert_not_found": lambda: store.get_alert(999),
        "alert_events_not_found": lambda: store.list_alert_events(999),
        "busy": store.list_health_samples,
        "locked": store.list_health_samples,
        "persistence_failed": store.list_health_samples,
        "invalid_query": lambda: store.list_health_samples(page_size=0),
        "invalid_cursor": lambda: store.list_health_samples(  # type: ignore[arg-type]
            cursor=object()
        ),
    }[operation_case]
    with pytest.raises(QueryError) as caught:
        operation()
    assert caught.value.reason is QueryRejection.TRUST_FAILED
    assert str(caught.value) == "trust_failed"
    assert caught.value.__cause__ is None
    assert store.closed
    assert _snapshot(path) == before


@pytest.mark.parametrize("failure_call", [1, 2])
def test_main_identity_rejection_before_or_after_query_closes_store(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
    failure_call: int,
) -> None:
    path, store = store_path
    before = _snapshot(path)
    original = store_module.validate_database_file
    calls = 0

    def changed(candidate: Path, *, expected: object | None = None) -> Any:
        nonlocal calls
        calls += 1
        if calls == failure_call:
            raise FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED)
        return original(candidate, expected=expected)  # type: ignore[arg-type]

    monkeypatch.setattr(store_module, "validate_database_file", changed)
    with pytest.raises(QueryError) as caught:
        store.list_health_samples()
    assert caught.value.reason is QueryRejection.TRUST_FAILED
    assert caught.value.__cause__ is None
    assert store.closed
    assert _snapshot(path) == before


@pytest.mark.parametrize("failure_call", [1, 2, 3])
def test_sidecar_identity_rejection_before_or_after_query_closes_store(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
    failure_call: int,
) -> None:
    path, store = store_path
    before = _snapshot(path)
    original = store_module._advance_sidecar_snapshot
    calls = 0

    def changed(candidate: Path, prior: object) -> Any:
        nonlocal calls
        calls += 1
        if calls == failure_call:
            raise FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED)
        return original(candidate, prior)  # type: ignore[arg-type]

    monkeypatch.setattr(store_module, "_advance_sidecar_snapshot", changed)
    with pytest.raises(QueryError) as caught:
        store.list_alerts()
    assert caught.value.reason is QueryRejection.TRUST_FAILED
    assert caught.value.__cause__ is None
    assert store.closed
    assert _snapshot(path) == before


def test_exact_query_plans_use_code_owned_indexes(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, _store = store_path
    cases = (
        (
            queries._HEALTH_SAMPLES_SQL,
            (DEFAULT_HEALTH_SAMPLE_PAGE_SIZE + 1,),
            "idx_health_samples_observed",
        ),
        (
            queries._HEALTH_SAMPLES_STATUS_SQL,
            (HealthHistoryStatus.HEALTHY.value, DEFAULT_HEALTH_SAMPLE_PAGE_SIZE + 1),
            "idx_health_samples_status_observed",
        ),
        (
            queries._HEALTH_SAMPLES_STATUS_CURSOR_SQL,
            (
                HealthHistoryStatus.HEALTHY.value,
                _BASE_TIME,
                1,
                DEFAULT_HEALTH_SAMPLE_PAGE_SIZE + 1,
            ),
            "idx_health_samples_status_observed",
        ),
        (
            queries._ALERTS_SQL,
            (DEFAULT_ALERT_PAGE_SIZE + 1,),
            "idx_alerts_opened",
        ),
        (
            queries._ALERTS_LIFECYCLE_SQL,
            (AlertLifecycle.OPEN.value, DEFAULT_ALERT_PAGE_SIZE + 1),
            "idx_alerts_lifecycle_opened",
        ),
        (
            queries._ALERT_EVENTS_SQL,
            (1, DEFAULT_ALERT_EVENT_PAGE_SIZE + 1),
            "idx_alert_events_alert_time",
        ),
        (
            queries._HEALTH_SAMPLES_CURSOR_SQL,
            (_BASE_TIME, 1, DEFAULT_HEALTH_SAMPLE_PAGE_SIZE + 1),
            "idx_health_samples_observed",
        ),
        (
            queries._ALERTS_CURSOR_SQL,
            (_BASE_TIME, 1, DEFAULT_ALERT_PAGE_SIZE + 1),
            "idx_alerts_opened",
        ),
        (
            queries._ALERTS_LIFECYCLE_CURSOR_SQL,
            (
                AlertLifecycle.OPEN.value,
                _BASE_TIME,
                1,
                DEFAULT_ALERT_PAGE_SIZE + 1,
            ),
            "idx_alerts_lifecycle_opened",
        ),
        (
            queries._ALERT_EVENTS_CURSOR_SQL,
            (1, _BASE_TIME, 1, DEFAULT_ALERT_EVENT_PAGE_SIZE + 1),
            "idx_alert_events_alert_time",
        ),
    )
    for sql, parameters, expected_index in cases:
        plan = _rows(path, f"EXPLAIN QUERY PLAN {sql}", parameters)
        assert expected_index in " ".join(str(row[3]) for row in plan)

    primary_key_cases = (
        (queries._ALERT_BY_ID_SQL, (1,), "SEARCH alerts USING INTEGER PRIMARY KEY"),
        (
            queries._ALERT_SAMPLE_REFERENCES_SQL,
            (1, 2),
            "SEARCH health_samples USING INTEGER PRIMARY KEY",
        ),
        (
            queries._EVENT_SAMPLE_REFERENCE_SQL,
            (1,),
            "SEARCH health_samples USING INTEGER PRIMARY KEY",
        ),
    )
    for sql, parameters, expected in primary_key_cases:
        plan = _rows(path, f"EXPLAIN QUERY PLAN {sql}", parameters)
        assert expected in " ".join(str(row[3]) for row in plan)
    component_plan = _rows(
        path,
        f"EXPLAIN QUERY PLAN {queries._COMPONENTS_FOR_SAMPLE_SQL}",
        (1,),
    )
    assert "sqlite_autoindex_component_samples_1" in " ".join(
        str(row[3]) for row in component_plan
    )


def test_every_successful_public_read_leaves_all_schema_v1_tables_unchanged(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    store.ingest(_projection(1))
    sample_id = _stored_sample_id(path, 1)
    alert_id = _insert_alert(path, sample_id=sample_id)
    _insert_event(
        path,
        alert_id,
        LifecycleEvent.OPENED,
        AlertLifecycle.OPEN,
        event_at=_BASE_TIME,
        sample_id=sample_id,
    )
    before = _snapshot(path)
    assert store.list_health_samples().items
    assert store.list_alerts().items
    assert store.get_alert(alert_id).id == alert_id
    assert store.list_alert_events(alert_id).items
    assert _snapshot(path) == before


def test_public_query_surface_has_no_generic_sql_offset_or_mutation_method() -> None:
    for name in (
        "execute",
        "query",
        "search",
        "acknowledge",
        "archive",
        "delete_history",
        "retain",
        "vacuum",
    ):
        assert not hasattr(HealthHistoryStore, name)
    source = inspect.getsource(queries)
    assert " OFFSET " not in source.upper()
    assert "BEGIN IMMEDIATE" not in source
    assert "subprocess" not in source
    assert "socket" not in source
    assert "rejected_transition" not in {event.value for event in LifecycleEvent}


def test_cursor_models_contain_only_fixed_bounded_ordering_integers() -> None:
    health = HealthSampleCursor(_BASE_TIME, 1)
    alert = AlertCursor(_BASE_TIME, 2)
    event = AlertEventCursor(_BASE_TIME, 3)
    assert tuple(health.__dataclass_fields__) == ("observed_at_utc_us", "sample_id")
    assert tuple(alert.__dataclass_fields__) == ("opened_at_utc_us", "alert_id")
    assert tuple(event.__dataclass_fields__) == ("event_at_utc_us", "event_id")
    with pytest.raises(FrozenInstanceError):
        health.sample_id = 2  # type: ignore[misc]


def test_query_limit_constants_are_fixed_and_bounded() -> None:
    assert (
        DEFAULT_HEALTH_SAMPLE_PAGE_SIZE,
        MAX_HEALTH_SAMPLE_PAGE_SIZE,
        DEFAULT_ALERT_PAGE_SIZE,
        MAX_ALERT_PAGE_SIZE,
        DEFAULT_ALERT_EVENT_PAGE_SIZE,
        MAX_ALERT_EVENT_PAGE_SIZE,
    ) == (50, 100, 50, 100, 50, 100)


def test_runtime_entry_points_do_not_import_health_history_query_layer() -> None:
    root = Path(__file__).parents[1] / "src" / "aurora_core"
    runtime_files = (
        root / "__main__.py",
        root / "dashboard" / "server.py",
        *sorted((root / "runtime").glob("*.py")),
    )
    for path in runtime_files:
        source = path.read_text(encoding="utf-8")
        assert "health_history" not in source
        assert "m18_validation" not in source


def test_schema_identity_version_and_exact_query_indexes_remain_v1(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, _store = store_path
    assert _rows(path, "PRAGMA application_id") == [(0x41555248,)]
    assert _rows(path, "PRAGMA user_version") == [(1,)]
    indexes = {
        row[0]
        for row in _rows(
            path,
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND name NOT LIKE 'sqlite_%'",
        )
    }
    assert "idx_alerts_opened" in indexes
    assert "idx_alerts_lifecycle_opened" in indexes
    connection = _connect(path)
    schema.verify_schema_v1(connection)
    connection.close()
