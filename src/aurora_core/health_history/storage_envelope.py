"""Fixed storage-envelope inspection and passive-checkpoint primitives."""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from aurora_core.health_history.filesystem import (
    FilesystemBoundaryError,
    validate_database_file,
)
from aurora_core.health_history.models import (
    MAX_DATABASE_BYTES,
    MAX_DATABASE_PAGES,
    PAGE_SIZE_BYTES,
)

FREE_SPACE_RESERVE_BYTES: Final = 128 * 1024 * 1024
WAL_HEADER_BYTES: Final = 32
WAL_FRAME_HEADER_BYTES: Final = 24
WAL_FRAME_BYTES: Final = WAL_FRAME_HEADER_BYTES + PAGE_SIZE_BYTES
WAL_CHECKPOINT_THRESHOLD_FRAMES: Final = 256
WAL_HARD_LIMIT_FRAMES: Final = 960
WAL_HARD_LIMIT_BYTES: Final = 4 * 1024 * 1024
WAL_INSPECTION_LIMIT_BYTES: Final = MAX_DATABASE_BYTES
MAX_WAL_INSPECTION_FRAMES: Final = (
    WAL_INSPECTION_LIMIT_BYTES - WAL_HEADER_BYTES
) // WAL_FRAME_BYTES
CHECKPOINT_SECONDS: Final = 1.0
PROGRESS_HANDLER_STEPS: Final = 1_000
MAX_FILESYSTEM_BYTES: Final = 2**63 - 1

_PAGE_SIZE_SQL: Final = "PRAGMA page_size"
_PAGE_COUNT_SQL: Final = "PRAGMA page_count"
_MAX_PAGE_COUNT_SQL: Final = "PRAGMA max_page_count"
_PASSIVE_CHECKPOINT_SQL: Final = "PRAGMA wal_checkpoint(PASSIVE)"


class StorageEnvelopeRejection(StrEnum):
    STORAGE_BUSY = "storage_busy"
    TIMED_OUT = "timed_out"
    PERSISTENCE_FAILED = "persistence_failed"
    MALFORMED_STATE = "malformed_state"
    TRUST_FAILED = "trust_failed"


class StorageEnvelopeError(Exception):
    """Fixed storage-envelope failure without private context."""

    def __init__(
        self, reason: StorageEnvelopeRejection, *, trust_lost: bool = False
    ) -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.trust_lost = trust_lost


class StorageDecisionOutcome(StrEnum):
    PROCEED = "proceed"
    CLEANUP_REQUIRED = "cleanup_required"
    CAPACITY_BLOCKED = "capacity_blocked"
    WAL_CHECKPOINT_DUE = "wal_checkpoint_due"
    WAL_OVERSIZE_BLOCKED = "wal_oversize_blocked"


class PassiveCheckpointOutcome(StrEnum):
    NO_WORK = "no_work"
    COMPLETED = "completed"
    BUSY = "busy"
    OVERSIZE_BLOCKED = "oversize_blocked"


class StorageEnvelopeStage(StrEnum):
    """Fixed test-only fault seam; production behavior is a no-op."""

    CAPACITY = "capacity"
    FREE_SPACE = "free_space"
    WAL = "wal"
    CHECKPOINT_BEFORE = "checkpoint_before"
    CHECKPOINT_AFTER = "checkpoint_after"


@dataclass(frozen=True, slots=True)
class StorageCapacityResult:
    page_count: int
    maximum_page_count: int
    used_bytes: int
    maximum_bytes: int
    pages_remaining: int

    def __post_init__(self) -> None:
        if (
            type(self.page_count) is not int
            or type(self.maximum_page_count) is not int
            or type(self.used_bytes) is not int
            or type(self.maximum_bytes) is not int
            or type(self.pages_remaining) is not int
            or not 0 <= self.page_count <= MAX_DATABASE_PAGES
            or self.maximum_page_count != MAX_DATABASE_PAGES
            or self.used_bytes != self.page_count * PAGE_SIZE_BYTES
            or self.maximum_bytes != MAX_DATABASE_BYTES
            or self.pages_remaining != MAX_DATABASE_PAGES - self.page_count
        ):
            raise ValueError("invalid_storage_capacity")


@dataclass(frozen=True, slots=True)
class FreeSpaceResult:
    sufficient: bool
    free_bytes: int
    required_reserve_bytes: int

    def __post_init__(self) -> None:
        if (
            type(self.sufficient) is not bool
            or type(self.free_bytes) is not int
            or not 0 <= self.free_bytes <= MAX_FILESYSTEM_BYTES
            or self.required_reserve_bytes != FREE_SPACE_RESERVE_BYTES
            or self.sufficient != (self.free_bytes >= FREE_SPACE_RESERVE_BYTES)
        ):
            raise ValueError("invalid_free_space")


@dataclass(frozen=True, slots=True)
class WalInspectionResult:
    exists: bool
    frame_count: int
    total_bytes: int
    checkpoint_due: bool
    oversize: bool

    def __post_init__(self) -> None:
        if (
            type(self.exists) is not bool
            or type(self.frame_count) is not int
            or type(self.total_bytes) is not int
            or type(self.checkpoint_due) is not bool
            or type(self.oversize) is not bool
            or not 0 <= self.frame_count <= MAX_WAL_INSPECTION_FRAMES
            or not 0 <= self.total_bytes <= WAL_INSPECTION_LIMIT_BYTES
            or (not self.exists and (self.frame_count != 0 or self.total_bytes != 0))
            or (
                self.exists
                and not _wal_size_matches(self.total_bytes, self.frame_count)
            )
            or self.oversize
            != (
                self.frame_count > WAL_HARD_LIMIT_FRAMES
                or self.total_bytes > WAL_HARD_LIMIT_BYTES
            )
            or self.checkpoint_due
            != (
                self.frame_count >= WAL_CHECKPOINT_THRESHOLD_FRAMES
                and not self.oversize
            )
        ):
            raise ValueError("invalid_wal_inspection")


@dataclass(frozen=True, slots=True)
class StorageDecisionResult:
    outcome: StorageDecisionOutcome
    write_permitted: bool

    def __post_init__(self) -> None:
        if (
            not isinstance(self.outcome, StorageDecisionOutcome)
            or type(self.write_permitted) is not bool
            or self.write_permitted != (self.outcome is StorageDecisionOutcome.PROCEED)
        ):
            raise ValueError("invalid_storage_decision")


@dataclass(frozen=True, slots=True)
class PassiveCheckpointResult:
    outcome: PassiveCheckpointOutcome
    wal_frames_before: int
    busy: bool
    log_frames: int
    checkpointed_frames: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.outcome, PassiveCheckpointOutcome)
            or type(self.wal_frames_before) is not int
            or type(self.busy) is not bool
            or type(self.log_frames) is not int
            or type(self.checkpointed_frames) is not int
            or not 0 <= self.wal_frames_before <= MAX_WAL_INSPECTION_FRAMES
            or not 0 <= self.log_frames <= MAX_WAL_INSPECTION_FRAMES
            or not 0 <= self.checkpointed_frames <= self.log_frames
            or self.log_frames > self.wal_frames_before
            or (
                self.outcome
                in {
                    PassiveCheckpointOutcome.NO_WORK,
                    PassiveCheckpointOutcome.OVERSIZE_BLOCKED,
                }
                and (self.busy or self.log_frames != 0 or self.checkpointed_frames != 0)
            )
            or (
                self.outcome is PassiveCheckpointOutcome.NO_WORK
                and self.wal_frames_before >= WAL_CHECKPOINT_THRESHOLD_FRAMES
            )
            or (
                self.outcome is PassiveCheckpointOutcome.OVERSIZE_BLOCKED
                and self.wal_frames_before <= WAL_HARD_LIMIT_FRAMES
            )
            or (
                self.outcome
                in {PassiveCheckpointOutcome.COMPLETED, PassiveCheckpointOutcome.BUSY}
                and not (
                    WAL_CHECKPOINT_THRESHOLD_FRAMES
                    <= self.wal_frames_before
                    <= WAL_HARD_LIMIT_FRAMES
                )
            )
            or (self.outcome is PassiveCheckpointOutcome.COMPLETED and self.busy)
            or (self.outcome is PassiveCheckpointOutcome.BUSY and not self.busy)
        ):
            raise ValueError("invalid_checkpoint_result")


class _Deadline:
    def __init__(self, monotonic: Callable[[], float]) -> None:
        self._monotonic = monotonic
        self._deadline = monotonic() + CHECKPOINT_SECONDS
        self.expired = False

    def progress(self) -> int:
        if self._monotonic() >= self._deadline:
            self.expired = True
            return 1
        return 0

    def check(self) -> None:
        if self._monotonic() >= self._deadline:
            self.expired = True
            raise StorageEnvelopeError(StorageEnvelopeRejection.TIMED_OUT)


def decide_storage_action(
    capacity: StorageCapacityResult,
    free_space: FreeSpaceResult,
    wal: WalInspectionResult,
    *,
    cleanup_attempted: bool = False,
) -> StorageDecisionResult:
    """Return one pure fixed-priority decision for a future writer."""
    if (
        type(capacity) is not StorageCapacityResult
        or type(free_space) is not FreeSpaceResult
        or type(wal) is not WalInspectionResult
        or type(cleanup_attempted) is not bool
    ):
        raise ValueError("invalid_storage_observation")
    if wal.oversize:
        outcome = StorageDecisionOutcome.WAL_OVERSIZE_BLOCKED
    elif capacity.pages_remaining == 0 or not free_space.sufficient:
        outcome = (
            StorageDecisionOutcome.CAPACITY_BLOCKED
            if cleanup_attempted
            else StorageDecisionOutcome.CLEANUP_REQUIRED
        )
    elif wal.checkpoint_due:
        outcome = StorageDecisionOutcome.WAL_CHECKPOINT_DUE
    else:
        outcome = StorageDecisionOutcome.PROCEED
    return StorageDecisionResult(
        outcome=outcome,
        write_permitted=outcome is StorageDecisionOutcome.PROCEED,
    )


def _inspect_storage_capacity(
    connection: sqlite3.Connection,
) -> StorageCapacityResult:
    try:
        page_size = _pragma_integer(connection, _PAGE_SIZE_SQL)
        page_count = _pragma_integer(connection, _PAGE_COUNT_SQL)
        maximum_page_count = _pragma_integer(connection, _MAX_PAGE_COUNT_SQL)
        _fault(StorageEnvelopeStage.CAPACITY)
        if page_size != PAGE_SIZE_BYTES or maximum_page_count != MAX_DATABASE_PAGES:
            raise _malformed()
        if not 0 <= page_count <= MAX_DATABASE_PAGES:
            raise _malformed()
        return StorageCapacityResult(
            page_count=page_count,
            maximum_page_count=maximum_page_count,
            used_bytes=page_count * PAGE_SIZE_BYTES,
            maximum_bytes=MAX_DATABASE_BYTES,
            pages_remaining=MAX_DATABASE_PAGES - page_count,
        )
    except StorageEnvelopeError:
        raise
    except sqlite3.Error as error:
        raise _classified_sqlite_error(error) from None
    except (TypeError, ValueError):
        raise _malformed() from None


def _inspect_free_space(parent: Path) -> FreeSpaceResult:
    try:
        usage = shutil.disk_usage(parent)
        _fault(StorageEnvelopeStage.FREE_SPACE)
    except OSError:
        raise StorageEnvelopeError(
            StorageEnvelopeRejection.PERSISTENCE_FAILED
        ) from None
    try:
        total, used, free = usage.total, usage.used, usage.free
        if (
            type(total) is not int
            or type(used) is not int
            or type(free) is not int
            or not 0 <= total <= MAX_FILESYSTEM_BYTES
            or not 0 <= used <= total
            or not 0 <= free <= total
        ):
            raise _malformed()
        return FreeSpaceResult(
            sufficient=free >= FREE_SPACE_RESERVE_BYTES,
            free_bytes=free,
            required_reserve_bytes=FREE_SPACE_RESERVE_BYTES,
        )
    except StorageEnvelopeError:
        raise
    except (AttributeError, TypeError, ValueError):
        raise _malformed() from None


def _inspect_wal(path: Path) -> WalInspectionResult:
    wal_path = path.with_name(f"{path.name}-wal")
    try:
        wal_path.lstat()
    except FileNotFoundError:
        return WalInspectionResult(False, 0, 0, False, False)
    except OSError:
        raise StorageEnvelopeError(
            StorageEnvelopeRejection.PERSISTENCE_FAILED
        ) from None
    try:
        before = validate_database_file(
            wal_path, maximum_bytes=WAL_INSPECTION_LIMIT_BYTES
        )
        frame_count = _wal_frame_count(before.size)
        after = validate_database_file(
            wal_path,
            expected=before,
            maximum_bytes=WAL_INSPECTION_LIMIT_BYTES,
        )
        if after != before:
            raise _malformed()
        _fault(StorageEnvelopeStage.WAL)
        oversize = (
            frame_count > WAL_HARD_LIMIT_FRAMES or before.size > WAL_HARD_LIMIT_BYTES
        )
        return WalInspectionResult(
            exists=True,
            frame_count=frame_count,
            total_bytes=before.size,
            checkpoint_due=(
                frame_count >= WAL_CHECKPOINT_THRESHOLD_FRAMES and not oversize
            ),
            oversize=oversize,
        )
    except StorageEnvelopeError:
        raise
    except (FilesystemBoundaryError, OSError):
        raise StorageEnvelopeError(
            StorageEnvelopeRejection.TRUST_FAILED, trust_lost=True
        ) from None
    except (TypeError, ValueError):
        raise _malformed() from None


def _passive_wal_checkpoint(
    connection: sqlite3.Connection,
    path: Path,
    *,
    monotonic: Callable[[], float],
) -> PassiveCheckpointResult:
    deadline = _Deadline(monotonic)
    wal = _inspect_wal(path)
    deadline.check()
    if not wal.checkpoint_due:
        return PassiveCheckpointResult(
            outcome=(
                PassiveCheckpointOutcome.OVERSIZE_BLOCKED
                if wal.oversize
                else PassiveCheckpointOutcome.NO_WORK
            ),
            wal_frames_before=wal.frame_count,
            busy=False,
            log_frames=0,
            checkpointed_frames=0,
        )
    try:
        _install_progress_handler(connection, deadline)
        try:
            deadline.check()
            _fault(StorageEnvelopeStage.CHECKPOINT_BEFORE)
            cursor = connection.execute(_PASSIVE_CHECKPOINT_SQL)
            deadline.check()
            rows = cursor.fetchmany(2)
            deadline.check()
            if len(rows) != 1 or len(rows[0]) != 3:
                raise _malformed()
            values: list[int] = []
            for value in rows[0]:
                deadline.check()
                if (
                    type(value) is not int
                    or not 0 <= value <= MAX_WAL_INSPECTION_FRAMES
                ):
                    raise _malformed()
                values.append(value)
            busy_value, log_frames, checkpointed_frames = values
            if (
                busy_value not in {0, 1}
                or checkpointed_frames > log_frames
                or log_frames > wal.frame_count
            ):
                raise _malformed()
            _fault(StorageEnvelopeStage.CHECKPOINT_AFTER)
            deadline.check()
            return PassiveCheckpointResult(
                outcome=(
                    PassiveCheckpointOutcome.BUSY
                    if busy_value == 1
                    else PassiveCheckpointOutcome.COMPLETED
                ),
                wal_frames_before=wal.frame_count,
                busy=busy_value == 1,
                log_frames=log_frames,
                checkpointed_frames=checkpointed_frames,
            )
        except StorageEnvelopeError:
            raise
        except sqlite3.Error as error:
            raise _classified_sqlite_error(error, deadline) from None
        except (TypeError, ValueError):
            raise _malformed() from None
    finally:
        _clear_progress_handler(connection)


def _wal_frame_count(total_bytes: object) -> int:
    if (
        type(total_bytes) is not int
        or not 0 <= total_bytes <= WAL_INSPECTION_LIMIT_BYTES
    ):
        raise _malformed()
    if total_bytes == 0:
        return 0
    if total_bytes < WAL_HEADER_BYTES:
        raise _malformed()
    payload = total_bytes - WAL_HEADER_BYTES
    if payload % WAL_FRAME_BYTES != 0:
        raise _malformed()
    frame_count = payload // WAL_FRAME_BYTES
    if frame_count > MAX_WAL_INSPECTION_FRAMES:
        raise _malformed()
    return frame_count


def _wal_size_matches(total_bytes: int, frame_count: int) -> bool:
    if total_bytes == 0:
        return frame_count == 0
    if total_bytes < WAL_HEADER_BYTES:
        return False
    payload = total_bytes - WAL_HEADER_BYTES
    return payload % WAL_FRAME_BYTES == 0 and payload // WAL_FRAME_BYTES == frame_count


def _pragma_integer(connection: sqlite3.Connection, sql: str) -> int:
    row = connection.execute(sql).fetchone()
    if row is None or len(row) != 1 or type(row[0]) is not int:
        raise _malformed()
    return row[0]


def _install_progress_handler(
    connection: sqlite3.Connection, deadline: _Deadline
) -> None:
    try:
        connection.set_progress_handler(deadline.progress, PROGRESS_HANDLER_STEPS)
    except sqlite3.Error as error:
        raise _classified_sqlite_error(error, deadline) from None


def _clear_progress_handler(connection: sqlite3.Connection) -> None:
    try:
        connection.set_progress_handler(None, 0)
    except sqlite3.Error:
        raise StorageEnvelopeError(
            StorageEnvelopeRejection.TRUST_FAILED, trust_lost=True
        ) from None


def _classified_sqlite_error(
    error: sqlite3.Error, deadline: _Deadline | None = None
) -> StorageEnvelopeError:
    raw_code = getattr(error, "sqlite_errorcode", None)
    primary_code = raw_code & 0xFF if type(raw_code) is int else None
    if isinstance(error, sqlite3.IntegrityError) or primary_code in {
        sqlite3.SQLITE_CORRUPT,
        sqlite3.SQLITE_NOTADB,
        sqlite3.SQLITE_SCHEMA,
        sqlite3.SQLITE_CONSTRAINT,
    }:
        return StorageEnvelopeError(
            StorageEnvelopeRejection.TRUST_FAILED, trust_lost=True
        )
    if primary_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        return StorageEnvelopeError(StorageEnvelopeRejection.STORAGE_BUSY)
    if (
        deadline is not None
        and deadline.expired
        and primary_code in {None, sqlite3.SQLITE_INTERRUPT}
    ):
        return StorageEnvelopeError(StorageEnvelopeRejection.TIMED_OUT)
    return StorageEnvelopeError(StorageEnvelopeRejection.PERSISTENCE_FAILED)


def _malformed() -> StorageEnvelopeError:
    return StorageEnvelopeError(
        StorageEnvelopeRejection.MALFORMED_STATE, trust_lost=True
    )


def _fault(stage: StorageEnvelopeStage) -> None:
    del stage
