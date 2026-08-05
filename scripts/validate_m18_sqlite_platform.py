#!/usr/bin/env python3
"""Run isolated Milestone 18 target-platform SQLite validation."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from aurora_core.m18_validation.platform import run_platform_validation


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate SQLite gates in an isolated synthetic directory."
    )
    parser.add_argument(
        "--test-dir",
        type=Path,
        help=(
            "existing operator-owned mode-0700 directory; omitted uses a secure "
            "temporary directory"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.test_dir is not None:
        report = run_platform_validation(args.test_dir)
        print(report.render_human(), file=sys.stderr)
        print(report.to_json())
        return 0 if report.passed else 1
    with tempfile.TemporaryDirectory(prefix="aurora-m18-platform-") as raw:
        root = Path(raw).resolve()
        os.chmod(root, 0o700)
        report = run_platform_validation(root)
        print(report.render_human(), file=sys.stderr)
        print(report.to_json())
        return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
