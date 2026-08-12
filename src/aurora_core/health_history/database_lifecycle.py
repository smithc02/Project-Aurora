"""Direct ownership composition for one protected history database."""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Never, cast

from pydantic import ValidationError

from aurora_core.config import HealthHistoryDatabaseMode, HealthHistorySettings
from aurora_core.health_history.leadership import (
    HEALTH_HISTORY_LEADERSHIP_LOCK_FILENAME,
    HealthHistoryLeadership,
    LeadershipError,
    LeadershipRejection,
)
from aurora_core.health_history.models import MAX_TIMESTAMP_US
from aurora_core.health_history.store import HealthHistoryStore, StoreError


class DatabaseLifecycleRejection(StrEnum):
    """Fixed sanitized database-lifecycle failure registry."""

    INVALID_SETTINGS = "invalid_settings"
    RESERVED_DATABASE_PATH = "reserved_database_path"
    LEADERSHIP_UNAVAILABLE = "leadership_unavailable"
    TRUST_FAILED = "trust_failed"
    UNSUPPORTED_RUNTIME = "unsupported_runtime"
    BOOTSTRAP_FAILED = "bootstrap_failed"
    CLEANUP_FAILED = "cleanup_failed"
    CLOSED = "closed"


class DatabaseLifecycleError(Exception):
    """Sanitized protected-database lifecycle failure."""

    def __init__(self, reason: DatabaseLifecycleRejection) -> None:
        super().__init__(reason.value)
        self.reason = reason


class HealthHistoryDatabaseLifecycle:
    """Own one verified Store while retaining its directory leadership."""

    __slots__ = (
        "_cleanup_failed",
        "_closed",
        "_leadership",
        "_store",
    )

    def __init__(
        self,
        *,
        store: HealthHistoryStore,
        leadership: HealthHistoryLeadership,
    ) -> None:
        self._store: HealthHistoryStore | None = store
        self._leadership: HealthHistoryLeadership | None = leadership
        self._cleanup_failed = False
        self._closed = False

    @property
    def store(self) -> HealthHistoryStore:
        """Return the owned Store only while lifecycle state is usable."""
        if self._closed:
            raise DatabaseLifecycleError(DatabaseLifecycleRejection.CLOSED)
        if self._cleanup_failed or self._store is None:
            raise DatabaseLifecycleError(DatabaseLifecycleRejection.CLEANUP_FAILED)
        return self._store

    @property
    def closed(self) -> bool:
        """Return whether both owned resources have definitively closed."""
        return self._closed

    def close(self) -> None:
        """Close Store before leadership, without an internal retry loop."""
        if self._closed:
            return

        store = self._store
        if store is not None:
            try:
                store.close()
            except Exception:
                self._cleanup_failed = True
                raise DatabaseLifecycleError(
                    DatabaseLifecycleRejection.CLEANUP_FAILED
                ) from None
            self._store = None

        leadership = self._leadership
        if leadership is None:
            self._closed = True
            self._cleanup_failed = False
            return
        try:
            leadership.close()
        except Exception:
            if leadership.closed:
                self._leadership = None
                self._closed = True
                self._cleanup_failed = False
            else:
                self._cleanup_failed = True
            raise DatabaseLifecycleError(
                DatabaseLifecycleRejection.CLEANUP_FAILED
            ) from None

        self._leadership = None
        self._closed = True
        self._cleanup_failed = False

    def __enter__(self) -> HealthHistoryDatabaseLifecycle:
        if self._closed:
            raise DatabaseLifecycleError(DatabaseLifecycleRejection.CLOSED)
        if self._cleanup_failed:
            raise DatabaseLifecycleError(DatabaseLifecycleRejection.CLEANUP_FAILED)
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exception_type, exception, traceback
        self.close()


def bootstrap_health_history_database(
    settings: HealthHistorySettings,
    *,
    created_at_utc_us: int | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> HealthHistoryDatabaseLifecycle | None:
    """Own one configured Store under leadership, or no-op when disabled."""
    snapshot = _validated_settings_snapshot(settings)
    if not snapshot.enabled:
        return None
    if not callable(monotonic):
        raise DatabaseLifecycleError(
            DatabaseLifecycleRejection.INVALID_SETTINGS
        ) from None

    database_path_value = snapshot.database_path
    if database_path_value is None:
        raise DatabaseLifecycleError(
            DatabaseLifecycleRejection.INVALID_SETTINGS
        ) from None
    if snapshot.database_mode is HealthHistoryDatabaseMode.CREATE_IF_MISSING:
        if (
            type(created_at_utc_us) is not int
            or not 0 <= created_at_utc_us <= MAX_TIMESTAMP_US
        ):
            raise DatabaseLifecycleError(
                DatabaseLifecycleRejection.INVALID_SETTINGS
            ) from None

    try:
        database_path = Path(database_path_value)
    except (TypeError, ValueError):
        raise DatabaseLifecycleError(
            DatabaseLifecycleRejection.INVALID_SETTINGS
        ) from None
    if database_path.name == HEALTH_HISTORY_LEADERSHIP_LOCK_FILENAME:
        raise DatabaseLifecycleError(
            DatabaseLifecycleRejection.RESERVED_DATABASE_PATH
        ) from None

    leadership = _acquire_leadership(database_path.parent)
    try:
        store = _bootstrap_store(
            snapshot.database_mode,
            database_path,
            created_at_utc_us=created_at_utc_us,
            monotonic=monotonic,
        )
    except DatabaseLifecycleError as error:
        _release_after_bootstrap_failure(leadership, error.reason)
    return HealthHistoryDatabaseLifecycle(store=store, leadership=leadership)


def _validated_settings_snapshot(
    settings: HealthHistorySettings,
) -> HealthHistorySettings:
    if type(settings) is not HealthHistorySettings:
        raise DatabaseLifecycleError(
            DatabaseLifecycleRejection.INVALID_SETTINGS
        ) from None
    try:
        model_data = settings.model_dump(mode="python", warnings=False)
        return HealthHistorySettings.model_validate(model_data)
    except (AttributeError, TypeError, ValueError, ValidationError):
        raise DatabaseLifecycleError(
            DatabaseLifecycleRejection.INVALID_SETTINGS
        ) from None


def _acquire_leadership(directory: Path) -> HealthHistoryLeadership:
    try:
        return HealthHistoryLeadership.acquire(directory)
    except LeadershipError as error:
        reason = {
            LeadershipRejection.BUSY: DatabaseLifecycleRejection.LEADERSHIP_UNAVAILABLE,
            LeadershipRejection.TRUST_FAILED: DatabaseLifecycleRejection.TRUST_FAILED,
            LeadershipRejection.UNSUPPORTED_RUNTIME: (
                DatabaseLifecycleRejection.UNSUPPORTED_RUNTIME
            ),
            LeadershipRejection.ACQUISITION_FAILED: (
                DatabaseLifecycleRejection.BOOTSTRAP_FAILED
            ),
            LeadershipRejection.RELEASE_FAILED: (
                DatabaseLifecycleRejection.BOOTSTRAP_FAILED
            ),
        }[error.reason]
        raise DatabaseLifecycleError(reason) from None
    except Exception:
        raise DatabaseLifecycleError(
            DatabaseLifecycleRejection.BOOTSTRAP_FAILED
        ) from None


def _bootstrap_store(
    database_mode: HealthHistoryDatabaseMode,
    database_path: Path,
    *,
    created_at_utc_us: int | None,
    monotonic: Callable[[], float],
) -> HealthHistoryStore:
    if database_mode is HealthHistoryDatabaseMode.OPEN_EXISTING:
        return _open_existing(database_path, monotonic=monotonic)

    validated_created_at_utc_us = cast(int, created_at_utc_us)
    try:
        return HealthHistoryStore.create(
            database_path,
            created_at_utc_us=validated_created_at_utc_us,
            monotonic=monotonic,
        )
    except StoreError as error:
        if error.reason == "already_exists":
            return _open_existing(database_path, monotonic=monotonic)
        raise DatabaseLifecycleError(_store_rejection(error)) from None
    except Exception:
        raise DatabaseLifecycleError(
            DatabaseLifecycleRejection.BOOTSTRAP_FAILED
        ) from None


def _open_existing(
    database_path: Path,
    *,
    monotonic: Callable[[], float],
) -> HealthHistoryStore:
    try:
        return HealthHistoryStore.open_existing(database_path, monotonic=monotonic)
    except StoreError as error:
        raise DatabaseLifecycleError(_store_rejection(error)) from None
    except Exception:
        raise DatabaseLifecycleError(
            DatabaseLifecycleRejection.BOOTSTRAP_FAILED
        ) from None


def _store_rejection(error: StoreError) -> DatabaseLifecycleRejection:
    if error.reason == "unsupported_runtime":
        return DatabaseLifecycleRejection.UNSUPPORTED_RUNTIME
    return DatabaseLifecycleRejection.BOOTSTRAP_FAILED


def _release_after_bootstrap_failure(
    leadership: HealthHistoryLeadership,
    original_reason: DatabaseLifecycleRejection,
) -> Never:
    try:
        leadership.close()
    except Exception:
        raise DatabaseLifecycleError(
            DatabaseLifecycleRejection.CLEANUP_FAILED
        ) from None
    raise DatabaseLifecycleError(original_reason) from None
