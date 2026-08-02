"""Server-rendered authentication pages with no client-side behavior."""

from __future__ import annotations

import html
import math

from aurora_core.config.models import WLEDOperation
from aurora_core.control_plane.contracts import (
    POWER_OFF_CONFIRMATION_VALUE,
    ControlCapabilities,
)
from aurora_core.control_plane.sessions import SessionContext
from aurora_core.control_plane.wled_service import WLEDControlAvailability
from aurora_core.dashboard.models import ComponentHealth


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


def render_controls(
    session: SessionContext,
    *,
    capabilities: ControlCapabilities | None = None,
    wled_availability: WLEDControlAvailability = (
        WLEDControlAvailability.CONTROLS_DISABLED
    ),
) -> str:
    """Render authenticated capability status and a protected WLED card."""
    if capabilities is None:
        capabilities = ControlCapabilities()
    remaining_minutes = max(1, math.ceil(session.expires_in_seconds / 60))
    operations = (
        "None"
        if not capabilities.available_operations
        else ", ".join(capabilities.available_operations)
    )
    mutations = "Yes" if capabilities.mutations_enabled else "No"
    availability_messages = {
        WLEDControlAvailability.AUTHENTICATION_UNAVAILABLE: (
            "Authentication unavailable",
            "Authentication is disabled, so the control plane remains unavailable.",
            False,
        ),
        WLEDControlAvailability.WLED_UNAVAILABLE: (
            "WLED unavailable",
            "No validated enabled WLED endpoint is available for bounded controls.",
            False,
        ),
        WLEDControlAvailability.CONTROLS_DISABLED: (
            "WLED controls disabled",
            "The separate WLED control switch is disabled.",
            True,
        ),
        WLEDControlAvailability.NO_OPERATIONS: (
            "No operations allowlisted",
            "WLED controls are configured, but the operation allowlist is empty.",
            True,
        ),
        WLEDControlAvailability.AVAILABLE: (
            "WLED controls available",
            "Only the configured and implemented WLED operations are available.",
            True,
        ),
    }
    availability_title, availability_text, link_allowed = availability_messages[
        wled_availability
    ]
    wled_link = (
        '<p><a href="/controls/wled">Open bounded WLED controls</a></p>'
        if link_allowed
        else ""
    )
    content = f"""
<header class="page-heading">
  <p class="eyebrow">Authenticated · bounded control plane</p>
  <h1>Control-plane status</h1>
  <p class="lede">Capabilities are the intersection of code-owned operations and
  explicitly enabled configuration.</p>
</header>
<div class="detail-layout">
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
  <aside class="panel" aria-labelledby="wled-control-heading">
    <span class="future-label">Milestone 15</span>
    <h2 id="wled-control-heading">{_escape(availability_title)}</h2>
    <p>{_escape(availability_text)}</p>
    {wled_link}
  </aside>
</div>
<section class="panel safety-statement" aria-labelledby="safety-heading">
  <h2 id="safety-heading">Strict control boundary</h2>
  <p>No presets, effects, colors, segments, HyperHDR, DDP, service, power-supply,
  room-map, multi-zone, capture, or AI controls exist. Authentication alone does not
  enable WLED operations.</p>
</section>"""
    return _shell(
        title="Control-plane status", content=content, active_label="Controls"
    )


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
    availability_text = {
        WLEDControlAvailability.AUTHENTICATION_UNAVAILABLE: (
            "Authentication unavailable"
        ),
        WLEDControlAvailability.WLED_UNAVAILABLE: "WLED unavailable",
        WLEDControlAvailability.CONTROLS_DISABLED: "WLED controls disabled",
        WLEDControlAvailability.NO_OPERATIONS: "No operations allowlisted",
        WLEDControlAvailability.AVAILABLE: "WLED controls available",
    }[availability]
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

    forms: list[str] = []
    if WLEDOperation.POWER_ON in operations:
        forms.append(f"""
<form method="post" action="/controls/wled/power-on" class="auth-form">
  <input type="hidden" name="csrf_token" value="{_escape(session.csrf_token)}">
  <button type="submit">Power on</button>
</form>""")
    if WLEDOperation.POWER_OFF in operations:
        forms.append(f"""
<form method="post" action="/controls/wled/power-off" class="auth-form">
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
    forms_html = (
        "".join(forms)
        if forms
        else "<p>No WLED operation forms are currently available.</p>"
    )
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
