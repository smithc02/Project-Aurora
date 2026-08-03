"""CLI-only orchestration for validated profiles, activation, and rollback."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from aurora_core.config_profiles.filesystem import (
    FileIdentity,
    SafeFilesystem,
    SecureFileSnapshot,
)
from aurora_core.config_profiles.identifiers import (
    is_profile_id,
    require_backup_id,
    require_profile_id,
)
from aurora_core.config_profiles.manifests import BackupRepository
from aurora_core.config_profiles.models import (
    DEFAULT_MAXIMUM_BACKUPS,
    MAX_MANIFEST_BYTES,
    MAX_PROFILE_ENTRIES,
    MAX_YAML_BYTES,
    MAXIMUM_BACKUPS,
    MINIMUM_BACKUPS,
    AtomicWriteFailure,
    BackupOperation,
    MutationLockBusy,
    ProfileCommandResult,
    ProfileExitCode,
    ProfileOperationError,
    ProfilePlan,
    ProfileReason,
)
from aurora_core.config_profiles.planner import plan_changes
from aurora_core.config_profiles.validation import (
    validate_effective_yaml,
    validate_raw_yaml,
)


class ConfigurationProfileService:
    """Perform bounded local YAML-layer profile operations."""

    def __init__(
        self,
        filesystem: SafeFilesystem | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        random_suffix: Callable[[int], str] | None = None,
        before_copy: Callable[[], None] | None = None,
        after_publish: Callable[[], None] | None = None,
    ) -> None:
        self._filesystem = filesystem or SafeFilesystem()
        if random_suffix is None:
            self._backups = BackupRepository(self._filesystem, clock=clock)
        else:
            self._backups = BackupRepository(
                self._filesystem, clock=clock, random_suffix=random_suffix
            )
        self._before_copy = before_copy
        self._after_publish = after_publish

    def list_profiles(self, profiles_directory: Path) -> ProfileCommandResult:
        """List secure logical profile IDs without reading profile content."""
        try:
            entries = self._filesystem.enumerate_directory(
                profiles_directory, limit=MAX_PROFILE_ENTRIES
            )
            profiles: list[str] = []
            skipped = 0
            for name in entries.names:
                if not name.endswith(".yaml"):
                    skipped += 1
                    continue
                profile_id = name[:-5]
                if not is_profile_id(profile_id):
                    skipped += 1
                    continue
                try:
                    self._filesystem.inspect_secure_file(
                        profiles_directory / name, maximum_bytes=MAX_YAML_BYTES
                    )
                except ProfileOperationError:
                    skipped += 1
                    continue
                profiles.append(profile_id)
            return ProfileCommandResult(
                ProfileExitCode.SUCCESS,
                ProfileReason.LISTED,
                "Configuration profiles listed.",
                profiles=tuple(sorted(profiles)),
                skipped_entries=skipped,
                entry_limit_reached=entries.limit_reached,
            )
        except ProfileOperationError as error:
            return _invalid(error.reason)
        except Exception:
            return _invalid(ProfileReason.FILESYSTEM_BOUNDARY)

    def validate_profile(
        self, profiles_directory: Path, profile_id: str
    ) -> ProfileCommandResult:
        """Perform raw and current-environment validation without active-file I/O."""
        try:
            profile_id = require_profile_id(profile_id)
            self._filesystem.validate_directory(profiles_directory, restrictive=True)
            path = _profile_path(profiles_directory, profile_id)
            snapshot = self._filesystem.read_secure_file(
                path, maximum_bytes=MAX_YAML_BYTES
            )
            document, _ = validate_raw_yaml(
                snapshot.data, expected_profile_id=profile_id
            )
            validate_effective_yaml(document)
            self._require_snapshot_unchanged(
                snapshot,
                self._filesystem.read_secure_file(path, maximum_bytes=MAX_YAML_BYTES),
                ProfileReason.PROFILE_CHANGED,
            )
            return ProfileCommandResult(
                ProfileExitCode.SUCCESS,
                ProfileReason.VALID,
                "Profile passed raw and effective validation.",
                profile_id=profile_id,
            )
        except ValueError:
            return _invalid(ProfileReason.INVALID_PROFILE_ID)
        except ProfileOperationError as error:
            return _invalid(error.reason)
        except Exception:
            return _invalid(ProfileReason.FILESYSTEM_BOUNDARY)

    def plan(
        self,
        config_path: Path,
        profiles_directory: Path,
        profile_id: str,
    ) -> ProfileCommandResult:
        """Compare validated raw mappings without printing old or new values."""
        try:
            profile_id = require_profile_id(profile_id)
            active, active_document = self._load_active(config_path)
            candidate_path = _profile_path(profiles_directory, profile_id)
            candidate, candidate_document = self._load_profile(
                profiles_directory, profile_id
            )
            changes, truncated = plan_changes(active_document, candidate_document)
            plan = ProfilePlan(
                profile_id,
                active.sha256,
                candidate.sha256,
                active.data == candidate.data,
                changes,
                truncated,
            )
            self._require_snapshot_unchanged(
                candidate,
                self._filesystem.read_secure_file(
                    candidate_path, maximum_bytes=MAX_YAML_BYTES
                ),
                ProfileReason.PROFILE_CHANGED,
            )
            return ProfileCommandResult(
                ProfileExitCode.SUCCESS,
                ProfileReason.PLANNED,
                "Sanitized profile plan is ready.",
                profile_id=profile_id,
                plan=plan,
            )
        except ValueError:
            return _invalid(ProfileReason.INVALID_PROFILE_ID)
        except ProfileOperationError as error:
            return _invalid(error.reason)
        except Exception:
            return _invalid(ProfileReason.FILESYSTEM_BOUNDARY)

    def apply(
        self,
        config_path: Path,
        profiles_directory: Path,
        backups_directory: Path,
        profile_id: str,
        confirmation: str,
        *,
        maximum_backups: int = DEFAULT_MAXIMUM_BACKUPS,
    ) -> ProfileCommandResult:
        """Back up and atomically activate one complete profile YAML file."""
        try:
            profile_id = require_profile_id(profile_id)
            _require_maximum_backups(maximum_backups)
            if confirmation != profile_id:
                return _invalid(ProfileReason.CONFIRMATION_MISMATCH)
            self._preflight_apply_paths(
                config_path, profiles_directory, backups_directory, profile_id
            )
            with self._filesystem.mutation_lock(backups_directory):
                active, _ = self._load_active(config_path)
                candidate_path = _profile_path(profiles_directory, profile_id)
                candidate, _ = self._load_profile(profiles_directory, profile_id)
                if active.data == candidate.data:
                    return ProfileCommandResult(
                        ProfileExitCode.SUCCESS,
                        ProfileReason.NO_CHANGE,
                        "Profile is already byte-identical; no backup was created.",
                        profile_id=profile_id,
                    )
                self._run_before_copy()
                current_active = self._filesystem.read_secure_file(
                    config_path, maximum_bytes=MAX_YAML_BYTES
                )
                current_candidate = self._filesystem.read_secure_file(
                    candidate_path, maximum_bytes=MAX_YAML_BYTES
                )
                self._require_snapshot_unchanged(
                    active,
                    current_active,
                    ProfileReason.ACTIVE_CHANGED,
                )
                self._require_snapshot_unchanged(
                    candidate,
                    current_candidate,
                    ProfileReason.PROFILE_CHANGED,
                )
                self._backups.create(
                    backups_directory,
                    active,
                    operation=BackupOperation.APPLY,
                    maximum_backups=maximum_backups,
                    target_profile_id=profile_id,
                    target_backup_id=None,
                )
                failure = self._publish_with_recovery(
                    config_path,
                    candidate.data,
                    previous=active,
                    expected_identity=current_active.identity,
                    expected_profile_id=profile_id,
                )
                if failure is not None:
                    return failure
                return ProfileCommandResult(
                    ProfileExitCode.SUCCESS,
                    ProfileReason.APPLIED,
                    (
                        "Profile activated; an external Aurora service restart "
                        "is required."
                    ),
                    profile_id=profile_id,
                )
        except ValueError:
            return _invalid(ProfileReason.INVALID_PROFILE_ID)
        except MutationLockBusy:
            return ProfileCommandResult(
                ProfileExitCode.BUSY,
                ProfileReason.MUTATION_BUSY,
                "Another configuration mutation is in progress.",
                profile_id=profile_id if is_profile_id(profile_id) else None,
            )
        except ProfileOperationError as error:
            return _invalid(error.reason)
        except Exception:
            return _invalid(ProfileReason.FILESYSTEM_BOUNDARY)

    def list_backups(self, backups_directory: Path) -> ProfileCommandResult:
        """List strict managed records and bounded integrity status."""
        try:
            self._filesystem.validate_directory(backups_directory, restrictive=True)
            listing = self._backups.list(backups_directory)
            return ProfileCommandResult(
                ProfileExitCode.SUCCESS,
                ProfileReason.LISTED,
                "Configuration backups listed.",
                backups=listing.records,
                skipped_entries=listing.skipped_entries,
                entry_limit_reached=listing.entry_limit_reached,
            )
        except ProfileOperationError as error:
            return _invalid(error.reason)
        except Exception:
            return _invalid(ProfileReason.FILESYSTEM_BOUNDARY)

    def rollback(
        self,
        config_path: Path,
        backups_directory: Path,
        backup_id: str,
        confirmation: str,
        *,
        maximum_backups: int = DEFAULT_MAXIMUM_BACKUPS,
    ) -> ProfileCommandResult:
        """Create a reversible backup, then atomically install a selected backup."""
        try:
            backup_id = require_backup_id(backup_id)
            _require_maximum_backups(maximum_backups)
            if confirmation != backup_id:
                return _invalid(ProfileReason.CONFIRMATION_MISMATCH)
            self._preflight_rollback_paths(config_path, backups_directory, backup_id)
            with self._filesystem.mutation_lock(backups_directory):
                active, _ = self._load_active(config_path)
                selected = self._backups.load(backups_directory, backup_id)
                selected_document, _ = validate_raw_yaml(selected.snapshot.data)
                validate_effective_yaml(selected_document)
                self._run_before_copy()
                current_active = self._filesystem.read_secure_file(
                    config_path, maximum_bytes=MAX_YAML_BYTES
                )
                current_selected = self._backups.load(backups_directory, backup_id)
                self._require_snapshot_unchanged(
                    active,
                    current_active,
                    ProfileReason.ACTIVE_CHANGED,
                )
                self._require_snapshot_unchanged(
                    selected.snapshot,
                    current_selected.snapshot,
                    ProfileReason.BACKUP_INVALID,
                )
                self._backups.create(
                    backups_directory,
                    active,
                    operation=BackupOperation.ROLLBACK,
                    maximum_backups=maximum_backups,
                    target_profile_id=None,
                    target_backup_id=backup_id,
                )
                failure = self._publish_with_recovery(
                    config_path,
                    selected.snapshot.data,
                    previous=active,
                    expected_identity=current_active.identity,
                    expected_profile_id=None,
                )
                if failure is not None:
                    return failure
                return ProfileCommandResult(
                    ProfileExitCode.SUCCESS,
                    ProfileReason.ROLLED_BACK,
                    "Backup activated; an external Aurora service restart is required.",
                    backup_id=backup_id,
                )
        except ValueError:
            return _invalid(ProfileReason.INVALID_BACKUP_ID)
        except MutationLockBusy:
            return ProfileCommandResult(
                ProfileExitCode.BUSY,
                ProfileReason.MUTATION_BUSY,
                "Another configuration mutation is in progress.",
                backup_id=backup_id,
            )
        except ProfileOperationError as error:
            return _invalid(error.reason)
        except Exception:
            return _invalid(ProfileReason.FILESYSTEM_BOUNDARY)

    def _load_active(
        self, config_path: Path
    ) -> tuple[SecureFileSnapshot, dict[str, object]]:
        self._filesystem.validate_directory(config_path.parent, restrictive=False)
        snapshot = self._filesystem.read_secure_file(
            config_path, maximum_bytes=MAX_YAML_BYTES
        )
        document, _ = validate_raw_yaml(snapshot.data)
        validate_effective_yaml(document)
        return snapshot, document

    def _load_profile(
        self, profiles_directory: Path, profile_id: str
    ) -> tuple[SecureFileSnapshot, dict[str, object]]:
        self._filesystem.validate_directory(profiles_directory, restrictive=True)
        path = _profile_path(profiles_directory, profile_id)
        snapshot = self._filesystem.read_secure_file(path, maximum_bytes=MAX_YAML_BYTES)
        document, _ = validate_raw_yaml(snapshot.data, expected_profile_id=profile_id)
        validate_effective_yaml(document)
        return snapshot, document

    def _preflight_apply_paths(
        self,
        config_path: Path,
        profiles_directory: Path,
        backups_directory: Path,
        profile_id: str,
    ) -> None:
        self._filesystem.validate_directory(config_path.parent, restrictive=False)
        self._filesystem.validate_directory(profiles_directory, restrictive=True)
        self._filesystem.validate_directory(backups_directory, restrictive=True)
        self._filesystem.inspect_secure_file(config_path, maximum_bytes=MAX_YAML_BYTES)
        self._filesystem.inspect_secure_file(
            _profile_path(profiles_directory, profile_id), maximum_bytes=MAX_YAML_BYTES
        )

    def _preflight_rollback_paths(
        self, config_path: Path, backups_directory: Path, backup_id: str
    ) -> None:
        self._filesystem.validate_directory(config_path.parent, restrictive=False)
        self._filesystem.validate_directory(backups_directory, restrictive=True)
        self._filesystem.inspect_secure_file(config_path, maximum_bytes=MAX_YAML_BYTES)
        self._filesystem.inspect_secure_file(
            backups_directory / f"{backup_id}.yaml", maximum_bytes=MAX_YAML_BYTES
        )
        self._filesystem.inspect_secure_file(
            backups_directory / f"{backup_id}.json",
            maximum_bytes=MAX_MANIFEST_BYTES,
        )

    def _publish_with_recovery(
        self,
        config_path: Path,
        replacement: bytes,
        *,
        previous: SecureFileSnapshot,
        expected_identity: FileIdentity,
        expected_profile_id: str | None,
    ) -> ProfileCommandResult | None:
        published = False
        try:
            self._filesystem.atomic_replace(
                config_path,
                replacement,
                expected_identity=expected_identity,
            )
            published = True
            if self._after_publish is not None:
                self._after_publish()
            self._verify_published(
                config_path, replacement, expected_profile_id=expected_profile_id
            )
            return None
        except AtomicWriteFailure as error:
            published = error.published
            if not published:
                return _invalid(ProfileReason.FILESYSTEM_BOUNDARY)
        except ProfileOperationError as error:
            if not published:
                return _invalid(error.reason)
        except OSError:
            if not published:
                return _invalid(ProfileReason.FILESYSTEM_BOUNDARY)
        except Exception:
            if not published:
                return _invalid(ProfileReason.FILESYSTEM_BOUNDARY)
        return self._recover_previous(config_path, previous)

    def _recover_previous(
        self, config_path: Path, previous: SecureFileSnapshot
    ) -> ProfileCommandResult:
        try:
            self._filesystem.atomic_replace(
                config_path,
                previous.data,
                expected_identity=None,
            )
            self._verify_published(config_path, previous.data, expected_profile_id=None)
        except Exception:
            return ProfileCommandResult(
                ProfileExitCode.RESTORATION_FAILED,
                ProfileReason.AUTOMATIC_RESTORATION_FAILED,
                (
                    "Automatic restoration failed; active configuration validity "
                    "is unknown."
                ),
            )
        return ProfileCommandResult(
            ProfileExitCode.RESTORED,
            ProfileReason.ACTIVATION_FAILED_RESTORED,
            "Activation failed and the previous configuration was restored.",
        )

    def _verify_published(
        self,
        config_path: Path,
        expected: bytes,
        *,
        expected_profile_id: str | None,
    ) -> None:
        snapshot = self._filesystem.read_secure_file(
            config_path, maximum_bytes=MAX_YAML_BYTES
        )
        if (
            snapshot.data != expected
            or snapshot.sha256 != hashlib.sha256(expected).hexdigest()
        ):
            raise ProfileOperationError(ProfileReason.FILESYSTEM_BOUNDARY)
        document, _ = validate_raw_yaml(
            snapshot.data, expected_profile_id=expected_profile_id
        )
        validate_effective_yaml(document)

    def _run_before_copy(self) -> None:
        if self._before_copy is not None:
            self._before_copy()

    @staticmethod
    def _require_snapshot_unchanged(
        expected: SecureFileSnapshot,
        actual: SecureFileSnapshot,
        reason: ProfileReason,
    ) -> None:
        if expected.identity != actual.identity or expected.data != actual.data:
            raise ProfileOperationError(reason)


def _profile_path(directory: Path, profile_id: str) -> Path:
    return directory / f"{require_profile_id(profile_id)}.yaml"


def _require_maximum_backups(value: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < MINIMUM_BACKUPS
        or value > MAXIMUM_BACKUPS
    ):
        raise ProfileOperationError(ProfileReason.INVALID_ARGUMENT)


def _invalid(reason: ProfileReason) -> ProfileCommandResult:
    messages = {
        ProfileReason.INVALID_ARGUMENT: "Profile command arguments are invalid.",
        ProfileReason.INVALID_PROFILE_ID: "Profile identifier is invalid.",
        ProfileReason.INVALID_BACKUP_ID: "Backup identifier is invalid.",
        ProfileReason.CONFIRMATION_MISMATCH: "Explicit confirmation did not match.",
        ProfileReason.FILESYSTEM_BOUNDARY: (
            "Managed filesystem boundary validation failed."
        ),
        ProfileReason.DIRECTORY_LIMIT: "Managed directory entry limit was reached.",
        ProfileReason.FILE_TOO_LARGE: "Managed file exceeds the supported size limit.",
        ProfileReason.INVALID_YAML: "Managed YAML validation failed.",
        ProfileReason.INVALID_CONFIGURATION: "Aurora configuration validation failed.",
        ProfileReason.PROFILE_MISMATCH: (
            "Profile identifier does not match its configuration."
        ),
        ProfileReason.PROFILE_CHANGED: "Profile changed during validation.",
        ProfileReason.ACTIVE_CHANGED: "Active configuration changed during validation.",
        ProfileReason.BACKUP_CAPACITY: "Managed backup capacity has been reached.",
        ProfileReason.BACKUP_INVALID: "Selected backup is invalid or corrupt.",
        ProfileReason.BACKUP_WRITE_FAILED: "Configuration backup could not be created.",
    }
    return ProfileCommandResult(
        ProfileExitCode.INVALID,
        reason,
        messages.get(reason, "Profile command failed safely."),
    )
