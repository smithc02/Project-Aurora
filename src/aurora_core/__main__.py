"""Command-line entry point for the Project Aurora scaffold."""

from __future__ import annotations

import argparse

from aurora_core import __version__

APPLICATION_NAME = "Project Aurora"


def build_parser() -> argparse.ArgumentParser:
    """Create the scaffold command-line parser."""
    parser = argparse.ArgumentParser(description="Project Aurora scaffold")
    parser.add_argument(
        "--version", action="store_true", help="Print the version and exit."
    )
    parser.add_argument(
        "--check", action="store_true", help="Validate that the package can start."
    )
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
    print(f"{APPLICATION_NAME} {__version__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
