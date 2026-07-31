"""Dashboard-only settings loaded from CLI arguments or environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DashboardSettings:
    """Runtime settings for the local read-only dashboard."""

    bind_host: str = "0.0.0.0"
    port: int = 8080
    refresh_seconds: int = 5
    request_timeout_seconds: float = 2.0
    wled_host: str = ""
    hyperhdr_host: str = "127.0.0.1"
    hyperhdr_port: int = 8090
    capture_device: str = "/dev/video0"
    expected_led_count: int = 282
    expected_active_leds: int = 266
    expected_skipped_leds: int = 16

    @classmethod
    def from_environment(cls) -> DashboardSettings:
        """Load settings without reading or committing a local config file."""
        return cls(
            bind_host=os.getenv("AURORA_DASHBOARD_BIND_HOST", "0.0.0.0"),
            port=int(os.getenv("AURORA_DASHBOARD_PORT", "8080")),
            refresh_seconds=int(os.getenv("AURORA_DASHBOARD_REFRESH_SECONDS", "5")),
            request_timeout_seconds=float(
                os.getenv("AURORA_DASHBOARD_TIMEOUT_SECONDS", "2.0")
            ),
            wled_host=os.getenv("AURORA_WLED_HOST", ""),
            hyperhdr_host=os.getenv("AURORA_HYPERHDR_HOST", "127.0.0.1"),
            hyperhdr_port=int(os.getenv("AURORA_HYPERHDR_PORT", "8090")),
            capture_device=os.getenv("AURORA_CAPTURE_DEVICE", "/dev/video0"),
            expected_led_count=int(os.getenv("AURORA_EXPECTED_LED_COUNT", "282")),
            expected_active_leds=int(os.getenv("AURORA_EXPECTED_ACTIVE_LEDS", "266")),
            expected_skipped_leds=int(os.getenv("AURORA_EXPECTED_SKIPPED_LEDS", "16")),
        )
