"""Restrictive synthetic SQLite filesystem checks."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class FilesystemRejection(StrEnum):
    MISSING = "missing"
    SYMLINK = "symlink"
    WRONG_TYPE = "wrong_type"
    WRONG_OWNER = "wrong_owner"
    WRONG_MODE = "wrong_mode"
    HARD_LINK = "hard_link"
    IDENTITY_CHANGED = "identity_changed"


class FilesystemBoundaryError(Exception):
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


def validate_protected_directory(path: Path) -> PathIdentity:
    validate_no_symlink_components(path)
    try:
        metadata = path.lstat()
    except OSError as error:
        raise FilesystemBoundaryError(FilesystemRejection.MISSING) from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise FilesystemBoundaryError(FilesystemRejection.WRONG_TYPE)
    if metadata.st_uid != os.geteuid():
        raise FilesystemBoundaryError(FilesystemRejection.WRONG_OWNER)
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise FilesystemBoundaryError(FilesystemRejection.WRONG_MODE)
    return _identity(metadata)


def validate_no_symlink_components(path: Path) -> None:
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


def validate_regular_file(path: Path) -> PathIdentity:
    try:
        path_metadata = path.lstat()
    except OSError as error:
        raise FilesystemBoundaryError(FilesystemRejection.MISSING) from error
    if stat.S_ISLNK(path_metadata.st_mode):
        raise FilesystemBoundaryError(FilesystemRejection.SYMLINK)
    if not stat.S_ISREG(path_metadata.st_mode):
        raise FilesystemBoundaryError(FilesystemRejection.WRONG_TYPE)
    if path_metadata.st_uid != os.geteuid():
        raise FilesystemBoundaryError(FilesystemRejection.WRONG_OWNER)
    if stat.S_IMODE(path_metadata.st_mode) != 0o600:
        raise FilesystemBoundaryError(FilesystemRejection.WRONG_MODE)
    if path_metadata.st_nlink != 1:
        raise FilesystemBoundaryError(FilesystemRejection.HARD_LINK)
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        descriptor_metadata = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    path_identity = _identity(path_metadata)
    if path_identity != _identity(descriptor_metadata):
        raise FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED)
    return path_identity


def require_same_identity(path: Path, expected: PathIdentity) -> None:
    if validate_regular_file(path) != expected:
        raise FilesystemBoundaryError(FilesystemRejection.IDENTITY_CHANGED)


def create_secure_file(path: Path) -> PathIdentity:
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return validate_regular_file(path)


def _identity(metadata: os.stat_result) -> PathIdentity:
    return PathIdentity(
        device=metadata.st_dev,
        inode=metadata.st_ino,
        mode=metadata.st_mode,
        owner=metadata.st_uid,
        links=metadata.st_nlink,
    )
