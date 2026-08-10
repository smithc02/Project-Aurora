"""Synthetic tests for the isolated Milestone 18 storage envelope."""

from __future__ import annotations

import inspect
import os
import socket
import sqlite3
import subprocess
import threading
import time
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import aurora_core.health_history.storage_envelope as envelope
import aurora_core.health_history.store as store_module
from aurora_core.health_history import (
    FREE_SPACE_RESERVE_BYTES,
    WAL_CHECKPOINT_THRESHOLD_FRAMES,
    WAL_HARD_LIMIT_BYTES,
    WAL_HARD_LIMIT_FRAMES,
    FreeSpaceResult,
    PassiveCheckpointOutcome,
    PassiveCheckpointResult,
    StorageCapacityResult,
    StorageDecisionOutcome,
    StorageDecisionResult,
    StorageEnvelopeError,
    StorageEnvelopeRejection,
    WalInspectionResult,
    decide_storage_action,
)
from aurora_core.health_history.filesystem import (
    FilesystemBoundaryError,
    FilesystemRejection,
)
from aurora_core.health_history.maintenance import (
    INCREMENTAL_VACUUM_PAGES,
    RETENTION_ROW_BUDGET,
)
from aurora_core.health_history.models import (
    APPLICATION_ID,
    MAX_DATABASE_BYTES,
    MAX_DATABASE_PAGES,
    PAGE_SIZE_BYTES,
    SCHEMA_VERSION,
)
from aurora_core.health_history.schema import SchemaVerificationError
from aurora_core.health_history.store import HealthHistoryStore, StoreError

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
def store_path(tmp_path: Path) -> tuple[Path, HealthHistoryStore]:
    tmp_path.chmod(0o700)
    path = tmp_path / "history.db"
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


def _rows(path: Path, sql: str) -> list[Any]:
    connection = _connect(path)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


def _snapshot(path: Path) -> dict[str, list[Any]]:
    return {
        table: _rows(path, f"SELECT * FROM {table} ORDER BY rowid") for table in _TABLES
    }


def _wal_size(frame_count: int) -> int:
    return envelope.WAL_HEADER_BYTES + frame_count * envelope.WAL_FRAME_BYTES


def _write_wal(path: Path, size: int) -> Path:
    wal = path.with_name(f"{path.name}-wal")
    with wal.open("wb") as output:
        output.truncate(size)
    wal.chmod(0o600)
    return wal


def _wal_result(
    logical_frame_count: int,
    *,
    physical_frame_slots: int | None = None,
    total_bytes: int | None = None,
    checkpointed_frames: int = 0,
) -> WalInspectionResult:
    if physical_frame_slots is None:
        physical_frame_slots = logical_frame_count
    if total_bytes is None:
        total_bytes = _wal_size(physical_frame_slots) if physical_frame_slots else 0
    oversize = (
        logical_frame_count > WAL_HARD_LIMIT_FRAMES
        or total_bytes > WAL_HARD_LIMIT_BYTES
    )
    return WalInspectionResult(
        exists=True,
        logical_frame_count=logical_frame_count,
        physical_frame_slots=physical_frame_slots,
        total_bytes=total_bytes,
        checkpointed_frames=checkpointed_frames,
        checkpoint_due=(
            logical_frame_count >= WAL_CHECKPOINT_THRESHOLD_FRAMES and not oversize
        ),
        oversize=oversize,
    )


class _FakeCursor:
    def __init__(
        self, rows: list[tuple[object, ...]], clock: list[float] | None = None
    ):
        self._rows = rows
        self._clock = clock
        self.fetch_sizes: list[int] = []

    def fetchmany(self, size: int) -> list[tuple[object, ...]]:
        self.fetch_sizes.append(size)
        if self._clock is not None:
            self._clock[0] = 2.0
        return self._rows[:size]


class _FakeConnection:
    def __init__(
        self,
        rows: list[tuple[object, ...]],
        *,
        error: sqlite3.Error | None = None,
        clock: list[float] | None = None,
    ) -> None:
        self.cursor = _FakeCursor(rows, clock)
        self.error = error
        self.execute_calls: list[str] = []
        self.progress_calls: list[tuple[object, int]] = []

    def execute(self, sql: str) -> _FakeCursor:
        self.execute_calls.append(sql)
        if self.error is not None:
            raise self.error
        return self.cursor

    def set_progress_handler(self, callback: object, steps: int) -> None:
        self.progress_calls.append((callback, steps))


def _fake_connection(
    rows: list[tuple[object, ...]],
    *,
    error: sqlite3.Error | None = None,
    clock: list[float] | None = None,
) -> tuple[sqlite3.Connection, _FakeConnection]:
    fake = _FakeConnection(rows, error=error, clock=clock)
    return cast(sqlite3.Connection, fake), fake


def _sqlite_error(code: int, message: str) -> sqlite3.OperationalError:
    error = sqlite3.OperationalError(message)
    error.sqlite_errorcode = code  # type: ignore[attr-defined]
    return error


@pytest.mark.parametrize("version", [(3, 51, 3), (3, 51, 4), (3, 53, 1)])
def test_safe_wal_sqlite_versions_are_accepted(
    monkeypatch: pytest.MonkeyPatch, version: tuple[int, int, int]
) -> None:
    monkeypatch.setattr(envelope.sqlite3, "sqlite_version_info", version)
    connection, fake = _fake_connection([(0, 1, 0)])
    assert envelope._read_noop_status(connection, None) == (1, 0, False)
    assert fake.execute_calls == ["PRAGMA wal_checkpoint(NOOP)"]


@pytest.mark.parametrize(
    "version",
    [(3, 51, 2), (3, 51, 1), (3, 51, 0), (3, 50, 99), (2, 99, 99)],
)
def test_unsafe_wal_sqlite_version_executes_no_checkpoint_sql(
    monkeypatch: pytest.MonkeyPatch, version: tuple[int, int, int]
) -> None:
    monkeypatch.setattr(envelope.sqlite3, "sqlite_version_info", version)
    connection, fake = _fake_connection([(0, 300, 0)])
    with pytest.raises(StorageEnvelopeError) as caught:
        envelope._passive_wal_checkpoint(
            connection, Path("unused.db"), monotonic=lambda: 0.0
        )
    assert caught.value.reason is StorageEnvelopeRejection.UNSUPPORTED_RUNTIME
    assert not caught.value.trust_lost
    assert str(caught.value) == "unsupported_runtime"
    assert fake.execute_calls == []
    assert fake.progress_calls == []


@pytest.mark.parametrize(
    "version",
    [
        None,
        [3, 51, 3],
        (3, 51),
        (3, 51, 3, 0),
        (True, 51, 3),
        (3, "51", 3),
        (3, 51, 3.0),
        (-1, 51, 3),
        (3, envelope._MAX_SQLITE_VERSION_COMPONENT + 1, 3),
    ],
)
def test_malformed_sqlite_version_fails_safe_wal_guard_before_noop(
    monkeypatch: pytest.MonkeyPatch, version: object
) -> None:
    monkeypatch.setattr(envelope.sqlite3, "sqlite_version_info", version)
    connection, fake = _fake_connection([(0, 1, 0)])
    with pytest.raises(StorageEnvelopeError) as caught:
        envelope._read_noop_status(connection, None)
    assert caught.value.reason is StorageEnvelopeRejection.UNSUPPORTED_RUNTIME
    assert not caught.value.trust_lost
    assert fake.execute_calls == []


def test_reviewed_pi_sqlite_3531_satisfies_safe_wal_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(envelope.sqlite3, "sqlite_version_info", (3, 53, 1))
    envelope._require_safe_wal_sqlite_version()


def test_new_database_capacity_and_fixed_connection_limit(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    before = _snapshot(path)
    result = store.inspect_storage_capacity()
    page_count = _rows(path, "PRAGMA page_count")[0][0]
    assert result == StorageCapacityResult(
        page_count=page_count,
        freelist_count=0,
        maximum_page_count=MAX_DATABASE_PAGES,
        used_bytes=page_count * PAGE_SIZE_BYTES,
        maximum_bytes=MAX_DATABASE_BYTES,
        pages_remaining=MAX_DATABASE_PAGES - page_count,
    )
    assert store._connection.execute("PRAGMA max_page_count").fetchone() == (  # noqa: SLF001
        MAX_DATABASE_PAGES,
    )
    assert _snapshot(path) == before


def test_max_page_count_is_set_on_every_connection_and_not_claimed_persistent(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    store.close()
    raw = _connect(path)
    assert raw.execute("PRAGMA max_page_count").fetchone() != (MAX_DATABASE_PAGES,)
    raw.close()
    reopened = HealthHistoryStore.open_existing(path)
    try:
        assert reopened._connection.execute(  # noqa: SLF001
            "PRAGMA max_page_count"
        ).fetchone() == (MAX_DATABASE_PAGES,)
        reopened.verify()
    finally:
        reopened.close()


def test_creation_and_open_fail_when_fixed_max_page_count_cannot_be_effective(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, store = store_path
    store.close()

    def fail(connection: sqlite3.Connection) -> None:
        del connection
        raise SchemaVerificationError("max_page_count_mismatch")

    monkeypatch.setattr(store_module, "_set_fixed_max_page_count", fail)
    with pytest.raises(StoreError) as caught:
        HealthHistoryStore.open_existing(path)
    assert caught.value.reason == "open_failed"


@pytest.mark.parametrize("page_count", [MAX_DATABASE_PAGES - 1, MAX_DATABASE_PAGES])
def test_capacity_fixed_boundary_values(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
    page_count: int,
) -> None:
    _path, store = store_path
    values = {
        envelope._PAGE_SIZE_SQL: PAGE_SIZE_BYTES,
        envelope._PAGE_COUNT_SQL: page_count,
        envelope._FREELIST_COUNT_SQL: 0,
        envelope._MAX_PAGE_COUNT_SQL: MAX_DATABASE_PAGES,
    }
    monkeypatch.setattr(
        envelope, "_pragma_integer", lambda connection, sql: values[sql]
    )
    result = store.inspect_storage_capacity()
    assert result.pages_remaining == MAX_DATABASE_PAGES - page_count
    assert result.used_bytes == page_count * PAGE_SIZE_BYTES


@pytest.mark.parametrize(
    ("sql", "value"),
    [
        (envelope._PAGE_SIZE_SQL, 1024),
        (envelope._PAGE_SIZE_SQL, True),
        (envelope._PAGE_COUNT_SQL, -1),
        (envelope._PAGE_COUNT_SQL, True),
        (envelope._PAGE_COUNT_SQL, 1.5),
        (envelope._PAGE_COUNT_SQL, MAX_DATABASE_PAGES + 1),
        (envelope._FREELIST_COUNT_SQL, -1),
        (envelope._FREELIST_COUNT_SQL, True),
        (envelope._FREELIST_COUNT_SQL, 2),
        (envelope._MAX_PAGE_COUNT_SQL, MAX_DATABASE_PAGES + 1),
    ],
)
def test_capacity_malformed_values_are_trust_loss(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
    sql: str,
    value: object,
) -> None:
    path, store = store_path
    before = _snapshot(path)
    values: dict[str, object] = {
        envelope._PAGE_SIZE_SQL: PAGE_SIZE_BYTES,
        envelope._PAGE_COUNT_SQL: 1,
        envelope._FREELIST_COUNT_SQL: 0,
        envelope._MAX_PAGE_COUNT_SQL: MAX_DATABASE_PAGES,
    }
    values[sql] = value
    monkeypatch.setattr(
        envelope, "_pragma_integer", lambda connection, statement: values[statement]
    )
    with pytest.raises(StorageEnvelopeError) as caught:
        store.inspect_storage_capacity()
    assert caught.value.reason is StorageEnvelopeRejection.MALFORMED_STATE
    assert caught.value.trust_lost
    assert store.closed
    assert _snapshot(path) == before


@pytest.mark.parametrize(
    "free_bytes",
    [
        FREE_SPACE_RESERVE_BYTES + 1,
        FREE_SPACE_RESERVE_BYTES,
        FREE_SPACE_RESERVE_BYTES - 1,
    ],
)
def test_free_space_fixed_reserve_boundaries(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
    free_bytes: int,
) -> None:
    path, store = store_path
    before = _snapshot(path)
    monkeypatch.setattr(
        envelope.shutil,
        "disk_usage",
        lambda parent: SimpleNamespace(
            total=FREE_SPACE_RESERVE_BYTES * 4,
            used=FREE_SPACE_RESERVE_BYTES,
            free=free_bytes,
        ),
    )
    result = store.inspect_free_space()
    assert result == FreeSpaceResult(
        free_bytes >= FREE_SPACE_RESERVE_BYTES,
        free_bytes,
        FREE_SPACE_RESERVE_BYTES,
    )
    assert str(result).find(str(path)) == -1
    assert _snapshot(path) == before


def test_free_space_inspection_error_is_sanitized_non_trust(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, store = store_path
    before = _snapshot(path)

    def fail(parent: Path) -> object:
        del parent
        raise OSError("private filesystem detail")

    monkeypatch.setattr(envelope.shutil, "disk_usage", fail)
    with pytest.raises(StorageEnvelopeError) as caught:
        store.inspect_free_space()
    assert caught.value.reason is StorageEnvelopeRejection.PERSISTENCE_FAILED
    assert str(caught.value) == "persistence_failed"
    assert not store.closed
    assert _snapshot(path) == before


@pytest.mark.parametrize(
    "usage",
    [
        SimpleNamespace(total=-1, used=0, free=0),
        SimpleNamespace(total=10, used=-1, free=1),
        SimpleNamespace(total=10, used=1, free=-1),
        SimpleNamespace(total=10, used=11, free=0),
        SimpleNamespace(total=10, used=0, free=11),
        SimpleNamespace(total=True, used=0, free=0),
    ],
)
def test_impossible_free_space_metadata_is_trust_loss(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
    usage: object,
) -> None:
    path, store = store_path
    before = _snapshot(path)
    monkeypatch.setattr(envelope.shutil, "disk_usage", lambda parent: usage)
    with pytest.raises(StorageEnvelopeError) as caught:
        store.inspect_free_space()
    assert caught.value.reason is StorageEnvelopeRejection.MALFORMED_STATE
    assert store.closed
    assert _snapshot(path) == before


def test_wal_absence_and_zero_length_file(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    path = tmp_path / "history.db"
    connection, fake = _fake_connection([(0, -1, -1)])
    assert envelope._inspect_wal(connection, path) == WalInspectionResult(
        False, 0, 0, 0, 0, False, False
    )
    assert fake.execute_calls == ["PRAGMA wal_checkpoint(NOOP)"]
    _write_wal(path, 0)
    connection, fake = _fake_connection([(0, 0, 0)])
    assert envelope._inspect_wal(connection, path) == WalInspectionResult(
        True, 0, 0, 0, 0, False, False
    )
    assert fake.cursor.fetch_sizes == [2]


def test_real_noop_no_wal_result_is_normalized_to_zero(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    connection = sqlite3.connect(":memory:")
    try:
        assert connection.execute("PRAGMA wal_checkpoint(NOOP)").fetchone() == (
            0,
            -1,
            -1,
        )
        result = envelope._inspect_wal(connection, tmp_path / "history.db")
    finally:
        connection.close()
    assert result == WalInspectionResult(False, 0, 0, 0, 0, False, False)


@pytest.mark.parametrize(
    ("frames", "checkpoint_due", "oversize"),
    [
        (0, False, False),
        (255, False, False),
        (256, True, False),
        (959, True, False),
        (960, True, False),
        (961, False, True),
    ],
)
def test_wal_frame_boundaries_from_fixed_framing(
    tmp_path: Path, frames: int, checkpoint_due: bool, oversize: bool
) -> None:
    tmp_path.chmod(0o700)
    path = tmp_path / f"history-{frames}.db"
    size = envelope.WAL_HEADER_BYTES if frames == 0 else _wal_size(frames)
    _write_wal(path, size)
    connection, _fake = _fake_connection([(0, frames, 0)])
    result = envelope._inspect_wal(connection, path)
    assert result == WalInspectionResult(
        True,
        frames,
        frames,
        size,
        0,
        checkpoint_due,
        oversize,
    )


def test_wal_hard_byte_boundary_arithmetic() -> None:
    below_frames = (
        WAL_HARD_LIMIT_BYTES - envelope.WAL_HEADER_BYTES
    ) // envelope.WAL_FRAME_BYTES
    below = _wal_size(below_frames)
    above = _wal_size(below_frames + 1)
    assert below <= WAL_HARD_LIMIT_BYTES < above
    assert envelope._wal_frame_count(below) == below_frames
    assert envelope._wal_frame_count(above) == below_frames + 1
    with pytest.raises(StorageEnvelopeError):
        envelope._wal_frame_count(WAL_HARD_LIMIT_BYTES)
    with pytest.raises(StorageEnvelopeError):
        envelope._wal_frame_count(WAL_HARD_LIMIT_BYTES + 1)


@pytest.mark.parametrize("size", [1, 31, 33, _wal_size(1) + 1, -1, True, 1.5])
def test_malformed_wal_sizes_fail_closed(size: object) -> None:
    with pytest.raises(StorageEnvelopeError) as caught:
        envelope._wal_frame_count(size)
    assert caught.value.reason is StorageEnvelopeRejection.MALFORMED_STATE
    assert caught.value.trust_lost


def test_wal_identity_replacement_is_trust_loss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path.chmod(0o700)
    path = tmp_path / "history.db"
    wal = _write_wal(path, _wal_size(1))
    replacement = tmp_path / "replacement"
    with replacement.open("wb") as output:
        output.truncate(_wal_size(1))
    replacement.chmod(0o600)
    original = envelope.validate_database_file
    calls = 0

    def replace(candidate: Path, **kwargs: object) -> Any:
        nonlocal calls
        result = original(candidate, **kwargs)  # type: ignore[arg-type]
        calls += 1
        if calls == 1:
            os.replace(replacement, wal)
        return result

    monkeypatch.setattr(envelope, "validate_database_file", replace)
    connection, _fake = _fake_connection([(0, 1, 0)])
    with pytest.raises(StorageEnvelopeError) as caught:
        envelope._inspect_wal(connection, path)
    assert caught.value.reason is StorageEnvelopeRejection.TRUST_FAILED
    assert caught.value.trust_lost


def test_noop_reports_current_logical_frames_not_physical_slots(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    path = tmp_path / "history.db"
    physical_slots = 500
    _write_wal(path, _wal_size(physical_slots))
    connection, fake = _fake_connection([(0, 3, 2)])
    result = envelope._inspect_wal(connection, path)
    assert result == WalInspectionResult(
        exists=True,
        logical_frame_count=3,
        physical_frame_slots=physical_slots,
        total_bytes=_wal_size(physical_slots),
        checkpointed_frames=2,
        checkpoint_due=False,
        oversize=False,
    )
    assert fake.execute_calls == ["PRAGMA wal_checkpoint(NOOP)"]
    assert fake.cursor.fetch_sizes == [2]


def test_physical_wal_above_four_mib_is_blocked_with_small_logical_generation(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    path = tmp_path / "history.db"
    physical_slots = (
        WAL_HARD_LIMIT_BYTES - envelope.WAL_HEADER_BYTES
    ) // envelope.WAL_FRAME_BYTES + 1
    physical_bytes = _wal_size(physical_slots)
    _write_wal(path, physical_bytes)
    connection, _fake = _fake_connection([(0, 1, 0)])
    result = envelope._inspect_wal(connection, path)
    assert result.logical_frame_count == 1
    assert result.physical_frame_slots == physical_slots
    assert result.total_bytes > WAL_HARD_LIMIT_BYTES
    assert result.oversize
    assert not result.checkpoint_due


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [(0, 1, 0), (0, 1, 0)],
        [(0, 1)],
        [(False, 1, 0)],
        [(0, True, 0)],
        [(0, 1, False)],
        [(0, -1, 0)],
        [(0, 0, -1)],
        [(2, 0, 0)],
        [(0, 1, 2)],
        [(0, envelope.MAX_WAL_INSPECTION_FRAMES + 1, 0)],
    ],
)
def test_noop_malformed_results_are_trust_loss(
    tmp_path: Path, rows: list[tuple[object, ...]]
) -> None:
    tmp_path.chmod(0o700)
    path = tmp_path / "history.db"
    _write_wal(path, _wal_size(1))
    connection, _fake = _fake_connection(rows)
    with pytest.raises(StorageEnvelopeError) as caught:
        envelope._inspect_wal(connection, path)
    assert caught.value.reason is StorageEnvelopeRejection.MALFORMED_STATE
    assert caught.value.trust_lost


def test_noop_checkpoint_lock_busy_is_sanitized_non_trust(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    path = tmp_path / "history.db"
    _write_wal(path, _wal_size(1))
    for connection, fake in (
        _fake_connection([(1, 1, 0)]),
        _fake_connection(
            [], error=_sqlite_error(sqlite3.SQLITE_BUSY, "private lock detail")
        ),
    ):
        with pytest.raises(StorageEnvelopeError) as caught:
            envelope._inspect_wal(connection, path)
        assert caught.value.reason is StorageEnvelopeRejection.STORAGE_BUSY
        assert not caught.value.trust_lost
        assert len(fake.execute_calls) == 1


def test_noop_logical_or_checkpointed_count_cannot_exceed_physical_state(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    path = tmp_path / "history.db"
    _write_wal(path, _wal_size(1))
    connection, _fake = _fake_connection([(0, 2, 0)])
    with pytest.raises(StorageEnvelopeError) as logical:
        envelope._inspect_wal(connection, path)
    assert logical.value.reason is StorageEnvelopeRejection.MALFORMED_STATE

    connection, _fake = _fake_connection([(0, 1, 2)])
    with pytest.raises(StorageEnvelopeError) as checkpointed:
        envelope._inspect_wal(connection, path)
    assert checkpointed.value.reason is StorageEnvelopeRejection.MALFORMED_STATE


def test_malformed_physical_size_is_trust_loss_with_valid_noop_fixture(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    path = tmp_path / "history.db"
    _write_wal(path, _wal_size(1) + 1)
    connection, _fake = _fake_connection([(0, 1, 0)])
    with pytest.raises(StorageEnvelopeError) as caught:
        envelope._inspect_wal(connection, path)
    assert caught.value.reason is StorageEnvelopeRejection.MALFORMED_STATE
    assert caught.value.trust_lost


@pytest.mark.parametrize(
    ("frames", "outcome"),
    [
        (255, PassiveCheckpointOutcome.NO_WORK),
        (961, PassiveCheckpointOutcome.OVERSIZE_BLOCKED),
    ],
)
def test_checkpoint_no_work_and_oversize_issue_no_pragma(
    monkeypatch: pytest.MonkeyPatch,
    frames: int,
    outcome: PassiveCheckpointOutcome,
) -> None:
    connection, fake = _fake_connection([])
    monkeypatch.setattr(
        envelope,
        "_inspect_wal",
        lambda connection, path, **kwargs: _wal_result(frames),
    )
    result = envelope._passive_wal_checkpoint(
        connection, Path("unused.db"), monotonic=lambda: 0.0
    )
    assert result.outcome is outcome
    assert fake.execute_calls == []
    assert fake.progress_calls == []


@pytest.mark.parametrize(
    ("wal", "outcome"),
    [
        (
            _wal_result(1, physical_frame_slots=500),
            PassiveCheckpointOutcome.NO_WORK,
        ),
        (
            _wal_result(
                1,
                physical_frame_slots=(
                    (WAL_HARD_LIMIT_BYTES - envelope.WAL_HEADER_BYTES)
                    // envelope.WAL_FRAME_BYTES
                    + 1
                ),
            ),
            PassiveCheckpointOutcome.OVERSIZE_BLOCKED,
        ),
    ],
)
def test_checkpoint_uses_logical_threshold_and_physical_byte_block(
    monkeypatch: pytest.MonkeyPatch,
    wal: WalInspectionResult,
    outcome: PassiveCheckpointOutcome,
) -> None:
    connection, fake = _fake_connection([])
    monkeypatch.setattr(
        envelope,
        "_inspect_wal",
        lambda connection, path, **kwargs: wal,
    )
    result = envelope._passive_wal_checkpoint(
        connection, Path("unused.db"), monotonic=lambda: 0.0
    )
    assert result.outcome is outcome
    assert result.wal_logical_frames_before == 1
    assert result.wal_physical_bytes_before == wal.total_bytes
    assert fake.execute_calls == []


@pytest.mark.parametrize("frames", [256, 500, 960])
def test_checkpoint_executes_exactly_one_fixed_passive_pragma(
    monkeypatch: pytest.MonkeyPatch, frames: int
) -> None:
    connection, fake = _fake_connection([(0, frames, frames - 1)])
    monkeypatch.setattr(
        envelope,
        "_inspect_wal",
        lambda connection, path, **kwargs: _wal_result(frames),
    )
    result = envelope._passive_wal_checkpoint(
        connection, Path("unused.db"), monotonic=lambda: 0.0
    )
    assert result == PassiveCheckpointResult(
        PassiveCheckpointOutcome.COMPLETED,
        frames,
        _wal_size(frames),
        False,
        frames,
        frames - 1,
    )
    assert fake.execute_calls == ["PRAGMA wal_checkpoint(PASSIVE)"]
    assert fake.cursor.fetch_sizes == [2]
    assert fake.progress_calls[0][0] is not None
    assert fake.progress_calls[-1] == (None, 0)


def test_checkpoint_busy_result_is_sanitized_and_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection, fake = _fake_connection([(1, 300, 100)])
    monkeypatch.setattr(
        envelope,
        "_inspect_wal",
        lambda connection, path, **kwargs: _wal_result(300),
    )
    result = envelope._passive_wal_checkpoint(
        connection, Path("unused.db"), monotonic=lambda: 0.0
    )
    assert result == PassiveCheckpointResult(
        PassiveCheckpointOutcome.BUSY,
        300,
        _wal_size(300),
        True,
        300,
        100,
    )
    assert len(fake.execute_calls) == 1


def test_checkpoint_unavailable_count_busy_result_is_normalized_without_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection, fake = _fake_connection([(1, -1, -1)])
    monkeypatch.setattr(
        envelope,
        "_inspect_wal",
        lambda connection, path, **kwargs: _wal_result(300),
    )
    result = envelope._passive_wal_checkpoint(
        connection, Path("unused.db"), monotonic=lambda: 0.0
    )
    assert result == PassiveCheckpointResult(
        PassiveCheckpointOutcome.BUSY,
        300,
        _wal_size(300),
        True,
        0,
        0,
    )
    assert fake.execute_calls == ["PRAGMA wal_checkpoint(PASSIVE)"]
    assert fake.cursor.fetch_sizes == [2]
    assert fake.progress_calls[-1] == (None, 0)


def test_incomplete_passive_progress_is_a_valid_completed_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection, fake = _fake_connection([(0, 320, 100)])
    monkeypatch.setattr(
        envelope,
        "_inspect_wal",
        lambda connection, path, **kwargs: _wal_result(300),
    )
    result = envelope._passive_wal_checkpoint(
        connection, Path("unused.db"), monotonic=lambda: 0.0
    )
    assert result == PassiveCheckpointResult(
        PassiveCheckpointOutcome.COMPLETED,
        300,
        _wal_size(300),
        False,
        320,
        100,
    )
    assert fake.execute_calls == ["PRAGMA wal_checkpoint(PASSIVE)"]


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [(0, 1, 1), (0, 1, 1)],
        [(0, 1)],
        [(False, 1, 1)],
        [(0, True, 1)],
        [(0, 1, False)],
        [(0, -1, 0)],
        [(0, 1, -1)],
        [(1, -1, 0)],
        [(1, 0, -1)],
        [(1, 1, 2)],
        [(1, envelope.MAX_WAL_INSPECTION_FRAMES + 1, 0)],
        [(2, 1, 1)],
        [(0, 1, 2)],
        [(0, envelope.MAX_WAL_INSPECTION_FRAMES + 1, 0)],
    ],
)
def test_checkpoint_malformed_results_are_trust_loss(
    monkeypatch: pytest.MonkeyPatch, rows: list[tuple[object, ...]]
) -> None:
    connection, fake = _fake_connection(rows)
    monkeypatch.setattr(
        envelope,
        "_inspect_wal",
        lambda connection, path, **kwargs: _wal_result(300),
    )
    with pytest.raises(StorageEnvelopeError) as caught:
        envelope._passive_wal_checkpoint(
            connection, Path("unused.db"), monotonic=lambda: 0.0
        )
    assert caught.value.reason is StorageEnvelopeRejection.MALFORMED_STATE
    assert caught.value.trust_lost
    assert fake.progress_calls[-1] == (None, 0)


def test_checkpoint_timeout_before_pragma(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection, fake = _fake_connection([(0, 300, 300)])
    calls = 0

    def elapsed() -> float:
        nonlocal calls
        calls += 1
        return 0.0 if calls == 1 else 2.0

    monkeypatch.setattr(
        envelope,
        "_inspect_wal",
        lambda connection, path, **kwargs: _wal_result(300),
    )
    with pytest.raises(StorageEnvelopeError) as caught:
        envelope._passive_wal_checkpoint(
            connection, Path("unused.db"), monotonic=elapsed
        )
    assert caught.value.reason is StorageEnvelopeRejection.TIMED_OUT
    assert fake.execute_calls == []


def test_checkpoint_timeout_during_result_consumption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    connection, fake = _fake_connection([(0, 300, 300)], clock=clock)
    monkeypatch.setattr(
        envelope,
        "_inspect_wal",
        lambda connection, path, **kwargs: _wal_result(300),
    )
    with pytest.raises(StorageEnvelopeError) as caught:
        envelope._passive_wal_checkpoint(
            connection, Path("unused.db"), monotonic=lambda: clock[0]
        )
    assert caught.value.reason is StorageEnvelopeRejection.TIMED_OUT
    assert fake.execute_calls == ["PRAGMA wal_checkpoint(PASSIVE)"]
    assert fake.progress_calls[-1] == (None, 0)


def test_checkpoint_timeout_after_completed_pragma_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    connection, fake = _fake_connection([(0, 300, 300)])
    monkeypatch.setattr(
        envelope,
        "_inspect_wal",
        lambda connection, path, **kwargs: _wal_result(300),
    )

    def cross_deadline(stage: envelope.StorageEnvelopeStage) -> None:
        if stage is envelope.StorageEnvelopeStage.CHECKPOINT_AFTER:
            clock[0] = 2.0

    monkeypatch.setattr(envelope, "_fault", cross_deadline)
    with pytest.raises(StorageEnvelopeError) as caught:
        envelope._passive_wal_checkpoint(
            connection, Path("unused.db"), monotonic=lambda: clock[0]
        )
    assert caught.value.reason is StorageEnvelopeRejection.TIMED_OUT
    assert fake.execute_calls == ["PRAGMA wal_checkpoint(PASSIVE)"]
    assert fake.cursor.fetch_sizes == [2]
    assert fake.progress_calls[-1] == (None, 0)


@pytest.mark.parametrize(
    ("code", "reason", "trust_lost"),
    [
        (sqlite3.SQLITE_BUSY, StorageEnvelopeRejection.STORAGE_BUSY, False),
        (sqlite3.SQLITE_LOCKED, StorageEnvelopeRejection.STORAGE_BUSY, False),
        (sqlite3.SQLITE_CORRUPT, StorageEnvelopeRejection.TRUST_FAILED, True),
        (sqlite3.SQLITE_SCHEMA, StorageEnvelopeRejection.TRUST_FAILED, True),
    ],
)
def test_checkpoint_sqlite_error_classification(
    monkeypatch: pytest.MonkeyPatch,
    code: int,
    reason: StorageEnvelopeRejection,
    trust_lost: bool,
) -> None:
    error = sqlite3.OperationalError("private sqlite detail")
    error.sqlite_errorcode = code  # type: ignore[attr-defined]
    connection, fake = _fake_connection([], error=error)
    monkeypatch.setattr(
        envelope,
        "_inspect_wal",
        lambda connection, path, **kwargs: _wal_result(300),
    )
    with pytest.raises(StorageEnvelopeError) as caught:
        envelope._passive_wal_checkpoint(
            connection, Path("unused.db"), monotonic=lambda: 0.0
        )
    assert caught.value.reason is reason
    assert caught.value.trust_lost is trust_lost
    assert caught.value.__cause__ is None
    assert len(fake.execute_calls) == 1
    assert fake.progress_calls[-1] == (None, 0)


def test_progress_handler_clear_failure_is_trust_loss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection, _fake = _fake_connection([(0, 300, 300)])
    monkeypatch.setattr(
        envelope,
        "_inspect_wal",
        lambda connection, path, **kwargs: _wal_result(300),
    )

    def fail_clear(candidate: sqlite3.Connection) -> None:
        del candidate
        raise StorageEnvelopeError(
            StorageEnvelopeRejection.TRUST_FAILED, trust_lost=True
        )

    monkeypatch.setattr(envelope, "_clear_progress_handler", fail_clear)
    with pytest.raises(StorageEnvelopeError) as caught:
        envelope._passive_wal_checkpoint(
            connection, Path("unused.db"), monotonic=lambda: 0.0
        )
    assert caught.value.reason is StorageEnvelopeRejection.TRUST_FAILED


def _seed_real_wal(store: HealthHistoryStore, event_count: int = 30_000) -> int:
    connection = store._connection  # noqa: SLF001
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    cursor = connection.execute(
        "INSERT INTO alerts(scope, kind, lifecycle, severity, opened_at_utc_us, "
        "recovered_at_utc_us, archived_at_utc_us, episode_count, occurrence_count, "
        "cooldown_until_utc_us) VALUES "
        "('overall', 'degraded', 'archived', 'degraded', 1, 2, 3, 1, 1, 2)"
    )
    assert cursor.lastrowid is not None
    alert_id = cursor.lastrowid
    connection.execute("BEGIN")
    connection.executemany(
        "INSERT INTO alert_events(alert_id, event_type, event_at_utc_us, "
        "resulting_lifecycle) VALUES (?, 'occurrence_updated', ?, 'recovered')",
        ((alert_id, index + 1) for index in range(event_count)),
    )
    connection.commit()
    wal = store.inspect_wal()
    assert wal.checkpoint_due and not wal.oversize
    return wal.logical_frame_count


def test_real_passive_checkpoint_preserves_every_logical_table(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    frames = _seed_real_wal(store)
    before = _snapshot(path)
    traced: list[str] = []
    store._connection.set_trace_callback(traced.append)  # noqa: SLF001
    try:
        result = store.passive_wal_checkpoint()
    finally:
        store._connection.set_trace_callback(None)  # noqa: SLF001
    assert result.outcome in {
        PassiveCheckpointOutcome.COMPLETED,
        PassiveCheckpointOutcome.BUSY,
    }
    assert result.wal_logical_frames_before == frames
    assert 0 <= result.checkpointed_frames <= result.log_frames
    assert traced.count("PRAGMA wal_checkpoint(NOOP)") == 1
    assert traced.count("PRAGMA wal_checkpoint(PASSIVE)") == 1
    assert _snapshot(path) == before


def test_real_checkpoint_lock_contention_is_busy_without_trust_loss(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, store = store_path
    _seed_real_wal(store)
    wal_before = store.inspect_wal()
    before = _snapshot(path)
    uri = f"{path.absolute().as_uri()}?mode=rw"
    writer = sqlite3.connect(uri, uri=True, isolation_level=None)
    probe = sqlite3.connect(uri, uri=True, isolation_level=None)
    holder_started = threading.Event()
    holder_done = threading.Event()
    holder_result: list[tuple[object, ...]] = []

    def hold_checkpoint_lock() -> None:
        holder = sqlite3.connect(uri, uri=True, isolation_level=None)
        holder.execute("PRAGMA busy_timeout = 3000")
        holder_started.set()
        try:
            row = holder.execute("PRAGMA wal_checkpoint(FULL)").fetchone()
            holder_result.append(cast(tuple[object, ...], row))
        finally:
            holder.close()
            holder_done.set()

    writer.execute("PRAGMA busy_timeout = 0")
    probe.execute("PRAGMA busy_timeout = 0")
    writer.execute("BEGIN IMMEDIATE")
    thread = threading.Thread(target=hold_checkpoint_lock, daemon=True)
    thread.start()
    traced: list[str] = []
    clear_calls = 0
    original_clear = envelope._clear_progress_handler

    def tracked_clear(connection: sqlite3.Connection) -> None:
        nonlocal clear_calls
        original_clear(connection)
        clear_calls += 1

    try:
        assert holder_started.wait(timeout=1.0)
        raw_busy: tuple[int, int, int] | None = None
        lock_deadline = time.monotonic() + 2.0
        while time.monotonic() < lock_deadline:
            candidate = probe.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            assert candidate is not None
            if candidate[0] == 1:
                raw_busy = cast(tuple[int, int, int], candidate)
                break
            time.sleep(0.005)
        assert raw_busy is not None
        assert raw_busy[0] == 1
        if raw_busy[1:] != (-1, -1):
            assert 0 <= raw_busy[2] <= raw_busy[1]

        monkeypatch.setattr(
            envelope,
            "_inspect_wal",
            lambda connection, candidate, **kwargs: wal_before,
        )
        monkeypatch.setattr(envelope, "_clear_progress_handler", tracked_clear)
        store._connection.set_trace_callback(traced.append)  # noqa: SLF001
        try:
            result = store.passive_wal_checkpoint()
        finally:
            store._connection.set_trace_callback(None)  # noqa: SLF001
    finally:
        writer.rollback()
        thread.join(timeout=4.0)
        probe.close()
        writer.close()

    assert holder_done.is_set()
    assert holder_result and holder_result[0][0] == 0
    assert result.outcome is PassiveCheckpointOutcome.BUSY
    assert result.busy
    assert 0 <= result.checkpointed_frames <= result.log_frames
    assert traced.count("PRAGMA wal_checkpoint(PASSIVE)") == 1
    assert clear_calls == 1
    assert not store.closed
    assert _snapshot(path) == before


def test_real_recycled_wal_uses_current_logical_generation(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    original_logical_frames = _seed_real_wal(store)
    wal_path = path.with_name(f"{path.name}-wal")
    physical_bytes_before = wal_path.stat().st_size
    physical_slots_before = envelope._wal_frame_count(physical_bytes_before)
    assert original_logical_frames >= WAL_CHECKPOINT_THRESHOLD_FRAMES
    assert physical_slots_before >= WAL_CHECKPOINT_THRESHOLD_FRAMES
    assert physical_bytes_before <= WAL_HARD_LIMIT_BYTES

    connection = store._connection  # noqa: SLF001
    restart = connection.execute("PRAGMA wal_checkpoint(RESTART)").fetchall()
    assert restart == [(0, original_logical_frames, original_logical_frames)]
    connection.execute(
        "UPDATE alerts SET occurrence_count = occurrence_count + 1 WHERE id = 1"
    )
    raw_status = connection.execute("PRAGMA wal_checkpoint(NOOP)").fetchone()
    assert raw_status is not None
    assert raw_status[0] == 0
    assert 0 < raw_status[1] < WAL_CHECKPOINT_THRESHOLD_FRAMES
    assert wal_path.stat().st_size == physical_bytes_before

    inspected = store.inspect_wal()
    assert inspected.total_bytes == physical_bytes_before
    assert inspected.physical_frame_slots == physical_slots_before
    assert inspected.logical_frame_count == raw_status[1]
    assert inspected.checkpointed_frames == raw_status[2]
    assert not inspected.checkpoint_due
    assert not inspected.oversize


@pytest.mark.parametrize("operation", ["capacity", "free", "wal", "checkpoint"])
def test_public_envelope_operations_preserve_logical_tables(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    path, store = store_path
    before = _snapshot(path)
    if operation == "capacity":
        store.inspect_storage_capacity()
    elif operation == "free":
        monkeypatch.setattr(
            envelope.shutil,
            "disk_usage",
            lambda parent: SimpleNamespace(total=1000, used=100, free=900),
        )
        store.inspect_free_space()
    elif operation == "wal":
        store.inspect_wal()
    else:
        monkeypatch.setattr(
            envelope,
            "_inspect_wal",
            lambda connection, path, **kwargs: _wal_result(0),
        )
        store.passive_wal_checkpoint()
    assert _snapshot(path) == before


@pytest.mark.parametrize("version", [(3, 51, 2), (3, 51, 1), (3, 51, 0)])
@pytest.mark.parametrize("operation", ["inspect", "checkpoint"])
def test_public_unsupported_runtime_is_non_trust_and_executes_no_checkpoint(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    version: tuple[int, int, int],
) -> None:
    path, store = store_path
    before = _snapshot(path)
    traced: list[str] = []
    monkeypatch.setattr(envelope.sqlite3, "sqlite_version_info", version)
    store._connection.set_trace_callback(traced.append)  # noqa: SLF001
    try:
        with pytest.raises(StorageEnvelopeError) as caught:
            if operation == "inspect":
                store.inspect_wal()
            else:
                store.passive_wal_checkpoint()
    finally:
        store._connection.set_trace_callback(None)  # noqa: SLF001
    assert caught.value.reason is StorageEnvelopeRejection.UNSUPPORTED_RUNTIME
    assert not caught.value.trust_lost
    assert not store.closed
    assert not any("wal_checkpoint" in statement.lower() for statement in traced)
    assert _snapshot(path) == before


@pytest.mark.parametrize("mode", ["busy", "timed_out", "malformed"])
def test_public_checkpoint_failures_preserve_logical_tables(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    path, store = store_path
    before = _snapshot(path)

    def checkpoint(
        connection: sqlite3.Connection,
        candidate: Path,
        *,
        monotonic: object,
    ) -> PassiveCheckpointResult:
        del connection, candidate, monotonic
        if mode == "busy":
            return PassiveCheckpointResult(
                PassiveCheckpointOutcome.BUSY,
                300,
                _wal_size(300),
                True,
                300,
                100,
            )
        if mode == "timed_out":
            raise StorageEnvelopeError(StorageEnvelopeRejection.TIMED_OUT)
        raise StorageEnvelopeError(
            StorageEnvelopeRejection.MALFORMED_STATE, trust_lost=True
        )

    monkeypatch.setattr(store_module, "_passive_wal_checkpoint", checkpoint)
    if mode == "busy":
        assert store.passive_wal_checkpoint().outcome is PassiveCheckpointOutcome.BUSY
    else:
        with pytest.raises(StorageEnvelopeError) as caught:
            store.passive_wal_checkpoint()
        assert caught.value.reason is (
            StorageEnvelopeRejection.TIMED_OUT
            if mode == "timed_out"
            else StorageEnvelopeRejection.MALFORMED_STATE
        )
    assert store.closed is (mode == "malformed")
    assert _snapshot(path) == before


@pytest.mark.parametrize("identity_target", ["main", "sidecar"])
def test_identity_replacement_before_envelope_operation_closes_store(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
    identity_target: str,
) -> None:
    path, store = store_path
    before = _snapshot(path)
    if identity_target == "main":
        monkeypatch.setattr(
            store_module,
            "validate_database_file",
            lambda candidate, **kwargs: (_ for _ in ()).throw(
                FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED)
            ),
        )
    else:
        monkeypatch.setattr(
            store_module,
            "_advance_storage_sidecar_snapshot",
            lambda candidate, prior: (_ for _ in ()).throw(
                FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED)
            ),
        )
    with pytest.raises(StorageEnvelopeError) as caught:
        store.inspect_storage_capacity()
    assert caught.value.reason is StorageEnvelopeRejection.TRUST_FAILED
    assert store.closed
    assert _snapshot(path) == before


@pytest.mark.parametrize("identity_target", ["main", "sidecar"])
def test_post_checkpoint_identity_loss_closes_without_rollback_claim(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
    identity_target: str,
) -> None:
    path, store = store_path
    _seed_real_wal(store)
    before = _snapshot(path)
    calls = 0
    if identity_target == "main":
        original = store_module.validate_database_file

        def changed(candidate: Path, **kwargs: object) -> Any:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED)
            return original(candidate, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(store_module, "validate_database_file", changed)
    else:
        original_sidecars = store_module._advance_storage_sidecar_snapshot

        def changed_sidecars(candidate: Path, prior: object) -> Any:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED)
            return original_sidecars(candidate, prior)  # type: ignore[arg-type]

        monkeypatch.setattr(
            store_module, "_advance_storage_sidecar_snapshot", changed_sidecars
        )
    with pytest.raises(StorageEnvelopeError) as caught:
        store.passive_wal_checkpoint()
    assert caught.value.reason is StorageEnvelopeRejection.TRUST_FAILED
    assert calls == 2
    assert store.closed
    assert _snapshot(path) == before


def _capacity(*, remaining: int, freelist_count: int = 0) -> StorageCapacityResult:
    page_count = MAX_DATABASE_PAGES - remaining
    return StorageCapacityResult(
        page_count,
        freelist_count,
        MAX_DATABASE_PAGES,
        page_count * PAGE_SIZE_BYTES,
        MAX_DATABASE_BYTES,
        remaining,
    )


@pytest.mark.parametrize(
    ("remaining", "sufficient", "frames", "maintenance_attempted", "outcome"),
    [
        (1, True, 0, False, StorageDecisionOutcome.PROCEED),
        (1, True, 256, False, StorageDecisionOutcome.WAL_CHECKPOINT_DUE),
        (
            0,
            True,
            0,
            False,
            StorageDecisionOutcome.CAPACITY_MAINTENANCE_REQUIRED,
        ),
        (0, True, 0, True, StorageDecisionOutcome.CAPACITY_BLOCKED),
        (
            1,
            False,
            0,
            False,
            StorageDecisionOutcome.CAPACITY_MAINTENANCE_REQUIRED,
        ),
        (1, False, 0, True, StorageDecisionOutcome.CAPACITY_BLOCKED),
        (0, False, 961, False, StorageDecisionOutcome.WAL_OVERSIZE_BLOCKED),
    ],
)
def test_storage_decision_fixed_priority(
    remaining: int,
    sufficient: bool,
    frames: int,
    maintenance_attempted: bool,
    outcome: StorageDecisionOutcome,
) -> None:
    free = FREE_SPACE_RESERVE_BYTES if sufficient else FREE_SPACE_RESERVE_BYTES - 1
    result = decide_storage_action(
        _capacity(remaining=remaining),
        FreeSpaceResult(sufficient, free, FREE_SPACE_RESERVE_BYTES),
        _wal_result(frames),
        capacity_maintenance_attempted=maintenance_attempted,
    )
    assert result == StorageDecisionResult(
        outcome, outcome is StorageDecisionOutcome.PROCEED
    )


def test_capacity_maintenance_decision_is_conservative_with_freelist_pages() -> None:
    capacity = _capacity(remaining=0, freelist_count=8)
    free_space = FreeSpaceResult(
        True, FREE_SPACE_RESERVE_BYTES, FREE_SPACE_RESERVE_BYTES
    )
    wal = _wal_result(0)
    assert decide_storage_action(capacity, free_space, wal).outcome is (
        StorageDecisionOutcome.CAPACITY_MAINTENANCE_REQUIRED
    )
    assert (
        decide_storage_action(
            capacity,
            free_space,
            wal,
            capacity_maintenance_attempted=True,
        ).outcome
        is StorageDecisionOutcome.CAPACITY_BLOCKED
    )


def test_storage_decision_performs_no_sql_or_filesystem_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def prohibited(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("pure decision performed external work")

    monkeypatch.setattr(envelope, "_pragma_integer", prohibited)
    monkeypatch.setattr(envelope.shutil, "disk_usage", prohibited)
    result = decide_storage_action(
        _capacity(remaining=1),
        FreeSpaceResult(True, FREE_SPACE_RESERVE_BYTES, FREE_SPACE_RESERVE_BYTES),
        _wal_result(0),
    )
    assert result.outcome is StorageDecisionOutcome.PROCEED


def test_retention_then_one_incremental_vacuum_is_the_bounded_capacity_sequence(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    _path, store = store_path
    connection = store._connection  # noqa: SLF001
    cursor = connection.execute(
        "INSERT INTO alerts(scope, kind, lifecycle, severity, opened_at_utc_us, "
        "recovered_at_utc_us, archived_at_utc_us, episode_count, occurrence_count, "
        "cooldown_until_utc_us) VALUES "
        "('overall', 'degraded', 'archived', 'degraded', 1, 2, 3, 1, 1, 2)"
    )
    assert cursor.lastrowid is not None
    connection.execute("BEGIN")
    connection.executemany(
        "INSERT INTO alert_events(alert_id, event_type, event_at_utc_us, "
        "resulting_lifecycle) VALUES (?, 'occurrence_updated', ?, 'recovered')",
        ((cursor.lastrowid, index + 1) for index in range(800)),
    )
    connection.commit()
    capacity_before = store.inspect_storage_capacity()

    cleanup = store.cleanup_retention(
        now_utc_us=31 * 24 * 60 * 60 * 1_000_000,
    )
    capacity_after_cleanup = store.inspect_storage_capacity()
    assert cleanup.logical_rows_deleted == RETENTION_ROW_BUDGET
    assert capacity_after_cleanup.page_count == capacity_before.page_count
    assert capacity_after_cleanup.used_bytes == capacity_before.used_bytes
    assert capacity_after_cleanup.freelist_count > capacity_before.freelist_count
    shortage = FreeSpaceResult(
        False,
        FREE_SPACE_RESERVE_BYTES - 1,
        FREE_SPACE_RESERVE_BYTES,
    )
    assert (
        decide_storage_action(capacity_after_cleanup, shortage, _wal_result(0)).outcome
        is StorageDecisionOutcome.CAPACITY_MAINTENANCE_REQUIRED
    )

    vacuum = store.incremental_vacuum()
    capacity_after_vacuum = store.inspect_storage_capacity()
    assert vacuum.pages_requested == INCREMENTAL_VACUUM_PAGES
    assert capacity_after_vacuum.page_count <= capacity_after_cleanup.page_count
    assert capacity_after_vacuum.freelist_count <= (
        capacity_after_cleanup.freelist_count
    )
    assert (
        decide_storage_action(
            capacity_after_vacuum,
            FreeSpaceResult(
                True,
                FREE_SPACE_RESERVE_BYTES,
                FREE_SPACE_RESERVE_BYTES,
            ),
            _wal_result(0),
            capacity_maintenance_attempted=True,
        ).outcome
        is StorageDecisionOutcome.PROCEED
    )
    assert (
        decide_storage_action(
            _capacity(remaining=0, freelist_count=1),
            shortage,
            _wal_result(0),
            capacity_maintenance_attempted=True,
        ).outcome
        is StorageDecisionOutcome.CAPACITY_BLOCKED
    )


def test_storage_result_models_are_immutable_and_sanitized() -> None:
    capacity = _capacity(remaining=1)
    with pytest.raises(FrozenInstanceError):
        capacity.page_count = 1  # type: ignore[misc]
    with pytest.raises(ValueError):
        StorageCapacityResult(1, 0, 2, PAGE_SIZE_BYTES, MAX_DATABASE_BYTES, 1)
    with pytest.raises(ValueError):
        FreeSpaceResult(True, FREE_SPACE_RESERVE_BYTES - 1, FREE_SPACE_RESERVE_BYTES)
    with pytest.raises(ValueError):
        WalInspectionResult(True, 2, 1, _wal_size(1), 0, False, False)
    with pytest.raises(ValueError):
        PassiveCheckpointResult(
            PassiveCheckpointOutcome.COMPLETED,
            256,
            _wal_size(256),
            False,
            1,
            2,
        )
    assert "path" not in repr(capacity).lower()
    assert "device" not in repr(capacity).lower()


def test_public_surface_and_regression_boundaries_are_fixed() -> None:
    assert (PAGE_SIZE_BYTES, MAX_DATABASE_PAGES, MAX_DATABASE_BYTES) == (
        4096,
        16_384,
        64 * 1024 * 1024,
    )
    assert FREE_SPACE_RESERVE_BYTES == 128 * 1024 * 1024
    assert (
        WAL_CHECKPOINT_THRESHOLD_FRAMES,
        WAL_HARD_LIMIT_FRAMES,
        WAL_HARD_LIMIT_BYTES,
    ) == (256, 960, 4 * 1024 * 1024)
    assert RETENTION_ROW_BUDGET == 500
    assert INCREMENTAL_VACUUM_PAGES == 128
    assert tuple(
        inspect.signature(HealthHistoryStore.inspect_storage_capacity).parameters
    ) == ("self",)
    assert tuple(
        inspect.signature(HealthHistoryStore.inspect_free_space).parameters
    ) == ("self",)
    assert tuple(inspect.signature(HealthHistoryStore.inspect_wal).parameters) == (
        "self",
    )
    assert tuple(
        inspect.signature(HealthHistoryStore.passive_wal_checkpoint).parameters
    ) == ("self",)
    source = inspect.getsource(envelope).lower()
    assert source.count("pragma wal_checkpoint(noop)") == 1
    assert source.count("pragma wal_checkpoint(passive)") == 1
    assert "wal_checkpoint(full)" not in source
    assert "wal_checkpoint(restart)" not in source
    assert "wal_checkpoint(truncate)" not in source
    assert "while " not in source
    assert "subprocess" not in source
    assert "socket" not in source
    ingestion_source = (
        Path(__file__).parents[1]
        / "src"
        / "aurora_core"
        / "health_history"
        / "ingestion.py"
    ).read_text(encoding="utf-8")
    assert "cleanup_retention" not in ingestion_source
    assert "incremental_vacuum" not in ingestion_source
    assert "wal_checkpoint" not in ingestion_source
    root = Path(__file__).parents[1] / "src" / "aurora_core"
    runtime_files = (
        root / "__main__.py",
        root / "dashboard" / "server.py",
        *sorted((root / "runtime").glob("*.py")),
    )
    for path in runtime_files:
        runtime_source = path.read_text(encoding="utf-8")
        assert "health_history.storage_envelope" not in runtime_source
        assert "m18_validation" not in runtime_source
    assert APPLICATION_ID == 0x41555248
    assert SCHEMA_VERSION == 1
