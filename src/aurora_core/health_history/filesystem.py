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


def validate_protected_directory(path: Path) -> PathIdentity:
    """Require an existing owned mode-0700 directory with no symlink component."""
    _require_path(path)
    _validate_parent_components(path)
    try:
        before = path.lstat()
    except OSError as error:
        raise FilesystemBoundaryError(FilesystemRejection.MISSING) from error
    _validate_directory_metadata(before)
    descriptor = _open_directory(path)
    try:
        opened = os.fstat(descriptor)
        _validate_directory_metadata(opened)
        _require_same_object(before, opened)
    except OSError as error:
        raise FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED) from error
    finally:
        os.close(descriptor)
    try:
        after = path.lstat()
    except OSError as error:
        raise FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED) from error
    _validate_directory_metadata(after)
    _require_unchanged(before, after)
    return _identity(after)


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


def _validate_parent_components(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        try:
            metadata = current.lstat()
        except OSError as error:
            raise FilesystemBoundaryError(FilesystemRejection.MISSING) from error
        if stat.S_ISLNK(metadata.st_mode):
            raise FilesystemBoundaryError(FilesystemRejection.SYMLINK)
        if current != absolute and not stat.S_ISDIR(metadata.st_mode):
            raise FilesystemBoundaryError(FilesystemRejection.WRONG_TYPE)


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
