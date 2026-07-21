"""Command-line entry point for the Project Aurora scaffold."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from aurora_core import __version__
from aurora_core.config import AuroraConfigurationError, load_settings

APPLICATION_NAME = "Project Aurora"


def build_parser() -> argparse.ArgumentParser:
    """Create the hardware-free Aurora command-line parser."""
    parser = argparse.ArgumentParser(description="Project Aurora scaffold")
    parser.add_argument(
        "--version", action="store_true", help="Print the version and exit."
    )
    parser.add_argument(
        "--check", action="store_true", help="Validate that the package can start."
    )
    subparsers = parser.add_subparsers(dest="command")
    config_parser = subparsers.add_parser("config", help="Configuration commands.")
    config_subparsers = config_parser.add_subparsers(dest="config_command")
    validate_parser = config_subparsers.add_parser(
        "validate", help="Load and validate configuration without connectivity checks."
    )
    validate_parser.add_argument("--config", type=Path, help="Path to a YAML file.")
    validate_parser.add_argument("--log-level", help="Override logging.level.")
    return parser


def main() -> int:
    """Run the minimal command-line interface without hardware interaction."""
    args = build_parser().parse_args()
    if args.version:
        print(f"{APPLICATION_NAME} {__version__}")
        return 0
    if args.check:
        print(f"{APPLICATION_NAME} {__version__}: package startup check passed")
        return 0
    if args.command == "config" and args.config_command == "validate":
        overrides = (
            {"logging": {"level": args.log_level}}
            if args.log_level is not None
            else None
        )
        try:
            load_settings(config_path=args.config, cli_overrides=overrides)
        except AuroraConfigurationError as error:
            print(f"Configuration validation failed: {error}", file=sys.stderr)
            return 1
        print("Configuration is valid (connectivity was not tested).")
        return 0
    print(f"{APPLICATION_NAME} {__version__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
