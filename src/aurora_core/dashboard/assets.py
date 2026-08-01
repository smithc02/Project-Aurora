"""Locally bundled presentation assets for the read-only Aurora portal."""

from __future__ import annotations

PORTAL_CSS_PATH = "/static/portal.css"

PORTAL_CSS = b"""\
:root {
  color-scheme: dark;
  --background: #080c12;
  --surface: #111924;
  --surface-raised: #172231;
  --border: #2a394c;
  --text: #f1f5f9;
  --muted: #9cabbd;
  --accent: #7dd3fc;
  --accent-strong: #38bdf8;
  --healthy: #6ee7b7;
  --healthy-bg: #123b31;
  --degraded: #fde68a;
  --degraded-bg: #463a13;
  --unavailable: #fda4af;
  --unavailable-bg: #4a1f2a;
  font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-synthesis: none;
}

* { box-sizing: border-box; }

html { background: var(--background); }

body {
  margin: 0;
  min-width: 280px;
  color: var(--text);
  background:
    radial-gradient(circle at 85% -10%, #12375a 0, transparent 38rem),
    var(--background);
  line-height: 1.55;
}

a { color: var(--accent); }
a:hover { color: #bae6fd; }
a:focus-visible, [tabindex]:focus-visible {
  outline: 3px solid var(--accent-strong);
  outline-offset: 3px;
}

.skip-link {
  position: fixed;
  z-index: 10;
  top: 0.75rem;
  left: 0.75rem;
  transform: translateY(-250%);
  padding: 0.65rem 0.9rem;
  border-radius: 0.5rem;
  color: #06101a;
  background: var(--accent);
  font-weight: 800;
}
.skip-link:focus { transform: translateY(0); }

.site-header {
  border-bottom: 1px solid var(--border);
  background: rgb(8 12 18 / 88%);
}

.header-inner, main, .site-footer {
  width: min(100% - 2rem, 76rem);
  margin-inline: auto;
}

.header-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  padding-block: 1rem;
}

.brand {
  display: inline-flex;
  align-items: center;
  gap: 0.7rem;
  color: var(--text);
  text-decoration: none;
  font-weight: 800;
  letter-spacing: 0.01em;
}

.brand-mark {
  display: grid;
  width: 2.35rem;
  height: 2.35rem;
  place-items: center;
  border: 1px solid #256a91;
  border-radius: 0.75rem;
  color: #e0f2fe;
  background: linear-gradient(135deg, #0e7490, #4338ca);
}

.snapshot-summary {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 0.85rem;
  color: var(--muted);
  font-size: 0.82rem;
  text-align: right;
}
.snapshot-summary p { margin: 0; }

.status-badge {
  display: inline-flex;
  align-items: center;
  gap: 0.42rem;
  width: max-content;
  padding: 0.32rem 0.65rem;
  border: 1px solid transparent;
  border-radius: 999px;
  font-size: 0.72rem;
  font-weight: 800;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  white-space: nowrap;
}
.status-badge::before {
  width: 0.45rem;
  height: 0.45rem;
  border-radius: 50%;
  background: currentcolor;
  content: "";
}
.status-badge.healthy {
  border-color: #246c56;
  color: var(--healthy);
  background: var(--healthy-bg);
}
.status-badge.degraded {
  border-color: #806a25;
  color: var(--degraded);
  background: var(--degraded-bg);
}
.status-badge.unavailable {
  border-color: #864052;
  color: var(--unavailable);
  background: var(--unavailable-bg);
}

.primary-nav { overflow-x: auto; }
.primary-nav ul {
  display: flex;
  gap: 0.25rem;
  min-width: max-content;
  margin: 0;
  padding: 0 0 0.7rem;
  list-style: none;
}
.primary-nav a {
  display: block;
  padding: 0.5rem 0.7rem;
  border-radius: 0.5rem;
  color: var(--muted);
  text-decoration: none;
  font-size: 0.9rem;
  font-weight: 650;
}
.primary-nav a:hover { color: var(--text); background: var(--surface); }
.primary-nav a[aria-current="page"] {
  color: #e0f2fe;
  background: #11304a;
}

main { padding-block: 2.2rem 3rem; }
.page-heading { max-width: 50rem; margin-bottom: 1.8rem; }
.eyebrow {
  margin: 0 0 0.35rem;
  color: var(--accent);
  font-size: 0.75rem;
  font-weight: 800;
  letter-spacing: 0.13em;
  text-transform: uppercase;
}
h1, h2, h3, p { overflow-wrap: anywhere; }
h1 { margin: 0; font-size: clamp(2rem, 6vw, 3.3rem); line-height: 1.08; }
h2 { margin-block: 0; font-size: 1.22rem; }
h3 { margin-block: 0; font-size: 1rem; }
.lede { margin: 0.7rem 0 0; color: var(--muted); font-size: 1.05rem; }

.overview-hero, .panel, .component-card, .preview-panel {
  border: 1px solid var(--border);
  border-radius: 1rem;
  background: linear-gradient(145deg, rgb(23 34 49 / 96%), rgb(13 20 29 / 96%));
  box-shadow: 0 1.2rem 3rem rgb(0 0 0 / 18%);
}
.overview-hero {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 1.5rem;
  align-items: center;
  padding: clamp(1.15rem, 4vw, 2rem);
  margin-bottom: 1.3rem;
}
.overview-hero p { margin: 0.45rem 0 0; color: var(--muted); }
.overview-hero .status-badge { font-size: 0.85rem; padding: 0.55rem 0.8rem; }

.component-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 1rem;
}
.component-card, .panel { padding: 1.15rem; }
.card-heading, .panel-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 0.8rem;
}
.card-heading a { color: var(--text); text-decoration: none; }
.card-heading a:hover { color: var(--accent); }
.component-message { min-height: 2.9rem; margin: 0.65rem 0 1rem; color: var(--muted); }

.metrics {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(7rem, auto);
  gap: 0;
  margin: 0;
  border-top: 1px solid var(--border);
}
.metrics div {
  display: contents;
}
.metrics dt, .metrics dd {
  padding-block: 0.48rem;
  border-bottom: 1px solid rgb(42 57 76 / 65%);
}
.metrics dt { color: var(--muted); }
.metrics dd {
  margin: 0;
  padding-left: 1rem;
  text-align: right;
  overflow-wrap: anywhere;
}
.observation-meta {
  margin: 1rem 0 0;
  color: var(--muted);
  font-size: 0.8rem;
}

.detail-layout {
  display: grid;
  grid-template-columns: minmax(0, 2fr) minmax(16rem, 1fr);
  gap: 1rem;
  align-items: start;
}
.stack { display: grid; gap: 1rem; }
.future-note p, .preview-panel p { color: var(--muted); }
.future-label {
  display: inline-block;
  margin-bottom: 0.75rem;
  padding: 0.25rem 0.55rem;
  border: 1px solid #5d4e98;
  border-radius: 999px;
  color: #ddd6fe;
  background: #2d2450;
  font-size: 0.72rem;
  font-weight: 800;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}
.preview-panel { max-width: 52rem; padding: clamp(1.2rem, 5vw, 2rem); }
.flow-list { margin: 1.2rem 0 0; padding-left: 1.2rem; color: var(--muted); }
.flow-list li + li { margin-top: 0.45rem; }

.auth-card {
  width: min(100%, 34rem);
  padding: clamp(1.2rem, 5vw, 2rem);
  border: 1px solid var(--border);
  border-radius: 1rem;
  background: linear-gradient(145deg, rgb(23 34 49 / 96%), rgb(13 20 29 / 96%));
}
.auth-card h2 { margin-bottom: 1rem; }
.auth-form { display: grid; gap: 1rem; }
.form-field { display: grid; gap: 0.35rem; }
.form-field label { color: var(--muted); font-weight: 700; }
.form-field input {
  width: 100%;
  min-height: 2.75rem;
  padding: 0.6rem 0.7rem;
  border: 1px solid var(--border);
  border-radius: 0.5rem;
  color: var(--text);
  background: #080d14;
  font: inherit;
}
.auth-form button, .logout-form button {
  width: max-content;
  min-height: 2.7rem;
  padding: 0.55rem 0.9rem;
  border: 1px solid #2677a5;
  border-radius: 0.55rem;
  color: #06101a;
  background: var(--accent);
  font: inherit;
  font-weight: 800;
  cursor: pointer;
}
.auth-form button:hover { background: #bae6fd; }
.form-error {
  padding: 0.7rem 0.8rem;
  border: 1px solid #864052;
  border-radius: 0.55rem;
  color: var(--unavailable);
  background: var(--unavailable-bg);
}
.logout-form { margin-top: 1.2rem; }
.logout-form .secondary-button {
  color: var(--text);
  background: var(--surface-raised);
  border-color: var(--border);
}
.safety-statement { margin-top: 1rem; }
.safety-statement p { color: var(--muted); }

.site-footer {
  padding-block: 1.2rem 2rem;
  border-top: 1px solid var(--border);
  color: var(--muted);
  font-size: 0.82rem;
}

@media (max-width: 760px) {
  .header-top, .overview-hero { align-items: flex-start; }
  .snapshot-summary {
    flex-direction: column-reverse;
    align-items: flex-end;
    gap: 0.35rem;
  }
  .component-grid, .detail-layout { grid-template-columns: 1fr; }
}

@media (max-width: 520px) {
  .header-inner, main, .site-footer { width: min(100% - 1.2rem, 76rem); }
  .header-top { align-items: stretch; flex-direction: column; }
  .snapshot-summary { align-items: flex-start; text-align: left; }
  .overview-hero { grid-template-columns: 1fr; }
  .metrics {
    grid-template-columns: minmax(0, 1fr) minmax(6rem, auto);
    font-size: 0.9rem;
  }
}

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { scroll-behavior: auto !important; }
}
"""
