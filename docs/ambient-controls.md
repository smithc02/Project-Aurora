# Combined ambient-mode controls

Milestone 19 Slice Three adds exactly two Aurora-owned composite operations:

- `aurora.ambient_on`; and
- `aurora.ambient_off`.

They are protected, bounded synchronous compositions over the existing WLED and
HyperHDR services. They do not create a runtime controller, infer state from the
cached health report, poll devices directly, retry, queue, or roll back. The
public portal and `GET /api/health` remain read-only at schema version 1.

## Activation and availability

Composite controls have a separate fail-closed policy:

```yaml
ambient_controls:
  enabled: false
  allowed_operations: []
  operation_limit: 20
  operation_window_seconds: 60
```

`enabled` is a strict Boolean. The allowlist accepts only the two unique fixed
identifiers above. The attempt limit is 1–120 and the monotonic window is
1–3600 seconds. Equivalent environment names are
`AURORA_AMBIENT_CONTROLS__ENABLED`,
`AURORA_AMBIENT_CONTROLS__ALLOWED_OPERATIONS`,
`AURORA_AMBIENT_CONTROLS__OPERATION_LIMIT`, and
`AURORA_AMBIENT_CONTROLS__OPERATION_WINDOW_SECONDS`.

Authentication, the parent switch, and explicit parent allowlisting are all
required. Each required child must also be available through its existing
component switch and allowlist. Enabling all child operations never implicitly
authorizes either parent operation.

## Exact sequences

Ambient On calls the existing services in this order:

1. enable the HyperHDR video grabber;
2. power WLED on; and
3. enable HyperHDR LED output.

It stops on the first child result that is not `VERIFIED`. Earlier verified
states remain in place, and no compensation is attempted.

Ambient Off calls:

1. disable HyperHDR LED output;
2. disable the HyperHDR video grabber; and
3. power WLED off.

The first step is the safety boundary. If it is not verified, processing stops.
After verified LED-output isolation, a non-verified grabber result does not
prevent the independently safe WLED power-off attempt. Existing fixed child
confirmation values are supplied by Aurora only after the browser's dedicated
`confirm_ambient_off` parent confirmation is validated.

No step is skipped because cached Current Lighting evidence claims that the
desired state is already present. Each invoked child retains its existing
timeout, verification, limiter, cache invalidation, and audit behavior.

## Results and partial completion

The aggregate result exposes only fixed enums: status, attempted steps, and
verified steps. Status is one of `COMPLETED`, `PARTIALLY_COMPLETED`,
`UNVERIFIED`, `FAILED`, `DENIED`, `BUSY`, or `RATE_LIMITED`.

Any non-verified outcome after earlier verified work is partial completion.
Without earlier verified work, the first child's fixed classification maps to
unverified, failed, denied, busy, or rate limited. A failure never claims that a
physical mutation definitely did not occur; transport loss can make the device
outcome ambiguous. Redirects use fixed notice identifiers only. Partial and
unverified notices direct the operator to refreshed Current Lighting and the
individual component controls.

## Serialization and limiting

Production construction creates one reentrant, nonblocking process-local
mutation gate shared by the ambient, WLED, and HyperHDR services. The ambient
service owns it for the complete sequence; reentrant child calls on that same
request thread then acquire it without deadlock. Standalone component requests
also acquire it before their existing private component lock. Any competing
mutation returns busy immediately: there is no wait, queue, worker, or
background thread. Component private locks remain in place and nonblocking.

One browser request consumes one parent limiter attempt. Every invoked child
also consumes its existing component limiter attempt. Capacity is not reserved
or rolled back, so a later child limiter may intentionally produce a partial
result after earlier verified work.

At configured maximum timeouts, one synchronous request can take approximately
25 seconds because the two HyperHDR steps each retain bounded mutation and
verification work. At most two HyperHDR mutations and one WLED mutation occur.
There is no aggregate deadline, retry, rollback, polling loop, or persistent
job. `ThreadingHTTPServer` can continue serving other request threads while
competing mutations fail busy.

## Routes, audit, and cache

The authenticated routes are:

- `POST /controls/ambient/on`;
- `POST /controls/ambient/off`.

They reuse the existing process-memory session, CSRF, strict form parsing,
bounded body, POST-redirect-GET, and fixed-notice boundaries. Ambient Off alone
requires its dedicated parent confirmation. The authenticated `/controls` page
renders only available parent forms and retains all seven individual component
forms for recovery. No public mutation route exists.

Every invoked child emits its normal child audit event. Exactly one final parent
event records the aggregate outcome with only schema version, fixed event,
fixed reason, and fixed parent operation ID. Raw responses, exceptions,
endpoints, credentials, sessions, CSRF tokens, and client identifiers are never
included.

Each verified child retains its existing cache invalidation; other child
outcomes do not invalidate. The ambient service does not collect health, inspect
`HealthReport`, or add another invalidation policy. The redirected `/controls`
GET makes the normal single cached-health request.

## Non-goals

This slice adds no cached-state optimization, automatic recovery, transaction,
rollback, retry, preset, effect, color, tuning, service restart, configuration
write, DDP, scheduler, worker, persistence, health-history connection, or
production-history behavior. Milestone 18 remains paused, disabled, and
disconnected.
