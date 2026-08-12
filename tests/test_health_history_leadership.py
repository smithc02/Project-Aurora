"""Direct-only tests for the Milestone 18 history leadership lock."""

from __future__ import annotations

import errno
import inspect
import multiprocessing
import os
import stat
from dataclasses import replace
from multiprocessing.connection import Connection
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import aurora_core.health_history.leadership as leadership_module
from aurora_core.health_history import (
    HEALTH_HISTORY_LEADERSHIP_LOCK_FILENAME,
    HealthHistoryLeadership,
    LeadershipError,
    LeadershipRejection,
)

_LOCK_NAME = ".aurora-health-history.lock"
_CHILD_TIMEOUT_SECONDS = 10.0


def _child_acquire(directory: str, connection: Connection, *, crash: bool) -> None:
    try:
        leadership = HealthHistoryLeadership.acquire(Path(directory))
    except LeadershipError as error:
        connection.send(error.reason.value)
        connection.close()
        return
    connection.send("acquired")
    connection.close()
    if crash:
        return
    leadership.close()


def _spawn_acquire(directory: Path, *, crash: bool = False) -> tuple[str, int]:
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(
        target=_child_acquire,
        args=(str(directory), child),
        kwargs={"crash": crash},
    )
    coverage_environment = {
        name: value
        for name, value in os.environ.items()
        if name.startswith("COV_CORE_")
    }
    for name in coverage_environment:
        os.environ.pop(name)
    try:
        process.start()
    finally:
        os.environ.update(coverage_environment)
    child.close()
    try:
        assert parent.poll(_CHILD_TIMEOUT_SECONDS)
        result = cast(str, parent.recv())
        process.join(_CHILD_TIMEOUT_SECONDS)
        assert not process.is_alive()
        assert process.exitcode is not None
        return result, process.exitcode
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
            process.join(_CHILD_TIMEOUT_SECONDS)


def _secure_empty_file(path: Path) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def _lock_path(directory: Path) -> Path:
    return directory / _LOCK_NAME


def _changed_stat(
    metadata: os.stat_result,
    *,
    inode_delta: int = 0,
    mode: int | None = None,
    size: int | None = None,
) -> os.stat_result:
    values = list(metadata)
    values[0] = metadata.st_mode if mode is None else mode
    values[1] = metadata.st_ino + inode_delta
    values[6] = metadata.st_size if size is None else size
    return os.stat_result(values)


def test_missing_lock_is_securely_created_and_remains_empty(
    history_test_directory: Path,
) -> None:
    with HealthHistoryLeadership.acquire(history_test_directory) as leadership:
        assert leadership.held
        assert not leadership.closed
        path = _lock_path(history_test_directory)
        metadata = path.lstat()
        assert stat.S_ISREG(metadata.st_mode)
        assert stat.S_IMODE(metadata.st_mode) == 0o600
        assert metadata.st_uid == os.geteuid()
        assert metadata.st_nlink == 1
        assert metadata.st_size == 0
        assert path.read_bytes() == b""
    assert leadership.closed
    assert not leadership.held
    assert _lock_path(history_test_directory).read_bytes() == b""


def test_existing_secure_lock_is_reused_without_chmod_or_truncation(
    history_test_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _lock_path(history_test_directory)
    _secure_empty_file(path)
    identity = (path.stat().st_dev, path.stat().st_ino)

    def unexpected_fchmod(descriptor: int, mode: int) -> None:
        raise AssertionError("existing lock must not be repaired")

    monkeypatch.setattr(leadership_module.os, "fchmod", unexpected_fchmod)
    leadership = HealthHistoryLeadership.acquire(history_test_directory)
    leadership.close()
    assert (path.stat().st_dev, path.stat().st_ino) == identity
    assert path.read_bytes() == b""


def test_concurrent_secure_creation_is_reopened_once(
    history_test_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_open = os.open
    collision_count = 0

    def collide_once(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal collision_count
        if path == _LOCK_NAME and flags & os.O_EXCL and collision_count == 0:
            collision_count += 1
            descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            os.close(descriptor)
            raise FileExistsError
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(leadership_module, "_require_supported_runtime", lambda: None)
    monkeypatch.setattr(leadership_module.os, "open", collide_once)
    leadership = HealthHistoryLeadership.acquire(history_test_directory)
    leadership.close()
    assert collision_count == 1
    assert _lock_path(history_test_directory).read_bytes() == b""


def test_failed_new_file_validation_closes_the_created_descriptor(
    history_test_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_descriptors: list[int] = []

    def fail_fchmod(descriptor: int, mode: int) -> None:
        created_descriptors.append(descriptor)
        raise OSError(errno.EIO, "private-fchmod-canary")

    monkeypatch.setattr(leadership_module.os, "fchmod", fail_fchmod)
    with pytest.raises(LeadershipError) as captured:
        HealthHistoryLeadership.acquire(history_test_directory)
    assert captured.value.reason is LeadershipRejection.TRUST_FAILED
    assert len(created_descriptors) == 1
    with pytest.raises(OSError) as closed:
        os.fstat(created_descriptors[0])
    assert closed.value.errno == errno.EBADF


def test_fixed_name_is_not_a_caller_argument() -> None:
    signature = inspect.signature(HealthHistoryLeadership.acquire)
    assert tuple(signature.parameters) == ("directory",)
    assert HEALTH_HISTORY_LEADERSHIP_LOCK_FILENAME == _LOCK_NAME


@pytest.mark.parametrize("mode", [0o755, 0o770, 0o777])
def test_insecure_ancestry_is_rejected_before_lock_creation(
    history_test_directory: Path,
    mode: int,
) -> None:
    history_test_directory.chmod(mode)
    with pytest.raises(LeadershipError) as captured:
        HealthHistoryLeadership.acquire(history_test_directory)
    assert captured.value.reason is LeadershipRejection.TRUST_FAILED
    assert not _lock_path(history_test_directory).exists()


@pytest.mark.parametrize(
    "object_kind",
    ["symlink", "directory", "hard_link", "wrong_mode", "nonempty", "fifo"],
)
def test_existing_insecure_object_is_rejected_unchanged(
    history_test_directory: Path,
    object_kind: str,
) -> None:
    path = _lock_path(history_test_directory)
    outside = history_test_directory / "outside"
    if object_kind == "symlink":
        _secure_empty_file(outside)
        path.symlink_to(outside)
    elif object_kind == "directory":
        path.mkdir(mode=0o700)
    elif object_kind == "hard_link":
        _secure_empty_file(path)
        os.link(path, outside)
    elif object_kind == "wrong_mode":
        _secure_empty_file(path)
        path.chmod(0o640)
    elif object_kind == "nonempty":
        path.write_bytes(b"private-lock-content-canary")
        path.chmod(0o600)
    else:
        os.mkfifo(path, mode=0o600)
    before = path.lstat()
    content = path.read_bytes() if object_kind == "nonempty" else None

    with pytest.raises(LeadershipError) as captured:
        HealthHistoryLeadership.acquire(history_test_directory)

    after = path.lstat()
    assert captured.value.reason is LeadershipRejection.TRUST_FAILED
    assert (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_nlink,
        after.st_size,
    ) == (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_nlink,
        before.st_size,
    )
    if content is not None:
        assert path.read_bytes() == content


def test_foreign_owner_metadata_is_rejected() -> None:
    metadata = SimpleNamespace(
        st_mode=stat.S_IFREG | 0o600,
        st_uid=os.geteuid() + 1,
        st_nlink=1,
        st_size=0,
    )
    with pytest.raises(LeadershipError) as captured:
        leadership_module._validate_lock_metadata(cast(os.stat_result, metadata))
    assert captured.value.reason is LeadershipRejection.TRUST_FAILED


def test_path_entry_descriptor_identity_mismatch_is_rejected(
    history_test_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _lock_path(history_test_directory)
    _secure_empty_file(path)
    real_stat = leadership_module._stat_lock_entry
    calls = 0

    def mismatched_entry(descriptor: int) -> os.stat_result:
        nonlocal calls
        calls += 1
        metadata = real_stat(descriptor)
        return _changed_stat(metadata, inode_delta=1) if calls == 1 else metadata

    monkeypatch.setattr(leadership_module, "_stat_lock_entry", mismatched_entry)
    with pytest.raises(LeadershipError) as captured:
        HealthHistoryLeadership.acquire(history_test_directory)
    assert captured.value.reason is LeadershipRejection.TRUST_FAILED


@pytest.mark.parametrize("replacement", ["file", "symlink"])
def test_entry_replacement_before_post_flock_validation_is_rejected(
    history_test_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str,
) -> None:
    path = _lock_path(history_test_directory)
    outside = history_test_directory / "replacement"
    real_open = leadership_module._open_lock_file

    def open_then_replace(directory_descriptor: int) -> int:
        descriptor = real_open(directory_descriptor)
        path.unlink()
        if replacement == "symlink":
            _secure_empty_file(outside)
            path.symlink_to(outside)
        else:
            _secure_empty_file(path)
        return descriptor

    monkeypatch.setattr(leadership_module, "_open_lock_file", open_then_replace)
    with pytest.raises(LeadershipError) as captured:
        HealthHistoryLeadership.acquire(history_test_directory)
    assert captured.value.reason is LeadershipRejection.TRUST_FAILED


@pytest.mark.parametrize("mutation", ["mode", "link", "size"])
def test_metadata_change_after_successful_flock_is_rejected(
    history_test_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    path = _lock_path(history_test_directory)
    real_flock = leadership_module._flock

    def flock_then_mutate(descriptor: int, operation: int) -> None:
        real_flock(descriptor, operation)
        if operation & leadership_module._lock_exclusive_flag():
            if mutation == "mode":
                path.chmod(0o640)
            elif mutation == "link":
                os.link(path, history_test_directory / "second-link")
            else:
                with path.open("wb") as lock_file:
                    lock_file.write(b"x")

    monkeypatch.setattr(leadership_module, "_flock", flock_then_mutate)
    with pytest.raises(LeadershipError) as captured:
        HealthHistoryLeadership.acquire(history_test_directory)
    assert captured.value.reason is LeadershipRejection.TRUST_FAILED


def test_directory_identity_mismatch_after_flock_is_rejected(
    history_test_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_validate = leadership_module.validate_protected_directory
    calls = 0

    def changed_directory(path: Path):
        nonlocal calls
        calls += 1
        identity = real_validate(path)
        return replace(identity, inode=identity.inode + 1) if calls == 2 else identity

    monkeypatch.setattr(
        leadership_module,
        "validate_protected_directory",
        changed_directory,
    )
    with pytest.raises(LeadershipError) as captured:
        HealthHistoryLeadership.acquire(history_test_directory)
    assert captured.value.reason is LeadershipRejection.TRUST_FAILED


def test_directory_metadata_helpers_reject_wrong_type_links_and_replacement() -> None:
    metadata = SimpleNamespace(
        st_mode=stat.S_IFREG | 0o700,
        st_dev=1,
        st_ino=2,
        st_uid=os.geteuid(),
        st_nlink=1,
    )
    expected = leadership_module.PathIdentity(1, 2, 0o700, os.geteuid(), 2, 0)
    with pytest.raises(LeadershipError):
        leadership_module._require_directory_identity(
            cast(os.stat_result, metadata), expected
        )
    metadata.st_mode = stat.S_IFDIR | 0o700
    with pytest.raises(LeadershipError):
        leadership_module._require_directory_identity(
            cast(os.stat_result, metadata), expected
        )
    with pytest.raises(LeadershipError):
        leadership_module._require_same_directory_identity(
            expected,
            replace(expected, inode=3),
        )


def test_acquisition_uses_one_nonblocking_exclusive_flock(
    history_test_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_flock = leadership_module._flock
    operations: list[int] = []

    def record_flock(descriptor: int, operation: int) -> None:
        operations.append(operation)
        real_flock(descriptor, operation)

    monkeypatch.setattr(leadership_module, "_flock", record_flock)
    leadership = HealthHistoryLeadership.acquire(history_test_directory)
    expected = (
        leadership_module._lock_exclusive_flag()
        | leadership_module._lock_nonblocking_flag()
    )
    assert operations == [expected]
    leadership.close()
    assert operations == [expected, leadership_module._lock_un_flag()]


def test_same_process_independent_acquisition_is_busy_then_reusable(
    history_test_directory: Path,
) -> None:
    first = HealthHistoryLeadership.acquire(history_test_directory)
    try:
        with pytest.raises(LeadershipError) as captured:
            HealthHistoryLeadership.acquire(history_test_directory)
        assert captured.value.reason is LeadershipRejection.BUSY
    finally:
        first.close()
    second = HealthHistoryLeadership.acquire(history_test_directory)
    second.close()


def test_busy_and_operational_flock_errors_are_fixed_and_sanitized(
    history_test_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private = "private-flock-canary"
    for error, expected in (
        (BlockingIOError(errno.EAGAIN, private), LeadershipRejection.BUSY),
        (OSError(errno.EIO, private), LeadershipRejection.ACQUISITION_FAILED),
    ):
        calls = 0

        def fail_flock(
            descriptor: int,
            operation: int,
            selected_error: OSError = error,
        ) -> None:
            nonlocal calls
            calls += 1
            raise selected_error

        monkeypatch.setattr(leadership_module, "_flock", fail_flock)
        with pytest.raises(LeadershipError) as captured:
            HealthHistoryLeadership.acquire(history_test_directory)
        assert captured.value.reason is expected
        assert str(captured.value) == expected.value
        assert private not in str(captured.value)
        assert calls == 1


def test_context_manager_and_idempotent_close(
    history_test_directory: Path,
) -> None:
    leadership = HealthHistoryLeadership.acquire(history_test_directory)
    with leadership as entered:
        assert entered is leadership
        assert leadership.held
    assert leadership.closed
    leadership.close()
    with pytest.raises(LeadershipError) as captured:
        leadership.__enter__()
    assert captured.value.reason is LeadershipRejection.RELEASE_FAILED


def test_release_error_still_closes_and_releases_descriptor(
    history_test_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leadership = HealthHistoryLeadership.acquire(history_test_directory)
    real_flock = leadership_module._flock

    def fail_unlock(descriptor: int, operation: int) -> None:
        if operation == leadership_module._lock_un_flag():
            raise OSError(errno.EIO, "private-release-canary")
        real_flock(descriptor, operation)

    monkeypatch.setattr(leadership_module, "_flock", fail_unlock)
    with pytest.raises(LeadershipError) as captured:
        leadership.close()
    assert captured.value.reason is LeadershipRejection.RELEASE_FAILED
    assert str(captured.value) == "release_failed"
    assert leadership.closed
    monkeypatch.setattr(leadership_module, "_flock", real_flock)
    later = HealthHistoryLeadership.acquire(history_test_directory)
    later.close()


def test_close_failure_is_sanitized_after_the_descriptor_is_closed(
    history_test_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leadership = HealthHistoryLeadership.acquire(history_test_directory)
    descriptor = leadership._descriptor
    assert descriptor is not None
    real_close = os.close

    def close_then_fail(selected: int) -> None:
        real_close(selected)
        if selected == descriptor:
            raise OSError(errno.EIO, "private-close-canary")

    monkeypatch.setattr(leadership_module.os, "close", close_then_fail)
    with pytest.raises(LeadershipError) as captured:
        leadership.close()
    assert captured.value.reason is LeadershipRejection.RELEASE_FAILED
    assert str(captured.value) == "release_failed"
    assert leadership.closed


@pytest.mark.parametrize("failure", ["early", "busy"])
def test_failure_paths_close_directory_and_lock_descriptors(
    history_test_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    captured_descriptors: list[int] = []
    real_open_lock = leadership_module._open_lock_file

    def capture_open(directory_descriptor: int) -> int:
        captured_descriptors.append(directory_descriptor)
        if failure == "early":
            raise LeadershipError(LeadershipRejection.TRUST_FAILED)
        descriptor = real_open_lock(directory_descriptor)
        captured_descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(leadership_module, "_open_lock_file", capture_open)
    if failure == "busy":
        monkeypatch.setattr(
            leadership_module,
            "_acquire_nonblocking",
            lambda descriptor: (_ for _ in ()).throw(
                LeadershipError(LeadershipRejection.BUSY)
            ),
        )
    with pytest.raises(LeadershipError):
        HealthHistoryLeadership.acquire(history_test_directory)
    for descriptor in captured_descriptors:
        with pytest.raises(OSError) as closed:
            os.fstat(descriptor)
        assert closed.value.errno == errno.EBADF


def test_success_closes_directory_descriptor_but_retains_lock(
    history_test_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory_descriptors: list[int] = []
    real_open_lock = leadership_module._open_lock_file

    def capture_open(directory_descriptor: int) -> int:
        directory_descriptors.append(directory_descriptor)
        return real_open_lock(directory_descriptor)

    monkeypatch.setattr(leadership_module, "_open_lock_file", capture_open)
    leadership = HealthHistoryLeadership.acquire(history_test_directory)
    assert leadership.held
    with pytest.raises(OSError) as closed:
        os.fstat(directory_descriptors[0])
    assert closed.value.errno == errno.EBADF
    leadership.close()


def test_unsupported_runtime_fails_before_creating_lock(
    history_test_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(leadership_module, "_fcntl_module", None)
    with pytest.raises(LeadershipError) as captured:
        HealthHistoryLeadership.acquire(history_test_directory)
    assert captured.value.reason is LeadershipRejection.UNSUPPORTED_RUNTIME
    assert not _lock_path(history_test_directory).exists()


def test_malformed_fcntl_capabilities_fail_closed() -> None:
    module_without_flock = SimpleNamespace(LOCK_EX=2, LOCK_NB=4, LOCK_UN=8)
    original = leadership_module._fcntl_module
    leadership_module._fcntl_module = cast(
        leadership_module.ModuleType, module_without_flock
    )
    try:
        with pytest.raises(LeadershipError) as captured:
            leadership_module._flock(1, 2)
        assert captured.value.reason is LeadershipRejection.UNSUPPORTED_RUNTIME
        module_without_flock.flock = lambda descriptor, operation: None
        module_without_flock.LOCK_EX = True
        with pytest.raises(LeadershipError) as captured:
            leadership_module._lock_exclusive_flag()
        assert captured.value.reason is LeadershipRejection.UNSUPPORTED_RUNTIME
    finally:
        leadership_module._fcntl_module = original


def test_spawned_process_contention_and_release(
    history_test_directory: Path,
) -> None:
    parent_leadership = HealthHistoryLeadership.acquire(history_test_directory)
    try:
        result, exit_code = _spawn_acquire(history_test_directory)
        assert result == LeadershipRejection.BUSY.value
        assert exit_code == 0
    finally:
        parent_leadership.close()
    result, exit_code = _spawn_acquire(history_test_directory)
    assert result == "acquired"
    assert exit_code == 0


def test_spawned_process_crash_releases_kernel_lock_and_keeps_file(
    history_test_directory: Path,
) -> None:
    result, exit_code = _spawn_acquire(history_test_directory, crash=True)
    assert result == "acquired"
    assert exit_code == 0
    leadership = HealthHistoryLeadership.acquire(history_test_directory)
    leadership.close()
    path = _lock_path(history_test_directory)
    assert path.is_file()
    assert path.read_bytes() == b""


def test_errors_expose_no_path_or_metadata(
    history_test_directory: Path,
) -> None:
    private = "private-leadership-path-canary"
    target = history_test_directory / private
    target.mkdir(mode=0o755)
    with pytest.raises(LeadershipError) as captured:
        HealthHistoryLeadership.acquire(target)
    message = str(captured.value)
    assert message == LeadershipRejection.TRUST_FAILED.value
    assert private not in message
    assert str(os.geteuid()) not in message


def test_module_is_direct_only_and_has_no_sqlite_or_runtime_dependencies() -> None:
    source = inspect.getsource(leadership_module)
    for prohibited in (
        "sqlite3",
        "HealthHistoryStore",
        "HealthHistoryOrchestrator",
        "HealthHistoryScheduler",
        "aurora_core.dashboard",
        "aurora_core.runtime",
        "sleep(",
        "Thread(",
    ):
        assert prohibited not in source
