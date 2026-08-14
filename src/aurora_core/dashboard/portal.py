"""HTML rendering for the dependency-free, read-only Aurora portal."""

from __future__ import annotations

import html
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from aurora_core.config_profiles.identifiers import is_profile_id
from aurora_core.dashboard.models import ComponentHealth, HealthReport, HealthStatus


@dataclass(frozen=True, slots=True)
class PortalPage:
    """One native portal page exposed by the local server."""

    path: str
    navigation_label: str
    title: str
    description: str


PORTAL_PAGES = (
    PortalPage(
        "/",
        "Overview",
        "Overview",
        "A single read-only view of the current Project Aurora installation.",
    ),
    PortalPage(
        "/wled",
        "WLED",
        "WLED status",
        "Sanitized state from the bounded WLED health observations.",
    ),
    PortalPage(
        "/hyperhdr",
        "HyperHDR",
        "HyperHDR status",
        "Sanitized instance, grabber, and LED-output observations.",
    ),
    PortalPage(
        "/capture",
        "Capture",
        "Capture-device status",
        "Non-opening device metadata and HyperHDR-reported grabber activity.",
    ),
    PortalPage(
        "/system",
        "System",
        "Raspberry Pi health",
        "Bounded host resource observations and dashboard service uptime.",
    ),
    PortalPage(
        "/room-map",
        "Room Map",
        "Room Map",
        "A preview of the future virtual room and lighting-zone model.",
    ),
    PortalPage(
        "/spatial-intelligence",
        "Spatial Intelligence",
        "Spatial Intelligence",
        "A preview of the future bounded spatial-event architecture.",
    ),
)

PORTAL_PAGE_BY_PATH = {page.path: page for page in PORTAL_PAGES}
PORTAL_PATHS = frozenset(PORTAL_PAGE_BY_PATH)


class ControlNavigationLink(StrEnum):
    """Server-selected control-plane navigation state."""

    LOGIN = "login"
    CONTROLS = "controls"


class _ReportedAmbientPath(StrEnum):
    ACTIVE = "Active"
    INACTIVE = "Inactive"
    UNAVAILABLE = "Unavailable"


ValueFormatter = Callable[[object], str]
MetricSpec = tuple[str, str, ValueFormatter]

_AMBIENT_ACTIVITY_FIELDS = (
    ("wled", "output_on"),
    ("hyperhdr", "instance_running"),
    ("hyperhdr", "grabber_active"),
    ("hyperhdr", "led_output_active"),
    ("capture", "device_node_present"),
)


def _plain(value: object) -> str:
    return str(value)


def _yes_no(value: object) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return _plain(value)


def _active_inactive(value: object) -> str:
    if isinstance(value, bool):
        return "Active" if value else "Inactive"
    return _plain(value)


def _running_stopped(value: object) -> str:
    if isinstance(value, bool):
        return "Running" if value else "Not running"
    return _plain(value)


def _on_off(value: object) -> str:
    if isinstance(value, bool):
        return "On" if value else "Off"
    return _plain(value)


def _match(value: object) -> str:
    if isinstance(value, bool):
        return "Match" if value else "Mismatch"
    return _plain(value)


def _number(value: object) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:g}"
    return _plain(value)


def _seconds(value: object) -> str:
    return f"{_number(value)} s"


def _milliseconds(value: object) -> str:
    return f"{_number(value)} ms"


def _percent(value: object) -> str:
    return f"{_number(value)}%"


def _temperature(value: object) -> str:
    return f"{_number(value)} °C"


def _milliamps(value: object) -> str:
    return f"{_number(value)} mA"


def _brightness(value: object) -> str:
    return f"{_number(value)} / 255"


WLED_METRICS: tuple[MetricSpec, ...] = (
    ("output_on", "Current output", _on_off),
    ("brightness", "Brightness", _brightness),
    ("firmware_version", "Firmware version", _plain),
    ("uptime_seconds", "WLED uptime", _seconds),
    ("reported_led_count", "Reported LED count", _number),
    ("expected_led_count", "Expected LED count", _number),
    ("expected_active_led_count", "Expected active LEDs", _number),
    ("expected_skipped_leds", "Expected skipped LEDs", _number),
    ("led_count_matches", "LED-count match", _match),
    ("estimated_current_milliamps", "Estimated current", _milliamps),
    ("current_limit_milliamps", "Configured current limit", _milliamps),
    ("info_reason_code", "Information observation", _plain),
    ("state_reason_code", "State observation", _plain),
)

HYPERHDR_METRICS: tuple[MetricSpec, ...] = (
    ("instance_running", "Instance", _running_stopped),
    ("grabber_active", "Grabber", _active_inactive),
    ("led_output_active", "LED output", _active_inactive),
    ("hdr_mode_enabled", "HDR mode", _on_off),
    ("server_info_received", "Server information received", _yes_no),
    ("reason_code", "Observation", _plain),
)

CAPTURE_METRICS: tuple[MetricSpec, ...] = (
    ("device_node_present", "Node present", _yes_no),
    ("character_device", "Character-device validation", _yes_no),
    ("v4l2_registered", "V4L2 registration", _yes_no),
    ("process_read_access", "Process read access", _yes_no),
    ("device_name", "Bounded device name", _plain),
    ("activity_source", "Grabber activity source", _plain),
    ("grabber_active", "Active grabber", _active_inactive),
    ("reason_code", "Observation", _plain),
)

SYSTEM_METRICS: tuple[MetricSpec, ...] = (
    ("cpu_temperature_c", "CPU temperature", _temperature),
    ("cpu_temperature_warning_c", "Temperature warning threshold", _temperature),
    ("load_average_1m", "Load average (1 minute)", _number),
    ("load_average_5m", "Load average (5 minutes)", _number),
    ("load_average_15m", "Load average (15 minutes)", _number),
    ("logical_cpu_count", "Logical CPU count", _number),
    ("memory_used_percent", "Memory use", _percent),
    ("memory_warning_percent", "Memory warning threshold", _percent),
    ("root_storage_used_percent", "Root-storage use", _percent),
    ("storage_warning_percent", "Storage warning threshold", _percent),
    ("host_uptime_seconds", "Host uptime", _seconds),
)


@dataclass(frozen=True, slots=True)
class ComponentPresentation:
    """Allowlisted presentation metadata for one health component."""

    name: str
    label: str
    path: str
    metrics: tuple[MetricSpec, ...]


COMPONENTS = (
    ComponentPresentation("wled", "WLED", "/wled", WLED_METRICS),
    ComponentPresentation("hyperhdr", "HyperHDR", "/hyperhdr", HYPERHDR_METRICS),
    ComponentPresentation("capture", "Capture device", "/capture", CAPTURE_METRICS),
    ComponentPresentation(
        "raspberry_pi",
        "Raspberry Pi",
        "/system",
        SYSTEM_METRICS,
    ),
)
COMPONENT_BY_NAME = {component.name: component for component in COMPONENTS}


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _component(report: HealthReport, name: str) -> ComponentHealth | None:
    return next((item for item in report.components if item.name == name), None)


def _badge(status: HealthStatus, *, label: str | None = None) -> str:
    text = status.value if label is None else label
    return (
        f'<span class="status-badge {status.value}" '
        f'aria-label="Status: {_escape(status.value)}">{_escape(text)}</span>'
    )


def _formatted_value(
    component: ComponentHealth | None,
    key: str,
    formatter: ValueFormatter,
) -> str:
    if component is None:
        return "Not reported"
    value = component.details.get(key)
    if value is None:
        return "Not reported"
    return formatter(value)


def _metric_list(
    component: ComponentHealth | None,
    metrics: tuple[MetricSpec, ...],
    *,
    extra: tuple[tuple[str, str], ...] = (),
) -> str:
    items = [
        (
            f"<div><dt>{_escape(label)}</dt>"
            f"<dd>{_escape(_formatted_value(component, key, formatter))}</dd></div>"
        )
        for key, label, formatter in metrics
    ]
    items.extend(
        f"<div><dt>{_escape(label)}</dt><dd>{_escape(value)}</dd></div>"
        for label, value in extra
    )
    return f'<dl class="metrics">{"".join(items)}</dl>'


def _observation_meta(component: ComponentHealth | None) -> str:
    if component is None:
        return (
            '<p class="observation-meta">Last successful observation: '
            "No successful observation<br>Request latency: Not reported</p>"
        )
    last_success = component.last_successful_at or "No successful observation"
    return (
        '<p class="observation-meta">Last successful observation: '
        f"{_escape(last_success)}<br>Request latency: "
        f"{_escape(_milliseconds(component.latency_ms))}</p>"
    )


def _component_state(
    component: ComponentHealth | None,
) -> tuple[HealthStatus, str]:
    if component is None:
        return (
            HealthStatus.UNAVAILABLE,
            "No component observation is available in this snapshot.",
        )
    return component.status, component.message


def _strict_boolean_detail(
    report: HealthReport,
    component_name: str,
    detail_name: str,
) -> bool | None:
    component = _component(report, component_name)
    if component is None or component.status is HealthStatus.UNAVAILABLE:
        return None
    value = component.details.get(detail_name)
    return value if type(value) is bool else None


def _reported_ambient_path(report: HealthReport) -> _ReportedAmbientPath:
    values = tuple(
        _strict_boolean_detail(report, component_name, detail_name)
        for component_name, detail_name in _AMBIENT_ACTIVITY_FIELDS
    )
    if any(value is None for value in values):
        return _ReportedAmbientPath.UNAVAILABLE
    return _ReportedAmbientPath.ACTIVE if all(values) else _ReportedAmbientPath.INACTIVE


def _lighting_boolean(value: bool | None, true_label: str, false_label: str) -> str:
    if value is None:
        return "Unavailable"
    return true_label if value else false_label


def _lighting_brightness(report: HealthReport) -> str:
    component = _component(report, "wled")
    if component is None or component.status is HealthStatus.UNAVAILABLE:
        return "Unavailable"
    value = component.details.get("brightness")
    if type(value) is not int or not 0 <= value <= 255:
        return "Unavailable"
    return f"{value} / 255"


def _configuration_profile_label(configuration_profile: object) -> str:
    if not is_profile_id(configuration_profile):
        return "Custom configuration"
    assert isinstance(configuration_profile, str)
    return configuration_profile


def _lighting_control_link(control_link: ControlNavigationLink | None) -> str:
    if control_link is ControlNavigationLink.LOGIN:
        return '<a class="lighting-action" href="/login">Login</a>'
    if control_link is ControlNavigationLink.CONTROLS:
        return '<a class="lighting-action" href="/controls">Controls</a>'
    return ""


def render_current_lighting(
    report: HealthReport,
    configuration_profile: object,
    control_link: ControlNavigationLink | None = None,
) -> str:
    """Render the shared allowlisted Current Lighting presentation."""
    ambient_path = _reported_ambient_path(report)
    badge_status = {
        _ReportedAmbientPath.ACTIVE: HealthStatus.HEALTHY,
        _ReportedAmbientPath.INACTIVE: HealthStatus.DEGRADED,
        _ReportedAmbientPath.UNAVAILABLE: HealthStatus.UNAVAILABLE,
    }[ambient_path]
    wled_output = _strict_boolean_detail(report, "wled", "output_on")
    hyperhdr_instance = _strict_boolean_detail(report, "hyperhdr", "instance_running")
    hyperhdr_grabber = _strict_boolean_detail(report, "hyperhdr", "grabber_active")
    hyperhdr_led_output = _strict_boolean_detail(
        report, "hyperhdr", "led_output_active"
    )
    capture_available = _strict_boolean_detail(report, "capture", "device_node_present")
    metrics = (
        (
            "WLED output state",
            _lighting_boolean(wled_output, "On", "Off"),
        ),
        ("WLED brightness", _lighting_brightness(report)),
        (
            "HyperHDR instance state",
            _lighting_boolean(hyperhdr_instance, "Running", "Not running"),
        ),
        (
            "HyperHDR video-grabber state",
            _lighting_boolean(hyperhdr_grabber, "Active", "Inactive"),
        ),
        (
            "HyperHDR LED-output state",
            _lighting_boolean(hyperhdr_led_output, "Active", "Inactive"),
        ),
        (
            "Capture-device availability",
            _lighting_boolean(capture_available, "Available", "Unavailable"),
        ),
        (
            "Aurora configuration profile",
            _configuration_profile_label(configuration_profile),
        ),
    )
    metric_items = "".join(
        f"<div><dt>{_escape(label)}</dt><dd>{_escape(value)}</dd></div>"
        for label, value in metrics
    )
    return f"""
<section class="current-lighting" aria-labelledby="current-lighting-heading">
  <div class="current-lighting-heading">
    <div>
      <p class="eyebrow">Lighting at a glance</p>
      <h2 id="current-lighting-heading">Current Lighting</h2>
    </div>
    <div class="ambient-path">
      <span>Reported Ambient Path</span>
      <span class="status-badge {badge_status.value}"
        aria-label="Reported Ambient Path: {_escape(ambient_path.value)}">
        {_escape(ambient_path.value)}
      </span>
    </div>
  </div>
  <dl class="metrics lighting-metrics">{metric_items}</dl>
  <div class="lighting-footer">
    <p>Reported state does not verify physical LED illumination, live HDMI signal
    freshness, visual correctness, or screen-content matching.</p>
    {_lighting_control_link(control_link)}
  </div>
</section>"""


def _navigation(
    active_path: str,
    control_link: ControlNavigationLink | None,
) -> str:
    links = []
    for page in PORTAL_PAGES:
        current = ' aria-current="page"' if page.path == active_path else ""
        links.append(
            f'<li><a href="{_escape(page.path)}"{current}>'
            f"{_escape(page.navigation_label)}</a></li>"
        )
    if control_link is ControlNavigationLink.LOGIN:
        links.append('<li><a href="/login">Login</a></li>')
    elif control_link is ControlNavigationLink.CONTROLS:
        links.append('<li><a href="/controls">Controls</a></li>')
    return (
        '<nav class="primary-nav" aria-label="Primary navigation">'
        f"<ul>{''.join(links)}</ul></nav>"
    )


def _component_card(
    report: HealthReport,
    presentation: ComponentPresentation,
) -> str:
    component = _component(report, presentation.name)
    status, message = _component_state(component)
    return f"""
<article class="component-card">
  <div class="card-heading">
    <h2><a href="{_escape(presentation.path)}">{_escape(presentation.label)}</a></h2>
    {_badge(status)}
  </div>
  <p class="component-message">{_escape(message)}</p>
  {_metric_list(component, presentation.metrics)}
  {_observation_meta(component)}
</article>"""


def _overview(
    report: HealthReport,
    configuration_profile: object,
    control_link: ControlNavigationLink | None,
) -> str:
    cards = "".join(
        _component_card(report, presentation) for presentation in COMPONENTS
    )
    return f"""
<section class="overview-hero" aria-labelledby="overall-heading">
  <div>
    <h2 id="overall-heading">Overall Aurora status</h2>
    <p>The worst current component state determines this summary.</p>
  </div>
  {_badge(report.status)}
</section>
{render_current_lighting(report, configuration_profile, control_link)}
<section class="component-grid" aria-label="Component health">
  {cards}
</section>"""


def _detail_panel(
    report: HealthReport,
    presentation: ComponentPresentation,
    *,
    future_title: str | None = None,
    future_text: str | None = None,
    future_label: str = "Planned · Not active",
    extra: tuple[tuple[str, str], ...] = (),
) -> str:
    component = _component(report, presentation.name)
    status, message = _component_state(component)
    future = ""
    if future_title is not None and future_text is not None:
        future = f"""
<aside class="panel future-note" aria-labelledby="future-heading">
  <span class="future-label">{_escape(future_label)}</span>
  <h2 id="future-heading">{_escape(future_title)}</h2>
  <p>{_escape(future_text)}</p>
</aside>"""
    return f"""
<div class="detail-layout">
  <section class="panel" aria-labelledby="component-heading">
    <div class="panel-heading">
      <h2 id="component-heading">Current observation</h2>
      {_badge(status)}
    </div>
    <p class="component-message">{_escape(message)}</p>
    {_metric_list(component, presentation.metrics, extra=extra)}
    {_observation_meta(component)}
  </section>
  <div class="stack">{future}</div>
</div>"""


def _preview(path: str) -> str:
    if path == "/room-map":
        title = "Room mapping is planned and not active"
        text = (
            "A future milestone may model named, independently validated room zones. "
            "This preview does not discover devices, map a room, or send lighting "
            "output."
        )
        items = (
            "No zones are configured or controlled by this page.",
            "No endpoint, LED layout, or installation value is exposed.",
            "The existing single-zone ambient path remains unchanged.",
        )
    else:
        title = "Spatial intelligence is planned and not active"
        text = (
            "Future analysis may emit typed, bounded spatial events for a "
            "deterministic effects engine. This portal performs no capture, tracking, "
            "prediction, AI inference, or multi-zone output."
        )
        items = (
            "AI will not send unrestricted LED commands.",
            "Low-confidence future events will be suppressed or use ambient fallback.",
            "Physical output remains outside this milestone.",
        )
    list_items = "".join(f"<li>{_escape(item)}</li>" for item in items)
    return f"""
<section class="preview-panel">
  <span class="future-label">Planned · Not active</span>
  <h2>{_escape(title)}</h2>
  <p>{_escape(text)}</p>
  <ul class="flow-list">{list_items}</ul>
</section>"""


def _page_content(
    report: HealthReport,
    path: str,
    configuration_profile: object,
    control_link: ControlNavigationLink | None,
) -> str:
    if path == "/":
        return _overview(report, configuration_profile, control_link)
    if path == "/wled":
        return _detail_panel(
            report,
            COMPONENT_BY_NAME["wled"],
            future_title="Controls require the protected control plane",
            future_text=(
                "This public status page remains read-only. Milestone 15 exposes only "
                "separately enabled, authenticated, allowlisted WLED operations under "
                "the protected Controls area."
            ),
            future_label="Protected · separately enabled",
        )
    if path == "/hyperhdr":
        return _detail_panel(
            report,
            COMPONENT_BY_NAME["hyperhdr"],
            future_title="Controls require the protected control plane",
            future_text=(
                "This public status page remains read-only. Milestone 16 exposes only "
                "separately enabled, authenticated, allowlisted HyperHDR component "
                "operations under the protected Controls area."
            ),
            future_label="Protected · separately enabled",
        )
    if path == "/capture":
        return _detail_panel(report, COMPONENT_BY_NAME["capture"])
    if path == "/system":
        system = _component(report, "raspberry_pi")
        observed_at = "Not reported" if system is None else system.checked_at
        return _detail_panel(
            report,
            COMPONENT_BY_NAME["raspberry_pi"],
            extra=(
                (
                    "Aurora dashboard service uptime",
                    _seconds(report.service_uptime_seconds),
                ),
                ("Last observation time", observed_at),
            ),
        )
    return _preview(path)


def render_portal(
    report: HealthReport,
    path: str,
    refresh_seconds: int,
    *,
    control_link: ControlNavigationLink | None = None,
    configuration_profile: object = None,
) -> str:
    """Render one allowlisted portal route from a shared sanitized snapshot."""
    page = PORTAL_PAGE_BY_PATH[path]
    content = _page_content(report, path, configuration_profile, control_link)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="{refresh_seconds}">
  <title>{_escape(page.title)} · Project Aurora</title>
  <link rel="stylesheet" href="/static/portal.css">
</head>
<body>
  <a class="skip-link" href="#main-content">Skip to main content</a>
  <header class="site-header">
    <div class="header-inner">
      <div class="header-top">
        <a class="brand" href="/" aria-label="Project Aurora home">
          <span class="brand-mark" aria-hidden="true">A</span>
          <span>Project Aurora</span>
        </a>
        <div class="snapshot-summary" aria-label="Current health snapshot">
          <p>Last refresh: {_escape(report.checked_at)}<br>
          Service uptime: {_escape(_seconds(report.service_uptime_seconds))}</p>
          {_badge(report.status, label=f"Overall {report.status.value}")}
        </div>
      </div>
      {_navigation(path, control_link)}
    </div>
  </header>
  <main id="main-content" tabindex="-1">
    <header class="page-heading">
      <p class="eyebrow">Read-only local portal</p>
      <h1>{_escape(page.title)}</h1>
      <p class="lede">{_escape(page.description)}</p>
    </header>
    {content}
  </main>
  <footer class="site-footer">
    Read-only monitoring · automatic refresh every {_escape(refresh_seconds)} seconds
  </footer>
</body>
</html>"""
