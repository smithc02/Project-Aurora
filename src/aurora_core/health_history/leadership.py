"""Direct-only cross-process leadership for future history scheduling."""

from __future__ import annotations

import errno
import importlib
import os
import stat
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from types import ModuleType, TracebackType
from typing import cast

from aurora_core.health_history.filesystem import (
    FilesystemBoundaryError,
    PathIdentity,
    validate_protected_directory,
)

_LOCK_FILENAME = ".aurora-health-history.lock"
_LOCK_FILE_MODE = 0o600

try:
    _fcntl_module: ModuleType | None = importlib.import_module("fcntl")
except ImportError:  # pragma: no cover - the supported target is POSIX.
    _fcntl_module = None


class LeadershipRejection(StrEnum):
    """Fixed sanitized leadership failure registry."""

    BUSY = "busy"
    TRUST_FAILED = "trust_failed"
    UNSUPPORTED_RUNTIME = "unsupported_runtime"
    ACQUISITION_FAILED = "acquisition_failed"
    RELEASE_FAILED = "release_failed"


class LeadershipError(Exception):
    """Sanitized history-leadership failure."""

    def __init__(self, reason: LeadershipRejection) -> None:
        super().__init__(reason.value)
        self.reason = reason


class HealthHistoryLeadership:
    """Own one nonblocking kernel lock until explicitly closed."""

    __slots__ = ("_descriptor",)

    def __init__(self, descriptor: int) -> None:
        self._descriptor: int | None = descriptor

    @classmethod
    def acquire(cls, directory: Path) -> HealthHistoryLeadership:
        """Acquire the fixed history lock without waiting or retrying."""
        _require_supported_runtime()
        directory_descriptor: int | None = None
        lock_descriptor: int | None = None
        lock_held = False
        try:
            expected_directory = validate_protected_directory(directory)
            directory_descriptor = os.open(directory, _directory_flags())
            _require_directory_identity(
                os.fstat(directory_descriptor), expected_directory
            )
            lock_descriptor = _open_lock_file(directory_descriptor)
            expected_lock = _lock_identity(os.fstat(lock_descriptor))
            _acquire_nonblocking(lock_descriptor)
            lock_held = True
            _require_stable_lock(
                directory_descriptor,
                lock_descriptor,
                expected_lock,
            )
            current_directory = os.fstat(directory_descriptor)
            _require_directory_identity(
                current_directory,
                expected_directory,
                allow_entry_count_change=True,
            )
            revalidated_directory = validate_protected_directory(directory)
            _require_directory_identity(current_directory, revalidated_directory)
            _require_same_directory_identity(
                expected_directory,
                revalidated_directory,
            )
            _require_stable_lock(
                directory_descriptor,
                lock_descriptor,
                expected_lock,
            )
            leadership = cls(lock_descriptor)
            lock_descriptor = None
            lock_held = False
            return leadership
        except LeadershipError:
            raise
        except (FilesystemBoundaryError, OSError, TypeError, ValueError):
            raise LeadershipError(LeadershipRejection.TRUST_FAILED) from None
        finally:
            if lock_descriptor is not None:
                if lock_held:
                    _best_effort_unlock(lock_descriptor)
                _best_effort_close(lock_descriptor)
            if directory_descriptor is not None:
                _best_effort_close(directory_descriptor)

    @property
    def held(self) -> bool:
        """Return whether this handle still owns its descriptor."""
        return self._descriptor is not None

    @property
    def closed(self) -> bool:
        """Return whether this handle has released its descriptor."""
        return self._descriptor is None

    def close(self) -> None:
        """Release and close exactly once; later calls are no-ops."""
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        release_failed = False
        try:
            _flock(descriptor, _lock_un_flag())
        except (LeadershipError, OSError):
            release_failed = True
        try:
            os.close(descriptor)
        except OSError:
            release_failed = True
        if release_failed:
            raise LeadershipError(LeadershipRejection.RELEASE_FAILED) from None

    def __enter__(self) -> HealthHistoryLeadership:
        if self.closed:
            raise LeadershipError(LeadershipRejection.RELEASE_FAILED)
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def _open_lock_file(directory_descriptor: int) -> int:
    try:
        before = _stat_lock_entry(directory_descriptor)
    except FileNotFoundError:
        return _create_or_open_raced_lock(directory_descriptor)
    _validate_lock_metadata(before)
    descriptor = os.open(
        _LOCK_FILENAME,
        _existing_lock_flags(),
        dir_fd=directory_descriptor,
    )
    try:
        opened = os.fstat(descriptor)
        _validate_lock_metadata(opened)
        _require_same_lock(before, opened)
        after = _stat_lock_entry(directory_descriptor)
        _validate_lock_metadata(after)
        _require_same_lock(opened, after)
        return descriptor
    except Exception:
        _best_effort_close(descriptor)
        raise


def _create_or_open_raced_lock(directory_descriptor: int) -> int:
    try:
        descriptor = os.open(
            _LOCK_FILENAME,
            _new_lock_flags(),
            _LOCK_FILE_MODE,
            dir_fd=directory_descriptor,
        )
    except FileExistsError:
        return _open_lock_after_creation_race(directory_descriptor)
    try:
        os.fchmod(descriptor, _LOCK_FILE_MODE)
        opened = os.fstat(descriptor)
        _validate_lock_metadata(opened)
        after = _stat_lock_entry(directory_descriptor)
        _validate_lock_metadata(after)
        _require_same_lock(opened, after)
        return descriptor
    except Exception:
        _best_effort_close(descriptor)
        raise


def _open_lock_after_creation_race(directory_descriptor: int) -> int:
    before = _stat_lock_entry(directory_descriptor)
    _validate_lock_metadata(before)
    descriptor = os.open(
        _LOCK_FILENAME,
        _existing_lock_flags(),
        dir_fd=directory_descriptor,
    )
    try:
        opened = os.fstat(descriptor)
        _validate_lock_metadata(opened)
        _require_same_lock(before, opened)
        after = _stat_lock_entry(directory_descriptor)
        _validate_lock_metadata(after)
        _require_same_lock(opened, after)
        return descriptor
    except Exception:
        _best_effort_close(descriptor)
        raise


def _require_stable_lock(
    directory_descriptor: int,
    lock_descriptor: int,
    expected: tuple[int, int, int, int, int, int, int],
) -> None:
    opened = os.fstat(lock_descriptor)
    _validate_lock_metadata(opened)
    if _lock_identity(opened) != expected:
        raise LeadershipError(LeadershipRejection.TRUST_FAILED)
    entry = _stat_lock_entry(directory_descriptor)
    _validate_lock_metadata(entry)
    if _lock_identity(entry) != expected:
        raise LeadershipError(LeadershipRejection.TRUST_FAILED)


def _validate_lock_metadata(metadata: os.stat_result) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise LeadershipError(LeadershipRejection.TRUST_FAILED)
    if metadata.st_uid != os.geteuid():
        raise LeadershipError(LeadershipRejection.TRUST_FAILED)
    if stat.S_IMODE(metadata.st_mode) != _LOCK_FILE_MODE:
        raise LeadershipError(LeadershipRejection.TRUST_FAILED)
    if metadata.st_nlink != 1 or metadata.st_size != 0:
        raise LeadershipError(LeadershipRejection.TRUST_FAILED)


def _stat_lock_entry(directory_descriptor: int) -> os.stat_result:
    return os.stat(
        _LOCK_FILENAME,
        dir_fd=directory_descriptor,
        follow_symlinks=False,
    )


def _require_same_lock(left: os.stat_result, right: os.stat_result) -> None:
    if _lock_identity(left) != _lock_identity(right):
        raise LeadershipError(LeadershipRejection.TRUST_FAILED)


def _lock_identity(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IFMT(metadata.st_mode),
        stat.S_IMODE(metadata.st_mode),
        metadata.st_uid,
        metadata.st_nlink,
        metadata.st_size,
    )


def _require_directory_identity(
    metadata: os.stat_result,
    expected: PathIdentity,
    *,
    allow_entry_count_change: bool = False,
) -> None:
    if not stat.S_ISDIR(metadata.st_mode):
        raise LeadershipError(LeadershipRejection.TRUST_FAILED)
    actual = (
        metadata.st_dev,
        metadata.st_ino,
        stat.S_IMODE(metadata.st_mode),
        metadata.st_uid,
    )
    expected_security = (
        expected.device,
        expected.inode,
        expected.mode,
        expected.owner,
    )
    if actual != expected_security:
        raise LeadershipError(LeadershipRejection.TRUST_FAILED)
    if not allow_entry_count_change and metadata.st_nlink != expected.links:
        raise LeadershipError(LeadershipRejection.TRUST_FAILED)


def _require_same_directory_identity(
    left: PathIdentity,
    right: PathIdentity,
) -> None:
    left_security = (
        left.device,
        left.inode,
        left.mode,
        left.owner,
    )
    right_security = (
        right.device,
        right.inode,
        right.mode,
        right.owner,
    )
    if left_security != right_security:
        raise LeadershipError(LeadershipRejection.TRUST_FAILED)


def _acquire_nonblocking(descriptor: int) -> None:
    try:
        _flock(descriptor, _lock_exclusive_flag() | _lock_nonblocking_flag())
    except OSError as error:
        if isinstance(error, BlockingIOError) or error.errno in {
            errno.EACCES,
            errno.EAGAIN,
            errno.EWOULDBLOCK,
        }:
            raise LeadershipError(LeadershipRejection.BUSY) from None
        raise LeadershipError(LeadershipRejection.ACQUISITION_FAILED) from None


def _best_effort_unlock(descriptor: int) -> None:
    try:
        _flock(descriptor, _lock_un_flag())
    except (LeadershipError, OSError):
        pass


def _best_effort_close(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


def _flock(descriptor: int, operation: int) -> None:
    module = _fcntl_module
    if module is None:
        raise LeadershipError(LeadershipRejection.UNSUPPORTED_RUNTIME)
    flock = getattr(module, "flock", None)
    if not callable(flock):
        raise LeadershipError(LeadershipRejection.UNSUPPORTED_RUNTIME)
    cast(Callable[[int, int], None], flock)(descriptor, operation)


def _lock_exclusive_flag() -> int:
    return _fcntl_flag("LOCK_EX")


def _lock_nonblocking_flag() -> int:
    return _fcntl_flag("LOCK_NB")


def _lock_un_flag() -> int:
    return _fcntl_flag("LOCK_UN")


def _fcntl_flag(name: str) -> int:
    module = _fcntl_module
    value = None if module is None else getattr(module, name, None)
    if type(value) is not int:
        raise LeadershipError(LeadershipRejection.UNSUPPORTED_RUNTIME)
    return value


def _require_supported_runtime() -> None:
    if (
        _fcntl_module is None
        or not callable(getattr(_fcntl_module, "flock", None))
        or any(
            not hasattr(os, name) for name in ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW")
        )
        or os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.stat not in os.supports_follow_symlinks
        or not callable(getattr(os, "fchmod", None))
        or not callable(getattr(os, "fstat", None))
        or not callable(getattr(os, "geteuid", None))
    ):
        raise LeadershipError(LeadershipRejection.UNSUPPORTED_RUNTIME)
    _lock_exclusive_flag()
    _lock_nonblocking_flag()
    _lock_un_flag()


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW


def _existing_lock_flags() -> int:
    return os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW


def _new_lock_flags() -> int:
    return _existing_lock_flags() | os.O_CREAT | os.O_EXCL
