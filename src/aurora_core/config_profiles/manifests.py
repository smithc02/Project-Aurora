"""Bounded backup creation, strict manifests, integrity checks, and listings."""

from __future__ import annotations

import json
import os
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from aurora_core.config_profiles.filesystem import SafeFilesystem, SecureFileSnapshot
from aurora_core.config_profiles.identifiers import is_backup_id, require_backup_id
from aurora_core.config_profiles.models import (
    MAX_BACKUP_ENTRIES,
    MAX_MANIFEST_BYTES,
    MAX_YAML_BYTES,
    BackupManifest,
    BackupOperation,
    BackupRecord,
    ProfileOperationError,
    ProfileReason,
)


@dataclass(frozen=True, slots=True)
class BackupListing:
    records: tuple[BackupRecord, ...]
    skipped_entries: int
    entry_limit_reached: bool


@dataclass(frozen=True, slots=True)
class SelectedBackup:
    manifest: BackupManifest
    snapshot: SecureFileSnapshot


class BackupRepository:
    """Code-named exact backups inside one explicit protected directory."""

    def __init__(
        self,
        filesystem: SafeFilesystem,
        *,
        clock: Callable[[], datetime] | None = None,
        random_suffix: Callable[[int], str] = secrets.token_hex,
    ) -> None:
        self._filesystem = filesystem
        self._clock = clock or (lambda: datetime.now(UTC))
        self._random_suffix = random_suffix

    def ensure_capacity(self, directory: Path, maximum_backups: int) -> None:
        """Refuse before mutation when the bounded managed-ID count is full."""
        entries = self._filesystem.enumerate_directory(
            directory, limit=MAX_BACKUP_ENTRIES
        )
        if entries.limit_reached:
            raise ProfileOperationError(ProfileReason.DIRECTORY_LIMIT)
        identifiers = {
            identifier
            for name in entries.names
            if (identifier := _managed_identifier(name)) is not None
        }
        if len(identifiers) >= maximum_backups:
            raise ProfileOperationError(ProfileReason.BACKUP_CAPACITY)

    def create(
        self,
        directory: Path,
        source: SecureFileSnapshot,
        *,
        operation: BackupOperation,
        maximum_backups: int,
        target_profile_id: str | None,
        target_backup_id: str | None,
    ) -> BackupManifest:
        """Create exactly one durable YAML/manifest pair without overwriting."""
        self.ensure_capacity(directory, maximum_backups)
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ProfileOperationError(ProfileReason.BACKUP_WRITE_FAILED)
        now = now.astimezone(UTC)
        backup_id = self._available_identifier(directory, now)
        manifest = BackupManifest(
            backup_id=backup_id,
            created_at_utc=now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            source_sha256=source.sha256,
            source_byte_count=len(source.data),
            operation=operation,
            target_profile_id=target_profile_id,
            target_backup_id=target_backup_id,
        )
        manifest_bytes = serialize_manifest(manifest)
        try:
            self._filesystem.write_new_file(directory, f"{backup_id}.yaml", source.data)
            self._filesystem.write_new_file(
                directory, f"{backup_id}.json", manifest_bytes
            )
            self._filesystem.fsync_directory(directory, restrictive=True)
        except ProfileOperationError:
            try:
                self._filesystem.fsync_directory(directory, restrictive=True)
            except ProfileOperationError:
                pass
            raise
        return manifest

    def list(self, directory: Path) -> BackupListing:
        """Return newest-first valid manifests with bounded integrity status."""
        entries = self._filesystem.enumerate_directory(
            directory, limit=MAX_BACKUP_ENTRIES
        )
        grouped: dict[str, set[str]] = {}
        skipped = 0
        for name in entries.names:
            parsed = _managed_name(name)
            if parsed is None:
                if name != ".aurora-config.lock":
                    skipped += 1
                continue
            backup_id, extension = parsed
            grouped.setdefault(backup_id, set()).add(extension)
        records: list[BackupRecord] = []
        for backup_id in sorted(grouped, reverse=True):
            if grouped[backup_id] != {"yaml", "json"}:
                skipped += len(grouped[backup_id])
                continue
            try:
                selected = self._load_pair(
                    directory, backup_id, require_integrity=False
                )
            except ProfileOperationError:
                skipped += 2
                continue
            manifest = selected.manifest
            integrity = (
                manifest.source_sha256 == selected.snapshot.sha256
                and manifest.source_byte_count == len(selected.snapshot.data)
            )
            records.append(
                BackupRecord(
                    manifest.backup_id,
                    manifest.created_at_utc,
                    manifest.source_sha256,
                    manifest.source_byte_count,
                    manifest.operation,
                    manifest.target_profile_id,
                    manifest.target_backup_id,
                    integrity,
                )
            )
        return BackupListing(tuple(records), skipped, entries.limit_reached)

    def load(self, directory: Path, backup_id: str) -> SelectedBackup:
        """Load and authenticate one code-resolved selected backup pair."""
        return self._load_pair(
            directory, require_backup_id(backup_id), require_integrity=True
        )

    def _load_pair(
        self,
        directory: Path,
        backup_id: str,
        *,
        require_integrity: bool,
    ) -> SelectedBackup:
        try:
            manifest_snapshot = self._filesystem.read_secure_file(
                directory / f"{backup_id}.json", maximum_bytes=MAX_MANIFEST_BYTES
            )
            source_snapshot = self._filesystem.read_secure_file(
                directory / f"{backup_id}.yaml", maximum_bytes=MAX_YAML_BYTES
            )
            manifest = parse_manifest(manifest_snapshot.data)
        except (ValueError, ProfileOperationError) as error:
            raise ProfileOperationError(ProfileReason.BACKUP_INVALID) from error
        if manifest.backup_id != backup_id:
            raise ProfileOperationError(ProfileReason.BACKUP_INVALID)
        integrity = (
            manifest.source_sha256 == source_snapshot.sha256
            and manifest.source_byte_count == len(source_snapshot.data)
        )
        if require_integrity and not integrity:
            raise ProfileOperationError(ProfileReason.BACKUP_INVALID)
        return SelectedBackup(manifest, source_snapshot)

    def _available_identifier(self, directory: Path, now: datetime) -> str:
        timestamp = now.strftime("%Y%m%dT%H%M%S%fZ")
        for _ in range(8):
            suffix = self._random_suffix(6)
            candidate = f"{timestamp}-{suffix}"
            if not is_backup_id(candidate):
                raise ProfileOperationError(ProfileReason.BACKUP_WRITE_FAILED)
            if not _entry_exists(directory / f"{candidate}.yaml") and not _entry_exists(
                directory / f"{candidate}.json"
            ):
                return candidate
        raise ProfileOperationError(ProfileReason.BACKUP_WRITE_FAILED)


def serialize_manifest(manifest: BackupManifest) -> bytes:
    """Encode one strict compact UTF-8 manifest within the fixed bound."""
    data = json.dumps(
        manifest.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    if len(data) > MAX_MANIFEST_BYTES:
        raise ProfileOperationError(ProfileReason.BACKUP_WRITE_FAILED)
    return data


def parse_manifest(data: bytes) -> BackupManifest:
    """Decode one strict manifest without returning raw JSON errors."""
    if len(data) > MAX_MANIFEST_BYTES:
        raise ProfileOperationError(ProfileReason.BACKUP_INVALID)
    try:
        document = json.loads(
            data.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
        )
        return BackupManifest.model_validate(document)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
    ) as error:
        raise ProfileOperationError(ProfileReason.BACKUP_INVALID) from error


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate manifest key")
        result[key] = value
    return result


def _managed_name(name: str) -> tuple[str, str] | None:
    for extension in ("yaml", "json"):
        suffix = f".{extension}"
        if name.endswith(suffix):
            identifier = name[: -len(suffix)]
            if is_backup_id(identifier):
                return identifier, extension
    return None


def _managed_identifier(name: str) -> str | None:
    parsed = _managed_name(name)
    return None if parsed is None else parsed[0]


def _entry_exists(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as error:
        raise ProfileOperationError(ProfileReason.FILESYSTEM_BOUNDARY) from error
    return True
