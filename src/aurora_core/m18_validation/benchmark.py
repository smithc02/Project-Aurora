"""Disposable SQLite endurance benchmark for Milestone 18 design gates."""

from __future__ import annotations

import multiprocessing
import os
import random
import resource
import sqlite3
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from aurora_core.m18_validation.filesystem import (
    FilesystemBoundaryError,
    create_secure_file,
    require_same_identity,
    validate_protected_directory,
    validate_regular_file,
)
from aurora_core.m18_validation.models import (
    CheckResult,
    CheckStatus,
    Measurement,
    MeasurementKind,
    ToolReport,
)
from aurora_core.m18_validation.platform import BUSY_TIMEOUT_MS, PAGE_SIZE

BENCHMARK_APPLICATION_ID = 0x4D313842
RESERVED_PRODUCTION_APPLICATION_ID = 0x41555248
BENCHMARK_SCHEMA_VERSION = 1
PROJECTED_SAMPLE_INTERVAL_SECONDS = 30
PROJECTED_TRANSACTIONS_PER_HOUR = 120
PROJECTED_TRANSACTIONS_PER_DAY = 2880
MAX_CLEANUP_ROWS = 500


class BenchmarkScenario(StrEnum):
    HEALTHY = "healthy"
    MIXED = "mixed"
    TRANSITION_HEAVY = "transition-heavy"
    GAP_RECOVERY = "gap-recovery"


class _StartableProcess(Protocol):
    def start(self) -> None: ...


@dataclass(frozen=True, slots=True)
class BenchmarkConfig:
    transactions: int = 240
    seed: int = 18
    scenario: BenchmarkScenario = BenchmarkScenario.TRANSITION_HEAVY
    pace_seconds: float = 0.0
    checkpoint_interval: int = 60
    cleanup_interval: int = 120

    def validate(self) -> None:
        if not 1 <= self.transactions <= 100_000:
            raise ValueError("transaction_count")
        if not 0 <= self.seed <= 0xFFFFFFFF:
            raise ValueError("seed")
        if not 0.0 <= self.pace_seconds <= 30.0:
            raise ValueError("pace")
        if not 1 <= self.checkpoint_interval <= 10_000:
            raise ValueError("checkpoint_interval")
        if not 1 <= self.cleanup_interval <= 10_000:
            raise ValueError("cleanup_interval")


@dataclass(slots=True)
class _Counters:
    sample_transactions: int = 0
    maintenance_transactions: int = 0
    history_rows: int = 0
    component_rows: int = 0
    alert_events: int = 0
    checkpoints: int = 0
    checkpoint_seconds: float = 0.0
    checkpoint_bytes: int = 0
    cleanup_seconds: float = 0.0
    cleanup_rows: int = 0
    peak_main_bytes: int = 0
    peak_wal_bytes: int = 0
    peak_shm_bytes: int = 0
    peak_total_bytes: int = 0


def run_benchmark(root: Path, config: BenchmarkConfig) -> ToolReport:
    checks: list[CheckResult] = []
    measurements: list[Measurement] = []
    try:
        config.validate()
        validate_protected_directory(root)
    except (ValueError, FilesystemBoundaryError):
        return ToolReport(
            "aurora.m18.sqlite-benchmark.v1",
            (
                CheckResult(
                    "benchmark_boundary",
                    CheckStatus.FAIL,
                    "configuration or protected test-directory validation failed",
                ),
            ),
        )

    database = root / f"m18-benchmark-{config.scenario.value}.sqlite3"
    try:
        create_secure_file(database)
        connection = _connect_existing_database(database)
        _create_schema(connection)
    except (OSError, sqlite3.Error, FilesystemBoundaryError):
        return ToolReport(
            "aurora.m18.sqlite-benchmark.v1",
            (
                CheckResult(
                    "benchmark_database_creation",
                    CheckStatus.FAIL,
                    "disposable benchmark database creation failed",
                ),
            ),
        )

    checks.append(
        CheckResult(
            "benchmark_identity",
            CheckStatus.PASS,
            "benchmark application_id is fixed and distinct from the reserved "
            "production identity",
        )
    )
    counters = _Counters()
    setup_files = _managed_sizes(database)
    _record_managed_sizes(counters, setup_files)
    rng = random.Random(config.seed)
    before_writes = _linux_write_bytes()
    before_cpu = time.process_time()
    started = time.monotonic()
    previous_statuses: tuple[str, ...] | None = None
    try:
        for sequence in range(1, config.transactions + 1):
            statuses, missed = _scenario_sample(config.scenario, sequence, rng)
            transition = previous_statuses is None or statuses != previous_statuses
            heartbeat = sequence % 30 == 0
            sample_kind = (
                "gap"
                if missed > 0
                else "transition"
                if transition
                else "heartbeat"
                if heartbeat
                else None
            )
            _commit_sample(
                connection,
                sequence=sequence,
                statuses=statuses,
                missed_intervals=missed,
                sample_kind=sample_kind,
                counters=counters,
            )
            previous_statuses = statuses
            if sequence % config.cleanup_interval == 0:
                _bounded_cleanup(connection, counters)
            if sequence % config.checkpoint_interval == 0:
                _bounded_checkpoint(connection, database, counters)
            if config.pace_seconds:
                time.sleep(config.pace_seconds)
        _bounded_cleanup(connection, counters)
        _bounded_checkpoint(connection, database, counters)
    except (sqlite3.Error, OSError):
        checks.append(
            CheckResult(
                "synthetic_workload",
                CheckStatus.FAIL,
                "the fixed synthetic workload failed without retry",
            )
        )
    else:
        checks.append(
            CheckResult(
                "synthetic_workload",
                CheckStatus.PASS,
                "all sample, maintenance, and checkpoint attempts were bounded "
                "and completed without retry",
            )
        )
    elapsed = time.monotonic() - started
    cpu = time.process_time() - before_cpu
    after_writes = _linux_write_bytes()
    connection.close()

    restart_ok = _validate_database(database)
    checks.append(
        CheckResult(
            "clean_restart",
            CheckStatus.PASS if restart_ok else CheckStatus.FAIL,
            "committed state, integrity, schema, and benchmark identity survived "
            "clean restart"
            if restart_ok
            else "clean restart validation failed",
        )
    )
    crash_ok = _crash_recovery(root, config.scenario)
    checks.append(
        CheckResult(
            "abrupt_termination_recovery",
            CheckStatus.PASS if crash_ok else CheckStatus.FAIL,
            "SQLite recovered committed state and discarded the fixed uncommitted "
            "child transaction"
            if crash_ok
            else "fixed abrupt-termination recovery probe failed",
        )
    )
    identity_ok = _identity_matches(database)
    checks.append(
        CheckResult(
            "schema_and_application_identity",
            CheckStatus.PASS if identity_ok else CheckStatus.FAIL,
            "schema version and benchmark-only application identity match"
            if identity_ok
            else "schema or benchmark application identity mismatch",
        )
    )
    integrity_ok = _quick_check(database)
    checks.append(
        CheckResult(
            "integrity",
            CheckStatus.PASS if integrity_ok else CheckStatus.FAIL,
            "one bounded quick_check(1) returned ok"
            if integrity_ok
            else "bounded integrity probe failed",
        )
    )
    final_files = _managed_sizes(database)
    write_delta = (
        None
        if before_writes is None or after_writes is None
        else after_writes - before_writes
        if after_writes >= before_writes
        else None
    )
    rate = counters.sample_transactions / elapsed if elapsed > 0 else 0.0
    max_rss = _maximum_resident_bytes()
    measurements.extend(
        (
            _measured(
                "committed_sample_transactions",
                counters.sample_transactions,
                "transactions",
            ),
            _measured(
                "committed_maintenance_transactions",
                counters.maintenance_transactions,
                "transactions",
            ),
            _measured("transactions_per_second", rate, "transactions/second"),
            _projected(
                "transactions_per_hour_at_30_seconds",
                PROJECTED_TRANSACTIONS_PER_HOUR,
                "transactions/hour",
            ),
            _projected(
                "transactions_per_day_at_30_seconds",
                PROJECTED_TRANSACTIONS_PER_DAY,
                "transactions/day",
            ),
            *_storage_measurements(setup_files, counters, final_files),
            _write_measurement("process_write_bytes", write_delta),
            _project_write_measurement(
                "projected_write_bytes_per_hour",
                write_delta,
                counters.sample_transactions,
                PROJECTED_TRANSACTIONS_PER_HOUR,
                "bytes/hour",
            ),
            _project_write_measurement(
                "projected_write_bytes_per_day",
                write_delta,
                counters.sample_transactions,
                PROJECTED_TRANSACTIONS_PER_DAY,
                "bytes/day",
            ),
            _measured("checkpoint_count", counters.checkpoints, "checkpoints"),
            _measured("checkpoint_duration", counters.checkpoint_seconds, "seconds"),
            _measured("checkpoint_bytes_moved", counters.checkpoint_bytes, "bytes"),
            _measured("cleanup_duration", counters.cleanup_seconds, "seconds"),
            _measured("cleanup_rows_removed", counters.cleanup_rows, "rows"),
            _measured("history_rows_inserted", counters.history_rows, "rows"),
            _measured("component_rows_inserted", counters.component_rows, "rows"),
            _measured("alert_events_inserted", counters.alert_events, "rows"),
            _measured("elapsed_wall_time", elapsed, "seconds"),
            _measured("cpu_time", cpu, "seconds"),
            _measured("maximum_resident_memory", max_rss, "bytes"),
            Measurement(
                "proposed_main_database_limit",
                MeasurementKind.ARCHITECTURE_LIMIT,
                64 * 1024 * 1024,
                "bytes",
                "provisional architecture limit; not approved by this run",
            ),
            Measurement(
                "target_platform_acceptance",
                MeasurementKind.DECISION_PENDING,
                None,
                note=(
                    "30-second sampling, 30-day retention, and 64-MiB defaults "
                    "require reviewed Raspberry Pi measurements"
                ),
            ),
        )
    )
    return ToolReport(
        "aurora.m18.sqlite-benchmark.v1", tuple(checks), tuple(measurements)
    )


def _connect_existing_database(path: Path) -> sqlite3.Connection:
    """Open one securely precreated benchmark database without creating it."""
    identity = validate_regular_file(path)
    uri = f"file:{quote(path.as_posix(), safe='/')}?mode=rw"
    previous_umask = os.umask(0o077)
    try:
        connection = sqlite3.connect(
            uri,
            uri=True,
            timeout=BUSY_TIMEOUT_MS / 1000,
        )
        try:
            require_same_identity(path, identity)
        except FilesystemBoundaryError:
            connection.close()
            raise
        return connection
    finally:
        os.umask(previous_umask)


def _create_schema(connection: sqlite3.Connection) -> None:
    if BENCHMARK_APPLICATION_ID == RESERVED_PRODUCTION_APPLICATION_ID:
        raise sqlite3.DatabaseError("invalid benchmark identity")
    connection.execute(f"PRAGMA page_size={PAGE_SIZE}")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute(f"PRAGMA application_id={BENCHMARK_APPLICATION_ID}")
    connection.execute(f"PRAGMA user_version={BENCHMARK_SCHEMA_VERSION}")
    connection.executescript(
        """
        CREATE TABLE benchmark_identity(
            marker TEXT PRIMARY KEY CHECK(marker='synthetic-benchmark-only')
        ) WITHOUT ROWID;
        INSERT INTO benchmark_identity(marker) VALUES ('synthetic-benchmark-only');
        CREATE TABLE evaluation_state(
            scope TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            candidate TEXT NOT NULL,
            consecutive INTEGER NOT NULL,
            missed_intervals INTEGER NOT NULL,
            alert_open INTEGER NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE history(
            id INTEGER PRIMARY KEY,
            sequence INTEGER NOT NULL,
            kind TEXT NOT NULL,
            overall_status TEXT NOT NULL
        );
        CREATE TABLE components(
            history_id INTEGER NOT NULL REFERENCES history(id) ON DELETE CASCADE,
            component TEXT NOT NULL,
            status TEXT NOT NULL,
            PRIMARY KEY(history_id, component)
        ) WITHOUT ROWID;
        CREATE TABLE alerts(
            scope TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            occurrence_count INTEGER NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE alert_events(
            id INTEGER PRIMARY KEY,
            sequence INTEGER NOT NULL,
            scope TEXT NOT NULL,
            event TEXT NOT NULL
        );
        """
    )
    for scope in ("overall", "wled", "hyperhdr", "capture", "raspberry_pi"):
        connection.execute(
            "INSERT INTO evaluation_state VALUES (?, 'healthy', 'healthy', 0, 0, 0)",
            (scope,),
        )
    connection.commit()


def _scenario_sample(
    scenario: BenchmarkScenario, sequence: int, rng: random.Random
) -> tuple[tuple[str, ...], int]:
    healthy = ("healthy", "healthy", "healthy", "healthy")
    if scenario is BenchmarkScenario.HEALTHY:
        return healthy, 0
    if scenario is BenchmarkScenario.TRANSITION_HEAVY:
        value = "healthy" if sequence % 2 else "unavailable"
        return (value, "degraded" if value == "healthy" else value, value, value), 0
    if scenario is BenchmarkScenario.GAP_RECOVERY:
        phase = sequence % 12
        missed = 2 if phase == 4 else 1 if phase == 5 else 0
        state = "degraded" if phase in {4, 5, 6} else "healthy"
        return (state, state, "healthy", "healthy"), missed
    values = ("healthy", "degraded", "unavailable")
    return tuple(rng.choices(values, weights=(7, 2, 1), k=4)), 0


def _commit_sample(
    connection: sqlite3.Connection,
    *,
    sequence: int,
    statuses: tuple[str, ...],
    missed_intervals: int,
    sample_kind: str | None,
    counters: _Counters,
) -> None:
    components = ("wled", "hyperhdr", "capture", "raspberry_pi")
    overall = (
        "unavailable"
        if "unavailable" in statuses
        else "degraded"
        if "degraded" in statuses
        else "healthy"
    )
    connection.execute("BEGIN IMMEDIATE")
    try:
        for scope, status in zip(components, statuses, strict=True):
            prior = connection.execute(
                "SELECT status, consecutive, alert_open FROM evaluation_state "
                "WHERE scope=?",
                (scope,),
            ).fetchone()
            if prior is None:
                raise sqlite3.IntegrityError("missing synthetic state")
            changed = prior[0] != status
            consecutive = 1 if changed else min(int(prior[1]) + 1, 255)
            alert_open = int(status != "healthy")
            connection.execute(
                "UPDATE evaluation_state SET status=?, candidate=?, consecutive=?, "
                "missed_intervals=?, alert_open=? WHERE scope=?",
                (status, status, consecutive, missed_intervals, alert_open, scope),
            )
            if changed:
                event = "opened" if alert_open else "recovered"
                connection.execute(
                    "INSERT INTO alert_events(sequence, scope, event) VALUES (?, ?, ?)",
                    (sequence, scope, event),
                )
                connection.execute(
                    "INSERT INTO alerts(scope, state, occurrence_count) "
                    "VALUES (?, ?, 1) ON CONFLICT(scope) DO UPDATE SET "
                    "state=excluded.state, occurrence_count="
                    "min(alerts.occurrence_count+1, 65535)",
                    (scope, event),
                )
                counters.alert_events += 1
        connection.execute(
            "UPDATE evaluation_state SET status=?, candidate=?, "
            "consecutive=min(consecutive+1,255), missed_intervals=? "
            "WHERE scope='overall'",
            (overall, overall, missed_intervals),
        )
        if sample_kind is not None:
            cursor = connection.execute(
                "INSERT INTO history(sequence, kind, overall_status) VALUES (?, ?, ?)",
                (sequence, sample_kind, overall),
            )
            history_id = cursor.lastrowid
            if history_id is None:
                raise sqlite3.IntegrityError("missing synthetic id")
            connection.executemany(
                "INSERT INTO components(history_id, component, status) "
                "VALUES (?, ?, ?)",
                (
                    (history_id, component, status)
                    for component, status in zip(components, statuses, strict=True)
                ),
            )
            counters.history_rows += 1
            counters.component_rows += len(components)
        connection.commit()
        counters.sample_transactions += 1
    except Exception:
        connection.rollback()
        raise


def _bounded_cleanup(connection: sqlite3.Connection, counters: _Counters) -> None:
    started = time.monotonic()
    count_row = connection.execute("SELECT count(*) FROM history").fetchone()
    if count_row is None:
        raise sqlite3.IntegrityError("missing synthetic history count")
    delete_limit = min(max(0, int(count_row[0]) - 64), MAX_CLEANUP_ROWS)
    connection.execute("BEGIN IMMEDIATE")
    try:
        cursor = connection.execute(
            "DELETE FROM history WHERE id IN "
            "(SELECT id FROM history ORDER BY id LIMIT ?)",
            (delete_limit,),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    deleted = max(0, cursor.rowcount)
    counters.cleanup_rows += min(deleted, MAX_CLEANUP_ROWS)
    counters.cleanup_seconds += time.monotonic() - started
    counters.maintenance_transactions += 1


def _bounded_checkpoint(
    connection: sqlite3.Connection, database: Path, counters: _Counters
) -> None:
    sizes = _managed_sizes(database)
    _record_managed_sizes(counters, sizes)
    wal = Path(f"{database}-wal")
    before = wal.stat().st_size if wal.exists() else 0
    if before > 4 * 1024 * 1024:
        raise sqlite3.OperationalError("synthetic WAL input limit")
    started = time.monotonic()
    result = connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
    duration = time.monotonic() - started
    if result is None or len(result) != 3 or result[0] != 0 or duration > 1.0:
        raise sqlite3.OperationalError("synthetic checkpoint budget")
    counters.checkpoints += 1
    counters.checkpoint_seconds += duration
    counters.checkpoint_bytes += int(result[2]) * PAGE_SIZE
    _record_managed_sizes(counters, _managed_sizes(database))


def _validate_database(path: Path) -> bool:
    try:
        validate_regular_file(path)
        connection = _connect_existing_database(path)
        try:
            identity = connection.execute("PRAGMA application_id").fetchone()
            version = connection.execute("PRAGMA user_version").fetchone()
            quick = connection.execute("PRAGMA quick_check(1)").fetchmany(2)
            rows = connection.execute(
                "SELECT count(*) FROM benchmark_identity WHERE marker=?",
                ("synthetic-benchmark-only",),
            ).fetchone()
            return (
                identity == (BENCHMARK_APPLICATION_ID,)
                and version == (BENCHMARK_SCHEMA_VERSION,)
                and quick == [("ok",)]
                and rows == (1,)
            )
        finally:
            connection.close()
    except (OSError, sqlite3.Error, FilesystemBoundaryError):
        return False


def _crash_recovery(root: Path, scenario: BenchmarkScenario) -> bool:
    path = root / f"m18-fixed-crash-probe-{scenario.value}.sqlite3"
    try:
        create_secure_file(path)
        connection = _connect_existing_database(path)
        _create_schema(connection)
        connection.execute("CREATE TABLE crash_probe(value TEXT NOT NULL)")
        connection.execute("INSERT INTO crash_probe(value) VALUES ('committed')")
        connection.commit()
        connection.close()
        context = multiprocessing.get_context("spawn")
        process = context.Process(target=_crash_child, args=(path,))
        _start_without_coverage_hooks(process)
        process.join(5.0)
        if process.is_alive():
            process.terminate()
            process.join(1.0)
            return False
        if process.exitcode != 23:
            return False
        recovered = _connect_existing_database(path)
        try:
            values = recovered.execute(
                "SELECT value FROM crash_probe ORDER BY rowid"
            ).fetchall()
            quick = recovered.execute("PRAGMA quick_check(1)").fetchmany(2)
            return values == [("committed",)] and quick == [("ok",)]
        finally:
            recovered.close()
    except (OSError, sqlite3.Error, FilesystemBoundaryError):
        return False


def _crash_child(path: Path) -> None:
    connection = _connect_existing_database(path)
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("INSERT INTO crash_probe(value) VALUES ('uncommitted')")
    os._exit(23)


def _start_without_coverage_hooks(process: _StartableProcess) -> None:
    keys = tuple(
        key
        for key in os.environ
        if key.startswith("COV_CORE_") or key == "COVERAGE_PROCESS_START"
    )
    saved = {key: os.environ.pop(key) for key in keys}
    try:
        process.start()
    finally:
        os.environ.update(saved)


def _identity_matches(path: Path) -> bool:
    try:
        connection = _connect_existing_database(path)
        try:
            application_id = connection.execute("PRAGMA application_id").fetchone()
            user_version = connection.execute("PRAGMA user_version").fetchone()
            return bool(
                application_id == (BENCHMARK_APPLICATION_ID,)
                and user_version == (BENCHMARK_SCHEMA_VERSION,)
            )
        finally:
            connection.close()
    except (OSError, sqlite3.Error, FilesystemBoundaryError):
        return False


def _quick_check(path: Path) -> bool:
    try:
        connection = _connect_existing_database(path)
        started = time.monotonic()
        try:
            return (
                connection.execute("PRAGMA quick_check(1)").fetchmany(2) == [("ok",)]
                and time.monotonic() - started <= 2.0
            )
        finally:
            connection.close()
    except (OSError, sqlite3.Error, FilesystemBoundaryError):
        return False


def _managed_sizes(path: Path) -> dict[str, int]:
    return {
        "main": path.stat().st_size if path.exists() else 0,
        "wal": Path(f"{path}-wal").stat().st_size
        if Path(f"{path}-wal").exists()
        else 0,
        "shm": Path(f"{path}-shm").stat().st_size
        if Path(f"{path}-shm").exists()
        else 0,
    }


def _record_managed_sizes(counters: _Counters, sizes: dict[str, int]) -> None:
    counters.peak_main_bytes = max(counters.peak_main_bytes, sizes["main"])
    counters.peak_wal_bytes = max(counters.peak_wal_bytes, sizes["wal"])
    counters.peak_shm_bytes = max(counters.peak_shm_bytes, sizes["shm"])
    counters.peak_total_bytes = max(counters.peak_total_bytes, sum(sizes.values()))


def _storage_measurements(
    setup: dict[str, int], counters: _Counters, final: dict[str, int]
) -> tuple[Measurement, ...]:
    setup_total = sum(setup.values())
    final_total = sum(final.values())
    return (
        _measured("setup_main_database_bytes", setup["main"], "bytes"),
        _measured("setup_wal_bytes", setup["wal"], "bytes"),
        _measured("setup_shared_memory_bytes", setup["shm"], "bytes"),
        _measured("setup_total_managed_bytes", setup_total, "bytes"),
        _measured("peak_main_database_bytes", counters.peak_main_bytes, "bytes"),
        _measured("peak_wal_bytes", counters.peak_wal_bytes, "bytes"),
        _measured("peak_shared_memory_bytes", counters.peak_shm_bytes, "bytes"),
        _measured("peak_total_managed_bytes", counters.peak_total_bytes, "bytes"),
        _measured("final_main_database_bytes", final["main"], "bytes"),
        _measured("final_wal_bytes", final["wal"], "bytes"),
        _measured("final_shared_memory_bytes", final["shm"], "bytes"),
        _measured("final_total_managed_bytes", final_total, "bytes"),
        Measurement(
            "workload_managed_file_growth_bytes",
            MeasurementKind.MEASURED,
            counters.peak_total_bytes - setup_total,
            "bytes",
            "peak managed-file growth above the measured post-schema baseline",
        ),
        Measurement(
            "workload_final_managed_file_delta_bytes",
            MeasurementKind.MEASURED,
            final_total - setup_total,
            "bytes",
            "signed final managed-file delta from the post-schema baseline",
        ),
    )


def _linux_write_bytes() -> int | None:
    path = Path("/proc/self/io")
    try:
        for line in path.read_text(encoding="ascii").splitlines():
            if line.startswith("write_bytes:"):
                return int(line.partition(":")[2].strip())
    except (OSError, UnicodeError, ValueError):
        return None
    return None


def _maximum_resident_bytes() -> int:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    import sys

    return int(usage if sys.platform == "darwin" else usage * 1024)


def _measured(name: str, value: int | float, unit: str) -> Measurement:
    return Measurement(name, MeasurementKind.MEASURED, value, unit)


def _projected(name: str, value: int | float, unit: str) -> Measurement:
    return Measurement(
        name,
        MeasurementKind.PROJECTED,
        value,
        unit,
        "architecture projection at the provisional 30-second interval",
    )


def _write_measurement(name: str, value: int | None) -> Measurement:
    if value is None:
        return Measurement(
            name,
            MeasurementKind.UNAVAILABLE,
            None,
            "bytes",
            "Linux /proc/self/io is unavailable on this platform",
        )
    return _measured(name, value, "bytes")


def _project_write_measurement(
    name: str,
    write_bytes: int | None,
    transactions: int,
    projected_transactions: int,
    unit: str,
) -> Measurement:
    if write_bytes is None or transactions == 0:
        return Measurement(
            name,
            MeasurementKind.UNAVAILABLE,
            None,
            unit,
            "requires Linux process-write accounting",
        )
    return Measurement(
        name,
        MeasurementKind.PROJECTED,
        round(write_bytes / transactions * projected_transactions),
        unit,
        "linear projection from measured synthetic process writes; final "
        "acceptance remains pending",
    )
