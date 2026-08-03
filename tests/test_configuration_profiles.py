"""Hardware-free tests for Milestone 17 local configuration profiles."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TextIO, cast

import pytest

from aurora_core.__main__ import build_parser, main
from aurora_core.config_profiles.filesystem import (
    AtomicWriteFailure,
    FileIdentity,
    SafeFilesystem,
    SecureFileSnapshot,
)
from aurora_core.config_profiles.identifiers import is_backup_id, is_profile_id
from aurora_core.config_profiles.manifests import (
    BackupRepository,
    parse_manifest,
    serialize_manifest,
)
from aurora_core.config_profiles.models import (
    MAX_MANIFEST_BYTES,
    MAX_PLAN_CHANGES,
    MAX_PROFILE_ENTRIES,
    MAX_YAML_BYTES,
    BackupManifest,
    BackupOperation,
    ChangeType,
    ProfileExitCode,
    ProfileOperationError,
    ProfileReason,
)
from aurora_core.config_profiles.planner import plan_changes
from aurora_core.config_profiles.rendering import render_profile_result
from aurora_core.config_profiles.service import ConfigurationProfileService
from aurora_core.config_profiles.validation import (
    parse_raw_yaml,
    validate_effective_yaml,
    validate_raw_yaml,
)

_NOW = datetime(2026, 1, 2, 3, 4, 5, 123456, UTC)
_FIRST_SUFFIX = "a1b2c3d4e5f6"
_FIRST_BACKUP_ID = "20260102T030405123456Z-a1b2c3d4e5f6"


@pytest.fixture(autouse=True)
def _clear_aurora_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name.startswith("AURORA_"):
            monkeypatch.delenv(name, raising=False)


class SuffixSequence:
    def __init__(self) -> None:
        self._value = 0

    def __call__(self, byte_count: int) -> str:
        assert byte_count == 6
        self._value += 1
        return f"{self._value:012x}"


def _secure_directory(parent: Path, name: str) -> Path:
    path = parent / name
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _secure_file(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def _yaml(
    profile_id: str,
    *,
    level: str = "INFO",
    extra: str = "",
) -> bytes:
    return (
        "application:\n"
        "  name: Project Aurora\n"
        f"  configuration_profile: {profile_id}\n"
        "logging:\n"
        f"  level: {level}\n"
        f"{extra}"
    ).encode()


def _layout(
    tmp_path: Path,
    *,
    active: bytes | None = None,
    profile_id: str = "maintenance",
    candidate: bytes | None = None,
) -> tuple[Path, Path, Path, Path]:
    profiles = _secure_directory(tmp_path, "profiles")
    backups = _secure_directory(tmp_path, "backups")
    config = _secure_file(tmp_path / "aurora.yaml", active or _yaml("current"))
    profile = _secure_file(
        profiles / f"{profile_id}.yaml", candidate or _yaml(profile_id, level="DEBUG")
    )
    return config, profiles, backups, profile


def _service(
    *,
    filesystem: SafeFilesystem | None = None,
    suffix: Callable[[int], str] | None = None,
    before_copy: Callable[[], None] | None = None,
    after_publish: Callable[[], None] | None = None,
) -> ConfigurationProfileService:
    return ConfigurationProfileService(
        filesystem,
        clock=lambda: _NOW,
        random_suffix=suffix or (lambda byte_count: _FIRST_SUFFIX),
        before_copy=before_copy,
        after_publish=after_publish,
    )


@pytest.mark.parametrize(
    "profile_id",
    (
        "a",
        "home-theater",
        "maintenance",
        "diagnostics-2",
        "a" * 40,
    ),
)
def test_profile_identifier_accepts_exact_grammar(profile_id: str) -> None:
    assert is_profile_id(profile_id)


@pytest.mark.parametrize(
    "profile_id",
    (
        "",
        " ",
        "../production",
        "/absolute",
        "profile.yaml",
        ".hidden",
        "home_theater",
        "Home-Theater",
        "home--theater",
        "-home",
        "home-",
        "café",
        "percent%2fpath",
        "a" * 41,
    ),
)
def test_profile_identifier_rejects_path_and_extension_forms(profile_id: str) -> None:
    assert not is_profile_id(profile_id)


@pytest.mark.parametrize(
    ("backup_id", "valid"),
    (
        (_FIRST_BACKUP_ID, True),
        ("20260230T030405123456Z-a1b2c3d4e5f6", False),
        ("../../backup", False),
        ("20260102T030405123456Z-A1B2C3D4E5F6", False),
        ("20260102T030405123456Z-a1b2", False),
    ),
)
def test_backup_identifier_is_generated_and_calendar_valid(
    backup_id: str, valid: bool
) -> None:
    assert is_backup_id(backup_id) is valid


@pytest.mark.parametrize(
    "data",
    (
        b"[unterminated",
        b"- list\n",
        b"---\na: 1\n---\nb: 2\n",
        b"\xff\xfe",
        b"value: !unsafe tag\n",
        b"base: &base\n  enabled: false\ncopy: *base\n",
        (
            b"application:\n  configuration_profile: one\n"
            b"application:\n  configuration_profile: two\n"
        ),
        b"application:\n  configuration_profile: one\n  configuration_profile: two\n",
    ),
)
def test_raw_yaml_rejects_malformed_nonmapping_multi_document_utf8_tags_and_duplicates(
    data: bytes,
) -> None:
    with pytest.raises(ProfileOperationError) as error:
        parse_raw_yaml(data)
    assert error.value.reason is ProfileReason.INVALID_YAML


def test_raw_yaml_direct_model_validation_and_profile_match() -> None:
    document, settings = validate_raw_yaml(
        _yaml("maintenance"), expected_profile_id="maintenance"
    )
    assert document["application"]["configuration_profile"] == "maintenance"
    assert settings.application.configuration_profile == "maintenance"
    with pytest.raises(ProfileOperationError) as mismatch:
        validate_raw_yaml(_yaml("maintenance"), expected_profile_id="other")
    assert mismatch.value.reason is ProfileReason.PROFILE_MISMATCH
    with pytest.raises(ProfileOperationError) as invalid:
        validate_raw_yaml(_yaml("maintenance", level="PRIVATE-VALUE"))
    assert invalid.value.reason is ProfileReason.INVALID_CONFIGURATION
    assert "PRIVATE-VALUE" not in str(invalid.value)


def test_raw_validation_ignores_environment_and_effective_validation_uses_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile = _secure_file(tmp_path / "profile.yaml", _yaml("maintenance"))
    monkeypatch.setenv("AURORA_LOGGING__LEVEL", "DEBUG")
    document, raw = validate_raw_yaml(profile.read_bytes())
    effective = validate_effective_yaml(document)
    assert raw.logging.level.value == "INFO"
    assert effective.logging.level.value == "DEBUG"


def test_validation_errors_never_expose_yaml_or_environment_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profiles = _secure_directory(tmp_path, "profiles")
    private_value = "private-environment-and-yaml-canary"
    _secure_file(
        profiles / "maintenance.yaml",
        _yaml("maintenance", level=private_value),
    )
    monkeypatch.setenv("AURORA_MQTT__PASSWORD", private_value)
    result = _service().validate_profile(profiles, "maintenance")
    assert result.exit_code == 2
    assert private_value not in result.message
    assert private_value not in result.reason_code.value


def test_list_is_sorted_skips_invalid_entries_and_never_parses_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profiles = _secure_directory(tmp_path, "profiles")
    _secure_file(profiles / "zeta.yaml", b"not: parsed: yaml")
    _secure_file(profiles / "alpha.yaml", b"also: [not parsed")
    _secure_file(profiles / "Bad.yaml", _yaml("bad"))
    _secure_file(profiles / "notes.txt", b"ignored")
    monkeypatch.setattr(
        "aurora_core.config_profiles.validation.parse_raw_yaml",
        lambda data: pytest.fail("list must not parse content"),
    )
    result = _service().list_profiles(profiles)
    assert result.exit_code == 0
    assert result.profiles == ("alpha", "zeta")
    assert result.skipped_entries == 2


def test_list_bounds_directory_enumeration(tmp_path: Path) -> None:
    profiles = _secure_directory(tmp_path, "profiles")
    for index in range(MAX_PROFILE_ENTRIES + 1):
        _secure_file(profiles / f"p{index:03d}.yaml", b"ignored")
    result = _service().list_profiles(profiles)
    assert len(result.profiles) <= MAX_PROFILE_ENTRIES
    assert result.entry_limit_reached


def test_validate_profile_does_not_read_active_configuration(tmp_path: Path) -> None:
    profiles = _secure_directory(tmp_path, "profiles")
    _secure_file(profiles / "maintenance.yaml", _yaml("maintenance"))
    result = _service().validate_profile(profiles, "maintenance")
    assert result.exit_code == 0
    assert result.profile_id == "maintenance"


@pytest.mark.parametrize("target", ("profile", "active"))
def test_symlink_profile_and_active_config_are_rejected(
    tmp_path: Path, target: str
) -> None:
    config, profiles, backups, profile = _layout(tmp_path)
    outside = _secure_file(tmp_path / "outside.yaml", _yaml("maintenance"))
    path = profile if target == "profile" else config
    path.unlink()
    path.symlink_to(outside)
    result = _service().apply(config, profiles, backups, "maintenance", "maintenance")
    assert result.exit_code == 2
    assert result.reason_code is ProfileReason.FILESYSTEM_BOUNDARY


def test_symlinked_directory_component_is_rejected(tmp_path: Path) -> None:
    config, profiles, backups, _ = _layout(tmp_path)
    alias = tmp_path / "profiles-alias"
    alias.symlink_to(profiles, target_is_directory=True)
    result = _service().apply(config, alias, backups, "maintenance", "maintenance")
    assert result.reason_code is ProfileReason.FILESYSTEM_BOUNDARY


@pytest.mark.parametrize("target", ("yaml", "json"))
def test_symlink_selected_backup_and_manifest_are_rejected(
    tmp_path: Path, target: str
) -> None:
    config, profiles, backups, _ = _layout(tmp_path)
    service = _service()
    assert (
        service.apply(config, profiles, backups, "maintenance", "maintenance").exit_code
        == 0
    )
    path = backups / f"{_FIRST_BACKUP_ID}.{target}"
    outside = _secure_file(tmp_path / f"outside.{target}", b"outside")
    path.unlink()
    path.symlink_to(outside)
    result = service.rollback(config, backups, _FIRST_BACKUP_ID, _FIRST_BACKUP_ID)
    assert result.exit_code == 2


@pytest.mark.parametrize("extension", ("yaml", "json"))
def test_hardlinked_selected_backup_and_manifest_are_rejected(
    tmp_path: Path, extension: str
) -> None:
    config, profiles, backups, _ = _layout(tmp_path)
    service = _service()
    assert (
        service.apply(config, profiles, backups, "maintenance", "maintenance").exit_code
        == 0
    )
    managed = backups / f"{_FIRST_BACKUP_ID}.{extension}"
    os.link(managed, backups / f"evidence-copy.{extension}")
    result = service.rollback(config, backups, _FIRST_BACKUP_ID, _FIRST_BACKUP_ID)
    assert result.exit_code == 2


def test_nonregular_hardlinked_and_insecure_files_are_rejected(tmp_path: Path) -> None:
    config, profiles, backups, profile = _layout(tmp_path)
    profile.unlink()
    profile.mkdir()
    assert (
        _service()
        .apply(config, profiles, backups, "maintenance", "maintenance")
        .exit_code
        == 2
    )
    profile.rmdir()
    source = _secure_file(tmp_path / "source.yaml", _yaml("maintenance"))
    os.link(source, profile)
    assert (
        _service()
        .apply(config, profiles, backups, "maintenance", "maintenance")
        .exit_code
        == 2
    )
    profile.unlink()
    _secure_file(profile, _yaml("maintenance"))
    profile.chmod(0o640)
    assert (
        _service()
        .apply(config, profiles, backups, "maintenance", "maintenance")
        .exit_code
        == 2
    )


@pytest.mark.parametrize("directory_name", ("profiles", "backups"))
def test_insecure_managed_directory_mode_is_rejected(
    tmp_path: Path, directory_name: str
) -> None:
    config, profiles, backups, _ = _layout(tmp_path)
    target = profiles if directory_name == "profiles" else backups
    target.chmod(0o770)
    result = _service().apply(config, profiles, backups, "maintenance", "maintenance")
    assert result.exit_code == 2


def test_wrong_owner_is_rejected_through_injected_effective_uid(tmp_path: Path) -> None:
    config, profiles, backups, _ = _layout(tmp_path)
    filesystem = SafeFilesystem(effective_uid=os.geteuid() + 1)
    result = _service(filesystem=filesystem).apply(
        config, profiles, backups, "maintenance", "maintenance"
    )
    assert result.exit_code == 2


def test_active_file_size_bound_is_enforced(tmp_path: Path) -> None:
    config, profiles, backups, _ = _layout(tmp_path)
    _secure_file(config, b"x" * (MAX_YAML_BYTES + 1))
    result = _service().apply(config, profiles, backups, "maintenance", "maintenance")
    assert result.reason_code is ProfileReason.FILE_TOO_LARGE


def test_profile_inode_and_bytes_changed_before_copy_are_rejected(
    tmp_path: Path,
) -> None:
    config, profiles, backups, profile = _layout(tmp_path)
    old_active = config.read_bytes()

    def replace_profile() -> None:
        replacement = _secure_file(
            tmp_path / "replacement.yaml", _yaml("maintenance", level="WARNING")
        )
        os.replace(replacement, profile)

    result = _service(before_copy=replace_profile).apply(
        config, profiles, backups, "maintenance", "maintenance"
    )
    assert result.reason_code is ProfileReason.PROFILE_CHANGED
    assert config.read_bytes() == old_active
    assert not tuple(backups.glob("*.json"))


def test_active_config_changed_before_copy_is_rejected(tmp_path: Path) -> None:
    config, profiles, backups, _ = _layout(tmp_path)

    def change_active() -> None:
        _secure_file(config, _yaml("changed", level="WARNING"))

    result = _service(before_copy=change_active).apply(
        config, profiles, backups, "maintenance", "maintenance"
    )
    assert result.reason_code is ProfileReason.ACTIVE_CHANGED
    assert not tuple(backups.glob("*.json"))


def test_plan_reports_only_sorted_paths_types_and_digests(tmp_path: Path) -> None:
    active = _yaml(
        "current",
        extra="lighting_zones:\n  - name: rear\n    enabled: false\n",
    )
    candidate = _yaml(
        "maintenance",
        level="DEBUG",
        extra=(
            "lighting_zones:\n"
            "  - name: rear\n"
            "    enabled: true\n"
            "  - name: side\n"
            "    enabled: false\n"
            "mqtt:\n"
            "  enabled: false\n"
        ),
    )
    config, profiles, _, _ = _layout(tmp_path, active=active, candidate=candidate)
    result = _service().plan(config, profiles, "maintenance")
    assert result.exit_code == 0
    assert result.plan is not None
    paths = tuple(change.path for change in result.plan.changes)
    assert paths == tuple(sorted(paths))
    assert "application.configuration_profile" in paths
    assert "lighting_zones[0].enabled" in paths
    assert "lighting_zones[1]" in paths
    assert "mqtt" in paths
    assert {change.change_type for change in result.plan.changes} >= {
        ChangeType.ADDED,
        ChangeType.CHANGED,
    }
    assert result.plan.active_sha256 == hashlib.sha256(active).hexdigest()
    assert result.plan.candidate_sha256 == hashlib.sha256(candidate).hexdigest()
    assert not result.plan.byte_identical


def test_plan_removed_path_list_change_truncation_and_no_write(tmp_path: Path) -> None:
    active = {"a": {f"k{index:03d}": index for index in range(MAX_PLAN_CHANGES + 2)}}
    candidate = {"a": {}}
    changes, truncated = plan_changes(active, candidate)
    assert len(changes) == MAX_PLAN_CHANGES
    assert truncated
    assert all(change.change_type is ChangeType.REMOVED for change in changes)

    identical = _yaml("maintenance")
    config, profiles, backups, _ = _layout(
        tmp_path, active=identical, candidate=identical
    )
    before = config.stat().st_mtime_ns
    result = _service().plan(config, profiles, "maintenance")
    assert result.plan is not None and result.plan.byte_identical
    assert config.stat().st_mtime_ns == before
    assert not tuple(backups.iterdir())


def test_rendered_plan_never_contains_changed_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    private_old = "private-old-value"
    private_new = "private-new-value"
    config, profiles, _, _ = _layout(
        tmp_path,
        active=_yaml("current", extra=f"application_name: {private_old}\n"),
        candidate=_yaml("maintenance", extra=f"application_name: {private_new}\n"),
    )
    result = _service().plan(config, profiles, "maintenance")
    assert (
        result.exit_code == 2
    )  # unknown keys fail before any diff can disclose values
    render_profile_result(result, stdout=os.sys.stdout, stderr=os.sys.stderr)
    output = capsys.readouterr()
    assert private_old not in output.out + output.err
    assert private_new not in output.out + output.err


def test_apply_creates_exact_backup_manifest_and_atomic_replacement(
    tmp_path: Path,
) -> None:
    old = _yaml("current", extra="# exact source bytes retained\n")
    candidate = _yaml("maintenance", level="DEBUG")
    config, profiles, backups, _ = _layout(tmp_path, active=old, candidate=candidate)
    result = _service().apply(config, profiles, backups, "maintenance", "maintenance")
    assert result.exit_code == 0
    assert result.reason_code is ProfileReason.APPLIED
    assert config.read_bytes() == candidate
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    backup = backups / f"{_FIRST_BACKUP_ID}.yaml"
    manifest_path = backups / f"{_FIRST_BACKUP_ID}.json"
    assert backup.read_bytes() == old
    manifest = parse_manifest(manifest_path.read_bytes())
    assert manifest == BackupManifest(
        schema_version=1,
        backup_id=_FIRST_BACKUP_ID,
        created_at_utc="2026-01-02T03:04:05.123456Z",
        source_sha256=hashlib.sha256(old).hexdigest(),
        source_byte_count=len(old),
        operation=BackupOperation.APPLY,
        target_profile_id="maintenance",
        target_backup_id=None,
    )
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
    assert stat.S_IMODE((backups / ".aurora-config.lock").stat().st_mode) == 0o600
    assert not tuple(tmp_path.glob(".aurora-config.tmp-*"))


def test_manifest_is_compact_bounded_and_rejects_unknown_fields() -> None:
    manifest = BackupManifest(
        backup_id=_FIRST_BACKUP_ID,
        created_at_utc="2026-01-02T03:04:05.123456Z",
        source_sha256="a" * 64,
        source_byte_count=12,
        operation=BackupOperation.APPLY,
        target_profile_id="maintenance",
        target_backup_id=None,
    )
    encoded = serialize_manifest(manifest)
    assert len(encoded) < MAX_MANIFEST_BYTES
    assert b"\n" not in encoded and b" " not in encoded
    document = json.loads(encoded)
    document["unknown"] = "forbidden"
    with pytest.raises(ProfileOperationError):
        parse_manifest(json.dumps(document).encode())
    duplicate = encoded[:-1] + b',"schema_version":1}'
    with pytest.raises(ProfileOperationError):
        parse_manifest(duplicate)
    document = json.loads(encoded)
    document["created_at_utc"] = "2026-01-02T03:04:06.123456Z"
    with pytest.raises(ProfileOperationError):
        parse_manifest(json.dumps(document).encode())


def test_apply_byte_identical_is_noop_without_backup(tmp_path: Path) -> None:
    data = _yaml("maintenance")
    config, profiles, backups, _ = _layout(tmp_path, active=data, candidate=data)
    result = _service().apply(config, profiles, backups, "maintenance", "maintenance")
    assert result.exit_code == 0
    assert result.reason_code is ProfileReason.NO_CHANGE
    assert not tuple(backups.glob("*.yaml"))
    assert not tuple(backups.glob("*.json"))


def test_apply_requires_exact_confirmation_and_bounded_backup_count(
    tmp_path: Path,
) -> None:
    config, profiles, backups, _ = _layout(tmp_path)
    service = _service()
    for confirmation in ("", "Maintenance", "maintenance "):
        result = service.apply(config, profiles, backups, "maintenance", confirmation)
        assert result.reason_code is ProfileReason.CONFIRMATION_MISMATCH
    for maximum in (0, 101):
        result = service.apply(
            config,
            profiles,
            backups,
            "maintenance",
            "maintenance",
            maximum_backups=maximum,
        )
        assert result.reason_code is ProfileReason.INVALID_ARGUMENT


def test_backup_capacity_refuses_before_active_mutation(tmp_path: Path) -> None:
    config, profiles, backups, _ = _layout(tmp_path)
    old = config.read_bytes()
    first = _service().apply(
        config,
        profiles,
        backups,
        "maintenance",
        "maintenance",
        maximum_backups=1,
    )
    assert first.exit_code == 0
    _secure_file(config, old)
    second = _service(suffix=SuffixSequence()).apply(
        config,
        profiles,
        backups,
        "maintenance",
        "maintenance",
        maximum_backups=1,
    )
    assert second.reason_code is ProfileReason.BACKUP_CAPACITY
    assert config.read_bytes() == old
    assert len(tuple(backups.glob("*.yaml"))) == 1
    assert (backups / f"{_FIRST_BACKUP_ID}.yaml").exists()
    assert (backups / f"{_FIRST_BACKUP_ID}.json").exists()


def test_backup_identifier_collision_is_retried(tmp_path: Path) -> None:
    config, _, backups, _ = _layout(tmp_path)
    filesystem = SafeFilesystem()
    source = filesystem.read_secure_file(config, maximum_bytes=MAX_YAML_BYTES)
    _secure_file(backups / f"{_FIRST_BACKUP_ID}.yaml", b"collision")
    suffixes = iter((_FIRST_SUFFIX, "000000000002"))
    repository = BackupRepository(
        filesystem,
        clock=lambda: _NOW,
        random_suffix=lambda count: next(suffixes),
    )
    manifest = repository.create(
        backups,
        source,
        operation=BackupOperation.APPLY,
        maximum_backups=20,
        target_profile_id="maintenance",
        target_backup_id=None,
    )
    assert manifest.backup_id.endswith("-000000000002")


def test_backup_and_manifest_write_failures_leave_active_unchanged(
    tmp_path: Path,
) -> None:
    config, profiles, backups, _ = _layout(tmp_path)
    original = config.read_bytes()

    class FailingBackupFilesystem(SafeFilesystem):
        def __init__(self, fail_on: int) -> None:
            super().__init__()
            self.calls = 0
            self.fail_on = fail_on

        def write_new_file(self, directory: Path, name: str, data: bytes) -> None:
            self.calls += 1
            if self.calls == self.fail_on:
                raise ProfileOperationError(ProfileReason.BACKUP_WRITE_FAILED)
            super().write_new_file(directory, name, data)

    for fail_on in (1, 2):
        for path in tuple(backups.iterdir()):
            if path.name != ".aurora-config.lock":
                path.unlink()
        _secure_file(config, original)
        result = _service(filesystem=FailingBackupFilesystem(fail_on)).apply(
            config, profiles, backups, "maintenance", "maintenance"
        )
        assert result.exit_code == 2
        assert config.read_bytes() == original


def test_unexpected_internal_failure_is_sanitized_and_leaves_active_unchanged(
    tmp_path: Path,
) -> None:
    config, profiles, backups, _ = _layout(tmp_path)
    original = config.read_bytes()
    private_error = "private-raw-exception-canary"

    def fail_clock() -> datetime:
        raise RuntimeError(private_error)

    service = ConfigurationProfileService(
        clock=fail_clock, random_suffix=lambda count: _FIRST_SUFFIX
    )
    result = service.apply(config, profiles, backups, "maintenance", "maintenance")
    assert result.exit_code == 2
    assert private_error not in result.message
    assert config.read_bytes() == original


def test_nonblocking_shared_mutation_lock_returns_busy(tmp_path: Path) -> None:
    config, profiles, backups, _ = _layout(tmp_path)
    filesystem = SafeFilesystem()
    with filesystem.mutation_lock(backups):
        result = _service(filesystem=filesystem).apply(
            config, profiles, backups, "maintenance", "maintenance"
        )
    assert result.exit_code == 3
    assert result.reason_code is ProfileReason.MUTATION_BUSY


@pytest.mark.parametrize("lock_kind", ("insecure", "symlink", "hardlink"))
def test_existing_lock_must_be_secure_empty_owned_and_single_link(
    tmp_path: Path, lock_kind: str
) -> None:
    config, profiles, backups, _ = _layout(tmp_path)
    lock = backups / ".aurora-config.lock"
    if lock_kind == "symlink":
        lock.symlink_to(_secure_file(tmp_path / "outside-lock", b""))
    else:
        _secure_file(lock, b"")
        if lock_kind == "insecure":
            lock.chmod(0o640)
        else:
            os.link(lock, tmp_path / "lock-link")
    result = _service().apply(config, profiles, backups, "maintenance", "maintenance")
    assert result.exit_code == 2
    assert not tuple(backups.glob("*.json"))


def test_atomic_replace_failure_before_publication_leaves_active_unchanged(
    tmp_path: Path,
) -> None:
    config, profiles, backups, _ = _layout(tmp_path)
    original = config.read_bytes()

    class ReplaceFailureFilesystem(SafeFilesystem):
        def atomic_replace(
            self,
            destination: Path,
            data: bytes,
            *,
            expected_identity: FileIdentity | None,
        ) -> None:
            raise AtomicWriteFailure(published=False)

    result = _service(filesystem=ReplaceFailureFilesystem()).apply(
        config, profiles, backups, "maintenance", "maintenance"
    )
    assert result.exit_code == 2
    assert config.read_bytes() == original
    assert (backups / f"{_FIRST_BACKUP_ID}.yaml").read_bytes() == original


def test_os_replace_failure_removes_unpublished_temp_and_keeps_active(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, profiles, backups, _ = _layout(tmp_path)
    original = config.read_bytes()
    monkeypatch.setattr(
        "aurora_core.config_profiles.filesystem.os.replace",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("replace failed")),
    )
    result = _service().apply(config, profiles, backups, "maintenance", "maintenance")
    assert result.exit_code == 2
    assert config.read_bytes() == original
    assert not tuple(tmp_path.glob(".aurora-config.tmp-*"))


def test_backup_and_activation_fsync_files_before_directories(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, profiles, backups, _ = _layout(tmp_path)
    actual_fsync = os.fsync
    calls: list[bool] = []

    def record(descriptor: int) -> None:
        calls.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))
        actual_fsync(descriptor)

    monkeypatch.setattr("aurora_core.config_profiles.filesystem.os.fsync", record)
    assert (
        _service()
        .apply(config, profiles, backups, "maintenance", "maintenance")
        .exit_code
        == 0
    )
    assert calls[:5] == [False, False, True, False, True]


def test_directory_fsync_failure_after_publication_restores_active(
    tmp_path: Path,
) -> None:
    config, profiles, backups, _ = _layout(tmp_path)
    original = config.read_bytes()

    class PublishedFailureOnceFilesystem(SafeFilesystem):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def atomic_replace(
            self,
            destination: Path,
            data: bytes,
            *,
            expected_identity: FileIdentity | None,
        ) -> None:
            self.calls += 1
            super().atomic_replace(
                destination, data, expected_identity=expected_identity
            )
            if self.calls == 1:
                raise AtomicWriteFailure(published=True)

    result = _service(filesystem=PublishedFailureOnceFilesystem()).apply(
        config, profiles, backups, "maintenance", "maintenance"
    )
    assert result.exit_code == 4
    assert result.reason_code is ProfileReason.ACTIVATION_FAILED_RESTORED
    assert config.read_bytes() == original


def test_post_write_content_mismatch_restores_previous_bytes(tmp_path: Path) -> None:
    config, profiles, backups, _ = _layout(tmp_path)
    original = config.read_bytes()

    def corrupt_after_publish() -> None:
        _secure_file(config, _yaml("corrupted"))

    result = _service(after_publish=corrupt_after_publish).apply(
        config, profiles, backups, "maintenance", "maintenance"
    )
    assert result.exit_code == 4
    assert config.read_bytes() == original
    assert (backups / f"{_FIRST_BACKUP_ID}.yaml").exists()


def test_post_write_hash_mismatch_restores_previous_bytes(tmp_path: Path) -> None:
    config, profiles, backups, _ = _layout(tmp_path)
    original = config.read_bytes()

    class HashMismatchFilesystem(SafeFilesystem):
        def __init__(self) -> None:
            super().__init__()
            self.reads = 0

        def read_secure_file(
            self, path: Path, *, maximum_bytes: int
        ) -> SecureFileSnapshot:
            snapshot = super().read_secure_file(path, maximum_bytes=maximum_bytes)
            self.reads += 1
            if self.reads == 5:
                return SecureFileSnapshot(snapshot.data, "0" * 64, snapshot.identity)
            return snapshot

    result = _service(filesystem=HashMismatchFilesystem()).apply(
        config, profiles, backups, "maintenance", "maintenance"
    )
    assert result.exit_code == 4
    assert config.read_bytes() == original


@pytest.mark.parametrize("stage", ("raw", "effective"))
def test_post_write_validation_failure_restores_previous_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stage: str
) -> None:
    config, profiles, backups, _ = _layout(tmp_path)
    original = config.read_bytes()
    module = "aurora_core.config_profiles.service"
    if stage == "raw":
        import aurora_core.config_profiles.service as service_module

        actual = service_module.validate_raw_yaml
        calls = 0

        def fail_post(data: bytes, *, expected_profile_id: str | None = None):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            if calls == 3:
                raise ProfileOperationError(ProfileReason.INVALID_YAML)
            return actual(data, expected_profile_id=expected_profile_id)

        monkeypatch.setattr(f"{module}.validate_raw_yaml", fail_post)
    else:
        import aurora_core.config_profiles.service as service_module

        actual_effective = service_module.validate_effective_yaml
        calls = 0

        def fail_post_effective(document):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1
            if calls == 3:
                raise ProfileOperationError(ProfileReason.INVALID_CONFIGURATION)
            return actual_effective(document)

        monkeypatch.setattr(f"{module}.validate_effective_yaml", fail_post_effective)
    result = _service().apply(config, profiles, backups, "maintenance", "maintenance")
    assert result.exit_code == 4
    assert config.read_bytes() == original


def test_failed_automatic_restoration_has_distinct_exit_code_and_keeps_evidence(
    tmp_path: Path,
) -> None:
    config, profiles, backups, _ = _layout(tmp_path)

    class RestorationFailureFilesystem(SafeFilesystem):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def atomic_replace(
            self,
            destination: Path,
            data: bytes,
            *,
            expected_identity: FileIdentity | None,
        ) -> None:
            self.calls += 1
            if self.calls == 2:
                raise AtomicWriteFailure(published=False)
            super().atomic_replace(
                destination, data, expected_identity=expected_identity
            )

    def force_post_failure() -> None:
        _secure_file(config, b"invalid: [")

    result = _service(
        filesystem=RestorationFailureFilesystem(), after_publish=force_post_failure
    ).apply(config, profiles, backups, "maintenance", "maintenance")
    assert result.exit_code == 5
    assert result.reason_code is ProfileReason.AUTOMATIC_RESTORATION_FAILED
    assert (backups / f"{_FIRST_BACKUP_ID}.yaml").exists()
    assert (backups / f"{_FIRST_BACKUP_ID}.json").exists()


def test_backup_listing_is_newest_first_and_reports_integrity(tmp_path: Path) -> None:
    config, profiles, backups, _ = _layout(tmp_path)
    suffix = SuffixSequence()
    times = iter((_NOW, _NOW + timedelta(seconds=1)))
    service = ConfigurationProfileService(
        clock=lambda: next(times), random_suffix=suffix
    )
    assert (
        service.apply(config, profiles, backups, "maintenance", "maintenance").exit_code
        == 0
    )
    _secure_file(config, _yaml("current"))
    assert (
        service.apply(config, profiles, backups, "maintenance", "maintenance").exit_code
        == 0
    )
    listing = service.list_backups(backups)
    assert listing.exit_code == 0
    assert len(listing.backups) == 2
    assert listing.backups[0].backup_id > listing.backups[1].backup_id
    assert all(record.integrity_valid for record in listing.backups)


def test_backup_listing_detects_corrupt_yaml_hash_and_byte_count(
    tmp_path: Path,
) -> None:
    config, profiles, backups, _ = _layout(tmp_path)
    service = _service()
    assert (
        service.apply(config, profiles, backups, "maintenance", "maintenance").exit_code
        == 0
    )
    backup = backups / f"{_FIRST_BACKUP_ID}.yaml"
    _secure_file(backup, b"corrupted backup bytes")
    listing = service.list_backups(backups)
    assert len(listing.backups) == 1
    assert not listing.backups[0].integrity_valid


@pytest.mark.parametrize("corruption", ("malformed", "unknown", "oversized"))
def test_backup_listing_skips_corrupt_manifest_without_printing_content(
    tmp_path: Path, corruption: str
) -> None:
    config, profiles, backups, _ = _layout(tmp_path)
    service = _service()
    assert (
        service.apply(config, profiles, backups, "maintenance", "maintenance").exit_code
        == 0
    )
    manifest = backups / f"{_FIRST_BACKUP_ID}.json"
    if corruption == "malformed":
        content = b"{private-manifest-canary"
    elif corruption == "unknown":
        document = json.loads(manifest.read_bytes())
        document["private_unknown"] = "private-manifest-canary"
        content = json.dumps(document).encode()
    else:
        content = b"x" * (MAX_MANIFEST_BYTES + 1)
    _secure_file(manifest, content)
    listing = service.list_backups(backups)
    assert listing.backups == ()
    assert listing.skipped_entries >= 2


def test_backup_listing_skips_missing_pair_and_malformed_filename(
    tmp_path: Path,
) -> None:
    backups = _secure_directory(tmp_path, "backups")
    _secure_file(backups / f"{_FIRST_BACKUP_ID}.yaml", b"orphan")
    _secure_file(backups / "../not-possible.yaml", b"ignored") if False else None
    _secure_file(backups / "not-a-backup.yaml", b"ignored")
    listing = _service().list_backups(backups)
    assert listing.backups == ()
    assert listing.skipped_entries == 2


def test_backup_listing_bounds_directory_enumeration(tmp_path: Path) -> None:
    backups = _secure_directory(tmp_path, "backups")
    for index in range(513):
        _secure_file(backups / f"unmanaged-{index:03d}", b"")
    listing = _service().list_backups(backups)
    assert listing.exit_code == 0
    assert listing.entry_limit_reached
    assert listing.backups == ()


def test_backup_rendering_does_not_print_yaml_or_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    private = "private-yaml-content-canary"
    config, profiles, backups, _ = _layout(
        tmp_path, active=_yaml("current", extra=f"# {private}\n")
    )
    service = _service()
    assert (
        service.apply(config, profiles, backups, "maintenance", "maintenance").exit_code
        == 0
    )
    result = service.list_backups(backups)
    render_profile_result(result, stdout=os.sys.stdout, stderr=os.sys.stderr)
    output = capsys.readouterr().out
    assert private not in output
    assert str(config) not in output
    assert str(backups) not in output


def _applied_layout(
    tmp_path: Path,
) -> tuple[ConfigurationProfileService, Path, Path, bytes, bytes, str]:
    original = _yaml("current", level="INFO")
    candidate = _yaml("maintenance", level="DEBUG")
    config, profiles, backups, _ = _layout(
        tmp_path, active=original, candidate=candidate
    )
    service = _service(suffix=SuffixSequence())
    applied = service.apply(config, profiles, backups, "maintenance", "maintenance")
    assert applied.exit_code == 0
    selected = tuple(backups.glob("*.json"))[0].stem
    return service, config, backups, original, candidate, selected


def test_rollback_requires_exact_generated_id_and_confirmation(tmp_path: Path) -> None:
    service, config, backups, _, candidate, selected = _applied_layout(tmp_path)
    for backup_id in ("../backup", "/absolute", "backup.yaml"):
        result = service.rollback(config, backups, backup_id, backup_id)
        assert result.reason_code is ProfileReason.INVALID_BACKUP_ID
    mismatch = service.rollback(config, backups, selected, "wrong")
    assert mismatch.reason_code is ProfileReason.CONFIRMATION_MISMATCH
    assert config.read_bytes() == candidate


def test_rollback_refuses_backup_capacity_before_mutation(tmp_path: Path) -> None:
    service, config, backups, _, candidate, selected = _applied_layout(tmp_path)
    result = service.rollback(
        config,
        backups,
        selected,
        selected,
        maximum_backups=1,
    )
    assert result.reason_code is ProfileReason.BACKUP_CAPACITY
    assert config.read_bytes() == candidate
    assert len(tuple(backups.glob("*.json"))) == 1


def test_selected_backup_changed_after_validation_is_rejected(tmp_path: Path) -> None:
    _, config, backups, _, candidate, selected = _applied_layout(tmp_path)

    def change_selected() -> None:
        _secure_file(backups / f"{selected}.yaml", _yaml("changed"))

    result = _service(suffix=SuffixSequence(), before_copy=change_selected).rollback(
        config, backups, selected, selected
    )
    assert result.reason_code is ProfileReason.BACKUP_INVALID
    assert config.read_bytes() == candidate
    assert len(tuple(backups.glob("*.json"))) == 1


def test_rollback_rejects_missing_or_corrupt_selected_backup_before_mutation(
    tmp_path: Path,
) -> None:
    service, config, backups, _, candidate, selected = _applied_layout(tmp_path)
    missing_id = "20260102T030406123456Z-ffffffffffff"
    assert (
        service.rollback(config, backups, missing_id, missing_id).reason_code
        is ProfileReason.FILESYSTEM_BOUNDARY
    )
    _secure_file(backups / f"{selected}.yaml", b"corrupt")
    result = service.rollback(config, backups, selected, selected)
    assert result.exit_code == 2
    assert config.read_bytes() == candidate
    assert len(tuple(backups.glob("*.json"))) == 1


def test_selected_backup_raw_and_effective_validation_precedes_mutation(
    tmp_path: Path,
) -> None:
    service, config, backups, _, candidate, selected = _applied_layout(tmp_path)
    invalid = (
        b"application:\n  configuration_profile: invalid\nlogging:\n  level: SECRET\n"
    )
    _secure_file(backups / f"{selected}.yaml", invalid)
    manifest_path = backups / f"{selected}.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["source_sha256"] = hashlib.sha256(invalid).hexdigest()
    manifest["source_byte_count"] = len(invalid)
    _secure_file(manifest_path, json.dumps(manifest, separators=(",", ":")).encode())
    result = service.rollback(config, backups, selected, selected)
    assert result.reason_code is ProfileReason.INVALID_CONFIGURATION
    assert config.read_bytes() == candidate
    assert len(tuple(backups.glob("*.json"))) == 1


def test_successful_rollback_is_atomic_and_creates_reversible_backup(
    tmp_path: Path,
) -> None:
    service, config, backups, original, candidate, selected = _applied_layout(tmp_path)
    result = service.rollback(config, backups, selected, selected)
    assert result.exit_code == 0
    assert result.reason_code is ProfileReason.ROLLED_BACK
    assert config.read_bytes() == original
    manifests = [parse_manifest(path.read_bytes()) for path in backups.glob("*.json")]
    assert len(manifests) == 2
    rollback_manifest = next(
        manifest
        for manifest in manifests
        if manifest.operation is BackupOperation.ROLLBACK
    )
    assert rollback_manifest.target_backup_id == selected
    assert rollback_manifest.target_profile_id is None
    reversible_yaml = backups / f"{rollback_manifest.backup_id}.yaml"
    assert reversible_yaml.read_bytes() == candidate


def test_rollback_is_itself_reversible(tmp_path: Path) -> None:
    service, config, backups, original, candidate, selected = _applied_layout(tmp_path)
    assert service.rollback(config, backups, selected, selected).exit_code == 0
    pre_rollback = next(
        parse_manifest(path.read_bytes())
        for path in backups.glob("*.json")
        if parse_manifest(path.read_bytes()).operation is BackupOperation.ROLLBACK
    )
    assert config.read_bytes() == original
    assert (
        service.rollback(
            config, backups, pre_rollback.backup_id, pre_rollback.backup_id
        ).exit_code
        == 0
    )
    assert config.read_bytes() == candidate


def test_failed_rollback_restores_pre_rollback_bytes(tmp_path: Path) -> None:
    _, config, backups, _, candidate, selected = _applied_layout(tmp_path)

    def corrupt_after_publish() -> None:
        _secure_file(config, b"invalid: [")

    service = _service(suffix=SuffixSequence(), after_publish=corrupt_after_publish)
    result = service.rollback(config, backups, selected, selected)
    assert result.exit_code == 4
    assert config.read_bytes() == candidate
    assert len(tuple(backups.glob("*.json"))) == 2


def test_rollback_restoration_failure_uses_exit_five(tmp_path: Path) -> None:
    _, config, backups, _, _, selected = _applied_layout(tmp_path)

    class RollbackRestorationFailureFilesystem(SafeFilesystem):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def atomic_replace(
            self,
            destination: Path,
            data: bytes,
            *,
            expected_identity: FileIdentity | None,
        ) -> None:
            self.calls += 1
            if self.calls == 2:
                raise AtomicWriteFailure(published=False)
            super().atomic_replace(
                destination, data, expected_identity=expected_identity
            )

    result = _service(
        filesystem=RollbackRestorationFailureFilesystem(),
        suffix=SuffixSequence(),
        after_publish=lambda: _secure_file(config, b"invalid: ["),
    ).rollback(config, backups, selected, selected)
    assert result.exit_code == 5


def test_profile_cli_registers_exact_six_commands_and_required_arguments() -> None:
    parser = build_parser()
    commands = (
        ["config", "profile", "list", "--profiles-dir", "profiles"],
        [
            "config",
            "profile",
            "validate",
            "--profiles-dir",
            "profiles",
            "--profile",
            "maintenance",
        ],
        [
            "config",
            "profile",
            "plan",
            "--config",
            "active.yaml",
            "--profiles-dir",
            "profiles",
            "--profile",
            "maintenance",
        ],
        [
            "config",
            "profile",
            "apply",
            "--config",
            "active.yaml",
            "--profiles-dir",
            "profiles",
            "--backups-dir",
            "backups",
            "--profile",
            "maintenance",
            "--confirm-apply",
            "maintenance",
        ],
        ["config", "profile", "backups", "--backups-dir", "backups"],
        [
            "config",
            "profile",
            "rollback",
            "--config",
            "active.yaml",
            "--backups-dir",
            "backups",
            "--backup-id",
            _FIRST_BACKUP_ID,
            "--confirm-rollback",
            _FIRST_BACKUP_ID,
        ],
    )
    assert tuple(
        parser.parse_args(command).profile_command for command in commands
    ) == (
        "list",
        "validate",
        "plan",
        "apply",
        "backups",
        "rollback",
    )


def test_profile_exit_code_mapping_is_stable() -> None:
    assert {member.name: int(member) for member in ProfileExitCode} == {
        "SUCCESS": 0,
        "INVALID": 2,
        "BUSY": 3,
        "RESTORED": 4,
        "RESTORATION_FAILED": 5,
    }


def test_profile_cli_apply_and_backups_messages_require_external_restart(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    config, profiles, backups, _ = _layout(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        [
            "aurora",
            "config",
            "profile",
            "apply",
            "--config",
            str(config),
            "--profiles-dir",
            str(profiles),
            "--backups-dir",
            str(backups),
            "--profile",
            "maintenance",
            "--confirm-apply",
            "maintenance",
        ],
    )
    assert main() == 0
    output = capsys.readouterr().out
    assert "external Aurora service restart is required" in output
    assert "systemctl" not in output


def test_console_failure_after_success_does_not_restore_active(tmp_path: Path) -> None:
    config, profiles, backups, profile = _layout(tmp_path)
    result = _service().apply(config, profiles, backups, "maintenance", "maintenance")
    assert result.exit_code == 0

    class FailingStream:
        def write(self, value: str) -> int:
            raise OSError("console unavailable")

    with pytest.raises(OSError):
        render_profile_result(
            result,
            stdout=cast(TextIO, FailingStream()),
            stderr=cast(TextIO, FailingStream()),
        )
    assert config.read_bytes() == profile.read_bytes()


def test_profile_operations_never_invoke_subprocess_or_network(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config, profiles, backups, _ = _layout(tmp_path)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("subprocess execution is prohibited"),
    )
    monkeypatch.setattr(
        "socket.create_connection",
        lambda *args, **kwargs: pytest.fail("network access is prohibited"),
    )
    service = _service()
    assert service.plan(config, profiles, "maintenance").exit_code == 0
    assert (
        service.apply(config, profiles, backups, "maintenance", "maintenance").exit_code
        == 0
    )
