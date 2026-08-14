"""Server-rendered authentication pages with no client-side behavior."""

from __future__ import annotations

import html
import math

from aurora_core.config.models import HyperHDROperation, WLEDOperation
from aurora_core.control_plane.contracts import (
    LED_OUTPUT_DISABLE_CONFIRMATION_VALUE,
    POWER_OFF_CONFIRMATION_VALUE,
    VIDEO_GRABBER_DISABLE_CONFIRMATION_VALUE,
    ControlCapabilities,
)
from aurora_core.control_plane.hyperhdr_service import HyperHDRControlAvailability
from aurora_core.control_plane.sessions import SessionContext
from aurora_core.control_plane.wled_service import WLEDControlAvailability
from aurora_core.dashboard.models import ComponentHealth, HealthReport
from aurora_core.dashboard.portal import render_current_lighting


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _shell(*, title: str, content: str, active_label: str) -> str:
    active_path = "/login" if active_label == "Login" else "/controls"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_escape(title)} · Project Aurora</title>
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
        <span class="future-label">Protected control plane</span>
      </div>
      <nav class="primary-nav" aria-label="Primary navigation">
        <ul>
          <li><a href="/">Overview</a></li>
          <li>
            <a href="{active_path}" aria-current="page">{_escape(active_label)}</a>
          </li>
        </ul>
      </nav>
    </div>
  </header>
  <main id="main-content" tabindex="-1">
    {content}
  </main>
  <footer class="site-footer">
    Protected local control plane · bounded operations only
  </footer>
</body>
</html>"""


def render_login(
    *,
    authentication_enabled: bool,
    next_path: str,
    error_message: str | None = None,
) -> str:
    """Render a credential form or a fail-closed unavailable explanation."""
    if not authentication_enabled:
        content = """
<header class="page-heading">
  <p class="eyebrow">Read-only public portal</p>
  <h1>Control plane unavailable</h1>
  <p class="lede">Authentication is disabled. Protected control routes remain
  unavailable and no controls are active.</p>
</header>
<section class="auth-card">
  <h2>Fail-closed by default</h2>
  <p>Configure and validate authentication before attempting to use the protected
  control-plane foundation.</p>
  <p><a href="/">Return to Overview</a></p>
</section>"""
        return _shell(
            title="Control plane unavailable", content=content, active_label="Login"
        )

    error = (
        ""
        if error_message is None
        else f'<p class="form-error" role="alert">{_escape(error_message)}</p>'
    )
    content = f"""
<header class="page-heading">
  <p class="eyebrow">Protected control plane</p>
  <h1>Operator login</h1>
  <p class="lede">Authenticate locally to view the protected control plane.
  Authentication alone does not enable device controls.</p>
</header>
<section class="auth-card" aria-labelledby="login-heading">
  <h2 id="login-heading">Sign in</h2>
  {error}
  <form method="post" action="/login" class="auth-form">
    <input type="hidden" name="next" value="{_escape(next_path)}">
    <div class="form-field">
      <label for="username">Username</label>
      <input id="username" name="username" type="text" maxlength="64"
        autocomplete="username" required>
    </div>
    <div class="form-field">
      <label for="password">Password</label>
      <input id="password" name="password" type="password" maxlength="1024"
        autocomplete="current-password" required>
    </div>
    <button type="submit">Sign in</button>
  </form>
</section>"""
    return _shell(title="Operator login", content=content, active_label="Login")


def _component(report: HealthReport, name: str) -> ComponentHealth | None:
    return next((item for item in report.components if item.name == name), None)


def _wled_availability_text(availability: WLEDControlAvailability) -> str:
    return {
        WLEDControlAvailability.AUTHENTICATION_UNAVAILABLE: (
            "Authentication unavailable"
        ),
        WLEDControlAvailability.WLED_UNAVAILABLE: "WLED unavailable",
        WLEDControlAvailability.CONTROLS_DISABLED: "WLED controls disabled",
        WLEDControlAvailability.NO_OPERATIONS: "No operations allowlisted",
        WLEDControlAvailability.AVAILABLE: "WLED controls available",
    }[availability]


def _hyperhdr_availability_text(
    availability: HyperHDRControlAvailability,
) -> str:
    return {
        HyperHDRControlAvailability.AUTHENTICATION_UNAVAILABLE: (
            "Authentication unavailable"
        ),
        HyperHDRControlAvailability.HYPERHDR_UNAVAILABLE: "HyperHDR unavailable",
        HyperHDRControlAvailability.CONTROLS_DISABLED: "HyperHDR controls disabled",
        HyperHDRControlAvailability.NO_OPERATIONS: "No operations allowlisted",
        HyperHDRControlAvailability.AVAILABLE: "HyperHDR controls available",
    }[availability]


def _wled_forms(
    session: SessionContext,
    operations: tuple[WLEDOperation, ...],
    maximum_brightness: int,
) -> str:
    forms: list[str] = []
    if WLEDOperation.POWER_ON in operations:
        forms.append(f"""
<form method="post" action="/controls/wled/power-on" class="auth-form">
  <input type="hidden" name="csrf_token" value="{_escape(session.csrf_token)}">
  <button type="submit">Power on</button>
</form>""")
    if WLEDOperation.POWER_OFF in operations:
        forms.append(f"""
<form method="post" action="/controls/wled/power-off"
  class="auth-form destructive-control">
  <input type="hidden" name="csrf_token" value="{_escape(session.csrf_token)}">
  <label>
    <input type="checkbox" name="confirmation"
      value="{POWER_OFF_CONFIRMATION_VALUE}" required>
    Confirm that lighting may black out
  </label>
  <button type="submit">Power off</button>
</form>""")
    if WLEDOperation.BRIGHTNESS_SET in operations:
        forms.append(f"""
<form method="post" action="/controls/wled/brightness" class="auth-form">
  <input type="hidden" name="csrf_token" value="{_escape(session.csrf_token)}">
  <div class="form-field">
    <label for="brightness">Brightness (1–{_escape(maximum_brightness)})</label>
    <input id="brightness" name="brightness" type="number" min="1"
      max="{_escape(maximum_brightness)}" step="1" required>
  </div>
  <button type="submit">Set brightness</button>
</form>""")
    if not forms:
        return "<p>No WLED operation forms are currently available.</p>"
    return f'<div class="control-operation-grid">{"".join(forms)}</div>'


def _hyperhdr_forms(
    session: SessionContext,
    operations: tuple[HyperHDROperation, ...],
) -> str:
    forms: list[str] = []
    if HyperHDROperation.VIDEO_GRABBER_ENABLE in operations:
        forms.append(f"""
<form method="post" action="/controls/hyperhdr/video-grabber/enable"
  class="auth-form">
  <input type="hidden" name="csrf_token" value="{_escape(session.csrf_token)}">
  <button type="submit">Enable video grabber</button>
</form>""")
    if HyperHDROperation.VIDEO_GRABBER_DISABLE in operations:
        forms.append(f"""
<form method="post" action="/controls/hyperhdr/video-grabber/disable"
  class="auth-form destructive-control">
  <input type="hidden" name="csrf_token" value="{_escape(session.csrf_token)}">
  <label>
    <input type="checkbox" name="confirmation"
      value="{VIDEO_GRABBER_DISABLE_CONFIRMATION_VALUE}" required>
    Confirm that disabling the video grabber interrupts HyperHDR capture
  </label>
  <button type="submit">Disable video grabber</button>
</form>""")
    if HyperHDROperation.LED_OUTPUT_ENABLE in operations:
        forms.append(f"""
<form method="post" action="/controls/hyperhdr/led-output/enable"
  class="auth-form">
  <input type="hidden" name="csrf_token" value="{_escape(session.csrf_token)}">
  <button type="submit">Enable LED output</button>
</form>""")
    if HyperHDROperation.LED_OUTPUT_DISABLE in operations:
        forms.append(f"""
<form method="post" action="/controls/hyperhdr/led-output/disable"
  class="auth-form destructive-control">
  <input type="hidden" name="csrf_token" value="{_escape(session.csrf_token)}">
  <label>
    <input type="checkbox" name="confirmation"
      value="{LED_OUTPUT_DISABLE_CONFIRMATION_VALUE}" required>
    Confirm that disabling LED output interrupts HyperHDR LED transmission
  </label>
  <button type="submit">Disable LED output</button>
</form>""")
    if not forms:
        return "<p>No HyperHDR operation forms are currently available.</p>"
    return f'<div class="control-operation-grid">{"".join(forms)}</div>'


def render_controls(
    session: SessionContext,
    *,
    report: HealthReport,
    configuration_profile: object = None,
    capabilities: ControlCapabilities | None = None,
    wled_availability: WLEDControlAvailability = (
        WLEDControlAvailability.CONTROLS_DISABLED
    ),
    wled_operations: tuple[WLEDOperation, ...] = (),
    wled_maximum_brightness: int = 255,
    hyperhdr_availability: HyperHDRControlAvailability = (
        HyperHDRControlAvailability.CONTROLS_DISABLED
    ),
    hyperhdr_operations: tuple[HyperHDROperation, ...] = (),
) -> str:
    """Render one authenticated view of cached state and fixed controls."""
    if capabilities is None:
        capabilities = ControlCapabilities()
    remaining_minutes = max(1, math.ceil(session.expires_in_seconds / 60))
    operations = (
        "None"
        if not capabilities.available_operations
        else ", ".join(capabilities.available_operations)
    )
    mutations = "Yes" if capabilities.mutations_enabled else "No"
    wled_component = _component(report, "wled")
    hyperhdr_component = _component(report, "hyperhdr")
    wled_health = (
        "Not reported" if wled_component is None else wled_component.status.value
    )
    hyperhdr_health = (
        "Not reported"
        if hyperhdr_component is None
        else hyperhdr_component.status.value
    )
    content = f"""
<header class="page-heading">
  <p class="eyebrow">Authenticated · bounded appliance controls</p>
  <h1>Lighting Controls</h1>
  <p class="lede">Current cached lighting state and the existing independently
  bounded WLED and HyperHDR controls in one Aurora view.</p>
</header>
{render_current_lighting(report, configuration_profile)}
<div class="lighting-controls-grid">
  <section class="panel control-section" aria-labelledby="wled-control-heading">
    <div class="panel-heading">
      <div>
        <p class="eyebrow">WLED / Brightness</p>
        <h2 id="wled-control-heading">WLED lighting output</h2>
      </div>
      <span class="status-badge healthy">Authenticated</span>
    </div>
    <dl class="metrics compact-metrics">
      <div><dt>Control availability</dt>
        <dd>{_escape(_wled_availability_text(wled_availability))}</dd></div>
      <div><dt>Cached component health</dt><dd>{_escape(wled_health)}</dd></div>
    </dl>
    <div class="control-forms" aria-labelledby="wled-operations-heading">
      <h3 id="wled-operations-heading">Allowlisted operations</h3>
      {_wled_forms(session, wled_operations, wled_maximum_brightness)}
    </div>
    <p class="detail-link"><a href="/controls/wled">Open detailed WLED status</a></p>
  </section>
  <section class="panel control-section"
    aria-labelledby="hyperhdr-control-heading">
    <div class="panel-heading">
      <div>
        <p class="eyebrow">Ambient Processing / HyperHDR</p>
        <h2 id="hyperhdr-control-heading">Ambient processing</h2>
      </div>
      <span class="status-badge healthy">Authenticated</span>
    </div>
    <dl class="metrics compact-metrics">
      <div><dt>Control availability</dt>
        <dd>{_escape(_hyperhdr_availability_text(hyperhdr_availability))}</dd></div>
      <div><dt>Cached component health</dt><dd>{_escape(hyperhdr_health)}</dd></div>
    </dl>
    <div class="control-forms" aria-labelledby="hyperhdr-operations-heading">
      <h3 id="hyperhdr-operations-heading">Allowlisted operations</h3>
      {_hyperhdr_forms(session, hyperhdr_operations)}
    </div>
    <p class="detail-link"><a href="/controls/hyperhdr">
      Open detailed HyperHDR status</a></p>
  </section>
</div>
<div class="detail-layout controls-meta">
  <section class="panel" aria-labelledby="session-heading">
    <div class="panel-heading">
      <h2 id="session-heading">Authenticated session</h2>
      <span class="status-badge healthy">Authenticated</span>
    </div>
    <dl class="metrics">
      <div><dt>Operator</dt><dd>{_escape(session.username)}</dd></div>
      <div><dt>Absolute session expiration</dt>
        <dd>In {_escape(remaining_minutes)} minute(s)</dd></div>
      <div><dt>Mutations enabled</dt><dd>{mutations}</dd></div>
      <div><dt>Available operations</dt><dd>{_escape(operations)}</dd></div>
    </dl>
    <form method="post" action="/logout" class="logout-form">
      <input type="hidden" name="csrf_token" value="{_escape(session.csrf_token)}">
      <button type="submit" class="secondary-button">Sign out</button>
    </form>
  </section>
  <section class="panel safety-statement" aria-labelledby="safety-heading">
    <h2 id="safety-heading">Strict control boundary</h2>
    <p>Each form invokes one existing independently bounded operation. No combined
    Ambient On or Ambient Off action, sequencing, rollback, preset, effect,
    service, power-supply, or arbitrary device operation exists.</p>
  </section>
</div>"""
    return _shell(title="Lighting Controls", content=content, active_label="Controls")


def render_wled_controls(
    session: SessionContext,
    *,
    component: ComponentHealth | None,
    availability: WLEDControlAvailability,
    operations: tuple[WLEDOperation, ...],
    maximum_brightness: int,
    notice: str | None,
) -> str:
    """Render fixed forms and allowlisted WLED snapshot fields only."""
    notices = {
        "verified": "The requested state was verified. Health will refresh next.",
        "denied": "The operation was denied by the bounded control policy.",
        "invalid": "The submitted value was invalid.",
        "rate_limited": "The operation-attempt limit has been reached.",
        "busy": "Another WLED operation is already in progress.",
        "failed": "The device request failed safely.",
        "unverified": (
            "State could not be verified. The requested change may have applied."
        ),
    }
    notice_html = (
        ""
        if notice not in notices
        else f'<p class="form-error" role="status">{_escape(notices[notice])}</p>'
    )
    availability_text = _wled_availability_text(availability)
    status = "Not reported" if component is None else component.status.value
    output = "Not reported"
    brightness = "Not reported"
    latency = "Not reported"
    observed = "No successful observation"
    if component is not None:
        observed = component.last_successful_at or observed
        latency = f"{component.latency_ms:g} ms"
        output_value = component.details.get("output_on")
        if isinstance(output_value, bool):
            output = "On" if output_value else "Off"
        brightness_value = component.details.get("brightness")
        if type(brightness_value) is int:
            brightness = str(brightness_value)

    forms_html = _wled_forms(session, operations, maximum_brightness)
    content = f"""
<header class="page-heading">
  <p class="eyebrow">Authenticated · fixed WLED adapter</p>
  <h1>Bounded WLED controls</h1>
  <p class="lede">Only explicitly allowlisted operations are rendered and accepted.</p>
</header>
{notice_html}
<div class="detail-layout">
  <section class="panel" aria-labelledby="wled-state-heading">
    <h2 id="wled-state-heading">Current cached observation</h2>
    <dl class="metrics">
      <div><dt>Control availability</dt><dd>{_escape(availability_text)}</dd></div>
      <div><dt>WLED health</dt><dd>{_escape(status)}</dd></div>
      <div><dt>Current output</dt><dd>{_escape(output)}</dd></div>
      <div><dt>Current brightness</dt><dd>{_escape(brightness)}</dd></div>
      <div><dt>Last successful observation</dt><dd>{_escape(observed)}</dd></div>
      <div><dt>Request latency</dt><dd>{_escape(latency)}</dd></div>
      <div><dt>Configured brightness maximum</dt>
        <dd>{_escape(maximum_brightness)}</dd></div>
    </dl>
    <p><a href="/controls">Back to control-plane status</a></p>
  </section>
  <section class="panel" aria-labelledby="wled-operation-heading">
    <h2 id="wled-operation-heading">Allowlisted operations</h2>
    {forms_html}
  </section>
</div>
<aside class="panel future-note" aria-labelledby="realtime-warning-heading">
  <h2 id="realtime-warning-heading">Live-output interaction</h2>
  <p>HyperHDR or WLED realtime data may subsequently change the observed WLED state.
  These controls do not override, stop, or resume live data.</p>
</aside>"""
    return _shell(
        title="Bounded WLED controls", content=content, active_label="Controls"
    )


def render_hyperhdr_controls(
    session: SessionContext,
    *,
    component: ComponentHealth | None,
    availability: HyperHDRControlAvailability,
    operations: tuple[HyperHDROperation, ...],
    notice: str | None,
) -> str:
    """Render fixed HyperHDR forms and shared cached snapshot fields only."""
    notices = {
        "verified": "The requested state was verified. Health will refresh next.",
        "denied": "The operation was denied by the bounded control policy.",
        "rate_limited": "The operation-attempt limit has been reached.",
        "busy": "Another HyperHDR operation is already in progress.",
        "failed": "The HyperHDR request failed safely.",
        "unverified": (
            "The change may have applied, but its state could not be verified."
        ),
    }
    notice_html = (
        ""
        if notice not in notices
        else f'<p class="form-error" role="status">{_escape(notices[notice])}</p>'
    )
    availability_text = _hyperhdr_availability_text(availability)
    health = "Not reported" if component is None else component.status.value
    instance = "Not reported"
    grabber = "Not reported"
    led_output = "Not reported"
    observed = "No observation"
    if component is not None:
        observed = component.checked_at
        values = (
            ("instance_running", "instance"),
            ("grabber_active", "grabber"),
            ("led_output_active", "led_output"),
        )
        rendered: dict[str, str] = {}
        for key, target in values:
            value = component.details.get(key)
            rendered[target] = (
                "Active"
                if value is True
                else "Inactive"
                if value is False
                else "Not reported"
            )
        instance = rendered["instance"]
        grabber = rendered["grabber"]
        led_output = rendered["led_output"]

    forms_html = _hyperhdr_forms(session, operations)
    content = f"""
<header class="page-heading">
  <p class="eyebrow">Authenticated · fixed HyperHDR adapter</p>
  <h1>Bounded HyperHDR controls</h1>
  <p class="lede">Only explicitly allowlisted component-state operations are
  rendered and accepted.</p>
</header>
{notice_html}
<div class="detail-layout">
  <section class="panel" aria-labelledby="hyperhdr-state-heading">
    <h2 id="hyperhdr-state-heading">Current cached observation</h2>
    <dl class="metrics">
      <div><dt>Control availability</dt><dd>{_escape(availability_text)}</dd></div>
      <div><dt>HyperHDR health</dt><dd>{_escape(health)}</dd></div>
      <div><dt>Instance running</dt><dd>{_escape(instance)}</dd></div>
      <div><dt>Video grabber active</dt><dd>{_escape(grabber)}</dd></div>
      <div><dt>LED output active</dt><dd>{_escape(led_output)}</dd></div>
      <div><dt>Last observation</dt><dd>{_escape(observed)}</dd></div>
    </dl>
    <p><a href="/controls">Back to control-plane status</a></p>
  </section>
  <section class="panel" aria-labelledby="hyperhdr-operation-heading">
    <h2 id="hyperhdr-operation-heading">Allowlisted operations</h2>
    {forms_html}
  </section>
</div>
<aside class="panel future-note" aria-labelledby="verification-warning-heading">
  <h2 id="verification-warning-heading">Bounded verification</h2>
  <p>A verified serverinfo Boolean does not prove physical LED output, fresh capture
  frames, HDMI signal, WLED state, wiring, or power state.</p>
</aside>"""
    return _shell(
        title="Bounded HyperHDR controls", content=content, active_label="Controls"
    )
