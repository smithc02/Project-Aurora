"""Direct-only tests for protected history database ownership composition."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import cast

import pytest

import aurora_core.health_history.database_lifecycle as lifecycle_module
from aurora_core.config import HealthHistoryDatabaseMode, HealthHistorySettings
from aurora_core.health_history import (
    HEALTH_HISTORY_LEADERSHIP_LOCK_FILENAME,
    DatabaseLifecycleError,
    DatabaseLifecycleRejection,
    HealthHistoryDatabaseLifecycle,
    HealthHistoryLeadership,
    LeadershipError,
    LeadershipRejection,
    bootstrap_health_history_database,
)
from aurora_core.health_history.models import MAX_TIMESTAMP_US, SCHEMA_VERSION
from aurora_core.health_history.store import HealthHistoryStore, StoreError


class _FakeStore:
    def __init__(
        self,
        events: list[str] | None = None,
        *,
        close_failures: int = 0,
    ) -> None:
        self.events = events if events is not None else []
        self.close_failures = close_failures
        self.close_calls = 0
        self.closed = False

    def close(self) -> None:
        self.close_calls += 1
        self.events.append("store_close")
        if self.close_failures:
            self.close_failures -= 1
            raise sqlite3.OperationalError("private-store-close-canary")
        self.closed = True


class _FakeLeadership:
    def __init__(
        self,
        events: list[str] | None = None,
        *,
        close_failures: int = 0,
        closes_on_failure: bool = True,
    ) -> None:
        self.events = events if events is not None else []
        self.close_failures = close_failures
        self.closes_on_failure = closes_on_failure
        self.close_calls = 0
        self.closed = False

    @property
    def held(self) -> bool:
        return not self.closed

    def close(self) -> None:
        self.close_calls += 1
        self.events.append("leadership_close")
        if self.close_failures:
            self.close_failures -= 1
            if self.closes_on_failure:
                self.closed = True
            raise LeadershipError(LeadershipRejection.RELEASE_FAILED)
        self.closed = True


def _settings(
    path: Path | str,
    *,
    mode: HealthHistoryDatabaseMode = HealthHistoryDatabaseMode.OPEN_EXISTING,
) -> HealthHistorySettings:
    return HealthHistorySettings(
        enabled=True,
        database_path=str(path),
        database_mode=mode,
    )


def _lifecycle(
    store: _FakeStore,
    leadership: _FakeLeadership,
) -> HealthHistoryDatabaseLifecycle:
    return HealthHistoryDatabaseLifecycle(
        store=cast(HealthHistoryStore, store),
        leadership=cast(HealthHistoryLeadership, leadership),
    )


def _patch_acquire(
    monkeypatch: pytest.MonkeyPatch,
    acquire: Callable[[Path], _FakeLeadership],
) -> None:
    def replacement(
        cls: type[HealthHistoryLeadership], directory: Path
    ) -> HealthHistoryLeadership:
        del cls
        return cast(HealthHistoryLeadership, acquire(directory))

    monkeypatch.setattr(
        lifecycle_module.HealthHistoryLeadership,
        "acquire",
        classmethod(replacement),
    )


def _patch_open(
    monkeypatch: pytest.MonkeyPatch,
    open_existing: Callable[[Path, Callable[[], float]], _FakeStore],
) -> None:
    def replacement(
        cls: type[HealthHistoryStore],
        path: Path,
        *,
        monotonic: Callable[[], float],
    ) -> HealthHistoryStore:
        del cls
        return cast(HealthHistoryStore, open_existing(path, monotonic))

    monkeypatch.setattr(
        lifecycle_module.HealthHistoryStore,
        "open_existing",
        classmethod(replacement),
    )


def _patch_create(
    monkeypatch: pytest.MonkeyPatch,
    create: Callable[[Path, int, Callable[[], float]], _FakeStore],
) -> None:
    def replacement(
        cls: type[HealthHistoryStore],
        path: Path,
        *,
        created_at_utc_us: int,
        monotonic: Callable[[], float],
    ) -> HealthHistoryStore:
        del cls
        return cast(HealthHistoryStore, create(path, created_at_utc_us, monotonic))

    monkeypatch.setattr(
        lifecycle_module.HealthHistoryStore,
        "create",
        classmethod(replacement),
    )


def test_disabled_settings_are_a_true_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = HealthHistorySettings()

    def unexpected(*args: object, **kwargs: object) -> None:
        raise AssertionError("disabled lifecycle must not touch dependencies")

    monkeypatch.setattr(lifecycle_module, "Path", unexpected)
    monkeypatch.setattr(
        lifecycle_module.HealthHistoryLeadership,
        "acquire",
        classmethod(unexpected),
    )
    monkeypatch.setattr(
        lifecycle_module.HealthHistoryStore,
        "create",
        classmethod(unexpected),
    )
    monkeypatch.setattr(
        lifecycle_module.HealthHistoryStore,
        "open_existing",
        classmethod(unexpected),
    )

    def monotonic() -> float:
        raise AssertionError("disabled lifecycle must not read monotonic time")

    assert (
        bootstrap_health_history_database(
            settings,
            created_at_utc_us=cast(int, object()),
            monotonic=monotonic,
        )
        is None
    )


@pytest.mark.parametrize("settings", [object(), None, "invalid"])
def test_non_settings_objects_fail_before_io(
    settings: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def unexpected(directory: Path) -> _FakeLeadership:
        nonlocal calls
        calls += 1
        raise AssertionError(directory)

    _patch_acquire(monkeypatch, unexpected)
    with pytest.raises(DatabaseLifecycleError) as captured:
        bootstrap_health_history_database(cast(HealthHistorySettings, settings))
    assert captured.value.reason is DatabaseLifecycleRejection.INVALID_SETTINGS
    assert str(captured.value) == "invalid_settings"
    assert calls == 0


def test_mutated_settings_are_defensively_revalidated_without_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = HealthHistorySettings()
    settings.enabled = "true"  # type: ignore[assignment]
    calls = 0

    def unexpected(directory: Path) -> _FakeLeadership:
        nonlocal calls
        calls += 1
        raise AssertionError(directory)

    _patch_acquire(monkeypatch, unexpected)
    with pytest.raises(DatabaseLifecycleError) as captured:
        bootstrap_health_history_database(settings)
    assert captured.value.reason is DatabaseLifecycleRejection.INVALID_SETTINGS
    assert calls == 0


def test_settings_snapshot_failure_is_sanitized_without_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private = "private-settings-canary"

    def fail_dump(*args: object, **kwargs: object) -> dict[str, object]:
        raise ValueError(private)

    monkeypatch.setattr(HealthHistorySettings, "model_dump", fail_dump)
    with pytest.raises(DatabaseLifecycleError) as captured:
        bootstrap_health_history_database(HealthHistorySettings())
    assert captured.value.reason is DatabaseLifecycleRejection.INVALID_SETTINGS
    assert private not in str(captured.value)


def test_defensive_enabled_missing_path_fails_before_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid_snapshot = HealthHistorySettings.model_construct(
        enabled=True,
        database_path=None,
        database_mode=HealthHistoryDatabaseMode.OPEN_EXISTING,
        sample_interval_seconds=30,
        retention_days=30,
    )
    monkeypatch.setattr(
        lifecycle_module,
        "_validated_settings_snapshot",
        lambda settings: invalid_snapshot,
    )
    with pytest.raises(DatabaseLifecycleError) as captured:
        bootstrap_health_history_database(HealthHistorySettings())
    assert captured.value.reason is DatabaseLifecycleRejection.INVALID_SETTINGS


@pytest.mark.parametrize(
    "timestamp",
    [None, True, -1, MAX_TIMESTAMP_US + 1, 1.0, "1"],
)
def test_create_requires_bounded_exact_integer_timestamp_before_leadership(
    timestamp: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def unexpected(directory: Path) -> _FakeLeadership:
        nonlocal calls
        calls += 1
        raise AssertionError(directory)

    _patch_acquire(monkeypatch, unexpected)
    with pytest.raises(DatabaseLifecycleError) as captured:
        bootstrap_health_history_database(
            _settings(
                "/protected/history.sqlite3",
                mode=HealthHistoryDatabaseMode.CREATE_IF_MISSING,
            ),
            created_at_utc_us=cast(int | None, timestamp),
        )
    assert captured.value.reason is DatabaseLifecycleRejection.INVALID_SETTINGS
    assert calls == 0


@pytest.mark.parametrize("timestamp", [0, MAX_TIMESTAMP_US])
def test_create_accepts_timestamp_boundaries(
    timestamp: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leadership = _FakeLeadership()
    store = _FakeStore()
    seen: list[int] = []
    _patch_acquire(monkeypatch, lambda directory: leadership)
    _patch_create(
        monkeypatch,
        lambda path, created_at, monotonic: seen.append(created_at) or store,
    )
    result = bootstrap_health_history_database(
        _settings(
            "/protected/history.sqlite3",
            mode=HealthHistoryDatabaseMode.CREATE_IF_MISSING,
        ),
        created_at_utc_us=timestamp,
    )
    assert result is not None
    assert seen == [timestamp]
    result.close()


def test_open_existing_ignores_creation_timestamp_and_does_not_read_monotonic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leadership = _FakeLeadership()
    store = _FakeStore()
    supplied: list[Callable[[], float]] = []

    def monotonic() -> float:
        raise AssertionError("lifecycle must pass rather than read monotonic")

    _patch_acquire(monkeypatch, lambda directory: leadership)
    _patch_open(
        monkeypatch,
        lambda path, selected_monotonic: supplied.append(selected_monotonic) or store,
    )
    result = bootstrap_health_history_database(
        _settings("/protected/history.sqlite3"),
        created_at_utc_us=cast(int, object()),
        monotonic=monotonic,
    )
    assert result is not None
    assert supplied == [monotonic]
    result.close()


def test_enabled_lifecycle_rejects_noncallable_monotonic_before_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def unexpected(directory: Path) -> _FakeLeadership:
        nonlocal calls
        calls += 1
        raise AssertionError(directory)

    _patch_acquire(monkeypatch, unexpected)
    with pytest.raises(DatabaseLifecycleError) as captured:
        bootstrap_health_history_database(
            _settings("/protected/history.sqlite3"),
            monotonic=cast(Callable[[], float], None),
        )
    assert captured.value.reason is DatabaseLifecycleRejection.INVALID_SETTINGS
    assert calls == 0


def test_path_is_materialized_literally_without_expansion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_path = "/literal/$PRIVATE/history-wal"
    converted: list[str] = []
    acquired: list[PurePosixPath] = []
    opened: list[PurePosixPath] = []
    leadership = _FakeLeadership()
    store = _FakeStore()

    def literal_path(value: str) -> PurePosixPath:
        converted.append(value)
        return PurePosixPath(value)

    monkeypatch.setattr(lifecycle_module, "Path", literal_path)
    _patch_acquire(
        monkeypatch,
        lambda directory: acquired.append(cast(PurePosixPath, directory)) or leadership,
    )
    _patch_open(
        monkeypatch,
        lambda path, monotonic: opened.append(cast(PurePosixPath, path)) or store,
    )
    result = bootstrap_health_history_database(_settings(raw_path))
    assert result is not None
    assert converted == [raw_path]
    assert acquired == [PurePosixPath("/literal/$PRIVATE")]
    assert opened == [PurePosixPath(raw_path)]
    result.close()


def test_path_materialization_failure_is_sanitized_before_leadership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private = "private-path-canary"
    acquire_calls = 0

    def fail_path(value: str) -> Path:
        raise ValueError(private)

    def unexpected_acquire(directory: Path) -> _FakeLeadership:
        nonlocal acquire_calls
        acquire_calls += 1
        raise AssertionError(directory)

    monkeypatch.setattr(lifecycle_module, "Path", fail_path)
    _patch_acquire(monkeypatch, unexpected_acquire)
    with pytest.raises(DatabaseLifecycleError) as captured:
        bootstrap_health_history_database(_settings("/protected/history.sqlite3"))
    assert captured.value.reason is DatabaseLifecycleRejection.INVALID_SETTINGS
    assert private not in str(captured.value)
    assert acquire_calls == 0


def test_reserved_leadership_basename_is_rejected_before_acquisition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_path = f"/private-canary/{HEALTH_HISTORY_LEADERSHIP_LOCK_FILENAME}"
    calls = 0

    def unexpected(directory: Path) -> _FakeLeadership:
        nonlocal calls
        calls += 1
        raise AssertionError(directory)

    _patch_acquire(monkeypatch, unexpected)
    with pytest.raises(DatabaseLifecycleError) as captured:
        bootstrap_health_history_database(_settings(private_path))
    assert captured.value.reason is DatabaseLifecycleRejection.RESERVED_DATABASE_PATH
    assert str(captured.value) == "reserved_database_path"
    assert private_path not in str(captured.value)
    assert calls == 0


@pytest.mark.parametrize("basename", ["history-wal", "history-shm"])
def test_sidecar_looking_main_names_are_not_broadly_rejected(
    basename: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leadership = _FakeLeadership()
    store = _FakeStore()
    opened: list[Path] = []
    _patch_acquire(monkeypatch, lambda directory: leadership)
    _patch_open(
        monkeypatch,
        lambda path, monotonic: opened.append(path) or store,
    )
    result = bootstrap_health_history_database(_settings(f"/protected/{basename}"))
    assert result is not None
    assert opened == [Path(f"/protected/{basename}")]
    result.close()


@pytest.mark.parametrize(
    ("leadership_reason", "lifecycle_reason"),
    [
        (LeadershipRejection.BUSY, DatabaseLifecycleRejection.LEADERSHIP_UNAVAILABLE),
        (LeadershipRejection.TRUST_FAILED, DatabaseLifecycleRejection.TRUST_FAILED),
        (
            LeadershipRejection.UNSUPPORTED_RUNTIME,
            DatabaseLifecycleRejection.UNSUPPORTED_RUNTIME,
        ),
        (
            LeadershipRejection.ACQUISITION_FAILED,
            DatabaseLifecycleRejection.BOOTSTRAP_FAILED,
        ),
        (
            LeadershipRejection.RELEASE_FAILED,
            DatabaseLifecycleRejection.BOOTSTRAP_FAILED,
        ),
    ],
)
def test_leadership_failures_are_sanitized_before_store(
    leadership_reason: LeadershipRejection,
    lifecycle_reason: DatabaseLifecycleRejection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_calls = 0

    def fail_acquire(directory: Path) -> _FakeLeadership:
        raise LeadershipError(leadership_reason)

    def unexpected_open(path: Path, monotonic: Callable[[], float]) -> _FakeStore:
        nonlocal store_calls
        store_calls += 1
        raise AssertionError(path)

    _patch_acquire(monkeypatch, fail_acquire)
    _patch_open(monkeypatch, unexpected_open)
    with pytest.raises(DatabaseLifecycleError) as captured:
        bootstrap_health_history_database(_settings("/protected/history.sqlite3"))
    assert captured.value.reason is lifecycle_reason
    assert str(captured.value) == lifecycle_reason.value
    assert store_calls == 0


def test_unexpected_leadership_failure_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private = "private-leadership-canary"

    def fail_acquire(directory: Path) -> _FakeLeadership:
        raise OSError(private)

    _patch_acquire(monkeypatch, fail_acquire)
    with pytest.raises(DatabaseLifecycleError) as captured:
        bootstrap_health_history_database(_settings("/protected/history.sqlite3"))
    assert captured.value.reason is DatabaseLifecycleRejection.BOOTSTRAP_FAILED
    assert private not in str(captured.value)


def test_open_existing_acquires_first_and_opens_once_without_create(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    leadership = _FakeLeadership(events)
    store = _FakeStore(events)

    def monotonic() -> float:
        return 1.0

    _patch_acquire(
        monkeypatch,
        lambda directory: events.append(f"leadership:{directory}") or leadership,
    )
    _patch_open(
        monkeypatch,
        lambda path, selected: events.append(f"open:{path}") or store,
    )

    def unexpected_create(
        path: Path,
        created_at: int,
        selected: Callable[[], float],
    ) -> _FakeStore:
        raise AssertionError("open_existing must never create")

    _patch_create(monkeypatch, unexpected_create)
    result = bootstrap_health_history_database(
        _settings("/protected/history.sqlite3"), monotonic=monotonic
    )
    assert result is not None
    assert events == ["leadership:/protected", "open:/protected/history.sqlite3"]
    assert result.store is cast(HealthHistoryStore, store)
    result.close()
    assert events[-2:] == ["store_close", "leadership_close"]


@pytest.mark.parametrize(
    ("store_reason", "expected"),
    [
        ("open_failed", DatabaseLifecycleRejection.BOOTSTRAP_FAILED),
        ("unsupported_runtime", DatabaseLifecycleRejection.UNSUPPORTED_RUNTIME),
        ("unexpected", DatabaseLifecycleRejection.BOOTSTRAP_FAILED),
    ],
)
def test_open_failure_releases_leadership_and_preserves_sanitized_reason(
    store_reason: str,
    expected: DatabaseLifecycleRejection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leadership = _FakeLeadership()
    _patch_acquire(monkeypatch, lambda directory: leadership)

    def fail_open(path: Path, monotonic: Callable[[], float]) -> _FakeStore:
        raise StoreError(store_reason) from RuntimeError("private-store-canary")

    _patch_open(monkeypatch, fail_open)
    with pytest.raises(DatabaseLifecycleError) as captured:
        bootstrap_health_history_database(_settings("/protected/history.sqlite3"))
    assert captured.value.reason is expected
    assert "private" not in str(captured.value)
    assert leadership.close_calls == 1
    assert leadership.closed


@pytest.mark.parametrize("operation", ["open", "create"])
def test_unexpected_store_failures_are_sanitized_and_release_leadership(
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leadership = _FakeLeadership()
    private = "private-raw-store-canary"
    _patch_acquire(monkeypatch, lambda directory: leadership)
    if operation == "open":
        _patch_open(
            monkeypatch,
            lambda path, monotonic: (_ for _ in ()).throw(OSError(private)),
        )
        settings = _settings("/protected/history.sqlite3")
        timestamp = None
    else:
        _patch_create(
            monkeypatch,
            lambda path, created_at, monotonic: (_ for _ in ()).throw(OSError(private)),
        )
        settings = _settings(
            "/protected/history.sqlite3",
            mode=HealthHistoryDatabaseMode.CREATE_IF_MISSING,
        )
        timestamp = 1
    with pytest.raises(DatabaseLifecycleError) as captured:
        bootstrap_health_history_database(
            settings,
            created_at_utc_us=timestamp,
        )
    assert captured.value.reason is DatabaseLifecycleRejection.BOOTSTRAP_FAILED
    assert private not in str(captured.value)
    assert leadership.close_calls == 1


def test_create_success_does_not_open_existing(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    leadership = _FakeLeadership(events)
    store = _FakeStore(events)
    _patch_acquire(
        monkeypatch,
        lambda directory: events.append("leadership") or leadership,
    )
    _patch_create(
        monkeypatch,
        lambda path, timestamp, monotonic: (
            events.append(f"create:{timestamp}") or store
        ),
    )

    def unexpected_open(path: Path, monotonic: Callable[[], float]) -> _FakeStore:
        raise AssertionError("successful creation must not open again")

    _patch_open(monkeypatch, unexpected_open)
    result = bootstrap_health_history_database(
        _settings(
            "/protected/history.sqlite3",
            mode=HealthHistoryDatabaseMode.CREATE_IF_MISSING,
        ),
        created_at_utc_us=7,
    )
    assert result is not None
    assert events == ["leadership", "create:7"]
    result.close()


def test_exact_already_exists_falls_through_to_one_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    leadership = _FakeLeadership(events)
    store = _FakeStore(events)
    _patch_acquire(monkeypatch, lambda directory: leadership)

    def already_exists(
        path: Path,
        timestamp: int,
        monotonic: Callable[[], float],
    ) -> _FakeStore:
        events.append("create")
        raise StoreError("already_exists")

    _patch_create(monkeypatch, already_exists)
    _patch_open(
        monkeypatch,
        lambda path, monotonic: events.append("open") or store,
    )
    result = bootstrap_health_history_database(
        _settings(
            "/protected/history.sqlite3",
            mode=HealthHistoryDatabaseMode.CREATE_IF_MISSING,
        ),
        created_at_utc_us=8,
    )
    assert result is not None
    assert events == ["create", "open"]
    result.close()


@pytest.mark.parametrize(
    ("store_reason", "expected"),
    [
        ("creation_failed", DatabaseLifecycleRejection.BOOTSTRAP_FAILED),
        ("unsupported_runtime", DatabaseLifecycleRejection.UNSUPPORTED_RUNTIME),
        ("unexpected", DatabaseLifecycleRejection.BOOTSTRAP_FAILED),
    ],
)
def test_create_failure_never_falls_through_and_releases_leadership(
    store_reason: str,
    expected: DatabaseLifecycleRejection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leadership = _FakeLeadership()
    open_calls = 0
    _patch_acquire(monkeypatch, lambda directory: leadership)

    def fail_create(
        path: Path,
        timestamp: int,
        monotonic: Callable[[], float],
    ) -> _FakeStore:
        raise StoreError(store_reason)

    def unexpected_open(path: Path, monotonic: Callable[[], float]) -> _FakeStore:
        nonlocal open_calls
        open_calls += 1
        raise AssertionError(path)

    _patch_create(monkeypatch, fail_create)
    _patch_open(monkeypatch, unexpected_open)
    with pytest.raises(DatabaseLifecycleError) as captured:
        bootstrap_health_history_database(
            _settings(
                "/protected/history.sqlite3",
                mode=HealthHistoryDatabaseMode.CREATE_IF_MISSING,
            ),
            created_at_utc_us=9,
        )
    assert captured.value.reason is expected
    assert open_calls == 0
    assert leadership.close_calls == 1


def test_fallback_open_failure_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leadership = _FakeLeadership()
    create_calls = 0
    open_calls = 0
    _patch_acquire(monkeypatch, lambda directory: leadership)

    def already_exists(
        path: Path,
        timestamp: int,
        monotonic: Callable[[], float],
    ) -> _FakeStore:
        nonlocal create_calls
        create_calls += 1
        raise StoreError("already_exists")

    def fail_open(path: Path, monotonic: Callable[[], float]) -> _FakeStore:
        nonlocal open_calls
        open_calls += 1
        raise StoreError("open_failed")

    _patch_create(monkeypatch, already_exists)
    _patch_open(monkeypatch, fail_open)
    with pytest.raises(DatabaseLifecycleError) as captured:
        bootstrap_health_history_database(
            _settings(
                "/protected/history.sqlite3",
                mode=HealthHistoryDatabaseMode.CREATE_IF_MISSING,
            ),
            created_at_utc_us=10,
        )
    assert captured.value.reason is DatabaseLifecycleRejection.BOOTSTRAP_FAILED
    assert create_calls == 1
    assert open_calls == 1
    assert leadership.close_calls == 1


def test_bootstrap_cleanup_failure_supersedes_original_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leadership = _FakeLeadership(close_failures=1)
    _patch_acquire(monkeypatch, lambda directory: leadership)
    _patch_open(
        monkeypatch,
        lambda path, monotonic: (_ for _ in ()).throw(StoreError("open_failed")),
    )
    with pytest.raises(DatabaseLifecycleError) as captured:
        bootstrap_health_history_database(_settings("/protected/history.sqlite3"))
    assert captured.value.reason is DatabaseLifecycleRejection.CLEANUP_FAILED
    assert leadership.close_calls == 1


def test_normal_close_is_store_first_idempotent_and_hides_owned_handles() -> None:
    events: list[str] = []
    store = _FakeStore(events)
    leadership = _FakeLeadership(events)
    lifecycle = _lifecycle(store, leadership)
    assert lifecycle.store is cast(HealthHistoryStore, store)
    assert not lifecycle.closed
    assert not hasattr(lifecycle, "leadership")
    assert not hasattr(lifecycle, "connection")
    lifecycle.close()
    lifecycle.close()
    assert events == ["store_close", "leadership_close"]
    assert lifecycle.closed
    with pytest.raises(DatabaseLifecycleError) as captured:
        _ = lifecycle.store
    assert captured.value.reason is DatabaseLifecycleRejection.CLOSED


def test_store_close_failure_poisoning_retains_leadership_until_retry() -> None:
    events: list[str] = []
    store = _FakeStore(events, close_failures=1)
    leadership = _FakeLeadership(events)
    lifecycle = _lifecycle(store, leadership)
    with pytest.raises(DatabaseLifecycleError) as captured:
        lifecycle.close()
    assert captured.value.reason is DatabaseLifecycleRejection.CLEANUP_FAILED
    assert not lifecycle.closed
    assert leadership.held
    assert leadership.close_calls == 0
    with pytest.raises(DatabaseLifecycleError) as captured:
        _ = lifecycle.store
    assert captured.value.reason is DatabaseLifecycleRejection.CLEANUP_FAILED
    with pytest.raises(DatabaseLifecycleError) as captured:
        lifecycle.__enter__()
    assert captured.value.reason is DatabaseLifecycleRejection.CLEANUP_FAILED

    lifecycle.close()
    assert lifecycle.closed
    assert events == ["store_close", "store_close", "leadership_close"]


@pytest.mark.parametrize("reports_closed", [True, False])
def test_leadership_close_failure_is_terminal_and_never_retried(
    reports_closed: bool,
) -> None:
    events: list[str] = []
    store = _FakeStore(events)
    leadership = _FakeLeadership(
        events,
        close_failures=1,
        closes_on_failure=reports_closed,
    )
    lifecycle = _lifecycle(store, leadership)
    with pytest.raises(DatabaseLifecycleError) as captured:
        lifecycle.close()
    assert captured.value.reason is DatabaseLifecycleRejection.CLEANUP_FAILED
    assert not lifecycle.closed
    assert store.closed
    assert store.close_calls == 1
    assert leadership.closed is reports_closed
    assert leadership.close_calls == 1
    assert events == ["store_close", "leadership_close"]
    with pytest.raises(DatabaseLifecycleError) as captured:
        _ = lifecycle.store
    assert captured.value.reason is DatabaseLifecycleRejection.CLEANUP_FAILED
    with pytest.raises(DatabaseLifecycleError) as captured:
        lifecycle.__enter__()
    assert captured.value.reason is DatabaseLifecycleRejection.CLEANUP_FAILED
    with pytest.raises(DatabaseLifecycleError) as captured:
        lifecycle.close()
    assert captured.value.reason is DatabaseLifecycleRejection.CLEANUP_FAILED
    assert not lifecycle.closed
    assert store.close_calls == 1
    assert leadership.close_calls == 1
    assert events == ["store_close", "leadership_close"]


def test_context_manager_returns_self_releases_and_does_not_suppress() -> None:
    events: list[str] = []
    lifecycle = _lifecycle(_FakeStore(events), _FakeLeadership(events))
    with pytest.raises(ValueError, match="body-canary"):
        with lifecycle as entered:
            assert entered is lifecycle
            raise ValueError("body-canary")
    assert lifecycle.closed
    assert events == ["store_close", "leadership_close"]
    with pytest.raises(DatabaseLifecycleError) as captured:
        lifecycle.__enter__()
    assert captured.value.reason is DatabaseLifecycleRejection.CLOSED


@pytest.mark.parametrize("reason", list(DatabaseLifecycleRejection))
def test_lifecycle_errors_expose_only_fixed_reason(
    reason: DatabaseLifecycleRejection,
) -> None:
    error = DatabaseLifecycleError(reason)
    assert error.reason is reason
    assert str(error) == reason.value


def test_real_create_then_create_if_missing_reopens_without_replacement(
    history_test_directory: Path,
) -> None:
    path = history_test_directory / "history.sqlite3"
    settings = _settings(
        path,
        mode=HealthHistoryDatabaseMode.CREATE_IF_MISSING,
    )
    first = bootstrap_health_history_database(settings, created_at_utc_us=1)
    assert first is not None
    assert not first.closed
    first.store.verify()
    with pytest.raises(LeadershipError) as captured:
        HealthHistoryLeadership.acquire(history_test_directory)
    assert captured.value.reason is LeadershipRejection.BUSY
    first.close()
    identity = (path.stat().st_dev, path.stat().st_ino)

    second = bootstrap_health_history_database(settings, created_at_utc_us=2)
    assert second is not None
    second.store.verify()
    assert (path.stat().st_dev, path.stat().st_ino) == identity
    second.close()

    later = HealthHistoryLeadership.acquire(history_test_directory)
    later.close()
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
    finally:
        connection.close()


def test_real_open_existing_uses_verified_database(
    history_test_directory: Path,
) -> None:
    path = history_test_directory / "history.sqlite3"
    created = HealthHistoryStore.create(path, created_at_utc_us=1)
    created.close()
    identity = (path.stat().st_dev, path.stat().st_ino)
    lifecycle = bootstrap_health_history_database(_settings(path))
    assert lifecycle is not None
    lifecycle.store.verify()
    lifecycle.close()
    assert (path.stat().st_dev, path.stat().st_ino) == identity


def test_real_open_existing_missing_target_creates_no_database_artifacts(
    history_test_directory: Path,
) -> None:
    path = history_test_directory / "missing.sqlite3"
    with pytest.raises(DatabaseLifecycleError) as captured:
        bootstrap_health_history_database(_settings(path))
    assert captured.value.reason is DatabaseLifecycleRejection.BOOTSTRAP_FAILED
    assert not path.exists()
    assert not Path(f"{path}-wal").exists()
    assert not Path(f"{path}-shm").exists()
    later = HealthHistoryLeadership.acquire(history_test_directory)
    later.close()


def test_database_lifecycle_remains_absent_from_runtime_entry_points() -> None:
    root = Path(__file__).parents[1] / "src" / "aurora_core"
    paths = [
        root / "__main__.py",
        root / "dashboard" / "server.py",
        root / "dashboard" / "service.py",
        *sorted((root / "runtime").glob("*.py")),
    ]
    for path in paths:
        source = path.read_text(encoding="utf-8")
        assert "health_history.database_lifecycle" not in source
        assert "bootstrap_health_history_database" not in source
        assert "HealthHistoryDatabaseLifecycle" not in source


def test_database_lifecycle_source_has_no_deferred_runtime_behavior() -> None:
    source = Path(lifecycle_module.__file__).read_text(encoding="utf-8")
    prohibited = (
        "sqlite3",
        "HealthHistoryOrchestrator",
        "HealthHistoryScheduler",
        "HealthService",
        "storage_envelope",
        "retention_days",
        "sample_interval_seconds",
        "threading",
        "Thread(",
        "create_task",
        "sleep(",
        "subprocess",
        "socket",
        "requests",
        "os.environ",
        ".exists(",
        ".stat(",
        ".lstat(",
        ".resolve(",
        ".absolute(",
        ".expanduser(",
        "expandvars",
        "validate_protected_directory",
    )
    for token in prohibited:
        assert token not in source


def test_fixed_leadership_filename_is_the_exported_artifact_policy() -> None:
    assert HEALTH_HISTORY_LEADERSHIP_LOCK_FILENAME == ".aurora-health-history.lock"
