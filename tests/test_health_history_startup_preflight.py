"""Direct-only tests for startup health-history storage readiness."""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest

import aurora_core.health_history.sqlite_runtime as sqlite_runtime
import aurora_core.health_history.startup_preflight as preflight_module
from aurora_core.config import HealthHistoryDatabaseMode, HealthHistorySettings
from aurora_core.dashboard.models import HealthReport
from aurora_core.health_history import (
    DatabaseLifecycleError,
    DatabaseLifecycleRejection,
    FreeSpaceResult,
    HealthHistoryDatabaseLifecycle,
    HealthHistoryLeadership,
    LeadershipError,
    LeadershipRejection,
    StorageCapacityResult,
    StorageDecisionOutcome,
    StorageDecisionResult,
    StorageEnvelopeError,
    StorageEnvelopeRejection,
    WalInspectionResult,
    bootstrap_health_history_database,
    preflight_health_history_storage,
)
from aurora_core.health_history.models import (
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
    FREE_SPACE_RESERVE_BYTES,
    WAL_CHECKPOINT_THRESHOLD_FRAMES,
    WAL_FRAME_BYTES,
    WAL_HARD_LIMIT_BYTES,
    WAL_HARD_LIMIT_FRAMES,
    WAL_HEADER_BYTES,
    decide_storage_action,
)
from aurora_core.health_history.store import HealthHistoryStore

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


class _FakeStore:
    def __init__(
        self,
        *,
        capacity: StorageCapacityResult | None = None,
        free_space: FreeSpaceResult | None = None,
        wal: WalInspectionResult | None = None,
        failure_stage: str | None = None,
        close_failures: int = 0,
    ) -> None:
        self.capacity = capacity if capacity is not None else _capacity()
        self.free_space = free_space if free_space is not None else _free_space()
        self.wal = wal if wal is not None else _wal()
        self.failure_stage = failure_stage
        self.close_failures = close_failures
        self.events: list[str] = []
        self.close_calls = 0
        self.closed = False

    def _inspect[T](self, stage: str, result: T) -> T:
        self.events.append(stage)
        if self.failure_stage == stage:
            try:
                raise sqlite3.OperationalError(_PRIVATE_CANARY)
            except sqlite3.OperationalError:
                raise StorageEnvelopeError(
                    StorageEnvelopeRejection.PERSISTENCE_FAILED
                ) from None
        return result

    def inspect_storage_capacity(self) -> StorageCapacityResult:
        return self._inspect("capacity", self.capacity)

    def inspect_free_space(self) -> FreeSpaceResult:
        return self._inspect("free_space", self.free_space)

    def inspect_wal(self) -> WalInspectionResult:
        return self._inspect("wal", self.wal)

    def passive_wal_checkpoint(self) -> None:
        raise AssertionError("preflight must not checkpoint")

    def cleanup_retention(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("preflight must not run retention cleanup")

    def incremental_vacuum(self) -> None:
        raise AssertionError("preflight must not vacuum")

    def verify(self) -> None:
        raise AssertionError("preflight must not run extra verification")

    def close(self) -> None:
        self.close_calls += 1
        self.events.append("store_close")
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
        self.close_calls = 0
        self.closed = False

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


def _capacity(*, pages_remaining: int = 100) -> StorageCapacityResult:
    page_count = MAX_DATABASE_PAGES - pages_remaining
    return StorageCapacityResult(
        page_count=page_count,
        freelist_count=0,
        maximum_page_count=MAX_DATABASE_PAGES,
        used_bytes=page_count * PAGE_SIZE_BYTES,
        maximum_bytes=MAX_DATABASE_BYTES,
        pages_remaining=pages_remaining,
    )


def _free_space(*, sufficient: bool = True) -> FreeSpaceResult:
    return FreeSpaceResult(
        sufficient=sufficient,
        free_bytes=(
            FREE_SPACE_RESERVE_BYTES if sufficient else FREE_SPACE_RESERVE_BYTES - 1
        ),
        required_reserve_bytes=FREE_SPACE_RESERVE_BYTES,
    )


def _wal(
    logical_frames: int = 0,
    *,
    physical_slots: int | None = None,
) -> WalInspectionResult:
    if physical_slots is None:
        physical_slots = logical_frames
    exists = physical_slots > 0
    total_bytes = WAL_HEADER_BYTES + physical_slots * WAL_FRAME_BYTES if exists else 0
    oversize = (
        logical_frames > WAL_HARD_LIMIT_FRAMES or total_bytes > WAL_HARD_LIMIT_BYTES
    )
    return WalInspectionResult(
        exists=exists,
        logical_frame_count=logical_frames,
        physical_frame_slots=physical_slots,
        total_bytes=total_bytes,
        checkpointed_frames=0,
        checkpoint_due=(
            WAL_CHECKPOINT_THRESHOLD_FRAMES <= logical_frames <= WAL_HARD_LIMIT_FRAMES
            and not oversize
        ),
        oversize=oversize,
    )


def _lifecycle(
    store: _FakeStore,
    leadership: _FakeLeadership,
) -> HealthHistoryDatabaseLifecycle:
    return HealthHistoryDatabaseLifecycle(
        store=cast(HealthHistoryStore, store),
        leadership=cast(HealthHistoryLeadership, leadership),
    )


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


def test_ready_preflight_inspects_once_in_order_and_decides_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _FakeStore()
    leadership = _FakeLeadership()
    lifecycle = _lifecycle(store, leadership)
    decision_calls: list[
        tuple[
            StorageCapacityResult,
            FreeSpaceResult,
            WalInspectionResult,
            bool,
        ]
    ] = []

    def tracked_decision(
        capacity: StorageCapacityResult,
        free_space: FreeSpaceResult,
        wal: WalInspectionResult,
        *,
        capacity_maintenance_attempted: bool = False,
    ) -> StorageDecisionResult:
        decision_calls.append(
            (capacity, free_space, wal, capacity_maintenance_attempted)
        )
        return decide_storage_action(
            capacity,
            free_space,
            wal,
            capacity_maintenance_attempted=capacity_maintenance_attempted,
        )

    monkeypatch.setattr(preflight_module, "decide_storage_action", tracked_decision)

    result = preflight_health_history_storage(lifecycle)

    assert result == StorageDecisionResult(
        outcome=StorageDecisionOutcome.PROCEED,
        write_permitted=True,
    )
    assert store.events == ["capacity", "free_space", "wal"]
    assert decision_calls == [(store.capacity, store.free_space, store.wal, False)]
    assert not lifecycle.closed
    assert leadership.held
    assert store.close_calls == leadership.close_calls == 0

    lifecycle.close()


@pytest.mark.parametrize(
    ("capacity", "free_space", "wal", "expected"),
    [
        (
            _capacity(pages_remaining=0),
            _free_space(),
            _wal(),
            StorageDecisionOutcome.CAPACITY_MAINTENANCE_REQUIRED,
        ),
        (
            _capacity(),
            _free_space(sufficient=False),
            _wal(),
            StorageDecisionOutcome.CAPACITY_MAINTENANCE_REQUIRED,
        ),
        (
            _capacity(),
            _free_space(),
            _wal(WAL_CHECKPOINT_THRESHOLD_FRAMES),
            StorageDecisionOutcome.WAL_CHECKPOINT_DUE,
        ),
        (
            _capacity(),
            _free_space(),
            _wal(WAL_HARD_LIMIT_FRAMES + 1),
            StorageDecisionOutcome.WAL_OVERSIZE_BLOCKED,
        ),
        (
            _capacity(),
            _free_space(),
            _wal(1, physical_slots=1020),
            StorageDecisionOutcome.WAL_OVERSIZE_BLOCKED,
        ),
    ],
    ids=(
        "full-capacity",
        "low-free-space",
        "checkpoint-threshold",
        "logical-wal-oversize",
        "physical-wal-oversize",
    ),
)
def test_existing_non_ready_decisions_are_returned_without_remediation(
    capacity: StorageCapacityResult,
    free_space: FreeSpaceResult,
    wal: WalInspectionResult,
    expected: StorageDecisionOutcome,
) -> None:
    store = _FakeStore(capacity=capacity, free_space=free_space, wal=wal)
    leadership = _FakeLeadership()
    lifecycle = _lifecycle(store, leadership)

    result = preflight_health_history_storage(lifecycle)

    assert result.outcome is expected
    assert not result.write_permitted
    assert result.outcome is not StorageDecisionOutcome.CAPACITY_BLOCKED
    assert store.events == ["capacity", "free_space", "wal"]
    assert not lifecycle.closed
    assert leadership.held

    lifecycle.close()


@pytest.mark.parametrize(
    ("capacity", "wal", "expected"),
    [
        (
            _capacity(pages_remaining=0),
            _wal(WAL_HARD_LIMIT_FRAMES + 1),
            StorageDecisionOutcome.WAL_OVERSIZE_BLOCKED,
        ),
        (
            _capacity(pages_remaining=0),
            _wal(WAL_CHECKPOINT_THRESHOLD_FRAMES),
            StorageDecisionOutcome.CAPACITY_MAINTENANCE_REQUIRED,
        ),
    ],
    ids=("wal-oversize-over-capacity", "capacity-over-checkpoint"),
)
def test_preflight_preserves_existing_decision_priority(
    capacity: StorageCapacityResult,
    wal: WalInspectionResult,
    expected: StorageDecisionOutcome,
) -> None:
    lifecycle = _lifecycle(_FakeStore(capacity=capacity, wal=wal), _FakeLeadership())
    assert preflight_health_history_storage(lifecycle).outcome is expected
    lifecycle.close()


@pytest.mark.parametrize(
    ("failure_stage", "expected_events"),
    [
        ("capacity", ["capacity"]),
        ("free_space", ["capacity", "free_space"]),
        ("wal", ["capacity", "free_space", "wal"]),
    ],
)
def test_first_inspection_failure_stops_and_remains_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
    expected_events: list[str],
) -> None:
    store = _FakeStore(failure_stage=failure_stage)
    leadership = _FakeLeadership()
    lifecycle = _lifecycle(store, leadership)

    def unexpected_decision(*args: object, **kwargs: object) -> StorageDecisionResult:
        del args, kwargs
        raise AssertionError("decision must not follow failed inspection")

    monkeypatch.setattr(preflight_module, "decide_storage_action", unexpected_decision)

    with pytest.raises(StorageEnvelopeError) as captured:
        preflight_health_history_storage(lifecycle)

    assert captured.value.reason is StorageEnvelopeRejection.PERSISTENCE_FAILED
    assert str(captured.value) == "persistence_failed"
    assert captured.value.__cause__ is None
    assert _PRIVATE_CANARY not in str(captured.value)
    assert store.events == expected_events
    assert not lifecycle.closed
    assert leadership.held
    assert store.close_calls == leadership.close_calls == 0

    lifecycle.close()


def test_closed_and_cleanup_failed_lifecycles_reject_before_inspection() -> None:
    closed_store = _FakeStore()
    closed_lifecycle = _lifecycle(closed_store, _FakeLeadership())
    closed_lifecycle.close()
    with pytest.raises(DatabaseLifecycleError) as closed:
        preflight_health_history_storage(closed_lifecycle)
    assert closed.value.reason is DatabaseLifecycleRejection.CLOSED
    assert not any(
        event in closed_store.events for event in ("capacity", "free_space", "wal")
    )

    failed_store = _FakeStore(close_failures=1)
    leadership = _FakeLeadership()
    failed_lifecycle = _lifecycle(failed_store, leadership)
    with pytest.raises(DatabaseLifecycleError):
        failed_lifecycle.close()
    with pytest.raises(DatabaseLifecycleError) as cleanup_failed:
        preflight_health_history_storage(failed_lifecycle)
    assert cleanup_failed.value.reason is DatabaseLifecycleRejection.CLEANUP_FAILED
    assert not any(
        event in failed_store.events for event in ("capacity", "free_space", "wal")
    )
    assert leadership.held

    failed_lifecycle.close()


def test_closed_owned_store_fails_closed_without_releasing_ownership() -> None:
    store = _FakeStore()
    leadership = _FakeLeadership()
    lifecycle = _lifecycle(store, leadership)
    store.close()

    with pytest.raises(DatabaseLifecycleError) as captured:
        preflight_health_history_storage(lifecycle)

    assert captured.value.reason is DatabaseLifecycleRejection.TRUST_FAILED
    assert str(captured.value) == "trust_failed"
    assert store.events == ["store_close"]
    assert store.close_calls == 1
    assert not lifecycle.closed
    assert leadership.held
    assert leadership.close_calls == 0

    lifecycle.close()
    assert lifecycle.closed
    assert store.close_calls == 2
    assert leadership.close_calls == 1


@pytest.mark.parametrize(
    ("method_name", "expected_events"),
    [
        ("inspect_storage_capacity", ["capacity"]),
        ("inspect_free_space", ["capacity", "free_space"]),
        ("inspect_wal", ["capacity", "free_space", "wal"]),
    ],
)
def test_unrelated_inspection_exceptions_are_not_broadly_translated(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    expected_events: list[str],
) -> None:
    store = _FakeStore()
    leadership = _FakeLeadership()
    lifecycle = _lifecycle(store, leadership)
    stage = expected_events[-1]
    failure = RuntimeError("unrelated-inspection-failure")

    def fail() -> None:
        store.events.append(stage)
        raise failure

    def unexpected_decision(*args: object, **kwargs: object) -> StorageDecisionResult:
        del args, kwargs
        raise AssertionError("decision must not follow failed inspection")

    monkeypatch.setattr(store, method_name, fail)
    monkeypatch.setattr(preflight_module, "decide_storage_action", unexpected_decision)

    with pytest.raises(RuntimeError) as captured:
        preflight_health_history_storage(lifecycle)

    assert captured.value is failure
    assert store.events == expected_events
    assert not lifecycle.closed
    assert leadership.held
    assert store.close_calls == leadership.close_calls == 0

    lifecycle.close()


def test_store_close_failure_after_preflight_remains_retryable() -> None:
    store = _FakeStore(close_failures=1)
    leadership = _FakeLeadership()
    lifecycle = _lifecycle(store, leadership)
    preflight_health_history_storage(lifecycle)

    with pytest.raises(DatabaseLifecycleError) as captured:
        lifecycle.close()
    assert captured.value.reason is DatabaseLifecycleRejection.CLEANUP_FAILED
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
def test_leadership_release_failure_after_preflight_is_terminal(
    reports_closed: bool,
) -> None:
    store = _FakeStore()
    leadership = _FakeLeadership(
        close_failures=1,
        closes_on_failure=reports_closed,
    )
    lifecycle = _lifecycle(store, leadership)
    preflight_health_history_storage(lifecycle)

    with pytest.raises(DatabaseLifecycleError) as first:
        lifecycle.close()
    assert first.value.reason is DatabaseLifecycleRejection.CLEANUP_FAILED
    assert not lifecycle.closed
    assert store.closed
    assert leadership.closed is reports_closed
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


@pytest.mark.parametrize(
    "version",
    [(3, 51, 2), (3, 51), ("3", 51, 3)],
    ids=("unsupported", "short", "non-integer"),
)
def test_wal_runtime_rejection_has_no_fallback_and_preserves_ownership(
    history_test_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: object,
) -> None:
    path = history_test_directory / "history.sqlite3"
    lifecycle = bootstrap_health_history_database(_settings(path), created_at_utc_us=1)
    assert lifecycle is not None
    store = lifecycle.store
    traced: list[str] = []
    monkeypatch.setattr(sqlite_runtime.sqlite3, "sqlite_version_info", version)
    store._connection.set_trace_callback(traced.append)  # noqa: SLF001
    try:
        with pytest.raises(StorageEnvelopeError) as captured:
            preflight_health_history_storage(lifecycle)
    finally:
        store._connection.set_trace_callback(None)  # noqa: SLF001

    assert captured.value.reason is StorageEnvelopeRejection.UNSUPPORTED_RUNTIME
    assert str(captured.value) == "unsupported_runtime"
    assert not captured.value.trust_lost
    assert not lifecycle.closed
    assert lifecycle.store is store
    assert not any("wal_checkpoint" in statement.lower() for statement in traced)

    lifecycle.close()


def test_real_preflight_preserves_schema_tables_and_leadership_ownership(
    history_test_directory: Path,
) -> None:
    path = history_test_directory / "history.sqlite3"
    lifecycle = bootstrap_health_history_database(_settings(path), created_at_utc_us=1)
    assert lifecycle is not None
    before = _snapshot(path)

    result = preflight_health_history_storage(lifecycle)

    assert result.outcome is StorageDecisionOutcome.PROCEED
    assert result.write_permitted
    assert _snapshot(path) == before
    with pytest.raises(LeadershipError) as busy:
        HealthHistoryLeadership.acquire(history_test_directory)
    assert busy.value.reason is LeadershipRejection.BUSY

    lifecycle.close()
    later = HealthHistoryLeadership.acquire(history_test_directory)
    later.close()

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
    finally:
        connection.close()


def test_preflight_public_contract_and_preserved_storage_bounds() -> None:
    signature = inspect.signature(preflight_health_history_storage)
    assert list(signature.parameters) == ["lifecycle"]
    assert SCHEMA_VERSION == 1
    assert MAX_DATABASE_BYTES == 64 * 1024 * 1024
    assert MAX_DATABASE_PAGES == MAX_DATABASE_BYTES // PAGE_SIZE_BYTES
    assert MINIMUM_SAFE_SQLITE_VERSION == (3, 51, 3)
    assert FOREIGN_KEY_CHECK_SECONDS == 1.0
    assert QUICK_CHECK_SECONDS == 2.0
    assert HealthReport.__dataclass_fields__["schema_version"].default == 1


def test_preflight_remains_disconnected_from_runtime_and_remediation() -> None:
    source = Path(preflight_module.__file__).read_text(encoding="utf-8")
    prohibited = (
        "passive_wal_checkpoint",
        "cleanup_retention",
        "incremental_vacuum",
        "HealthHistoryOrchestrator",
        "HealthHistoryScheduler",
        "HealthService",
        "dashboard",
        "runtime",
        "config",
        "yaml",
        "os.environ",
        "argparse",
        "threading",
        "create_task",
        "datetime",
        "monotonic",
        "sleep",
        "lifecycle.close",
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
        assert "health_history.startup_preflight" not in entry_source
        assert "preflight_health_history_storage" not in entry_source
