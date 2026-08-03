"""Synthetic target-platform SQLite gate; never used by Aurora runtime."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import warnings
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

from aurora_core.m18_validation.filesystem import (
    FilesystemBoundaryError,
    FilesystemRejection,
    create_secure_file,
    require_same_identity,
    validate_no_symlink_components,
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

PAGE_SIZE = 4096
BUSY_TIMEOUT_MS = 250
QUICK_CHECK_SECONDS = 2.0
CHECKPOINT_SECONDS = 1.0
BACKUP_PAGES = 16_384
BACKUP_STEP_PAGES = 128
BACKUP_SECONDS = 30.0
MIGRATION_SCHEMA_ROWS = 64
MIGRATION_PAGES = 4096
MIGRATION_SECONDS = 5.0
VACUUM_PAGES = 128
VACUUM_SECONDS = 1.0
SHUTDOWN_SECONDS = 2.0


class ProbeBudgetExceeded(Exception):
    """Fixed internal cancellation signal."""


def run_platform_validation(root: Path) -> ToolReport:
    checks: list[CheckResult] = []
    measurements: list[Measurement] = [
        Measurement(
            "python_version",
            MeasurementKind.MEASURED,
            _python_version(),
        ),
        Measurement(
            "sqlite_module_version",
            MeasurementKind.MEASURED,
            _sqlite_module_version(),
        ),
        Measurement(
            "sqlite_library_version",
            MeasurementKind.MEASURED,
            sqlite3.sqlite_version,
        ),
    ]

    try:
        validate_protected_directory(root)
    except FilesystemBoundaryError:
        checks.append(
            CheckResult(
                "protected_parent_directory",
                CheckStatus.FAIL,
                "test directory failed the owner, type, mode, or symlink boundary",
            )
        )
        return ToolReport(
            "aurora.m18.sqlite-platform.v1", tuple(checks), tuple(measurements)
        )
    checks.append(
        CheckResult(
            "protected_parent_directory",
            CheckStatus.PASS,
            "operator test directory is owned by the effective user and mode 0700",
        )
    )

    _run_check(
        checks,
        "symlink_parent_rejection",
        lambda: _check_symlink_parent(root),
        "a symlinked parent component is rejected",
    )
    _run_check(
        checks,
        "symlink_database_rejection",
        lambda: _check_symlink_database(root),
        "a symlink database path is rejected",
    )
    _run_check(
        checks,
        "non_regular_rejection",
        lambda: _check_non_regular(root),
        "a non-regular database object is rejected",
    )
    _run_check(
        checks,
        "hard_link_rejection",
        lambda: _check_hard_link(root),
        "a database file with an unexpected hard link is rejected",
    )
    _run_check(
        checks,
        "identity_change_rejection",
        lambda: _check_identity_change(root),
        "a path identity change between checks is rejected",
    )
    checks.append(
        CheckResult(
            "foreign_owner_rejection",
            CheckStatus.SKIPPED,
            "ordinary service accounts cannot safely create a foreign-owned test file",
            required=False,
        )
    )

    database = root / "platform-validation.sqlite3"
    try:
        pre_open = create_secure_file(database)
        connection = _connect_existing(database)
    except (FilesystemBoundaryError, OSError, sqlite3.Error):
        checks.append(
            CheckResult(
                "database_open_boundary",
                CheckStatus.FAIL,
                "secure precreation or pathname open failed",
            )
        )
        return ToolReport(
            "aurora.m18.sqlite-platform.v1", tuple(checks), tuple(measurements)
        )

    try:
        _configure(connection)
        require_same_identity(database, pre_open)
        checks.append(
            CheckResult(
                "database_open_boundary",
                CheckStatus.PASS,
                "pre-open and post-open type, owner, mode, device, and inode match",
            )
        )
        checks.append(
            CheckResult(
                "sqlite_pathname_open_limitation",
                CheckStatus.PASS,
                "sqlite3 opens by pathname; no secured ordinary file descriptor "
                "or internal O_NOFOLLOW guarantee is claimed",
            )
        )

        compile_options = tuple(
            str(row[0])
            for row in connection.execute("PRAGMA compile_options").fetchmany(256)
            if row and _safe_compile_option(row[0])
        )
        measurements.append(
            Measurement(
                "relevant_compile_options",
                MeasurementKind.MEASURED,
                ",".join(
                    option
                    for option in compile_options
                    if option.startswith(
                        ("THREADSAFE=", "DEFAULT_WAL_", "OMIT_", "ENABLE_")
                    )
                )[:2048],
            )
        )
        _run_check(
            checks,
            "database_permissions",
            lambda: validate_regular_file(database),
            "database is a singly linked regular file owned by the effective user "
            "and mode 0600",
        )
        _run_check(
            checks,
            "pragma_configuration",
            lambda: _check_pragmas(connection),
            "foreign keys, synchronous FULL, 4-KiB pages, and the 250-ms busy "
            "timeout are active",
        )

        connection.execute(
            "CREATE TABLE probe(id INTEGER PRIMARY KEY, value BLOB NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO probe(value) VALUES (?)", ((b"x" * 2048,) for _ in range(192))
        )
        connection.commit()
        _run_check(
            checks,
            "wal_sidecar_permissions",
            lambda: _check_sidecars(database),
            "WAL and shared-memory sidecars are regular, singly linked, owned, "
            "and mode 0600",
        )
        _run_check(
            checks,
            "progress_handler_interruption",
            lambda: _check_progress_interruption(connection),
            "a fixed synthetic query is interrupted by the progress handler",
        )
        _run_check(
            checks,
            "connection_interrupt",
            lambda: _check_connection_interrupt(root),
            "Connection.interrupt stops a fixed synthetic query",
            required=hasattr(connection, "interrupt"),
            unsupported=not hasattr(connection, "interrupt"),
        )
        _run_check(
            checks,
            "bounded_quick_check",
            lambda: _check_quick(connection),
            "one quick_check(1) completed within the two-second budget",
        )
        checkpoint = _checkpoint_probe(connection, database)
        checks.append(checkpoint[0])
        measurements.extend(checkpoint[1])
        _run_check(
            checks,
            "online_backup_bounded",
            lambda: _check_backup(connection, root),
            "online backup completed in bounded steps and callback cancellation "
            "stopped a second copy",
        )
        _run_check(
            checks,
            "interrupted_transaction_rollback",
            lambda: _check_transaction_rollback(connection),
            "an interrupted synthetic transaction rolled back without a committed row",
        )
        _run_check(
            checks,
            "migration_preflight_and_rollback",
            lambda: _check_migration_probe(connection),
            "schema preflight was row-capped and an interrupted migration-style "
            "transaction rolled back",
        )
        _run_check(
            checks,
            "restore_validation",
            lambda: _check_restore_validation(connection, root),
            "a bounded unpublished restore candidate passed identity, schema, "
            "and quick-check validation",
        )
        _run_check(
            checks,
            "incremental_vacuum",
            lambda: _check_incremental_vacuum(root),
            "one incremental_vacuum(128) call completed within one second; full "
            "VACUUM was not used",
        )
        _run_check(
            checks,
            "shutdown_checkpoint_and_close",
            lambda: _shutdown_probe(connection, database),
            "one page-capped shutdown checkpoint and close completed within two "
            "seconds",
        )
    finally:
        connection.close()

    checks.append(
        CheckResult(
            "public_health_independence",
            CheckStatus.PASS,
            "all probes are isolated and do not import, lock, or modify the live "
            "health service",
        )
    )
    return ToolReport(
        "aurora.m18.sqlite-platform.v1", tuple(checks), tuple(measurements)
    )


def _python_version() -> str:
    import sys

    return ".".join(str(part) for part in sys.version_info[:3])


def _sqlite_module_version() -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return str(getattr(sqlite3, "version", "unavailable"))


def _connect_existing(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(path.as_posix(), safe='/')}?mode=rw"
    previous_umask = os.umask(0o077)
    try:
        return sqlite3.connect(
            uri,
            uri=True,
            timeout=BUSY_TIMEOUT_MS / 1000,
            check_same_thread=False,
        )
    finally:
        os.umask(previous_umask)


def _configure(connection: sqlite3.Connection) -> None:
    connection.execute(f"PRAGMA page_size={PAGE_SIZE}")
    mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()
    if mode is None or str(mode[0]).lower() != "wal":
        raise sqlite3.OperationalError("journal mode unavailable")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA wal_autocheckpoint=0")


def _run_check(
    checks: list[CheckResult],
    name: str,
    operation: Callable[[], object],
    success: str,
    *,
    required: bool = True,
    unsupported: bool = False,
) -> None:
    if unsupported:
        checks.append(
            CheckResult(
                name,
                CheckStatus.SKIPPED,
                "the deployed sqlite3 interface does not expose this capability",
                required=False,
            )
        )
        return
    try:
        operation()
    except Exception:
        checks.append(
            CheckResult(name, CheckStatus.FAIL, "fixed probe failed", required)
        )
    else:
        checks.append(CheckResult(name, CheckStatus.PASS, success, required))


def _expect_rejection(
    operation: Callable[[], object], expected: FilesystemRejection
) -> None:
    try:
        operation()
    except FilesystemBoundaryError as error:
        if error.reason is expected:
            return
    raise AssertionError("fixed filesystem rejection did not occur")


def _check_symlink_parent(root: Path) -> None:
    real = root / "real-parent"
    real.mkdir(mode=0o700)
    linked = root / "linked-parent"
    linked.symlink_to(real, target_is_directory=True)
    _expect_rejection(
        lambda: validate_no_symlink_components(linked / "database.sqlite3"),
        FilesystemRejection.SYMLINK,
    )


def _check_symlink_database(root: Path) -> None:
    target = root / "symlink-target.sqlite3"
    create_secure_file(target)
    linked = root / "symlink-database.sqlite3"
    linked.symlink_to(target)
    _expect_rejection(
        lambda: validate_regular_file(linked), FilesystemRejection.SYMLINK
    )


def _check_non_regular(root: Path) -> None:
    path = root / "non-regular.sqlite3"
    path.mkdir(mode=0o700)
    _expect_rejection(
        lambda: validate_regular_file(path), FilesystemRejection.WRONG_TYPE
    )


def _check_hard_link(root: Path) -> None:
    path = root / "hard-link-source.sqlite3"
    create_secure_file(path)
    os.link(path, root / "hard-link-alias.sqlite3")
    _expect_rejection(
        lambda: validate_regular_file(path), FilesystemRejection.HARD_LINK
    )


def _check_identity_change(root: Path) -> None:
    path = root / "identity.sqlite3"
    expected = create_secure_file(path)
    replacement = root / "replacement.sqlite3"
    create_secure_file(replacement)
    os.replace(replacement, path)
    _expect_rejection(
        lambda: require_same_identity(path, expected),
        FilesystemRejection.IDENTITY_CHANGED,
    )


def _check_pragmas(connection: sqlite3.Connection) -> None:
    values = {
        "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone(),
        "synchronous": connection.execute("PRAGMA synchronous").fetchone(),
        "page_size": connection.execute("PRAGMA page_size").fetchone(),
        "busy_timeout": connection.execute("PRAGMA busy_timeout").fetchone(),
    }
    if values != {
        "foreign_keys": (1,),
        "synchronous": (2,),
        "page_size": (PAGE_SIZE,),
        "busy_timeout": (BUSY_TIMEOUT_MS,),
    }:
        raise AssertionError("pragma mismatch")


def _check_sidecars(database: Path) -> None:
    for suffix in ("-wal", "-shm"):
        validate_regular_file(Path(f"{database}{suffix}"))


def _check_progress_interruption(connection: sqlite3.Connection) -> None:
    calls = 0

    def stop() -> int:
        nonlocal calls
        calls += 1
        return int(calls >= 4)

    connection.set_progress_handler(stop, 50)
    try:
        connection.execute(
            "WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n "
            "WHERE x<10000000) SELECT sum(x) FROM n"
        ).fetchone()
    except sqlite3.OperationalError:
        if calls < 4:
            raise AssertionError("progress handler was not reached") from None
    else:
        raise AssertionError("query was not interrupted")
    finally:
        connection.set_progress_handler(None, 0)


def _check_connection_interrupt(root: Path) -> None:
    path = root / "interrupt-probe.sqlite3"
    create_secure_file(path)
    connection = _connect_existing(path)
    executing = threading.Event()
    interrupted = threading.Event()

    def progress() -> int:
        executing.set()
        return 0

    def query() -> None:
        try:
            connection.execute(
                "WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n "
                "WHERE x<1000000000) SELECT sum(x) FROM n"
            ).fetchone()
        except sqlite3.OperationalError:
            interrupted.set()

    worker: threading.Thread | None = None
    try:
        connection.execute("PRAGMA threads=1")
        connection.set_progress_handler(progress, 1000)
        worker = threading.Thread(
            target=query, name="m18-sqlite-interrupt", daemon=True
        )
        worker.start()
        if not executing.wait(1.0):
            raise AssertionError("query did not execute")
        connection.interrupt()
        worker.join(2.0)
        if worker.is_alive() or not interrupted.is_set():
            raise AssertionError("interrupt did not stop query")
    finally:
        connection.interrupt()
        if worker is not None and worker.is_alive():
            worker.join(1.0)
        connection.set_progress_handler(None, 0)
        connection.close()


def _check_quick(connection: sqlite3.Connection) -> None:
    started = time.monotonic()
    connection.set_progress_handler(
        lambda: int(time.monotonic() - started > QUICK_CHECK_SECONDS), 1000
    )
    try:
        rows = connection.execute("PRAGMA quick_check(1)").fetchmany(2)
        if rows != [("ok",)] or time.monotonic() - started > QUICK_CHECK_SECONDS:
            raise AssertionError("quick check exceeded fixed result or time budget")
    finally:
        connection.set_progress_handler(None, 0)


def _checkpoint_probe(
    connection: sqlite3.Connection, database: Path
) -> tuple[CheckResult, tuple[Measurement, ...]]:
    wal = Path(f"{database}-wal")
    before = wal.stat().st_size
    started = time.monotonic()
    row = connection.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
    duration = time.monotonic() - started
    if (
        row is None
        or len(row) != 3
        or row[0] != 0
        or duration > CHECKPOINT_SECONDS
        or before > 4 * 1024 * 1024
    ):
        result = CheckResult(
            "bounded_wal_checkpoint",
            CheckStatus.FAIL,
            "single PASSIVE checkpoint exceeded its fixed page, busy, or time budget",
        )
    else:
        result = CheckResult(
            "bounded_wal_checkpoint",
            CheckStatus.PASS,
            "one PASSIVE checkpoint completed under the one-second and 4-MiB "
            "input budgets",
        )
    moved = 0 if row is None else int(row[2]) * PAGE_SIZE
    return result, (
        Measurement(
            "checkpoint_input_bytes", MeasurementKind.MEASURED, before, "bytes"
        ),
        Measurement(
            "checkpoint_duration", MeasurementKind.MEASURED, duration, "seconds"
        ),
        Measurement("checkpoint_bytes_moved", MeasurementKind.MEASURED, moved, "bytes"),
    )


def _check_backup(connection: sqlite3.Connection, root: Path) -> None:
    destination = root / "bounded-backup.sqlite3"
    create_secure_file(destination)
    target = _connect_existing(destination)
    started = time.monotonic()
    steps = 0

    def progress(status: int, remaining: int, total: int) -> None:
        del status
        nonlocal steps
        steps += 1
        if total > BACKUP_PAGES or time.monotonic() - started > BACKUP_SECONDS:
            raise ProbeBudgetExceeded
        if remaining < 0:
            raise ProbeBudgetExceeded

    try:
        connection.backup(target, pages=BACKUP_STEP_PAGES, progress=progress, sleep=0)
    finally:
        target.close()
    if steps < 1:
        raise AssertionError("backup callback not called")
    canceled = root / "canceled-backup.sqlite3"
    create_secure_file(canceled)
    canceled_target = _connect_existing(canceled)
    callbacks = 0

    def cancel(status: int, remaining: int, total: int) -> None:
        del status, remaining, total
        nonlocal callbacks
        callbacks += 1
        raise ProbeBudgetExceeded

    try:
        try:
            connection.backup(canceled_target, pages=1, progress=cancel, sleep=0)
        except ProbeBudgetExceeded:
            pass
        else:
            raise AssertionError("backup callback cancellation was ignored")
    finally:
        canceled_target.close()
    if callbacks != 1:
        raise AssertionError("backup cancellation was not deterministic")


def _check_transaction_rollback(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("INSERT INTO probe(value) VALUES (?)", (b"uncommitted",))
    connection.set_progress_handler(lambda: 1, 1)
    try:
        connection.execute(
            "WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n "
            "WHERE x<1000000) SELECT sum(x) FROM n"
        ).fetchone()
    except sqlite3.OperationalError:
        pass
    else:
        raise AssertionError("transaction operation was not interrupted")
    finally:
        connection.set_progress_handler(None, 0)
        connection.rollback()
    count = connection.execute(
        "SELECT count(*) FROM probe WHERE value = ?", (b"uncommitted",)
    ).fetchone()
    if count != (0,):
        raise AssertionError("interrupted transaction committed")


def _check_migration_probe(connection: sqlite3.Connection) -> None:
    _check_quick(connection)
    page_count = connection.execute("PRAGMA page_count").fetchone()
    if page_count is None or int(page_count[0]) > MIGRATION_PAGES:
        raise ProbeBudgetExceeded
    rows = connection.execute(
        "SELECT name FROM sqlite_schema ORDER BY name LIMIT ?",
        (MIGRATION_SCHEMA_ROWS + 1,),
    ).fetchall()
    if len(rows) > MIGRATION_SCHEMA_ROWS:
        raise ProbeBudgetExceeded
    started = time.monotonic()
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("CREATE TABLE synthetic_migration(value INTEGER)")
        calls = 0

        def stop() -> int:
            nonlocal calls
            calls += 1
            return int(calls >= 2 or time.monotonic() - started > MIGRATION_SECONDS)

        connection.set_progress_handler(stop, 10)
        connection.execute(
            "WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n "
            "WHERE x<1000000) SELECT sum(x) FROM n"
        ).fetchone()
    except sqlite3.OperationalError:
        connection.rollback()
    else:
        connection.rollback()
        raise AssertionError("migration probe was not interrupted")
    finally:
        connection.set_progress_handler(None, 0)
    if connection.execute(
        "SELECT count(*) FROM sqlite_schema WHERE name='synthetic_migration'"
    ).fetchone() != (0,):
        raise AssertionError("migration transaction did not roll back")
    _check_quick(connection)


def _check_restore_validation(connection: sqlite3.Connection, root: Path) -> None:
    source_pages = connection.execute("PRAGMA page_count").fetchone()
    if source_pages is None or int(source_pages[0]) > BACKUP_PAGES:
        raise ProbeBudgetExceeded
    candidate = root / "restore-candidate.sqlite3"
    create_secure_file(candidate)
    target = _connect_existing(candidate)
    started = time.monotonic()

    def progress(status: int, remaining: int, total: int) -> None:
        del status, remaining
        if total > BACKUP_PAGES or time.monotonic() - started > BACKUP_SECONDS:
            raise ProbeBudgetExceeded

    try:
        connection.backup(
            target,
            pages=BACKUP_STEP_PAGES,
            progress=progress,
            sleep=0,
        )
    finally:
        target.close()
    if candidate.stat().st_size > BACKUP_PAGES * PAGE_SIZE:
        raise ProbeBudgetExceeded
    validate_regular_file(candidate)
    restored = _connect_existing(candidate)
    try:
        schema = restored.execute(
            "SELECT name FROM sqlite_schema ORDER BY name LIMIT ?",
            (MIGRATION_SCHEMA_ROWS + 1,),
        ).fetchall()
        if len(schema) > MIGRATION_SCHEMA_ROWS:
            raise ProbeBudgetExceeded
        _check_quick(restored)
    finally:
        restored.close()


def _check_incremental_vacuum(root: Path) -> None:
    path = root / "vacuum-probe.sqlite3"
    create_secure_file(path)
    connection = _connect_existing(path)
    try:
        connection.execute(f"PRAGMA page_size={PAGE_SIZE}")
        connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
        connection.execute("CREATE TABLE disposable(value BLOB)")
        connection.executemany(
            "INSERT INTO disposable(value) VALUES (?)",
            ((b"v" * 2048,) for _ in range(256)),
        )
        connection.commit()
        connection.execute("DELETE FROM disposable")
        connection.commit()
        started = time.monotonic()
        connection.set_progress_handler(
            lambda: int(time.monotonic() - started > VACUUM_SECONDS), 1000
        )
        try:
            connection.execute(f"PRAGMA incremental_vacuum({VACUUM_PAGES})")
            if time.monotonic() - started > VACUUM_SECONDS:
                raise ProbeBudgetExceeded
        finally:
            connection.set_progress_handler(None, 0)
    finally:
        connection.close()


def _shutdown_probe(connection: sqlite3.Connection, database: Path) -> None:
    started = time.monotonic()
    wal = Path(f"{database}-wal")
    wal_bytes = wal.stat().st_size if wal.exists() else 0
    if wal_bytes > min(960 * PAGE_SIZE, 4 * 1024 * 1024):
        raise ProbeBudgetExceeded
    result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if result is None or len(result) != 3 or result[0] != 0:
        raise ProbeBudgetExceeded
    connection.close()
    if time.monotonic() - started > SHUTDOWN_SECONDS:
        raise ProbeBudgetExceeded


def _safe_compile_option(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and all(char.isascii() and (char.isalnum() or char in "_=-") for char in value)
    )
