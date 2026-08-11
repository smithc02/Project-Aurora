"""Direct-only scheduler and restart-resume composition for health history."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from threading import Lock
from typing import TYPE_CHECKING, Final, cast

from aurora_core.dashboard.models import HealthReport
from aurora_core.health_history.models import (
    MAX_BOUNDED_COUNTER,
    MAX_OBSERVATION_SEQUENCE,
    MAX_TIMESTAMP_US,
    SampleKind,
)
from aurora_core.health_history.orchestration import (
    HealthHistoryOrchestrator,
    MaintenanceOpportunityResult,
    MaintenanceTriggerDecision,
    ObservationCycleResult,
    OrchestrationError,
    OrchestrationOutcome,
    OrchestrationRejection,
)
from aurora_core.health_history.projection import (
    HealthProjection,
    ProjectionError,
    project_health_report,
)
from aurora_core.health_history.queries import (
    QueryError,
    QueryRejection,
    SchedulerResumeState,
)
from aurora_core.health_history.store import StoreError

if TYPE_CHECKING:
    from aurora_core.health_history.store import HealthHistoryStore

DEFAULT_SAMPLE_INTERVAL_SECONDS: Final = 30
MIN_SAMPLE_INTERVAL_SECONDS: Final = 5
MAX_SAMPLE_INTERVAL_SECONDS: Final = 300
DEFAULT_DASHBOARD_REFRESH_SECONDS: Final = 5


class SchedulerOutcome(StrEnum):
    """Fixed scheduler result registry without health or storage details."""

    NOT_DUE = "not_due"
    STORED = "stored"
    STATE_ONLY = "state_only"
    REPLAYED = "replayed"
    COLLECTION_FAILED = "collection_failed"
    PROJECTION_FAILED = "projection_failed"
    SEQUENCE_EXHAUSTED = "sequence_exhausted"
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


class SchedulerRejection(StrEnum):
    """Fixed construction and pure-model rejections."""

    INVALID_CONFIGURATION = "invalid_configuration"
    INVALID_CLOCK = "invalid_clock"
    INVALID_STATE = "invalid_state"
    STORAGE_BUSY = "storage_busy"
    PERSISTENCE_FAILED = "persistence_failed"
    TRUST_FAILED = "trust_failed"
    REENTRANT = "reentrant"


class SchedulerError(Exception):
    """Sanitized scheduler error with no health, SQL, path, or clock detail."""

    def __init__(self, reason: SchedulerRejection, *, trust_lost: bool = False) -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.trust_lost = trust_lost


@dataclass(frozen=True, slots=True)
class SchedulerDueDecision:
    """One pure monotonic cadence decision and its bounded missed count."""

    due: bool
    missed_intervals: int
    checked_monotonic: float
    next_due_monotonic: float

    def __post_init__(self) -> None:
        if (
            type(self.due) is not bool
            or type(self.missed_intervals) is not int
            or not 0 <= self.missed_intervals <= MAX_BOUNDED_COUNTER
            or not _is_valid_monotonic(self.checked_monotonic)
            or not _is_valid_monotonic(self.next_due_monotonic)
            or (not self.due and self.missed_intervals != 0)
        ):
            raise ValueError("invalid_scheduler_due_decision")


@dataclass(frozen=True, slots=True)
class SchedulerCadenceState:
    """Immutable in-process cadence anchor and volatile missed evidence."""

    planned_due_monotonic: float
    last_seen_monotonic: float
    pending_missed_intervals: int = 0

    def __post_init__(self) -> None:
        if (
            not _is_valid_monotonic(self.planned_due_monotonic)
            or not _is_valid_monotonic(self.last_seen_monotonic)
            or type(self.pending_missed_intervals) is not int
            or not 0 <= self.pending_missed_intervals <= MAX_BOUNDED_COUNTER
        ):
            raise ValueError("invalid_scheduler_cadence_state")

    def decision(
        self,
        now_monotonic: object,
        *,
        sample_interval_seconds: object,
    ) -> SchedulerDueDecision:
        """Evaluate one deadline without consuming it or looping over misses."""
        interval = _validated_sample_interval(sample_interval_seconds)
        now = _validated_monotonic(now_monotonic)
        if now < self.last_seen_monotonic:
            raise SchedulerError(SchedulerRejection.INVALID_CLOCK)
        if now < self.planned_due_monotonic:
            return SchedulerDueDecision(
                False,
                0,
                now,
                self.planned_due_monotonic,
            )
        elapsed_intervals = math.floor((now - self.planned_due_monotonic) / interval)
        missed = min(elapsed_intervals, MAX_BOUNDED_COUNTER)
        next_due = self.planned_due_monotonic + (elapsed_intervals + 1) * interval
        if not _is_valid_monotonic(float(next_due)) or next_due <= now:
            raise SchedulerError(SchedulerRejection.INVALID_CLOCK)
        return SchedulerDueDecision(True, missed, now, float(next_due))

    def after_check(
        self,
        decision: SchedulerDueDecision,
        *,
        consume_due: bool,
        pending_missed_intervals: int | None = None,
    ) -> SchedulerCadenceState:
        """Return the next immutable state for one validated clock check."""
        if type(decision) is not SchedulerDueDecision or type(consume_due) is not bool:
            raise SchedulerError(SchedulerRejection.INVALID_STATE)
        if (
            decision.checked_monotonic < self.last_seen_monotonic
            or (consume_due and not decision.due)
            or (
                decision.due
                and (
                    decision.checked_monotonic < self.planned_due_monotonic
                    or decision.next_due_monotonic <= decision.checked_monotonic
                )
            )
            or (
                not decision.due
                and (
                    decision.checked_monotonic >= self.planned_due_monotonic
                    or decision.next_due_monotonic != self.planned_due_monotonic
                )
            )
        ):
            raise SchedulerError(SchedulerRejection.INVALID_STATE)
        pending = (
            self.pending_missed_intervals
            if pending_missed_intervals is None
            else pending_missed_intervals
        )
        if type(pending) is not int or not 0 <= pending <= MAX_BOUNDED_COUNTER:
            raise SchedulerError(SchedulerRejection.INVALID_STATE)
        return SchedulerCadenceState(
            planned_due_monotonic=(
                decision.next_due_monotonic
                if consume_due
                else self.planned_due_monotonic
            ),
            last_seen_monotonic=decision.checked_monotonic,
            pending_missed_intervals=pending,
        )


@dataclass(frozen=True, slots=True)
class SchedulerResult:
    """One sanitized direct scheduler-call result."""

    outcome: SchedulerOutcome
    observation_sequence: int | None = None
    missed_intervals: int = 0
    sampling_outcome: SchedulerOutcome | None = None
    maintenance_outcome: OrchestrationOutcome | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, SchedulerOutcome):
            raise ValueError("invalid_scheduler_result")
        if self.observation_sequence is not None and (
            type(self.observation_sequence) is not int
            or not 1 <= self.observation_sequence <= MAX_OBSERVATION_SEQUENCE
        ):
            raise ValueError("invalid_scheduler_result")
        if (
            type(self.missed_intervals) is not int
            or not 0 <= self.missed_intervals <= MAX_BOUNDED_COUNTER
            or (
                self.sampling_outcome is not None
                and not isinstance(self.sampling_outcome, SchedulerOutcome)
            )
            or (
                self.maintenance_outcome is not None
                and not isinstance(self.maintenance_outcome, OrchestrationOutcome)
            )
        ):
            raise ValueError("invalid_scheduler_result")


class HealthHistoryScheduler:
    """One-at-a-time direct scheduler over injected clocks and health service."""

    def __init__(
        self,
        store: HealthHistoryStore,
        orchestrator: HealthHistoryOrchestrator,
        *,
        health_report_supplier: Callable[[], HealthReport],
        monotonic: Callable[[], float],
        utc_now: Callable[[], datetime],
        sample_interval_seconds: int = DEFAULT_SAMPLE_INTERVAL_SECONDS,
        dashboard_refresh_seconds: int = DEFAULT_DASHBOARD_REFRESH_SECONDS,
    ) -> None:
        _validate_scheduler_intervals(
            sample_interval_seconds,
            dashboard_refresh_seconds,
        )
        if not all(
            callable(value) for value in (health_report_supplier, monotonic, utc_now)
        ):
            raise SchedulerError(SchedulerRejection.INVALID_CONFIGURATION)
        self._store = store
        self._orchestrator = orchestrator
        self._health_report_supplier = health_report_supplier
        self._monotonic = monotonic
        self._utc_now = utc_now
        self._sample_interval_seconds = sample_interval_seconds
        self._resume_state = self._read_resume_state()
        started = self._read_monotonic()
        self._cadence_state = SchedulerCadenceState(started, started)
        self._next_sequence = _next_sequence(self._resume_state)
        self._startup_marker_pending = True
        self._cycle_guard = Lock()

    @property
    def resume_state(self) -> SchedulerResumeState:
        return self._resume_state

    @property
    def cadence_state(self) -> SchedulerCadenceState:
        return self._cadence_state

    @property
    def next_observation_sequence(self) -> int | None:
        return self._next_sequence

    def is_due(self) -> SchedulerDueDecision:
        """Read one monotonic value and return a non-consuming due decision."""
        if not self._cycle_guard.acquire(blocking=False):
            raise SchedulerError(SchedulerRejection.REENTRANT)
        try:
            decision = self._cadence_state.decision(
                self._read_monotonic(),
                sample_interval_seconds=self._sample_interval_seconds,
            )
            self._cadence_state = self._cadence_state.after_check(
                decision,
                consume_due=False,
            )
            return decision
        finally:
            self._cycle_guard.release()

    def run_due_opportunity(self) -> SchedulerResult:
        """Run at most one collection, projection, orchestration, and maintenance."""
        if not self._cycle_guard.acquire(blocking=False):
            return SchedulerResult(SchedulerOutcome.REENTRANT)
        try:
            return self._run_due_opportunity()
        finally:
            self._cycle_guard.release()

    def _run_due_opportunity(self) -> SchedulerResult:
        if self._next_sequence is None:
            return SchedulerResult(SchedulerOutcome.SEQUENCE_EXHAUSTED)
        previous_cadence = self._cadence_state
        try:
            decision = previous_cadence.decision(
                self._read_monotonic(),
                sample_interval_seconds=self._sample_interval_seconds,
            )
        except SchedulerError:
            return SchedulerResult(SchedulerOutcome.INVALID_CLOCK)
        if not decision.due:
            self._cadence_state = previous_cadence.after_check(
                decision,
                consume_due=False,
            )
            return SchedulerResult(SchedulerOutcome.NOT_DUE)
        cumulative_missed = min(
            previous_cadence.pending_missed_intervals + decision.missed_intervals,
            MAX_BOUNDED_COUNTER,
        )
        self._cadence_state = previous_cadence.after_check(
            decision,
            consume_due=True,
            pending_missed_intervals=cumulative_missed,
        )
        refresh_failure = self._refresh_resume_state()
        if refresh_failure is not None:
            self._record_failed_sampling()
            return SchedulerResult(
                refresh_failure,
                missed_intervals=cumulative_missed,
                sampling_outcome=refresh_failure,
            )
        sequence = self._next_sequence
        if sequence is None:
            return SchedulerResult(SchedulerOutcome.SEQUENCE_EXHAUSTED)
        try:
            recorded_at, recorded_at_utc_us = self._read_utc_now()
        except SchedulerError:
            self._record_failed_sampling()
            return SchedulerResult(
                SchedulerOutcome.INVALID_CLOCK,
                observation_sequence=sequence,
                missed_intervals=cumulative_missed,
                sampling_outcome=SchedulerOutcome.INVALID_CLOCK,
            )
        sample_kind = self._sample_kind(recorded_at_utc_us)
        reported_missed = (
            0 if sample_kind is SampleKind.CLOCK_DISCONTINUITY else cumulative_missed
        )
        try:
            report = self._health_report_supplier()
        except Exception:
            self._record_failed_sampling()
            return self._finish_with_maintenance(
                SchedulerResult(
                    SchedulerOutcome.COLLECTION_FAILED,
                    observation_sequence=sequence,
                    missed_intervals=reported_missed,
                    sampling_outcome=SchedulerOutcome.COLLECTION_FAILED,
                )
            )
        try:
            projection = project_health_report(
                report,
                observation_sequence=sequence,
                recorded_at=recorded_at,
                sample_kind=sample_kind,
                missed_intervals=reported_missed,
            )
        except ProjectionError:
            self._record_failed_sampling()
            return self._finish_with_maintenance(
                SchedulerResult(
                    SchedulerOutcome.PROJECTION_FAILED,
                    observation_sequence=sequence,
                    missed_intervals=reported_missed,
                    sampling_outcome=SchedulerOutcome.PROJECTION_FAILED,
                )
            )
        observation = self._process_observation(projection)
        outcome = _scheduler_outcome(observation.outcome)
        result = SchedulerResult(
            outcome,
            observation_sequence=sequence,
            missed_intervals=reported_missed,
            sampling_outcome=outcome,
        )
        if observation.outcome in _ACCEPTED_OBSERVATION_OUTCOMES:
            self._record_accepted_observation(projection)
            if observation.storage_maintenance_attempted:
                return result
            return self._finish_with_maintenance(result)
        self._record_failed_sampling()
        return result

    def _process_observation(
        self, projection: HealthProjection
    ) -> ObservationCycleResult:
        try:
            result = self._orchestrator.process_observation(projection)
        except Exception:
            return ObservationCycleResult(OrchestrationOutcome.TRUST_FAILED)
        if type(result) is not ObservationCycleResult:
            return ObservationCycleResult(OrchestrationOutcome.TRUST_FAILED)
        return result

    def _finish_with_maintenance(self, result: SchedulerResult) -> SchedulerResult:
        try:
            decision = self._orchestrator.maintenance_trigger()
        except OrchestrationError as error:
            outcome = (
                SchedulerOutcome.REENTRANT
                if error.reason is OrchestrationRejection.REENTRANT
                else SchedulerOutcome.INVALID_CLOCK
                if error.reason is OrchestrationRejection.INVALID_MONOTONIC
                else SchedulerOutcome.TRUST_FAILED
            )
            return replace(result, outcome=outcome)
        except Exception:
            return replace(result, outcome=SchedulerOutcome.TRUST_FAILED)
        if type(decision) is not MaintenanceTriggerDecision:
            return replace(result, outcome=SchedulerOutcome.TRUST_FAILED)
        if not decision.due:
            return result
        try:
            maintenance = self._orchestrator.run_maintenance_opportunity()
        except Exception:
            return replace(result, outcome=SchedulerOutcome.TRUST_FAILED)
        if type(maintenance) is not MaintenanceOpportunityResult:
            return replace(result, outcome=SchedulerOutcome.TRUST_FAILED)
        if maintenance.outcome is OrchestrationOutcome.MAINTENANCE_COMPLETED:
            return replace(result, maintenance_outcome=maintenance.outcome)
        return replace(
            result,
            outcome=_scheduler_outcome(maintenance.outcome),
            maintenance_outcome=maintenance.outcome,
        )

    def _sample_kind(self, recorded_at_utc_us: int) -> SampleKind:
        previous_observed = self._resume_state.last_accepted_observed_at_utc_us
        if previous_observed is not None and recorded_at_utc_us < previous_observed:
            return SampleKind.CLOCK_DISCONTINUITY
        if self._startup_marker_pending:
            return SampleKind.STARTUP_GAP
        return SampleKind.HEARTBEAT

    def _record_accepted_observation(
        self,
        projection: HealthProjection,
    ) -> None:
        count = min(
            self._resume_state.accepted_observation_count + 1,
            MAX_BOUNDED_COUNTER,
        )
        self._resume_state = SchedulerResumeState(
            projection.observation_sequence,
            projection.observed_at_utc_us,
            projection.sample_kind,
            count,
        )
        self._next_sequence = (
            None
            if projection.observation_sequence == MAX_OBSERVATION_SEQUENCE
            else projection.observation_sequence + 1
        )
        self._startup_marker_pending = False
        pending = (
            self._cadence_state.pending_missed_intervals
            if projection.sample_kind is SampleKind.CLOCK_DISCONTINUITY
            else 0
        )
        self._cadence_state = replace(
            self._cadence_state,
            pending_missed_intervals=pending,
        )

    def _record_failed_sampling(self) -> None:
        self._cadence_state = replace(
            self._cadence_state,
            pending_missed_intervals=min(
                self._cadence_state.pending_missed_intervals + 1,
                MAX_BOUNDED_COUNTER,
            ),
        )

    def _refresh_resume_state(self) -> SchedulerOutcome | None:
        try:
            refreshed = self._read_resume_state()
        except SchedulerError as error:
            return _scheduler_outcome_from_rejection(error.reason)
        previous = self._resume_state.last_committed_sequence
        current = refreshed.last_committed_sequence
        if previous is not None and (current is None or current < previous):
            self._store.close()
            return SchedulerOutcome.TRUST_FAILED
        self._resume_state = refreshed
        self._next_sequence = _next_sequence(refreshed)
        return None

    def _read_resume_state(self) -> SchedulerResumeState:
        try:
            state = self._store.get_scheduler_resume_state()
        except QueryError as error:
            reason = {
                QueryRejection.STORAGE_BUSY: SchedulerRejection.STORAGE_BUSY,
                QueryRejection.PERSISTENCE_FAILED: (
                    SchedulerRejection.PERSISTENCE_FAILED
                ),
            }.get(error.reason, SchedulerRejection.TRUST_FAILED)
            if reason is SchedulerRejection.TRUST_FAILED:
                self._store.close()
            raise SchedulerError(reason, trust_lost=error.trust_lost) from None
        except StoreError:
            self._store.close()
            raise SchedulerError(
                SchedulerRejection.TRUST_FAILED, trust_lost=True
            ) from None
        if type(state) is not SchedulerResumeState:
            self._store.close()
            raise SchedulerError(SchedulerRejection.TRUST_FAILED, trust_lost=True)
        return state

    def _read_monotonic(self) -> float:
        try:
            value: object = self._monotonic()
        except Exception:
            raise SchedulerError(SchedulerRejection.INVALID_CLOCK) from None
        return _validated_monotonic(value)

    def _read_utc_now(self) -> tuple[datetime, int]:
        try:
            value: object = self._utc_now()
        except Exception:
            raise SchedulerError(SchedulerRejection.INVALID_CLOCK) from None
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise SchedulerError(SchedulerRejection.INVALID_CLOCK)
        try:
            normalized = value.astimezone(UTC)
            epoch = datetime(1970, 1, 1, tzinfo=UTC)
            delta = normalized - epoch
            timestamp = (
                delta.days * 86_400_000_000
                + delta.seconds * 1_000_000
                + delta.microseconds
            )
        except Exception:
            raise SchedulerError(SchedulerRejection.INVALID_CLOCK) from None
        if type(timestamp) is not int or not 0 <= timestamp <= MAX_TIMESTAMP_US:
            raise SchedulerError(SchedulerRejection.INVALID_CLOCK)
        return normalized, timestamp


_ACCEPTED_OBSERVATION_OUTCOMES: Final = frozenset(
    {
        OrchestrationOutcome.STORED,
        OrchestrationOutcome.STATE_ONLY,
        OrchestrationOutcome.REPLAYED,
    }
)


def _scheduler_outcome(outcome: OrchestrationOutcome) -> SchedulerOutcome:
    if outcome is OrchestrationOutcome.MAINTENANCE_COMPLETED:
        raise SchedulerError(SchedulerRejection.INVALID_STATE)
    try:
        return SchedulerOutcome(outcome.value)
    except ValueError:
        raise SchedulerError(SchedulerRejection.INVALID_STATE) from None


def _scheduler_outcome_from_rejection(
    rejection: SchedulerRejection,
) -> SchedulerOutcome:
    return {
        SchedulerRejection.INVALID_CLOCK: SchedulerOutcome.INVALID_CLOCK,
        SchedulerRejection.REENTRANT: SchedulerOutcome.REENTRANT,
        SchedulerRejection.STORAGE_BUSY: SchedulerOutcome.STORAGE_BUSY,
        SchedulerRejection.PERSISTENCE_FAILED: SchedulerOutcome.PERSISTENCE_FAILED,
        SchedulerRejection.TRUST_FAILED: SchedulerOutcome.TRUST_FAILED,
        SchedulerRejection.INVALID_CONFIGURATION: SchedulerOutcome.TRUST_FAILED,
        SchedulerRejection.INVALID_STATE: SchedulerOutcome.TRUST_FAILED,
    }[rejection]


def _next_sequence(state: SchedulerResumeState) -> int | None:
    sequence = state.last_committed_sequence
    if sequence is None:
        return 1
    if sequence == MAX_OBSERVATION_SEQUENCE:
        return None
    return sequence + 1


def _validate_scheduler_intervals(
    sample_interval_seconds: object,
    dashboard_refresh_seconds: object,
) -> None:
    interval = _validated_sample_interval(sample_interval_seconds)
    if (
        type(dashboard_refresh_seconds) is not int
        or not 1 <= dashboard_refresh_seconds <= MAX_SAMPLE_INTERVAL_SECONDS
        or interval < dashboard_refresh_seconds
    ):
        raise SchedulerError(SchedulerRejection.INVALID_CONFIGURATION)


def _validated_sample_interval(value: object) -> int:
    if (
        type(value) is not int
        or not MIN_SAMPLE_INTERVAL_SECONDS <= value <= MAX_SAMPLE_INTERVAL_SECONDS
    ):
        raise SchedulerError(SchedulerRejection.INVALID_CONFIGURATION)
    return value


def _is_valid_monotonic(value: object) -> bool:
    return (
        type(value) is float
        and math.isfinite(value)
        and 0.0 <= value <= float(MAX_TIMESTAMP_US)
    )


def _validated_monotonic(value: object) -> float:
    if not _is_valid_monotonic(value):
        raise SchedulerError(SchedulerRejection.INVALID_CLOCK)
    return cast(float, value)
