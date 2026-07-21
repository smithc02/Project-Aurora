"""Bounded Linux V4L2 presence probe; it never opens a video device."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Protocol

from aurora_core.hardware.models import CaptureDeviceProbeResult

_VIDEO_NODE = re.compile(r"/dev/video[0-9]+$")
_SYSFS_ROOT = Path("/sys/class/video4linux")


class CaptureDeviceProbe(Protocol):
    def probe(self, *, identifier: str) -> CaptureDeviceProbeResult: ...


class LinuxCaptureDeviceProbe:
    """Inspect one configured identifier and the one fixed sysfs attribute."""

    def probe(self, *, identifier: str) -> CaptureDeviceProbeResult:
        try:
            os.lstat(identifier)
        except FileNotFoundError:
            return CaptureDeviceProbeResult("device_not_found")
        except OSError:
            return CaptureDeviceProbeResult("probe_failed")
        try:
            resolved = str(Path(identifier).resolve(strict=True))
        except (OSError, RuntimeError):
            return CaptureDeviceProbeResult("symlink_resolution_failed")
        if _VIDEO_NODE.fullmatch(resolved) is None:
            return CaptureDeviceProbeResult("invalid_device_target", True)
        try:
            target_stat = os.stat(resolved)
        except FileNotFoundError:
            return CaptureDeviceProbeResult("device_not_found")
        except OSError:
            return CaptureDeviceProbeResult("probe_failed")
        if not stat.S_ISCHR(target_stat.st_mode):
            return CaptureDeviceProbeResult("not_character_device", True)
        class_entry = _SYSFS_ROOT / Path(resolved).name
        try:
            os.stat(class_entry)
        except FileNotFoundError:
            return CaptureDeviceProbeResult("v4l2_registration_missing", True, True)
        except OSError:
            return CaptureDeviceProbeResult("metadata_unavailable", True, True)
        name = self._read_name(class_entry / "name")
        if name is None:
            return CaptureDeviceProbeResult("invalid_device_name", True, True, True)
        if not os.access(resolved, os.R_OK):
            return CaptureDeviceProbeResult(
                "permission_denied", True, True, True, False, name
            )
        return CaptureDeviceProbeResult("validated", True, True, True, True, name)

    @staticmethod
    def _read_name(path: Path) -> str | None:
        try:
            with path.open("rb") as attribute:
                raw = attribute.read(256)
        except OSError:
            return None
        if len(raw) == 256:
            return None
        try:
            name = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
        if name.endswith("\r\n"):
            name = name[:-2]
        elif name.endswith("\n") or name.endswith("\r"):
            name = name[:-1]
        if (
            not name
            or len(name) > 128
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
        ):
            return None
        return name
