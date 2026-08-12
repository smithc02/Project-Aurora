"""Shared SQLite runtime capability boundary for health-history storage."""

from __future__ import annotations

import sqlite3
from enum import StrEnum
from typing import Final, cast

MINIMUM_SAFE_SQLITE_VERSION: Final = (3, 51, 3)
_MAX_SQLITE_VERSION_COMPONENT: Final = 2**31 - 1


class SQLiteRuntimeRejection(StrEnum):
    """Fixed sanitized SQLite runtime failure registry."""

    UNSUPPORTED_RUNTIME = "unsupported_runtime"


class SQLiteRuntimeError(Exception):
    """Sanitized SQLite runtime capability failure."""

    def __init__(self, reason: SQLiteRuntimeRejection) -> None:
        super().__init__(reason.value)
        self.reason = reason


def require_safe_sqlite_runtime() -> None:
    """Require the reviewed SQLite safety floor without opening SQLite."""
    version: object = getattr(sqlite3, "sqlite_version_info", None)
    if type(version) is not tuple or len(version) != 3:
        raise SQLiteRuntimeError(SQLiteRuntimeRejection.UNSUPPORTED_RUNTIME)
    components = cast(tuple[object, object, object], version)
    if any(
        type(component) is not int
        or not 0 <= component <= _MAX_SQLITE_VERSION_COMPONENT
        for component in components
    ):
        raise SQLiteRuntimeError(SQLiteRuntimeRejection.UNSUPPORTED_RUNTIME)
    safe_version = cast(tuple[int, int, int], components)
    if safe_version < MINIMUM_SAFE_SQLITE_VERSION:
        raise SQLiteRuntimeError(SQLiteRuntimeRejection.UNSUPPORTED_RUNTIME)
