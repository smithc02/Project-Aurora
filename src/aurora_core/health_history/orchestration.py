"""Direct-only composition of reviewed health-history primitives."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock
from typing import Final, cast

from aurora_core.health_history.ingestion import (
    IngestionError,
    IngestionOutcome,
    IngestionRejection,
)
from aurora_core.health_history.maintenance import (
    MaintenanceError,
    MaintenanceRejection,
)
from aurora_core.health_history.models import MAX_BOUNDED_COUNTER, MAX_TIMESTAMP_US
from aurora_core.health_history.projection import HealthProjection
from aurora_core.health_history.storage_envelope import (
    PassiveCheckpointOutcome,
    StorageDecisionOutcome,
    StorageEnvelopeError,
    StorageEnvelopeRejection,
    decide_storage_action,
)
from aurora_core.health_history.store import HealthHistoryStore, StoreError

MAINTENANCE_INTERVAL_SECONDS: Final = 60.0 * 60.0
STORED_ROWS_MAINTENANCE_TRIGGER: Final = 120


class OrchestrationOutcome(StrEnum):
    STORED = "stored"
    STATE_ONLY = "state_only"
    REPLAYED = "replayed"
    CAPACITY_BLOCKED = "capacity_blocked"
    WAL_OVERSIZE_BLOCKED = "wal_oversize_blocked"
    CHECKPOINT_BUSY = "checkpoint_busy"
    CHECKPOINT_INCOMPLETE = "checkpoint_incomplete"
    STORAGE_BUSY = "storage_busy"
    TIMED_OUT = "timed_out"
    PERSISTENCE_FAILED = "persistence_failed"
    TRUST_FAILED = "trust_failed"
    UNSUPPORTED_RUNTIME = "unsupported_runtime"
    INVALID_OBSERVATION = "invalid_observation"
    STALE_SEQUENCE = "stale_sequence"
    SEQUENCE_CONFLICT = "sequence_conflict"
    GENERATION_EXHAUSTED = "generation_exhausted"
    INVALID_CLOCK = "invalid_clock"
    REENTRANT = "reentrant"
    MAINTENANCE_COMPLETED = "maintenance_completed"


class OrchestrationRejection(StrEnum):
    INVALID_TRIGGER_STATE = "invalid_trigger_state"
    INVALID_MONOTONIC = "invalid_monotonic"
    REENTRANT = "reentrant"


class MaintenanceTriggerReason(StrEnum):
    NONE = "none"
    STARTUP = "startup"
    HOURLY = "hourly"
    STORED_ROWS = "stored_rows"


class OrchestrationError(Exception):
    """Fixed orchestration-model rejection without private context."""

    def __init__(self, reason: OrchestrationRejection) -> None:
        super().__init__(reason.value)
        self.reason = reason


_OBSERVATION_OUTCOMES: Final = frozenset(
    {
        OrchestrationOutcome.STORED,
        OrchestrationOutcome.STATE_ONLY,
        OrchestrationOutcome.REPLAYED,
        OrchestrationOutcome.CAPACITY_BLOCKED,
        OrchestrationOutcome.WAL_OVERSIZE_BLOCKED,
        OrchestrationOutcome.CHECKPOINT_BUSY,
        OrchestrationOutcome.CHECKPOINT_INCOMPLETE,
        OrchestrationOutcome.STORAGE_BUSY,
        OrchestrationOutcome.TIMED_OUT,
        OrchestrationOutcome.PERSISTENCE_FAILED,
        OrchestrationOutcome.TRUST_FAILED,
        OrchestrationOutcome.UNSUPPORTED_RUNTIME,
        OrchestrationOutcome.INVALID_OBSERVATION,
        OrchestrationOutcome.STALE_SEQUENCE,
        OrchestrationOutcome.SEQUENCE_CONFLICT,
        OrchestrationOutcome.GENERATION_EXHAUSTED,
        OrchestrationOutcome.INVALID_CLOCK,
        OrchestrationOutcome.REENTRANT,
    }
)
_MAINTENANCE_OUTCOMES: Final = frozenset(
    {
        OrchestrationOutcome.MAINTENANCE_COMPLETED,
        OrchestrationOutcome.WAL_OVERSIZE_BLOCKED,
        OrchestrationOutcome.CHECKPOINT_BUSY,
        OrchestrationOutcome.STORAGE_BUSY,
        OrchestrationOutcome.TIMED_OUT,
        OrchestrationOutcome.PERSISTENCE_FAILED,
        OrchestrationOutcome.TRUST_FAILED,
        OrchestrationOutcome.UNSUPPORTED_RUNTIME,
        OrchestrationOutcome.INVALID_CLOCK,
        OrchestrationOutcome.REENTRANT,
    }
)


@dataclass(frozen=True, slots=True)
class ObservationCycleResult:
    outcome: OrchestrationOutcome

    def __post_init__(self) -> None:
        if self.outcome not in _OBSERVATION_OUTCOMES:
            raise ValueError("invalid_observation_cycle_result")


@dataclass(frozen=True, slots=True)
class MaintenanceOpportunityResult:
    outcome: OrchestrationOutcome

    def __post_init__(self) -> None:
        if self.outcome not in _MAINTENANCE_OUTCOMES:
            raise ValueError("invalid_maintenance_opportunity_result")


@dataclass(frozen=True, slots=True)
class MaintenanceTriggerDecision:
    due: bool
    reason: MaintenanceTriggerReason

    def __post_init__(self) -> None:
        if (
            type(self.due) is not bool
            or not isinstance(self.reason, MaintenanceTriggerReason)
            or self.due != (self.reason is not MaintenanceTriggerReason.NONE)
        ):
            raise ValueError("invalid_maintenance_trigger_decision")


@dataclass(frozen=True, slots=True)
class MaintenanceTriggerState:
    startup_maintenance_completed: bool = False
    last_completed_monotonic: float | None = None
    stored_rows_since_maintenance: int = 0

    def __post_init__(self) -> None:
        marker = self.last_completed_monotonic
        if (
            type(self.startup_maintenance_completed) is not bool
            or type(self.stored_rows_since_maintenance) is not int
            or not 0 <= self.stored_rows_since_maintenance <= MAX_BOUNDED_COUNTER
            or (self.startup_maintenance_completed != (marker is not None))
            or (marker is not None and not _is_valid_monotonic(marker))
        ):
            raise ValueError("invalid_maintenance_trigger_state")

    def decision(self, now_monotonic: object) -> MaintenanceTriggerDecision:
        """Return the fixed-priority trigger decision without side effects."""
        now = _validated_monotonic(now_monotonic)
        marker = self.last_completed_monotonic
        if marker is not None and now < marker:
            raise OrchestrationError(OrchestrationRejection.INVALID_MONOTONIC)
        if not self.startup_maintenance_completed:
            reason = MaintenanceTriggerReason.STARTUP
        elif self.stored_rows_since_maintenance >= STORED_ROWS_MAINTENANCE_TRIGGER:
            reason = MaintenanceTriggerReason.STORED_ROWS
        elif marker is not None and now - marker >= MAINTENANCE_INTERVAL_SECONDS:
            reason = MaintenanceTriggerReason.HOURLY
        else:
            reason = MaintenanceTriggerReason.NONE
        return MaintenanceTriggerDecision(
            due=reason is not MaintenanceTriggerReason.NONE,
            reason=reason,
        )

    def after_observation(
        self, result: ObservationCycleResult
    ) -> MaintenanceTriggerState:
        """Saturatingly count only observations that stored history."""
        if type(result) is not ObservationCycleResult:
            raise OrchestrationError(OrchestrationRejection.INVALID_TRIGGER_STATE)
        if result.outcome is not OrchestrationOutcome.STORED:
            return self
        return MaintenanceTriggerState(
            startup_maintenance_completed=self.startup_maintenance_completed,
            last_completed_monotonic=self.last_completed_monotonic,
            stored_rows_since_maintenance=min(
                self.stored_rows_since_maintenance + 1,
                MAX_BOUNDED_COUNTER,
            ),
        )

    def after_maintenance(
        self,
        result: MaintenanceOpportunityResult,
        *,
        started_monotonic: object,
        completed_monotonic: object,
    ) -> MaintenanceTriggerState:
        """Reset triggers only after a successful bounded opportunity."""
        if type(result) is not MaintenanceOpportunityResult:
            raise OrchestrationError(OrchestrationRejection.INVALID_TRIGGER_STATE)
        if result.outcome is not OrchestrationOutcome.MAINTENANCE_COMPLETED:
            return self
        started = _validated_monotonic(started_monotonic)
        completed = _validated_monotonic(completed_monotonic)
        if completed < started or (
            self.last_completed_monotonic is not None
            and completed < self.last_completed_monotonic
        ):
            raise OrchestrationError(OrchestrationRejection.INVALID_MONOTONIC)
        return MaintenanceTriggerState(
            startup_maintenance_completed=True,
            last_completed_monotonic=completed,
            stored_rows_since_maintenance=0,
        )


class HealthHistoryOrchestrator:
    """Single-cycle, non-retrying composition over one injected open store."""

    def __init__(
        self,
        store: HealthHistoryStore,
        *,
        monotonic: Callable[[], float],
        utc_now_us: Callable[[], int],
        trigger_state: MaintenanceTriggerState | None = None,
    ) -> None:
        if not callable(monotonic) or not callable(utc_now_us):
            raise ValueError("invalid_orchestration_clock")
        if (
            trigger_state is not None
            and type(trigger_state) is not MaintenanceTriggerState
        ):
            raise ValueError("invalid_maintenance_trigger_state")
        self._store = store
        self._monotonic = monotonic
        self._utc_now_us = utc_now_us
        self._trigger_state = trigger_state or MaintenanceTriggerState()
        self._cycle_guard = Lock()

    @property
    def trigger_state(self) -> MaintenanceTriggerState:
        return self._trigger_state

    def maintenance_trigger(self) -> MaintenanceTriggerDecision:
        """Evaluate startup/hourly/stored-row policy without scheduling it."""
        if not self._cycle_guard.acquire(blocking=False):
            raise OrchestrationError(OrchestrationRejection.REENTRANT)
        try:
            return self._trigger_state.decision(self._read_monotonic())
        finally:
            self._cycle_guard.release()

    def process_observation(
        self, projection: HealthProjection
    ) -> ObservationCycleResult:
        """Run one storage-safe observation opportunity without retry."""
        if not self._cycle_guard.acquire(blocking=False):
            return ObservationCycleResult(OrchestrationOutcome.REENTRANT)
        try:
            result = self._process_observation(projection)
            self._trigger_state = self._trigger_state.after_observation(result)
            return result
        finally:
            self._cycle_guard.release()

    def run_maintenance_opportunity(self) -> MaintenanceOpportunityResult:
        """Run one direct bounded maintenance opportunity without scheduling."""
        if not self._cycle_guard.acquire(blocking=False):
            return MaintenanceOpportunityResult(OrchestrationOutcome.REENTRANT)
        try:
            try:
                started_monotonic = self._read_monotonic()
            except OrchestrationError:
                return MaintenanceOpportunityResult(OrchestrationOutcome.INVALID_CLOCK)
            result = self._run_maintenance_opportunity()
            if result.outcome is OrchestrationOutcome.MAINTENANCE_COMPLETED:
                try:
                    completed_monotonic = self._read_monotonic()
                    self._trigger_state = self._trigger_state.after_maintenance(
                        result,
                        started_monotonic=started_monotonic,
                        completed_monotonic=completed_monotonic,
                    )
                except OrchestrationError:
                    return MaintenanceOpportunityResult(
                        OrchestrationOutcome.INVALID_CLOCK
                    )
            return result
        finally:
            self._cycle_guard.release()

    def _process_observation(
        self, projection: HealthProjection
    ) -> ObservationCycleResult:
        attempted_capacity_maintenance = False
        decision = self._inspect_storage(attempted=False)
        if isinstance(decision, ObservationCycleResult):
            return decision
        if decision is StorageDecisionOutcome.WAL_OVERSIZE_BLOCKED:
            return ObservationCycleResult(OrchestrationOutcome.WAL_OVERSIZE_BLOCKED)
        if decision is StorageDecisionOutcome.CAPACITY_MAINTENANCE_REQUIRED:
            try:
                now_utc_us = self._read_utc_now_us()
            except OrchestrationError:
                return ObservationCycleResult(OrchestrationOutcome.INVALID_CLOCK)
            try:
                self._store.cleanup_retention(now_utc_us=now_utc_us)
                self._store.incremental_vacuum()
            except MaintenanceError as error:
                return ObservationCycleResult(_maintenance_error_outcome(error))
            except StoreError:
                return ObservationCycleResult(OrchestrationOutcome.TRUST_FAILED)
            attempted_capacity_maintenance = True
            decision = self._inspect_storage(attempted=True)
            if isinstance(decision, ObservationCycleResult):
                return decision
        return self._finish_observation_decision(
            projection,
            decision,
            capacity_maintenance_attempted=attempted_capacity_maintenance,
        )

    def _finish_observation_decision(
        self,
        projection: HealthProjection,
        decision: StorageDecisionOutcome,
        *,
        capacity_maintenance_attempted: bool,
    ) -> ObservationCycleResult:
        if decision in {
            StorageDecisionOutcome.CAPACITY_BLOCKED,
            StorageDecisionOutcome.CAPACITY_MAINTENANCE_REQUIRED,
        }:
            return ObservationCycleResult(OrchestrationOutcome.CAPACITY_BLOCKED)
        if decision is StorageDecisionOutcome.WAL_OVERSIZE_BLOCKED:
            return ObservationCycleResult(OrchestrationOutcome.WAL_OVERSIZE_BLOCKED)
        if decision is StorageDecisionOutcome.WAL_CHECKPOINT_DUE:
            checkpoint = self._checkpoint_before_ingestion()
            if isinstance(checkpoint, ObservationCycleResult):
                return checkpoint
            post_checkpoint_decision = self._inspect_storage(
                attempted=capacity_maintenance_attempted
            )
            if isinstance(post_checkpoint_decision, ObservationCycleResult):
                return post_checkpoint_decision
            if post_checkpoint_decision is StorageDecisionOutcome.WAL_CHECKPOINT_DUE:
                return ObservationCycleResult(
                    OrchestrationOutcome.CHECKPOINT_INCOMPLETE
                )
            if post_checkpoint_decision in {
                StorageDecisionOutcome.CAPACITY_BLOCKED,
                StorageDecisionOutcome.CAPACITY_MAINTENANCE_REQUIRED,
            }:
                return ObservationCycleResult(OrchestrationOutcome.CAPACITY_BLOCKED)
            if post_checkpoint_decision is StorageDecisionOutcome.WAL_OVERSIZE_BLOCKED:
                return ObservationCycleResult(OrchestrationOutcome.WAL_OVERSIZE_BLOCKED)
        return self._ingest_once(projection)

    def _inspect_storage(
        self, *, attempted: bool
    ) -> StorageDecisionOutcome | ObservationCycleResult:
        try:
            capacity = self._store.inspect_storage_capacity()
            free_space = self._store.inspect_free_space()
            wal = self._store.inspect_wal()
            return decide_storage_action(
                capacity,
                free_space,
                wal,
                capacity_maintenance_attempted=attempted,
            ).outcome
        except StorageEnvelopeError as error:
            return ObservationCycleResult(_storage_error_outcome(error))
        except StoreError:
            return ObservationCycleResult(OrchestrationOutcome.TRUST_FAILED)
        except (TypeError, ValueError):
            return ObservationCycleResult(OrchestrationOutcome.TRUST_FAILED)

    def _checkpoint_before_ingestion(
        self,
    ) -> None | ObservationCycleResult:
        try:
            checkpoint = self._store.passive_wal_checkpoint()
        except StorageEnvelopeError as error:
            return ObservationCycleResult(
                _storage_error_outcome(error, checkpoint=True)
            )
        except StoreError:
            return ObservationCycleResult(OrchestrationOutcome.TRUST_FAILED)
        if checkpoint.outcome is PassiveCheckpointOutcome.BUSY:
            return ObservationCycleResult(OrchestrationOutcome.CHECKPOINT_BUSY)
        if checkpoint.outcome is PassiveCheckpointOutcome.OVERSIZE_BLOCKED:
            return ObservationCycleResult(OrchestrationOutcome.WAL_OVERSIZE_BLOCKED)
        return None

    def _ingest_once(self, projection: HealthProjection) -> ObservationCycleResult:
        try:
            result = self._store.ingest(projection)
        except IngestionError as error:
            return ObservationCycleResult(_ingestion_error_outcome(error))
        except StoreError:
            return ObservationCycleResult(OrchestrationOutcome.TRUST_FAILED)
        outcome = {
            IngestionOutcome.REPLAYED: OrchestrationOutcome.REPLAYED,
            IngestionOutcome.STATE_ONLY: OrchestrationOutcome.STATE_ONLY,
            IngestionOutcome.TRANSITION_STORED: OrchestrationOutcome.STORED,
            IngestionOutcome.HEARTBEAT_STORED: OrchestrationOutcome.STORED,
            IngestionOutcome.STARTUP_MARKER_STORED: OrchestrationOutcome.STORED,
            IngestionOutcome.CLOCK_MARKER_STORED: OrchestrationOutcome.STORED,
        }[result.outcome]
        return ObservationCycleResult(outcome)

    def _run_maintenance_opportunity(self) -> MaintenanceOpportunityResult:
        try:
            now_utc_us = self._read_utc_now_us()
        except OrchestrationError:
            return MaintenanceOpportunityResult(OrchestrationOutcome.INVALID_CLOCK)
        try:
            self._store.cleanup_retention(now_utc_us=now_utc_us)
            self._store.incremental_vacuum()
        except MaintenanceError as error:
            return MaintenanceOpportunityResult(_maintenance_error_outcome(error))
        except StoreError:
            return MaintenanceOpportunityResult(OrchestrationOutcome.TRUST_FAILED)
        try:
            wal = self._store.inspect_wal()
        except StorageEnvelopeError as error:
            return MaintenanceOpportunityResult(_storage_error_outcome(error))
        except StoreError:
            return MaintenanceOpportunityResult(OrchestrationOutcome.TRUST_FAILED)
        if wal.oversize:
            return MaintenanceOpportunityResult(
                OrchestrationOutcome.WAL_OVERSIZE_BLOCKED
            )
        if wal.checkpoint_due:
            try:
                checkpoint = self._store.passive_wal_checkpoint()
            except StorageEnvelopeError as error:
                return MaintenanceOpportunityResult(
                    _storage_error_outcome(error, checkpoint=True)
                )
            except StoreError:
                return MaintenanceOpportunityResult(OrchestrationOutcome.TRUST_FAILED)
            if checkpoint.outcome is PassiveCheckpointOutcome.BUSY:
                return MaintenanceOpportunityResult(
                    OrchestrationOutcome.CHECKPOINT_BUSY
                )
            if checkpoint.outcome is PassiveCheckpointOutcome.OVERSIZE_BLOCKED:
                return MaintenanceOpportunityResult(
                    OrchestrationOutcome.WAL_OVERSIZE_BLOCKED
                )
        return MaintenanceOpportunityResult(OrchestrationOutcome.MAINTENANCE_COMPLETED)

    def _read_monotonic(self) -> float:
        try:
            value: object = self._monotonic()
        except Exception:
            raise OrchestrationError(OrchestrationRejection.INVALID_MONOTONIC) from None
        return _validated_monotonic(value)

    def _read_utc_now_us(self) -> int:
        try:
            value: object = self._utc_now_us()
        except Exception:
            raise OrchestrationError(
                OrchestrationRejection.INVALID_TRIGGER_STATE
            ) from None
        if type(value) is not int or not 0 <= value <= MAX_TIMESTAMP_US:
            raise OrchestrationError(OrchestrationRejection.INVALID_TRIGGER_STATE)
        return value


def _is_valid_monotonic(value: object) -> bool:
    return (
        type(value) is float
        and math.isfinite(value)
        and 0.0 <= value <= float(MAX_TIMESTAMP_US)
    )


def _validated_monotonic(value: object) -> float:
    if not _is_valid_monotonic(value):
        raise OrchestrationError(OrchestrationRejection.INVALID_MONOTONIC)
    return cast(float, value)


def _storage_error_outcome(
    error: StorageEnvelopeError,
    *,
    checkpoint: bool = False,
) -> OrchestrationOutcome:
    if error.trust_lost or error.reason in {
        StorageEnvelopeRejection.MALFORMED_STATE,
        StorageEnvelopeRejection.TRUST_FAILED,
    }:
        return OrchestrationOutcome.TRUST_FAILED
    if error.reason is StorageEnvelopeRejection.UNSUPPORTED_RUNTIME:
        return OrchestrationOutcome.UNSUPPORTED_RUNTIME
    if error.reason is StorageEnvelopeRejection.STORAGE_BUSY:
        return (
            OrchestrationOutcome.CHECKPOINT_BUSY
            if checkpoint
            else OrchestrationOutcome.STORAGE_BUSY
        )
    if error.reason is StorageEnvelopeRejection.TIMED_OUT:
        return OrchestrationOutcome.TIMED_OUT
    return OrchestrationOutcome.PERSISTENCE_FAILED


def _maintenance_error_outcome(error: MaintenanceError) -> OrchestrationOutcome:
    if error.trust_lost or error.reason in {
        MaintenanceRejection.MALFORMED_STATE,
        MaintenanceRejection.TRUST_FAILED,
    }:
        return OrchestrationOutcome.TRUST_FAILED
    if error.reason is MaintenanceRejection.STORAGE_BUSY:
        return OrchestrationOutcome.STORAGE_BUSY
    if error.reason is MaintenanceRejection.TIMED_OUT:
        return OrchestrationOutcome.TIMED_OUT
    return OrchestrationOutcome.PERSISTENCE_FAILED


def _ingestion_error_outcome(error: IngestionError) -> OrchestrationOutcome:
    if error.trust_lost or error.reason in {
        IngestionRejection.MALFORMED_STATE,
        IngestionRejection.TRUST_FAILED,
    }:
        return OrchestrationOutcome.TRUST_FAILED
    return {
        IngestionRejection.INVALID_PROJECTION: OrchestrationOutcome.INVALID_OBSERVATION,
        IngestionRejection.STALE_SEQUENCE: OrchestrationOutcome.STALE_SEQUENCE,
        IngestionRejection.SEQUENCE_CONFLICT: OrchestrationOutcome.SEQUENCE_CONFLICT,
        IngestionRejection.GENERATION_EXHAUSTED: (
            OrchestrationOutcome.GENERATION_EXHAUSTED
        ),
        IngestionRejection.STORAGE_BUSY: OrchestrationOutcome.STORAGE_BUSY,
        IngestionRejection.PERSISTENCE_FAILED: OrchestrationOutcome.PERSISTENCE_FAILED,
    }[error.reason]
