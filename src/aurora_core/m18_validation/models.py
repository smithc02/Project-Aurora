"""Bounded, serialization-safe report models for validation tools."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum

type Scalar = str | int | float | bool | None


class CheckStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"


class MeasurementKind(StrEnum):
    MEASURED = "measured"
    PROJECTED = "projected"
    UNAVAILABLE = "unavailable"
    ARCHITECTURE_LIMIT = "architecture_limit"
    DECISION_PENDING = "decision_pending"


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    status: CheckStatus
    summary: str
    required: bool = True

    def to_dict(self) -> dict[str, Scalar]:
        return {
            "name": self.name,
            "status": self.status.value,
            "summary": self.summary,
            "required": self.required,
        }


@dataclass(frozen=True, slots=True)
class Measurement:
    name: str
    kind: MeasurementKind
    value: Scalar
    unit: str | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, Scalar]:
        return {
            "name": self.name,
            "kind": self.kind.value,
            "value": self.value,
            "unit": self.unit,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class ToolReport:
    report_schema: str
    checks: tuple[CheckResult, ...]
    measurements: tuple[Measurement, ...] = ()
    root: str = "<isolated-test-root>"

    @property
    def passed(self) -> bool:
        return not any(
            check.required and check.status is CheckStatus.FAIL for check in self.checks
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "report_schema": self.report_schema,
            "result": CheckStatus.PASS.value if self.passed else CheckStatus.FAIL.value,
            "test_root": self.root,
            "checks": [check.to_dict() for check in self.checks],
            "measurements": [item.to_dict() for item in self.measurements],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def render_human(self) -> str:
        lines = [
            f"{self.report_schema}: "
            f"{CheckStatus.PASS.value if self.passed else CheckStatus.FAIL.value}"
        ]
        lines.extend(
            f"[{check.status.value}] {check.name}: {check.summary}"
            for check in self.checks
        )
        lines.extend(
            f"[{item.kind.value}] {item.name}: "
            f"{item.value if item.value is not None else 'unavailable'}"
            f"{'' if item.unit is None else f' {item.unit}'}"
            for item in self.measurements
        )
        return "\n".join(lines)
