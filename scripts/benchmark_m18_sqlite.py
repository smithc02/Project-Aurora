#!/usr/bin/env python3
"""Run the disposable Milestone 18 SQLite endurance benchmark."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from aurora_core.m18_validation.benchmark import (
    BenchmarkConfig,
    BenchmarkScenario,
    run_benchmark,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Benchmark a synthetic, non-production Milestone 18 schema."
    )
    parser.add_argument("--test-dir", type=Path)
    parser.add_argument("--transactions", type=int, default=240)
    parser.add_argument("--seed", type=int, default=18)
    parser.add_argument(
        "--scenario",
        choices=tuple(item.value for item in BenchmarkScenario),
        default=BenchmarkScenario.TRANSITION_HEAVY.value,
    )
    parser.add_argument(
        "--pace-milliseconds",
        type=float,
        default=0.0,
        help="0 runs accelerated; nonzero values are bounded to 30000 ms",
    )
    parser.add_argument("--checkpoint-interval", type=int, default=60)
    parser.add_argument("--cleanup-interval", type=int, default=120)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        scenario = BenchmarkScenario(args.scenario)
    except ValueError:
        return 2
    config = BenchmarkConfig(
        transactions=args.transactions,
        seed=args.seed,
        scenario=scenario,
        pace_seconds=args.pace_milliseconds / 1000,
        checkpoint_interval=args.checkpoint_interval,
        cleanup_interval=args.cleanup_interval,
    )
    if args.test_dir is not None:
        report = run_benchmark(args.test_dir, config)
        print(report.render_human(), file=sys.stderr)
        print(report.to_json())
        return 0 if report.passed else 1
    with tempfile.TemporaryDirectory(prefix="aurora-m18-benchmark-") as raw:
        root = Path(raw).resolve()
        os.chmod(root, 0o700)
        report = run_benchmark(root, config)
        print(report.render_human(), file=sys.stderr)
        print(report.to_json())
        return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
