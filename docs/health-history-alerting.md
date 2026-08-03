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
a short bounded busy timeout. WAL is preferred over a rollback journal because
future bounded history reads should not block the scheduled writer during its
short commit, and a failed reader should not delay sampling. The tradeoff is
the additional WAL and shared-memory sidecars, which must be included in
permission, capacity, backup, checkpoint, shutdown, and recovery handling.

Transactions remain small, one writer gate prevents process-local contention,
automatic checkpoints are page-bounded, and an explicit truncate checkpoint is
attempted only during bounded maintenance or clean shutdown. WAL is not a
license for network filesystems or multiple uncoordinated writer services.

## Recommended resource envelope

These are proposed implementation defaults, not current configuration fields:

| Policy | Recommendation | Required bound |
| --- | --- | --- |
| Scheduled sample interval | 30 seconds | Configurable 5–300 seconds; must be at least the dashboard refresh interval. |
| Unchanged heartbeat interval | 15 minutes | From one sample interval through 24 hours. |
| History retention | 30 days | Configurable 1–365 days. |
| Main database limit | 64 MiB | Configurable 8–512 MiB. |
| WAL target | Checkpoint at 1 MiB; truncate by 4 MiB | Leave additional filesystem reserve; fail writes rather than grow without bound. |
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
status, bounded consecutive count, last sample and heartbeat times, current
alert reference, and cooldown deadline. It is updated on every accepted sample
even when snapshot history is compacted. It contains no raw snapshot.

### `alerts`

- integer primary key;
- fixed scope and alert kind;
- lifecycle state: open, acknowledged, recovered, expired, or archived;
- severity;
- opened, acknowledged, recovered, expired, and archived UTC times as nullable
  columns;
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
verify a separate SQLite backup using the SQLite backup API. The migration then
runs under one exclusive transaction, updates `user_version` and the migration
ledger atomically, runs bounded structural checks, and commits. Failure rolls
back the transaction and leaves the pre-migration evidence intact.

There are no automatic down migrations and no silent destructive repair. An
older Aurora release must refuse a newer schema. Software rollback therefore
requires restoring the verified pre-migration history backup or disabling the
history feature; it must not alter the active Aurora YAML or device state.

## Snapshot ingestion and deduplication

The scheduler asks the shared `HealthService` for one report at each monotonic
deadline. It never performs catch-up polls. The projection is validated and
canonically encoded in fixed field order before its digest is calculated.

Within one transaction the writer:

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
plus periodic heartbeats, not every sample. Debounce counters still observe
every accepted sample through `evaluation_state`, so compaction does not weaken
transition policy.

If a dashboard request recently refreshed the cache, the scheduler may ingest
that same report once. Repeated cache reads with the same observation time and
digest are idempotent and do not inflate counters or history.

## Missed sampling periods

Scheduling uses monotonic deadlines. A delayed scheduler performs one current
sample and records a capped count of missed intervals; it never launches a
burst of overdue collections. A process restart compares the last persisted UTC
observation with startup time and may add one startup-gap marker. It does not
fabricate health for the missing period or backfill samples.

A gap does not count toward healthy recovery. Two consecutive missed intervals
are the recommended threshold for a sampling-gap alert. One later successful
sample closes the gap condition after the normal two-sample recovery rule.

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
4. **Expired:** reserved for a future explicit policy-retirement decision; the
   initial implementation must not expire an active problem merely because it
   is old.
5. **Archived:** recovered or explicitly expired records become read-only and
   are eligible for retention cleanup.

An acknowledged alert that escalates from degraded to unavailable returns to
open and records a fixed escalation event. A repeat condition during cooldown
increments the existing alert's bounded episode count; after cooldown it creates
a new alert. The partial unique index and transaction enforce duplicate
prevention even if evaluation is invoked twice.

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
alerts first become archived, then their records may be removed when both the
alert and all events are beyond retention.

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

## Startup, shutdown, locking, and corruption

The future configuration must require an explicit local database path and must
not search the filesystem. Its parent directory should be operator-created,
owned by the service account, mode `0700`, local to the Raspberry Pi, and not
group/world writable. The database, WAL, shared-memory file, advisory lock, and
backup artifacts must be mode `0600`; symlinks, non-regular files, hard-linked
managed files, wrong ownership, and changed identities are rejected.

Startup order is:

1. validate configuration and filesystem boundaries;
2. acquire scheduler leadership without waiting;
3. open SQLite with the expected application identity and bounded pragmas;
4. verify schema version and required objects;
5. run one bounded `quick_check`; and
6. start exactly one scheduler thread only after validation passes.

There is no work queue. Scheduler and future acknowledgment writes use one
shared nonblocking process-local writer gate and short SQLite transactions.
SQLite supplies cross-process serialization. A busy writer causes the scheduled
sample to be skipped or the acknowledgment request to return a fixed temporary
failure; neither caller waits indefinitely.

Shutdown signals the scheduler, waits at most five seconds for its current
bounded operation, attempts one bounded checkpoint, and closes connections. A
timeout relies on SQLite transaction atomicity and does not kill a thread or
retry indefinitely.

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
or environment value. A backward wall-clock step records one fixed clock-
discontinuity marker and pauses time-based archival until ordering is safe. A
forward step produces at most one bounded gap marker. It does not synthesize
samples, immediately expire active alerts, or delete retention data solely
because the wall clock jumped.

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
It should use SQLite's online backup API to a code-generated temporary file,
run an integrity check, set restrictive permissions, fsync content and the
directory, and publish atomically. Copying the live main file without its WAL
is prohibited. Backup count and total backup bytes require independent bounds;
automatic deletion is not implied by this design.

Recovery should stop history writes, preserve the corrupt files, validate the
selected backup offline, atomically install it, verify schema and integrity,
and then resume. Creating a new empty database after corruption requires a
separate explicit operator choice; Aurora must never do it silently.

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
  acknowledgment, recovery, cooldown, expiration, and archive behavior;
- missed periods, restart gaps, UTC ties, backward/forward clock movement, and
  monotonic scheduling;
- retention order, row bounds, page bounds, database/WAL limits, checkpoints,
  and incremental cleanup;
- full, read-only, locked, corrupt, truncated, wrong-owner, insecure,
  symlinked, hard-linked, and identity-changed storage;
- one writer, lock contention, concurrent bounded readers, shutdown during a
  transaction, and crash recovery;
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
2. Validate restrictive modes, ownership, no-follow behavior, schema identity,
   WAL handling, and bounded leadership locking on Linux.
3. Replay synthetic healthy, degraded, unavailable, gap, acknowledgment, and
   recovery sequences; verify exact rows and no duplicates.
4. Measure CPU, memory, write volume, checkpoint duration, query latency, and
   database growth under accelerated synthetic sampling on a Raspberry Pi 5.
5. Inject full, read-only, busy, interrupted-write, and corrupt-copy failures;
   verify live schema-version-1 health remains available and no database is
   silently recreated.
6. Exercise verified backup, migration, restore, and software rollback using
   only synthetic data and isolated operator-owned paths.
7. Confirm all WLED, HyperHDR, capture, DDP, MQTT, configuration-profile,
   service, and hardware operations remain untouched.

Production history enablement, production configuration-profile use, and
outbound notification testing are not implied by this controlled plan.

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
| Symlink or file replacement redirects storage | Restrictive ownership/modes, no-follow opens, link-count and identity rechecks. |
| Longitudinal status leaks occupancy patterns | Authentication for history surfaces, minimal fields, retention, no public export. |
| Forged acknowledgment | Authenticated session, CSRF, strict form parsing, generated ID validation, and audit. |
| Clock manipulation expires data or hides an outage | Monotonic scheduling, sequence ordering, discontinuity/gap markers, and suspended time-based deletion. |
| Notification target causes SSRF or exfiltration | No outbound notifications initially; later adapters may not accept user-supplied URLs or payloads. |
| Health alert triggers unsafe remediation | No action executor and no connection from alert state to control, profile, service, or device APIs. |

## Recommendations and unresolved decisions

The design recommends the following as the implementation baseline:

- one scheduler inside the dashboard process;
- a disabled-by-default, standard-library SQLite store using WAL;
- 30-second samples, change-plus-15-minute-heartbeat history, 30-day retention,
  and a 64 MiB main-database limit;
- one nonqueued writer path with a 250-millisecond busy timeout;
- three-sample degraded, two-sample unavailable, two-sample recovery, and
  15-minute duplicate-cooldown policies;
- authenticated history reads and authentication-plus-CSRF acknowledgment by
  the existing single operator class;
- portal display and sanitized logs only, with outbound delivery deferred;
- explicit sampling-gap records with no catch-up polling; and
- SQLite backups and migrations completely separate from Milestone 17 YAML
  backups.

The following decisions remain open for implementation review and measured
Raspberry Pi validation:

1. Whether the proposed 30-second, 30-day, and 64 MiB defaults provide the best
   operational tradeoff after accelerated write and SD-card endurance tests.
2. The exact finite normalized reason-code registry derived from current
   `HealthReport.details`; unknown fields will remain excluded regardless.
3. Whether overall and component alerts should both be displayed or whether
   the portal should visually group an overall alert with its component causes.
4. The exact explicit policy-retirement condition that may use the reserved
   `expired` lifecycle; active alerts will never expire merely due to age.
5. The operator command, backup count, and byte cap for explicit SQLite backup
   and restore; these will not reuse Milestone 17 artifacts.
6. Which fixed outbound notification channel, if any, deserves a later design.
   No outbound channel is authorized by Milestone 18's initial implementation.

None of these open decisions authorizes implementation or broadens the safe
automation boundary in this document.
