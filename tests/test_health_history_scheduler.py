"""Synthetic tests for the direct-only Milestone 18 scheduler core."""

from __future__ import annotations

import math
import socket
import sqlite3
import subprocess
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest

import aurora_core.health_history.queries as queries
import aurora_core.health_history.scheduler as scheduler_module
import aurora_core.health_history.storage_envelope as storage_envelope
from aurora_core.dashboard.models import ComponentHealth, HealthReport, HealthStatus
from aurora_core.health_history.ingestion import IngestionOutcome, IngestionResult
from aurora_core.health_history.maintenance import (
    IncrementalVacuumResult,
    MaintenanceOutcome,
    RetentionCleanupResult,
)
from aurora_core.health_history.models import (
    COMPONENT_ORDER,
    MAX_BOUNDED_COUNTER,
    MAX_DATABASE_BYTES,
    MAX_DATABASE_PAGES,
    MAX_OBSERVATION_SEQUENCE,
    MAX_TIMESTAMP_US,
    PAGE_SIZE_BYTES,
    ComponentName,
    SampleKind,
)
from aurora_core.health_history.orchestration import (
    HealthHistoryOrchestrator,
    MaintenanceOpportunityResult,
    MaintenanceTriggerDecision,
    MaintenanceTriggerReason,
    ObservationCycleResult,
    OrchestrationError,
    OrchestrationOutcome,
    OrchestrationRejection,
)
from aurora_core.health_history.projection import (
    HealthProjection,
    ProjectionError,
    ProjectionRejection,
    project_health_report,
)
from aurora_core.health_history.queries import (
    QueryError,
    QueryRejection,
    QueryStage,
    SchedulerResumeState,
)
from aurora_core.health_history.scheduler import (
    DEFAULT_SAMPLE_INTERVAL_SECONDS,
    MAX_SAMPLE_INTERVAL_SECONDS,
    MIN_SAMPLE_INTERVAL_SECONDS,
    HealthHistoryScheduler,
    SchedulerCadenceState,
    SchedulerDueDecision,
    SchedulerError,
    SchedulerOutcome,
    SchedulerRejection,
    SchedulerResult,
)
from aurora_core.health_history.store import HealthHistoryStore, StoreError

_NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
_NOW_US = 1_786_449_600_000_000
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
_DEFAULT_OBSERVATION = ObservationCycleResult(OrchestrationOutcome.STORED)
_DEFAULT_TRIGGER = MaintenanceTriggerDecision(False, MaintenanceTriggerReason.NONE)
_DEFAULT_MAINTENANCE = MaintenanceOpportunityResult(
    OrchestrationOutcome.MAINTENANCE_COMPLETED
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
    store = HealthHistoryStore.create(path, created_at_utc_us=_NOW_US)
    try:
        yield path, store
    finally:
        store.close()


def _wled_details() -> dict[str, object]:
    return {
        "info_reason_code": "validated",
        "state_reason_code": "validated",
        "firmware_version": "excluded",
        "uptime_seconds": 1,
        "reported_led_count": 10,
        "expected_led_count": 10,
        "expected_active_led_count": 8,
        "expected_skipped_leds": 2,
        "led_count_matches": True,
        "estimated_current_milliamps": 10,
        "current_limit_milliamps": 20,
        "brightness": 1,
        "output_on": True,
    }


def _component(
    name: ComponentName,
    checked_at: datetime,
) -> ComponentHealth:
    details: dict[str, object]
    if name is ComponentName.WLED:
        details = _wled_details()
    elif name is ComponentName.HYPERHDR:
        details = {
            "reason_code": "validated",
            "server_info_received": True,
            "hdr_mode_enabled": None,
            "instance_running": True,
            "grabber_active": True,
            "led_output_active": True,
        }
    elif name is ComponentName.CAPTURE:
        details = {
            "reason_code": "validated",
            "device_node_present": True,
            "character_device": True,
            "v4l2_registered": True,
            "process_read_access": True,
            "device_name": "excluded",
        }
    else:
        details = {
            "cpu_temperature_c": 40.0,
            "cpu_temperature_warning_c": 80.0,
            "load_average_1m": 0.1,
            "load_average_5m": 0.2,
            "load_average_15m": 0.3,
            "logical_cpu_count": 4,
            "memory_used_percent": 20.0,
            "memory_warning_percent": 90.0,
            "root_storage_used_percent": 30.0,
            "storage_warning_percent": 90.0,
            "host_uptime_seconds": 100.0,
        }
    text = checked_at.isoformat()
    return ComponentHealth(
        name=name.value,
        status=HealthStatus.HEALTHY,
        message="excluded",
        checked_at=text,
        latency_ms=1.0,
        details=details,
        last_successful_at=text,
    )


def _report(checked_at: datetime = _NOW) -> HealthReport:
    return HealthReport(
        status=HealthStatus.HEALTHY,
        checked_at=checked_at.isoformat(),
        service_uptime_seconds=1.0,
        components=tuple(_component(name, checked_at) for name in COMPONENT_ORDER),
    )


def _empty_resume() -> SchedulerResumeState:
    return SchedulerResumeState(None, None, None, 0)


def _resume(
    sequence: int,
    *,
    observed_at: int = _NOW_US,
    sample_kind: SampleKind = SampleKind.HEARTBEAT,
    count: int = 1,
) -> SchedulerResumeState:
    return SchedulerResumeState(sequence, observed_at, sample_kind, count)


class _Clock:
    def __init__(self, *values: object) -> None:
        self.values = list(values)
        self.calls = 0

    def __call__(self) -> object:
        self.calls += 1
        if not self.values:
            raise AssertionError("unexpected clock read")
        return self.values.pop(0)


class _FakeStore:
    def __init__(self, resume: SchedulerResumeState | BaseException | object) -> None:
        self.resume: SchedulerResumeState | BaseException | object = resume
        self.calls: list[str] = []
        self.closed = False

    def get_scheduler_resume_state(self) -> SchedulerResumeState:
        self.calls.append("resume")
        if isinstance(self.resume, BaseException):
            raise self.resume
        return cast(SchedulerResumeState, self.resume)

    def close(self) -> None:
        self.calls.append("close")
        self.closed = True


class _FakeOrchestrator:
    def __init__(
        self,
        store: _FakeStore,
        *,
        observation: ObservationCycleResult
        | BaseException
        | object = _DEFAULT_OBSERVATION,
        trigger: MaintenanceTriggerDecision | BaseException | object = _DEFAULT_TRIGGER,
        maintenance: MaintenanceOpportunityResult
        | BaseException
        | object = _DEFAULT_MAINTENANCE,
        events: list[str] | None = None,
    ) -> None:
        self.store = store
        self.observation = observation
        self.trigger = trigger
        self.maintenance = maintenance
        self.events = events if events is not None else []
        self.projections: list[HealthProjection] = []
        self.observation_calls = 0
        self.trigger_calls = 0
        self.maintenance_calls = 0

    def process_observation(
        self, projection: HealthProjection
    ) -> ObservationCycleResult:
        self.events.append("orchestration")
        self.observation_calls += 1
        self.projections.append(projection)
        if isinstance(self.observation, BaseException):
            raise self.observation
        result = cast(ObservationCycleResult, self.observation)
        if result.outcome in {
            OrchestrationOutcome.STORED,
            OrchestrationOutcome.STATE_ONLY,
            OrchestrationOutcome.REPLAYED,
        }:
            prior = cast(SchedulerResumeState, self.store.resume)
            self.store.resume = SchedulerResumeState(
                projection.observation_sequence,
                projection.observed_at_utc_us,
                projection.sample_kind,
                min(prior.accepted_observation_count + 1, MAX_BOUNDED_COUNTER),
            )
        return result

    def maintenance_trigger(self) -> MaintenanceTriggerDecision:
        self.events.append("trigger")
        self.trigger_calls += 1
        if isinstance(self.trigger, BaseException):
            raise self.trigger
        return cast(MaintenanceTriggerDecision, self.trigger)

    def run_maintenance_opportunity(self) -> MaintenanceOpportunityResult:
        self.events.append("maintenance")
        self.maintenance_calls += 1
        if isinstance(self.maintenance, BaseException):
            raise self.maintenance
        return cast(MaintenanceOpportunityResult, self.maintenance)


class _RetryHazardOrchestrator(_FakeOrchestrator):
    def __init__(
        self,
        store: _FakeStore,
        *,
        observation_outcome: OrchestrationOutcome,
        hazard: str,
        storage_maintenance_attempted: bool = False,
    ) -> None:
        super().__init__(
            store,
            observation=ObservationCycleResult(
                observation_outcome,
                storage_maintenance_attempted=storage_maintenance_attempted,
            ),
            trigger=MaintenanceTriggerDecision(
                True,
                MaintenanceTriggerReason.STARTUP,
            ),
        )
        self.hazard = hazard
        self.passive_calls = 0
        self.cleanup_calls = 0
        self.vacuum_calls = 0

    def process_observation(
        self, projection: HealthProjection
    ) -> ObservationCycleResult:
        if self.hazard == "checkpoint":
            self.passive_calls += 1
        else:
            self.cleanup_calls += 1
            self.vacuum_calls += 1
        return super().process_observation(projection)

    def run_maintenance_opportunity(self) -> MaintenanceOpportunityResult:
        if self.hazard == "checkpoint":
            self.passive_calls += 1
        else:
            self.cleanup_calls += 1
            self.vacuum_calls += 1
        return super().run_maintenance_opportunity()


def _scheduler(
    *,
    resume: SchedulerResumeState | BaseException | object | None = None,
    monotonic: Callable[[], float] | None = None,
    utc_now: Callable[[], datetime] = lambda: _NOW,
    supplier: Callable[[], HealthReport] = _report,
    observation: ObservationCycleResult | BaseException | object = _DEFAULT_OBSERVATION,
    trigger: MaintenanceTriggerDecision | BaseException | object = _DEFAULT_TRIGGER,
    maintenance: MaintenanceOpportunityResult
    | BaseException
    | object = _DEFAULT_MAINTENANCE,
    interval: int = DEFAULT_SAMPLE_INTERVAL_SECONDS,
    refresh: int = 5,
    events: list[str] | None = None,
) -> tuple[HealthHistoryScheduler, _FakeStore, _FakeOrchestrator]:
    store = _FakeStore(_empty_resume() if resume is None else resume)
    orchestrator = _FakeOrchestrator(
        store,
        observation=observation,
        trigger=trigger,
        maintenance=maintenance,
        events=events,
    )
    selected_clock = monotonic or cast(Callable[[], float], _Clock(0.0, 0.0))
    instance = HealthHistoryScheduler(
        cast(HealthHistoryStore, store),
        cast(HealthHistoryOrchestrator, orchestrator),
        health_report_supplier=supplier,
        monotonic=selected_clock,
        utc_now=utc_now,
        sample_interval_seconds=interval,
        dashboard_refresh_seconds=refresh,
    )
    return instance, store, orchestrator


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"{path.absolute().as_uri()}?mode=rw",
        uri=True,
        isolation_level=None,
    )
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _snapshot(path: Path) -> dict[str, list[tuple[object, ...]]]:
    connection = _connect(path)
    try:
        return {
            table: connection.execute(
                f'SELECT * FROM "{table}" ORDER BY rowid'
            ).fetchall()
            for table in _TABLES
        }
    finally:
        connection.close()


def _seed(store: HealthHistoryStore, sequence: int = 1) -> None:
    store.ingest(
        project_health_report(
            _report(),
            observation_sequence=sequence,
            recorded_at=_NOW + timedelta(seconds=1),
        )
    )


def _capacity(*, full: bool = False) -> storage_envelope.StorageCapacityResult:
    page_count = MAX_DATABASE_PAGES if full else 20
    return storage_envelope.StorageCapacityResult(
        page_count=page_count,
        freelist_count=0,
        maximum_page_count=MAX_DATABASE_PAGES,
        used_bytes=page_count * PAGE_SIZE_BYTES,
        maximum_bytes=MAX_DATABASE_BYTES,
        pages_remaining=MAX_DATABASE_PAGES - page_count,
    )


def _free_space() -> storage_envelope.FreeSpaceResult:
    return storage_envelope.FreeSpaceResult(
        sufficient=True,
        free_bytes=storage_envelope.FREE_SPACE_RESERVE_BYTES,
        required_reserve_bytes=storage_envelope.FREE_SPACE_RESERVE_BYTES,
    )


def _wal(frames: int = 0) -> storage_envelope.WalInspectionResult:
    exists = frames > 0
    total_bytes = (
        storage_envelope.WAL_HEADER_BYTES + frames * storage_envelope.WAL_FRAME_BYTES
        if exists
        else 0
    )
    return storage_envelope.WalInspectionResult(
        exists=exists,
        logical_frame_count=frames,
        physical_frame_slots=frames,
        total_bytes=total_bytes,
        checkpointed_frames=0,
        checkpoint_due=frames >= storage_envelope.WAL_CHECKPOINT_THRESHOLD_FRAMES,
        oversize=False,
    )


def _composed_store() -> Mock:
    store = Mock()
    store.get_scheduler_resume_state.return_value = _empty_resume()
    store.inspect_storage_capacity.return_value = _capacity()
    store.inspect_free_space.return_value = _free_space()
    store.inspect_wal.return_value = _wal()
    store.cleanup_retention.return_value = RetentionCleanupResult(
        MaintenanceOutcome.NO_WORK,
        0,
        0,
        0,
    )
    store.incremental_vacuum.return_value = IncrementalVacuumResult(
        MaintenanceOutcome.NO_WORK,
        0,
        0,
        0,
    )
    store.passive_wal_checkpoint.return_value = (
        storage_envelope.PassiveCheckpointResult(
            storage_envelope.PassiveCheckpointOutcome.COMPLETED,
            storage_envelope.WAL_CHECKPOINT_THRESHOLD_FRAMES,
            storage_envelope.WAL_HEADER_BYTES
            + storage_envelope.WAL_CHECKPOINT_THRESHOLD_FRAMES
            * storage_envelope.WAL_FRAME_BYTES,
            False,
            storage_envelope.WAL_CHECKPOINT_THRESHOLD_FRAMES,
            storage_envelope.WAL_CHECKPOINT_THRESHOLD_FRAMES,
        )
    )
    store.ingest.return_value = IngestionResult(IngestionOutcome.TRANSITION_STORED)
    return store


def _composed_scheduler(
    store: Mock,
) -> tuple[HealthHistoryScheduler, HealthHistoryOrchestrator]:
    orchestrator = HealthHistoryOrchestrator(
        cast(HealthHistoryStore, store),
        monotonic=lambda: 0.0,
        utc_now_us=lambda: _NOW_US,
    )
    scheduler = HealthHistoryScheduler(
        cast(HealthHistoryStore, store),
        orchestrator,
        health_report_supplier=_report,
        monotonic=lambda: 0.0,
        utc_now=lambda: _NOW,
    )
    return scheduler, orchestrator


def test_empty_resume_state_read_is_immutable(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    before = _snapshot(path)
    assert store.get_scheduler_resume_state() == _empty_resume()
    assert _snapshot(path) == before


def test_populated_resume_state_returns_only_scheduler_fields(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    _seed(store)
    before = _snapshot(path)
    assert store.get_scheduler_resume_state() == _resume(1)
    assert _snapshot(path) == before


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("last_committed_sequence", "bad"),
        ("last_accepted_observed_at_utc_us", "bad"),
        ("last_accepted_sample_kind", "bad"),
        ("accepted_observation_count", "bad"),
    ],
)
def test_malformed_resume_checkpoint_fails_closed(
    store_path: tuple[Path, HealthHistoryStore],
    column: str,
    value: object,
) -> None:
    path, store = store_path
    _seed(store)
    connection = _connect(path)
    try:
        connection.execute("DROP TRIGGER trg_ingestion_checkpoint_no_regression")
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            f"UPDATE ingestion_checkpoint SET {column} = ? WHERE singleton_id = 1",
            (value,),
        )
    finally:
        connection.close()
    before = _snapshot(path)
    with pytest.raises(QueryError) as caught:
        store.get_scheduler_resume_state()
    assert caught.value.reason is QueryRejection.MALFORMED_STATE
    assert caught.value.trust_lost
    assert str(caught.value) == "malformed_state"
    assert store.closed
    assert _snapshot(path) == before


def test_resume_read_revalidates_the_checkpoint_replay_anchor(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    _seed(store)
    connection = _connect(path)
    try:
        connection.execute("DELETE FROM accepted_observation_replay")
    finally:
        connection.close()
    before = _snapshot(path)
    with pytest.raises(QueryError) as caught:
        store.get_scheduler_resume_state()
    assert caught.value.reason is QueryRejection.MALFORMED_STATE
    assert caught.value.trust_lost
    assert store.closed
    assert _snapshot(path) == before


@pytest.mark.parametrize("error_code", [sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED])
def test_resume_busy_and_locked_are_non_trust_failures(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
    error_code: int,
) -> None:
    path, store = store_path
    before = _snapshot(path)

    def fail(stage: QueryStage) -> None:
        assert stage is QueryStage.SCHEDULER_RESUME
        error = sqlite3.OperationalError("private lock detail")
        error.sqlite_errorcode = error_code  # type: ignore[attr-defined]
        raise error

    monkeypatch.setattr(queries, "_fault", fail)
    with pytest.raises(QueryError) as caught:
        store.get_scheduler_resume_state()
    assert caught.value.reason is QueryRejection.STORAGE_BUSY
    assert not store.closed
    assert _snapshot(path) == before


def test_resume_corruption_error_closes_store(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, store = store_path
    before = _snapshot(path)

    def fail(stage: QueryStage) -> None:
        assert stage is QueryStage.SCHEDULER_RESUME
        error = sqlite3.DatabaseError("private corrupt detail")
        error.sqlite_errorcode = sqlite3.SQLITE_CORRUPT  # type: ignore[attr-defined]
        raise error

    monkeypatch.setattr(queries, "_fault", fail)
    with pytest.raises(QueryError) as caught:
        store.get_scheduler_resume_state()
    assert caught.value.reason is QueryRejection.TRUST_FAILED
    assert store.closed
    assert _snapshot(path) == before


@pytest.mark.parametrize(
    "state",
    [
        (None, None, None, 1),
        (None, None, None, -1),
        (None, None, None, True),
        (0, None, None, 1),
        (None, _NOW_US, SampleKind.HEARTBEAT, 1),
        (-1, _NOW_US, SampleKind.HEARTBEAT, 1),
        (MAX_OBSERVATION_SEQUENCE + 1, _NOW_US, SampleKind.HEARTBEAT, 1),
        (1, -1, SampleKind.HEARTBEAT, 1),
        (1, MAX_TIMESTAMP_US + 1, SampleKind.HEARTBEAT, 1),
        (1, _NOW_US, "heartbeat", 1),
        (1, _NOW_US, SampleKind.HEARTBEAT, 0),
        (1, _NOW_US, SampleKind.HEARTBEAT, MAX_BOUNDED_COUNTER + 1),
    ],
)
def test_resume_model_rejects_invalid_combinations(
    state: tuple[object, object, object, object],
) -> None:
    with pytest.raises(ValueError, match="invalid_scheduler_resume_state"):
        SchedulerResumeState(*state)  # type: ignore[arg-type]


def test_sequence_starts_at_one_and_advances_after_stored() -> None:
    instance, _store, orchestrator = _scheduler()
    assert instance.next_observation_sequence == 1
    result = instance.run_due_opportunity()
    assert result.outcome is SchedulerOutcome.STORED
    assert result.observation_sequence == 1
    assert orchestrator.projections[0].observation_sequence == 1
    assert instance.next_observation_sequence == 2


def test_restart_sequence_resumes_exactly_after_checkpoint() -> None:
    instance, _store, orchestrator = _scheduler(resume=_resume(41))
    assert instance.next_observation_sequence == 42
    assert instance.run_due_opportunity().outcome is SchedulerOutcome.STORED
    assert orchestrator.projections[0].observation_sequence == 42


def test_maximum_sequence_exhausts_without_any_opportunity() -> None:
    supplier_calls = 0

    def supplier() -> HealthReport:
        nonlocal supplier_calls
        supplier_calls += 1
        return _report()

    clock = _Clock(0.0)
    instance, store, orchestrator = _scheduler(
        resume=_resume(MAX_OBSERVATION_SEQUENCE),
        monotonic=cast(Callable[[], float], clock),
        supplier=supplier,
    )
    result = instance.run_due_opportunity()
    assert result.outcome is SchedulerOutcome.SEQUENCE_EXHAUSTED
    assert instance.next_observation_sequence is None
    assert supplier_calls == 0
    assert orchestrator.observation_calls == 0
    assert orchestrator.trigger_calls == 0
    assert store.calls == ["resume"]
    assert clock.calls == 1


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (OrchestrationOutcome.STORED, SchedulerOutcome.STORED),
        (OrchestrationOutcome.STATE_ONLY, SchedulerOutcome.STATE_ONLY),
        (OrchestrationOutcome.REPLAYED, SchedulerOutcome.REPLAYED),
    ],
)
def test_accepted_outcomes_advance_sequence(
    outcome: OrchestrationOutcome,
    expected: SchedulerOutcome,
) -> None:
    instance, _store, _orchestrator = _scheduler(
        observation=ObservationCycleResult(outcome)
    )
    result = instance.run_due_opportunity()
    assert result.outcome is expected
    assert instance.next_observation_sequence == 2


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (OrchestrationOutcome.PERSISTENCE_FAILED, SchedulerOutcome.PERSISTENCE_FAILED),
        (OrchestrationOutcome.STORAGE_BUSY, SchedulerOutcome.STORAGE_BUSY),
        (OrchestrationOutcome.STALE_SEQUENCE, SchedulerOutcome.STALE_SEQUENCE),
    ],
)
def test_unaccepted_orchestration_does_not_consume_sequence(
    failure: OrchestrationOutcome,
    expected: SchedulerOutcome,
) -> None:
    clock = _Clock(0.0, 0.0, 30.0)
    instance, _store, orchestrator = _scheduler(
        monotonic=cast(Callable[[], float], clock),
        observation=ObservationCycleResult(failure),
    )
    first = instance.run_due_opportunity()
    assert first.outcome is expected
    assert instance.next_observation_sequence == 1
    orchestrator.observation = ObservationCycleResult(OrchestrationOutcome.STORED)
    second = instance.run_due_opportunity()
    assert second.observation_sequence == 1
    assert second.outcome is SchedulerOutcome.STORED


def test_stale_sequence_refreshes_from_authoritative_checkpoint_next_cycle() -> None:
    clock = _Clock(0.0, 0.0, 30.0)
    instance, store, orchestrator = _scheduler(
        monotonic=cast(Callable[[], float], clock),
        observation=ObservationCycleResult(OrchestrationOutcome.STALE_SEQUENCE),
    )
    assert instance.run_due_opportunity().outcome is SchedulerOutcome.STALE_SEQUENCE
    store.resume = _resume(5, count=5)
    orchestrator.observation = ObservationCycleResult(OrchestrationOutcome.STORED)
    second = instance.run_due_opportunity()
    assert second.outcome is SchedulerOutcome.STORED
    assert second.observation_sequence == 6


def test_checkpoint_regression_relative_to_scheduler_closes_store() -> None:
    clock = _Clock(0.0, 0.0, 30.0)
    instance, store, orchestrator = _scheduler(
        resume=_resume(5, count=5),
        monotonic=cast(Callable[[], float], clock),
    )
    assert instance.run_due_opportunity().outcome is SchedulerOutcome.STORED
    store.resume = _resume(4, count=4)
    result = instance.run_due_opportunity()
    assert result.outcome is SchedulerOutcome.TRUST_FAILED
    assert store.closed
    assert orchestrator.observation_calls == 1


def test_refresh_to_maximum_sequence_exhausts_before_collection() -> None:
    supplier_calls = 0

    def supplier() -> HealthReport:
        nonlocal supplier_calls
        supplier_calls += 1
        return _report()

    instance, store, orchestrator = _scheduler(
        resume=_resume(MAX_OBSERVATION_SEQUENCE - 1),
        supplier=supplier,
    )
    store.resume = _resume(MAX_OBSERVATION_SEQUENCE)
    result = instance.run_due_opportunity()
    assert result.outcome is SchedulerOutcome.SEQUENCE_EXHAUSTED
    assert supplier_calls == 0
    assert orchestrator.observation_calls == 0
    assert instance.next_observation_sequence is None


@pytest.mark.parametrize(
    ("now", "missed", "next_due"),
    [
        (0.0, 0, 30.0),
        (30.0, 1, 60.0),
        (59.0, 1, 60.0),
        (60.0, 2, 90.0),
        (95.0, 3, 120.0),
    ],
)
def test_cadence_missed_formula_and_future_anchor(
    now: float,
    missed: int,
    next_due: float,
) -> None:
    state = SchedulerCadenceState(0.0, 0.0)
    decision = state.decision(now, sample_interval_seconds=30)
    assert decision == SchedulerDueDecision(True, missed, now, next_due)
    advanced = state.after_check(decision, consume_due=True)
    assert advanced.planned_due_monotonic == next_due


def test_cadence_not_due_before_next_anchor() -> None:
    state = SchedulerCadenceState(30.0, 0.0)
    decision = state.decision(29.999, sample_interval_seconds=30)
    assert decision == SchedulerDueDecision(False, 0, 29.999, 30.0)
    checked = state.after_check(decision, consume_due=False)
    assert checked.planned_due_monotonic == 30.0
    assert checked.last_seen_monotonic == 29.999


def test_very_late_cadence_saturates_without_loop() -> None:
    state = SchedulerCadenceState(0.0, 0.0)
    now = float((MAX_BOUNDED_COUNTER + 100) * 30)
    decision = state.decision(now, sample_interval_seconds=30)
    assert decision.missed_intervals == MAX_BOUNDED_COUNTER
    assert decision.next_due_monotonic == now + 30.0


def test_repeated_call_at_same_monotonic_does_not_resample() -> None:
    clock = _Clock(0.0, 0.0, 0.0)
    instance, _store, orchestrator = _scheduler(
        monotonic=cast(Callable[[], float], clock)
    )
    assert instance.run_due_opportunity().outcome is SchedulerOutcome.STORED
    assert instance.run_due_opportunity().outcome is SchedulerOutcome.NOT_DUE
    assert orchestrator.observation_calls == 1


def test_is_due_does_not_consume_the_due_opportunity() -> None:
    clock = _Clock(0.0, 0.0, 0.0)
    instance, _store, orchestrator = _scheduler(
        monotonic=cast(Callable[[], float], clock)
    )
    assert instance.is_due().due
    assert instance.run_due_opportunity().outcome is SchedulerOutcome.STORED
    assert orchestrator.observation_calls == 1


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, -1.0, 1, True])
def test_invalid_monotonic_run_preserves_state_and_calls_nothing(value: object) -> None:
    clock = _Clock(0.0, value)
    supplier_calls = 0

    def supplier() -> HealthReport:
        nonlocal supplier_calls
        supplier_calls += 1
        return _report()

    instance, store, orchestrator = _scheduler(
        monotonic=cast(Callable[[], float], clock),
        supplier=supplier,
    )
    before = instance.cadence_state
    result = instance.run_due_opportunity()
    assert result.outcome is SchedulerOutcome.INVALID_CLOCK
    assert instance.cadence_state is before
    assert supplier_calls == 0
    assert orchestrator.observation_calls == 0
    assert store.calls == ["resume"]


def test_monotonic_regression_is_rejected_without_state_change() -> None:
    clock = _Clock(100.0, 90.0)
    instance, _store, orchestrator = _scheduler(
        monotonic=cast(Callable[[], float], clock)
    )
    before = instance.cadence_state
    assert instance.run_due_opportunity().outcome is SchedulerOutcome.INVALID_CLOCK
    assert instance.cadence_state is before
    assert orchestrator.observation_calls == 0


def test_cadence_models_reject_impossible_state_and_transitions() -> None:
    with pytest.raises(ValueError, match="invalid_scheduler_due_decision"):
        SchedulerDueDecision(False, 1, 0.0, 1.0)
    with pytest.raises(ValueError, match="invalid_scheduler_cadence_state"):
        SchedulerCadenceState(-1.0, 0.0)
    state = SchedulerCadenceState(30.0, 0.0)
    not_due = state.decision(1.0, sample_interval_seconds=30)
    with pytest.raises(SchedulerError) as caught:
        state.after_check(not_due, consume_due=True)
    assert caught.value.reason is SchedulerRejection.INVALID_STATE
    with pytest.raises(SchedulerError) as caught:
        state.after_check(not_due, consume_due=False, pending_missed_intervals=-1)
    assert caught.value.reason is SchedulerRejection.INVALID_STATE
    fabricated = SchedulerDueDecision(True, 0, 1.0, 2.0)
    with pytest.raises(SchedulerError) as caught:
        SchedulerCadenceState(30.0, 0.0).after_check(
            fabricated,
            consume_due=False,
        )
    assert caught.value.reason is SchedulerRejection.INVALID_STATE


def test_cadence_rejects_when_no_future_anchor_is_representable() -> None:
    state = SchedulerCadenceState(0.0, 0.0)
    with pytest.raises(SchedulerError) as caught:
        state.decision(
            float(scheduler_module.MAX_TIMESTAMP_US),
            sample_interval_seconds=300,
        )
    assert caught.value.reason is SchedulerRejection.INVALID_CLOCK


@pytest.mark.parametrize(
    ("interval", "refresh"),
    [
        (MIN_SAMPLE_INTERVAL_SECONDS - 1, 1),
        (MAX_SAMPLE_INTERVAL_SECONDS + 1, 1),
        (30, 31),
        (30, 0),
        (True, 1),
        (30, True),
    ],
)
def test_scheduler_interval_validation(interval: object, refresh: object) -> None:
    with pytest.raises(SchedulerError) as caught:
        _scheduler(interval=interval, refresh=refresh)  # type: ignore[arg-type]
    assert caught.value.reason is SchedulerRejection.INVALID_CONFIGURATION


def test_interval_boundaries_are_accepted() -> None:
    for interval in (MIN_SAMPLE_INTERVAL_SECONDS, MAX_SAMPLE_INTERVAL_SECONDS):
        instance, _store, _orchestrator = _scheduler(
            interval=interval,
            refresh=interval,
        )
        assert instance.cadence_state.planned_due_monotonic == 0.0


def test_constructor_rejects_noncallable_dependency_and_initial_clock_failure() -> None:
    store = _FakeStore(_empty_resume())
    orchestrator = _FakeOrchestrator(store)
    with pytest.raises(SchedulerError) as caught:
        HealthHistoryScheduler(
            cast(HealthHistoryStore, store),
            cast(HealthHistoryOrchestrator, orchestrator),
            health_report_supplier=cast(Callable[[], HealthReport], None),
            monotonic=lambda: 0.0,
            utc_now=lambda: _NOW,
        )
    assert caught.value.reason is SchedulerRejection.INVALID_CONFIGURATION

    def fail_clock() -> float:
        raise RuntimeError("private clock detail")

    with pytest.raises(SchedulerError) as caught:
        HealthHistoryScheduler(
            cast(HealthHistoryStore, store),
            cast(HealthHistoryOrchestrator, orchestrator),
            health_report_supplier=_report,
            monotonic=fail_clock,
            utc_now=lambda: _NOW,
        )
    assert caught.value.reason is SchedulerRejection.INVALID_CLOCK


def test_empty_database_first_observation_is_startup_marker_without_gap() -> None:
    instance, _store, orchestrator = _scheduler()
    result = instance.run_due_opportunity()
    assert result.outcome is SchedulerOutcome.STORED
    projection = orchestrator.projections[0]
    assert projection.sample_kind is SampleKind.STARTUP_GAP
    assert projection.missed_intervals == 0


@pytest.mark.parametrize(
    ("now_monotonic", "missed"),
    [(0.0, 0), (30.0, 1), (60.0, 2)],
)
def test_restart_startup_marker_uses_only_monotonic_misses(
    now_monotonic: float,
    missed: int,
) -> None:
    clock = _Clock(0.0, now_monotonic)
    instance, _store, orchestrator = _scheduler(
        resume=_resume(10),
        monotonic=cast(Callable[[], float], clock),
        utc_now=lambda: _NOW + timedelta(seconds=10_000),
    )
    assert instance.run_due_opportunity().outcome is SchedulerOutcome.STORED
    projection = orchestrator.projections[0]
    assert projection.sample_kind is SampleKind.STARTUP_GAP
    assert projection.missed_intervals == missed


def test_very_long_restart_monotonic_delay_saturates_startup_misses() -> None:
    late = float((MAX_BOUNDED_COUNTER + 50) * 30)
    clock = _Clock(0.0, late)
    instance, _store, orchestrator = _scheduler(
        resume=_resume(10),
        monotonic=cast(Callable[[], float], clock),
    )
    instance.run_due_opportunity()
    projection = orchestrator.projections[0]
    assert projection.sample_kind is SampleKind.STARTUP_GAP
    assert projection.missed_intervals == MAX_BOUNDED_COUNTER


def test_utc_restart_duration_does_not_fabricate_misses() -> None:
    clock = _Clock(0.0, 0.0)
    instance, _store, orchestrator = _scheduler(
        resume=_resume(10, observed_at=1),
        monotonic=cast(Callable[[], float], clock),
        utc_now=lambda: _NOW + timedelta(days=365),
    )
    instance.run_due_opportunity()
    assert orchestrator.projections[0].missed_intervals == 0


def test_backward_utc_selects_clock_marker_and_zero_misses() -> None:
    clock = _Clock(0.0, 60.0)
    future = _NOW_US + 1_000_000
    instance, _store, orchestrator = _scheduler(
        resume=_resume(10, observed_at=future),
        monotonic=cast(Callable[[], float], clock),
        utc_now=lambda: _NOW,
    )
    assert instance.run_due_opportunity().outcome is SchedulerOutcome.STORED
    projection = orchestrator.projections[0]
    assert projection.sample_kind is SampleKind.CLOCK_DISCONTINUITY
    assert projection.missed_intervals == 0
    assert instance.cadence_state.pending_missed_intervals == 2


def test_clock_marker_preserves_monotonic_misses_for_next_ordinary_sample() -> None:
    clock = _Clock(0.0, 60.0, 90.0)
    instance, _store, orchestrator = _scheduler(
        resume=_resume(10, observed_at=_NOW_US + 1_000_000),
        monotonic=cast(Callable[[], float], clock),
        utc_now=lambda: _NOW,
    )
    assert instance.run_due_opportunity().outcome is SchedulerOutcome.STORED
    assert instance.run_due_opportunity().outcome is SchedulerOutcome.STORED
    assert [item.sample_kind for item in orchestrator.projections] == [
        SampleKind.CLOCK_DISCONTINUITY,
        SampleKind.HEARTBEAT,
    ]
    assert [item.missed_intervals for item in orchestrator.projections] == [0, 2]
    assert instance.cadence_state.pending_missed_intervals == 0


@pytest.mark.parametrize("utc_value", [None, _NOW.replace(tzinfo=None), "bad"])
def test_invalid_utc_consumes_slot_without_collection(utc_value: object) -> None:
    supplier_calls = 0

    def supplier() -> HealthReport:
        nonlocal supplier_calls
        supplier_calls += 1
        return _report()

    instance, _store, orchestrator = _scheduler(
        utc_now=cast(Callable[[], datetime], lambda: utc_value),
        supplier=supplier,
    )
    result = instance.run_due_opportunity()
    assert result.outcome is SchedulerOutcome.INVALID_CLOCK
    assert supplier_calls == 0
    assert orchestrator.observation_calls == 0
    assert instance.next_observation_sequence == 1
    assert instance.cadence_state.pending_missed_intervals == 1


def test_utc_supplier_exception_is_sanitized() -> None:
    def fail() -> datetime:
        raise RuntimeError("private UTC detail")

    instance, _store, orchestrator = _scheduler(utc_now=fail)
    result = instance.run_due_opportunity()
    assert result.outcome is SchedulerOutcome.INVALID_CLOCK
    assert orchestrator.observation_calls == 0
    assert "private" not in str(result)


def test_collection_projection_orchestration_order_is_fixed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def supplier() -> HealthReport:
        events.append("collection")
        return _report()

    original = scheduler_module.project_health_report

    def tracked_projection(*args: object, **kwargs: object) -> HealthProjection:
        events.append("projection")
        return original(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(scheduler_module, "project_health_report", tracked_projection)
    instance, _store, orchestrator = _scheduler(
        supplier=supplier,
        events=events,
    )
    assert instance.run_due_opportunity().outcome is SchedulerOutcome.STORED
    assert events == ["collection", "projection", "orchestration", "trigger"]
    assert orchestrator.observation_calls == 1


def test_collection_failure_is_sanitized_once_and_carries_one_miss() -> None:
    calls = 0

    def supplier() -> HealthReport:
        nonlocal calls
        calls += 1
        raise RuntimeError("private device detail")

    clock = _Clock(0.0, 0.0, 30.0)
    instance, _store, orchestrator = _scheduler(
        monotonic=cast(Callable[[], float], clock),
        supplier=supplier,
    )
    first = instance.run_due_opportunity()
    assert first.outcome is SchedulerOutcome.COLLECTION_FAILED
    assert str(first) not in {"private device detail"}
    assert calls == 1
    assert orchestrator.observation_calls == 0
    assert instance.next_observation_sequence == 1
    instance._health_report_supplier = _report
    assert instance.run_due_opportunity().outcome is SchedulerOutcome.STORED
    assert orchestrator.projections[0].missed_intervals == 1


def test_projection_failure_is_sanitized_and_not_orchestrated() -> None:
    invalid = HealthReport(
        status=HealthStatus.HEALTHY,
        checked_at=_NOW.isoformat(),
        service_uptime_seconds=1.0,
        components=(),
    )
    clock = _Clock(0.0, 0.0, 30.0)
    instance, _store, orchestrator = _scheduler(
        monotonic=cast(Callable[[], float], clock),
        supplier=lambda: invalid,
    )
    first = instance.run_due_opportunity()
    assert first.outcome is SchedulerOutcome.PROJECTION_FAILED
    assert orchestrator.observation_calls == 0
    assert instance.next_observation_sequence == 1
    instance._health_report_supplier = _report
    instance.run_due_opportunity()
    assert orchestrator.projections[0].missed_intervals == 1


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("collection", SchedulerOutcome.COLLECTION_FAILED),
        ("projection", SchedulerOutcome.PROJECTION_FAILED),
    ],
)
def test_pre_orchestration_failures_may_still_run_one_due_maintenance(
    failure: str,
    expected: SchedulerOutcome,
) -> None:
    if failure == "collection":

        def supplier() -> HealthReport:
            raise RuntimeError("private")

    else:
        invalid = HealthReport(
            status=HealthStatus.HEALTHY,
            checked_at=_NOW.isoformat(),
            service_uptime_seconds=1.0,
            components=(),
        )

        def supplier() -> HealthReport:
            return invalid

    instance, _store, orchestrator = _scheduler(
        supplier=supplier,
        trigger=MaintenanceTriggerDecision(
            True,
            MaintenanceTriggerReason.STARTUP,
        ),
    )
    result = instance.run_due_opportunity()
    assert result.outcome is expected
    assert result.maintenance_outcome is OrchestrationOutcome.MAINTENANCE_COMPLETED
    assert orchestrator.observation_calls == 0
    assert orchestrator.trigger_calls == 1
    assert orchestrator.maintenance_calls == 1
    assert instance.next_observation_sequence == 1


@pytest.mark.parametrize(
    "outcome",
    [
        OrchestrationOutcome.CAPACITY_BLOCKED,
        OrchestrationOutcome.CHECKPOINT_BUSY,
        OrchestrationOutcome.CHECKPOINT_INCOMPLETE,
        OrchestrationOutcome.STORAGE_BUSY,
        OrchestrationOutcome.TIMED_OUT,
        OrchestrationOutcome.PERSISTENCE_FAILED,
        OrchestrationOutcome.INVALID_OBSERVATION,
        OrchestrationOutcome.STALE_SEQUENCE,
        OrchestrationOutcome.SEQUENCE_CONFLICT,
        OrchestrationOutcome.GENERATION_EXHAUSTED,
        OrchestrationOutcome.WAL_OVERSIZE_BLOCKED,
        OrchestrationOutcome.UNSUPPORTED_RUNTIME,
        OrchestrationOutcome.INVALID_CLOCK,
        OrchestrationOutcome.REENTRANT,
        OrchestrationOutcome.TRUST_FAILED,
    ],
)
def test_every_unaccepted_orchestration_result_stops_before_maintenance(
    outcome: OrchestrationOutcome,
) -> None:
    due = MaintenanceTriggerDecision(True, MaintenanceTriggerReason.STARTUP)
    instance, _store, orchestrator = _scheduler(
        observation=ObservationCycleResult(outcome),
        trigger=due,
    )
    result = instance.run_due_opportunity()
    assert result.outcome is SchedulerOutcome(outcome.value)
    assert orchestrator.observation_calls == 1
    assert orchestrator.trigger_calls == 0
    assert orchestrator.maintenance_calls == 0


@pytest.mark.parametrize("observation", [RuntimeError("private"), object()])
def test_invalid_or_raising_orchestrator_fails_closed(
    observation: BaseException | object,
) -> None:
    instance, _store, orchestrator = _scheduler(observation=observation)
    result = instance.run_due_opportunity()
    assert result.outcome is SchedulerOutcome.TRUST_FAILED
    assert orchestrator.observation_calls == 1
    assert orchestrator.trigger_calls == 0


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        (OrchestrationOutcome.STORED, MaintenanceTriggerReason.STARTUP),
        (OrchestrationOutcome.STATE_ONLY, MaintenanceTriggerReason.HOURLY),
        (OrchestrationOutcome.REPLAYED, MaintenanceTriggerReason.STORED_ROWS),
    ],
)
def test_each_accepted_observation_may_run_one_due_maintenance(
    outcome: OrchestrationOutcome,
    reason: MaintenanceTriggerReason,
) -> None:
    events: list[str] = []
    due = MaintenanceTriggerDecision(True, reason)
    instance, _store, orchestrator = _scheduler(
        observation=ObservationCycleResult(outcome),
        trigger=due,
        events=events,
    )
    result = instance.run_due_opportunity()
    assert result.outcome is SchedulerOutcome(outcome.value)
    assert result.maintenance_outcome is OrchestrationOutcome.MAINTENANCE_COMPLETED
    assert events == ["orchestration", "trigger", "maintenance"]
    assert orchestrator.observation_calls == 1
    assert orchestrator.trigger_calls == 1
    assert orchestrator.maintenance_calls == 1


@pytest.mark.parametrize(
    "outcome",
    [
        OrchestrationOutcome.STORED,
        OrchestrationOutcome.STATE_ONLY,
        OrchestrationOutcome.REPLAYED,
    ],
)
@pytest.mark.parametrize("hazard", ["capacity", "checkpoint"])
def test_accepted_observation_with_storage_maintenance_skips_scheduled_maintenance(
    outcome: OrchestrationOutcome,
    hazard: str,
) -> None:
    store = _FakeStore(_empty_resume())
    orchestrator = _RetryHazardOrchestrator(
        store,
        observation_outcome=outcome,
        hazard=hazard,
        storage_maintenance_attempted=True,
    )
    instance = HealthHistoryScheduler(
        cast(HealthHistoryStore, store),
        cast(HealthHistoryOrchestrator, orchestrator),
        health_report_supplier=_report,
        monotonic=cast(Callable[[], float], _Clock(0.0, 0.0)),
        utc_now=lambda: _NOW,
    )
    result = instance.run_due_opportunity()
    assert result.outcome is SchedulerOutcome(outcome.value)
    assert orchestrator.observation_calls == 1
    assert orchestrator.trigger_calls == 0
    assert orchestrator.maintenance_calls == 0
    if hazard == "checkpoint":
        assert orchestrator.passive_calls == 1
        assert orchestrator.cleanup_calls == 0
        assert orchestrator.vacuum_calls == 0
    else:
        assert orchestrator.passive_calls == 0
        assert orchestrator.cleanup_calls == 1
        assert orchestrator.vacuum_calls == 1


def test_real_capacity_recovery_cannot_start_a_second_maintenance_phase() -> None:
    store = _composed_store()
    store.inspect_storage_capacity.side_effect = [_capacity(full=True), _capacity()]
    scheduler, orchestrator = _composed_scheduler(store)
    result = scheduler.run_due_opportunity()
    assert result.outcome is SchedulerOutcome.STORED
    assert result.maintenance_outcome is None
    assert store.cleanup_retention.call_count == 1
    assert store.incremental_vacuum.call_count == 1
    assert store.passive_wal_checkpoint.call_count == 0
    assert store.inspect_storage_capacity.call_count == 2
    assert orchestrator.trigger_state.startup_maintenance_completed is False
    assert orchestrator.trigger_state.stored_rows_since_maintenance == 1


def test_real_checkpoint_recovery_cannot_start_a_second_maintenance_phase() -> None:
    store = _composed_store()
    store.inspect_wal.side_effect = [
        _wal(storage_envelope.WAL_CHECKPOINT_THRESHOLD_FRAMES),
        _wal(),
    ]
    scheduler, orchestrator = _composed_scheduler(store)
    result = scheduler.run_due_opportunity()
    assert result.outcome is SchedulerOutcome.STORED
    assert result.maintenance_outcome is None
    assert store.passive_wal_checkpoint.call_count == 1
    assert store.cleanup_retention.call_count == 0
    assert store.incremental_vacuum.call_count == 0
    assert orchestrator.trigger_state.startup_maintenance_completed is False
    assert orchestrator.trigger_state.stored_rows_since_maintenance == 1


def test_real_direct_acceptance_remains_eligible_for_scheduled_maintenance() -> None:
    store = _composed_store()
    scheduler, orchestrator = _composed_scheduler(store)
    result = scheduler.run_due_opportunity()
    assert result.outcome is SchedulerOutcome.STORED
    assert result.maintenance_outcome is OrchestrationOutcome.MAINTENANCE_COMPLETED
    assert store.cleanup_retention.call_count == 1
    assert store.incremental_vacuum.call_count == 1
    assert store.passive_wal_checkpoint.call_count == 0
    assert store.inspect_storage_capacity.call_count == 1
    assert orchestrator.trigger_state.startup_maintenance_completed is True
    assert orchestrator.trigger_state.stored_rows_since_maintenance == 0


@pytest.mark.parametrize(
    "outcome",
    [
        OrchestrationOutcome.CHECKPOINT_BUSY,
        OrchestrationOutcome.CHECKPOINT_INCOMPLETE,
        OrchestrationOutcome.TIMED_OUT,
    ],
)
def test_checkpoint_failure_cannot_make_a_second_passive_attempt(
    outcome: OrchestrationOutcome,
) -> None:
    store = _FakeStore(_empty_resume())
    orchestrator = _RetryHazardOrchestrator(
        store,
        observation_outcome=outcome,
        hazard="checkpoint",
    )
    instance = HealthHistoryScheduler(
        cast(HealthHistoryStore, store),
        cast(HealthHistoryOrchestrator, orchestrator),
        health_report_supplier=_report,
        monotonic=cast(Callable[[], float], _Clock(0.0, 0.0)),
        utc_now=lambda: _NOW,
    )
    result = instance.run_due_opportunity()
    assert result.outcome is SchedulerOutcome(outcome.value)
    assert orchestrator.passive_calls == 1
    assert orchestrator.trigger_calls == 0
    assert orchestrator.maintenance_calls == 0


def test_capacity_block_cannot_make_a_second_cleanup_or_vacuum_attempt() -> None:
    store = _FakeStore(_empty_resume())
    orchestrator = _RetryHazardOrchestrator(
        store,
        observation_outcome=OrchestrationOutcome.CAPACITY_BLOCKED,
        hazard="capacity",
    )
    instance = HealthHistoryScheduler(
        cast(HealthHistoryStore, store),
        cast(HealthHistoryOrchestrator, orchestrator),
        health_report_supplier=_report,
        monotonic=cast(Callable[[], float], _Clock(0.0, 0.0)),
        utc_now=lambda: _NOW,
    )
    result = instance.run_due_opportunity()
    assert result.outcome is SchedulerOutcome.CAPACITY_BLOCKED
    assert orchestrator.cleanup_calls == 1
    assert orchestrator.vacuum_calls == 1
    assert orchestrator.trigger_calls == 0
    assert orchestrator.maintenance_calls == 0


def test_maintenance_not_due_calls_no_maintenance() -> None:
    instance, _store, orchestrator = _scheduler()
    instance.run_due_opportunity()
    assert orchestrator.trigger_calls == 1
    assert orchestrator.maintenance_calls == 0


@pytest.mark.parametrize(
    ("trigger", "expected"),
    [
        (
            OrchestrationError(OrchestrationRejection.REENTRANT),
            SchedulerOutcome.REENTRANT,
        ),
        (
            OrchestrationError(OrchestrationRejection.INVALID_MONOTONIC),
            SchedulerOutcome.INVALID_CLOCK,
        ),
        (
            OrchestrationError(OrchestrationRejection.INVALID_TRIGGER_STATE),
            SchedulerOutcome.TRUST_FAILED,
        ),
        (RuntimeError("private"), SchedulerOutcome.TRUST_FAILED),
        (object(), SchedulerOutcome.TRUST_FAILED),
    ],
)
def test_trigger_contract_failures_are_sanitized(
    trigger: MaintenanceTriggerDecision | BaseException | object,
    expected: SchedulerOutcome,
) -> None:
    instance, _store, orchestrator = _scheduler(trigger=trigger)
    result = instance.run_due_opportunity()
    assert result.outcome is expected
    assert result.sampling_outcome is SchedulerOutcome.STORED
    assert orchestrator.trigger_calls == 1
    assert orchestrator.maintenance_calls == 0


@pytest.mark.parametrize(
    ("maintenance", "expected"),
    [
        (
            OrchestrationOutcome.STORAGE_BUSY,
            SchedulerOutcome.STORAGE_BUSY,
        ),
        (OrchestrationOutcome.TIMED_OUT, SchedulerOutcome.TIMED_OUT),
        (OrchestrationOutcome.TRUST_FAILED, SchedulerOutcome.TRUST_FAILED),
    ],
)
def test_maintenance_failures_are_sanitized_without_retry(
    maintenance: OrchestrationOutcome,
    expected: SchedulerOutcome,
) -> None:
    due = MaintenanceTriggerDecision(True, MaintenanceTriggerReason.STARTUP)
    instance, _store, orchestrator = _scheduler(
        trigger=due,
        maintenance=MaintenanceOpportunityResult(maintenance),
    )
    result = instance.run_due_opportunity()
    assert result.outcome is expected
    assert result.sampling_outcome is SchedulerOutcome.STORED
    assert result.maintenance_outcome is maintenance
    assert orchestrator.maintenance_calls == 1


@pytest.mark.parametrize("maintenance", [RuntimeError("private"), object()])
def test_maintenance_contract_failure_is_sanitized(
    maintenance: BaseException | object,
) -> None:
    instance, _store, orchestrator = _scheduler(
        trigger=MaintenanceTriggerDecision(True, MaintenanceTriggerReason.STARTUP),
        maintenance=maintenance,
    )
    result = instance.run_due_opportunity()
    assert result.outcome is SchedulerOutcome.TRUST_FAILED
    assert result.sampling_outcome is SchedulerOutcome.STORED
    assert orchestrator.maintenance_calls == 1


def test_guard_restores_after_maintenance_failure() -> None:
    clock = _Clock(0.0, 0.0, 30.0)
    instance, _store, orchestrator = _scheduler(
        monotonic=cast(Callable[[], float], clock),
        trigger=MaintenanceTriggerDecision(True, MaintenanceTriggerReason.STARTUP),
        maintenance=MaintenanceOpportunityResult(OrchestrationOutcome.STORAGE_BUSY),
    )
    assert instance.run_due_opportunity().outcome is SchedulerOutcome.STORAGE_BUSY
    orchestrator.maintenance = MaintenanceOpportunityResult(
        OrchestrationOutcome.MAINTENANCE_COMPLETED
    )
    assert instance.run_due_opportunity().outcome is SchedulerOutcome.STORED
    assert orchestrator.observation_calls == 2
    assert orchestrator.maintenance_calls == 2


def test_reentrant_scheduler_call_is_nonblocking_and_guard_restores() -> None:
    nested: list[SchedulerResult] = []
    holder: list[HealthHistoryScheduler] = []

    def supplier() -> HealthReport:
        nested.append(holder[0].run_due_opportunity())
        return _report()

    clock = _Clock(0.0, 0.0, 30.0)
    instance, _store, orchestrator = _scheduler(
        monotonic=cast(Callable[[], float], clock),
        supplier=supplier,
    )
    holder.append(instance)
    assert instance.run_due_opportunity().outcome is SchedulerOutcome.STORED
    assert nested == [SchedulerResult(SchedulerOutcome.REENTRANT)]
    assert instance.run_due_opportunity().outcome is SchedulerOutcome.STORED
    assert orchestrator.observation_calls == 2


def test_concurrent_scheduler_invocation_is_rejected() -> None:
    entered = threading.Event()
    release = threading.Event()

    def supplier() -> HealthReport:
        entered.set()
        assert release.wait(timeout=2.0)
        return _report()

    clock = _Clock(0.0, 0.0, 0.0)
    instance, _store, _orchestrator = _scheduler(
        monotonic=cast(Callable[[], float], clock),
        supplier=supplier,
    )
    completed: list[SchedulerResult] = []
    thread = threading.Thread(
        target=lambda: completed.append(instance.run_due_opportunity()),
        daemon=True,
    )
    thread.start()
    assert entered.wait(timeout=1.0)
    assert instance.run_due_opportunity().outcome is SchedulerOutcome.REENTRANT
    release.set()
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert completed[0].outcome is SchedulerOutcome.STORED


def test_is_due_reentrancy_is_rejected_without_a_clock_read() -> None:
    nested: list[SchedulerRejection] = []
    holder: list[HealthHistoryScheduler] = []

    def supplier() -> HealthReport:
        with pytest.raises(SchedulerError) as caught:
            holder[0].is_due()
        nested.append(caught.value.reason)
        return _report()

    clock = _Clock(0.0, 0.0)
    instance, _store, _orchestrator = _scheduler(
        monotonic=cast(Callable[[], float], clock),
        supplier=supplier,
    )
    holder.append(instance)
    assert instance.run_due_opportunity().outcome is SchedulerOutcome.STORED
    assert nested == [SchedulerRejection.REENTRANT]
    assert clock.calls == 2


@pytest.mark.parametrize("failure", ["collection", "projection", "clock"])
def test_guard_restores_after_scheduler_failures(failure: str) -> None:
    clock_values: tuple[object, ...] = (0.0, 0.0, 30.0)
    supplier: Callable[[], HealthReport] = _report
    if failure == "collection":

        def supplier() -> HealthReport:
            raise RuntimeError("private")

    elif failure == "projection":

        def supplier() -> HealthReport:
            return cast(HealthReport, object())

    elif failure == "clock":
        clock_values = (0.0, math.nan, 30.0)
    clock = _Clock(*clock_values)
    instance, _store, orchestrator = _scheduler(
        monotonic=cast(Callable[[], float], clock),
        supplier=supplier,
    )
    instance.run_due_opportunity()
    instance._health_report_supplier = _report
    assert instance.run_due_opportunity().outcome is SchedulerOutcome.STORED
    assert orchestrator.observation_calls == 1


def test_scheduler_constructor_sanitizes_resume_failures() -> None:
    error = QueryError(QueryRejection.STORAGE_BUSY)
    with pytest.raises(SchedulerError) as caught:
        _scheduler(resume=error)
    assert caught.value.reason is SchedulerRejection.STORAGE_BUSY
    assert str(caught.value) == "storage_busy"
    assert caught.value.__cause__ is None


@pytest.mark.parametrize(
    "resume",
    [
        QueryError(QueryRejection.INVALID_QUERY),
        StoreError("private"),
        object(),
    ],
)
def test_scheduler_constructor_closes_on_invalid_resume_contract(
    resume: BaseException | object,
) -> None:
    with pytest.raises(SchedulerError) as caught:
        _scheduler(resume=resume)
    assert caught.value.reason is SchedulerRejection.TRUST_FAILED


def test_scheduler_refresh_failure_consumes_slot_without_collection() -> None:
    instance, store, orchestrator = _scheduler()
    store.resume = QueryError(QueryRejection.PERSISTENCE_FAILED)
    result = instance.run_due_opportunity()
    assert result.outcome is SchedulerOutcome.PERSISTENCE_FAILED
    assert orchestrator.observation_calls == 0
    assert instance.cadence_state.pending_missed_intervals == 1


def test_real_store_restart_resumes_next_sequence(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    orchestrator = HealthHistoryOrchestrator(
        store,
        monotonic=lambda: 100.0,
        utc_now_us=lambda: _NOW_US + 1_000_000,
    )
    first = HealthHistoryScheduler(
        store,
        orchestrator,
        health_report_supplier=_report,
        monotonic=cast(Callable[[], float], _Clock(0.0, 0.0)),
        utc_now=lambda: _NOW + timedelta(seconds=1),
    )
    assert first.run_due_opportunity().outcome is SchedulerOutcome.STORED
    assert store.get_scheduler_resume_state().last_committed_sequence == 1
    store.close()

    reopened = HealthHistoryStore.open_existing(path)
    try:
        resumed_orchestrator = HealthHistoryOrchestrator(
            reopened,
            monotonic=lambda: 200.0,
            utc_now_us=lambda: _NOW_US + 31_000_000,
        )
        resumed = HealthHistoryScheduler(
            reopened,
            resumed_orchestrator,
            health_report_supplier=lambda: _report(_NOW + timedelta(seconds=30)),
            monotonic=cast(Callable[[], float], _Clock(0.0, 0.0)),
            utc_now=lambda: _NOW + timedelta(seconds=31),
        )
        assert resumed.next_observation_sequence == 2
        assert resumed.run_due_opportunity().outcome is SchedulerOutcome.STORED
        state = reopened.get_scheduler_resume_state()
        assert state.last_committed_sequence == 2
        assert state.accepted_observation_count == 2
    finally:
        reopened.close()


def test_real_clock_discontinuity_does_not_open_sampling_gap(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    _path, store = store_path
    _seed(store)
    orchestrator = HealthHistoryOrchestrator(
        store,
        monotonic=lambda: 100.0,
        utc_now_us=lambda: _NOW_US - 1,
    )
    instance = HealthHistoryScheduler(
        store,
        orchestrator,
        health_report_supplier=lambda: _report(_NOW - timedelta(seconds=1)),
        monotonic=cast(Callable[[], float], _Clock(0.0, 60.0)),
        utc_now=lambda: _NOW - timedelta(microseconds=1),
    )
    assert instance.run_due_opportunity().outcome is SchedulerOutcome.STORED
    connection = _connect(_path)
    try:
        gap = connection.execute(
            "SELECT gap_phase, consecutive_count FROM evaluation_state "
            "WHERE scope = 'sampling'"
        ).fetchone()
        sample = connection.execute(
            "SELECT accepted_sample_kind, missed_intervals FROM health_samples "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        connection.close()
    assert gap == ("clear", 0)
    assert sample == ("clock_discontinuity", 0)


def test_runtime_entry_points_do_not_import_scheduler() -> None:
    root = Path(__file__).parents[1] / "src" / "aurora_core"
    paths = [
        root / "__main__.py",
        root / "dashboard" / "server.py",
        root / "dashboard" / "service.py",
        *sorted((root / "runtime").glob("*.py")),
    ]
    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert "health_history.scheduler" not in source
        assert "HealthHistoryScheduler" not in source


def test_scheduler_source_has_no_runtime_or_external_behavior() -> None:
    source = Path(scheduler_module.__file__).read_text(encoding="utf-8")
    prohibited = (
        "subprocess",
        "socket",
        "requests",
        "http.client",
        "Thread(",
        "create_task",
        "sleep(",
        "systemctl",
        "WLED",
        "HyperHDR",
        "DDP",
        "os.environ",
    )
    for text in prohibited:
        assert text not in source


def test_public_result_models_are_immutable_and_validate_bounds() -> None:
    result = SchedulerResult(SchedulerOutcome.NOT_DUE)
    with pytest.raises((AttributeError, TypeError)):
        result.outcome = SchedulerOutcome.STORED  # type: ignore[misc]
    with pytest.raises(ValueError, match="invalid_scheduler_result"):
        SchedulerResult(SchedulerOutcome.STORED, observation_sequence=0)
    with pytest.raises(ValueError, match="invalid_scheduler_result"):
        SchedulerResult(SchedulerOutcome.STORED, missed_intervals=-1)


def test_projection_error_text_never_escapes_scheduler_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object) -> HealthProjection:
        del args, kwargs
        raise ProjectionError(ProjectionRejection.INVALID_REPORT)

    monkeypatch.setattr(scheduler_module, "project_health_report", fail)
    instance, _store, orchestrator = _scheduler()
    result = instance.run_due_opportunity()
    assert result.outcome is SchedulerOutcome.PROJECTION_FAILED
    assert orchestrator.observation_calls == 0
    assert "invalid_report" not in str(result)
