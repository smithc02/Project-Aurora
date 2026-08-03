"""Typed, sanitized Milestone 17 profile results and backup manifests."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum, StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from aurora_core.config_profiles.identifiers import (
    require_backup_id,
    require_profile_id,
)

MAX_YAML_BYTES = 256 * 1024
MAX_MANIFEST_BYTES = 16 * 1024
MAX_PROFILE_ENTRIES = 256
MAX_BACKUP_ENTRIES = 512
MAX_PLAN_CHANGES = 256
DEFAULT_MAXIMUM_BACKUPS = 20
MINIMUM_BACKUPS = 1
MAXIMUM_BACKUPS = 100


class ProfileExitCode(IntEnum):
    """Stable CLI exit codes for profile operations."""

    SUCCESS = 0
    INVALID = 2
    BUSY = 3
    RESTORED = 4
    RESTORATION_FAILED = 5


class ProfileReason(StrEnum):
    """Fixed reason codes which never contain installation data."""

    LISTED = "profiles_listed"
    VALID = "profile_valid"
    PLANNED = "profile_plan_ready"
    APPLIED = "profile_applied"
    ROLLED_BACK = "backup_rolled_back"
    NO_CHANGE = "profile_byte_identical"
    INVALID_ARGUMENT = "invalid_argument"
    INVALID_PROFILE_ID = "invalid_profile_id"
    INVALID_BACKUP_ID = "invalid_backup_id"
    CONFIRMATION_MISMATCH = "confirmation_mismatch"
    FILESYSTEM_BOUNDARY = "filesystem_boundary_rejected"
    DIRECTORY_LIMIT = "directory_entry_limit_reached"
    FILE_TOO_LARGE = "managed_file_too_large"
    INVALID_YAML = "invalid_yaml"
    INVALID_CONFIGURATION = "invalid_configuration"
    PROFILE_MISMATCH = "profile_identifier_mismatch"
    PROFILE_CHANGED = "profile_changed_during_activation"
    ACTIVE_CHANGED = "active_configuration_changed"
    BACKUP_CAPACITY = "backup_capacity_reached"
    BACKUP_INVALID = "backup_invalid"
    BACKUP_WRITE_FAILED = "backup_write_failed"
    MUTATION_BUSY = "mutation_lock_busy"
    ACTIVATION_FAILED_RESTORED = "activation_failed_restored"
    AUTOMATIC_RESTORATION_FAILED = "automatic_restoration_failed"


class ChangeType(StrEnum):
    """Value-free configuration plan change types."""

    ADDED = "added"
    REMOVED = "removed"
    CHANGED = "changed"


class BackupOperation(StrEnum):
    """The only operations allowed in a managed backup manifest."""

    APPLY = "apply"
    ROLLBACK = "rollback"


class ProfileOperationError(Exception):
    """Internal fixed-reason failure without a raw exception payload."""

    def __init__(self, reason: ProfileReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class MutationLockBusy(ProfileOperationError):
    """Raised when another apply or rollback holds the shared lock."""

    def __init__(self) -> None:
        super().__init__(ProfileReason.MUTATION_BUSY)


class AtomicWriteFailure(ProfileOperationError):
    """An atomic write failed, possibly after publishing the destination."""

    def __init__(self, *, published: bool) -> None:
        super().__init__(ProfileReason.FILESYSTEM_BOUNDARY)
        self.published = published


class BackupManifest(BaseModel):
    """Strict, bounded metadata for one exact configuration backup."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    backup_id: str
    created_at_utc: str
    source_sha256: str
    source_byte_count: int = Field(ge=0, le=MAX_YAML_BYTES, strict=True)
    operation: BackupOperation
    target_profile_id: str | None
    target_backup_id: str | None

    @field_validator("backup_id")
    @classmethod
    def backup_id_is_generated(cls, value: str) -> str:
        return require_backup_id(value)

    @field_validator("created_at_utc")
    @classmethod
    def timestamp_is_canonical_utc(cls, value: str) -> str:
        if (
            re.fullmatch(
                r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z",
                value,
            )
            is None
        ):
            raise ValueError("created_at_utc must be a canonical UTC timestamp")
        try:
            datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
        except ValueError as error:
            raise ValueError("created_at_utc must be a valid UTC timestamp") from error
        return value

    @field_validator("source_sha256")
    @classmethod
    def digest_is_sha256(cls, value: str) -> str:
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ValueError("source_sha256 must be lowercase hexadecimal")
        return value

    @field_validator("target_profile_id")
    @classmethod
    def target_profile_is_logical(cls, value: str | None) -> str | None:
        return None if value is None else require_profile_id(value)

    @field_validator("target_backup_id")
    @classmethod
    def target_backup_is_generated(cls, value: str | None) -> str | None:
        return None if value is None else require_backup_id(value)

    @model_validator(mode="after")
    def target_matches_operation(self) -> BackupManifest:
        if self.operation is BackupOperation.APPLY:
            if self.target_profile_id is None or self.target_backup_id is not None:
                raise ValueError("apply manifests require only target_profile_id")
        elif self.target_backup_id is None or self.target_profile_id is not None:
            raise ValueError("rollback manifests require only target_backup_id")
        compact_timestamp = datetime.strptime(
            self.created_at_utc, "%Y-%m-%dT%H:%M:%S.%fZ"
        ).strftime("%Y%m%dT%H%M%S%fZ")
        if not self.backup_id.startswith(f"{compact_timestamp}-"):
            raise ValueError("backup identifier and creation timestamp must match")
        return self


@dataclass(frozen=True, slots=True)
class PlanChange:
    path: str
    change_type: ChangeType


@dataclass(frozen=True, slots=True)
class ProfilePlan:
    profile_id: str
    active_sha256: str
    candidate_sha256: str
    byte_identical: bool
    changes: tuple[PlanChange, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class BackupRecord:
    backup_id: str
    created_at_utc: str
    source_sha256: str
    source_byte_count: int
    operation: BackupOperation
    target_profile_id: str | None
    target_backup_id: str | None
    integrity_valid: bool


@dataclass(frozen=True, slots=True)
class ProfileCommandResult:
    exit_code: ProfileExitCode
    reason_code: ProfileReason
    message: str
    profile_id: str | None = None
    profiles: tuple[str, ...] = ()
    skipped_entries: int = 0
    entry_limit_reached: bool = False
    plan: ProfilePlan | None = None
    backups: tuple[BackupRecord, ...] = ()
    backup_id: str | None = None
