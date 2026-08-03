# Milestone 18 health history and alerting design

## Status

This document proposes Milestone 18 architecture. It does not implement a
database, configuration fields, scheduler, worker, route, alert, notification,
migration, or automation action. Milestones 12 through 17 remain the current
behavior.

The initial implementation should be disabled by default and require explicit
local configuration. Enabling history must not change `GET /api/health`, its
schema version 1 contract, the shared `HealthService` cache, authentication,
WLED or HyperHDR controls, configuration-profile commands, or filesystem
backup behavior.

## Scope

Milestone 18 is limited to deterministic processing of Aurora's existing
immutable, sanitized `HealthReport` snapshots:

- scheduled requests to the existing cached, single-flight health service;
- a strict code-owned projection into bounded local history;
- deduplication, transition detection, and periodic heartbeat records;
- alert creation, acknowledgment state, recovery detection, and archival;
- deterministic retention and bounded storage maintenance; and
- separately protected, read-only history and alert presentation in a future
  implementation.

The alert engine is a state machine, not a general automation engine. The
initial notification channel is the authenticated Aurora portal plus sanitized
local structured logging. There is no outbound notification dispatch in the
initial Milestone 18 implementation. Any later email or other delivery channel
requires its own typed, bounded, explicitly enabled design.

## Explicit non-goals and prohibited automation

Milestone 18 must not automatically or indirectly:

- restart, stop, reload, or reconfigure Aurora, HyperHDR, WLED, systemd, or the
  Raspberry Pi;
- power a device on or off, change WLED brightness or state, or enable or
  disable a HyperHDR component;
- apply or roll back an Aurora configuration profile;
- send DDP frames or capture, inspect, or retain image frames;
- invoke shell commands, subprocesses, plugins, scripts, or service managers;
- edit YAML, environment files, process environment values, or credentials;
- make arbitrary HTTP, MQTT, webhook, socket, or other network requests;
- accept or invoke a user-supplied URL, endpoint, command, payload, or query;
- copy WLED or HyperHDR configuration or restore device state; or
- create a general rules engine, remediation engine, or scheduled profile
  switcher.

The existing seven device-control operations remain the complete control
registry. Health state never authorizes a WLED, HyperHDR, DDP, configuration,
service, or hardware operation.

## Architectural recommendation

The proposed flow is:

```text
one monotonic scheduler
  -> existing HealthService.get_health()
  -> strict persistent projection
  -> one bounded writer gate and SQLite transaction
  -> transition and alert state update
  -> periodic bounded cleanup

future authenticated history GET
  -> read-only bounded SQLite query
  -> separate response model
```

The scheduler should live inside the existing dashboard process initially. It
must call the shared `HealthService`; it must not construct collectors, bypass
the cache, or create a second hardware polling path. This avoids a second
service unit and preserves the existing single-flight guarantee. A separate
collector service is deferred unless later measurements demonstrate that the
dashboard process cannot meet the bounded resource envelope.

Only one scheduler may lead in a process. A nonblocking advisory lock in the
explicit history directory should prevent two dashboard processes from both
becoming scheduled writers. Failure to obtain leadership disables scheduled
ingestion in that process; it does not queue, wait indefinitely, or affect the
live health endpoint.

## Persistence technology decision

The recommendation is SQLite through Python's standard-library `sqlite3`
module. No new package is required.

| Option | Advantages | Problems for this milestone | Decision |
| --- | --- | --- | --- |
| Bounded local SQLite | Transactions, constraints, indexes, atomic alert-state updates, bounded queries, online backup support, and mature corruption checks. | Requires explicit schema migration, journal, locking, and corruption policy. | Recommended. |
| Append-only JSON Lines plus periodic compaction | Simple sequential writes and human-readable records. | Acknowledgment and recovery updates need secondary indexes or rewrites; retention compaction replaces a large file; concurrent reads and crash recovery are harder to bound; schema enforcement is weaker. | Rejected for the initial design. |
| Memory-only ring buffer | Very small and simple. | Loses history and acknowledgment state on restart, so it does not meet the objective. | Rejected. |

SQLite fits the single-host Raspberry Pi deployment and permits one small
transaction to update a sample, evaluation state, and alert lifecycle
atomically. SQL identifiers and statements must be code-owned. Every value must
use a parameterized statement; no query may concatenate request, snapshot, or
configuration text.

### Journal mode

Use write-ahead logging (WAL), `synchronous=FULL`, foreign-key enforcement, and
a fixed 4-KiB database page size established before schema creation, plus a
short bounded busy timeout. A different page size is rejected. WAL is preferred
over a rollback journal because future bounded history reads should not block
the scheduled writer during its short commit, and a failed reader should not
delay sampling. The tradeoff is the additional WAL and shared-memory sidecars,
which must be included in permission, capacity, backup, checkpoint, shutdown,
and recovery handling.

Transactions remain small and one writer gate prevents process-local
contention. Disable SQLite automatic checkpoints. At a code-observed 256-page
WAL threshold, one maintenance opportunity may make one budgeted `PASSIVE`
checkpoint attempt, at most once per hour; a prechecked WAL above 960 pages
disables further writes until operator review. Clean shutdown may make one
budgeted `TRUNCATE` checkpoint attempt. WAL is not a license for network
filesystems or multiple uncoordinated writer services.

## Recommended resource envelope

These are proposed implementation defaults, not current configuration fields:

| Policy | Recommendation | Required bound |
| --- | --- | --- |
| Scheduled sample interval | 30 seconds | Configurable 5–300 seconds; must be at least the dashboard refresh interval. |
| Unchanged heartbeat interval | 15 minutes | From one sample interval through 24 hours. |
| History retention | 30 days | Configurable 1–365 days. |
| Main database limit | 64 MiB | Configurable only downward to 8 MiB in schema version 1; a larger cap requires a new budget review. |
| WAL target | At most one `PASSIVE` attempt per hour after 256 pages; refuse writes above 960 pages or 4 MiB including WAL framing | Automatic checkpoints disabled; clean shutdown permits one budgeted `TRUNCATE` attempt. |
| Scheduled state transactions | At most 120 per hour and 2,880 per day at the 30-second default | At most one transaction per accepted scheduled sample; no retry. |
| History query page | 100 rows | Hard maximum 500 rows. |
| Alert query page | 50 rows | Hard maximum 200 rows. |
| Cleanup transaction | At most 500 history or event rows and 128 incremental-vacuum pages | One transaction per maintenance opportunity; no drain loop. |
| SQLite busy timeout | 250 milliseconds | No retry loop after timeout. |
| Shutdown join | 5 seconds | No indefinite wait. |

The configured database limit applies to the main SQLite file. Deployment must
also reserve bounded space for WAL, shared-memory, temporary, and explicit
backup files. The implementation should enforce `max_page_count`, bounded WAL
checkpoints, a minimum free-space reserve, and a pre-transaction storage check.
If those controls disagree, the most restrictive result wins.

### Write-volume strategy and acceptance gate

History-row compaction does not mean write-free sampling. Schema version 1
should deliberately perform at most one bounded SQLite state transaction for
each accepted scheduled sample. That transaction advances the persisted
candidate and debounce counters, sampling-gap state, alert state, and required
checkpoint state even when it inserts no `health_samples` row. Projection
rejection occurs before the transaction. There is no sampling-write retry and
no second transaction for alert evaluation.

At the proposed 30-second interval, sampling therefore has a hard ceiling of
120 transactions per hour and 2,880 per day. One hourly retention transaction
is permitted separately. Operator acknowledgment, explicit backup, migration,
and restore operations are not part of the scheduled rate and require their
own bounds. The initial implementation must not add a no-write fast path: doing
so safely would first require a separately reviewed durable representation of
debounce, gap, and restart-checkpoint state.

The proposed defaults remain provisional until an isolated synthetic workload
passes all of these Raspberry Pi 5 acceptance criteria for at least 24 hours,
including one clean restart and repeated abrupt process termination at
different transaction phases:

| Measurement | Provisional acceptance ceiling |
| --- | --- |
| Sampling transactions | No more than 120 per hour or 2,880 per day, with no hidden retry transaction. |
| Maintenance transactions | No more than one retention transaction per hour in steady state. |
| WAL bytes written | No more than 4 MiB per hour and 96 MiB per day at default sampling. |
| Main-database/checkpoint bytes written | No more than 4 MiB per hour and 96 MiB per day. |
| Combined SQLite-managed writes | No more than 8 MiB per hour and 192 MiB per day, measured as actual file writes rather than file-size delta alone. |
| One checkpoint | No more than 960 pages or 4 MiB including WAL framing, and one attempt. |
| Logical database growth | No more than 2 MiB per day before retention equilibrium, with a measured 30-day projection below the 64 MiB main-file cap. |
| CPU | No more than 2% of one core averaged over the run and no sustained sampling spike above 10%. |
| Memory | No more than 32 MiB incremental resident memory and no monotonic growth above 4 MiB after warm-up. |
| Restart durability | After clean and abrupt termination, bounded startup validation succeeds, all committed state is present, at most the active uncommitted sample is absent, no duplicate alert is created, and WAL size returns below its target after at most one permitted checkpoint. |

The test record must separate WAL writes, main-file writes attributed to
checkpoints, logical file growth, and underlying filesystem/block-device write
traffic so fsync and metadata amplification are visible. Failure of any ceiling
requires changing the interval, transaction shape, checkpoint policy, or
storage recommendation and repeating the measurement. The 30-second interval,
30-day retention, 15-minute heartbeat, and 64 MiB limit cannot be approved as
implementation defaults until this gate passes.

## Persistent privacy contract

The persistence adapter accepts only `HealthReport` schema version 1 and emits
a narrower internal record. Permitted fields are:

- the report's overall status;
- report observation time and bounded service uptime;
- one of the fixed component names `wled`, `hyperhdr`, `capture`, or
  `raspberry_pi`;
- component status and component observation time;
- finite, nonnegative latency rounded to integer milliseconds and capped by a
  code-owned maximum;
- last-successful observation time when valid;
- a normalized reason code selected from a finite code-owned registry;
- a fixed sample kind: transition, heartbeat, startup gap, or clock
  discontinuity;
- a bounded missed-interval count; and
- an internal digest of the canonical permitted-field projection for
  deduplication.

The adapter must not serialize the `details` mapping or free-form `message`
field. It may derive a normalized reason code only from an exact allowlist of
known sanitized detail keys and values; unknown names or values are omitted.
This allows intentional disabled states to be suppressed without storing
configuration.

Persistent history, alert records, query responses, and logs must never contain:

- configured hosts, IP addresses, URLs, or ports tied to hosts;
- capture-device paths, identifiers, names, or arbitrary device metadata;
- credentials, operator usernames, password hashes, cookies, session IDs, or
  CSRF tokens;
- raw WLED or HyperHDR responses, firmware data, LED counts, brightness, or
  configuration values;
- raw YAML, environment values, request headers or bodies, arbitrary detail
  mappings, or serialized `HealthReport` objects;
- raw exceptions, SQL text, database paths, temporary paths, or lock contents;
  or
- image frames, payloads, notification content, or user-provided annotations.

Projection validation occurs before a writer lock or transaction. A malformed,
unknown-version, oversized, non-finite, or unknown-component snapshot is
rejected as one sanitized ingestion failure; it is never partially stored.

### Finalized normalized reason-code registry

The preimplementation registry is finalized in the isolated
`aurora_core.m18_validation.reasons` module. It is validation tooling only and
is not imported by an Aurora runtime entry point. It accepts schema version 1,
the exact component names `wled`, `hyperhdr`, `capture`, and `raspberry_pi`, the
three current health statuses, and exact current collector detail shapes. It
returns an immutable tuple of at most two WLED reasons, three HyperHDR reasons,
two capture reasons, or one Raspberry Pi reason. The tuple order is fixed by
code, never by input mapping order.

The complete normalized registry is:

| Component | Exact normalized reasons |
| --- | --- |
| WLED fixed states | `wled.disabled`, `wled.collector_failed`, `wled.healthy` |
| WLED information | `wled.info.led_count_mismatch`, `wled.info.connection_failed`, `wled.info.timeout`, `wled.info.redirect_rejected`, `wled.info.http_error`, `wled.info.response_too_large`, `wled.info.invalid_json`, `wled.info.invalid_response` |
| WLED state | `wled.state.connection_failed`, `wled.state.timeout`, `wled.state.redirect_rejected`, `wled.state.http_error`, `wled.state.response_too_large`, `wled.state.invalid_json`, `wled.state.invalid_response` |
| HyperHDR fixed states | `hyperhdr.disabled`, `hyperhdr.collector_failed`, `hyperhdr.healthy` |
| HyperHDR observation failures | `hyperhdr.connection_failed`, `hyperhdr.timeout`, `hyperhdr.redirect_rejected`, `hyperhdr.authorization_required`, `hyperhdr.http_error`, `hyperhdr.response_too_large`, `hyperhdr.invalid_json`, `hyperhdr.invalid_response`, `hyperhdr.server_reported_failure` |
| HyperHDR validated inactivity | `hyperhdr.instance_inactive`, `hyperhdr.video_grabber_inactive`, `hyperhdr.led_output_inactive` |
| Capture fixed states | `capture.disabled`, `capture.collector_failed`, `capture.healthy` |
| Capture observation failures | `capture.unsupported_platform`, `capture.device_not_found`, `capture.probe_failed`, `capture.symlink_resolution_failed`, `capture.invalid_device_target`, `capture.not_character_device`, `capture.v4l2_registration_missing`, `capture.metadata_unavailable`, `capture.invalid_device_name`, `capture.permission_denied` |
| Capture activity | `capture.grabber_inactive`, `capture.activity_unreported` |
| Raspberry Pi | `raspberry_pi.collector_failed`, `raspberry_pi.healthy`, `raspberry_pi.degraded`, `raspberry_pi.unavailable` |

Intentional WLED, HyperHDR, and capture disablement is therefore distinct from
an observation or collector failure. WLED reasons are derived only from the
exact `reason_code`, `info_reason_code`, and `state_reason_code` values listed
above. HyperHDR inactivity is derived only from exact Boolean `false` values
for instance, video-grabber, and LED-output state. Capture activity uses only
the fixed `HyperHDR serverinfo` source marker and exact Boolean or null grabber
state. Raspberry Pi reasons use only the sanitized component status because
its numeric metric and configured-threshold details must not be persisted.

The registry recognizes the current collector's remaining detail keys only to
verify that the input shape is known. It does not compare, copy, interpolate,
or return their values. These excluded values include firmware, uptime, LED
counts, brightness, output state, HDR mode, capture-device name and metadata,
temperatures, load, CPU count, memory, storage, thresholds, and host uptime.
The free-form component message is never inspected. An unknown schema,
component, status, detail key, contributing value, activity source, or
status/reason combination produces one fixed rejected result and no persistent
reason. Comprehensive synthetic tests cover every mapping, input-order and
message independence, unknown rejection, disabled-state distinctions, and the
inability of prohibited values to enter output.

## Proposed database schema

The logical schema uses the following tables. Exact SQL belongs to the future
implementation review.

### `schema_migrations`

- `version`: positive integer primary key;
- `applied_at_utc_us`: UTC microseconds; and
- no free-form migration name, host, path, or operator metadata.

SQLite `application_id` identifies an Aurora history database and
`user_version` is the authoritative current schema version. The migration table
is an audit ledger. A mismatch among file identity, `user_version`, migration
rows, and expected tables is corruption or an unsupported schema, never a cue
to recreate the database.

### `health_samples`

- integer primary key used as the stable sequence;
- observed and recorded UTC microseconds;
- overall status;
- bounded service uptime milliseconds;
- sample kind;
- fixed-size projection digest; and
- bounded missed-interval count.

An index on observed time supports newest-first retention and history queries.
An index on `(overall_status, observed time)` supports transition views. A
unique constraint on `(observed time, projection digest)` makes replayed input
idempotent.

### `component_samples`

- sample foreign key with cascading deletion;
- fixed component name;
- status;
- normalized nullable reason code;
- checked-at UTC microseconds;
- bounded latency milliseconds; and
- nullable last-successful UTC microseconds.

The primary key is `(sample, component)`. An index on
`(component, status, checked-at time)` supports bounded component history.

### `evaluation_state`

One row per fixed scope stores only the current observed status, candidate
status, bounded consecutive count, last sample and heartbeat times, sampling-gap
state, current alert reference, and cooldown deadline. It is updated within the
single state transaction for every accepted scheduled sample even when snapshot
history is compacted. It contains no raw snapshot.

### `alerts`

- integer primary key;
- fixed scope and alert kind;
- lifecycle state: open, acknowledged, recovered, or archived;
- severity;
- opened, acknowledged, recovered, and archived UTC times as nullable columns;
- first and latest supporting sample references;
- bounded episode and occurrence counts; and
- cooldown deadline.

No acknowledgment username or note is stored. A partial unique index permits at
most one open or acknowledged health alert per `(scope, alert kind)`. Indexes on
`(lifecycle, opened time)` and recovery time support bounded alert and cleanup
queries.

### `alert_events`

This append-only lifecycle ledger contains an alert foreign key, fixed event
type, UTC time, optional supporting sample reference, and resulting lifecycle
state. It has no actor, comment, request, or arbitrary metadata. An index on
`(alert, event time)` supports a bounded timeline.

All enums require database `CHECK` constraints as defense in depth. Foreign
keys are mandatory. Unknown columns, versions, states, components, and reason
codes fail closed at the model boundary.

## Schema versioning and migrations

Schema version 1 should create the tables and indexes in one transaction. Each
later version must be a code-owned, forward-only migration from exactly one
known predecessor. Startup refuses a newer or unknown version.

Before a migration, the operator-visible migration workflow must create and
verify a separate SQLite backup under the 16,384-page, 64-MiB, 30-second online
backup budget. Preflight uses at most 64 schema rows and the one two-second
`quick_check(1)`. The migration then runs under one exclusive transaction with
the 4,096-page, 16-MiB, five-second budget, updates `user_version` and the
migration ledger atomically, and repeats the 64-row schema check and one
two-second `quick_check(1)`. Failure rolls back the transaction and leaves the
pre-migration evidence intact; there is no second attempt.

There are no automatic down migrations and no silent destructive repair. An
older Aurora release must refuse a newer schema. Software rollback therefore
requires restoring the verified pre-migration history backup or disabling the
history feature; it must not alter the active Aurora YAML or device state.

## Snapshot ingestion and deduplication

The scheduler asks the shared `HealthService` for one report at each monotonic
deadline. It never performs catch-up polls. The projection is validated and
canonically encoded in fixed field order before its digest is calculated.

Within the one permitted transaction for that accepted scheduled sample, the
writer:

1. rejects a replayed `(observed time, digest)`;
2. updates consecutive transition state for every fixed scope;
3. stores a history sample immediately when any permitted status or reason code
   changed;
4. otherwise stores one heartbeat only when the configured heartbeat interval
   has elapsed;
5. evaluates alert opening, escalation, recovery, and cooldown;
6. adds fixed lifecycle events; and
7. commits all state together.

Unchanged healthy snapshots are therefore compacted. History records changes
plus periodic heartbeats, not every sample. The transaction still persists
debounce, sampling-gap, and restart-checkpoint state for every accepted
scheduled sample through `evaluation_state`. Compaction reduces history rows;
it does not by itself reduce the transaction rate or guarantee low storage
write volume.

If a dashboard request recently refreshed the cache, the scheduler may ingest
that same report once. Repeated cache reads with the same observation time and
digest are idempotent and do not inflate counters or history.

## Missed sampling periods

Scheduling uses monotonic deadlines. A delayed scheduler performs one current
sample and reports a capped count of deadline slots missed before that
observation; it never launches a burst of overdue collections. Gap opening is
based on the cumulative number of missed intervals across consecutive delayed
scheduler observations, not on the number of delayed observations by itself:

- one delayed observation reporting two or more missed intervals immediately
  satisfies the opening threshold in its successful persistence transaction;
- one observation reporting one missed interval creates a one-interval
  candidate; another delayed observation before an on-time observation adds to
  it and opens the alert at two;
- an on-time scheduled observation with zero missed intervals clears a
  one-interval candidate that never opened; and
- the delayed observation that opens an alert does not also count as a recovery
  sample, even if its current health collection succeeded.

An open or acknowledged sampling-gap alert recovers only after two consecutive
on-time scheduled samples, each with zero missed intervals, successful health
collection, and a committed persistence transaction. A delayed, failed,
rejected, or unpersisted sample breaks that recovery sequence. This is the same
two-sample recovery count as a health alert but uses an explicit on-time
condition.

A failed persistence attempt cannot open, advance, acknowledge, recover, close,
or archive a persisted gap alert. The scheduler may retain only a capped
in-memory missed-interval accumulator for the next single persistence attempt;
it is not a work queue. If the process exits first, startup-gap handling replaces
that volatile evidence.

On startup, Aurora compares the last persisted sampling checkpoint with the
current UTC time and writes at most one `startup_gap` marker. A bounded estimate
of two or more missed intervals opens a gap alert immediately in that same
successful transaction; a smaller estimate becomes the corresponding candidate.
No sample is fabricated or backfilled. A `clock_discontinuity` marker never
opens, advances, or recovers a gap alert because wall-clock movement is not proof
that monotonic scheduler deadlines were missed. It pauses gap recovery until two
new consecutive on-time monotonic observations establish a reliable sequence.

The schema-version-1 gap evaluator has these exact persisted states and
transitions. Alert acknowledgment is orthogonal: it changes an open alert's
lifecycle but leaves the evaluator in `active` or `recovery_one`.

| Current gap state | Successfully persisted scheduler evidence | Next gap state and alert effect |
| --- | --- | --- |
| `clear` | Delayed; one missed interval | `candidate_one`; no alert. |
| `clear` or `candidate_one` | Delayed; cumulative missed intervals at least two | `active`; open one gap alert. The current delayed sample is not recovery evidence. |
| `candidate_one` | On time; zero missed intervals | `clear`; no alert was opened. |
| `active` | On time; zero missed intervals and successful health collection | `recovery_one`; alert remains open or acknowledged. |
| `recovery_one` | On time; zero missed intervals and successful health collection | `clear`; transition the alert to recovered. |
| `active` or `recovery_one` | Any delayed observation | `active`; add the bounded missed count and reset recovery progress. |
| Any state | `startup_gap` estimate | Apply the same one-miss or at-least-two-miss transition as delayed scheduler evidence. |
| Any state | `clock_discontinuity` | Preserve candidate/active status, reset any recovery progress, and make no alert lifecycle change. |
| Any state | Collection, projection, or persistence failure | No persisted evaluator or alert change. Preserve only the capped volatile missed accumulator described above. |

## Health transitions and alert lifecycle

The fixed severity order is healthy, degraded, unavailable. Evaluation occurs
for overall health and each fixed component. Known intentional-disabled reason
codes are retained in history but suppressed from alert opening.

Recommended defaults are:

- degraded opens after three consecutive samples;
- unavailable opens after two consecutive samples;
- recovery requires two consecutive healthy samples;
- sampling gaps open after two missed intervals; and
- a recovered scope has a 15-minute duplicate cooldown.

Counts, not wall-clock duration alone, provide debounce. A gap, rejected
snapshot, or failed database write neither advances nor resets a health
transition counter.

The lifecycle is deterministic:

1. **Open:** a confirmed threshold creates one alert or reopens the recently
   recovered alert during cooldown.
2. **Acknowledged:** a future authenticated operator action changes only the
   lifecycle state and acknowledgment time. It does not suppress collection or
   claim recovery.
3. **Recovered:** the required healthy observations close the active condition.
4. **Archived:** after the 15-minute duplicate cooldown elapses without
   recurrence, a recovered alert becomes read-only. It remains queryable until
   30 days after `recovered_at`, then becomes eligible for bounded deletion.

Schema version 1 has no `expired` lifecycle or expiration event. An active or
acknowledged alert never becomes terminal merely because it is old. Adding an
expiration policy later requires a new reviewed schema migration and exact
transition, authorization, retention, and audit rules.

The complete schema-version-1 lifecycle is:

| Current lifecycle | Fixed cause | Next lifecycle | Fixed event |
| --- | --- | --- | --- |
| No alert | Opening threshold satisfied | `open` | `opened` |
| `open` | Authenticated, CSRF-valid acknowledgment commits | `acknowledged` | `acknowledged` |
| `acknowledged` | Severity escalates | `open` | `escalated_reopened` |
| `open` or `acknowledged` | Recovery threshold satisfied | `recovered` | `recovered` |
| `recovered` | Condition recurs before cooldown ends | `open` | `recurrence_reopened` |
| `recovered` | Cooldown ends without recurrence | `archived` | `archived` |
| `archived` | A later opening threshold is satisfied | New `open` alert | `opened` on the new alert |

Those six event identifiers are the complete version-1 `alert_events` registry.
No other action may change lifecycle. Archival is automatic and deterministic;
acknowledgment is its only operator-authorized transition. Deletion after
retention is storage cleanup, not a new lifecycle or event.

An acknowledged alert that escalates from degraded to unavailable returns to
open and records a fixed escalation event. A repeat condition during cooldown
reopens the recovered alert and increments its bounded episode count. A repeat
after archival creates a new alert. The partial unique index and transaction
enforce duplicate prevention even if evaluation is invoked twice.

## Notification boundary

Initial Milestone 18 has no outbound delivery channel. The protected portal may
display alerts and the process may emit fixed sanitized transition logs.
Neither is an HTTP webhook, email, MQTT publication, or device action.

Notification dispatch belongs to a later separately approved design. Such a
design must choose a fixed channel adapter, prohibit user-supplied URLs and
payloads, use a small bounded attempt queue, finite timeouts, a strict retry cap,
cooldown and deduplication, redacted content, and explicit configuration. Failed
delivery must never change alert truth or trigger remediation.

## Retention, cleanup, and storage failure

The default 30-day policy applies to compacted history and terminal alert
events. Active and acknowledged alerts are never deleted by age. Recovered
alerts become archived only after the 15-minute duplicate cooldown elapses
without recurrence. An archived alert and its events may be deleted only after
`recovered_at` is more than 30 days old. Schema version 1 has no age-based
expiration transition.

Cleanup runs at startup and at most once per hour or after 120 stored history
rows, whichever occurs first. Each opportunity deletes at most 500 oldest
eligible rows and releases at most 128 pages with incremental vacuum. It does
not loop until empty. Deletion order is deterministic by timestamp and primary
key. No automatic full `VACUUM` is allowed because it can require large
temporary space and an unbounded exclusive operation.

When the database approaches its configured limit, cleanup runs once before the
new write. If the file remains full, storage is read-only, the busy timeout
expires, or free-space reserve is insufficient, the transaction fails. Aurora
drops that one persistence attempt, records no in-memory retry queue, emits a
rate-limited fixed log event, and continues serving live health through the
existing path. It must not claim an alert or acknowledgment was persisted.

## Integrity and maintenance budgets

Every integrity or maintenance operation is one attempt with a fixed work and
time budget. A connection progress handler checks an injected monotonic deadline
every 1,000 virtual-machine operations where SQLite exposes that mechanism.
`Connection.interrupt()` is the cancellation mechanism for an operation running
past its deadline when the standard-library call is interruptible. No operation
may fall back to an unrestricted integrity check, full `VACUUM`, repeated
checkpoint, repeated backup, or retry loop.

| Operation | Work and time budget | Interruption and over-budget behavior |
| --- | --- | --- |
| Startup `PRAGMA quick_check` | One `quick_check(1)` against a database no larger than 64 MiB; two seconds. | Progress deadline and `Connection.interrupt()` where effective. Timeout, error, or more than one result marks history unavailable; do not recreate or retry. |
| Routine WAL checkpoint | With automatic checkpoints disabled, at most one `PASSIVE` checkpoint per hour after the prechecked WAL reaches 256 pages and while it is no larger than 960 pages or 4 MiB including framing; one second and the 250-millisecond busy timeout. | Do not call it when the prechecked WAL exceeds either budget. Interrupt where supported; busy or timeout leaves WAL evidence intact. Oversize disables writes for operator review; other failures permit only the next hourly maintenance opportunity, never an immediate retry. |
| SQLite online backup | One source pass, at most 16,384 4-KiB pages or 64 MiB, in batches of 128 pages, with no sleep/retry and a 30-second total deadline. | The backup progress callback checks pages and time and aborts by fixed internal cancellation. Delete only the unpublished destination; retain the source and report fixed failure. |
| Migration preflight | Schema/object query capped at 64 rows, one startup-budget `quick_check(1)`, and one online backup under the preceding budget. | Any exceeded budget stops before migration and leaves the original database untouched. |
| Migration transaction and postflight | One code-owned transaction touching at most 4,096 pages or 16 MiB, five seconds; then at most 64 schema rows and one two-second `quick_check(1)`. | Progress deadline or interrupt rolls back the transaction. Postflight failure keeps history unavailable and preserves the pre-migration backup; no second migration attempt. |
| Restore validation and publication | Candidate at most 64 MiB; at most 64 schema rows; one two-second `quick_check(1)`; one 16,384-page, 30-second backup-API copy to an unpublished destination. | Cancellation leaves the active database and candidate evidence unchanged. Publish only after every check passes; never retry or substitute an empty database. |
| Incremental vacuum | One `incremental_vacuum(128)` call, at most 128 pages or 512 KiB, one second. | Progress deadline or interrupt ends that maintenance opportunity. Do not run full `VACUUM` or loop; ordinary writes may continue only if storage limits still pass. |
| Shutdown maintenance | Five seconds total: at most three seconds to join the scheduler and the remaining time, never more than two seconds, for one `TRUNCATE` checkpoint capped at 960 pages or 4 MiB including framing. | If join or checkpoint exceeds its share, skip remaining maintenance, close when safe, and rely on SQLite recovery. Do not extend shutdown or retry. |

The future implementation must verify which SQLite calls honor the progress
handler or `Connection.interrupt()` on the deployed Python/SQLite build. Where
mid-call cancellation is unavailable, the page/byte input limit and single-call
rule are the hard work bound, and measured worst-case time must pass the
deployment gate before that operation is enabled.

These operations must use the history writer boundary, never the
`HealthService` collection lock. Failure or budget exhaustion disables or
degrades only history work. Public `GET /api/health` remains served by the
existing in-memory service throughout maintenance; during ordinary process
shutdown it remains available only until the server follows its existing
shutdown lifecycle.

## Startup, shutdown, locking, and corruption

The future configuration must require an explicit local database path and must
not search the filesystem. Its parent directory must be operator-created on a
local filesystem, owned by the dedicated Aurora service account, mode `0700`,
and not writable by any other account. The database, WAL, shared-memory file,
advisory lock, and backup artifacts must be owned by that account and mode
`0600`.

### Feasible standard-library path-opening boundary

Python's standard-library `sqlite3` opens an ordinary database by pathname. It
does not accept an already validated file descriptor, expose SQLite's internal
database descriptor, or provide an atomic `openat`/`O_NOFOLLOW` contract for
the database and its sidecars. The design therefore does not claim that
SQLite's internal open is protected by `O_NOFOLLOW`.

The feasible future procedure is:

1. Walk every existing parent component with `lstat` and descriptor metadata;
   reject any symbolic link, non-directory component, or group/world-writable
   component. Intermediate directories may be root- or service-account-owned.
   Require the final parent to be the dedicated service-account-owned `0700`
   directory.
2. Before SQLite opens, inspect the database and the code-derived `-wal` and
   `-shm` names without following links. Reject symlinks, non-regular objects,
   an owner other than the effective service account, a mode other than `0600`,
   or a link count other than one.
3. If the database does not exist, create only that final file with exclusive
   creation, mode `0600`, and `O_NOFOLLOW` where supported, then close it. Never
   ask SQLite to create a missing path or directory.
4. Record the database's device, inode, type, owner, mode, and link count from
   both path and independently opened inspection descriptor. Set process umask
   `0077`, then connect to the validated pathname in existing-file `mode=rw`,
   not create mode.
5. Immediately after connection and before any application-issued pragma,
   schema query, or write, repeat no-follow path and descriptor inspection for
   the database and any sidecars. Require the database device and inode to equal
   the pre-open identity and require every other attribute to remain valid.
6. Recheck database and sidecar identity, ownership, type, link count, and mode
   after startup validation and after every write or maintenance operation.
   A sidecar at any name other than the code-derived WAL/shared-memory names, or
   replacement of a previously observed file identity, is a fixed
   storage-boundary failure. Normal SQLite creation of a missing validated
   sidecar is permitted only inside the protected directory and must pass the
   immediate post-operation checks.

If identity changes during opening or any recheck, close the SQLite connection,
preserve the files, mark history unavailable, and do not retry, recreate, or
continue to a query or transaction. Paths and metadata values are not logged.

This residual pathname-open race is acceptable only inside the protected
`0700` directory when the Aurora service account is dedicated and no untrusted
process shares that identity. Root and the trusted service account are already
inside the documented threat boundary. If deployment permits an untrusted
same-account process or cannot provide the protected local directory, the
standard-library SQLite design is blocked; implementation must use a genuinely
secured and tested VFS/opening mechanism or choose another persistence design.

Startup order is:

1. validate configuration and the pre-open filesystem boundary;
2. acquire scheduler leadership without waiting;
3. precreate when necessary, connect in `mode=rw`, and complete the immediate
   post-open identity checks before application-issued database operations;
4. set or verify the fixed page size, bounded pragmas, and application identity;
5. verify schema version and required objects;
6. run `quick_check(1)` under the explicit integrity budget; and
7. start exactly one scheduler thread only after validation passes.

There is no work queue. Scheduler and future acknowledgment writes use one
shared nonblocking process-local writer gate and short SQLite transactions.
SQLite supplies cross-process serialization. A busy writer causes the scheduled
sample to be skipped or the acknowledgment request to return a fixed temporary
failure; neither caller waits indefinitely.

Shutdown follows the five-second total budget above. It signals the scheduler,
waits for at most three seconds, uses only the remaining budget for one
page-capped checkpoint, and closes connections when safe. A timeout relies on
SQLite transaction atomicity and does not kill a thread or retry indefinitely.

Malformed headers, failed `quick_check`, impossible constraints, invalid pages,
or inconsistent schema metadata mark storage unavailable. Aurora must not
rename, delete, truncate, recreate, or silently replace a corrupt database. The
live health endpoint remains available, while history and alert mutation fail
closed with sanitized diagnostics. Recovery is an explicit operator action
against preserved evidence or a verified backup.

## Clock and timestamp handling

UTC and monotonic clocks have separate jobs:

- an injected aware UTC clock supplies persisted integer microseconds and
  rendered RFC 3339 timestamps;
- an injected monotonic clock schedules samples, runtime cooldown checks, busy
  budgets, and shutdown waits; and
- the integer primary key remains authoritative ordering when UTC values tie or
  move backward.

Persisted timestamps are never taken from a browser, device response, request,
or environment value. A backward or forward wall-clock step records at most one
fixed clock-discontinuity marker and pauses time-based archival until ordering
is safe. The clock marker does not contribute to sampling-gap state; only
monotonic missed scheduler deadlines do. A wall-clock jump does not synthesize
samples, recover or open an alert, or delete retention data.

## Future dashboard and API boundaries

No route is added by this design change. A future implementation should add
history and alert views beside, not inside, the existing public health contract:

- `GET /api/health` remains public, read-only, schema version 1, and independent
  of SQLite;
- new history and alert HTML/API reads require a valid authenticated session;
- history query inputs are limited to code-owned status/component enums,
  validated UTC bounds, an opaque or integer cursor, and a capped page size;
- queries return a separate versioned response containing only persisted
  allowlisted fields;
- all queries use indexed seek pagination, not unbounded offsets, exports,
  arbitrary filters, raw SQL, or caller-selected columns; and
- database unavailability returns a fixed sanitized response without changing
  public health or device controls.

Longitudinal status can reveal household activity patterns even when individual
records are sanitized. It is therefore not added to the unauthenticated public
portal. Bulk download, streaming, CSV export, database download, and raw
diagnostic views are outside the initial boundary.

## Future acknowledgment security

Any acknowledgment action is a mutation and must require:

- dashboard authentication enabled and a valid process-memory session;
- the existing per-session CSRF token and constant-time verification;
- strict form media type, encoding, length, field, and duplication checks;
- one fixed route and one validated generated alert identifier;
- a bounded writer attempt with a fixed result code; and
- a sanitized audit event containing only schema version, fixed event/reason,
  fixed alert kind, and generated alert identifier.

Authentication alone does not enable persistence or acknowledgment. The
initial recommendation is that any authenticated Aurora operator may
acknowledge because the current model has one operator class and no role system.
Acknowledgment stores no username, note, cookie, session digest, CSRF token, or
client identifier. Introducing roles, comments, or delegation requires a later
security design.

## Logging and privacy

History logs are fixed, structured, and rate-limited. Permitted fields are
schema version, fixed event, fixed reason code, fixed component or alert kind,
generated numeric record ID, and bounded counts. Logs must not contain snapshot
messages or details, database or backup paths, SQL statements, endpoint or
capture data, environment values, request data, credentials, session or CSRF
material, raw exceptions, or notification payloads.

Read and write errors map to fixed reason enums. Exception chaining may remain
internal for tests, but exception text must not reach logs, HTML, JSON,
redirects, or alert records.

## Backup and recovery boundary

Milestone 17 backs up only the active Aurora YAML layer. Its backup directory,
manifest schema, retention cap, lock, apply, and rollback commands must not be
reused for the history database. Configuration rollback never rolls back health
or device state, and database recovery never changes configuration.

A future history-backup operation should be explicit and operator-controlled.
It should use exactly one SQLite online-backup pass under the 16,384-page,
64-MiB, 128-page-step, 30-second budget to a code-generated temporary file,
then run the one two-second `quick_check(1)` and 64-row schema check before it
sets restrictive permissions, fsyncs content and the directory, and publishes
atomically. Copying the live main file without its WAL is prohibited. Backup
count and total backup bytes require independent bounds; automatic deletion is
not implied by this design.

Recovery should stop history writes, preserve the corrupt files, validate the
selected backup offline under the 64-MiB, 64-schema-row, and two-second
`quick_check(1)` budgets, copy it to an unpublished destination under the
30-second online-backup budget, atomically install it, and repeat those schema
and integrity checks before resuming. Budget exhaustion leaves the active file
and evidence unchanged. Creating a new empty database after corruption requires
a separate explicit operator choice; Aurora must never do it silently.

## Testing strategy

All automated tests must use injected clocks, temporary protected directories,
synthetic `HealthReport` values, and fault-injected SQLite connections or
filesystem operations. Tests must block real WLED, HyperHDR, capture, DDP, MQTT,
service, subprocess, and outbound network access.

Coverage should include:

- strict projection allowlists and rejection of every prohibited field class;
- schema creation, identity, constraints, version mismatch, migration success,
  migration rollback, and newer-schema refusal;
- parameter binding and rejection of SQL-like input as ordinary invalid data;
- transition, heartbeat, replay, duplicate, debounce, escalation,
  acknowledgment, recovery, cooldown, and archive behavior, plus rejection of
  any schema-version-1 `expired` state;
- missed periods, restart gaps, UTC ties, backward/forward clock movement, and
  monotonic scheduling, including the exact two-missed/two-recovery state
  machine and persistence-failure invariants;
- retention order, row bounds, page bounds, database/WAL limits, checkpoints,
  incremental cleanup, and cancellation at every integrity/maintenance budget;
- full, read-only, locked, corrupt, truncated, wrong-owner, insecure,
  symlinked, hard-linked, and identity-changed storage;
- pre-open and post-open identity checks, valid SQLite-created sidecars, path
  swaps during open, and refusal when the protected-directory/service-account
  assumption is not true;
- one writer, lock contention, concurrent bounded readers, shutdown during a
  transaction, and crash recovery;
- exactly one state transaction per accepted scheduled sample, no hidden retry,
  and measured database, WAL, checkpoint, filesystem, CPU, memory, and growth
  ceilings;
- authenticated read boundaries and authentication-plus-CSRF acknowledgment
  requirements when routes are separately implemented;
- unchanged `GET /api/health` schema version 1 and all existing WLED,
  HyperHDR, session, cache, profile, and filesystem behavior; and
- proof that no rejected or failed operation queues work, mutates a device,
  invokes a service, or leaks snapshot/configuration values.

Property tests should generate only synthetic status sequences, not deployment
configuration. A reference state-machine model should be compared against the
database result for arbitrary bounded sequences.

## Deployment validation plan

Implementation review should require these stages:

1. Run the complete hardware-free suite on a temporary local database.
2. Validate restrictive modes, ownership, symlink rejection, pre-open and
   post-open identity checks, the documented standard-library pathname-open
   limitation, schema identity, WAL handling, and leadership locking on Linux.
3. Replay synthetic healthy, degraded, unavailable, gap, acknowledgment, and
   recovery sequences; verify exact rows and no duplicates.
4. Measure scheduled and maintenance transactions per hour, database and WAL
   bytes written per hour and day, checkpoint bytes, logical growth, filesystem
   write amplification, CPU, and memory under normal and accelerated synthetic
   sampling on a Raspberry Pi 5. Every write-volume acceptance ceiling must
   pass before defaults are approved.
5. Exercise clean restart and abrupt termination at different transaction and
   checkpoint phases. Inject full, read-only, busy, interrupted-write, and
   corrupt-copy failures; verify committed state, bounded WAL recovery, live
   schema-version-1 health availability, and no silent database recreation.
6. Exercise verified backup, migration, restore, and software rollback using
   only synthetic data and isolated operator-owned paths.
7. Confirm all WLED, HyperHDR, capture, DDP, MQTT, configuration-profile,
   service, and hardware operations remain untouched.

Production history enablement, production configuration-profile use, and
outbound notification testing are not implied by this controlled plan.

### Preimplementation validation tooling

Two standard-library-only commands implement the current synthetic gates. They
operate only in an explicit existing `0700` directory or a securely created
temporary directory, use code-generated filenames, redact the test root from
reports, make no network or hardware request, and are not production database,
migration, backup, restore, or enablement commands.

Run the target-platform gate on the Raspberry Pi from a reviewed source tree:

```console
$ umask 077
$ m18_platform_root="$(mktemp -d)"
$ chmod 0700 "${m18_platform_root}"
$ uv run python scripts/validate_m18_sqlite_platform.py \
    --test-dir "${m18_platform_root}" \
    > "${m18_platform_root}/platform-report.json" \
    2> "${m18_platform_root}/platform-summary.txt"
```

The platform report records the Python, `sqlite3` module, and SQLite library
versions; relevant compile options; protected-directory and database identity
checks; symlink, type, hard-link, owner, mode, and identity-change behavior;
WAL and shared-memory permissions; required pragmas; busy timeout; progress
handler and `Connection.interrupt()` behavior; transaction rollback after
interruption; and the quick-check, checkpoint, online-backup,
migration-style rollback, restore, incremental-vacuum, and shutdown budgets.
It explicitly records that standard-library `sqlite3` opens by pathname and
cannot use an already secured ordinary database descriptor. It makes no claim
that SQLite's internal open has `O_NOFOLLOW` protection.

Run each accelerated workload with a fresh protected root or with the distinct
code-owned scenario filenames shown here:

```console
$ umask 077
$ m18_benchmark_root="$(mktemp -d)"
$ chmod 0700 "${m18_benchmark_root}"
$ uv run python scripts/benchmark_m18_sqlite.py \
    --test-dir "${m18_benchmark_root}" --transactions 2880 \
    --scenario healthy \
    > "${m18_benchmark_root}/healthy-report.json" \
    2> "${m18_benchmark_root}/healthy-summary.txt"
$ uv run python scripts/benchmark_m18_sqlite.py \
    --test-dir "${m18_benchmark_root}" --transactions 2880 \
    --scenario mixed \
    > "${m18_benchmark_root}/mixed-report.json" \
    2> "${m18_benchmark_root}/mixed-summary.txt"
$ uv run python scripts/benchmark_m18_sqlite.py \
    --test-dir "${m18_benchmark_root}" --transactions 2880 \
    --scenario transition-heavy \
    > "${m18_benchmark_root}/transition-heavy-report.json" \
    2> "${m18_benchmark_root}/transition-heavy-summary.txt"
$ uv run python scripts/benchmark_m18_sqlite.py \
    --test-dir "${m18_benchmark_root}" --transactions 2880 \
    --scenario gap-recovery \
    > "${m18_benchmark_root}/gap-recovery-report.json" \
    2> "${m18_benchmark_root}/gap-recovery-summary.txt"
```

For the required 24-hour pacing measurement, use a separate fresh protected
root and the fixed 30-second pace; this validates one workload and must be
repeated as the review selects additional worst cases:

```console
$ umask 077
$ m18_endurance_root="$(mktemp -d)"
$ chmod 0700 "${m18_endurance_root}"
$ uv run python scripts/benchmark_m18_sqlite.py \
    --test-dir "${m18_endurance_root}" --transactions 2880 \
    --scenario transition-heavy --pace-milliseconds 30000 \
    > "${m18_endurance_root}/endurance-report.json" \
    2> "${m18_endurance_root}/endurance-summary.txt"
```

Each run emits a concise human summary on standard error and one compact JSON
report on standard output. The protected root also contains only synthetic
benchmark databases and SQLite sidecars. `PASS` means that the individual
fixed probe completed within its implemented budget. `FAIL` means a required
gate failed and the command exits nonzero. `SKIPPED` is permitted only for a
clearly named check that an ordinary service account or deployed interface
cannot perform; it is not success evidence and review decides whether it is
blocking. The benchmark labels every value as measured, projected,
unavailable, architecture limit, or decision pending. A successful command
does not approve the provisional interval, retention, heartbeat, or size
defaults.

These artifacts are synthetic target-platform evidence, not production
enablement. Production SQLite schema and runtime integration, production
backup and restore commands, portal alert grouping, outbound notifications,
and production history enablement remain deferred. Production implementation
remains blocked until the Raspberry Pi reports are reviewed against every
acceptance ceiling and all remaining gates are accepted.

## Threat model and abuse cases

The trusted boundary is one local service account and authenticated local
operators. Root or an administrator who can replace the process, database, or
code is outside the boundary, but accidental exposure and less-privileged local
tampering remain in scope.

| Threat or abuse | Required control |
| --- | --- |
| Snapshot contains an unexpected endpoint, identifier, message, or detail | Project into fixed fields; omit open-ended data before persistence. |
| SQL injection through query or snapshot data | Code-owned SQL and identifiers; parameterized values only; enum validation. |
| Request asks for unbounded history | Hard page limits, seek cursors, indexed filters, and no exports. |
| Repeated samples or requests create an alert storm | Idempotent digest, consecutive thresholds, one-active-alert index, cooldown, and bounded counts. |
| Slow reader blocks ingestion | WAL, read-only connections, bounded queries, short busy timeout. |
| Multiple processes sample simultaneously | Nonblocking scheduler-leadership lock plus SQLite serialization. |
| Disk exhaustion | Main-file cap, WAL checkpoint target, free-space reserve, deterministic bounded cleanup, and fail-closed writes. |
| Corruption is mistaken for an empty database | Application identity, schema checks, `quick_check`, preserved evidence, and no automatic recreation. |
| Symlink or file replacement redirects storage | Protected `0700` directory, exclusive precreation, pre/post no-follow inspection, link-count and identity checks, and explicit rejection on change; no claim that SQLite's internal pathname open is atomic or no-follow. |
| Untrusted process shares the service account | Treat standard-library SQLite path opening as blocked; require a secured tested VFS/open mechanism or another store. |
| Longitudinal status leaks occupancy patterns | Authentication for history surfaces, minimal fields, retention, no public export. |
| Forged acknowledgment | Authenticated session, CSRF, strict form parsing, generated ID validation, and audit. |
| Clock manipulation expires data or hides an outage | Monotonic scheduling, sequence ordering, discontinuity/gap markers, and suspended time-based deletion. |
| Notification target causes SSRF or exfiltration | No outbound notifications initially; later adapters may not accept user-supplied URLs or payloads. |
| Health alert triggers unsafe remediation | No action executor and no connection from alert state to control, profile, service, or device APIs. |

## Pre-implementation gates

Application implementation must not begin until a design review explicitly
marks every gate below accepted. A disposable synthetic measurement or
filesystem-opening prototype may be used to close a gate, but it must remain
in an isolated validation namespace that no runtime entry point imports and may
not create routes, configuration, workers, migrations, alerts, notifications,
or deployment changes.

| Gate | Current design status | Acceptance required before implementation |
| --- | --- | --- |
| Normalized finite reason-code registry | Finalized by the isolated registry and comprehensive synthetic tests in this branch. | Review the exact list above and preserve fail-closed behavior when any health collector contract changes. |
| Exact write-volume strategy | One state transaction per accepted scheduled sample is selected; default rates remain blocking. | Raspberry Pi measurements must pass every transaction, byte, checkpoint, growth, CPU, memory, clean-restart, and abrupt-termination ceiling. |
| SQLite path-opening procedure | Isolated checks exist; target Linux proof remains blocking. | Demonstrate pre/post identity checks and sidecar handling on the target Python/SQLite/Linux build and confirm no untrusted process shares the service account. |
| Integrity and maintenance budgets | Concrete isolated probes exist; target-platform timing and cancellation behavior remain blocking. | Prove progress/interrupt behavior where claimed and measured worst-case completion within each single-call budget. |
| Sampling-gap transitions | Two cumulative misses open; two consecutive on-time committed samples recover; restart and clock rules are specified. | Approve a reference state-machine table and exhaustive synthetic transition tests before translating it to application code. |
| Schema-version-1 lifecycle | Exactly open, acknowledged, recovered, and archived; no expired state. | Approve transition, cooldown, archival, retention, authorization, and audit-event tables before encoding the schema. |

SQLite backup/restore implementation and every outbound notification channel
remain separately deferred. They are not prerequisites for a core history
implementation only if schema migration is also absent; any schema migration
requires the budgeted pre-migration backup design first.

## Recommendations and unresolved decisions

The design recommends the following as the implementation baseline:

- one scheduler inside the dashboard process;
- a disabled-by-default, standard-library SQLite store using WAL;
- 30-second samples, change-plus-15-minute-heartbeat history, 30-day retention,
  and a 64 MiB main-database limit;
- one nonqueued writer path, at most one state transaction per accepted sample,
  and a 250-millisecond busy timeout;
- three-sample degraded, two-sample unavailable, two-sample recovery, and
  15-minute duplicate-cooldown policies;
- authenticated history reads and authentication-plus-CSRF acknowledgment by
  the existing single operator class;
- portal display and sanitized logs only, with outbound delivery deferred;
- explicit sampling-gap records with no catch-up polling; and
- SQLite backups and migrations completely separate from Milestone 17 YAML
  backups.

The following decisions remain open and keep the applicable pre-implementation
gates closed:

1. Whether the proposed 30-second, 30-day, and 64 MiB defaults provide the best
   operational tradeoff after the complete Raspberry Pi write-volume and
   storage-endurance acceptance run.
2. Whether overall and component alerts should both be displayed or whether
   the portal should visually group an overall alert with its component causes.
3. Whether the documented standard-library pathname-open procedure and
   progress/interrupt behavior pass target Linux tests; failure requires a
   different secured VFS/open mechanism or persistence design.
4. The operator command, backup count, and byte cap for explicit SQLite backup
   and restore; these will not reuse Milestone 17 artifacts.
5. Which fixed outbound notification channel, if any, deserves a later design.
   No outbound channel is authorized by Milestone 18's initial implementation.

None of these open decisions authorizes implementation or broadens the safe
automation boundary in this document.
