"""Fail-closed filesystem boundary for Aurora history databases."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from aurora_core.health_history.models import (
    MAX_DATABASE_BYTES,
    MAX_SHARED_MEMORY_BYTES,
    MAX_WAL_BYTES,
)

_DESCRIPTOR_TRAVERSAL_SUPPORTED = (
    hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
)


class FilesystemRejection(StrEnum):
    MISSING = "missing"
    SYMLINK = "symlink"
    WRONG_TYPE = "wrong_type"
    WRONG_OWNER = "wrong_owner"
    WRONG_MODE = "wrong_mode"
    HARD_LINK = "hard_link"
    IDENTITY_CHANGED = "identity_changed"
    FILE_TOO_LARGE = "file_too_large"
    ALREADY_EXISTS = "already_exists"
    INVALID_PATH = "invalid_path"


class FilesystemBoundaryError(Exception):
    """Sanitized filesystem-boundary failure."""

    def __init__(self, reason: FilesystemRejection) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class PathIdentity:
    device: int
    inode: int
    mode: int
    owner: int
    links: int
    size: int


@dataclass(frozen=True, slots=True)
class _DirectorySnapshot:
    device: int
    inode: int
    file_type: int
    mode: int
    owner: int
    links: int


@dataclass(frozen=True, slots=True)
class _DirectoryWalk:
    components: tuple[_DirectorySnapshot, ...]
    final_identity: PathIdentity


def validate_protected_directory(path: Path) -> PathIdentity:
    """Require trusted ancestry ending in one owned mode-0700 directory."""
    _require_path(path)
    try:
        absolute, components = _directory_components(path)
        first = _walk_directory_chain(absolute, components)
        second = _walk_directory_chain(
            absolute,
            components,
            expected=first.components,
        )
        if first.final_identity != second.final_identity:
            raise FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED)
        return second.final_identity
    except FilesystemBoundaryError:
        raise
    except OSError as error:
        raise FilesystemBoundaryError(FilesystemRejection.INVALID_PATH) from error


def validate_database_file(
    path: Path,
    *,
    expected: PathIdentity | None = None,
    maximum_bytes: int = MAX_DATABASE_BYTES,
) -> PathIdentity:
    """Validate one existing database through path and no-follow descriptor checks."""
    _require_database_path(path)
    validate_protected_directory(path.parent)
    try:
        before = path.lstat()
    except OSError as error:
        raise FilesystemBoundaryError(FilesystemRejection.MISSING) from error
    _validate_file_metadata(before, maximum_bytes=maximum_bytes)
    descriptor = os.open(path, _read_flags())
    try:
        opened = os.fstat(descriptor)
        _validate_file_metadata(opened, maximum_bytes=maximum_bytes)
        _require_unchanged(before, opened)
    except OSError as error:
        raise FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED) from error
    finally:
        os.close(descriptor)
    try:
        after = path.lstat()
    except OSError as error:
        raise FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED) from error
    _validate_file_metadata(after, maximum_bytes=maximum_bytes)
    _require_unchanged(before, after)
    identity = _identity(after)
    if expected is not None and not _same_managed_identity(identity, expected):
        raise FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED)
    return identity


def create_database_file(path: Path) -> PathIdentity:
    """Exclusively create one empty mode-0600 database file."""
    _require_database_path(path)
    validate_protected_directory(path.parent)
    for candidate in _managed_paths(path):
        try:
            candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise FilesystemBoundaryError(FilesystemRejection.INVALID_PATH) from error
        raise FilesystemBoundaryError(FilesystemRejection.ALREADY_EXISTS)
    try:
        descriptor = os.open(path, _create_flags(), 0o600)
    except FileExistsError as error:
        raise FilesystemBoundaryError(FilesystemRejection.ALREADY_EXISTS) from error
    except OSError as error:
        raise FilesystemBoundaryError(FilesystemRejection.INVALID_PATH) from error
    created_identity = _identity(os.fstat(descriptor))
    identity: PathIdentity | None = None
    try:
        os.fchmod(descriptor, 0o600)
        metadata = os.fstat(descriptor)
        _validate_file_metadata(metadata, maximum_bytes=0)
        if metadata.st_size != 0:
            raise FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED)
        os.fsync(descriptor)
        identity = _identity(metadata)
    except Exception:
        os.close(descriptor)
        _unlink_created_file(path, identity or created_identity)
        raise
    os.close(descriptor)
    validate_database_file(path, expected=identity)
    fsync_directory(path.parent)
    return identity


def validate_sidecars(
    path: Path, *, maximum_wal_bytes: int = MAX_WAL_BYTES
) -> dict[str, PathIdentity]:
    """Validate SQLite's two code-derived sidecars when they exist."""
    identities: dict[str, PathIdentity] = {}
    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(f"{path.name}{suffix}")
        try:
            sidecar.lstat()
        except FileNotFoundError:
            continue
        maximum = maximum_wal_bytes if suffix == "-wal" else MAX_SHARED_MEMORY_BYTES
        identities[suffix] = validate_database_file(sidecar, maximum_bytes=maximum)
    return identities


def require_stable_sidecars(
    before: dict[str, PathIdentity], after: dict[str, PathIdentity]
) -> None:
    """Reject replacement of a sidecar that existed before an SQLite operation."""
    for suffix, identity in before.items():
        current = after.get(suffix)
        if current is None or not _same_managed_identity(current, identity):
            raise FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED)


def fsync_database_files(path: Path) -> None:
    """Fsync the validated main file, existing sidecars, and containing directory."""
    for candidate in (
        path,
        path.with_name(f"{path.name}-wal"),
        path.with_name(f"{path.name}-shm"),
    ):
        try:
            candidate.lstat()
        except FileNotFoundError:
            continue
        maximum = (
            MAX_WAL_BYTES
            if candidate.name.endswith("-wal")
            else MAX_SHARED_MEMORY_BYTES
            if candidate.name.endswith("-shm")
            else MAX_DATABASE_BYTES
        )
        validate_database_file(candidate, maximum_bytes=maximum)
        descriptor = os.open(candidate, _read_flags())
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    fsync_directory(path.parent)


def fsync_directory(path: Path) -> None:
    validate_protected_directory(path)
    descriptor = _open_directory(path)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def remove_created_database(path: Path, identity: PathIdentity) -> None:
    """Remove only the incomplete main file created with the supplied identity."""
    remove_created_artifacts(path, identity, {})


def remove_created_artifacts(
    path: Path,
    identity: PathIdentity,
    sidecars: dict[str, PathIdentity],
) -> None:
    """Remove only exact artifacts whose identities this creation captured."""
    for suffix in ("-wal", "-shm"):
        sidecar_identity = sidecars.get(suffix)
        if sidecar_identity is not None:
            _unlink_created_file(
                path.with_name(f"{path.name}{suffix}"), sidecar_identity
            )
    _unlink_created_file(path, identity)
    fsync_directory(path.parent)


def _unlink_created_file(path: Path, identity: PathIdentity) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        return
    current = _identity(metadata)
    if not stat.S_ISREG(metadata.st_mode) or not _same_managed_identity(
        current, identity
    ):
        return
    try:
        path.unlink()
    except OSError:
        return


def _directory_components(path: Path) -> tuple[Path, tuple[str, ...]]:
    absolute = path.absolute()
    if not absolute.anchor:
        raise FilesystemBoundaryError(FilesystemRejection.INVALID_PATH)
    components = absolute.parts[1:]
    if any(part in {"", ".", ".."} for part in components):
        raise FilesystemBoundaryError(FilesystemRejection.INVALID_PATH)
    return absolute, components


def _walk_directory_chain(
    absolute: Path,
    components: tuple[str, ...],
    *,
    expected: tuple[_DirectorySnapshot, ...] | None = None,
) -> _DirectoryWalk:
    _require_descriptor_traversal_support()
    root = Path(absolute.anchor)
    root_is_final = not components
    try:
        root_before = root.lstat()
    except OSError as error:
        raise FilesystemBoundaryError(FilesystemRejection.MISSING) from error
    _validate_directory_component(root_before, final=root_is_final)
    try:
        descriptor = _open_directory(root)
    except OSError as error:
        raise FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED) from error
    snapshots: list[_DirectorySnapshot] = []
    final_metadata = root_before
    try:
        root_opened = os.fstat(descriptor)
        _validate_directory_component(root_opened, final=root_is_final)
        _require_directory_unchanged(root_before, root_opened, final=root_is_final)
        try:
            root_after = root.lstat()
        except OSError as error:
            raise FilesystemBoundaryError(
                FilesystemRejection.IDENTITY_CHANGED
            ) from error
        _validate_directory_component(root_after, final=root_is_final)
        _require_directory_unchanged(root_before, root_after, final=root_is_final)
        snapshot = _directory_snapshot(root_after)
        _require_expected_directory(snapshot, expected, 0)
        snapshots.append(snapshot)
        final_metadata = root_after

        for index, component in enumerate(components, start=1):
            final = index == len(components)
            before = _stat_directory_entry(descriptor, component, initial=True)
            _validate_directory_component(before, final=final)
            child_descriptor = _open_directory_at(descriptor, component)
            try:
                opened = os.fstat(child_descriptor)
                _validate_directory_component(opened, final=final)
                _require_directory_unchanged(before, opened, final=final)
                after = _stat_directory_entry(descriptor, component, initial=False)
                _validate_directory_component(after, final=final)
                _require_directory_unchanged(before, after, final=final)
                snapshot = _directory_snapshot(after)
                _require_expected_directory(snapshot, expected, index)
                snapshots.append(snapshot)
                final_metadata = after
            except Exception:
                os.close(child_descriptor)
                raise
            parent_descriptor = descriptor
            descriptor = child_descriptor
            os.close(parent_descriptor)
    finally:
        os.close(descriptor)

    if expected is not None and len(expected) != len(snapshots):
        raise FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED)
    return _DirectoryWalk(tuple(snapshots), _identity(final_metadata))


def _stat_directory_entry(
    parent_descriptor: int,
    component: str,
    *,
    initial: bool,
) -> os.stat_result:
    try:
        return os.stat(
            component,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError as error:
        rejection = (
            FilesystemRejection.MISSING
            if initial
            else FilesystemRejection.IDENTITY_CHANGED
        )
        raise FilesystemBoundaryError(rejection) from error
    except OSError as error:
        rejection = (
            FilesystemRejection.INVALID_PATH
            if initial
            else FilesystemRejection.IDENTITY_CHANGED
        )
        raise FilesystemBoundaryError(rejection) from error


def _require_descriptor_traversal_support() -> None:
    if not _DESCRIPTOR_TRAVERSAL_SUPPORTED:
        raise FilesystemBoundaryError(FilesystemRejection.INVALID_PATH)


def _validate_directory_component(
    metadata: os.stat_result,
    *,
    final: bool,
) -> None:
    if stat.S_ISLNK(metadata.st_mode):
        raise FilesystemBoundaryError(FilesystemRejection.SYMLINK)
    if not stat.S_ISDIR(metadata.st_mode):
        raise FilesystemBoundaryError(FilesystemRejection.WRONG_TYPE)
    if final:
        _validate_directory_metadata(metadata)
        return
    if metadata.st_uid not in {0, os.geteuid()}:
        raise FilesystemBoundaryError(FilesystemRejection.WRONG_OWNER)
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise FilesystemBoundaryError(FilesystemRejection.WRONG_MODE)


def _require_directory_unchanged(
    left: os.stat_result,
    right: os.stat_result,
    *,
    final: bool,
) -> None:
    if _directory_snapshot(left) != _directory_snapshot(right):
        raise FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED)
    if final and _identity(left) != _identity(right):
        raise FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED)


def _require_expected_directory(
    snapshot: _DirectorySnapshot,
    expected: tuple[_DirectorySnapshot, ...] | None,
    index: int,
) -> None:
    if expected is not None and (index >= len(expected) or snapshot != expected[index]):
        raise FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED)


def _directory_snapshot(metadata: os.stat_result) -> _DirectorySnapshot:
    return _DirectorySnapshot(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        file_type=stat.S_IFMT(metadata.st_mode),
        mode=stat.S_IMODE(metadata.st_mode),
        owner=metadata.st_uid,
        links=metadata.st_nlink,
    )


def _validate_directory_metadata(metadata: os.stat_result) -> None:
    if stat.S_ISLNK(metadata.st_mode):
        raise FilesystemBoundaryError(FilesystemRejection.SYMLINK)
    if not stat.S_ISDIR(metadata.st_mode):
        raise FilesystemBoundaryError(FilesystemRejection.WRONG_TYPE)
    if metadata.st_uid != os.geteuid():
        raise FilesystemBoundaryError(FilesystemRejection.WRONG_OWNER)
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise FilesystemBoundaryError(FilesystemRejection.WRONG_MODE)


def _validate_file_metadata(metadata: os.stat_result, *, maximum_bytes: int) -> None:
    if stat.S_ISLNK(metadata.st_mode):
        raise FilesystemBoundaryError(FilesystemRejection.SYMLINK)
    if not stat.S_ISREG(metadata.st_mode):
        raise FilesystemBoundaryError(FilesystemRejection.WRONG_TYPE)
    if metadata.st_uid != os.geteuid():
        raise FilesystemBoundaryError(FilesystemRejection.WRONG_OWNER)
    if stat.S_IMODE(metadata.st_mode) != 0o600:
        raise FilesystemBoundaryError(FilesystemRejection.WRONG_MODE)
    if metadata.st_nlink != 1:
        raise FilesystemBoundaryError(FilesystemRejection.HARD_LINK)
    if metadata.st_size > maximum_bytes:
        raise FilesystemBoundaryError(FilesystemRejection.FILE_TOO_LARGE)


def _require_database_path(path: Path) -> None:
    _require_path(path)
    if not path.name or path.name in {".", ".."} or path.parent == path:
        raise FilesystemBoundaryError(FilesystemRejection.INVALID_PATH)


def _managed_paths(path: Path) -> tuple[Path, Path, Path]:
    return (
        path,
        path.with_name(f"{path.name}-wal"),
        path.with_name(f"{path.name}-shm"),
    )


def _require_path(path: Path) -> None:
    if not isinstance(path, Path):
        raise FilesystemBoundaryError(FilesystemRejection.INVALID_PATH)


def _same_managed_identity(left: PathIdentity, right: PathIdentity) -> bool:
    return (
        left.device,
        left.inode,
        left.mode,
        left.owner,
        left.links,
    ) == (
        right.device,
        right.inode,
        right.mode,
        right.owner,
        right.links,
    )


def _require_same_object(left: os.stat_result, right: os.stat_result) -> None:
    if (left.st_dev, left.st_ino) != (right.st_dev, right.st_ino):
        raise FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED)


def _require_unchanged(left: os.stat_result, right: os.stat_result) -> None:
    _require_same_object(left, right)
    if _identity(left) != _identity(right):
        raise FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED)


def _identity(metadata: os.stat_result) -> PathIdentity:
    return PathIdentity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=stat.S_IMODE(metadata.st_mode),
        owner=metadata.st_uid,
        links=metadata.st_nlink,
        size=metadata.st_size,
    )


def _open_directory(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags)


def _open_directory_at(parent_descriptor: int, component: str) -> int:
    try:
        return os.open(
            component,
            _directory_flags(),
            dir_fd=parent_descriptor,
        )
    except OSError as error:
        raise FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED) from error


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _read_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def _create_flags() -> int:
    return (
        os.O_RDWR
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
