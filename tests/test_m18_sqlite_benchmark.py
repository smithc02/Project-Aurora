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
    _connect_existing_database,
    _Counters,
    _crash_child,
    _identity_matches,
    _quick_check,
    _storage_measurements,
    _validate_database,
    run_benchmark,
)
from aurora_core.m18_validation.filesystem import FilesystemBoundaryError
from aurora_core.m18_validation.models import (
    CheckResult,
    CheckStatus,
    MeasurementKind,
    ToolReport,
)


def _protected(path: Path) -> Path:
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    return path


def _measurement(report, name: str):
    return next(item for item in report.measurements if item.name == name)


def _read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)


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
    setup = _measurement(report, "setup_total_managed_bytes").value
    peak = _measurement(report, "peak_total_managed_bytes").value
    final = _measurement(report, "final_total_managed_bytes").value
    growth = _measurement(report, "workload_managed_file_growth_bytes").value
    final_delta = _measurement(report, "workload_final_managed_file_delta_bytes").value
    assert isinstance(setup, int)
    assert isinstance(peak, int)
    assert isinstance(final, int)
    assert isinstance(growth, int)
    assert isinstance(final_delta, int)
    assert growth == peak - setup
    assert final_delta == final - setup
    assert _measurement(report, "history_rows_inserted").value == 80
    database = root / "m18-benchmark-transition-heavy.sqlite3"
    with _read_only(database) as connection:
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
    with _read_only(database) as connection:
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
    with _read_only(root / "m18-benchmark-transition-heavy.sqlite3") as connection:
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
    assert all(
        check.status is not CheckStatus.SKIPPED or not check.required
        for check in report.checks
    )


def test_existing_database_helpers_never_create_missing_paths(tmp_path: Path) -> None:
    root = _protected(tmp_path / "missing")
    missing = root / "expected.sqlite3"

    with pytest.raises(FilesystemBoundaryError):
        _connect_existing_database(missing)
    assert not missing.exists()
    assert not _validate_database(missing)
    assert not missing.exists()
    assert not _identity_matches(missing)
    assert not missing.exists()
    assert not _quick_check(missing)
    assert not missing.exists()


def test_crash_child_open_cannot_create_missing_database(tmp_path: Path) -> None:
    root = _protected(tmp_path / "crash")
    missing = root / "crash.sqlite3"

    with pytest.raises(FilesystemBoundaryError):
        _crash_child(missing)
    assert not missing.exists()


def test_existing_database_helpers_reject_unexpected_objects(tmp_path: Path) -> None:
    root = _protected(tmp_path / "object")
    unexpected = root / "database.sqlite3"
    unexpected.mkdir(mode=0o700)

    with pytest.raises(FilesystemBoundaryError):
        _connect_existing_database(unexpected)
    assert not _validate_database(unexpected)
    assert not _identity_matches(unexpected)
    assert not _quick_check(unexpected)
    assert unexpected.is_dir()


def test_storage_accounting_uses_post_schema_baseline_and_exact_names() -> None:
    setup = {"main": 100, "wal": 200, "shm": 40}
    counters = _Counters(
        peak_main_bytes=180,
        peak_wal_bytes=260,
        peak_shm_bytes=50,
        peak_total_bytes=430,
    )
    final = {"main": 160, "wal": 0, "shm": 0}

    measurements = _storage_measurements(setup, counters, final)
    values = {measurement.name: measurement.value for measurement in measurements}
    assert values == {
        "setup_main_database_bytes": 100,
        "setup_wal_bytes": 200,
        "setup_shared_memory_bytes": 40,
        "setup_total_managed_bytes": 340,
        "peak_main_database_bytes": 180,
        "peak_wal_bytes": 260,
        "peak_shared_memory_bytes": 50,
        "peak_total_managed_bytes": 430,
        "final_main_database_bytes": 160,
        "final_wal_bytes": 0,
        "final_shared_memory_bytes": 0,
        "final_total_managed_bytes": 160,
        "workload_managed_file_growth_bytes": 90,
        "workload_final_managed_file_delta_bytes": -180,
    }
    assert all(
        measurement.kind is MeasurementKind.MEASURED for measurement in measurements
    )

    payload = ToolReport(
        report_schema="storage-test",
        checks=(CheckResult("storage", CheckStatus.PASS, "fixed synthetic input"),),
        measurements=measurements,
    ).to_dict()
    serialized = payload["measurements"]
    assert isinstance(serialized, list)
    assert [item["name"] for item in serialized] == list(values)
    assert {item["kind"] for item in serialized} == {"measured"}
