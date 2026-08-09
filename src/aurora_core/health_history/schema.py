"""Exact code-owned SQLite schema version 1 and bounded verification."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterable
from enum import StrEnum
from typing import Final, cast

from aurora_core.health_history.models import (
    APPLICATION_ID,
    AUTO_VACUUM_INCREMENTAL,
    MAX_BOUNDED_COUNTER,
    MAX_COMPONENT_LATENCY_MS,
    MAX_OBSERVATION_SEQUENCE,
    MAX_SCHEMA_OBJECTS,
    MAX_SCHEMA_VERSION,
    MAX_SERVICE_UPTIME_MS,
    MAX_TIMESTAMP_US,
    PROJECTION_DIGEST_BYTES,
    REPLAY_LEDGER_CAPACITY,
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
            version INTEGER PRIMARY KEY
                CHECK (version BETWEEN 1 AND {MAX_SCHEMA_VERSION}),
            applied_at_utc_us INTEGER NOT NULL
                CHECK (applied_at_utc_us BETWEEN 0 AND {MAX_TIMESTAMP_US})
        )
    """,
    "ingestion_checkpoint": f"""
        CREATE TABLE ingestion_checkpoint (
            singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
            last_committed_sequence INTEGER
                CHECK (last_committed_sequence IS NULL
                    OR last_committed_sequence
                        BETWEEN 0 AND {MAX_OBSERVATION_SEQUENCE}),
            last_accepted_observed_at_utc_us INTEGER
                CHECK (last_accepted_observed_at_utc_us IS NULL
                    OR last_accepted_observed_at_utc_us
                        BETWEEN 0 AND {MAX_TIMESTAMP_US}),
            last_accepted_projection_digest BLOB
                CHECK (last_accepted_projection_digest IS NULL
                    OR (typeof(last_accepted_projection_digest) = 'blob'
                        AND length(last_accepted_projection_digest)
                            = {PROJECTION_DIGEST_BYTES})),
            last_accepted_sample_kind TEXT
                CHECK (last_accepted_sample_kind IS NULL
                    OR last_accepted_sample_kind IN ({_SAMPLE_KIND})),
            accepted_observation_count INTEGER NOT NULL DEFAULT 0
                CHECK (accepted_observation_count
                    BETWEEN 0 AND {MAX_BOUNDED_COUNTER}),
            CHECK ((last_accepted_observed_at_utc_us IS NULL
                    AND last_accepted_projection_digest IS NULL
                    AND last_accepted_sample_kind IS NULL
                    AND accepted_observation_count = 0
                    AND last_committed_sequence IS NULL)
                OR (last_accepted_observed_at_utc_us IS NOT NULL
                    AND last_accepted_projection_digest IS NOT NULL
                    AND last_accepted_sample_kind IS NOT NULL
                    AND accepted_observation_count >= 1
                    AND last_committed_sequence IS NOT NULL))
        )
    """,
    "accepted_observation_replay": f"""
        CREATE TABLE accepted_observation_replay (
            observation_sequence INTEGER PRIMARY KEY
                CHECK (observation_sequence
                    BETWEEN 0 AND {MAX_OBSERVATION_SEQUENCE}),
            observed_at_utc_us INTEGER NOT NULL
                CHECK (observed_at_utc_us BETWEEN 0 AND {MAX_TIMESTAMP_US}),
            projection_digest BLOB NOT NULL
                CHECK (typeof(projection_digest) = 'blob'
                    AND length(projection_digest) = {PROJECTION_DIGEST_BYTES}),
            accepted_sample_kind TEXT NOT NULL
                CHECK (accepted_sample_kind IN ({_SAMPLE_KIND}))
        )
    """,
    "health_samples": f"""
        CREATE TABLE health_samples (
            id INTEGER PRIMARY KEY,
            observation_sequence INTEGER NOT NULL
                CHECK (observation_sequence
                    BETWEEN 0 AND {MAX_OBSERVATION_SEQUENCE}),
            observed_at_utc_us INTEGER NOT NULL
                CHECK (observed_at_utc_us BETWEEN 0 AND {MAX_TIMESTAMP_US}),
            recorded_at_utc_us INTEGER NOT NULL
                CHECK (recorded_at_utc_us BETWEEN 0 AND {MAX_TIMESTAMP_US}),
            overall_status TEXT NOT NULL CHECK (overall_status IN ({_STATUS})),
            service_uptime_ms INTEGER NOT NULL
                CHECK (service_uptime_ms BETWEEN 0 AND {MAX_SERVICE_UPTIME_MS}),
            sample_kind TEXT NOT NULL CHECK (sample_kind IN ({_SAMPLE_KIND})),
            accepted_sample_kind TEXT NOT NULL
                CHECK (accepted_sample_kind IN ({_SAMPLE_KIND})),
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
            CHECK (reason_code_3 IS NULL OR reason_code_3 LIKE component || '.%'),
            CHECK ((component = 'wled' AND reason_code_3 IS NULL)
                OR component = 'hyperhdr'
                OR (component = 'capture' AND reason_code_3 IS NULL)
                OR (component = 'raspberry_pi'
                    AND reason_code_2 IS NULL AND reason_code_3 IS NULL))
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
            last_sample_id INTEGER
                REFERENCES health_samples(id) ON DELETE SET NULL
                CHECK (last_sample_id IS NULL OR last_sample_id > 0),
            last_heartbeat_at_utc_us INTEGER
                CHECK (last_heartbeat_at_utc_us IS NULL
                    OR last_heartbeat_at_utc_us BETWEEN 0 AND {MAX_TIMESTAMP_US}),
            gap_phase TEXT NOT NULL DEFAULT 'clear' CHECK (gap_phase IN ({_GAP_PHASE})),
            cooldown_until_utc_us INTEGER
                CHECK (cooldown_until_utc_us IS NULL
                    OR cooldown_until_utc_us BETWEEN 0 AND {MAX_TIMESTAMP_US}),
            CHECK ((scope = 'sampling'
                    AND current_status IS NULL
                    AND candidate_status IS NULL)
                OR (scope != 'sampling'
                    AND gap_phase = 'clear'
                    AND ((candidate_status IS NULL AND consecutive_count = 0)
                        OR (candidate_status IS NOT NULL
                            AND consecutive_count >= 1
                            AND candidate_status = current_status))
                    AND (current_status IS NOT NULL
                        OR (candidate_status IS NULL
                            AND consecutive_count = 0))))
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
                OR (event_type = 'occurrence_updated'
                    AND resulting_lifecycle IN ('open', 'acknowledged', 'recovered')))
        )
    """,
}

INDEX_DDL: Final[dict[str, str]] = {
    "uq_health_samples_sequence": """
        CREATE UNIQUE INDEX uq_health_samples_sequence
        ON health_samples(observation_sequence)
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
    "idx_alerts_terminal_scope_kind_id": """
        CREATE INDEX idx_alerts_terminal_scope_kind_id
        ON alerts(scope, kind, id DESC)
        WHERE lifecycle IN ('recovered', 'archived')
    """,
    "idx_alerts_recovered_cooldown_id": """
        CREATE INDEX idx_alerts_recovered_cooldown_id
        ON alerts(cooldown_until_utc_us, id) WHERE lifecycle = 'recovered'
    """,
    "idx_alerts_opened": """
        CREATE INDEX idx_alerts_opened
        ON alerts(opened_at_utc_us DESC, id DESC)
    """,
    "idx_alerts_lifecycle_opened": """
        CREATE INDEX idx_alerts_lifecycle_opened
        ON alerts(lifecycle, opened_at_utc_us DESC, id DESC)
    """,
    "idx_alerts_first_sample_id": """
        CREATE INDEX idx_alerts_first_sample_id
        ON alerts(first_sample_id)
    """,
    "idx_alerts_latest_sample_id": """
        CREATE INDEX idx_alerts_latest_sample_id
        ON alerts(latest_sample_id)
    """,
    "idx_alerts_archived_recovered_id": """
        CREATE INDEX idx_alerts_archived_recovered_id
        ON alerts(recovered_at_utc_us, id) WHERE lifecycle = 'archived'
    """,
    "idx_alert_events_alert_time": """
        CREATE INDEX idx_alert_events_alert_time
        ON alert_events(alert_id, event_at_utc_us, id)
    """,
    "idx_alert_events_supporting_sample_id": """
        CREATE INDEX idx_alert_events_supporting_sample_id
        ON alert_events(supporting_sample_id)
    """,
}

TRIGGER_DDL: Final[dict[str, str]] = {
    "trg_ingestion_checkpoint_no_regression": """
        CREATE TRIGGER trg_ingestion_checkpoint_no_regression
        BEFORE UPDATE ON ingestion_checkpoint
        WHEN NEW.accepted_observation_count < OLD.accepted_observation_count
            OR (OLD.last_committed_sequence IS NOT NULL
                AND (NEW.last_committed_sequence IS NULL
                    OR NEW.last_committed_sequence < OLD.last_committed_sequence))
            OR (NEW.last_committed_sequence IS OLD.last_committed_sequence
                AND (NEW.last_accepted_observed_at_utc_us
                        IS NOT OLD.last_accepted_observed_at_utc_us
                    OR NEW.last_accepted_projection_digest
                        IS NOT OLD.last_accepted_projection_digest
                    OR NEW.last_accepted_sample_kind
                        IS NOT OLD.last_accepted_sample_kind))
        BEGIN
            SELECT RAISE(ABORT, 'checkpoint_regression');
        END
    """,
}

EXPECTED_TABLES: Final = frozenset(TABLE_DDL)
EXPECTED_INDEXES: Final = frozenset(INDEX_DDL)
EXPECTED_TRIGGERS: Final = frozenset(TRIGGER_DDL)


def create_schema_v1(connection: sqlite3.Connection, *, applied_at_utc_us: int) -> None:
    """Create exact schema version 1 in one transaction."""
    if (
        type(applied_at_utc_us) is not int
        or not 0 <= applied_at_utc_us <= MAX_TIMESTAMP_US
    ):
        raise SchemaVerificationError("invalid_migration_timestamp")
    try:
        if _pragma_integer(connection, "auto_vacuum") != AUTO_VACUUM_INCREMENTAL:
            raise SchemaVerificationError("auto_vacuum_mismatch")
        connection.execute("BEGIN IMMEDIATE")
        for statement in TABLE_DDL.values():
            connection.execute(statement)
        for statement in INDEX_DDL.values():
            connection.execute(statement)
        for statement in TRIGGER_DDL.values():
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at_utc_us) VALUES (?, ?)",
            (SCHEMA_VERSION, applied_at_utc_us),
        )
        connection.execute("INSERT INTO ingestion_checkpoint(singleton_id) VALUES (1)")
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
        if _pragma_integer(connection, "auto_vacuum") != AUTO_VACUUM_INCREMENTAL:
            raise SchemaVerificationError("auto_vacuum_mismatch")
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
            **{("trigger", name): sql for name, sql in TRIGGER_DDL.items()},
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
        checkpoint = connection.execute(
            "SELECT singleton_id, last_committed_sequence, "
            "last_accepted_observed_at_utc_us, "
            "last_accepted_projection_digest, last_accepted_sample_kind, "
            "accepted_observation_count FROM ingestion_checkpoint LIMIT 2"
        ).fetchall()
        if len(checkpoint) != 1 or not _valid_checkpoint_row(checkpoint[0]):
            raise SchemaVerificationError("ingestion_checkpoint_mismatch")
        replay_rows = connection.execute(
            "SELECT observation_sequence, observed_at_utc_us, projection_digest, "
            "accepted_sample_kind FROM accepted_observation_replay "
            "ORDER BY observation_sequence LIMIT ?",
            (REPLAY_LEDGER_CAPACITY + 1,),
        ).fetchall()
        if not _valid_replay_rows(checkpoint[0], replay_rows):
            raise SchemaVerificationError("ingestion_checkpoint_mismatch")
        evaluation_rows = connection.execute(
            "SELECT scope, current_status, candidate_status, consecutive_count, "
            "last_sample_id, last_heartbeat_at_utc_us, gap_phase, "
            "cooldown_until_utc_us FROM evaluation_state ORDER BY scope LIMIT ?",
            (len(AlertScope) + 1,),
        ).fetchall()
        if not _valid_evaluation_rows(evaluation_rows):
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
        completed_at = monotonic()
    except sqlite3.Error as error:
        raise SchemaVerificationError("quick_check_failed") from error
    finally:
        connection.set_progress_handler(None, 0)
    if completed_at > deadline or rows != [("ok",)]:
        raise SchemaVerificationError("quick_check_failed")


def _pragma_integer(connection: sqlite3.Connection, name: str) -> int:
    row = connection.execute(f"PRAGMA {name}").fetchone()
    if row is None or type(row[0]) is not int:
        raise SchemaVerificationError("pragma_mismatch")
    return row[0]


def _normalize_sql(statement: str) -> str:
    return " ".join(statement.rstrip().rstrip(";").split()).lower()


def _valid_checkpoint_row(row: sqlite3.Row | tuple[object, ...]) -> bool:
    singleton_id, sequence, observed_at, digest, sample_kind, count = row
    if (
        singleton_id != 1
        or type(count) is not int
        or not 0 <= count <= MAX_BOUNDED_COUNTER
    ):
        return False
    empty = (
        sequence is None
        and observed_at is None
        and digest is None
        and sample_kind is None
        and count == 0
    )
    populated = (
        type(sequence) is int
        and 0 <= sequence <= MAX_OBSERVATION_SEQUENCE
        and type(observed_at) is int
        and 0 <= observed_at <= MAX_TIMESTAMP_US
        and type(digest) is bytes
        and len(digest) == PROJECTION_DIGEST_BYTES
        and sample_kind in {kind.value for kind in SampleKind}
        and count >= 1
    )
    return empty or populated


def _valid_replay_rows(
    checkpoint: sqlite3.Row | tuple[object, ...],
    rows: list[sqlite3.Row] | list[tuple[object, ...]],
) -> bool:
    _singleton_id, sequence, observed_at, digest, sample_kind, count = checkpoint
    if type(count) is not int:
        return False
    expected_rows = min(count, REPLAY_LEDGER_CAPACITY)
    if len(rows) != expected_rows:
        return False
    if sequence is None:
        return count == 0
    if not rows:
        return False
    for row in rows:
        row_sequence, row_observed, row_digest, row_kind = row
        if (
            type(row_sequence) is not int
            or not 0 <= row_sequence <= MAX_OBSERVATION_SEQUENCE
            or type(row_observed) is not int
            or not 0 <= row_observed <= MAX_TIMESTAMP_US
            or type(row_digest) is not bytes
            or len(row_digest) != PROJECTION_DIGEST_BYTES
            or row_kind not in {kind.value for kind in SampleKind}
        ):
            return False
    latest = rows[-1]
    return latest == (sequence, observed_at, digest, sample_kind)


def _valid_evaluation_rows(
    rows: list[sqlite3.Row] | list[tuple[object, ...]],
) -> bool:
    if len(rows) != len(AlertScope):
        return False
    seen: set[AlertScope] = set()
    for row in rows:
        try:
            scope = AlertScope(cast(str, row[0]))
            current = None if row[1] is None else HealthHistoryStatus(cast(str, row[1]))
            candidate = (
                None if row[2] is None else HealthHistoryStatus(cast(str, row[2]))
            )
            gap_phase = SamplingGapPhase(cast(str, row[6]))
        except (TypeError, ValueError):
            return False
        count, sample_id, heartbeat_at, cooldown_until = row[3], row[4], row[5], row[7]
        if (
            scope in seen
            or type(count) is not int
            or not 0 <= count <= MAX_BOUNDED_COUNTER
            or (sample_id is not None and (type(sample_id) is not int or sample_id < 1))
        ):
            return False
        for timestamp in (heartbeat_at, cooldown_until):
            if timestamp is not None and (
                type(timestamp) is not int or not 0 <= timestamp <= MAX_TIMESTAMP_US
            ):
                return False
        if scope is AlertScope.SAMPLING:
            if current is not None or candidate is not None:
                return False
        else:
            if gap_phase is not SamplingGapPhase.CLEAR:
                return False
            try:
                from aurora_core.health_history.evaluation import HealthEvaluationState

                HealthEvaluationState(current, candidate, count)
            except ValueError:
                return False
        seen.add(scope)
    return seen == set(AlertScope)
