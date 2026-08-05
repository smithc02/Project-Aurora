"""Exact code-owned SQLite schema version 1 and bounded verification."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterable
from enum import StrEnum
from typing import Final

from aurora_core.health_history.models import (
    APPLICATION_ID,
    MAX_BOUNDED_COUNTER,
    MAX_COMPONENT_LATENCY_MS,
    MAX_SCHEMA_OBJECTS,
    MAX_SERVICE_UPTIME_MS,
    MAX_TIMESTAMP_US,
    PROJECTION_DIGEST_BYTES,
    SCHEMA_VERSION,
    AlertKind,
    AlertLifecycle,
    AlertScope,
    ComponentName,
    HealthHistoryStatus,
    LifecycleEvent,
    SampleKind,
    SamplingGapPhase,
)
from aurora_core.health_history.reasons import NormalizedReason

QUICK_CHECK_SECONDS: Final = 2.0
PROGRESS_HANDLER_STEPS: Final = 1_000


class SchemaVerificationError(Exception):
    """Fixed fail-closed schema verification failure."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _sql_values(values: Iterable[StrEnum]) -> str:
    return ", ".join(f"'{item.value}'" for item in values)


_STATUS = _sql_values(HealthHistoryStatus)
_SAMPLE_KIND = _sql_values(SampleKind)
_COMPONENT = _sql_values(ComponentName)
_REASON = _sql_values(NormalizedReason)
_SCOPE = _sql_values(AlertScope)
_ALERT_KIND = _sql_values(AlertKind)
_LIFECYCLE = _sql_values(AlertLifecycle)
_EVENT = _sql_values(LifecycleEvent)
_GAP_PHASE = _sql_values(SamplingGapPhase)

TABLE_DDL: Final[dict[str, str]] = {
    "schema_migrations": f"""
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY CHECK (version = {SCHEMA_VERSION}),
            applied_at_utc_us INTEGER NOT NULL
                CHECK (applied_at_utc_us BETWEEN 0 AND {MAX_TIMESTAMP_US})
        )
    """,
    "health_samples": f"""
        CREATE TABLE health_samples (
            id INTEGER PRIMARY KEY,
            observed_at_utc_us INTEGER NOT NULL
                CHECK (observed_at_utc_us BETWEEN 0 AND {MAX_TIMESTAMP_US}),
            recorded_at_utc_us INTEGER NOT NULL
                CHECK (recorded_at_utc_us BETWEEN 0 AND {MAX_TIMESTAMP_US}),
            overall_status TEXT NOT NULL CHECK (overall_status IN ({_STATUS})),
            service_uptime_ms INTEGER NOT NULL
                CHECK (service_uptime_ms BETWEEN 0 AND {MAX_SERVICE_UPTIME_MS}),
            sample_kind TEXT NOT NULL CHECK (sample_kind IN ({_SAMPLE_KIND})),
            projection_digest BLOB NOT NULL
                CHECK (typeof(projection_digest) = 'blob'
                    AND length(projection_digest) = {PROJECTION_DIGEST_BYTES}),
            missed_intervals INTEGER NOT NULL
                CHECK (missed_intervals BETWEEN 0 AND {MAX_BOUNDED_COUNTER})
        )
    """,
    "component_samples": f"""
        CREATE TABLE component_samples (
            sample_id INTEGER NOT NULL
                REFERENCES health_samples(id) ON DELETE CASCADE,
            component TEXT NOT NULL CHECK (component IN ({_COMPONENT})),
            status TEXT NOT NULL CHECK (status IN ({_STATUS})),
            reason_code_1 TEXT NOT NULL CHECK (reason_code_1 IN ({_REASON})),
            reason_code_2 TEXT
                CHECK (reason_code_2 IS NULL OR reason_code_2 IN ({_REASON})),
            reason_code_3 TEXT
                CHECK (reason_code_3 IS NULL OR reason_code_3 IN ({_REASON})),
            checked_at_utc_us INTEGER NOT NULL
                CHECK (checked_at_utc_us BETWEEN 0 AND {MAX_TIMESTAMP_US}),
            latency_ms INTEGER NOT NULL
                CHECK (latency_ms BETWEEN 0 AND {MAX_COMPONENT_LATENCY_MS}),
            last_successful_at_utc_us INTEGER
                CHECK (last_successful_at_utc_us IS NULL
                    OR last_successful_at_utc_us BETWEEN 0 AND {MAX_TIMESTAMP_US}),
            PRIMARY KEY (sample_id, component),
            CHECK (reason_code_3 IS NULL OR reason_code_2 IS NOT NULL),
            CHECK (reason_code_1 IS NOT reason_code_2),
            CHECK (reason_code_1 IS NOT reason_code_3),
            CHECK (reason_code_2 IS NULL OR reason_code_2 IS NOT reason_code_3),
            CHECK (reason_code_1 LIKE component || '.%'),
            CHECK (reason_code_2 IS NULL OR reason_code_2 LIKE component || '.%'),
            CHECK (reason_code_3 IS NULL OR reason_code_3 LIKE component || '.%')
        )
    """,
    "alerts": f"""
        CREATE TABLE alerts (
            id INTEGER PRIMARY KEY,
            scope TEXT NOT NULL CHECK (scope IN ({_SCOPE})),
            kind TEXT NOT NULL CHECK (kind IN ({_ALERT_KIND})),
            lifecycle TEXT NOT NULL CHECK (lifecycle IN ({_LIFECYCLE})),
            severity TEXT NOT NULL CHECK (severity IN ('degraded', 'unavailable')),
            opened_at_utc_us INTEGER NOT NULL
                CHECK (opened_at_utc_us BETWEEN 0 AND {MAX_TIMESTAMP_US}),
            acknowledged_at_utc_us INTEGER
                CHECK (acknowledged_at_utc_us IS NULL
                    OR acknowledged_at_utc_us BETWEEN 0 AND {MAX_TIMESTAMP_US}),
            recovered_at_utc_us INTEGER
                CHECK (recovered_at_utc_us IS NULL
                    OR recovered_at_utc_us BETWEEN 0 AND {MAX_TIMESTAMP_US}),
            archived_at_utc_us INTEGER
                CHECK (archived_at_utc_us IS NULL
                    OR archived_at_utc_us BETWEEN 0 AND {MAX_TIMESTAMP_US}),
            first_sample_id INTEGER REFERENCES health_samples(id) ON DELETE SET NULL,
            latest_sample_id INTEGER REFERENCES health_samples(id) ON DELETE SET NULL,
            episode_count INTEGER NOT NULL
                CHECK (episode_count BETWEEN 1 AND {MAX_BOUNDED_COUNTER}),
            occurrence_count INTEGER NOT NULL
                CHECK (occurrence_count BETWEEN 1 AND {MAX_BOUNDED_COUNTER}),
            cooldown_until_utc_us INTEGER NOT NULL
                CHECK (cooldown_until_utc_us BETWEEN 0 AND {MAX_TIMESTAMP_US}),
            CHECK (lifecycle != 'acknowledged' OR acknowledged_at_utc_us IS NOT NULL),
            CHECK (acknowledged_at_utc_us IS NULL
                OR lifecycle IN ('acknowledged', 'recovered', 'archived')),
            CHECK (lifecycle NOT IN ('recovered', 'archived')
                OR recovered_at_utc_us IS NOT NULL),
            CHECK (lifecycle != 'archived' OR archived_at_utc_us IS NOT NULL),
            CHECK (lifecycle = 'archived' OR archived_at_utc_us IS NULL),
            CHECK (lifecycle IN ('recovered', 'archived')
                OR recovered_at_utc_us IS NULL),
            CHECK ((scope = 'sampling' AND kind = 'sampling_gap')
                OR (scope != 'sampling' AND kind != 'sampling_gap')),
            CHECK ((kind = 'degraded' AND severity = 'degraded')
                OR (kind IN ('unavailable', 'sampling_gap')
                    AND severity = 'unavailable'))
        )
    """,
    "evaluation_state": f"""
        CREATE TABLE evaluation_state (
            scope TEXT PRIMARY KEY CHECK (scope IN ({_SCOPE})),
            current_status TEXT
                CHECK (current_status IS NULL OR current_status IN ({_STATUS})),
            candidate_status TEXT
                CHECK (candidate_status IS NULL OR candidate_status IN ({_STATUS})),
            consecutive_count INTEGER NOT NULL DEFAULT 0
                CHECK (consecutive_count BETWEEN 0 AND {MAX_BOUNDED_COUNTER}),
            last_sample_id INTEGER REFERENCES health_samples(id) ON DELETE SET NULL,
            last_heartbeat_at_utc_us INTEGER
                CHECK (last_heartbeat_at_utc_us IS NULL
                    OR last_heartbeat_at_utc_us BETWEEN 0 AND {MAX_TIMESTAMP_US}),
            gap_phase TEXT NOT NULL DEFAULT 'clear' CHECK (gap_phase IN ({_GAP_PHASE})),
            current_alert_id INTEGER REFERENCES alerts(id) ON DELETE SET NULL,
            cooldown_until_utc_us INTEGER
                CHECK (cooldown_until_utc_us IS NULL
                    OR cooldown_until_utc_us BETWEEN 0 AND {MAX_TIMESTAMP_US})
        )
    """,
    "alert_events": f"""
        CREATE TABLE alert_events (
            id INTEGER PRIMARY KEY,
            alert_id INTEGER NOT NULL REFERENCES alerts(id) ON DELETE CASCADE,
            event_type TEXT NOT NULL CHECK (event_type IN ({_EVENT})),
            event_at_utc_us INTEGER NOT NULL
                CHECK (event_at_utc_us BETWEEN 0 AND {MAX_TIMESTAMP_US}),
            supporting_sample_id INTEGER
                REFERENCES health_samples(id) ON DELETE SET NULL,
            resulting_lifecycle TEXT NOT NULL
                CHECK (resulting_lifecycle IN ({_LIFECYCLE})),
            CHECK ((event_type = 'opened' AND resulting_lifecycle = 'open')
                OR (event_type = 'acknowledged'
                    AND resulting_lifecycle = 'acknowledged')
                OR (event_type = 'recovered'
                    AND resulting_lifecycle = 'recovered')
                OR (event_type = 'archived'
                    AND resulting_lifecycle = 'archived')
                OR event_type IN ('occurrence_updated', 'rejected_transition'))
        )
    """,
}

INDEX_DDL: Final[dict[str, str]] = {
    "uq_health_samples_replay": """
        CREATE UNIQUE INDEX uq_health_samples_replay
        ON health_samples(observed_at_utc_us, projection_digest)
    """,
    "idx_health_samples_observed": """
        CREATE INDEX idx_health_samples_observed
        ON health_samples(observed_at_utc_us DESC, id DESC)
    """,
    "idx_health_samples_status_observed": """
        CREATE INDEX idx_health_samples_status_observed
        ON health_samples(overall_status, observed_at_utc_us DESC, id DESC)
    """,
    "idx_component_samples_component_status_checked": """
        CREATE INDEX idx_component_samples_component_status_checked
        ON component_samples(component, status, checked_at_utc_us DESC, sample_id DESC)
    """,
    "uq_alerts_active_scope_kind": """
        CREATE UNIQUE INDEX uq_alerts_active_scope_kind
        ON alerts(scope, kind) WHERE lifecycle IN ('open', 'acknowledged')
    """,
    "idx_alerts_lifecycle_opened": """
        CREATE INDEX idx_alerts_lifecycle_opened
        ON alerts(lifecycle, opened_at_utc_us DESC, id DESC)
    """,
    "idx_alerts_recovered": """
        CREATE INDEX idx_alerts_recovered
        ON alerts(recovered_at_utc_us, id) WHERE recovered_at_utc_us IS NOT NULL
    """,
    "idx_alert_events_alert_time": """
        CREATE INDEX idx_alert_events_alert_time
        ON alert_events(alert_id, event_at_utc_us, id)
    """,
}

EXPECTED_TABLES: Final = frozenset(TABLE_DDL)
EXPECTED_INDEXES: Final = frozenset(INDEX_DDL)


def create_schema_v1(connection: sqlite3.Connection, *, applied_at_utc_us: int) -> None:
    """Create exact schema version 1 in one transaction."""
    if (
        type(applied_at_utc_us) is not int
        or not 0 <= applied_at_utc_us <= MAX_TIMESTAMP_US
    ):
        raise SchemaVerificationError("invalid_migration_timestamp")
    try:
        connection.execute("BEGIN IMMEDIATE")
        for statement in TABLE_DDL.values():
            connection.execute(statement)
        for statement in INDEX_DDL.values():
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at_utc_us) VALUES (?, ?)",
            (SCHEMA_VERSION, applied_at_utc_us),
        )
        connection.executemany(
            "INSERT INTO evaluation_state(scope) VALUES (?)",
            ((scope.value,) for scope in AlertScope),
        )
        connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
    except (sqlite3.Error, SchemaVerificationError) as error:
        connection.rollback()
        if isinstance(error, SchemaVerificationError):
            raise
        raise SchemaVerificationError("schema_creation_failed") from error


def verify_schema_v1(
    connection: sqlite3.Connection,
    *,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Verify identity, exact objects, columns, ledger, and bounded integrity."""
    try:
        if _pragma_integer(connection, "application_id") != APPLICATION_ID:
            raise SchemaVerificationError("application_identity_mismatch")
        if _pragma_integer(connection, "user_version") != SCHEMA_VERSION:
            raise SchemaVerificationError("schema_version_mismatch")
        foreign_keys = _pragma_integer(connection, "foreign_keys")
        if foreign_keys != 1:
            raise SchemaVerificationError("foreign_keys_disabled")
        objects = connection.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name LIMIT ?",
            (MAX_SCHEMA_OBJECTS + 1,),
        ).fetchall()
        if len(objects) > MAX_SCHEMA_OBJECTS:
            raise SchemaVerificationError("schema_object_limit")
        actual = {(str(row[0]), str(row[1])): row[2] for row in objects}
        expected = {
            **{("table", name): sql for name, sql in TABLE_DDL.items()},
            **{("index", name): sql for name, sql in INDEX_DDL.items()},
        }
        if set(actual) != set(expected):
            raise SchemaVerificationError("schema_objects_mismatch")
        for key, sql in expected.items():
            stored = actual[key]
            if not isinstance(stored, str) or _normalize_sql(stored) != _normalize_sql(
                sql
            ):
                raise SchemaVerificationError("schema_definition_mismatch")
        ledger = connection.execute(
            "SELECT version, applied_at_utc_us FROM schema_migrations LIMIT 2"
        ).fetchall()
        if len(ledger) != 1 or ledger[0][0] != SCHEMA_VERSION:
            raise SchemaVerificationError("migration_ledger_mismatch")
        if type(ledger[0][1]) is not int or not 0 <= ledger[0][1] <= MAX_TIMESTAMP_US:
            raise SchemaVerificationError("migration_ledger_mismatch")
        scopes = connection.execute(
            "SELECT scope FROM evaluation_state ORDER BY scope LIMIT ?",
            (len(AlertScope) + 1,),
        ).fetchall()
        if {row[0] for row in scopes} != {scope.value for scope in AlertScope}:
            raise SchemaVerificationError("evaluation_state_mismatch")
        _bounded_quick_check(connection, monotonic=monotonic)
    except SchemaVerificationError:
        raise
    except sqlite3.Error as error:
        raise SchemaVerificationError("schema_verification_failed") from error


def _bounded_quick_check(
    connection: sqlite3.Connection, *, monotonic: Callable[[], float]
) -> None:
    deadline = monotonic() + QUICK_CHECK_SECONDS

    def progress() -> int:
        return 1 if monotonic() >= deadline else 0

    connection.set_progress_handler(progress, PROGRESS_HANDLER_STEPS)
    try:
        rows = connection.execute("PRAGMA quick_check(1)").fetchmany(2)
    except sqlite3.Error as error:
        raise SchemaVerificationError("quick_check_failed") from error
    finally:
        connection.set_progress_handler(None, 0)
    if rows != [("ok",)]:
        raise SchemaVerificationError("quick_check_failed")


def _pragma_integer(connection: sqlite3.Connection, name: str) -> int:
    row = connection.execute(f"PRAGMA {name}").fetchone()
    if row is None or type(row[0]) is not int:
        raise SchemaVerificationError("pragma_mismatch")
    return row[0]


def _normalize_sql(statement: str) -> str:
    return " ".join(statement.rstrip().rstrip(";").split()).lower()
