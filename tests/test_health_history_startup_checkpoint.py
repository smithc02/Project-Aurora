"""Direct-only tests for bounded startup WAL-checkpoint remediation."""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest

import aurora_core.health_history.startup_checkpoint as checkpoint_module
from aurora_core.config import HealthHistoryDatabaseMode, HealthHistorySettings
from aurora_core.dashboard.models import HealthReport
from aurora_core.health_history import (
    DatabaseLifecycleError,
    DatabaseLifecycleRejection,
    HealthHistoryDatabaseLifecycle,
    HealthHistoryLeadership,
    LeadershipError,
    LeadershipRejection,
    PassiveCheckpointOutcome,
    PassiveCheckpointResult,
    StorageDecisionOutcome,
    StorageDecisionResult,
    StorageEnvelopeError,
    StorageEnvelopeRejection,
    bootstrap_health_history_database,
    checkpoint_health_history_startup_wal,
)
from aurora_core.health_history.models import (
    APPLICATION_ID,
    BUSY_TIMEOUT_MILLISECONDS,
    MAX_DATABASE_BYTES,
    MAX_DATABASE_PAGES,
    PAGE_SIZE_BYTES,
    SCHEMA_VERSION,
)
from aurora_core.health_history.schema import (
    FOREIGN_KEY_CHECK_SECONDS,
    QUICK_CHECK_SECONDS,
)
from aurora_core.health_history.sqlite_runtime import MINIMUM_SAFE_SQLITE_VERSION
from aurora_core.health_history.storage_envelope import (
    WAL_CHECKPOINT_THRESHOLD_FRAMES,
    WAL_HARD_LIMIT_BYTES,
    WAL_HARD_LIMIT_FRAMES,
)
from aurora_core.health_history.store import HealthHistoryStore, StoreError

_PRIVATE_CANARY = "/private/aurora/history.sqlite3 PRAGMA private-canary"
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


def _decision(outcome: StorageDecisionOutcome) -> StorageDecisionResult:
    return StorageDecisionResult(
        outcome=outcome,
        write_permitted=outcome is StorageDecisionOutcome.PROCEED,
    )


def _checkpoint_result(
    outcome: PassiveCheckpointOutcome,
) -> PassiveCheckpointResult:
    if outcome is PassiveCheckpointOutcome.NO_WORK:
        return PassiveCheckpointResult(
            outcome=outcome,
            wal_logical_frames_before=0,
            wal_physical_bytes_before=0,
            busy=False,
            log_frames=0,
            checkpointed_frames=0,
        )
    if outcome is PassiveCheckpointOutcome.OVERSIZE_BLOCKED:
        return PassiveCheckpointResult(
            outcome=outcome,
            wal_logical_frames_before=WAL_HARD_LIMIT_FRAMES + 1,
            wal_physical_bytes_before=WAL_HARD_LIMIT_BYTES,
            busy=False,
            log_frames=0,
            checkpointed_frames=0,
        )
    return PassiveCheckpointResult(
        outcome=outcome,
        wal_logical_frames_before=WAL_CHECKPOINT_THRESHOLD_FRAMES,
        wal_physical_bytes_before=WAL_HARD_LIMIT_BYTES,
        busy=outcome is PassiveCheckpointOutcome.BUSY,
        log_frames=WAL_CHECKPOINT_THRESHOLD_FRAMES,
        checkpointed_frames=(
            0
            if outcome is PassiveCheckpointOutcome.BUSY
            else WAL_CHECKPOINT_THRESHOLD_FRAMES
        ),
    )


class _FakeStore:
    def __init__(
        self,
        *,
        checkpoint_result: PassiveCheckpointResult | None = None,
        checkpoint_error: BaseException | None = None,
        close_failures: int = 0,
    ) -> None:
        self.checkpoint_result = checkpoint_result or _checkpoint_result(
            PassiveCheckpointOutcome.COMPLETED
        )
        self.checkpoint_error = checkpoint_error
        self.close_failures = close_failures
        self.closed = False
        self.checkpoint_calls = 0
        self.close_calls = 0
        self.prohibited_calls = 0

    def passive_wal_checkpoint(self) -> PassiveCheckpointResult:
        self.checkpoint_calls += 1
        if self.checkpoint_error is not None:
            raise self.checkpoint_error
        return self.checkpoint_result

    def cleanup_retention(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self.prohibited_calls += 1
        raise AssertionError("startup checkpoint must not clean retention")

    def incremental_vacuum(self) -> None:
        self.prohibited_calls += 1
        raise AssertionError("startup checkpoint must not vacuum")

    def ingest(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        self.prohibited_calls += 1
        raise AssertionError("startup checkpoint must not ingest")

    def verify(self) -> None:
        self.prohibited_calls += 1
        raise AssertionError("startup checkpoint must not verify")

    def close(self) -> None:
        self.close_calls += 1
        if self.close_failures:
            self.close_failures -= 1
            raise sqlite3.OperationalError(_PRIVATE_CANARY)
        self.closed = True


class _FakeLeadership:
    def __init__(
        self,
        *,
        close_failures: int = 0,
        closes_on_failure: bool = False,
    ) -> None:
        self.close_failures = close_failures
        self.closes_on_failure = closes_on_failure
        self.closed = False
        self.close_calls = 0

    @property
    def held(self) -> bool:
        return not self.closed

    def close(self) -> None:
        self.close_calls += 1
        if self.close_failures:
            self.close_failures -= 1
            if self.closes_on_failure:
                self.closed = True
            raise LeadershipError(LeadershipRejection.RELEASE_FAILED)
        self.closed = True


class _BorrowingLifecycle:
    def __init__(self, store: _FakeStore) -> None:
        self._store = store
        self.store_borrows = 0
        self.close_calls = 0
        self.leadership_release_calls = 0
        self.closed = False

    @property
    def store(self) -> _FakeStore:
        self.store_borrows += 1
        return self._store

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True


def _owned_lifecycle(
    store: _FakeStore,
    leadership: _FakeLeadership,
) -> HealthHistoryDatabaseLifecycle:
    return HealthHistoryDatabaseLifecycle(
        store=cast(HealthHistoryStore, store),
        leadership=cast(HealthHistoryLeadership, leadership),
    )


def _patch_preflights(
    monkeypatch: pytest.MonkeyPatch,
    *values: StorageDecisionResult | BaseException,
) -> list[HealthHistoryDatabaseLifecycle]:
    calls: list[HealthHistoryDatabaseLifecycle] = []
    pending = list(values)

    def preflight(
        lifecycle: HealthHistoryDatabaseLifecycle,
    ) -> StorageDecisionResult:
        calls.append(lifecycle)
        value = pending.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    monkeypatch.setattr(
        checkpoint_module,
        "preflight_health_history_storage",
        preflight,
    )
    return calls


@pytest.mark.parametrize(
    "outcome",
    [
        StorageDecisionOutcome.PROCEED,
        StorageDecisionOutcome.CAPACITY_MAINTENANCE_REQUIRED,
        StorageDecisionOutcome.WAL_OVERSIZE_BLOCKED,
    ],
)
def test_non_checkpoint_decision_is_returned_exactly_without_store_borrow(
    monkeypatch: pytest.MonkeyPatch,
    outcome: StorageDecisionOutcome,
) -> None:
    original = _decision(outcome)
    store = _FakeStore()
    lifecycle = _BorrowingLifecycle(store)
    calls = _patch_preflights(monkeypatch, original)

    result = checkpoint_health_history_startup_wal(
        cast(HealthHistoryDatabaseLifecycle, lifecycle)
    )

    assert result is original
    assert calls == [lifecycle]
    assert lifecycle.store_borrows == 0
    assert store.checkpoint_calls == store.prohibited_calls == 0
    assert lifecycle.close_calls == lifecycle.leadership_release_calls == 0
    assert not lifecycle.closed


@pytest.mark.parametrize(
    "checkpoint_outcome",
    [
        PassiveCheckpointOutcome.COMPLETED,
        PassiveCheckpointOutcome.NO_WORK,
        PassiveCheckpointOutcome.OVERSIZE_BLOCKED,
    ],
)
def test_non_busy_checkpoint_runs_once_then_returns_one_final_preflight(
    monkeypatch: pytest.MonkeyPatch,
    checkpoint_outcome: PassiveCheckpointOutcome,
) -> None:
    initial = _decision(StorageDecisionOutcome.WAL_CHECKPOINT_DUE)
    final = _decision(StorageDecisionOutcome.PROCEED)
    store = _FakeStore(checkpoint_result=_checkpoint_result(checkpoint_outcome))
    lifecycle = _BorrowingLifecycle(store)
    calls = _patch_preflights(monkeypatch, initial, final)

    result = checkpoint_health_history_startup_wal(
        cast(HealthHistoryDatabaseLifecycle, lifecycle)
    )

    assert result is final
    assert calls == [lifecycle, lifecycle]
    assert lifecycle.store_borrows == 1
    assert store.checkpoint_calls == 1
    assert store.prohibited_calls == 0
    assert lifecycle.close_calls == lifecycle.leadership_release_calls == 0


@pytest.mark.parametrize(
    "final_outcome",
    [
        StorageDecisionOutcome.WAL_CHECKPOINT_DUE,
        StorageDecisionOutcome.CAPACITY_MAINTENANCE_REQUIRED,
    ],
)
def test_completed_checkpoint_returns_current_non_ready_state_without_remediation(
    monkeypatch: pytest.MonkeyPatch,
    final_outcome: StorageDecisionOutcome,
) -> None:
    initial = _decision(StorageDecisionOutcome.WAL_CHECKPOINT_DUE)
    final = _decision(final_outcome)
    store = _FakeStore()
    lifecycle = _BorrowingLifecycle(store)
    calls = _patch_preflights(monkeypatch, initial, final)

    result = checkpoint_health_history_startup_wal(
        cast(HealthHistoryDatabaseLifecycle, lifecycle)
    )

    assert result is final
    assert not result.write_permitted
    assert calls == [lifecycle, lifecycle]
    assert store.checkpoint_calls == 1
    assert store.prohibited_calls == 0


def test_busy_returns_original_due_decision_without_retry_or_final_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = _decision(StorageDecisionOutcome.WAL_CHECKPOINT_DUE)
    store = _FakeStore(
        checkpoint_result=_checkpoint_result(PassiveCheckpointOutcome.BUSY)
    )
    lifecycle = _BorrowingLifecycle(store)
    calls = _patch_preflights(monkeypatch, initial)

    result = checkpoint_health_history_startup_wal(
        cast(HealthHistoryDatabaseLifecycle, lifecycle)
    )

    assert result is initial
    assert calls == [lifecycle]
    assert lifecycle.store_borrows == 1
    assert store.checkpoint_calls == 1
    assert lifecycle.close_calls == lifecycle.leadership_release_calls == 0


def test_initial_preflight_error_stops_before_store_borrow_and_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = StorageEnvelopeError(StorageEnvelopeRejection.PERSISTENCE_FAILED)
    store = _FakeStore()
    lifecycle = _BorrowingLifecycle(store)
    calls = _patch_preflights(monkeypatch, error)

    with pytest.raises(StorageEnvelopeError) as captured:
        checkpoint_health_history_startup_wal(
            cast(HealthHistoryDatabaseLifecycle, lifecycle)
        )

    assert captured.value is error
    assert calls == [lifecycle]
    assert lifecycle.store_borrows == 0
    assert store.checkpoint_calls == 0
    assert lifecycle.close_calls == lifecycle.leadership_release_calls == 0


def test_checkpoint_storage_error_propagates_without_final_preflight_or_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = _decision(StorageDecisionOutcome.WAL_CHECKPOINT_DUE)
    error = StorageEnvelopeError(StorageEnvelopeRejection.TIMED_OUT)
    store = _FakeStore(checkpoint_error=error)
    lifecycle = _BorrowingLifecycle(store)
    calls = _patch_preflights(monkeypatch, initial)

    with pytest.raises(StorageEnvelopeError) as captured:
        checkpoint_health_history_startup_wal(
            cast(HealthHistoryDatabaseLifecycle, lifecycle)
        )

    assert captured.value is error
    assert calls == [lifecycle]
    assert store.checkpoint_calls == 1
    assert lifecycle.close_calls == lifecycle.leadership_release_calls == 0


def test_final_preflight_error_propagates_without_checkpoint_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = _decision(StorageDecisionOutcome.WAL_CHECKPOINT_DUE)
    error = StorageEnvelopeError(StorageEnvelopeRejection.TRUST_FAILED, trust_lost=True)
    store = _FakeStore()
    lifecycle = _BorrowingLifecycle(store)
    calls = _patch_preflights(monkeypatch, initial, error)

    with pytest.raises(StorageEnvelopeError) as captured:
        checkpoint_health_history_startup_wal(
            cast(HealthHistoryDatabaseLifecycle, lifecycle)
        )

    assert captured.value is error
    assert calls == [lifecycle, lifecycle]
    assert store.checkpoint_calls == 1
    assert lifecycle.close_calls == lifecycle.leadership_release_calls == 0


def test_closed_owned_store_after_due_preflight_fails_closed_without_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FakeStore()
    store.closed = True
    lifecycle = _BorrowingLifecycle(store)
    calls = _patch_preflights(
        monkeypatch, _decision(StorageDecisionOutcome.WAL_CHECKPOINT_DUE)
    )

    with pytest.raises(DatabaseLifecycleError) as captured:
        checkpoint_health_history_startup_wal(
            cast(HealthHistoryDatabaseLifecycle, lifecycle)
        )

    assert captured.value.reason is DatabaseLifecycleRejection.TRUST_FAILED
    assert str(captured.value) == "trust_failed"
    assert _PRIVATE_CANARY not in str(captured.value)
    assert captured.value.__cause__ is None
    assert calls == [lifecycle]
    assert lifecycle.store_borrows == 1
    assert store.checkpoint_calls == 0
    assert lifecycle.close_calls == lifecycle.leadership_release_calls == 0
    assert not lifecycle.closed


def test_store_closed_race_is_sanitized_but_unrelated_store_error_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = _decision(StorageDecisionOutcome.WAL_CHECKPOINT_DUE)
    closed_store = _FakeStore(checkpoint_error=StoreError("store_closed"))
    closed_lifecycle = _BorrowingLifecycle(closed_store)
    _patch_preflights(monkeypatch, initial)

    with pytest.raises(DatabaseLifecycleError) as closed:
        checkpoint_health_history_startup_wal(
            cast(HealthHistoryDatabaseLifecycle, closed_lifecycle)
        )
    assert closed.value.reason is DatabaseLifecycleRejection.TRUST_FAILED
    assert str(closed.value) == "trust_failed"
    assert closed.value.__cause__ is None

    unrelated = StoreError("unrelated_store_error")
    unrelated_store = _FakeStore(checkpoint_error=unrelated)
    unrelated_lifecycle = _BorrowingLifecycle(unrelated_store)
    _patch_preflights(monkeypatch, initial)
    with pytest.raises(StoreError) as captured:
        checkpoint_health_history_startup_wal(
            cast(HealthHistoryDatabaseLifecycle, unrelated_lifecycle)
        )
    assert captured.value is unrelated


def test_unrelated_checkpoint_exception_is_not_broadly_translated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = _decision(StorageDecisionOutcome.WAL_CHECKPOINT_DUE)
    error = RuntimeError(_PRIVATE_CANARY)
    store = _FakeStore(checkpoint_error=error)
    lifecycle = _BorrowingLifecycle(store)
    calls = _patch_preflights(monkeypatch, initial)

    with pytest.raises(RuntimeError) as captured:
        checkpoint_health_history_startup_wal(
            cast(HealthHistoryDatabaseLifecycle, lifecycle)
        )

    assert captured.value is error
    assert calls == [lifecycle]
    assert store.checkpoint_calls == 1


@pytest.mark.parametrize(
    "reason",
    [
        DatabaseLifecycleRejection.CLOSED,
        DatabaseLifecycleRejection.CLEANUP_FAILED,
    ],
)
def test_lifecycle_state_rejection_propagates_before_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
    reason: DatabaseLifecycleRejection,
) -> None:
    error = DatabaseLifecycleError(reason)
    store = _FakeStore()
    lifecycle = _BorrowingLifecycle(store)
    calls = _patch_preflights(monkeypatch, error)

    with pytest.raises(DatabaseLifecycleError) as captured:
        checkpoint_health_history_startup_wal(
            cast(HealthHistoryDatabaseLifecycle, lifecycle)
        )

    assert captured.value is error
    assert calls == [lifecycle]
    assert lifecycle.store_borrows == 0
    assert store.checkpoint_calls == 0


@pytest.mark.parametrize(
    "reason",
    [
        StorageEnvelopeRejection.UNSUPPORTED_RUNTIME,
        StorageEnvelopeRejection.MALFORMED_STATE,
    ],
)
def test_runtime_rejection_has_no_fallback_checkpoint_or_retry(
    monkeypatch: pytest.MonkeyPatch,
    reason: StorageEnvelopeRejection,
) -> None:
    error = StorageEnvelopeError(reason)
    store = _FakeStore()
    lifecycle = _BorrowingLifecycle(store)
    calls = _patch_preflights(monkeypatch, error)

    with pytest.raises(StorageEnvelopeError) as captured:
        checkpoint_health_history_startup_wal(
            cast(HealthHistoryDatabaseLifecycle, lifecycle)
        )

    assert captured.value is error
    assert calls == [lifecycle]
    assert lifecycle.store_borrows == 0
    assert store.checkpoint_calls == 0


@pytest.mark.parametrize(
    ("initial", "checkpoint_result", "error"),
    [
        (_decision(StorageDecisionOutcome.PROCEED), None, None),
        (
            _decision(StorageDecisionOutcome.CAPACITY_MAINTENANCE_REQUIRED),
            None,
            None,
        ),
        (
            _decision(StorageDecisionOutcome.WAL_CHECKPOINT_DUE),
            _checkpoint_result(PassiveCheckpointOutcome.BUSY),
            None,
        ),
        (
            _decision(StorageDecisionOutcome.WAL_CHECKPOINT_DUE),
            None,
            StorageEnvelopeError(StorageEnvelopeRejection.PERSISTENCE_FAILED),
        ),
    ],
    ids=("ready", "non-ready", "busy", "exception"),
)
def test_lifecycle_remains_caller_owned_after_every_initial_outcome(
    monkeypatch: pytest.MonkeyPatch,
    initial: StorageDecisionResult,
    checkpoint_result: PassiveCheckpointResult | None,
    error: BaseException | None,
) -> None:
    store = _FakeStore(checkpoint_result=checkpoint_result, checkpoint_error=error)
    leadership = _FakeLeadership()
    lifecycle = _owned_lifecycle(store, leadership)
    _patch_preflights(monkeypatch, initial)

    if error is None:
        checkpoint_health_history_startup_wal(lifecycle)
    else:
        with pytest.raises(StorageEnvelopeError):
            checkpoint_health_history_startup_wal(lifecycle)

    assert not lifecycle.closed
    assert lifecycle.store is store
    assert store.close_calls == 0
    assert leadership.held
    assert leadership.close_calls == 0
    lifecycle.close()


def test_store_close_failure_after_checkpoint_remains_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FakeStore(close_failures=1)
    leadership = _FakeLeadership()
    lifecycle = _owned_lifecycle(store, leadership)
    _patch_preflights(monkeypatch, _decision(StorageDecisionOutcome.PROCEED))
    checkpoint_health_history_startup_wal(lifecycle)

    with pytest.raises(DatabaseLifecycleError) as first:
        lifecycle.close()
    assert first.value.reason is DatabaseLifecycleRejection.CLEANUP_FAILED
    assert not lifecycle.closed
    assert leadership.held
    assert leadership.close_calls == 0
    with pytest.raises(DatabaseLifecycleError) as unavailable:
        _ = lifecycle.store
    assert unavailable.value.reason is DatabaseLifecycleRejection.CLEANUP_FAILED

    lifecycle.close()
    assert lifecycle.closed
    assert store.close_calls == 2
    assert leadership.close_calls == 1


@pytest.mark.parametrize("reports_closed", [True, False])
def test_leadership_release_failure_after_checkpoint_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
    reports_closed: bool,
) -> None:
    store = _FakeStore()
    leadership = _FakeLeadership(
        close_failures=1,
        closes_on_failure=reports_closed,
    )
    lifecycle = _owned_lifecycle(store, leadership)
    _patch_preflights(monkeypatch, _decision(StorageDecisionOutcome.PROCEED))
    checkpoint_health_history_startup_wal(lifecycle)

    with pytest.raises(DatabaseLifecycleError) as first:
        lifecycle.close()
    assert first.value.reason is DatabaseLifecycleRejection.CLEANUP_FAILED
    assert not lifecycle.closed
    assert store.closed
    with pytest.raises(DatabaseLifecycleError) as unavailable:
        _ = lifecycle.store
    assert unavailable.value.reason is DatabaseLifecycleRejection.CLEANUP_FAILED
    with pytest.raises(DatabaseLifecycleError) as enter_failed:
        lifecycle.__enter__()
    assert enter_failed.value.reason is DatabaseLifecycleRejection.CLEANUP_FAILED

    with pytest.raises(DatabaseLifecycleError) as later:
        lifecycle.close()
    assert later.value.reason is DatabaseLifecycleRejection.CLEANUP_FAILED
    assert not lifecycle.closed
    assert store.close_calls == 1
    assert leadership.close_calls == 1


def _settings(path: Path) -> HealthHistorySettings:
    return HealthHistorySettings(
        enabled=True,
        database_path=str(path),
        database_mode=HealthHistoryDatabaseMode.CREATE_IF_MISSING,
    )


def _snapshot(path: Path) -> dict[str, list[Any]]:
    connection = sqlite3.connect(
        f"{path.absolute().as_uri()}?mode=rw", uri=True, isolation_level=None
    )
    try:
        return {
            table: connection.execute(
                f"SELECT * FROM {table} ORDER BY rowid"
            ).fetchall()
            for table in _TABLES
        }
    finally:
        connection.close()


def _schema_objects(path: Path) -> list[tuple[object, ...]]:
    connection = sqlite3.connect(
        f"{path.absolute().as_uri()}?mode=rw", uri=True, isolation_level=None
    )
    try:
        return connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name"
        ).fetchall()
    finally:
        connection.close()


def _seed_checkpoint_eligible_wal(store: HealthHistoryStore) -> None:
    connection = store._connection  # noqa: SLF001
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
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
        ((cursor.lastrowid, index + 1) for index in range(30_000)),
    )
    connection.commit()
    wal = store.inspect_wal()
    assert wal.checkpoint_due and not wal.oversize


def test_real_lifecycle_checkpoint_preserves_schema_data_identity_and_ownership(
    history_test_directory: Path,
) -> None:
    path = history_test_directory / "history.sqlite3"
    lifecycle = bootstrap_health_history_database(_settings(path), created_at_utc_us=1)
    assert lifecycle is not None
    store = lifecycle.store
    _seed_checkpoint_eligible_wal(store)
    before_rows = _snapshot(path)
    before_schema = _schema_objects(path)
    traced: list[str] = []
    store._connection.set_trace_callback(traced.append)  # noqa: SLF001
    try:
        result = checkpoint_health_history_startup_wal(lifecycle)
    finally:
        store._connection.set_trace_callback(None)  # noqa: SLF001

    assert result.outcome in {
        StorageDecisionOutcome.PROCEED,
        StorageDecisionOutcome.WAL_CHECKPOINT_DUE,
    }
    assert traced.count("PRAGMA wal_checkpoint(PASSIVE)") == 1
    assert 2 <= traced.count("PRAGMA wal_checkpoint(NOOP)") <= 3
    assert not any(statement.startswith("DELETE") for statement in traced)
    assert not any("incremental_vacuum" in statement.lower() for statement in traced)
    assert _snapshot(path) == before_rows
    assert _schema_objects(path) == before_schema
    assert not store.closed
    assert not lifecycle.closed
    assert lifecycle.store is store
    with pytest.raises(LeadershipError) as busy:
        HealthHistoryLeadership.acquire(history_test_directory)
    assert busy.value.reason is LeadershipRejection.BUSY

    identity = sqlite3.connect(path)
    try:
        assert identity.execute("PRAGMA application_id").fetchone() == (APPLICATION_ID,)
        assert identity.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
    finally:
        identity.close()

    lifecycle.close()
    later = HealthHistoryLeadership.acquire(history_test_directory)
    later.close()


def test_public_contract_bounds_and_import_isolation() -> None:
    signature = inspect.signature(checkpoint_health_history_startup_wal)
    assert list(signature.parameters) == ["lifecycle"]
    assert SCHEMA_VERSION == 1
    assert MINIMUM_SAFE_SQLITE_VERSION == (3, 51, 3)
    assert MAX_DATABASE_BYTES == 64 * 1024 * 1024
    assert MAX_DATABASE_PAGES == MAX_DATABASE_BYTES // PAGE_SIZE_BYTES
    assert FOREIGN_KEY_CHECK_SECONDS == 1.0
    assert QUICK_CHECK_SECONDS == 2.0
    assert BUSY_TIMEOUT_MILLISECONDS == 250
    assert HealthReport.__dataclass_fields__["schema_version"].default == 1

    source = Path(checkpoint_module.__file__).read_text(encoding="utf-8")
    assert source.count("preflight_health_history_storage(lifecycle)") == 2
    assert source.count("passive_wal_checkpoint()") == 1
    prohibited = (
        "cleanup_retention",
        "incremental_vacuum",
        ".ingest(",
        ".verify(",
        "lifecycle.close",
        "HealthHistoryOrchestrator",
        "HealthHistoryScheduler",
        "HealthService",
        "dashboard",
        "runtime",
        "config",
        "yaml",
        "argparse",
        "threading",
        "create_task",
        "datetime",
        "sleep",
        "while ",
        "\n    for ",
        "TRUNCATE",
        "RESTART",
        "FULL",
    )
    for token in prohibited:
        assert token not in source

    root = Path(__file__).parents[1] / "src" / "aurora_core"
    entry_points = [
        root / "__main__.py",
        root / "dashboard" / "server.py",
        root / "dashboard" / "service.py",
        root / "health_history" / "orchestration.py",
        root / "health_history" / "scheduler.py",
        *sorted((root / "runtime").glob("*.py")),
    ]
    for path in entry_points:
        entry_source = path.read_text(encoding="utf-8")
        assert "health_history.startup_checkpoint" not in entry_source
        assert "checkpoint_health_history_startup_wal" not in entry_source
