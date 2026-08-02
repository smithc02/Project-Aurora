# Control-plane security foundation

Milestone 14 added an authenticated boundary for future Project Aurora controls.
It did not add a device control. Milestone 15 now places three bounded WLED
operations inside that boundary without changing its session or CSRF model. The
public health portal and `GET /api/health`
continue to use the Milestone 12 single-flight health service and remain backward
compatible. Authentication state is held by a separate in-process service and
does not change the health API's schema-version-1 fields or meanings.

## Security boundary and routes

The route groups are intentionally separate:

| Boundary | Routes | Behavior |
| --- | --- | --- |
| Public health | `/`, `/wled`, `/hyperhdr`, `/capture`, `/system`, `/room-map`, `/spatial-intelligence`, `/api/health`, `/static/portal.css` | Existing sanitized read-only portal and API behavior. |
| Authentication | `GET /login`, `POST /login`, `POST /logout` | Local credential verification and session lifecycle only. |
| Protected status | `GET /controls`, `GET /api/control/status` | Authenticated status and a versioned, configuration-derived capability response. |
| Protected WLED | `GET /controls/wled`, three fixed route-specific POST handlers | Optional Milestone 15 operations described in the [WLED control guide](wled-controls.md). |

Authentication is disabled by default. In that state, protected routes return
an unavailable response rather than treating the visitor as authenticated. When
enabled, the HTML status page redirects an unauthenticated visitor to login and
the control-status API returns JSON `401`. Authentication therefore fails
closed: a missing or disabled authentication configuration cannot grant control
access.

The protected capability response reports mutations enabled only when
authentication, WLED, the separate control switch, and at least one implemented
allowlisted operation are all enabled. Otherwise it reports false and an empty
list. There is no generic execute route, HyperHDR mutation, DDP output, system
command, service action, configuration write, capture operation, room-zone
output, or AI operation.

Login, logout, `/controls`, and the capability API do not query the public
health snapshot. The protected WLED page uses the same cached snapshot as public
pages and never creates a direct read. Public pages continue to share the
existing cached, single-flight snapshot.

## Configuration

Authentication is nested under `dashboard.authentication`:

| Field | Default | Accepted boundary |
| --- | --- | --- |
| `enabled` | `false` | Strict boolean. |
| `username` | unset | Required when enabled; 1–64 ASCII letters, digits, `.`, `_`, or `-`, beginning with a letter or digit. |
| `password_hash` | unset | Required when enabled; a supported, bounded versioned hash stored as a protected value. |
| `session_ttl_minutes` | `480` | 5–1440 minutes; absolute, not sliding. |
| `maximum_sessions` | `16` | 1–64 process-local sessions. |
| `secure_cookie` | `false` | Adds the cookie `Secure` flag when true. |
| `login_attempt_limit` | `5` | 1–20 attempts in the configured window. |
| `login_attempt_window_seconds` | `300` | 30–3600 seconds. |

Existing configurations need no changes because the nested model has safe
defaults. Enabling authentication without both a valid username and password
hash is a configuration error. Validation output omits the invalid input and
the model represents the hash as a protected secret, so neither normal model
output nor validation errors reveal it.

Every field can be supplied with the existing nested environment syntax. The
credential variables are:

```text
AURORA_DASHBOARD__AUTHENTICATION__ENABLED
AURORA_DASHBOARD__AUTHENTICATION__USERNAME
AURORA_DASHBOARD__AUTHENTICATION__PASSWORD_HASH
```

The policy fields use the same prefix followed by
`SESSION_TTL_MINUTES`, `MAXIMUM_SESSIONS`, `SECURE_COOKIE`,
`LOGIN_ATTEMPT_LIMIT`, or `LOGIN_ATTEMPT_WINDOW_SECONDS`. Aurora does not load
`.env` files. `.env.example` lists names for documentation only.

Do not commit an operator name or password hash to YAML. Prefer a separately
protected systemd `EnvironmentFile` or another process environment mechanism.
The service example references an optional, untracked environment file without
providing deployment values. Restrict that file to the service administrator and
service account according to the host's operating policy.

## Generate a password hash

Run:

```bash
uv run aurora security hash-password
```

The command reads the password twice with `getpass`, rejects empty, mismatched,
or excessively long values, and accepts no plaintext-password command-line
argument. On success it writes only one copyable versioned hash to standard
output. It never edits configuration.

Milestone 14 uses standard-library PBKDF2-HMAC-SHA256 with a random 16-byte
salt, a 32-byte derived key, and an explicitly versioned iteration parameter.
The default is 600,000 iterations. Verification validates the complete grammar,
encoded lengths, and a 200,000–1,000,000 work-factor range before derivation,
then compares derived keys with a constant-time comparison. Unsupported,
malformed, or excessively expensive values fail safely.

## Session lifecycle and cookie policy

A successful login rotates any valid session presented with the request and
creates a new cryptographically random session identifier and per-session CSRF
token. Only a SHA-256 digest of the session identifier is retained server-side.
The cookie carries only the opaque identifier; it contains no username,
privilege, authentication assertion, or CSRF token.

Sessions are protected by a lock for the threaded HTTP server, expire against a
monotonic clock at an absolute deadline, and are capped by
`maximum_sessions`. Expired entries are removed during store operations. If the
cap is reached, the oldest session is evicted. Logout invalidates the server-side
entry and explicitly expires the cookie. All sessions disappear on process
restart. There is no remember-me or persistent-session feature.

The Project Aurora-specific cookie has `HttpOnly`, `SameSite=Strict`, and
`Path=/`, plus an absolute `Max-Age`. `Secure` is added only when
`secure_cookie` is true. Malformed, duplicate, unknown, expired, and invalid
cookies fail as unauthenticated and are never logged or rendered.

Direct trusted-LAN HTTP cannot use a `Secure` cookie. Leave `secure_cookie`
false only for that constrained deployment. If a separately managed TLS
boundary presents HTTPS to the browser, set it true after verifying the entire
proxy and cookie path. Aurora does not terminate TLS and does not add HSTS to
ordinary HTTP responses. Do not expose this service directly to the internet.

## CSRF and request parsing

Login creates authentication state before a CSRF-bearing session exists.
Logout was Milestone 14's only authenticated state-changing form. Milestone 15
WLED forms reuse the same requirement. Each contains a strong per-session CSRF
token and posts it in the request body, never in a URL. Validation uses a
constant-time comparison. A missing, malformed, expired, or incorrect token
rejects the request. Every future authenticated mutation must reuse this
requirement.

Login and logout accept only `application/x-www-form-urlencoded` with optional
UTF-8 charset declaration. The server rejects unsupported transfer encodings,
missing, repeated, negative, or malformed `Content-Length`, oversized bodies,
invalid percent escapes, invalid UTF-8, duplicated fields, and unexpected
fields. Header and size checks occur before the body is read. Login and logout
bodies are capped separately at small fixed limits. Errors are generic and do
not include request bodies.

The `next` value is a fixed allowlist containing `/controls` and
`/controls/wled`. Absolute, external, protocol-relative, and otherwise unknown
destinations fall back to the safe internal status route.

## Login-attempt limiting

The attempt limiter uses monotonic time and keyed, process-random digests of
client identifiers. It neither persists nor logs a raw identifier. Per-client
records have a fixed count, time window, and 256-client memory cap; stale
entries are removed. A bounded global window also limits attempts spread across
many client identifiers. A successful login clears the matching client record.
Restarting the process resets all attempt state.

All credential failures use the same response and still execute the bounded
password-verification path, so the response does not identify whether the
username or password differed. A limited request receives a generic failure
page with HTTP `429`.

## Audit-event policy

Security events use the existing structured logging dependency and fixed event
and reason-code enums. Events cover successful, failed, and rate-limited login;
logout; unauthorized protected-page and protected-API access; CSRF rejection;
malformed authentication requests; and malformed, invalid, or expired sessions.

Authentication audit fields contain only schema version, event name, and
bounded reason code. WLED audit events add only an allowlisted operation ID.
They never accept passwords, password hashes, cookies, session identifiers,
CSRF tokens, request bodies, client identifiers, raw exceptions, device
endpoints, or installation details. Events are not persisted to a database in
this milestone; retention is controlled by the operator's existing logging
environment.

## Typed operation contracts after Milestone 15

Milestone 14 defined non-executable metadata and an empty operation tuple.
Milestone 15 replaces that empty registry with strict contracts only for
`wled.power_on`, `wled.power_off`, and `wled.brightness_set`. Fixed server routes
invoke a fixed WLED adapter; no browser-facing generic executor exists. Every
future operation must separately review and implement all of these boundaries:

- authenticated session and valid CSRF token;
- known, code-allowlisted typed operation identifier;
- strict typed and bounded input model;
- explicit bounded timeout;
- code-allowlisted destination adapter;
- sanitized success and error behavior;
- required audit events; and
- confirmation metadata for disruptive actions.

No operation may accept an arbitrary URL, API path, JSON object, shell command,
device payload, or forwarding target. Authentication by itself never authorizes
adding those surfaces.

## Deployment and recovery

To enable the foundation after deploying the code:

1. Generate a hash with `uv run aurora security hash-password`.
2. Create the systemd environment file referenced by the locally installed unit
   outside the repository, restrict its filesystem permissions, and set the
   three credential variables plus any reviewed policy overrides.
3. Set `AURORA_DASHBOARD__AUTHENTICATION__ENABLED=true` only after the username
   and generated hash are present.
4. Set `SECURE_COOKIE=true` only when the browser uses a verified HTTPS path;
   retain false for direct trusted-LAN HTTP.
5. Validate the combined configuration, then restart the dashboard with the
   operator's normal service-management workflow. Aurora never invokes systemd.
6. Open `/login`, authenticate, and confirm `/controls` accurately reports the
   configured capability state. Authentication alone must still show no WLED
   operations.

For credential recovery, disable authentication or replace the protected
credential environment values, validate the configuration, and restart the
process using external administration. A restart invalidates every session and
resets the attempt limiter. Disabling authentication makes protected routes
unavailable; it never bypasses login.

## Milestone 14 non-goals

Milestone 14 does not implement WLED or HyperHDR changes, DDP output, service or
power management, configuration writes, backup or rollback, persistent
sessions, persistent audit storage, SQLite, health history, alerts, automation,
capture or frame analysis, room mapping, multi-zone output, object tracking, AI,
spatial effects, arbitrary HTTP or JSON forwarding, shell execution, TLS
termination, or internet exposure.

Milestone 15's later, narrow WLED exception to the original non-goal is fully
documented in the [bounded WLED control guide](wled-controls.md). The remaining
Milestone 14 non-goals are unchanged.
