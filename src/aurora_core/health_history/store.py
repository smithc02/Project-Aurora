"""Explicit create/open lifecycle for production-format history databases."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from types import TracebackType

from aurora_core.health_history.filesystem import (
    FilesystemBoundaryError,
    FilesystemRejection,
    PathIdentity,
    create_database_file,
    fsync_database_files,
    remove_created_artifacts,
    require_stable_sidecars,
    validate_database_file,
    validate_sidecars,
)
from aurora_core.health_history.ingestion import (
    IngestionError,
    IngestionRejection,
    IngestionResult,
    ingest_projection,
)
from aurora_core.health_history.models import (
    BUSY_TIMEOUT_MILLISECONDS,
    PAGE_SIZE_BYTES,
)
from aurora_core.health_history.projection import HealthProjection
from aurora_core.health_history.schema import (
    SchemaVerificationError,
    create_schema_v1,
    verify_schema_v1,
)


class StoreError(Exception):
    """Sanitized storage lifecycle failure."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class HealthHistoryStore:
    """A verified SQLite connection with no general SQL execution surface."""

    def __init__(
        self,
        *,
        path: Path,
        connection: sqlite3.Connection,
        identity: PathIdentity,
        sidecars: dict[str, PathIdentity],
        monotonic: Callable[[], float],
    ) -> None:
        self._path = path
        self._connection = connection
        self._identity = identity
        self._sidecars = sidecars
        self._monotonic = monotonic
        self._closed = False

    @classmethod
    def create(
        cls,
        path: Path,
        *,
        created_at_utc_us: int,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> HealthHistoryStore:
        """Exclusively create and verify one new production-format database."""
        identity: PathIdentity | None = None
        created_sidecars: dict[str, PathIdentity] = {}
        connection: sqlite3.Connection | None = None
        try:
            identity = create_database_file(path)
            connection = _connect_existing(path)
            validate_database_file(path, expected=identity)
            created_sidecars = _advance_sidecar_snapshot(path, created_sidecars)
            _configure_new_database(connection)
            created_sidecars = _advance_sidecar_snapshot(path, created_sidecars)
            create_schema_v1(connection, applied_at_utc_us=created_at_utc_us)
            created_sidecars = _advance_sidecar_snapshot(path, created_sidecars)
            _verify_connection_settings(connection)
            created_sidecars = _advance_sidecar_snapshot(path, created_sidecars)
            verify_schema_v1(connection, monotonic=monotonic)
            created_sidecars = _advance_sidecar_snapshot(path, created_sidecars)
            identity = validate_database_file(path, expected=identity)
            fsync_database_files(path)
            identity = validate_database_file(path, expected=identity)
            created_sidecars = _advance_sidecar_snapshot(path, created_sidecars)
            created_sidecars = _advance_sidecar_snapshot(path, created_sidecars)
            return cls(
                path=path,
                connection=connection,
                identity=identity,
                sidecars=created_sidecars,
                monotonic=monotonic,
            )
        except (
            FilesystemBoundaryError,
            OSError,
            SchemaVerificationError,
            sqlite3.Error,
        ) as error:
            if connection is not None:
                connection.close()
            if identity is not None:
                try:
                    remove_created_artifacts(path, identity, created_sidecars)
                except (FilesystemBoundaryError, OSError):
                    pass
            reason = (
                "already_exists"
                if isinstance(error, FilesystemBoundaryError)
                and error.reason is FilesystemRejection.ALREADY_EXISTS
                else "creation_failed"
            )
            raise StoreError(reason) from error

    @classmethod
    def open_existing(
        cls,
        path: Path,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> HealthHistoryStore:
        """Open one existing verified production database without creating it."""
        connection: sqlite3.Connection | None = None
        try:
            identity = validate_database_file(path)
            sidecars = validate_sidecars(path)
            connection = _connect_existing(path)
            validate_database_file(path, expected=identity)
            sidecars = _advance_sidecar_snapshot(path, sidecars)
            _configure_existing_database(connection)
            sidecars = _advance_sidecar_snapshot(path, sidecars)
            _verify_connection_settings(connection)
            sidecars = _advance_sidecar_snapshot(path, sidecars)
            verify_schema_v1(connection, monotonic=monotonic)
            sidecars = _advance_sidecar_snapshot(path, sidecars)
            identity = validate_database_file(path, expected=identity)
            sidecars = _advance_sidecar_snapshot(path, sidecars)
            sidecars = _advance_sidecar_snapshot(path, sidecars)
            return cls(
                path=path,
                connection=connection,
                identity=identity,
                sidecars=sidecars,
                monotonic=monotonic,
            )
        except (
            FilesystemBoundaryError,
            OSError,
            SchemaVerificationError,
            sqlite3.Error,
        ) as error:
            if connection is not None:
                connection.close()
            raise StoreError("open_failed") from error

    @property
    def closed(self) -> bool:
        return self._closed

    def verify(self) -> None:
        """Repeat the bounded production identity and schema checks."""
        self._require_open()
        try:
            validate_database_file(self._path, expected=self._identity)
            sidecars = _advance_sidecar_snapshot(self._path, self._sidecars)
            _verify_connection_settings(self._connection)
            sidecars = _advance_sidecar_snapshot(self._path, sidecars)
            verify_schema_v1(self._connection, monotonic=self._monotonic)
            sidecars = _advance_sidecar_snapshot(self._path, sidecars)
            validate_database_file(self._path, expected=self._identity)
            self._sidecars = _advance_sidecar_snapshot(self._path, sidecars)
        except (
            FilesystemBoundaryError,
            OSError,
            SchemaVerificationError,
            sqlite3.Error,
        ) as error:
            self.close()
            raise StoreError("verification_failed") from error

    def ingest(self, projection: HealthProjection) -> IngestionResult:
        """Atomically ingest one strict projection without retry or queuing."""
        self._require_open()
        try:
            validate_database_file(self._path, expected=self._identity)
            sidecars = _advance_sidecar_snapshot(self._path, self._sidecars)
            result = ingest_projection(self._connection, projection)
            validate_database_file(self._path, expected=self._identity)
            sidecars = _advance_sidecar_snapshot(self._path, sidecars)
            self._sidecars = _advance_sidecar_snapshot(self._path, sidecars)
            return result
        except IngestionError as error:
            if error.trust_lost:
                self.close()
            raise
        except (FilesystemBoundaryError, OSError):
            self.close()
            raise IngestionError(
                IngestionRejection.TRUST_FAILED, trust_lost=True
            ) from None

    def close(self) -> None:
        if self._closed:
            return
        self._connection.close()
        self._closed = True

    def __enter__(self) -> HealthHistoryStore:
        self._require_open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()

    def _require_open(self) -> None:
        if self._closed:
            raise StoreError("store_closed")


def _connect_existing(path: Path) -> sqlite3.Connection:
    uri = f"{path.absolute().as_uri()}?mode=rw"
    return sqlite3.connect(
        uri,
        uri=True,
        timeout=BUSY_TIMEOUT_MILLISECONDS / 1000,
        isolation_level=None,
    )


def _advance_sidecar_snapshot(
    path: Path, before: dict[str, PathIdentity]
) -> dict[str, PathIdentity]:
    after = validate_sidecars(path)
    require_stable_sidecars(before, after)
    return after


def _configure_new_database(connection: sqlite3.Connection) -> None:
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MILLISECONDS}")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA page_size = {PAGE_SIZE_BYTES}")
    mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()
    if mode is None or str(mode[0]).lower() != "wal":
        raise SchemaVerificationError("journal_mode_mismatch")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA wal_autocheckpoint = 0")


def _configure_existing_database(connection: sqlite3.Connection) -> None:
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MILLISECONDS}")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA wal_autocheckpoint = 0")


def _verify_connection_settings(connection: sqlite3.Connection) -> None:
    expected = {
        "busy_timeout": BUSY_TIMEOUT_MILLISECONDS,
        "foreign_keys": 1,
        "page_size": PAGE_SIZE_BYTES,
        "synchronous": 2,
        "wal_autocheckpoint": 0,
    }
    for name, value in expected.items():
        row = connection.execute(f"PRAGMA {name}").fetchone()
        if row is None or row[0] != value:
            raise SchemaVerificationError("pragma_mismatch")
    mode = connection.execute("PRAGMA journal_mode").fetchone()
    if mode is None or str(mode[0]).lower() != "wal":
        raise SchemaVerificationError("journal_mode_mismatch")
