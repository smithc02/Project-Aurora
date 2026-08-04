"""Synthetic tests for SQLite platform gates and report boundaries."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from aurora_core.m18_validation.filesystem import (
    FilesystemBoundaryError,
    FilesystemRejection,
    create_secure_file,
    require_same_identity,
    validate_protected_directory,
    validate_regular_file,
)
from aurora_core.m18_validation.models import (
    CheckResult,
    CheckStatus,
    ToolReport,
)
from aurora_core.m18_validation.platform import run_platform_validation


def _protected(path: Path) -> Path:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def test_filesystem_boundary_accepts_only_secure_directory_and_file(
    tmp_path: Path,
) -> None:
    root = _protected(tmp_path / "root")
    validate_protected_directory(root)
    database = root / "database.sqlite3"
    identity = create_secure_file(database)
    assert database.stat().st_mode & 0o777 == 0o600
    assert validate_regular_file(database) == identity
    require_same_identity(database, identity)


@pytest.mark.parametrize("mode", [0o755, 0o770, 0o777])
def test_insecure_directory_rejected(tmp_path: Path, mode: int) -> None:
    root = _protected(tmp_path / "root")
    root.chmod(mode)
    with pytest.raises(FilesystemBoundaryError) as caught:
        validate_protected_directory(root)
    assert caught.value.reason is FilesystemRejection.WRONG_MODE


def test_symlink_nonregular_hardlink_and_identity_change_rejected(
    tmp_path: Path,
) -> None:
    root = _protected(tmp_path / "root")
    target = root / "target"
    original = create_secure_file(target)
    link = root / "link"
    link.symlink_to(target)
    with pytest.raises(FilesystemBoundaryError) as symlink:
        validate_regular_file(link)
    assert symlink.value.reason is FilesystemRejection.SYMLINK

    directory = root / "directory"
    directory.mkdir(mode=0o700)
    with pytest.raises(FilesystemBoundaryError) as wrong_type:
        validate_regular_file(directory)
    assert wrong_type.value.reason is FilesystemRejection.WRONG_TYPE

    alias = root / "alias"
    os.link(target, alias)
    with pytest.raises(FilesystemBoundaryError) as hard_link:
        validate_regular_file(target)
    assert hard_link.value.reason is FilesystemRejection.HARD_LINK
    alias.unlink()

    replacement = root / "replacement"
    create_secure_file(replacement)
    os.replace(replacement, target)
    with pytest.raises(FilesystemBoundaryError) as changed:
        require_same_identity(target, original)
    assert changed.value.reason is FilesystemRejection.IDENTITY_CHANGED


def test_insecure_file_mode_and_wrong_owner_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _protected(tmp_path / "root")
    database = root / "database"
    create_secure_file(database)
    database.chmod(0o640)
    with pytest.raises(FilesystemBoundaryError) as insecure:
        validate_regular_file(database)
    assert insecure.value.reason is FilesystemRejection.WRONG_MODE

    database.chmod(0o600)
    monkeypatch.setattr(
        "aurora_core.m18_validation.filesystem.os.geteuid",
        lambda: root.stat().st_uid + 1,
    )
    with pytest.raises(FilesystemBoundaryError) as wrong_owner:
        validate_protected_directory(root)
    assert wrong_owner.value.reason is FilesystemRejection.WRONG_OWNER


def test_platform_report_exercises_required_gates_without_private_paths(
    tmp_path: Path,
) -> None:
    root = _protected(tmp_path / "isolated")
    report = run_platform_validation(root)
    assert report.passed
    by_name = {check.name: check for check in report.checks}
    required = {
        "protected_parent_directory",
        "symlink_parent_rejection",
        "symlink_database_rejection",
        "non_regular_rejection",
        "hard_link_rejection",
        "identity_change_rejection",
        "database_open_boundary",
        "wal_sidecar_permissions",
        "progress_handler_interruption",
        "bounded_quick_check",
        "bounded_wal_checkpoint",
        "online_backup_bounded",
        "interrupted_transaction_rollback",
        "migration_preflight_and_rollback",
        "restore_validation",
        "incremental_vacuum",
        "shutdown_checkpoint_and_close",
        "public_health_independence",
    }
    assert required <= by_name.keys()
    assert all(by_name[name].status is CheckStatus.PASS for name in required)
    payload = report.to_json()
    assert str(root) not in payload
    assert json.loads(payload)["test_root"] == "<isolated-test-root>"
    assert "O_NOFOLLOW guarantee is claimed" in payload


def test_platform_invalid_root_fails_without_creating_database(tmp_path: Path) -> None:
    root = _protected(tmp_path / "insecure")
    root.chmod(0o755)
    report = run_platform_validation(root)
    assert not report.passed
    assert not (root / "platform-validation.sqlite3").exists()


def test_report_fixed_status_and_required_skip_semantics() -> None:
    skipped = ToolReport(
        "synthetic.v1",
        (CheckResult("privileged", CheckStatus.SKIPPED, "not available", False),),
    )
    assert skipped.passed
    failed = ToolReport(
        "synthetic.v1", (CheckResult("required", CheckStatus.FAIL, "failed"),)
    )
    assert not failed.passed
    assert json.loads(failed.to_json())["result"] == "FAIL"
    with pytest.raises(ValueError, match="skipped_check_must_be_non_required"):
        CheckResult("required-skip", CheckStatus.SKIPPED, "invalid")
