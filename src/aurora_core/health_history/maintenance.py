"""Bounded retention and incremental-vacuum primitives for schema version 1."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from aurora_core.health_history.models import (
    AUTO_VACUUM_INCREMENTAL,
    MAX_TIMESTAMP_US,
    AlertLifecycle,
)
from aurora_core.health_history.queries import (
    _ALERT_COLUMNS,
    _ALERT_EVENT_COLUMNS,
    _HEALTH_SAMPLE_COLUMNS,
    AlertEventRecord,
    QueryError,
    QueryRejection,
    _alert_event_record,
    _alert_record,
    _health_sample_record,
)

DEFAULT_RETENTION_DAYS: Final = 30
MIN_RETENTION_DAYS: Final = 1
MAX_RETENTION_DAYS: Final = 365
RETENTION_ROW_BUDGET: Final = 500
INCREMENTAL_VACUUM_PAGES: Final = 128
MAINTENANCE_SECONDS: Final = 1.0
PROGRESS_HANDLER_STEPS: Final = 1_000
MICROSECONDS_PER_DAY: Final = 86_400_000_000
MAX_SQLITE_PAGE_COUNT: Final = 4_294_967_294

_HEALTH_RETENTION_CANDIDATES_SQL: Final = (
    f"SELECT {_HEALTH_SAMPLE_COLUMNS} FROM health_samples "
    "INDEXED BY idx_health_samples_observed "
    "WHERE observed_at_utc_us < ? "
    "ORDER BY observed_at_utc_us, id LIMIT ?"
)
_ARCHIVED_ALERT_CANDIDATES_SQL: Final = (
    f"SELECT {_ALERT_COLUMNS} FROM alerts "
    "INDEXED BY idx_alerts_archived_recovered_id "
    "WHERE lifecycle = 'archived' AND recovered_at_utc_us < ? "
    "ORDER BY recovered_at_utc_us, id LIMIT ?"
)
_ALERT_EVENTS_FOR_RETENTION_SQL: Final = (
    f"SELECT {_ALERT_EVENT_COLUMNS} FROM alert_events "
    "INDEXED BY idx_alert_events_alert_time WHERE alert_id = ? "
    "ORDER BY event_at_utc_us, id LIMIT ?"
)
_ALERT_HAS_EVENTS_SQL: Final = (
    "SELECT id FROM alert_events INDEXED BY idx_alert_events_alert_time "
    "WHERE alert_id = ? LIMIT 1"
)
_DELETE_HEALTH_SAMPLE_SQL: Final = (
    "DELETE FROM health_samples WHERE id = ? AND observed_at_utc_us < ?"
)
_DELETE_ALERT_EVENT_SQL: Final = (
    "DELETE FROM alert_events WHERE id = ? AND alert_id = ?"
)
_DELETE_ARCHIVED_ALERT_SQL: Final = (
    "DELETE FROM alerts WHERE id = ? AND lifecycle = 'archived' "
    "AND recovered_at_utc_us < ?"
)
_AUTO_VACUUM_SQL: Final = "PRAGMA auto_vacuum"
_FREELIST_COUNT_SQL: Final = "PRAGMA freelist_count"
_INCREMENTAL_VACUUM_SQL: Final = "PRAGMA incremental_vacuum(128)"


class MaintenanceOutcome(StrEnum):
    NO_WORK = "no_work"
    COMPLETED = "completed"


class MaintenanceRejection(StrEnum):
    INVALID_MAINTENANCE = "invalid_maintenance"
    STORAGE_BUSY = "storage_busy"
    TIMED_OUT = "timed_out"
    PERSISTENCE_FAILED = "persistence_failed"
    MALFORMED_STATE = "malformed_state"
    TRUST_FAILED = "trust_failed"


class MaintenanceStage(StrEnum):
    """Fixed test-only fault seam; production behavior is a no-op."""

    AFTER_BEGIN = "after_begin"
    AFTER_PLAN = "after_plan"
    AFTER_MUTATION = "after_mutation"
    BEFORE_COMMIT = "before_commit"
    VACUUM_BEFORE = "vacuum_before"
    VACUUM_AFTER = "vacuum_after"


class MaintenanceError(Exception):
    """Fixed failure with no SQLite text, path, SQL, or submitted value."""

    def __init__(
        self, reason: MaintenanceRejection, *, trust_lost: bool = False
    ) -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.trust_lost = trust_lost


@dataclass(frozen=True, slots=True)
class RetentionCleanupResult:
    outcome: MaintenanceOutcome
    health_samples_deleted: int
    alert_events_deleted: int
    alerts_deleted: int

    def __post_init__(self) -> None:
        counts = (
            self.health_samples_deleted,
            self.alert_events_deleted,
            self.alerts_deleted,
        )
        if (
            not isinstance(self.outcome, MaintenanceOutcome)
            or any(type(count) is not int or count < 0 for count in counts)
            or self.logical_rows_deleted > RETENTION_ROW_BUDGET
            or (
                self.outcome is MaintenanceOutcome.NO_WORK
                and self.logical_rows_deleted != 0
            )
            or (
                self.outcome is MaintenanceOutcome.COMPLETED
                and self.logical_rows_deleted == 0
            )
        ):
            raise ValueError("invalid_cleanup_result")

    @property
    def logical_rows_deleted(self) -> int:
        return (
            self.health_samples_deleted
            + self.alert_events_deleted
            + self.alerts_deleted
        )


@dataclass(frozen=True, slots=True)
class IncrementalVacuumResult:
    outcome: MaintenanceOutcome
    pages_requested: int
    freelist_before: int
    freelist_after: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.outcome, MaintenanceOutcome)
            or type(self.pages_requested) is not int
            or type(self.freelist_before) is not int
            or type(self.freelist_after) is not int
            or not 0 <= self.freelist_before <= MAX_SQLITE_PAGE_COUNT
            or not 0 <= self.freelist_after <= MAX_SQLITE_PAGE_COUNT
            or self.freelist_after > self.freelist_before
            or self.freelist_before - self.freelist_after > self.pages_requested
            or (
                self.outcome is MaintenanceOutcome.NO_WORK
                and (
                    self.pages_requested != 0
                    or self.freelist_before != 0
                    or self.freelist_after != 0
                )
            )
            or (
                self.outcome is MaintenanceOutcome.COMPLETED
                and (
                    self.pages_requested != INCREMENTAL_VACUUM_PAGES
                    or self.freelist_before < 1
                )
            )
        ):
            raise ValueError("invalid_vacuum_result")


@dataclass(frozen=True, slots=True)
class _RetentionCandidate:
    retained_at_utc_us: int
    type_order: int
    row_id: int
    kind: str


@dataclass(frozen=True, slots=True)
class _DeletionAction:
    kind: str
    row_id: int
    parent_alert_id: int | None = None


class _Deadline:
    def __init__(self, monotonic: Callable[[], float]) -> None:
        self._monotonic = monotonic
        self._deadline = monotonic() + MAINTENANCE_SECONDS
        self.expired = False

    def progress(self) -> int:
        if self._monotonic() >= self._deadline:
            self.expired = True
            return 1
        return 0

    def check(self) -> None:
        if self._monotonic() >= self._deadline:
            self.expired = True
            raise MaintenanceError(MaintenanceRejection.TIMED_OUT)


def _cleanup_retention(
    connection: sqlite3.Connection,
    *,
    now_utc_us: int,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    monotonic: Callable[[], float],
) -> RetentionCleanupResult:
    """Delete at most 500 deterministic logical rows in one transaction."""
    cutoff = _retention_cutoff(now_utc_us, retention_days)
    deadline = _Deadline(monotonic)
    transaction_started = False
    try:
        _install_progress_handler(connection, deadline)
        try:
            connection.execute("BEGIN IMMEDIATE")
            transaction_started = True
            _fault(MaintenanceStage.AFTER_BEGIN)
            plan = _retention_plan(connection, cutoff, deadline)
            _fault(MaintenanceStage.AFTER_PLAN)
            counts = _execute_retention_plan(connection, plan, cutoff, deadline)
            _fault(MaintenanceStage.AFTER_MUTATION)
            deadline.check()
            _fault(MaintenanceStage.BEFORE_COMMIT)
            _commit_transaction(connection)
            transaction_started = False
            deadline.check()
            deleted = sum(counts)
            return RetentionCleanupResult(
                outcome=(
                    MaintenanceOutcome.NO_WORK
                    if deleted == 0
                    else MaintenanceOutcome.COMPLETED
                ),
                health_samples_deleted=counts[0],
                alert_events_deleted=counts[1],
                alerts_deleted=counts[2],
            )
        except MaintenanceError:
            if transaction_started:
                _rollback_after_failure(connection)
            raise
        except QueryError as error:
            if transaction_started:
                _rollback_after_failure(connection)
            rejection = (
                MaintenanceRejection.MALFORMED_STATE
                if error.reason is QueryRejection.MALFORMED_STATE
                else MaintenanceRejection.TRUST_FAILED
            )
            raise MaintenanceError(rejection, trust_lost=True) from None
        except sqlite3.Error as error:
            if transaction_started:
                _rollback_after_failure(connection)
            raise _classified_sqlite_error(error, deadline) from None
        except (TypeError, ValueError):
            if transaction_started:
                _rollback_after_failure(connection)
            raise MaintenanceError(
                MaintenanceRejection.MALFORMED_STATE, trust_lost=True
            ) from None
    finally:
        _clear_progress_handler(connection)


def _incremental_vacuum(
    connection: sqlite3.Connection,
    *,
    monotonic: Callable[[], float],
) -> IncrementalVacuumResult:
    """Run at most one fixed 128-page incremental-vacuum request."""
    deadline = _Deadline(monotonic)
    try:
        _install_progress_handler(connection, deadline)
        try:
            if _auto_vacuum_mode(connection) != AUTO_VACUUM_INCREMENTAL:
                raise MaintenanceError(
                    MaintenanceRejection.TRUST_FAILED, trust_lost=True
                )
            before = _bounded_page_count(_freelist_count(connection))
            if before == 0:
                deadline.check()
                return IncrementalVacuumResult(
                    MaintenanceOutcome.NO_WORK,
                    pages_requested=0,
                    freelist_before=0,
                    freelist_after=0,
                )
            deadline.check()
            _fault(MaintenanceStage.VACUUM_BEFORE)
            vacuum_cursor = connection.execute(_INCREMENTAL_VACUUM_SQL)
            _consume_incremental_vacuum_cursor(vacuum_cursor)
            _fault(MaintenanceStage.VACUUM_AFTER)
            deadline.check()
            after = _bounded_page_count(_freelist_count(connection))
            if after > before or before - after > INCREMENTAL_VACUUM_PAGES:
                raise MaintenanceError(
                    MaintenanceRejection.MALFORMED_STATE, trust_lost=True
                )
            deadline.check()
            return IncrementalVacuumResult(
                MaintenanceOutcome.COMPLETED,
                pages_requested=INCREMENTAL_VACUUM_PAGES,
                freelist_before=before,
                freelist_after=after,
            )
        except MaintenanceError:
            raise
        except sqlite3.Error as error:
            raise _classified_sqlite_error(error, deadline) from None
        except (TypeError, ValueError):
            raise MaintenanceError(
                MaintenanceRejection.MALFORMED_STATE, trust_lost=True
            ) from None
    finally:
        _clear_progress_handler(connection)


def _retention_cutoff(now_utc_us: object, retention_days: object) -> int:
    if type(now_utc_us) is not int or not 0 <= now_utc_us <= MAX_TIMESTAMP_US:
        raise MaintenanceError(MaintenanceRejection.INVALID_MAINTENANCE)
    if (
        type(retention_days) is not int
        or not MIN_RETENTION_DAYS <= retention_days <= MAX_RETENTION_DAYS
    ):
        raise MaintenanceError(MaintenanceRejection.INVALID_MAINTENANCE)
    return now_utc_us - retention_days * MICROSECONDS_PER_DAY


def _retention_plan(
    connection: sqlite3.Connection, cutoff: int, deadline: _Deadline
) -> tuple[_DeletionAction, ...]:
    health_rows = connection.execute(
        _HEALTH_RETENTION_CANDIDATES_SQL, (cutoff, RETENTION_ROW_BUDGET)
    ).fetchall()
    deadline.check()
    alert_rows = connection.execute(
        _ARCHIVED_ALERT_CANDIDATES_SQL, (cutoff, RETENTION_ROW_BUDGET)
    ).fetchall()
    deadline.check()
    candidates: list[_RetentionCandidate] = []
    for row in health_rows:
        deadline.check()
        health_record = _health_sample_record(connection, row)
        deadline.check()
        if health_record.observed_at_utc_us >= cutoff:
            raise MaintenanceError(
                MaintenanceRejection.MALFORMED_STATE, trust_lost=True
            )
        candidates.append(
            _RetentionCandidate(
                health_record.observed_at_utc_us, 0, health_record.id, "health"
            )
        )
    for row in alert_rows:
        deadline.check()
        alert_record = _alert_record(connection, row)
        deadline.check()
        if (
            alert_record.lifecycle is not AlertLifecycle.ARCHIVED
            or alert_record.recovered_at_utc_us is None
            or alert_record.recovered_at_utc_us >= cutoff
        ):
            raise MaintenanceError(
                MaintenanceRejection.MALFORMED_STATE, trust_lost=True
            )
        candidates.append(
            _RetentionCandidate(
                alert_record.recovered_at_utc_us, 1, alert_record.id, "alert"
            )
        )
    deadline.check()
    candidates.sort(
        key=lambda candidate: (
            candidate.retained_at_utc_us,
            candidate.type_order,
            candidate.row_id,
        )
    )
    deadline.check()
    actions: list[_DeletionAction] = []
    for candidate in candidates:
        deadline.check()
        remaining = RETENTION_ROW_BUDGET - len(actions)
        if remaining == 0:
            break
        if candidate.kind == "health":
            actions.append(_DeletionAction("health", candidate.row_id))
            continue
        deadline.check()
        event_rows = connection.execute(
            _ALERT_EVENTS_FOR_RETENTION_SQL,
            (candidate.row_id, remaining + 1),
        ).fetchall()
        deadline.check()
        events: list[AlertEventRecord] = []
        for row in event_rows:
            deadline.check()
            events.append(_alert_event_record(connection, row, candidate.row_id))
            deadline.check()
        selected_events = events[:remaining]
        for event in selected_events:
            deadline.check()
            actions.append(_DeletionAction("event", event.id, candidate.row_id))
        if len(events) <= remaining and len(actions) < RETENTION_ROW_BUDGET:
            actions.append(_DeletionAction("alert", candidate.row_id))
    if len(actions) > RETENTION_ROW_BUDGET:
        raise MaintenanceError(MaintenanceRejection.MALFORMED_STATE, trust_lost=True)
    return tuple(actions)


def _execute_retention_plan(
    connection: sqlite3.Connection,
    actions: tuple[_DeletionAction, ...],
    cutoff: int,
    deadline: _Deadline,
) -> tuple[int, int, int]:
    health_deleted = 0
    events_deleted = 0
    alerts_deleted = 0
    for action in actions:
        deadline.check()
        if action.kind == "health":
            cursor = connection.execute(
                _DELETE_HEALTH_SAMPLE_SQL, (action.row_id, cutoff)
            )
            health_deleted += 1
        elif action.kind == "event" and action.parent_alert_id is not None:
            cursor = connection.execute(
                _DELETE_ALERT_EVENT_SQL,
                (action.row_id, action.parent_alert_id),
            )
            events_deleted += 1
        elif action.kind == "alert":
            if (
                connection.execute(_ALERT_HAS_EVENTS_SQL, (action.row_id,)).fetchone()
                is not None
            ):
                raise MaintenanceError(
                    MaintenanceRejection.MALFORMED_STATE, trust_lost=True
                )
            cursor = connection.execute(
                _DELETE_ARCHIVED_ALERT_SQL, (action.row_id, cutoff)
            )
            alerts_deleted += 1
        else:
            raise MaintenanceError(
                MaintenanceRejection.MALFORMED_STATE, trust_lost=True
            )
        if cursor.rowcount != 1:
            raise MaintenanceError(
                MaintenanceRejection.MALFORMED_STATE, trust_lost=True
            )
    return health_deleted, events_deleted, alerts_deleted


def _consume_incremental_vacuum_cursor(cursor: sqlite3.Cursor) -> int:
    rows = cursor.fetchmany(INCREMENTAL_VACUUM_PAGES + 1)
    if len(rows) > INCREMENTAL_VACUUM_PAGES:
        raise MaintenanceError(MaintenanceRejection.MALFORMED_STATE, trust_lost=True)
    return len(rows)


def _auto_vacuum_mode(connection: sqlite3.Connection) -> int:
    return _validated_pragma_integer(connection.execute(_AUTO_VACUUM_SQL).fetchone())


def _freelist_count(connection: sqlite3.Connection) -> int:
    return _validated_pragma_integer(connection.execute(_FREELIST_COUNT_SQL).fetchone())


def _validated_pragma_integer(row: tuple[object, ...] | None) -> int:
    if row is None or len(row) != 1 or type(row[0]) is not int:
        raise MaintenanceError(MaintenanceRejection.MALFORMED_STATE, trust_lost=True)
    return row[0]


def _bounded_page_count(value: int) -> int:
    if not 0 <= value <= MAX_SQLITE_PAGE_COUNT:
        raise MaintenanceError(MaintenanceRejection.MALFORMED_STATE, trust_lost=True)
    return value


def _install_progress_handler(
    connection: sqlite3.Connection, deadline: _Deadline
) -> None:
    try:
        connection.set_progress_handler(deadline.progress, PROGRESS_HANDLER_STEPS)
    except sqlite3.Error as error:
        raise _classified_sqlite_error(error, deadline) from None


def _clear_progress_handler(connection: sqlite3.Connection) -> None:
    try:
        connection.set_progress_handler(None, 0)
    except sqlite3.Error:
        raise MaintenanceError(
            MaintenanceRejection.TRUST_FAILED, trust_lost=True
        ) from None


def _classified_sqlite_error(
    error: sqlite3.Error, deadline: _Deadline
) -> MaintenanceError:
    raw_code = getattr(error, "sqlite_errorcode", None)
    primary_code = raw_code & 0xFF if type(raw_code) is int else None
    if isinstance(error, sqlite3.IntegrityError) or primary_code in {
        sqlite3.SQLITE_CORRUPT,
        sqlite3.SQLITE_NOTADB,
        sqlite3.SQLITE_SCHEMA,
        sqlite3.SQLITE_CONSTRAINT,
    }:
        return MaintenanceError(MaintenanceRejection.TRUST_FAILED, trust_lost=True)
    if primary_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        return MaintenanceError(MaintenanceRejection.STORAGE_BUSY)
    if deadline.expired and primary_code in {None, sqlite3.SQLITE_INTERRUPT}:
        return MaintenanceError(MaintenanceRejection.TIMED_OUT)
    return MaintenanceError(MaintenanceRejection.PERSISTENCE_FAILED)


def _rollback_after_failure(connection: sqlite3.Connection) -> None:
    try:
        _rollback_transaction(connection)
    except sqlite3.Error:
        raise MaintenanceError(
            MaintenanceRejection.TRUST_FAILED, trust_lost=True
        ) from None


def _rollback_transaction(connection: sqlite3.Connection) -> None:
    connection.rollback()


def _commit_transaction(connection: sqlite3.Connection) -> None:
    connection.commit()


def _fault(stage: MaintenanceStage) -> None:
    del stage
