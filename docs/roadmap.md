# Roadmap

## Completed foundation

1. **Milestone 1 (completed):** repository and development environment scaffold.
2. **Milestone 2 (completed):** validated configuration without hardware I/O.
3. **Milestone 3 (completed):** runtime planning and lifecycle contracts without
   adapters.
4. **Milestone 4 (completed):** explicit one-shot WLED read-only validation using
   GET `/json/info`.
5. **Milestone 5 (completed):** explicit one-shot HyperHDR read-only serverinfo
   validation using GET `/json-rpc` only.
6. **Milestone 6 (completed):** explicit non-invasive Linux capture-device
   presence and V4L2 registration metadata validation.
7. **Milestone 7 (completed):** explicit query-only Linux V4L2 capability
   validation using one `VIDIOC_QUERYCAP` ioctl.
8. **Milestone 8 (completed):** explicit bounded query-only Linux V4L2 format,
   size, and frame-interval enumeration.
9. **Milestone 9 (completed):** bounded single-frame V4L2 validation with
   read/write acquisition preferred, one-buffer MMAP fallback for
   streaming-only devices, and mandatory buffer wiping and cleanup.
10. **Milestone 10 (completed):** explicit operator-only bounded DDP output
    validation using one low-intensity frame followed by one blackout frame.
    Runtime DDP integration and continuous transmission remain deferred.
11. **Milestone 11 (documentation completed; physical proof operator-owned):**
    operator-controlled single-zone baseline-path proof and deployment runbook
    using the existing validation boundaries and HyperHDR-owned live color path.
    The repository does not claim the physical path has passed. See the
    [single-zone baseline proof](single-zone-baseline-proof.md).
12. **Milestone 12 (completed):** the original local read-only health dashboard
    with bounded, non-overlapping WLED, HyperHDR, capture metadata, and Raspberry
    Pi checks, plus a stable JSON endpoint. See the
    [health dashboard guide](health-dashboard.md).
13. **Milestone 13 (completed): Unified read-only Aurora portal.** A branded,
    responsive shell and native read-only pages reuse the Milestone 12 cached
    snapshot and retain `GET /api/health` schema version 1. Room Map and Spatial
    Intelligence are inactive previews only. See the
    [unified portal guide](unified-portal.md).
14. **Milestone 14 (completed): Authenticated control-plane and mutation-safety
    foundation.** Optional fail-closed local authentication, process-memory
    sessions, CSRF-protected logout, bounded login attempts, sanitized audit
    events, and an empty typed operation boundary protect status-only control
    routes. Public health behavior and API schema version 1 remain unchanged;
    no device controls exist. See the
    [control-plane security guide](control-plane-security.md).
15. **Milestone 15 (completed): Bounded WLED lighting controls.** Exactly three
    authenticated, CSRF-protected, separately enabled and allowlisted operations
    provide power on, confirmed power off, and bounded absolute brightness.
    Fixed payloads, response verification, nonblocking serialization, attempt
    limiting, sanitized audit events, and verified-success cache invalidation
    preserve the public health API. See the
    [bounded WLED control guide](wled-controls.md).
16. **Milestone 16 (completed, merged, deployed, and physically validated):
    Bounded HyperHDR controls.** Exactly four authenticated,
    CSRF-protected, separately enabled and allowlisted component-state
    operations control only the video grabber and LED output. One fixed mutation
    POST plus one fixed serverinfo verification GET, nonblocking serialization,
    separate attempt limiting, disruptive-action confirmation, sanitized audit,
    and verified-success cache invalidation preserve health schema version 1.
    See the [bounded HyperHDR control guide](hyperhdr-controls.md).

17. **Milestone 17 (completed, merged, and deployed; controlled Linux
    filesystem validation passed): Local configuration profiles, exact backup,
    validation, atomic activation, and rollback.**
    Complete YAML profiles use strict logical identifiers and replace only the
    YAML layer.
    Raw and environment-aware validation, restrictive no-follow filesystem
    handling, sanitized plans, exact bounded manifests, shared nonblocking
    locking, atomic publication, verified automatic recovery, and explicit
    reversible rollback add no dashboard, service, environment, network, or
    device operation. See the
    [configuration-profile guide](configuration-profiles.md).
    Deployment and validation do not assert that production profiles were
    created or activated.

## Planned progression

18. **Milestone 18 (implementation in progress; sixteen executable foundation
    slices plus the documentation-only Slice Seventeen clock-safety decision
    gate):
    Persistent health history, alerting, and bounded automation.** The proposed
    design records only a strict projection of existing sanitized health
    snapshots. Its first production slice adds the strict projection and reason
    registry plus explicit secure creation, fail-closed opening, and exact
    SQLite schema version 1 verification. The second slice adds the
    pre-deployment singleton ingestion checkpoint, monotonic scheduler sequence,
    fixed replay ledger, atomic projection ingestion, deterministic history
    compaction, health and sampling-gap evaluation, and ingestion-driven alert
    lifecycle. The third slice adds immutable bounded history, alert, and event
    read models with fixed keyset cursors, strict row/digest validation, and no
    mutation. The fourth slice makes schema version 1 incremental-vacuum ready,
    adds the minimum retention and foreign-key indexes, and exposes one
    deterministic 500-logical-row cleanup transaction plus one fixed 128-page
    incremental-vacuum call. The fifth slice adds fixed 64-MiB main-database and
    128-MiB free-space preflights, exact physical WAL framing plus one NOOP
    current-generation status read (available since SQLite 3.51.0) guarded by
    Aurora's SQLite 3.51.3 safe-WAL floor because that release fixes the
    WAL-reset corruption bug affecting versions through 3.51.2 (the reviewed
    production Pi provides SQLite 3.53.1), a pure bounded retention-cleanup/
    incremental-vacuum/checkpoint/block decision, and one bounded PASSIVE-
    checkpoint opportunity. The sixth slice adds a direct-only orchestration
    core that admits one write only after the fixed storage decision, performs
    at most one cleanup plus one 128-page vacuum plus one reinspection for
    capacity pressure, and permits at most one PASSIVE attempt for checkpoint
    pressure. Its immutable trigger state models startup, monotonic hourly, and
    120-newly-stored-row opportunities without scheduling them. It is not
    imported by a runtime entry point, creates no deployment database, and
    leaves production history disabled and unavailable. The seventh slice adds
    a read-only checkpoint resume model plus direct monotonic cadence state. It
    resumes at committed sequence plus one, performs arithmetic bounded missed-
    interval calculation without catch-up polling, selects one startup or
    backward-clock marker without deriving misses from restart UTC duration,
    calls an injected future shared-HealthService boundary and the existing
    projection/orchestrator at most once, then composes at most one existing
    maintenance opportunity. It starts no thread, timer, sleep loop, service
    hook, or runtime import. The eighth slice adds strict, lexical-only
    `health_history` settings that remain disabled by default: an explicit
    database path is required before enablement, database startup policy is one
    of `open_existing` or `create_if_missing`, sampling is bounded to 5–300
    seconds and cannot be faster than dashboard refresh when enabled, and
    retention is bounded to 1–365 days. Validation performs no filesystem or
    database access, and the tracked example remains disabled without a path.
    The ninth slice strengthens the direct-only history filesystem boundary:
    descriptor-relative no-follow walks require root and intermediate
    components to be root- or service-user-owned directories without group or
    world write permission, retain exact service-user-owned mode `0700` for the
    final parent, and reject identity or security-metadata changes across a
    second complete walk. Validation remains read-only, existing direct
    create/open operations inherit it, and the standard-library SQLite
    pathname-open limitation remains documented. The tenth slice adds a
    direct-only cross-process leadership handle using the fixed empty
    `.aurora-health-history.lock` file inside that protected directory. The
    service-user-owned mode-`0600` single-link file remains stable on disk while
    one nonblocking exclusive advisory `flock` represents leadership; busy does
    not retry, release never unlinks, and no PID, hostname, timestamp, UUID, or
    SQLite content is written. The lock, directory, and ancestry are revalidated
    around acquisition, but no runtime or settings integration invokes it. The
    eleventh slice centralizes the exact SQLite 3.51.3 minimum in one
    standard-library-only capability boundary. Direct Store create and
    open-existing enforce it before any filesystem or SQLite bootstrap work,
    while NOOP WAL inspection reuses the same gate; absent, malformed, or old
    metadata fails with fixed non-trust `unsupported_runtime`, creates no
    database artifact, and exposes no version detail. The twelfth slice adds one
    database-wide `PRAGMA foreign_key_check` after exact schema and persisted-
    state invariants and before the existing `quick_check(1)`. It requires zero
    violations under an independent one-second progress deadline, stops at the
    first violation without exposing its contents, treats cursor and handler
    cleanup as part of verification, performs no repair, and keeps schema
    version 1. The thirteenth slice composes an already-validated, enabled
    `HealthHistorySettings` snapshot with one protected-parent leadership handle
    and one verified Store. Disabled settings remain a no-op; the fixed lock
    basename is rejected as a database target; leadership precedes every Store
    operation; and `create_if_missing` falls back to one existing open only for
    exact `already_exists`. Store closes before leadership, cleanup uncertainty
    fails closed, and a leadership release failure remains terminal without
    treating its `closed` property as proof of kernel release or retrying writer
    handoff. The fourteenth slice borrows that already-open lifecycle for one
    direct-only startup storage preflight: capacity, free space, and WAL are
    inspected exactly once in order before the existing pure storage decision
    runs with capacity maintenance unattempted. It performs no remediation,
    retry, verification, or automatic close, and caller ownership remains
    unchanged. The fifteenth slice requires every direct
    `HealthHistoryOrchestrator` construction to inject a strict 1–365-day
    retention policy. Both existing cleanup paths receive that exact policy;
    construction remains I/O-free and no default fallback, runtime caller, or
    scheduler change is added. The sixteenth slice composes the existing
    startup preflight with at most one existing PASSIVE checkpoint only for
    `WAL_CHECKPOINT_DUE`. BUSY returns the original non-ready result without
    retry; a non-BUSY checkpoint outcome receives exactly one final preflight.
    It performs no capacity cleanup and retains caller ownership of the
    lifecycle. Slice Seventeen documents the required fail-closed wall-clock
    trust, suspension, restart, operator-recovery, and sanitized-readiness
    policy without implementing it. The divergence formula and inclusive
    boundary behavior are fixed, but repository evidence is insufficient to
    select the numeric tolerance. Schema version 1 also cannot represent the
    selected durable suspension, trusted UTC high-water, episode identity, and
    recovery state while ingestion continues. A separate persisted-state
    decision is required, and no schema change is authorized by this gate.
    Store thread affinity and writer serialization remain a separate decision:
    a `Lock` does not override default `sqlite3` thread ownership, and neither
    `check_same_thread=False` nor a writer thread/queue is authorized. No
    scheduler, runtime, deployment path, or production database is enabled.
    Clock behavior and mutation gating, startup capacity remediation, history
    failure isolation, Store/thread ownership, the production scheduler driver,
    writer serialization, bounded scheduler stop/join, bounded shutdown
    `TRUNCATE`, protected deployment directory and service-account validation,
    production startup/lifecycle composition, presentation routes,
    acknowledgment, backup/restore, migrations, notifications, and production
    enablement remain unimplemented. It
    authorizes no device, service, configuration, command, or arbitrary network
    action. See the
    [health-history and alerting design](health-history-alerting.md).
19. **Milestone 19: Multi-zone virtual room model.**
20. **Milestone 20: Real-time motion-tracking prototype.**
21. **Milestone 21: Predictive off-screen continuation.**
22. **Milestone 22: Scene and object intelligence.**
23. **Milestone 23: Adaptive calibration and user-guided learning.**
24. **Milestone 24: Deterministic spatial-effects engine.**
25. **Milestone 25: Game and content profiles.**

Each planned milestone requires a separate design and safety review. This order
does not authorize later behavior in an earlier milestone.

## Long-term spatial-intelligence flow

The intended future boundary is:

```text
capture analysis frame
→ motion and object tracking
→ trajectory estimation
→ predicted screen-edge exit
→ structured spatial event
→ deterministic bounded effects engine
→ mapped room LED zones
```

For example, a future analyzer might observe an aircraft moving from the center
of the screen toward and beyond the right edge. It would estimate the exit time
and emit a typed event describing direction, confidence, and timing. A separate
deterministic engine could then continue the effect through validated right-side
and rear-room LED zones at the proper time. The analyzer would not select raw
LED packets, arbitrary zones, or unrestricted brightness.

The following boundaries are mandatory for that future work:

- AI must never directly send unrestricted LED commands. The AI layer must emit
  a typed, bounded event.
- A deterministic effects engine must enforce brightness, duration, zone,
  confidence, and rate limits.
- Low-confidence events must be suppressed or fall back to standard ambient
  lighting.
- Initial development must use recorded clips and a visual debug overlay before
  physical multi-zone output is enabled.
- Learning must initially adjust bounded calibration and preference parameters,
  rather than rewrite control logic.
- Standard ambient operation must remain available as a fallback.

No spatial-intelligence implementation, AI dependency, frame-analysis loop,
room-zone output, or learning system exists through the Milestone 18 design.
