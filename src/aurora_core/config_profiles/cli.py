"""Narrow argparse registration and dispatch for local profile operations."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from aurora_core.config_profiles.models import DEFAULT_MAXIMUM_BACKUPS
from aurora_core.config_profiles.rendering import render_profile_result
from aurora_core.config_profiles.service import ConfigurationProfileService


def add_profile_parsers(
    config_subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register the exact Milestone 17 CLI command family."""
    profile_parser = config_subparsers.add_parser(
        "profile", help="Manage protected local YAML configuration profiles."
    )
    subparsers = profile_parser.add_subparsers(dest="profile_command")

    list_parser = subparsers.add_parser("list", help="List secure profile identifiers.")
    list_parser.add_argument("--profiles-dir", type=Path, required=True)

    validate_parser = subparsers.add_parser(
        "validate", help="Validate one complete profile."
    )
    validate_parser.add_argument("--profiles-dir", type=Path, required=True)
    validate_parser.add_argument("--profile", required=True)

    plan_parser = subparsers.add_parser(
        "plan", help="Plan sanitized YAML-layer changes."
    )
    plan_parser.add_argument("--config", type=Path, required=True)
    plan_parser.add_argument("--profiles-dir", type=Path, required=True)
    plan_parser.add_argument("--profile", required=True)

    apply_parser = subparsers.add_parser(
        "apply", help="Back up and atomically activate one profile."
    )
    apply_parser.add_argument("--config", type=Path, required=True)
    apply_parser.add_argument("--profiles-dir", type=Path, required=True)
    apply_parser.add_argument("--backups-dir", type=Path, required=True)
    apply_parser.add_argument("--profile", required=True)
    apply_parser.add_argument("--confirm-apply", required=True)
    apply_parser.add_argument(
        "--maximum-backups", type=int, default=DEFAULT_MAXIMUM_BACKUPS
    )

    backups_parser = subparsers.add_parser(
        "backups", help="List managed backup records and integrity."
    )
    backups_parser.add_argument("--backups-dir", type=Path, required=True)

    rollback_parser = subparsers.add_parser(
        "rollback", help="Atomically activate one selected managed backup."
    )
    rollback_parser.add_argument("--config", type=Path, required=True)
    rollback_parser.add_argument("--backups-dir", type=Path, required=True)
    rollback_parser.add_argument("--backup-id", required=True)
    rollback_parser.add_argument("--confirm-rollback", required=True)
    rollback_parser.add_argument(
        "--maximum-backups", type=int, default=DEFAULT_MAXIMUM_BACKUPS
    )


def dispatch_profile_command(
    args: argparse.Namespace, *, service: ConfigurationProfileService | None = None
) -> int:
    """Execute one already-parsed profile command and render its typed result."""
    service = service or ConfigurationProfileService()
    if args.profile_command == "list":
        result = service.list_profiles(args.profiles_dir)
    elif args.profile_command == "validate":
        result = service.validate_profile(args.profiles_dir, args.profile)
    elif args.profile_command == "plan":
        result = service.plan(args.config, args.profiles_dir, args.profile)
    elif args.profile_command == "apply":
        result = service.apply(
            args.config,
            args.profiles_dir,
            args.backups_dir,
            args.profile,
            args.confirm_apply,
            maximum_backups=args.maximum_backups,
        )
    elif args.profile_command == "backups":
        result = service.list_backups(args.backups_dir)
    elif args.profile_command == "rollback":
        result = service.rollback(
            args.config,
            args.backups_dir,
            args.backup_id,
            args.confirm_rollback,
            maximum_backups=args.maximum_backups,
        )
    else:
        return 2
    render_profile_result(result, stdout=sys.stdout, stderr=sys.stderr)
    return int(result.exit_code)
