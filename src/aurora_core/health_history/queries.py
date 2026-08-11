"""Bounded read-only queries for validated production health-history rows."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, cast

from aurora_core.health_history.ingestion import (
    IngestionError,
    _read_checkpoint,
    _validate_replay_anchor,
)
from aurora_core.health_history.lifecycle import AutomaticAlertState
from aurora_core.health_history.models import (
    COMPONENT_ORDER,
    MAX_BOUNDED_COUNTER,
    MAX_OBSERVATION_SEQUENCE,
    MAX_SERVICE_UPTIME_MS,
    MAX_TIMESTAMP_US,
    PROJECTION_DIGEST_BYTES,
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
    ProjectionError,
    validate_health_projection,
)
from aurora_core.health_history.reasons import NormalizedReason

DEFAULT_HEALTH_SAMPLE_PAGE_SIZE: Final = 50
MAX_HEALTH_SAMPLE_PAGE_SIZE: Final = 100
DEFAULT_ALERT_PAGE_SIZE: Final = 50
MAX_ALERT_PAGE_SIZE: Final = 100
DEFAULT_ALERT_EVENT_PAGE_SIZE: Final = 50
MAX_ALERT_EVENT_PAGE_SIZE: Final = 100

_HEALTH_SAMPLE_COLUMNS: Final = (
    "id, observation_sequence, observed_at_utc_us, recorded_at_utc_us, "
    "overall_status, service_uptime_ms, sample_kind, accepted_sample_kind, "
    "projection_digest, missed_intervals"
)
_HEALTH_SAMPLES_SQL: Final = (
    f"SELECT {_HEALTH_SAMPLE_COLUMNS} FROM health_samples "
    "ORDER BY observed_at_utc_us DESC, id DESC LIMIT ?"
)
_HEALTH_SAMPLES_CURSOR_SQL: Final = (
    f"SELECT {_HEALTH_SAMPLE_COLUMNS} FROM health_samples "
    "WHERE (observed_at_utc_us, id) < (?, ?) "
    "ORDER BY observed_at_utc_us DESC, id DESC LIMIT ?"
)
_HEALTH_SAMPLES_STATUS_SQL: Final = (
    f"SELECT {_HEALTH_SAMPLE_COLUMNS} FROM health_samples "
    "WHERE overall_status = ? "
    "ORDER BY observed_at_utc_us DESC, id DESC LIMIT ?"
)
_HEALTH_SAMPLES_STATUS_CURSOR_SQL: Final = (
    f"SELECT {_HEALTH_SAMPLE_COLUMNS} FROM health_samples "
    "WHERE overall_status = ? AND (observed_at_utc_us, id) < (?, ?) "
    "ORDER BY observed_at_utc_us DESC, id DESC LIMIT ?"
)
_COMPONENTS_FOR_SAMPLE_SQL: Final = (
    "SELECT component, status, reason_code_1, reason_code_2, reason_code_3, "
    "checked_at_utc_us, latency_ms, last_successful_at_utc_us "
    "FROM component_samples WHERE sample_id = ? LIMIT 5"
)

_ALERT_COLUMNS: Final = (
    "id, scope, kind, lifecycle, severity, opened_at_utc_us, "
    "acknowledged_at_utc_us, recovered_at_utc_us, archived_at_utc_us, "
    "first_sample_id, latest_sample_id, episode_count, occurrence_count, "
    "cooldown_until_utc_us"
)
_ALERTS_SQL: Final = (
    f"SELECT {_ALERT_COLUMNS} FROM alerts "
    "ORDER BY opened_at_utc_us DESC, id DESC LIMIT ?"
)
_ALERTS_CURSOR_SQL: Final = (
    f"SELECT {_ALERT_COLUMNS} FROM alerts "
    "WHERE (opened_at_utc_us, id) < (?, ?) "
    "ORDER BY opened_at_utc_us DESC, id DESC LIMIT ?"
)
_ALERTS_LIFECYCLE_SQL: Final = (
    f"SELECT {_ALERT_COLUMNS} FROM alerts WHERE lifecycle = ? "
    "ORDER BY opened_at_utc_us DESC, id DESC LIMIT ?"
)
_ALERTS_LIFECYCLE_CURSOR_SQL: Final = (
    f"SELECT {_ALERT_COLUMNS} FROM alerts "
    "WHERE lifecycle = ? AND (opened_at_utc_us, id) < (?, ?) "
    "ORDER BY opened_at_utc_us DESC, id DESC LIMIT ?"
)
_ALERT_BY_ID_SQL: Final = f"SELECT {_ALERT_COLUMNS} FROM alerts WHERE id = ? LIMIT 1"
_ALERT_SAMPLE_REFERENCES_SQL: Final = (
    "SELECT id FROM health_samples WHERE id IN (?, ?) LIMIT 3"
)

_ALERT_EVENT_COLUMNS: Final = (
    "id, alert_id, event_type, event_at_utc_us, supporting_sample_id, "
    "resulting_lifecycle"
)
_ALERT_EVENTS_SQL: Final = (
    f"SELECT {_ALERT_EVENT_COLUMNS} FROM alert_events WHERE alert_id = ? "
    "ORDER BY event_at_utc_us, id LIMIT ?"
)
_ALERT_EVENTS_CURSOR_SQL: Final = (
    f"SELECT {_ALERT_EVENT_COLUMNS} FROM alert_events "
    "WHERE alert_id = ? AND (event_at_utc_us, id) > (?, ?) "
    "ORDER BY event_at_utc_us, id LIMIT ?"
)
_EVENT_SAMPLE_REFERENCE_SQL: Final = (
    "SELECT id FROM health_samples WHERE id = ? LIMIT 2"
)


class QueryRejection(StrEnum):
    """Fixed sanitized read-query rejection registry."""

    INVALID_QUERY = "invalid_query"
    INVALID_CURSOR = "invalid_cursor"
    NOT_FOUND = "not_found"
    STORAGE_BUSY = "storage_busy"
    MALFORMED_STATE = "malformed_state"
    TRUST_FAILED = "trust_failed"
    PERSISTENCE_FAILED = "persistence_failed"


class QueryError(Exception):
    """Sanitized query failure with no persisted or submitted detail."""

    def __init__(self, reason: QueryRejection, *, trust_lost: bool = False) -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.trust_lost = trust_lost


@dataclass(frozen=True, slots=True)
class SchedulerResumeState:
    """Bounded scheduler fields from the authoritative ingestion checkpoint."""

    last_committed_sequence: int | None
    last_accepted_observed_at_utc_us: int | None
    last_accepted_sample_kind: SampleKind | None
    accepted_observation_count: int

    def __post_init__(self) -> None:
        count = self.accepted_observation_count
        if type(count) is not int or not 0 <= count <= MAX_BOUNDED_COUNTER:
            raise ValueError("invalid_scheduler_resume_state")
        empty = (
            self.last_committed_sequence is None
            and self.last_accepted_observed_at_utc_us is None
            and self.last_accepted_sample_kind is None
            and count == 0
        )
        populated = (
            type(self.last_committed_sequence) is int
            and 0 <= self.last_committed_sequence <= MAX_OBSERVATION_SEQUENCE
            and type(self.last_accepted_observed_at_utc_us) is int
            and 0 <= self.last_accepted_observed_at_utc_us <= MAX_TIMESTAMP_US
            and isinstance(self.last_accepted_sample_kind, SampleKind)
            and count >= 1
        )
        if not (empty or populated):
            raise ValueError("invalid_scheduler_resume_state")


@dataclass(frozen=True, slots=True)
class HealthSampleCursor:
    observed_at_utc_us: int
    sample_id: int

    def __post_init__(self) -> None:
        _validate_cursor_values(self.observed_at_utc_us, self.sample_id)


@dataclass(frozen=True, slots=True)
class AlertCursor:
    opened_at_utc_us: int
    alert_id: int

    def __post_init__(self) -> None:
        _validate_cursor_values(self.opened_at_utc_us, self.alert_id)


@dataclass(frozen=True, slots=True)
class AlertEventCursor:
    event_at_utc_us: int
    event_id: int

    def __post_init__(self) -> None:
        _validate_cursor_values(self.event_at_utc_us, self.event_id)


@dataclass(frozen=True, slots=True)
class HealthComponentRecord:
    component: ComponentName
    status: HealthHistoryStatus
    reasons: tuple[NormalizedReason, ...]
    checked_at_utc_us: int
    latency_ms: int
    last_successful_at_utc_us: int | None

    def __post_init__(self) -> None:
        ComponentProjection(
            component=self.component,
            status=self.status,
            reasons=self.reasons,
            checked_at_utc_us=self.checked_at_utc_us,
            latency_ms=self.latency_ms,
            last_successful_at_utc_us=self.last_successful_at_utc_us,
        )


@dataclass(frozen=True, slots=True)
class HealthSampleRecord:
    id: int
    observation_sequence: int
    observed_at_utc_us: int
    recorded_at_utc_us: int
    overall_status: HealthHistoryStatus
    service_uptime_ms: int
    sample_kind: SampleKind
    accepted_sample_kind: SampleKind
    missed_intervals: int
    components: tuple[HealthComponentRecord, ...]

    def __post_init__(self) -> None:
        _positive_id(self.id)
        _bounded_integer(
            self.observation_sequence,
            MAX_OBSERVATION_SEQUENCE,
        )
        _bounded_integer(self.observed_at_utc_us, MAX_TIMESTAMP_US)
        _bounded_integer(self.recorded_at_utc_us, MAX_TIMESTAMP_US)
        _bounded_integer(self.service_uptime_ms, MAX_SERVICE_UPTIME_MS)
        _bounded_integer(self.missed_intervals, MAX_BOUNDED_COUNTER)
        if not isinstance(self.overall_status, HealthHistoryStatus):
            raise ValueError("invalid_status")
        if not isinstance(self.sample_kind, SampleKind) or not isinstance(
            self.accepted_sample_kind, SampleKind
        ):
            raise ValueError("invalid_sample_kind")
        if (
            type(self.components) is not tuple
            or tuple(component.component for component in self.components)
            != COMPONENT_ORDER
        ):
            raise ValueError("invalid_components")
        ordering = {
            HealthHistoryStatus.HEALTHY: 0,
            HealthHistoryStatus.DEGRADED: 1,
            HealthHistoryStatus.UNAVAILABLE: 2,
        }
        worst = max(
            (component.status for component in self.components),
            key=ordering.__getitem__,
        )
        if worst is not self.overall_status:
            raise ValueError("inconsistent_status")


@dataclass(frozen=True, slots=True)
class HealthSamplePage:
    items: tuple[HealthSampleRecord, ...]
    next_cursor: HealthSampleCursor | None

    def __post_init__(self) -> None:
        if type(self.items) is not tuple or any(
            type(item) is not HealthSampleRecord for item in self.items
        ):
            raise ValueError("invalid_page")
        if (
            self.next_cursor is not None
            and type(self.next_cursor) is not HealthSampleCursor
        ):
            raise ValueError("invalid_page_cursor")


@dataclass(frozen=True, slots=True)
class AlertRecord:
    id: int
    scope: AlertScope
    kind: AlertKind
    lifecycle: AlertLifecycle
    severity: HealthHistoryStatus
    opened_at_utc_us: int
    acknowledged_at_utc_us: int | None
    recovered_at_utc_us: int | None
    archived_at_utc_us: int | None
    first_sample_id: int | None
    latest_sample_id: int | None
    episode_count: int
    occurrence_count: int
    cooldown_until_utc_us: int

    def __post_init__(self) -> None:
        _validate_alert_record(self)


@dataclass(frozen=True, slots=True)
class AlertPage:
    items: tuple[AlertRecord, ...]
    next_cursor: AlertCursor | None

    def __post_init__(self) -> None:
        if type(self.items) is not tuple or any(
            type(item) is not AlertRecord for item in self.items
        ):
            raise ValueError("invalid_page")
        if self.next_cursor is not None and type(self.next_cursor) is not AlertCursor:
            raise ValueError("invalid_page_cursor")


@dataclass(frozen=True, slots=True)
class AlertEventRecord:
    id: int
    alert_id: int
    event_type: LifecycleEvent
    event_at_utc_us: int
    supporting_sample_id: int | None
    resulting_lifecycle: AlertLifecycle

    def __post_init__(self) -> None:
        _positive_id(self.id)
        _positive_id(self.alert_id)
        _bounded_integer(self.event_at_utc_us, MAX_TIMESTAMP_US)
        if self.supporting_sample_id is not None:
            _positive_id(self.supporting_sample_id)
        if not isinstance(self.event_type, LifecycleEvent) or not isinstance(
            self.resulting_lifecycle, AlertLifecycle
        ):
            raise ValueError("invalid_event_enum")
        allowed = {
            LifecycleEvent.OPENED: {AlertLifecycle.OPEN},
            LifecycleEvent.ACKNOWLEDGED: {AlertLifecycle.ACKNOWLEDGED},
            LifecycleEvent.RECOVERED: {AlertLifecycle.RECOVERED},
            LifecycleEvent.ARCHIVED: {AlertLifecycle.ARCHIVED},
            LifecycleEvent.OCCURRENCE_UPDATED: {
                AlertLifecycle.OPEN,
                AlertLifecycle.ACKNOWLEDGED,
                AlertLifecycle.RECOVERED,
            },
        }
        if self.resulting_lifecycle not in allowed[self.event_type]:
            raise ValueError("inconsistent_event_lifecycle")


@dataclass(frozen=True, slots=True)
class AlertEventPage:
    items: tuple[AlertEventRecord, ...]
    next_cursor: AlertEventCursor | None

    def __post_init__(self) -> None:
        if type(self.items) is not tuple or any(
            type(item) is not AlertEventRecord for item in self.items
        ):
            raise ValueError("invalid_page")
        if (
            self.next_cursor is not None
            and type(self.next_cursor) is not AlertEventCursor
        ):
            raise ValueError("invalid_page_cursor")


def _list_health_samples(
    connection: sqlite3.Connection,
    *,
    page_size: int = DEFAULT_HEALTH_SAMPLE_PAGE_SIZE,
    cursor: HealthSampleCursor | None = None,
    overall_status: HealthHistoryStatus | None = None,
) -> HealthSamplePage:
    """Return one bounded newest-first validated sample page."""
    _page_size(page_size, MAX_HEALTH_SAMPLE_PAGE_SIZE)
    if cursor is not None and type(cursor) is not HealthSampleCursor:
        raise QueryError(QueryRejection.INVALID_CURSOR)
    if overall_status is not None and not isinstance(
        overall_status, HealthHistoryStatus
    ):
        raise QueryError(QueryRejection.INVALID_QUERY)

    def read() -> HealthSamplePage:
        _fault(QueryStage.HEALTH_SAMPLES)
        fetch_limit = page_size + 1
        if overall_status is None and cursor is None:
            rows = connection.execute(_HEALTH_SAMPLES_SQL, (fetch_limit,)).fetchall()
        elif overall_status is None:
            assert cursor is not None
            rows = connection.execute(
                _HEALTH_SAMPLES_CURSOR_SQL,
                (cursor.observed_at_utc_us, cursor.sample_id, fetch_limit),
            ).fetchall()
        elif cursor is None:
            rows = connection.execute(
                _HEALTH_SAMPLES_STATUS_SQL,
                (overall_status.value, fetch_limit),
            ).fetchall()
        else:
            rows = connection.execute(
                _HEALTH_SAMPLES_STATUS_CURSOR_SQL,
                (
                    overall_status.value,
                    cursor.observed_at_utc_us,
                    cursor.sample_id,
                    fetch_limit,
                ),
            ).fetchall()
        validated_records = tuple(
            _health_sample_record(connection, row) for row in rows
        )
        has_more = len(validated_records) > page_size
        records = validated_records[:page_size]
        next_cursor = (
            HealthSampleCursor(records[-1].observed_at_utc_us, records[-1].id)
            if has_more and records
            else None
        )
        return HealthSamplePage(records, next_cursor)

    return _read_transaction(connection, read)


def _list_alerts(
    connection: sqlite3.Connection,
    *,
    page_size: int = DEFAULT_ALERT_PAGE_SIZE,
    cursor: AlertCursor | None = None,
    lifecycle: AlertLifecycle | None = None,
) -> AlertPage:
    """Return one bounded newest-first validated alert page."""
    _page_size(page_size, MAX_ALERT_PAGE_SIZE)
    if cursor is not None and type(cursor) is not AlertCursor:
        raise QueryError(QueryRejection.INVALID_CURSOR)
    if lifecycle is not None and not isinstance(lifecycle, AlertLifecycle):
        raise QueryError(QueryRejection.INVALID_QUERY)

    def read() -> AlertPage:
        _fault(QueryStage.ALERTS)
        fetch_limit = page_size + 1
        if lifecycle is None and cursor is None:
            rows = connection.execute(_ALERTS_SQL, (fetch_limit,)).fetchall()
        elif lifecycle is None:
            assert cursor is not None
            rows = connection.execute(
                _ALERTS_CURSOR_SQL,
                (cursor.opened_at_utc_us, cursor.alert_id, fetch_limit),
            ).fetchall()
        elif cursor is None:
            rows = connection.execute(
                _ALERTS_LIFECYCLE_SQL,
                (lifecycle.value, fetch_limit),
            ).fetchall()
        else:
            rows = connection.execute(
                _ALERTS_LIFECYCLE_CURSOR_SQL,
                (
                    lifecycle.value,
                    cursor.opened_at_utc_us,
                    cursor.alert_id,
                    fetch_limit,
                ),
            ).fetchall()
        validated_records = tuple(_alert_record(connection, row) for row in rows)
        has_more = len(validated_records) > page_size
        records = validated_records[:page_size]
        next_cursor = (
            AlertCursor(records[-1].opened_at_utc_us, records[-1].id)
            if has_more and records
            else None
        )
        return AlertPage(records, next_cursor)

    return _read_transaction(connection, read)


def _get_alert(connection: sqlite3.Connection, alert_id: int) -> AlertRecord:
    """Return one validated alert or a fixed not-found rejection."""
    _query_id(alert_id)

    def read() -> AlertRecord:
        _fault(QueryStage.ALERT)
        row = connection.execute(_ALERT_BY_ID_SQL, (alert_id,)).fetchone()
        if row is None:
            raise QueryError(QueryRejection.NOT_FOUND)
        return _alert_record(connection, row)

    return _read_transaction(connection, read)


def _list_alert_events(
    connection: sqlite3.Connection,
    alert_id: int,
    *,
    page_size: int = DEFAULT_ALERT_EVENT_PAGE_SIZE,
    cursor: AlertEventCursor | None = None,
) -> AlertEventPage:
    """Return one bounded chronological validated event page for one alert."""
    _query_id(alert_id)
    _page_size(page_size, MAX_ALERT_EVENT_PAGE_SIZE)
    if cursor is not None and type(cursor) is not AlertEventCursor:
        raise QueryError(QueryRejection.INVALID_CURSOR)

    def read() -> AlertEventPage:
        _fault(QueryStage.ALERT_EVENTS)
        alert_row = connection.execute(_ALERT_BY_ID_SQL, (alert_id,)).fetchone()
        if alert_row is None:
            raise QueryError(QueryRejection.NOT_FOUND)
        _alert_record(connection, alert_row)
        fetch_limit = page_size + 1
        if cursor is None:
            rows = connection.execute(
                _ALERT_EVENTS_SQL, (alert_id, fetch_limit)
            ).fetchall()
        else:
            rows = connection.execute(
                _ALERT_EVENTS_CURSOR_SQL,
                (alert_id, cursor.event_at_utc_us, cursor.event_id, fetch_limit),
            ).fetchall()
        validated_records = tuple(
            _alert_event_record(connection, row, alert_id) for row in rows
        )
        has_more = len(validated_records) > page_size
        records = validated_records[:page_size]
        next_cursor = (
            AlertEventCursor(records[-1].event_at_utc_us, records[-1].id)
            if has_more and records
            else None
        )
        return AlertEventPage(records, next_cursor)

    return _read_transaction(connection, read)


def _health_sample_record(
    connection: sqlite3.Connection, row: tuple[object, ...]
) -> HealthSampleRecord:
    try:
        if len(row) != 10:
            raise ValueError("invalid_sample_row")
        sample_id = _positive_id(row[0])
        if type(row[8]) is not bytes or len(row[8]) != PROJECTION_DIGEST_BYTES:
            raise ValueError("invalid_digest")
        component_rows = connection.execute(
            _COMPONENTS_FOR_SAMPLE_SQL, (sample_id,)
        ).fetchall()
        if len(component_rows) != len(COMPONENT_ORDER):
            raise ValueError("invalid_component_count")
        components: dict[ComponentName, ComponentProjection] = {}
        for component_row in component_rows:
            projection = _component_projection(component_row)
            if projection.component in components:
                raise ValueError("duplicate_component")
            components[projection.component] = projection
        if set(components) != set(COMPONENT_ORDER):
            raise ValueError("invalid_components")
        accepted_components = tuple(components[name] for name in COMPONENT_ORDER)
        accepted_projection = HealthProjection(
            schema_version=1,
            observation_sequence=cast(int, row[1]),
            observed_at_utc_us=cast(int, row[2]),
            recorded_at_utc_us=cast(int, row[3]),
            overall_status=HealthHistoryStatus(cast(str, row[4])),
            service_uptime_ms=cast(int, row[5]),
            sample_kind=SampleKind(cast(str, row[7])),
            missed_intervals=cast(int, row[9]),
            components=accepted_components,
            digest=row[8],
        )
        validate_health_projection(accepted_projection)
        stored_sample_kind = SampleKind(cast(str, row[6]))
        result_components = tuple(
            HealthComponentRecord(
                component=component.component,
                status=component.status,
                reasons=component.reasons,
                checked_at_utc_us=component.checked_at_utc_us,
                latency_ms=component.latency_ms,
                last_successful_at_utc_us=component.last_successful_at_utc_us,
            )
            for component in accepted_components
        )
        return HealthSampleRecord(
            id=sample_id,
            observation_sequence=accepted_projection.observation_sequence,
            observed_at_utc_us=accepted_projection.observed_at_utc_us,
            recorded_at_utc_us=accepted_projection.recorded_at_utc_us,
            overall_status=accepted_projection.overall_status,
            service_uptime_ms=accepted_projection.service_uptime_ms,
            sample_kind=stored_sample_kind,
            accepted_sample_kind=accepted_projection.sample_kind,
            missed_intervals=accepted_projection.missed_intervals,
            components=result_components,
        )
    except (ProjectionError, TypeError, ValueError):
        raise _malformed() from None


def _component_projection(row: tuple[object, ...]) -> ComponentProjection:
    if len(row) != 8:
        raise ValueError("invalid_component_row")
    reasons = tuple(
        NormalizedReason(cast(str, value)) for value in row[2:5] if value is not None
    )
    return ComponentProjection(
        component=ComponentName(cast(str, row[0])),
        status=HealthHistoryStatus(cast(str, row[1])),
        reasons=reasons,
        checked_at_utc_us=cast(int, row[5]),
        latency_ms=cast(int, row[6]),
        last_successful_at_utc_us=cast(int | None, row[7]),
    )


def _alert_record(
    connection: sqlite3.Connection, row: tuple[object, ...]
) -> AlertRecord:
    try:
        if len(row) != 14:
            raise ValueError("invalid_alert_row")
        record = AlertRecord(
            id=cast(int, row[0]),
            scope=AlertScope(cast(str, row[1])),
            kind=AlertKind(cast(str, row[2])),
            lifecycle=AlertLifecycle(cast(str, row[3])),
            severity=HealthHistoryStatus(cast(str, row[4])),
            opened_at_utc_us=cast(int, row[5]),
            acknowledged_at_utc_us=cast(int | None, row[6]),
            recovered_at_utc_us=cast(int | None, row[7]),
            archived_at_utc_us=cast(int | None, row[8]),
            first_sample_id=cast(int | None, row[9]),
            latest_sample_id=cast(int | None, row[10]),
            episode_count=cast(int, row[11]),
            occurrence_count=cast(int, row[12]),
            cooldown_until_utc_us=cast(int, row[13]),
        )
        expected = {
            reference
            for reference in (record.first_sample_id, record.latest_sample_id)
            if reference is not None
        }
        reference_rows = connection.execute(
            _ALERT_SAMPLE_REFERENCES_SQL,
            (record.first_sample_id, record.latest_sample_id),
        ).fetchall()
        actual = {_positive_id(reference[0]) for reference in reference_rows}
        if len(reference_rows) > 2 or actual != expected:
            raise ValueError("invalid_alert_sample_reference")
        return record
    except (TypeError, ValueError):
        raise _malformed() from None


def _alert_event_record(
    connection: sqlite3.Connection,
    row: tuple[object, ...],
    requested_alert_id: int,
) -> AlertEventRecord:
    try:
        if len(row) != 6 or row[1] != requested_alert_id:
            raise ValueError("invalid_event_row")
        record = AlertEventRecord(
            id=cast(int, row[0]),
            alert_id=requested_alert_id,
            event_type=LifecycleEvent(cast(str, row[2])),
            event_at_utc_us=cast(int, row[3]),
            supporting_sample_id=cast(int | None, row[4]),
            resulting_lifecycle=AlertLifecycle(cast(str, row[5])),
        )
        if record.supporting_sample_id is not None:
            references = connection.execute(
                _EVENT_SAMPLE_REFERENCE_SQL, (record.supporting_sample_id,)
            ).fetchall()
            if references != [(record.supporting_sample_id,)]:
                raise ValueError("invalid_event_sample_reference")
        return record
    except (TypeError, ValueError):
        raise _malformed() from None


def _validate_alert_record(record: AlertRecord) -> None:
    _positive_id(record.id)
    _bounded_integer(record.opened_at_utc_us, MAX_TIMESTAMP_US)
    _bounded_integer(record.cooldown_until_utc_us, MAX_TIMESTAMP_US)
    for timestamp in (
        record.acknowledged_at_utc_us,
        record.recovered_at_utc_us,
        record.archived_at_utc_us,
    ):
        if timestamp is not None:
            _bounded_integer(timestamp, MAX_TIMESTAMP_US)
    for reference in (record.first_sample_id, record.latest_sample_id):
        if reference is not None:
            _positive_id(reference)
    _bounded_positive_counter(record.episode_count)
    _bounded_positive_counter(record.occurrence_count)
    if not isinstance(record.severity, HealthHistoryStatus) or record.severity not in {
        HealthHistoryStatus.DEGRADED,
        HealthHistoryStatus.UNAVAILABLE,
    }:
        raise ValueError("invalid_alert_severity")
    expected_severity = (
        HealthHistoryStatus.DEGRADED
        if record.kind is AlertKind.DEGRADED
        else HealthHistoryStatus.UNAVAILABLE
    )
    if record.severity is not expected_severity:
        raise ValueError("inconsistent_alert_severity")
    AutomaticAlertState(
        scope=record.scope,
        kind=record.kind,
        lifecycle=record.lifecycle,
        generation=record.episode_count,
        occurrence_count=record.occurrence_count,
        cooldown_until_utc_us=record.cooldown_until_utc_us,
        recovered_at_utc_us=record.recovered_at_utc_us,
    )
    if record.lifecycle is AlertLifecycle.OPEN:
        if any(
            value is not None
            for value in (
                record.acknowledged_at_utc_us,
                record.recovered_at_utc_us,
                record.archived_at_utc_us,
            )
        ):
            raise ValueError("inconsistent_open_alert")
    elif record.lifecycle is AlertLifecycle.ACKNOWLEDGED:
        if (
            record.acknowledged_at_utc_us is None
            or record.recovered_at_utc_us is not None
            or record.archived_at_utc_us is not None
        ):
            raise ValueError("inconsistent_acknowledged_alert")
    elif record.lifecycle is AlertLifecycle.RECOVERED:
        if record.recovered_at_utc_us is None or record.archived_at_utc_us is not None:
            raise ValueError("inconsistent_recovered_alert")
    elif record.recovered_at_utc_us is None or record.archived_at_utc_us is None:
        raise ValueError("inconsistent_archived_alert")
    if record.cooldown_until_utc_us < record.opened_at_utc_us:
        raise ValueError("invalid_alert_time_order")
    if (
        record.acknowledged_at_utc_us is not None
        and record.acknowledged_at_utc_us < record.opened_at_utc_us
    ):
        raise ValueError("invalid_alert_time_order")
    if record.recovered_at_utc_us is not None and record.recovered_at_utc_us < max(
        record.opened_at_utc_us,
        record.acknowledged_at_utc_us or 0,
    ):
        raise ValueError("invalid_alert_time_order")
    if (
        record.recovered_at_utc_us is not None
        and record.cooldown_until_utc_us < record.recovered_at_utc_us
    ):
        raise ValueError("invalid_alert_time_order")
    if record.archived_at_utc_us is not None and record.archived_at_utc_us < max(
        record.recovered_at_utc_us or 0,
        record.cooldown_until_utc_us,
    ):
        raise ValueError("invalid_alert_time_order")
    if (
        record.first_sample_id is not None
        and record.latest_sample_id is not None
        and record.latest_sample_id < record.first_sample_id
    ):
        raise ValueError("invalid_alert_sample_order")


def _read_transaction[T](connection: sqlite3.Connection, reader: Callable[[], T]) -> T:
    try:
        connection.execute("BEGIN")
    except sqlite3.Error as error:
        raise _classified_sqlite_error(error) from None
    try:
        result = reader()
    except QueryError:
        _rollback_read(connection)
        raise
    except sqlite3.Error as error:
        _rollback_read(connection)
        raise _classified_sqlite_error(error) from None
    except (ProjectionError, TypeError, ValueError):
        _rollback_read(connection)
        raise _malformed() from None
    _rollback_read(connection)
    return result


def _get_scheduler_resume_state(
    connection: sqlite3.Connection,
) -> SchedulerResumeState:
    def reader() -> SchedulerResumeState:
        try:
            checkpoint = _read_checkpoint(connection)
            _validate_replay_anchor(connection, checkpoint)
        except IngestionError:
            raise _malformed() from None
        _fault(QueryStage.SCHEDULER_RESUME)
        return SchedulerResumeState(
            last_committed_sequence=checkpoint.sequence,
            last_accepted_observed_at_utc_us=checkpoint.observed_at,
            last_accepted_sample_kind=checkpoint.sample_kind,
            accepted_observation_count=checkpoint.accepted_count,
        )

    return _read_transaction(connection, reader)


def _rollback_read(connection: sqlite3.Connection) -> None:
    try:
        connection.rollback()
    except sqlite3.Error:
        raise QueryError(QueryRejection.TRUST_FAILED, trust_lost=True) from None


def _classified_sqlite_error(error: sqlite3.Error) -> QueryError:
    raw_code = getattr(error, "sqlite_errorcode", None)
    primary_code = raw_code & 0xFF if type(raw_code) is int else None
    if primary_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        return QueryError(QueryRejection.STORAGE_BUSY)
    if isinstance(error, sqlite3.IntegrityError) or primary_code in {
        sqlite3.SQLITE_CORRUPT,
        sqlite3.SQLITE_NOTADB,
        sqlite3.SQLITE_SCHEMA,
        sqlite3.SQLITE_CONSTRAINT,
    }:
        return QueryError(QueryRejection.TRUST_FAILED, trust_lost=True)
    return QueryError(QueryRejection.PERSISTENCE_FAILED)


def _page_size(value: object, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise QueryError(QueryRejection.INVALID_QUERY)
    return value


def _query_id(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_OBSERVATION_SEQUENCE:
        raise QueryError(QueryRejection.INVALID_QUERY)
    return value


def _validate_cursor_values(timestamp: object, row_id: object) -> None:
    if (
        type(timestamp) is not int
        or not 0 <= timestamp <= MAX_TIMESTAMP_US
        or type(row_id) is not int
        or not 1 <= row_id <= MAX_OBSERVATION_SEQUENCE
    ):
        raise QueryError(QueryRejection.INVALID_CURSOR)


def _positive_id(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_OBSERVATION_SEQUENCE:
        raise ValueError("invalid_id")
    return value


def _bounded_integer(value: object, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError("invalid_integer")
    return value


def _bounded_positive_counter(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_BOUNDED_COUNTER:
        raise ValueError("invalid_counter")
    return value


def _malformed() -> QueryError:
    return QueryError(QueryRejection.MALFORMED_STATE, trust_lost=True)


class QueryStage(StrEnum):
    """Fixed test-only fault seam; production behavior is a no-op."""

    HEALTH_SAMPLES = "health_samples"
    ALERTS = "alerts"
    ALERT = "alert"
    ALERT_EVENTS = "alert_events"
    SCHEDULER_RESUME = "scheduler_resume"


def _fault(stage: QueryStage) -> None:
    del stage
