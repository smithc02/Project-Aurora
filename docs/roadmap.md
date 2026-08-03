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

18. **Milestone 18 (architecture proposed; implementation not started):
    Persistent health history, alerting, and bounded automation.** The proposed
    design records only a strict projection of existing sanitized health
    snapshots, evaluates deterministic alert state, and performs bounded
    retention work. It authorizes no device, service, configuration, command,
    or arbitrary network action. See the
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
