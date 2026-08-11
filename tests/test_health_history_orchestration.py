"""Synthetic tests for the direct-only Milestone 18 orchestration core."""

from __future__ import annotations

import hashlib
import math
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

import aurora_core.health_history.storage_envelope as envelope
from aurora_core.health_history.ingestion import (
    IngestionError,
    IngestionOutcome,
    IngestionRejection,
    IngestionResult,
)
from aurora_core.health_history.maintenance import (
    INCREMENTAL_VACUUM_PAGES,
    RETENTION_ROW_BUDGET,
    IncrementalVacuumResult,
    MaintenanceError,
    MaintenanceOutcome,
    MaintenanceRejection,
    RetentionCleanupResult,
)
from aurora_core.health_history.models import (
    COMPONENT_ORDER,
    MAX_BOUNDED_COUNTER,
    MAX_DATABASE_BYTES,
    MAX_DATABASE_PAGES,
    PAGE_SIZE_BYTES,
    ComponentName,
    HealthHistoryStatus,
    SampleKind,
)
from aurora_core.health_history.orchestration import (
    MAINTENANCE_INTERVAL_SECONDS,
    STORED_ROWS_MAINTENANCE_TRIGGER,
    HealthHistoryOrchestrator,
    MaintenanceOpportunityResult,
    MaintenanceTriggerDecision,
    MaintenanceTriggerReason,
    MaintenanceTriggerState,
    ObservationCycleResult,
    OrchestrationError,
    OrchestrationOutcome,
    OrchestrationRejection,
)
from aurora_core.health_history.projection import (
    ComponentProjection,
    HealthProjection,
    _canonical_bytes,
)
from aurora_core.health_history.reasons import NormalizedReason
from aurora_core.health_history.storage_envelope import (
    FREE_SPACE_RESERVE_BYTES,
    WAL_CHECKPOINT_THRESHOLD_FRAMES,
    WAL_FRAME_BYTES,
    WAL_HARD_LIMIT_BYTES,
    WAL_HARD_LIMIT_FRAMES,
    WAL_HEADER_BYTES,
    FreeSpaceResult,
    PassiveCheckpointOutcome,
    PassiveCheckpointResult,
    StorageCapacityResult,
    StorageEnvelopeError,
    StorageEnvelopeRejection,
    WalInspectionResult,
)
from aurora_core.health_history.store import HealthHistoryStore, StoreError

_BASE_TIME = 1_786_000_000_000_000
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


def _capacity(*, full: bool = False) -> StorageCapacityResult:
    page_count = MAX_DATABASE_PAGES if full else 20
    return StorageCapacityResult(
        page_count=page_count,
        freelist_count=0,
        maximum_page_count=MAX_DATABASE_PAGES,
        used_bytes=page_count * PAGE_SIZE_BYTES,
        maximum_bytes=MAX_DATABASE_BYTES,
        pages_remaining=MAX_DATABASE_PAGES - page_count,
    )


def _free_space(*, sufficient: bool = True) -> FreeSpaceResult:
    free_bytes = FREE_SPACE_RESERVE_BYTES
    if not sufficient:
        free_bytes -= 1
    return FreeSpaceResult(
        sufficient=sufficient,
        free_bytes=free_bytes,
        required_reserve_bytes=FREE_SPACE_RESERVE_BYTES,
    )


def _wal(frames: int = 0, *, physical_bytes: int | None = None) -> WalInspectionResult:
    exists = frames > 0 or physical_bytes is not None
    slots = frames
    total_bytes = 0
    if exists:
        total_bytes = (
            physical_bytes
            if physical_bytes is not None
            else WAL_HEADER_BYTES + slots * WAL_FRAME_BYTES
        )
        if physical_bytes is not None:
            slots = (physical_bytes - WAL_HEADER_BYTES) // WAL_FRAME_BYTES
    oversize = frames > WAL_HARD_LIMIT_FRAMES or total_bytes > WAL_HARD_LIMIT_BYTES
    return WalInspectionResult(
        exists=exists,
        logical_frame_count=frames,
        physical_frame_slots=slots,
        total_bytes=total_bytes,
        checkpointed_frames=0,
        checkpoint_due=(
            WAL_CHECKPOINT_THRESHOLD_FRAMES <= frames <= WAL_HARD_LIMIT_FRAMES
            and not oversize
        ),
        oversize=oversize,
    )


def _checkpoint(
    outcome: PassiveCheckpointOutcome = PassiveCheckpointOutcome.COMPLETED,
) -> PassiveCheckpointResult:
    if outcome is PassiveCheckpointOutcome.NO_WORK:
        return PassiveCheckpointResult(outcome, 0, 0, False, 0, 0)
    if outcome is PassiveCheckpointOutcome.OVERSIZE_BLOCKED:
        frames = WAL_HARD_LIMIT_FRAMES + 1
        return PassiveCheckpointResult(
            outcome,
            frames,
            WAL_HEADER_BYTES + frames * WAL_FRAME_BYTES,
            False,
            0,
            0,
        )
    frames = WAL_CHECKPOINT_THRESHOLD_FRAMES
    return PassiveCheckpointResult(
        outcome,
        frames,
        WAL_HEADER_BYTES + frames * WAL_FRAME_BYTES,
        outcome is PassiveCheckpointOutcome.BUSY,
        0 if outcome is PassiveCheckpointOutcome.BUSY else frames,
        0,
    )


def _cleanup() -> RetentionCleanupResult:
    return RetentionCleanupResult(MaintenanceOutcome.NO_WORK, 0, 0, 0)


def _vacuum() -> IncrementalVacuumResult:
    return IncrementalVacuumResult(MaintenanceOutcome.NO_WORK, 0, 0, 0)


class _FakeStore:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.cleanup_times: list[int] = []
        self.responses: dict[str, list[object]] = {
            "capacity": [_capacity()],
            "free": [_free_space()],
            "wal": [_wal()],
            "cleanup": [_cleanup()],
            "vacuum": [_vacuum()],
            "checkpoint": [_checkpoint()],
            "ingest": [IngestionResult(IngestionOutcome.TRANSITION_STORED)],
        }
        self.on_capacity: Callable[[], None] | None = None
        self.on_cleanup: Callable[[], None] | None = None

    def _take(self, name: str) -> object:
        self.calls.append(name)
        values = self.responses[name]
        value = values.pop(0) if len(values) > 1 else values[0]
        if isinstance(value, BaseException):
            raise value
        return value

    def inspect_storage_capacity(self) -> StorageCapacityResult:
        if self.on_capacity is not None:
            callback, self.on_capacity = self.on_capacity, None
            callback()
        return cast(StorageCapacityResult, self._take("capacity"))

    def inspect_free_space(self) -> FreeSpaceResult:
        return cast(FreeSpaceResult, self._take("free"))

    def inspect_wal(self) -> WalInspectionResult:
        return cast(WalInspectionResult, self._take("wal"))

    def cleanup_retention(self, *, now_utc_us: int) -> RetentionCleanupResult:
        self.cleanup_times.append(now_utc_us)
        if self.on_cleanup is not None:
            callback, self.on_cleanup = self.on_cleanup, None
            callback()
        return cast(RetentionCleanupResult, self._take("cleanup"))

    def incremental_vacuum(self) -> IncrementalVacuumResult:
        return cast(IncrementalVacuumResult, self._take("vacuum"))

    def passive_wal_checkpoint(self) -> PassiveCheckpointResult:
        return cast(PassiveCheckpointResult, self._take("checkpoint"))

    def ingest(self, projection: HealthProjection) -> IngestionResult:
        del projection
        return cast(IngestionResult, self._take("ingest"))


def _orchestrator(
    fake: _FakeStore,
    *,
    monotonic: Callable[[], float] = lambda: 10.0,
    utc_now_us: Callable[[], int] = lambda: _BASE_TIME,
    trigger_state: MaintenanceTriggerState | None = None,
) -> HealthHistoryOrchestrator:
    return HealthHistoryOrchestrator(
        cast(HealthHistoryStore, fake),
        monotonic=monotonic,
        utc_now_us=utc_now_us,
        trigger_state=trigger_state,
    )


def _projection(sequence: int = 1) -> HealthProjection:
    observed = _BASE_TIME + sequence * 30_000_000
    reasons = {
        ComponentName.WLED: NormalizedReason.WLED_HEALTHY,
        ComponentName.HYPERHDR: NormalizedReason.HYPERHDR_HEALTHY,
        ComponentName.CAPTURE: NormalizedReason.CAPTURE_HEALTHY,
        ComponentName.RASPBERRY_PI: NormalizedReason.RASPBERRY_PI_HEALTHY,
    }
    components = tuple(
        ComponentProjection(
            component=component,
            status=HealthHistoryStatus.HEALTHY,
            reasons=(reasons[component],),
            checked_at_utc_us=observed,
            latency_ms=1,
            last_successful_at_utc_us=observed,
        )
        for component in COMPONENT_ORDER
    )
    digest = hashlib.sha256(
        _canonical_bytes(
            observation_sequence=sequence,
            observed_at=observed,
            status=HealthHistoryStatus.HEALTHY,
            uptime=1_000,
            sample_kind=SampleKind.HEARTBEAT,
            missed_intervals=0,
            components=components,
        )
    ).digest()
    return HealthProjection(
        schema_version=1,
        observation_sequence=sequence,
        observed_at_utc_us=observed,
        recorded_at_utc_us=observed + 1,
        overall_status=HealthHistoryStatus.HEALTHY,
        service_uptime_ms=1_000,
        sample_kind=SampleKind.HEARTBEAT,
        missed_intervals=0,
        components=components,
        digest=digest,
    )


def _snapshot(path: Path) -> dict[str, list[tuple[object, ...]]]:
    connection = sqlite3.connect(path)
    try:
        return {
            table: connection.execute(
                f"SELECT * FROM {table} ORDER BY rowid"
            ).fetchall()
            for table in _TABLES
        }
    finally:
        connection.close()


@pytest.fixture
def store_path(tmp_path: Path) -> tuple[Path, HealthHistoryStore]:
    tmp_path.chmod(0o700)
    path = tmp_path / "history.db"
    store = HealthHistoryStore.create(path, created_at_utc_us=1)
    try:
        yield path, store
    finally:
        store.close()


def test_normal_observation_proceeds_to_exactly_one_ingestion() -> None:
    fake = _FakeStore()
    orchestrator = _orchestrator(fake)
    result = orchestrator.process_observation(_projection())
    assert result == ObservationCycleResult(OrchestrationOutcome.STORED)
    assert not result.storage_maintenance_attempted
    assert fake.calls == ["capacity", "free", "wal", "ingest"]
    assert orchestrator.trigger_state.stored_rows_since_maintenance == 1


@pytest.mark.parametrize(
    ("ingestion_outcome", "orchestration_outcome"),
    [
        (IngestionOutcome.REPLAYED, OrchestrationOutcome.REPLAYED),
        (IngestionOutcome.STATE_ONLY, OrchestrationOutcome.STATE_ONLY),
        (IngestionOutcome.HEARTBEAT_STORED, OrchestrationOutcome.STORED),
        (IngestionOutcome.STARTUP_MARKER_STORED, OrchestrationOutcome.STORED),
        (IngestionOutcome.CLOCK_MARKER_STORED, OrchestrationOutcome.STORED),
    ],
)
def test_ingestion_outcomes_map_without_retry(
    ingestion_outcome: IngestionOutcome,
    orchestration_outcome: OrchestrationOutcome,
) -> None:
    fake = _FakeStore()
    fake.responses["ingest"] = [IngestionResult(ingestion_outcome)]
    orchestrator = _orchestrator(fake)
    result = orchestrator.process_observation(_projection())
    assert result.outcome is orchestration_outcome
    assert not result.storage_maintenance_attempted
    assert fake.calls.count("ingest") == 1
    assert orchestrator.trigger_state.stored_rows_since_maintenance == (
        1 if orchestration_outcome is OrchestrationOutcome.STORED else 0
    )


def test_checkpoint_due_runs_once_then_fresh_decision_and_ingests() -> None:
    fake = _FakeStore()
    fake.responses["capacity"] = [_capacity(), _capacity()]
    fake.responses["free"] = [_free_space(), _free_space()]
    fake.responses["wal"] = [_wal(256), _wal()]
    result = _orchestrator(fake).process_observation(_projection())
    assert result.outcome is OrchestrationOutcome.STORED
    assert result.storage_maintenance_attempted
    assert fake.calls == [
        "capacity",
        "free",
        "wal",
        "checkpoint",
        "capacity",
        "free",
        "wal",
        "ingest",
    ]


@pytest.mark.parametrize(
    ("ingestion_outcome", "expected"),
    [
        (IngestionOutcome.TRANSITION_STORED, OrchestrationOutcome.STORED),
        (IngestionOutcome.STATE_ONLY, OrchestrationOutcome.STATE_ONLY),
        (IngestionOutcome.REPLAYED, OrchestrationOutcome.REPLAYED),
    ],
)
@pytest.mark.parametrize("maintenance_path", ["capacity", "checkpoint"])
def test_each_accepted_outcome_preserves_observation_maintenance_evidence(
    ingestion_outcome: IngestionOutcome,
    expected: OrchestrationOutcome,
    maintenance_path: str,
) -> None:
    fake = _FakeStore()
    fake.responses["capacity"] = [
        _capacity(full=maintenance_path == "capacity"),
        _capacity(),
    ]
    fake.responses["free"] = [_free_space(), _free_space()]
    fake.responses["wal"] = [
        _wal(256 if maintenance_path == "checkpoint" else 0),
        _wal(),
    ]
    fake.responses["ingest"] = [IngestionResult(ingestion_outcome)]
    result = _orchestrator(fake).process_observation(_projection())
    assert result.outcome is expected
    assert result.storage_maintenance_attempted
    assert fake.calls.count("cleanup") == (maintenance_path == "capacity")
    assert fake.calls.count("vacuum") == (maintenance_path == "capacity")
    assert fake.calls.count("checkpoint") == (maintenance_path == "checkpoint")
    assert fake.calls.count("ingest") == 1


@pytest.mark.parametrize(
    ("checkpoint_value", "expected"),
    [
        (
            _checkpoint(PassiveCheckpointOutcome.BUSY),
            OrchestrationOutcome.CHECKPOINT_BUSY,
        ),
        (
            StorageEnvelopeError(StorageEnvelopeRejection.TIMED_OUT),
            OrchestrationOutcome.TIMED_OUT,
        ),
        (
            StorageEnvelopeError(StorageEnvelopeRejection.STORAGE_BUSY),
            OrchestrationOutcome.CHECKPOINT_BUSY,
        ),
    ],
)
def test_checkpoint_failure_skips_ingestion_without_retry(
    checkpoint_value: object, expected: OrchestrationOutcome
) -> None:
    fake = _FakeStore()
    fake.responses["wal"] = [_wal(256)]
    fake.responses["checkpoint"] = [checkpoint_value]
    result = _orchestrator(fake).process_observation(_projection())
    assert result.outcome is expected
    assert result.storage_maintenance_attempted
    assert fake.calls.count("checkpoint") == 1
    assert "ingest" not in fake.calls


def test_checkpoint_no_work_concurrent_transition_uses_fresh_decision() -> None:
    fake = _FakeStore()
    fake.responses["capacity"] = [_capacity(), _capacity()]
    fake.responses["free"] = [_free_space(), _free_space()]
    fake.responses["wal"] = [_wal(256), _wal()]
    fake.responses["checkpoint"] = [_checkpoint(PassiveCheckpointOutcome.NO_WORK)]
    result = _orchestrator(fake).process_observation(_projection())
    assert result.outcome is OrchestrationOutcome.STORED
    assert result.storage_maintenance_attempted
    assert fake.calls.count("checkpoint") == 1
    assert fake.calls.count("ingest") == 1


def test_checkpoint_concurrent_oversize_blocks_observation() -> None:
    fake = _FakeStore()
    fake.responses["wal"] = [_wal(256)]
    fake.responses["checkpoint"] = [
        _checkpoint(PassiveCheckpointOutcome.OVERSIZE_BLOCKED)
    ]
    result = _orchestrator(fake).process_observation(_projection())
    assert result.outcome is OrchestrationOutcome.WAL_OVERSIZE_BLOCKED
    assert result.storage_maintenance_attempted
    assert fake.calls.count("checkpoint") == 1
    assert "ingest" not in fake.calls


def test_incomplete_checkpoint_does_not_repeat_or_ingest() -> None:
    fake = _FakeStore()
    fake.responses["capacity"] = [_capacity(), _capacity()]
    fake.responses["free"] = [_free_space(), _free_space()]
    fake.responses["wal"] = [_wal(256), _wal(256)]
    result = _orchestrator(fake).process_observation(_projection())
    assert result.outcome is OrchestrationOutcome.CHECKPOINT_INCOMPLETE
    assert result.storage_maintenance_attempted
    assert fake.calls.count("checkpoint") == 1
    assert "ingest" not in fake.calls


def test_wal_oversize_blocks_every_mutating_action() -> None:
    fake = _FakeStore()
    fake.responses["wal"] = [_wal(WAL_HARD_LIMIT_FRAMES + 1)]
    result = _orchestrator(fake).process_observation(_projection())
    assert result.outcome is OrchestrationOutcome.WAL_OVERSIZE_BLOCKED
    assert not result.storage_maintenance_attempted
    assert fake.calls == ["capacity", "free", "wal"]


def test_physical_wal_oversize_also_blocks_without_checkpoint() -> None:
    fake = _FakeStore()
    physical = WAL_HEADER_BYTES + 1024 * WAL_FRAME_BYTES
    assert physical > WAL_HARD_LIMIT_BYTES
    fake.responses["wal"] = [_wal(1, physical_bytes=physical)]
    result = _orchestrator(fake).process_observation(_projection())
    assert result.outcome is OrchestrationOutcome.WAL_OVERSIZE_BLOCKED
    assert "checkpoint" not in fake.calls
    assert "ingest" not in fake.calls


def test_capacity_maintenance_runs_cleanup_vacuum_and_one_reinspection() -> None:
    fake = _FakeStore()
    fake.responses["capacity"] = [_capacity(full=True), _capacity()]
    fake.responses["free"] = [_free_space(), _free_space()]
    fake.responses["wal"] = [_wal(), _wal()]
    result = _orchestrator(fake).process_observation(_projection())
    assert result.outcome is OrchestrationOutcome.STORED
    assert result.storage_maintenance_attempted
    assert fake.calls == [
        "capacity",
        "free",
        "wal",
        "cleanup",
        "vacuum",
        "capacity",
        "free",
        "wal",
        "ingest",
    ]
    assert fake.cleanup_times == [_BASE_TIME]


@pytest.mark.parametrize("shortage", ["capacity", "free_space"])
def test_one_capacity_maintenance_attempt_then_capacity_blocked(shortage: str) -> None:
    fake = _FakeStore()
    fake.responses["capacity"] = [
        _capacity(full=shortage == "capacity"),
        _capacity(full=shortage == "capacity"),
    ]
    fake.responses["free"] = [
        _free_space(sufficient=shortage != "free_space"),
        _free_space(sufficient=shortage != "free_space"),
    ]
    fake.responses["wal"] = [_wal(), _wal()]
    result = _orchestrator(fake).process_observation(_projection())
    assert result.outcome is OrchestrationOutcome.CAPACITY_BLOCKED
    assert result.storage_maintenance_attempted
    assert fake.calls.count("cleanup") == 1
    assert fake.calls.count("vacuum") == 1
    assert fake.calls.count("capacity") == 2
    assert fake.calls.count("free") == 2
    assert "ingest" not in fake.calls


def test_capacity_reinspection_can_become_wal_oversize_blocked() -> None:
    fake = _FakeStore()
    fake.responses["capacity"] = [_capacity(full=True), _capacity()]
    fake.responses["free"] = [_free_space(), _free_space()]
    fake.responses["wal"] = [_wal(), _wal(WAL_HARD_LIMIT_FRAMES + 1)]
    result = _orchestrator(fake).process_observation(_projection())
    assert result.outcome is OrchestrationOutcome.WAL_OVERSIZE_BLOCKED
    assert result.storage_maintenance_attempted
    assert fake.calls.count("cleanup") == 1
    assert fake.calls.count("vacuum") == 1
    assert "checkpoint" not in fake.calls
    assert "ingest" not in fake.calls


def test_capacity_reinspection_trust_failure_stops_before_ingestion() -> None:
    fake = _FakeStore()
    fake.responses["capacity"] = [
        _capacity(full=True),
        StorageEnvelopeError(StorageEnvelopeRejection.TRUST_FAILED, trust_lost=True),
    ]
    result = _orchestrator(fake).process_observation(_projection())
    assert result.outcome is OrchestrationOutcome.TRUST_FAILED
    assert result.storage_maintenance_attempted
    assert fake.calls[-3:] == ["cleanup", "vacuum", "capacity"]
    assert "ingest" not in fake.calls


@pytest.mark.parametrize(
    ("post_capacity", "post_wal", "expected"),
    [
        (_capacity(full=True), _wal(), OrchestrationOutcome.CAPACITY_BLOCKED),
        (
            _capacity(),
            _wal(WAL_HARD_LIMIT_FRAMES + 1),
            OrchestrationOutcome.WAL_OVERSIZE_BLOCKED,
        ),
    ],
)
def test_post_checkpoint_fresh_decision_can_block_write(
    post_capacity: StorageCapacityResult,
    post_wal: WalInspectionResult,
    expected: OrchestrationOutcome,
) -> None:
    fake = _FakeStore()
    fake.responses["capacity"] = [_capacity(), post_capacity]
    fake.responses["free"] = [_free_space(), _free_space()]
    fake.responses["wal"] = [_wal(256), post_wal]
    result = _orchestrator(fake).process_observation(_projection())
    assert result.outcome is expected
    assert result.storage_maintenance_attempted
    assert fake.calls.count("checkpoint") == 1
    assert "ingest" not in fake.calls


def test_post_checkpoint_reinspection_failure_stops_before_ingestion() -> None:
    fake = _FakeStore()
    fake.responses["capacity"] = [
        _capacity(),
        StorageEnvelopeError(StorageEnvelopeRejection.PERSISTENCE_FAILED),
    ]
    fake.responses["wal"] = [_wal(256)]
    result = _orchestrator(fake).process_observation(_projection())
    assert result.outcome is OrchestrationOutcome.PERSISTENCE_FAILED
    assert result.storage_maintenance_attempted
    assert fake.calls[-2:] == ["checkpoint", "capacity"]
    assert "ingest" not in fake.calls


def test_cleanup_failure_stops_vacuum_and_ingestion() -> None:
    fake = _FakeStore()
    fake.responses["capacity"] = [_capacity(full=True)]
    fake.responses["cleanup"] = [
        MaintenanceError(MaintenanceRejection.PERSISTENCE_FAILED)
    ]
    result = _orchestrator(fake).process_observation(_projection())
    assert result.outcome is OrchestrationOutcome.PERSISTENCE_FAILED
    assert result.storage_maintenance_attempted
    assert fake.calls[-1] == "cleanup"
    assert "vacuum" not in fake.calls
    assert "ingest" not in fake.calls


def test_vacuum_failure_stops_reinspection_and_ingestion() -> None:
    fake = _FakeStore()
    fake.responses["capacity"] = [_capacity(full=True)]
    fake.responses["vacuum"] = [MaintenanceError(MaintenanceRejection.TIMED_OUT)]
    result = _orchestrator(fake).process_observation(_projection())
    assert result.outcome is OrchestrationOutcome.TIMED_OUT
    assert result.storage_maintenance_attempted
    assert fake.calls[-2:] == ["cleanup", "vacuum"]
    assert fake.calls.count("capacity") == 1
    assert "ingest" not in fake.calls


@pytest.mark.parametrize(
    ("stage", "calls_before"),
    [
        ("capacity", []),
        ("free", ["capacity"]),
        ("wal", ["capacity", "free"]),
        ("cleanup", ["capacity", "free", "wal"]),
        ("vacuum", ["capacity", "free", "wal", "cleanup"]),
        ("checkpoint", ["capacity", "free", "wal"]),
        ("ingest", ["capacity", "free", "wal"]),
    ],
)
def test_trust_loss_stops_observation_at_every_stage(
    stage: str, calls_before: list[str]
) -> None:
    fake = _FakeStore()
    if stage in {"cleanup", "vacuum"}:
        fake.responses["capacity"] = [_capacity(full=True)]
        fake.responses[stage] = [
            MaintenanceError(MaintenanceRejection.TRUST_FAILED, trust_lost=True)
        ]
    elif stage == "ingest":
        fake.responses[stage] = [
            IngestionError(IngestionRejection.TRUST_FAILED, trust_lost=True)
        ]
    else:
        if stage == "checkpoint":
            fake.responses["wal"] = [_wal(256)]
        fake.responses[stage] = [
            StorageEnvelopeError(StorageEnvelopeRejection.TRUST_FAILED, trust_lost=True)
        ]
    result = _orchestrator(fake).process_observation(_projection())
    assert result.outcome is OrchestrationOutcome.TRUST_FAILED
    assert fake.calls == [*calls_before, stage]


@pytest.mark.parametrize(
    ("rejection", "expected"),
    [
        (
            IngestionRejection.INVALID_PROJECTION,
            OrchestrationOutcome.INVALID_OBSERVATION,
        ),
        (IngestionRejection.STALE_SEQUENCE, OrchestrationOutcome.STALE_SEQUENCE),
        (IngestionRejection.SEQUENCE_CONFLICT, OrchestrationOutcome.SEQUENCE_CONFLICT),
        (
            IngestionRejection.GENERATION_EXHAUSTED,
            OrchestrationOutcome.GENERATION_EXHAUSTED,
        ),
        (IngestionRejection.STORAGE_BUSY, OrchestrationOutcome.STORAGE_BUSY),
        (
            IngestionRejection.PERSISTENCE_FAILED,
            OrchestrationOutcome.PERSISTENCE_FAILED,
        ),
    ],
)
def test_ingestion_rejections_are_sanitized_without_retry(
    rejection: IngestionRejection, expected: OrchestrationOutcome
) -> None:
    fake = _FakeStore()
    fake.responses["ingest"] = [IngestionError(rejection)]
    result = _orchestrator(fake).process_observation(_projection())
    assert result.outcome is expected
    assert fake.calls.count("ingest") == 1


def test_unsupported_wal_runtime_skips_ingestion_and_leaves_no_retry() -> None:
    fake = _FakeStore()
    fake.responses["wal"] = [
        StorageEnvelopeError(StorageEnvelopeRejection.UNSUPPORTED_RUNTIME)
    ]
    result = _orchestrator(fake).process_observation(_projection())
    assert result.outcome is OrchestrationOutcome.UNSUPPORTED_RUNTIME
    assert fake.calls == ["capacity", "free", "wal"]


@pytest.mark.parametrize("stage", ["capacity", "cleanup", "checkpoint", "ingest"])
def test_closed_store_error_maps_to_trust_failure(stage: str) -> None:
    fake = _FakeStore()
    if stage == "cleanup":
        fake.responses["capacity"] = [_capacity(full=True)]
    elif stage == "checkpoint":
        fake.responses["wal"] = [_wal(256)]
    fake.responses[stage] = [StoreError("store_closed")]
    result = _orchestrator(fake).process_observation(_projection())
    assert result.outcome is OrchestrationOutcome.TRUST_FAILED
    assert fake.calls[-1] == stage


def test_invalid_injected_storage_result_fails_closed() -> None:
    fake = _FakeStore()
    fake.responses["capacity"] = [object()]
    result = _orchestrator(fake).process_observation(_projection())
    assert result.outcome is OrchestrationOutcome.TRUST_FAILED
    assert fake.calls == ["capacity", "free", "wal"]


def test_invalid_utc_clock_fails_before_capacity_maintenance_mutation() -> None:
    fake = _FakeStore()
    fake.responses["capacity"] = [_capacity(full=True)]
    result = _orchestrator(fake, utc_now_us=lambda: -1).process_observation(
        _projection()
    )
    assert result.outcome is OrchestrationOutcome.INVALID_CLOCK
    assert "cleanup" not in fake.calls


def test_maintenance_below_threshold_runs_each_fixed_primitive_once() -> None:
    fake = _FakeStore()
    orchestrator = _orchestrator(fake)
    result = orchestrator.run_maintenance_opportunity()
    assert result.outcome is OrchestrationOutcome.MAINTENANCE_COMPLETED
    assert fake.calls == ["cleanup", "vacuum", "wal"]
    assert orchestrator.trigger_state == MaintenanceTriggerState(True, 10.0, 0)


def test_maintenance_checkpoint_due_runs_exactly_one_checkpoint() -> None:
    fake = _FakeStore()
    fake.responses["wal"] = [_wal(256)]
    result = _orchestrator(fake).run_maintenance_opportunity()
    assert result.outcome is OrchestrationOutcome.MAINTENANCE_COMPLETED
    assert fake.calls == ["cleanup", "vacuum", "wal", "checkpoint"]


@pytest.mark.parametrize(
    ("checkpoint_value", "expected"),
    [
        (
            _checkpoint(PassiveCheckpointOutcome.BUSY),
            OrchestrationOutcome.CHECKPOINT_BUSY,
        ),
        (
            StorageEnvelopeError(StorageEnvelopeRejection.TIMED_OUT),
            OrchestrationOutcome.TIMED_OUT,
        ),
        (
            StorageEnvelopeError(StorageEnvelopeRejection.STORAGE_BUSY),
            OrchestrationOutcome.CHECKPOINT_BUSY,
        ),
    ],
)
def test_maintenance_checkpoint_failures_do_not_retry_or_reset_trigger(
    checkpoint_value: object, expected: OrchestrationOutcome
) -> None:
    fake = _FakeStore()
    fake.responses["wal"] = [_wal(256)]
    fake.responses["checkpoint"] = [checkpoint_value]
    initial = MaintenanceTriggerState(False, None, 120)
    orchestrator = _orchestrator(fake, trigger_state=initial)
    result = orchestrator.run_maintenance_opportunity()
    assert result.outcome is expected
    assert fake.calls.count("checkpoint") == 1
    assert orchestrator.trigger_state is initial


def test_maintenance_wal_oversize_preserves_evidence_without_checkpoint() -> None:
    fake = _FakeStore()
    fake.responses["wal"] = [_wal(WAL_HARD_LIMIT_FRAMES + 1)]
    result = _orchestrator(fake).run_maintenance_opportunity()
    assert result.outcome is OrchestrationOutcome.WAL_OVERSIZE_BLOCKED
    assert fake.calls == ["cleanup", "vacuum", "wal"]


def test_maintenance_checkpoint_concurrent_oversize_is_blocked() -> None:
    fake = _FakeStore()
    fake.responses["wal"] = [_wal(256)]
    fake.responses["checkpoint"] = [
        _checkpoint(PassiveCheckpointOutcome.OVERSIZE_BLOCKED)
    ]
    result = _orchestrator(fake).run_maintenance_opportunity()
    assert result.outcome is OrchestrationOutcome.WAL_OVERSIZE_BLOCKED
    assert fake.calls.count("checkpoint") == 1


@pytest.mark.parametrize("stage", ["cleanup", "wal", "checkpoint"])
def test_maintenance_closed_store_stops_immediately(stage: str) -> None:
    fake = _FakeStore()
    if stage == "checkpoint":
        fake.responses["wal"] = [_wal(256)]
    fake.responses[stage] = [StoreError("store_closed")]
    result = _orchestrator(fake).run_maintenance_opportunity()
    assert result.outcome is OrchestrationOutcome.TRUST_FAILED
    assert fake.calls[-1] == stage


def test_maintenance_storage_busy_stops_without_reset() -> None:
    fake = _FakeStore()
    fake.responses["cleanup"] = [MaintenanceError(MaintenanceRejection.STORAGE_BUSY)]
    initial = MaintenanceTriggerState(False, None, 120)
    orchestrator = _orchestrator(fake, trigger_state=initial)
    result = orchestrator.run_maintenance_opportunity()
    assert result.outcome is OrchestrationOutcome.STORAGE_BUSY
    assert orchestrator.trigger_state is initial


@pytest.mark.parametrize("failure_stage", ["cleanup", "vacuum", "wal"])
def test_maintenance_failure_stops_later_mutating_work(failure_stage: str) -> None:
    fake = _FakeStore()
    if failure_stage in {"cleanup", "vacuum"}:
        fake.responses[failure_stage] = [
            MaintenanceError(MaintenanceRejection.PERSISTENCE_FAILED)
        ]
    else:
        fake.responses["wal"] = [
            StorageEnvelopeError(StorageEnvelopeRejection.PERSISTENCE_FAILED)
        ]
    result = _orchestrator(fake).run_maintenance_opportunity()
    assert result.outcome is OrchestrationOutcome.PERSISTENCE_FAILED
    assert fake.calls[-1] == failure_stage
    assert "checkpoint" not in fake.calls


def test_startup_trigger_is_due_until_successful_maintenance() -> None:
    state = MaintenanceTriggerState()
    assert state.decision(0.0).reason is MaintenanceTriggerReason.STARTUP
    failed = MaintenanceOpportunityResult(OrchestrationOutcome.STORAGE_BUSY)
    assert (
        state.after_maintenance(
            failed,
            started_monotonic=1.0,
            completed_monotonic=1.0,
        )
        is state
    )
    completed = MaintenanceOpportunityResult(OrchestrationOutcome.MAINTENANCE_COMPLETED)
    updated = state.after_maintenance(
        completed,
        started_monotonic=1.0,
        completed_monotonic=1.0,
    )
    assert updated.decision(1.0).reason is MaintenanceTriggerReason.NONE


def test_orchestrator_exposes_pure_trigger_decision() -> None:
    fake = _FakeStore()
    orchestrator = _orchestrator(fake)
    assert orchestrator.maintenance_trigger() == MaintenanceTriggerDecision(
        True, MaintenanceTriggerReason.STARTUP
    )
    assert fake.calls == []


def test_hourly_trigger_uses_monotonic_exact_boundary() -> None:
    state = MaintenanceTriggerState(True, 10.0, 0)
    assert state.decision(10.0 + MAINTENANCE_INTERVAL_SECONDS - 0.001).reason is (
        MaintenanceTriggerReason.NONE
    )
    assert state.decision(10.0 + MAINTENANCE_INTERVAL_SECONDS).reason is (
        MaintenanceTriggerReason.HOURLY
    )
    assert state.decision(10.0 + MAINTENANCE_INTERVAL_SECONDS + 1.0).reason is (
        MaintenanceTriggerReason.HOURLY
    )


def test_stored_row_trigger_is_due_at_120_and_has_priority_over_hourly() -> None:
    state = MaintenanceTriggerState(True, 10.0, 119)
    stored = ObservationCycleResult(OrchestrationOutcome.STORED)
    assert state.decision(10.0).reason is MaintenanceTriggerReason.NONE
    state = state.after_observation(stored)
    assert state.stored_rows_since_maintenance == 120
    assert state.decision(10.0).reason is MaintenanceTriggerReason.STORED_ROWS
    assert state.decision(10.0 + MAINTENANCE_INTERVAL_SECONDS).reason is (
        MaintenanceTriggerReason.STORED_ROWS
    )


@pytest.mark.parametrize(
    "outcome",
    [
        OrchestrationOutcome.REPLAYED,
        OrchestrationOutcome.STATE_ONLY,
        OrchestrationOutcome.PERSISTENCE_FAILED,
        OrchestrationOutcome.STORAGE_BUSY,
    ],
)
def test_nonstored_observation_does_not_advance_trigger(
    outcome: OrchestrationOutcome,
) -> None:
    state = MaintenanceTriggerState(True, 10.0, 119)
    assert state.after_observation(ObservationCycleResult(outcome)) is state


def test_stored_row_counter_saturates_and_success_resets() -> None:
    state = MaintenanceTriggerState(True, 10.0, MAX_BOUNDED_COUNTER)
    state = state.after_observation(ObservationCycleResult(OrchestrationOutcome.STORED))
    assert state.stored_rows_since_maintenance == MAX_BOUNDED_COUNTER
    state = state.after_maintenance(
        MaintenanceOpportunityResult(OrchestrationOutcome.MAINTENANCE_COMPLETED),
        started_monotonic=19.0,
        completed_monotonic=20.0,
    )
    assert state == MaintenanceTriggerState(True, 20.0, 0)


@pytest.mark.parametrize("value", [-1.0, math.inf, -math.inf, math.nan, 1, True])
def test_malformed_monotonic_values_fail_safely(value: object) -> None:
    with pytest.raises(OrchestrationError) as caught:
        MaintenanceTriggerState().decision(value)
    assert caught.value.reason is OrchestrationRejection.INVALID_MONOTONIC
    assert str(caught.value) == "invalid_monotonic"


def test_regressed_monotonic_value_fails_safely() -> None:
    with pytest.raises(OrchestrationError) as caught:
        MaintenanceTriggerState(True, 10.0, 0).decision(9.0)
    assert caught.value.reason is OrchestrationRejection.INVALID_MONOTONIC


def test_regressed_completion_marker_is_rejected() -> None:
    state = MaintenanceTriggerState(True, 10.0, 120)
    with pytest.raises(OrchestrationError) as caught:
        state.after_maintenance(
            MaintenanceOpportunityResult(OrchestrationOutcome.MAINTENANCE_COMPLETED),
            started_monotonic=9.0,
            completed_monotonic=9.0,
        )
    assert caught.value.reason is OrchestrationRejection.INVALID_MONOTONIC


def test_pure_maintenance_transition_rejects_regressed_start() -> None:
    state = MaintenanceTriggerState(True, 100.0, 120)
    with pytest.raises(OrchestrationError) as caught:
        state.after_maintenance(
            MaintenanceOpportunityResult(OrchestrationOutcome.MAINTENANCE_COMPLETED),
            started_monotonic=90.0,
            completed_monotonic=110.0,
        )
    assert caught.value.reason is OrchestrationRejection.INVALID_MONOTONIC


@pytest.mark.parametrize(
    ("started", "completed", "expected_marker"),
    [(100.0, 100.0, 100.0), (101.0, 102.0, 102.0)],
)
def test_pure_maintenance_transition_accepts_nonregressed_boundaries(
    started: float,
    completed: float,
    expected_marker: float,
) -> None:
    state = MaintenanceTriggerState(True, 100.0, 120)
    updated = state.after_maintenance(
        MaintenanceOpportunityResult(OrchestrationOutcome.MAINTENANCE_COMPLETED),
        started_monotonic=started,
        completed_monotonic=completed,
    )
    assert updated == MaintenanceTriggerState(True, expected_marker, 0)


def test_trigger_model_rejects_wrong_fixed_result_types() -> None:
    state = MaintenanceTriggerState()
    with pytest.raises(OrchestrationError) as observation:
        state.after_observation(cast(ObservationCycleResult, object()))
    with pytest.raises(OrchestrationError) as maintenance:
        state.after_maintenance(
            cast(MaintenanceOpportunityResult, object()),
            started_monotonic=1.0,
            completed_monotonic=1.0,
        )
    assert observation.value.reason is OrchestrationRejection.INVALID_TRIGGER_STATE
    assert maintenance.value.reason is OrchestrationRejection.INVALID_TRIGGER_STATE


@pytest.mark.parametrize(
    "state",
    [
        (False, 1.0, 0),
        (True, None, 0),
        (True, math.nan, 0),
        (True, 1.0, -1),
        (True, 1.0, MAX_BOUNDED_COUNTER + 1),
    ],
)
def test_impossible_trigger_state_is_rejected(
    state: tuple[bool, float | None, int],
) -> None:
    with pytest.raises(ValueError, match="invalid_maintenance_trigger_state"):
        MaintenanceTriggerState(*state)


def test_reentrant_observation_is_rejected_and_guard_restores() -> None:
    fake = _FakeStore()
    orchestrator = _orchestrator(fake)
    nested: list[ObservationCycleResult] = []
    fake.on_capacity = lambda: nested.append(
        orchestrator.process_observation(_projection(2))
    )
    assert orchestrator.process_observation(_projection()).outcome is (
        OrchestrationOutcome.STORED
    )
    assert nested == [ObservationCycleResult(OrchestrationOutcome.REENTRANT)]
    assert orchestrator.process_observation(_projection(3)).outcome is (
        OrchestrationOutcome.STORED
    )


def test_concurrent_cycle_is_nonblocking_and_rejected() -> None:
    fake = _FakeStore()
    entered = threading.Event()
    release = threading.Event()
    orchestrator = _orchestrator(fake)

    def hold_cycle() -> None:
        entered.set()
        assert release.wait(timeout=2.0)

    fake.on_capacity = hold_cycle
    completed: list[ObservationCycleResult] = []
    thread = threading.Thread(
        target=lambda: completed.append(
            orchestrator.process_observation(_projection())
        ),
        daemon=True,
    )
    thread.start()
    assert entered.wait(timeout=1.0)
    assert orchestrator.process_observation(_projection(2)).outcome is (
        OrchestrationOutcome.REENTRANT
    )
    release.set()
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert completed == [ObservationCycleResult(OrchestrationOutcome.STORED)]


def test_reentrant_maintenance_and_trigger_are_rejected() -> None:
    fake = _FakeStore()
    orchestrator = _orchestrator(fake)
    nested_maintenance: list[MaintenanceOpportunityResult] = []
    nested_trigger: list[OrchestrationRejection] = []

    def reenter() -> None:
        nested_maintenance.append(orchestrator.run_maintenance_opportunity())
        with pytest.raises(OrchestrationError) as caught:
            orchestrator.maintenance_trigger()
        nested_trigger.append(caught.value.reason)

    fake.on_capacity = reenter
    assert orchestrator.process_observation(_projection()).outcome is (
        OrchestrationOutcome.STORED
    )
    assert nested_maintenance == [
        MaintenanceOpportunityResult(OrchestrationOutcome.REENTRANT)
    ]
    assert nested_trigger == [OrchestrationRejection.REENTRANT]
    assert orchestrator.maintenance_trigger().due


def test_fresh_start_intra_op_clock_regression_preserves_startup_state() -> None:
    fake = _FakeStore()
    values = iter([100.0, 50.0])
    initial = MaintenanceTriggerState()
    orchestrator = _orchestrator(
        fake,
        monotonic=lambda: next(values),
        trigger_state=initial,
    )
    result = orchestrator.run_maintenance_opportunity()
    assert result.outcome is OrchestrationOutcome.INVALID_CLOCK
    assert fake.calls == ["cleanup", "vacuum", "wal"]
    assert orchestrator.trigger_state is initial
    assert orchestrator.trigger_state.decision(100.0).reason is (
        MaintenanceTriggerReason.STARTUP
    )


def test_existing_marker_start_regression_stops_before_maintenance() -> None:
    fake = _FakeStore()
    monotonic_reads: list[float] = []
    utc_reads = 0

    def regressed_monotonic() -> float:
        monotonic_reads.append(90.0)
        return 90.0

    def tracked_utc() -> int:
        nonlocal utc_reads
        utc_reads += 1
        return _BASE_TIME

    initial = MaintenanceTriggerState(True, 100.0, 120)
    orchestrator = _orchestrator(
        fake,
        monotonic=regressed_monotonic,
        utc_now_us=tracked_utc,
        trigger_state=initial,
    )
    result = orchestrator.run_maintenance_opportunity()
    assert result.outcome is OrchestrationOutcome.INVALID_CLOCK
    assert fake.calls == []
    assert fake.cleanup_times == []
    assert monotonic_reads == [90.0]
    assert utc_reads == 0
    assert orchestrator.trigger_state is initial


def test_existing_marker_does_not_mask_intra_op_clock_regression() -> None:
    fake = _FakeStore()
    values = iter([100.0, 90.0])
    initial = MaintenanceTriggerState(True, 80.0, 120)
    orchestrator = _orchestrator(
        fake,
        monotonic=lambda: next(values),
        trigger_state=initial,
    )
    result = orchestrator.run_maintenance_opportunity()
    assert result.outcome is OrchestrationOutcome.INVALID_CLOCK
    assert fake.calls == ["cleanup", "vacuum", "wal"]
    assert orchestrator.trigger_state is initial


def test_valid_maintenance_clock_progression_records_completion() -> None:
    fake = _FakeStore()
    values = iter([100.0, 101.0])
    initial = MaintenanceTriggerState(True, 80.0, 120)
    orchestrator = _orchestrator(
        fake,
        monotonic=lambda: next(values),
        trigger_state=initial,
    )
    result = orchestrator.run_maintenance_opportunity()
    assert result.outcome is OrchestrationOutcome.MAINTENANCE_COMPLETED
    assert fake.calls == ["cleanup", "vacuum", "wal"]
    assert orchestrator.trigger_state == MaintenanceTriggerState(True, 101.0, 0)


def test_equal_maintenance_start_and_completion_is_valid() -> None:
    fake = _FakeStore()
    values = iter([100.0, 100.0])
    orchestrator = _orchestrator(fake, monotonic=lambda: next(values))
    result = orchestrator.run_maintenance_opportunity()
    assert result.outcome is OrchestrationOutcome.MAINTENANCE_COMPLETED
    assert orchestrator.trigger_state == MaintenanceTriggerState(True, 100.0, 0)


@pytest.mark.parametrize("completion", [math.nan, math.inf, -math.inf])
def test_invalid_completion_clock_preserves_trigger_without_retry(
    completion: float,
) -> None:
    fake = _FakeStore()
    values = iter([100.0, completion])
    initial = MaintenanceTriggerState(True, 80.0, 120)
    orchestrator = _orchestrator(
        fake,
        monotonic=lambda: next(values),
        trigger_state=initial,
    )
    result = orchestrator.run_maintenance_opportunity()
    assert result.outcome is OrchestrationOutcome.INVALID_CLOCK
    assert fake.calls == ["cleanup", "vacuum", "wal"]
    assert orchestrator.trigger_state is initial


def test_completion_clock_failure_never_repeats_eligible_checkpoint() -> None:
    fake = _FakeStore()
    fake.responses["wal"] = [_wal(256)]
    values = iter([100.0, 50.0])
    orchestrator = _orchestrator(fake, monotonic=lambda: next(values))
    result = orchestrator.run_maintenance_opportunity()
    assert result.outcome is OrchestrationOutcome.INVALID_CLOCK
    assert fake.calls == ["cleanup", "vacuum", "wal", "checkpoint"]
    assert fake.calls.count("cleanup") == 1
    assert fake.calls.count("vacuum") == 1
    assert fake.calls.count("wal") == 1
    assert fake.calls.count("checkpoint") == 1
    assert orchestrator.trigger_state == MaintenanceTriggerState()


def test_guard_restores_after_completion_clock_failure() -> None:
    fake = _FakeStore()
    values = iter([100.0, 50.0, 200.0, 201.0])
    orchestrator = _orchestrator(fake, monotonic=lambda: next(values))
    assert orchestrator.run_maintenance_opportunity().outcome is (
        OrchestrationOutcome.INVALID_CLOCK
    )
    assert orchestrator.run_maintenance_opportunity().outcome is (
        OrchestrationOutcome.MAINTENANCE_COMPLETED
    )
    assert fake.calls == [
        "cleanup",
        "vacuum",
        "wal",
        "cleanup",
        "vacuum",
        "wal",
    ]
    assert orchestrator.trigger_state == MaintenanceTriggerState(True, 201.0, 0)


def test_guard_restores_after_start_clock_regression() -> None:
    fake = _FakeStore()
    values = iter([90.0, 100.0, 101.0])
    initial = MaintenanceTriggerState(True, 100.0, 120)
    orchestrator = _orchestrator(
        fake,
        monotonic=lambda: next(values),
        trigger_state=initial,
    )
    assert orchestrator.run_maintenance_opportunity().outcome is (
        OrchestrationOutcome.INVALID_CLOCK
    )
    assert orchestrator.trigger_state is initial
    assert orchestrator.run_maintenance_opportunity().outcome is (
        OrchestrationOutcome.MAINTENANCE_COMPLETED
    )
    assert fake.calls == ["cleanup", "vacuum", "wal"]
    assert orchestrator.trigger_state == MaintenanceTriggerState(True, 101.0, 0)


def test_invalid_maintenance_clocks_stop_or_leave_trigger_due() -> None:
    fake = _FakeStore()
    invalid_start = _orchestrator(fake, monotonic=lambda: math.nan)
    assert invalid_start.run_maintenance_opportunity().outcome is (
        OrchestrationOutcome.INVALID_CLOCK
    )
    assert fake.calls == []

    fake = _FakeStore()
    invalid_utc = _orchestrator(fake, utc_now_us=lambda: -1)
    assert invalid_utc.run_maintenance_opportunity().outcome is (
        OrchestrationOutcome.INVALID_CLOCK
    )
    assert fake.calls == []


def test_clock_callback_exceptions_are_sanitized() -> None:
    def broken_monotonic() -> float:
        raise RuntimeError("private clock detail")

    def broken_utc() -> int:
        raise RuntimeError("private UTC detail")

    fake = _FakeStore()
    assert _orchestrator(
        fake, monotonic=broken_monotonic
    ).run_maintenance_opportunity().outcome is (OrchestrationOutcome.INVALID_CLOCK)
    fake = _FakeStore()
    fake.responses["capacity"] = [_capacity(full=True)]
    assert (
        _orchestrator(fake, utc_now_us=broken_utc)
        .process_observation(_projection())
        .outcome
        is OrchestrationOutcome.INVALID_CLOCK
    )


@pytest.mark.parametrize(
    "error",
    [
        StorageEnvelopeError(StorageEnvelopeRejection.PERSISTENCE_FAILED),
        StorageEnvelopeError(StorageEnvelopeRejection.TRUST_FAILED, trust_lost=True),
    ],
)
def test_guard_restores_after_ordinary_and_trust_failures(
    error: StorageEnvelopeError,
) -> None:
    fake = _FakeStore()
    fake.responses["capacity"] = [error, _capacity()]
    orchestrator = _orchestrator(fake)
    orchestrator.process_observation(_projection())
    assert orchestrator.process_observation(_projection(2)).outcome is (
        OrchestrationOutcome.STORED
    )


def test_real_store_observation_and_replay_use_existing_ingestion_once_each(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    orchestrator = HealthHistoryOrchestrator(
        store,
        monotonic=lambda: 10.0,
        utc_now_us=lambda: _BASE_TIME,
    )
    projection = _projection()
    assert orchestrator.process_observation(projection).outcome is (
        OrchestrationOutcome.STORED
    )
    after_stored = _snapshot(path)
    assert orchestrator.process_observation(projection).outcome is (
        OrchestrationOutcome.REPLAYED
    )
    assert _snapshot(path) == after_stored
    assert len(store.list_health_samples().items) == 1
    assert orchestrator.trigger_state.stored_rows_since_maintenance == 1


def test_real_no_work_maintenance_preserves_all_logical_tables(
    store_path: tuple[Path, HealthHistoryStore],
) -> None:
    path, store = store_path
    before = _snapshot(path)
    orchestrator = HealthHistoryOrchestrator(
        store,
        monotonic=lambda: 10.0,
        utc_now_us=lambda: _BASE_TIME,
    )
    assert orchestrator.run_maintenance_opportunity().outcome is (
        OrchestrationOutcome.MAINTENANCE_COMPLETED
    )
    assert _snapshot(path) == before


def test_real_unsupported_wal_runtime_skips_write_and_keeps_store_open(
    store_path: tuple[Path, HealthHistoryStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, store = store_path
    before = _snapshot(path)
    traced: list[str] = []
    monkeypatch.setattr(envelope.sqlite3, "sqlite_version_info", (3, 51, 2))
    store._connection.set_trace_callback(traced.append)  # noqa: SLF001
    try:
        result = HealthHistoryOrchestrator(
            store,
            monotonic=lambda: 10.0,
            utc_now_us=lambda: _BASE_TIME,
        ).process_observation(_projection())
    finally:
        store._connection.set_trace_callback(None)  # noqa: SLF001
    assert result.outcome is OrchestrationOutcome.UNSUPPORTED_RUNTIME
    assert not store.closed
    assert not any("wal_checkpoint" in statement.lower() for statement in traced)
    assert _snapshot(path) == before


def test_reviewed_storage_bounds_are_unchanged() -> None:
    assert PAGE_SIZE_BYTES == 4096
    assert MAX_DATABASE_PAGES == 16_384
    assert MAX_DATABASE_BYTES == 64 * 1024 * 1024
    assert FREE_SPACE_RESERVE_BYTES == 128 * 1024 * 1024
    assert RETENTION_ROW_BUDGET == 500
    assert INCREMENTAL_VACUUM_PAGES == 128
    assert WAL_CHECKPOINT_THRESHOLD_FRAMES == 256
    assert WAL_HARD_LIMIT_FRAMES == 960
    assert WAL_HARD_LIMIT_BYTES == 4 * 1024 * 1024
    assert STORED_ROWS_MAINTENANCE_TRIGGER == 120
    assert MAINTENANCE_INTERVAL_SECONDS == 3600.0


def test_orchestration_is_absent_from_runtime_entry_points() -> None:
    root = Path(__file__).parents[1] / "src" / "aurora_core"
    entry_points = [
        root / "__main__.py",
        root / "dashboard" / "server.py",
        *sorted((root / "runtime").glob("*.py")),
    ]
    for path in entry_points:
        source = path.read_text()
        assert "health_history.orchestration" not in source
        assert "m18_validation" not in source


def test_orchestration_source_has_no_runtime_or_external_operations() -> None:
    path = (
        Path(__file__).parents[1]
        / "src"
        / "aurora_core"
        / "health_history"
        / "orchestration.py"
    )
    source = path.read_text().lower()
    prohibited = (
        "subprocess",
        "socket",
        "requests",
        "urllib",
        "http.client",
        "threading.thread",
        "create_task",
        "sleep(",
        "systemctl",
        "wal_checkpoint(full)",
        "wal_checkpoint(restart)",
        "wal_checkpoint(truncate)",
    )
    for token in prohibited:
        assert token not in source


def test_public_result_models_are_frozen() -> None:
    observation = ObservationCycleResult(OrchestrationOutcome.STORED)
    maintenance = MaintenanceOpportunityResult(
        OrchestrationOutcome.MAINTENANCE_COMPLETED
    )
    with pytest.raises(AttributeError):
        observation.outcome = OrchestrationOutcome.REPLAYED  # type: ignore[misc]
    with pytest.raises(AttributeError):
        observation.storage_maintenance_attempted = True  # type: ignore[misc]
    with pytest.raises(AttributeError):
        maintenance.outcome = OrchestrationOutcome.STORAGE_BUSY  # type: ignore[misc]
    assert replace(observation, outcome=OrchestrationOutcome.REPLAYED).outcome is (
        OrchestrationOutcome.REPLAYED
    )
    assert not observation.storage_maintenance_attempted


def test_public_models_and_constructor_reject_impossible_combinations() -> None:
    with pytest.raises(ValueError, match="invalid_observation_cycle_result"):
        ObservationCycleResult(OrchestrationOutcome.MAINTENANCE_COMPLETED)
    with pytest.raises(ValueError, match="invalid_observation_cycle_result"):
        ObservationCycleResult(
            OrchestrationOutcome.STORED,
            storage_maintenance_attempted=1,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="invalid_maintenance_opportunity_result"):
        MaintenanceOpportunityResult(OrchestrationOutcome.STORED)
    with pytest.raises(ValueError, match="invalid_maintenance_trigger_decision"):
        MaintenanceTriggerDecision(False, MaintenanceTriggerReason.STARTUP)
    fake = _FakeStore()
    with pytest.raises(ValueError, match="invalid_orchestration_clock"):
        HealthHistoryOrchestrator(
            cast(HealthHistoryStore, fake),
            monotonic=cast(Callable[[], float], None),
            utc_now_us=lambda: _BASE_TIME,
        )
    with pytest.raises(ValueError, match="invalid_maintenance_trigger_state"):
        HealthHistoryOrchestrator(
            cast(HealthHistoryStore, fake),
            monotonic=lambda: 1.0,
            utc_now_us=lambda: _BASE_TIME,
            trigger_state=cast(MaintenanceTriggerState, object()),
        )
