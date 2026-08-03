"""Sanitized raw-YAML change planning without rendering configuration values."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from aurora_core.config_profiles.models import (
    MAX_PLAN_CHANGES,
    ChangeType,
    PlanChange,
)


def plan_changes(
    active: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    maximum_changes: int = MAX_PLAN_CHANGES,
) -> tuple[tuple[PlanChange, ...], bool]:
    """Return sorted value-free changes and whether the result was truncated."""
    changes: list[PlanChange] = []
    truncated = False

    def add(path: str, change_type: ChangeType) -> None:
        nonlocal truncated
        if len(changes) >= maximum_changes:
            truncated = True
            return
        changes.append(PlanChange(path, change_type))

    def walk(left: Any, right: Any, path: str) -> None:
        if isinstance(left, Mapping) and isinstance(right, Mapping):
            left_keys = set(left)
            right_keys = set(right)
            for key in sorted(left_keys | right_keys, key=str):
                child = f"{path}.{key}" if path else str(key)
                if key not in left:
                    add(child, ChangeType.ADDED)
                elif key not in right:
                    add(child, ChangeType.REMOVED)
                else:
                    walk(left[key], right[key], child)
            return
        if _is_sequence(left) and _is_sequence(right):
            common = min(len(left), len(right))
            for index in range(common):
                walk(left[index], right[index], f"{path}[{index}]")
            for index in range(common, len(left)):
                add(f"{path}[{index}]", ChangeType.REMOVED)
            for index in range(common, len(right)):
                add(f"{path}[{index}]", ChangeType.ADDED)
            return
        if type(left) is not type(right) or left != right:
            add(path, ChangeType.CHANGED)

    walk(active, candidate, "")
    return tuple(sorted(changes, key=lambda item: item.path)), truncated


def _is_sequence(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    )
