"""Sanitized console rendering for typed profile command results."""

from __future__ import annotations

from typing import TextIO

from aurora_core.config_profiles.models import ProfileCommandResult, ProfileExitCode


def render_profile_result(
    result: ProfileCommandResult, *, stdout: TextIO, stderr: TextIO
) -> None:
    """Render only allowlisted identifiers, metadata, digests, and fixed prose."""
    output = stdout if result.exit_code is ProfileExitCode.SUCCESS else stderr
    print(result.message, file=output)
    print(f"reason: {result.reason_code.value}", file=output)
    if result.exit_code is not ProfileExitCode.SUCCESS:
        return
    if result.profiles or result.reason_code.value == "profiles_listed":
        for profile_id in result.profiles:
            print(f"profile: {profile_id}", file=output)
        print(f"skipped_entries: {result.skipped_entries}", file=output)
        print(
            f"entry_limit_reached: {'yes' if result.entry_limit_reached else 'no'}",
            file=output,
        )
    if result.profile_id is not None:
        print(f"profile: {result.profile_id}", file=output)
    if result.plan is not None:
        print(
            f"byte_identical: {'yes' if result.plan.byte_identical else 'no'}",
            file=output,
        )
        print(f"active_sha256: {result.plan.active_sha256}", file=output)
        print(f"candidate_sha256: {result.plan.candidate_sha256}", file=output)
        for change in result.plan.changes:
            print(f"change: {change.change_type.value} {change.path}", file=output)
        print(
            f"changed_paths_truncated: {'yes' if result.plan.truncated else 'no'}",
            file=output,
        )
    if result.backups or result.message == "Configuration backups listed.":
        for record in result.backups:
            print(f"backup: {record.backup_id}", file=output)
            print(f"  created_at_utc: {record.created_at_utc}", file=output)
            print(f"  source_sha256: {record.source_sha256}", file=output)
            print(f"  source_byte_count: {record.source_byte_count}", file=output)
            print(f"  operation: {record.operation.value}", file=output)
            print(
                f"  target_profile_id: {record.target_profile_id or 'none'}",
                file=output,
            )
            print(
                f"  target_backup_id: {record.target_backup_id or 'none'}",
                file=output,
            )
            print(
                f"  integrity: {'valid' if record.integrity_valid else 'invalid'}",
                file=output,
            )
        print(f"skipped_entries: {result.skipped_entries}", file=output)
        print(
            f"entry_limit_reached: {'yes' if result.entry_limit_reached else 'no'}",
            file=output,
        )
    if result.backup_id is not None:
        print(f"backup: {result.backup_id}", file=output)
