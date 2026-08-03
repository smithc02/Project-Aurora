"""Race-aware, no-follow filesystem boundary for local profile operations."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import secrets
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from aurora_core.config_profiles.models import (
    AtomicWriteFailure,
    MutationLockBusy,
    ProfileOperationError,
    ProfileReason,
)

LOCK_FILENAME = ".aurora-config.lock"
_FILE_MODE = 0o600
_DIRECTORY_MODE = 0o700
_WRITE_CHUNK = 64 * 1024
_COLLISION_ATTEMPTS = 8


@dataclass(frozen=True, slots=True)
class FileIdentity:
    device: int
    inode: int
    size: int
    modified_ns: int


@dataclass(frozen=True, slots=True)
class SecureFileSnapshot:
    data: bytes
    sha256: str
    identity: FileIdentity


@dataclass(frozen=True, slots=True)
class DirectoryEntries:
    names: tuple[str, ...]
    limit_reached: bool


class SafeFilesystem:
    """All low-level managed file operations for Milestone 17."""

    def __init__(
        self,
        *,
        effective_uid: int | None = None,
        temporary_token_factory: Callable[[int], str] = secrets.token_hex,
    ) -> None:
        self._effective_uid = os.geteuid() if effective_uid is None else effective_uid
        self._temporary_token_factory = temporary_token_factory

    def validate_directory(self, path: Path, *, restrictive: bool) -> None:
        """Require one owned, non-symlink directory with the requested policy."""
        try:
            self._reject_symlink_components(path)
            before = os.lstat(path)
            self._validate_directory_stat(before, restrictive=restrictive)
            descriptor = self._open_directory(path)
            try:
                opened = os.fstat(descriptor)
                self._validate_directory_stat(opened, restrictive=restrictive)
                self._require_same_object(before, opened)
            finally:
                os.close(descriptor)
            after = os.lstat(path)
            self._require_same_object(before, after)
            self._validate_directory_stat(after, restrictive=restrictive)
        except ProfileOperationError:
            raise
        except OSError as error:
            raise ProfileOperationError(ProfileReason.FILESYSTEM_BOUNDARY) from error

    def inspect_secure_file(self, path: Path, *, maximum_bytes: int) -> FileIdentity:
        """Validate metadata through a no-follow open without reading content."""
        try:
            self._reject_symlink_components(path)
            before = os.lstat(path)
            self._validate_file_stat(before, maximum_bytes=maximum_bytes)
            descriptor = os.open(path, self._read_flags())
            try:
                opened = os.fstat(descriptor)
                self._validate_file_stat(opened, maximum_bytes=maximum_bytes)
                self._require_same_object(before, opened)
            finally:
                os.close(descriptor)
            after = os.lstat(path)
            self._validate_file_stat(after, maximum_bytes=maximum_bytes)
            self._require_unchanged(before, after)
            return self._identity(after)
        except ProfileOperationError:
            raise
        except OSError as error:
            raise ProfileOperationError(ProfileReason.FILESYSTEM_BOUNDARY) from error

    def read_secure_file(self, path: Path, *, maximum_bytes: int) -> SecureFileSnapshot:
        """Read one bounded owned regular file without following links."""
        try:
            self._reject_symlink_components(path)
            before = os.lstat(path)
            self._validate_file_stat(before, maximum_bytes=maximum_bytes)
            descriptor = os.open(path, self._read_flags())
            try:
                opened = os.fstat(descriptor)
                self._validate_file_stat(opened, maximum_bytes=maximum_bytes)
                self._require_same_object(before, opened)
                data = self._read_all(descriptor, maximum_bytes)
                completed = os.fstat(descriptor)
                self._validate_file_stat(completed, maximum_bytes=maximum_bytes)
                self._require_unchanged(opened, completed)
                if len(data) != completed.st_size:
                    raise ProfileOperationError(ProfileReason.FILESYSTEM_BOUNDARY)
            finally:
                os.close(descriptor)
            after = os.lstat(path)
            self._validate_file_stat(after, maximum_bytes=maximum_bytes)
            self._require_unchanged(before, after)
        except ProfileOperationError:
            raise
        except OSError as error:
            raise ProfileOperationError(ProfileReason.FILESYSTEM_BOUNDARY) from error
        return SecureFileSnapshot(
            data=data,
            sha256=hashlib.sha256(data).hexdigest(),
            identity=self._identity(after),
        )

    def enumerate_directory(self, path: Path, *, limit: int) -> DirectoryEntries:
        """Inspect at most ``limit`` entry names from one restrictive directory."""
        self.validate_directory(path, restrictive=True)
        descriptor = self._open_directory(path)
        names: list[str] = []
        limit_reached = False
        try:
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    if len(names) >= limit:
                        limit_reached = True
                        break
                    names.append(entry.name)
            opened = os.fstat(descriptor)
            self._validate_directory_stat(opened, restrictive=True)
        except OSError as error:
            raise ProfileOperationError(ProfileReason.FILESYSTEM_BOUNDARY) from error
        finally:
            os.close(descriptor)
        return DirectoryEntries(tuple(sorted(names)), limit_reached)

    def write_new_file(self, directory: Path, name: str, data: bytes) -> None:
        """Exclusively create, fsync, and verify one code-named managed file."""
        self.validate_directory(directory, restrictive=True)
        self._require_simple_name(name)
        descriptor = self._open_directory(directory)
        file_descriptor: int | None = None
        created = False
        try:
            file_descriptor = os.open(
                name,
                self._write_new_flags(),
                _FILE_MODE,
                dir_fd=descriptor,
            )
            created = True
            os.fchmod(file_descriptor, _FILE_MODE)
            self._write_all(file_descriptor, data)
            os.fsync(file_descriptor)
            completed = os.fstat(file_descriptor)
            self._validate_file_stat(completed, maximum_bytes=len(data))
            if completed.st_size != len(data):
                raise OSError(errno.EIO, "managed file byte count mismatch")
        except (OSError, ProfileOperationError) as error:
            if file_descriptor is not None:
                os.close(file_descriptor)
                file_descriptor = None
            if created:
                try:
                    os.unlink(name, dir_fd=descriptor)
                except OSError:
                    pass
            if isinstance(error, ProfileOperationError):
                raise
            raise ProfileOperationError(ProfileReason.BACKUP_WRITE_FAILED) from error
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
            os.close(descriptor)

    def fsync_directory(self, path: Path, *, restrictive: bool) -> None:
        """Fsync a validated directory descriptor."""
        self.validate_directory(path, restrictive=restrictive)
        descriptor = self._open_directory(path)
        try:
            os.fsync(descriptor)
        except OSError as error:
            raise ProfileOperationError(ProfileReason.FILESYSTEM_BOUNDARY) from error
        finally:
            os.close(descriptor)

    def atomic_replace(
        self,
        destination: Path,
        data: bytes,
        *,
        expected_identity: FileIdentity | None,
    ) -> None:
        """Durably publish bytes with same-directory exclusive temporary storage."""
        parent = destination.parent
        self.validate_directory(parent, restrictive=False)
        self._require_simple_name(destination.name)
        directory_descriptor = self._open_directory(parent)
        temporary_name: str | None = None
        temporary_descriptor: int | None = None
        published = False
        try:
            if expected_identity is not None:
                current = os.stat(
                    destination.name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                self._validate_file_stat(
                    current, maximum_bytes=max(len(data), current.st_size)
                )
                if self._identity(current) != expected_identity:
                    raise ProfileOperationError(ProfileReason.ACTIVE_CHANGED)
            for _ in range(_COLLISION_ATTEMPTS):
                token = self._temporary_token_factory(12)
                if re_full_hex(token, 24):
                    candidate = f".aurora-config.tmp-{token}"
                else:
                    raise ProfileOperationError(ProfileReason.FILESYSTEM_BOUNDARY)
                try:
                    temporary_descriptor = os.open(
                        candidate,
                        self._write_new_flags(),
                        _FILE_MODE,
                        dir_fd=directory_descriptor,
                    )
                except FileExistsError:
                    continue
                temporary_name = candidate
                break
            if temporary_descriptor is None or temporary_name is None:
                raise ProfileOperationError(ProfileReason.FILESYSTEM_BOUNDARY)
            os.fchmod(temporary_descriptor, _FILE_MODE)
            self._write_all(temporary_descriptor, data)
            os.fsync(temporary_descriptor)
            completed = os.fstat(temporary_descriptor)
            self._validate_file_stat(completed, maximum_bytes=len(data))
            if completed.st_size != len(data):
                raise OSError(errno.EIO, "temporary file byte count mismatch")
            os.close(temporary_descriptor)
            temporary_descriptor = None
            if expected_identity is not None:
                current = os.stat(
                    destination.name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                if self._identity(current) != expected_identity:
                    raise ProfileOperationError(ProfileReason.ACTIVE_CHANGED)
            os.replace(
                temporary_name,
                destination.name,
                src_dir_fd=directory_descriptor,
                dst_dir_fd=directory_descriptor,
            )
            published = True
            temporary_name = None
            os.fsync(directory_descriptor)
        except ProfileOperationError:
            raise
        except OSError as error:
            raise AtomicWriteFailure(published=published) from error
        finally:
            if temporary_descriptor is not None:
                os.close(temporary_descriptor)
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name, dir_fd=directory_descriptor)
                except OSError:
                    pass
            os.close(directory_descriptor)

    @contextmanager
    def mutation_lock(self, backups_directory: Path) -> Iterator[None]:
        """Acquire the shared nonblocking process-independent mutation lock."""
        self.validate_directory(backups_directory, restrictive=True)
        directory_descriptor = self._open_directory(backups_directory)
        lock_descriptor: int | None = None
        try:
            try:
                before = os.stat(
                    LOCK_FILENAME,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                try:
                    lock_descriptor = os.open(
                        LOCK_FILENAME,
                        self._read_write_create_exclusive_flags(),
                        _FILE_MODE,
                        dir_fd=directory_descriptor,
                    )
                except FileExistsError:
                    before = os.stat(
                        LOCK_FILENAME,
                        dir_fd=directory_descriptor,
                        follow_symlinks=False,
                    )
                    self._validate_file_stat(before, maximum_bytes=0)
                    lock_descriptor = os.open(
                        LOCK_FILENAME,
                        self._read_write_flags(),
                        dir_fd=directory_descriptor,
                    )
                else:
                    os.fchmod(lock_descriptor, _FILE_MODE)
                    before = os.fstat(lock_descriptor)
            else:
                self._validate_file_stat(before, maximum_bytes=0)
                lock_descriptor = os.open(
                    LOCK_FILENAME,
                    self._read_write_flags(),
                    dir_fd=directory_descriptor,
                )
            lock_stat = os.fstat(lock_descriptor)
            self._validate_file_stat(lock_stat, maximum_bytes=0)
            self._require_same_object(before, lock_stat)
            entry_stat = os.stat(
                LOCK_FILENAME,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            self._require_same_object(lock_stat, entry_stat)
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise MutationLockBusy() from error
            yield
        except ProfileOperationError:
            raise
        except OSError as error:
            raise ProfileOperationError(ProfileReason.FILESYSTEM_BOUNDARY) from error
        finally:
            if lock_descriptor is not None:
                try:
                    fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(lock_descriptor)
            os.close(directory_descriptor)

    def _validate_directory_stat(
        self, metadata: os.stat_result, *, restrictive: bool
    ) -> None:
        if not stat.S_ISDIR(metadata.st_mode):
            raise ProfileOperationError(ProfileReason.FILESYSTEM_BOUNDARY)
        mode = stat.S_IMODE(metadata.st_mode)
        if restrictive and (
            metadata.st_uid != self._effective_uid or mode != _DIRECTORY_MODE
        ):
            raise ProfileOperationError(ProfileReason.FILESYSTEM_BOUNDARY)
        if not restrictive and mode & 0o022:
            raise ProfileOperationError(ProfileReason.FILESYSTEM_BOUNDARY)

    def _validate_file_stat(
        self, metadata: os.stat_result, *, maximum_bytes: int
    ) -> None:
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != self._effective_uid
            or stat.S_IMODE(metadata.st_mode) != _FILE_MODE
        ):
            raise ProfileOperationError(ProfileReason.FILESYSTEM_BOUNDARY)
        if metadata.st_size > maximum_bytes:
            raise ProfileOperationError(ProfileReason.FILE_TOO_LARGE)

    @staticmethod
    def _require_same_object(left: os.stat_result, right: os.stat_result) -> None:
        if (left.st_dev, left.st_ino) != (right.st_dev, right.st_ino):
            raise ProfileOperationError(ProfileReason.FILESYSTEM_BOUNDARY)

    @classmethod
    def _require_unchanged(cls, left: os.stat_result, right: os.stat_result) -> None:
        cls._require_same_object(left, right)
        if cls._identity(left) != cls._identity(right):
            raise ProfileOperationError(ProfileReason.FILESYSTEM_BOUNDARY)

    @staticmethod
    def _identity(metadata: os.stat_result) -> FileIdentity:
        return FileIdentity(
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
        )

    @staticmethod
    def _require_simple_name(name: str) -> None:
        if not name or name in {".", ".."} or Path(name).name != name:
            raise ProfileOperationError(ProfileReason.FILESYSTEM_BOUNDARY)

    @staticmethod
    def _open_directory(path: Path) -> int:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        return os.open(path, flags)

    @staticmethod
    def _read_flags() -> int:
        return os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)

    @staticmethod
    def _write_new_flags() -> int:
        return (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )

    @staticmethod
    def _read_write_flags() -> int:
        return os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)

    @classmethod
    def _read_write_create_exclusive_flags(cls) -> int:
        return cls._read_write_flags() | os.O_CREAT | os.O_EXCL

    @staticmethod
    def _reject_symlink_components(path: Path) -> None:
        absolute = path if path.is_absolute() else Path.cwd() / path
        current = Path(absolute.anchor)
        try:
            for part in absolute.parts[1:]:
                current /= part
                if stat.S_ISLNK(os.lstat(current).st_mode):
                    raise ProfileOperationError(ProfileReason.FILESYSTEM_BOUNDARY)
        except FileNotFoundError as error:
            raise ProfileOperationError(ProfileReason.FILESYSTEM_BOUNDARY) from error

    @staticmethod
    def _read_all(descriptor: int, maximum_bytes: int) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(_WRITE_CHUNK, maximum_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum_bytes:
                raise ProfileOperationError(ProfileReason.FILE_TOO_LARGE)
        return b"".join(chunks)

    @staticmethod
    def _write_all(descriptor: int, data: bytes) -> None:
        view = memoryview(data)
        written = 0
        while written < len(view):
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise OSError(errno.EIO, "managed file write made no progress")
            written += count


def re_full_hex(value: object, length: int) -> bool:
    """Return whether an injected random token has the expected safe grammar."""
    return (
        isinstance(value, str)
        and len(value) == length
        and all(character in "0123456789abcdef" for character in value)
    )
