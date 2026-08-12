"""Tests for the shared Milestone 18 SQLite runtime capability gate."""

from __future__ import annotations

import ast
import socket
import time
from pathlib import Path

import pytest

import aurora_core.health_history.sqlite_runtime as sqlite_runtime
from aurora_core.health_history import (
    MINIMUM_SAFE_SQLITE_VERSION,
    SQLiteRuntimeError,
    SQLiteRuntimeRejection,
    require_safe_sqlite_runtime,
)


@pytest.mark.parametrize("version", [(3, 51, 3), (3, 51, 4), (3, 53, 1), (4, 0, 0)])
def test_safe_sqlite_runtime_versions_are_accepted(
    monkeypatch: pytest.MonkeyPatch, version: tuple[int, int, int]
) -> None:
    monkeypatch.setattr(sqlite_runtime.sqlite3, "sqlite_version_info", version)
    require_safe_sqlite_runtime()


@pytest.mark.parametrize(
    "version",
    [
        (3, 51, 2),
        (3, 51, 0),
        (3, 50, 99),
        (2, 99, 99),
        None,
        [3, 51, 3],
        (3, 51),
        (3, 51, 3, 0),
        (True, 51, 3),
        (3, "51", 3),
        (3, 51, 3.0),
        (-1, 51, 3),
        (3, sqlite_runtime._MAX_SQLITE_VERSION_COMPONENT + 1, 3),
    ],
)
def test_unsafe_or_malformed_sqlite_runtime_is_sanitized(
    monkeypatch: pytest.MonkeyPatch, version: object
) -> None:
    monkeypatch.setattr(sqlite_runtime.sqlite3, "sqlite_version_info", version)
    with pytest.raises(SQLiteRuntimeError) as caught:
        require_safe_sqlite_runtime()
    assert caught.value.reason is SQLiteRuntimeRejection.UNSUPPORTED_RUNTIME
    assert str(caught.value) == "unsupported_runtime"
    assert repr(version) not in str(caught.value)


def test_missing_sqlite_runtime_metadata_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(sqlite_runtime.sqlite3, "sqlite_version_info")
    with pytest.raises(SQLiteRuntimeError) as caught:
        require_safe_sqlite_runtime()
    assert caught.value.reason is SQLiteRuntimeRejection.UNSUPPORTED_RUNTIME
    assert str(caught.value) == "unsupported_runtime"


def test_sqlite_runtime_gate_rechecks_metadata_without_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sqlite_runtime.sqlite3, "sqlite_version_info", (3, 53, 1))
    require_safe_sqlite_runtime()
    monkeypatch.setattr(sqlite_runtime.sqlite3, "sqlite_version_info", (3, 51, 2))
    with pytest.raises(SQLiteRuntimeError):
        require_safe_sqlite_runtime()


def test_sqlite_runtime_gate_performs_no_external_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def prohibited(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("external operation is prohibited")

    monkeypatch.setattr(sqlite_runtime.sqlite3, "sqlite_version_info", (3, 53, 1))
    monkeypatch.setattr(sqlite_runtime.sqlite3, "connect", prohibited)
    monkeypatch.setattr(Path, "open", prohibited)
    monkeypatch.setattr(Path, "stat", prohibited)
    monkeypatch.setattr(time, "sleep", prohibited)
    monkeypatch.setattr(socket, "create_connection", prohibited)
    require_safe_sqlite_runtime()


def test_sqlite_runtime_public_contract_has_one_authoritative_floor() -> None:
    assert MINIMUM_SAFE_SQLITE_VERSION == (3, 51, 3)
    assert MINIMUM_SAFE_SQLITE_VERSION is sqlite_runtime.MINIMUM_SAFE_SQLITE_VERSION


def test_sqlite_runtime_module_has_only_standard_library_dependencies() -> None:
    source_path = Path(sqlite_runtime.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
    assert imported == {"__future__", "enum", "sqlite3", "typing"}
