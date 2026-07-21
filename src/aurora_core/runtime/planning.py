"""Conversion of validated configuration snapshots into safe runtime plans."""

from __future__ import annotations

from aurora_core.config.models import AuroraSettings
from aurora_core.runtime.models import (
    COMPONENT_ORDER,
    ComponentHealthState,
    ComponentId,
    ComponentLifecycleState,
    ComponentPlan,
    RuntimePlan,
)


def build_runtime_plan(settings: AuroraSettings) -> RuntimePlan:
    """Build a deterministic, secret-free plan without I/O."""
    enabled_by_id = {
        ComponentId.CAPTURE_DEVICE: settings.capture_device.enabled,
        ComponentId.HYPERHDR: settings.hyperhdr.enabled,
        ComponentId.WLED: settings.wled.enabled,
        ComponentId.DDP: settings.ddp.enabled,
        ComponentId.MQTT: settings.mqtt.enabled,
    }
    components = tuple(
        ComponentPlan(
            component_id=component_id,
            enabled=enabled_by_id[component_id],
            configured=enabled_by_id[component_id],
            initial_lifecycle=ComponentLifecycleState.CREATED,
            initial_health=(
                ComponentHealthState.UNKNOWN
                if enabled_by_id[component_id]
                else ComponentHealthState.DISABLED
            ),
        )
        for component_id in COMPONENT_ORDER
    )
    return RuntimePlan(
        components=components,
        lighting_zone_count=sum(zone.enabled for zone in settings.lighting_zones),
        led_layout_configured=(
            settings.led_layout.orientation is not None
            or settings.led_layout.starting_corner is not None
        ),
    )
