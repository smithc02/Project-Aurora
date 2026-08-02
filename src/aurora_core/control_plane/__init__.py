"""Protected control plane with explicitly bounded WLED operations."""

from aurora_core.control_plane.contracts import CONTROL_CAPABILITIES
from aurora_core.control_plane.service import ControlPlaneService

__all__ = ["CONTROL_CAPABILITIES", "ControlPlaneService"]
