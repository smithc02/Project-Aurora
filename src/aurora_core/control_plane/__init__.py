"""Fail-closed authentication foundation with no registered device controls."""

from aurora_core.control_plane.contracts import CONTROL_CAPABILITIES
from aurora_core.control_plane.service import ControlPlaneService

__all__ = ["CONTROL_CAPABILITIES", "ControlPlaneService"]
