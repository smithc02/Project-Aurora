"""Synthetic accounting and recovery tests for the endurance harness."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from aurora_core.m18_validation.benchmark import (
    BENCHMARK_APPLICATION_ID,
    RESERVED_PRODUCTION_APPLICATION_ID,
    BenchmarkConfig,
    BenchmarkScenario,
    run_benchmark,
)
from aurora_core.m18_validation.models import CheckStatus, MeasurementKind


def _protected(path: Path) -> Path:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _measurement(report, name: str):
    return next(item for item in report.measurements if item.name == name)


@pytest.mark.parametrize("scenario", tuple(BenchmarkScenario))
def test_every_synthetic_scenario_commits_bounded_transactions(
    tmp_path: Path, scenario: BenchmarkScenario
) -> None:
    root = _protected(tmp_path / scenario.value)
    report = run_benchmark(
        root,
        BenchmarkConfig(
            transactions=16,
            scenario=scenario,
            checkpoint_interval=8,
            cleanup_interval=8,
        ),
    )
    assert report.passed
    assert _measurement(report, "committed_sample_transactions").value == 16
    assert _measurement(report, "transactions_per_hour_at_30_seconds").value == 120
    assert _measurement(report, "transactions_per_day_at_30_seconds").value == 2880
    assert _measurement(report, "checkpoint_count").value == 3
    assert _measurement(report, "target_platform_acceptance").kind is (
        MeasurementKind.DECISION_PENDING
    )


def test_benchmark_measures_cleanup_accounting_and_file_growth(tmp_path: Path) -> None:
    root = _protected(tmp_path / "benchmark")
    report = run_benchmark(
        root,
        BenchmarkConfig(
            transactions=80,
            scenario=BenchmarkScenario.TRANSITION_HEAVY,
            checkpoint_interval=40,
            cleanup_interval=10_000,
        ),
    )
    assert report.passed
    removed = _measurement(report, "cleanup_rows_removed")
    assert isinstance(removed.value, int) and 0 < removed.value <= 500
    growth = _measurement(report, "total_managed_file_growth")
    assert isinstance(growth.value, int) and growth.value > 0
    assert _measurement(report, "history_rows_inserted").value == 80
    database = root / "m18-benchmark-transition-heavy.sqlite3"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM history").fetchone() == (64,)


def test_healthy_sequence_compacts_to_transition_and_periodic_heartbeat(
    tmp_path: Path,
) -> None:
    root = _protected(tmp_path / "heartbeat")
    report = run_benchmark(
        root,
        BenchmarkConfig(
            transactions=31,
            scenario=BenchmarkScenario.HEALTHY,
            checkpoint_interval=31,
            cleanup_interval=31,
        ),
    )
    assert report.passed
    database = root / "m18-benchmark-healthy.sqlite3"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT sequence, kind FROM history ORDER BY sequence"
        ).fetchall() == [(1, "transition"), (30, "heartbeat")]


def test_benchmark_identity_restart_crash_and_integrity_checks_pass(
    tmp_path: Path,
) -> None:
    root = _protected(tmp_path / "benchmark")
    report = run_benchmark(
        root,
        BenchmarkConfig(transactions=8, checkpoint_interval=4, cleanup_interval=4),
    )
    checks = {check.name: check.status for check in report.checks}
    assert checks["clean_restart"] is CheckStatus.PASS
    assert checks["abrupt_termination_recovery"] is CheckStatus.PASS
    assert checks["schema_and_application_identity"] is CheckStatus.PASS
    assert checks["integrity"] is CheckStatus.PASS
    assert BENCHMARK_APPLICATION_ID != RESERVED_PRODUCTION_APPLICATION_ID
    with sqlite3.connect(root / "m18-benchmark-transition-heavy.sqlite3") as connection:
        assert connection.execute("PRAGMA application_id").fetchone() == (
            BENCHMARK_APPLICATION_ID,
        )


def test_benchmark_refuses_invalid_configuration_and_existing_artifact(
    tmp_path: Path,
) -> None:
    invalid_root = _protected(tmp_path / "invalid")
    invalid = run_benchmark(invalid_root, BenchmarkConfig(transactions=0))
    assert not invalid.passed
    assert not tuple(invalid_root.iterdir())

    root = _protected(tmp_path / "collision")
    config = BenchmarkConfig(transactions=4, checkpoint_interval=2, cleanup_interval=2)
    assert run_benchmark(root, config).passed
    repeated = run_benchmark(root, config)
    assert not repeated.passed
    assert repeated.checks[0].name == "benchmark_database_creation"


def test_benchmark_report_redacts_root_and_distinguishes_measurement_kinds(
    tmp_path: Path,
) -> None:
    root = _protected(tmp_path / "redacted")
    report = run_benchmark(
        root,
        BenchmarkConfig(transactions=4, checkpoint_interval=2, cleanup_interval=2),
    )
    payload = report.to_json()
    assert str(root) not in payload
    kinds = {item.kind for item in report.measurements}
    assert MeasurementKind.MEASURED in kinds
    assert MeasurementKind.PROJECTED in kinds
    assert MeasurementKind.ARCHITECTURE_LIMIT in kinds
    assert MeasurementKind.DECISION_PENDING in kinds
    if _measurement(report, "process_write_bytes").value is None:
        assert MeasurementKind.UNAVAILABLE in kinds
