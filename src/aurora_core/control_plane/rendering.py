"""Server-rendered authentication pages with no client-side behavior."""

from __future__ import annotations

import html
import math

from aurora_core.control_plane.sessions import SessionContext


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
    Authentication foundation only · no device controls are active
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
  <p class="lede">Authenticate locally to view the Milestone 14 control-plane
  status. Authentication does not enable device controls.</p>
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


def render_controls(session: SessionContext) -> str:
    """Render an authenticated placeholder with only a CSRF-protected logout."""
    remaining_minutes = max(1, math.ceil(session.expires_in_seconds / 60))
    content = f"""
<header class="page-heading">
  <p class="eyebrow">Authenticated · mutations disabled</p>
  <h1>Control-plane status</h1>
  <p class="lede">The security foundation is active. No device-control operations
  are registered in this milestone.</p>
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
      <div><dt>Mutations enabled</dt><dd>No</dd></div>
      <div><dt>Available operations</dt><dd>None</dd></div>
    </dl>
    <form method="post" action="/logout" class="logout-form">
      <input type="hidden" name="csrf_token" value="{_escape(session.csrf_token)}">
      <button type="submit" class="secondary-button">Sign out</button>
    </form>
  </section>
  <aside class="panel future-note" aria-labelledby="planned-controls-heading">
    <span class="future-label">Planned · Not active</span>
    <h2 id="planned-controls-heading">Future control categories</h2>
    <ul class="flow-list">
      <li>Bounded WLED lighting operations</li>
      <li>Bounded HyperHDR operations</li>
      <li>Validated profiles with backup and rollback</li>
    </ul>
  </aside>
</div>
<section class="panel safety-statement" aria-labelledby="safety-heading">
  <h2 id="safety-heading">No controls are active</h2>
  <p>No WLED, HyperHDR, DDP, service, power, room-map, multi-zone, capture, or AI
  controls exist in Milestone 14. This page cannot contact or change a device.</p>
</section>"""
    return _shell(
        title="Control-plane status", content=content, active_label="Controls"
    )
