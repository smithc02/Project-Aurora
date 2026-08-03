# Local configuration profiles, backups, and rollback

Milestone 17 adds a CLI-only boundary for Project Aurora's local YAML layer. A
profile is one complete Aurora YAML document selected by a logical identifier.
Applying a profile replaces only the active YAML file on disk. It does not alter
the running process snapshot, environment variables, devices, or services.

Ordinary precedence remains:

```text
CLI overrides > current process environment > YAML > built-in defaults
```

Profiles and backups contain only YAML. Environment and future CLI overrides
remain authoritative and are neither copied into profiles nor backed up. Aurora
does not read dotenv files, systemd `EnvironmentFile` content, shell profiles,
or other credential sources for profile management.

## Full-profile model and identifiers

Profiles are complete YAML documents, not patches. There is no fragment,
inheritance, include, template, interpolation, remote source, profile chain, or
implicit merge with the active YAML. Candidate bytes replace active YAML bytes
exactly after validation.

Every profile must contain its selected identifier:

```yaml
application:
  configuration_profile: maintenance
```

The raw value must exactly equal `--profile`. Identifiers contain 1–40 lowercase
ASCII letters, digits, and isolated hyphens; they begin and end with a letter or
digit. `home-theater`, `maintenance`, and `diagnostics-2` are valid. Paths,
filenames, extensions, periods, slashes, whitespace, Unicode, uppercase,
underscores, percent encoding, leading/trailing hyphens, and repeated hyphens
are rejected.

Code maps a valid identifier only to:

```text
<profiles directory>/<identifier>.yaml
```

The operator cannot supply a profile filename, extension, profile path, or URL.

## Filesystem boundary

Every command receives paths explicitly. Aurora does not search for active
configuration, profiles, or backups and never creates their directories.

The boundary rejects symlinks and symlink path components, non-regular files,
hard-linked managed files, changed identities, wrong owners, and insecure
permissions. Opens use no-follow flags where supported, and metadata is checked
through the descriptor and after reads.

| Object | Required mode |
| --- | --- |
| Profile directory | `0700` |
| Backup directory | `0700` |
| Profile, active, and backup YAML | `0600` |
| Backup manifest | `0600` |
| Mutation lock | `0600` |

Profile and backup directories and their files must be owned by the effective
operator. The active YAML must also be operator-owned and have exactly one hard
link. Its parent may have ordinary deployment permissions, but it must be a real
non-group/world-writable directory.

Fixed bounds are 256 KiB per YAML file, 16 KiB per manifest, 256 inspected
profile entries, 512 inspected backup entries, and 256 reported changed paths.
Commands never recursively walk unrelated directories.

## Two validation stages

Profiles, active configurations, and selected rollback data pass two stages
without connectivity or hardware checks:

1. **Raw validation:** strict UTF-8, exactly one mapping document, no duplicate
   keys at any depth, aliases, unsupported tags, malformed YAML, or non-mapping
   roots; then direct `AuroraSettings` validation and profile-ID matching.
2. **Effective validation:** the same candidate is loaded through the existing
   `load_settings()` path with the invoking process environment and no
   additional CLI setting overrides.

Errors use fixed reason codes and never include YAML values, hosts, ports tied
to hosts, capture identifiers, usernames, password hashes, MQTT credentials,
cookies, tokens, environment values, contents, Pydantic inputs, or exceptions.

## CLI registry

### List

```bash
uv run aurora config profile list \
  --profiles-dir <protected-profile-directory>
```

Only secure regular `<identifier>.yaml` entries are listed in lexical order.
Content is not parsed or printed. Other entries contribute only to a fixed
skipped count, and enumeration is bounded.

### Validate

```bash
uv run aurora config profile validate \
  --profiles-dir <protected-profile-directory> \
  --profile maintenance
```

This performs raw and effective validation without reading the active file or
writing anything.

### Plan

```bash
uv run aurora config profile plan \
  --config <active-aurora-yaml> \
  --profiles-dir <protected-profile-directory> \
  --profile maintenance
```

The plan prints active/candidate SHA-256 digests, byte identity, and sorted key
paths marked `added`, `removed`, or `changed`. It never prints values,
serialized YAML, or a raw diff. Lists appear only as indexes. The report stops
after 256 paths and marks truncation.

### Apply

```bash
uv run aurora config profile apply \
  --config <active-aurora-yaml> \
  --profiles-dir <protected-profile-directory> \
  --backups-dir <protected-backup-directory> \
  --profile maintenance \
  --confirm-apply maintenance \
  --maximum-backups 20
```

Confirmation must exactly equal the profile ID. The backup cap defaults to 20
and accepts only 1–100. Reaching it refuses before changing the active file.
Backups are never automatically deleted.

Apply takes the shared nonblocking lock, validates active and candidate bytes,
detects changes between validation and copying, and returns a no-op without a
backup for byte-identical files. Otherwise it durably creates the exact backup
pair before atomic activation.

Success changes only YAML. The externally managed Aurora service must be
restarted separately under the operator's normal procedure. Aurora never
executes a service command, restarts itself, or reloads the running process.

### Backups

```bash
uv run aurora config profile backups \
  --backups-dir <protected-backup-directory>
```

Valid managed records are newest first. Output contains only backup ID, UTC
timestamp, SHA-256, exact byte count, operation, optional target ID, and
integrity status. It never prints paths, YAML, or raw JSON. Malformed,
incomplete, or insecure records are skipped with a fixed count.

### Rollback

```bash
uv run aurora config profile rollback \
  --config <active-aurora-yaml> \
  --backups-dir <protected-backup-directory> \
  --backup-id <generated-backup-id> \
  --confirm-rollback <same-generated-backup-id> \
  --maximum-backups 20
```

Both backup arguments must exactly match one generated ID. Aurora resolves only
the code-generated pair, validates integrity and raw/effective configuration,
and creates an exact pre-rollback backup. Rollback is therefore reversible. A
successful rollback also requires a separate external service restart.

## Backup format and corruption detection

Backup IDs use an injected UTC clock and secure random source:

```text
YYYYMMDDTHHMMSSffffffZ-<12 lowercase hexadecimal characters>
```

Each creates exactly `<backup-id>.yaml` and `<backup-id>.json`. YAML is the exact
previous active byte sequence. The compact UTF-8 manifest rejects unknown and
duplicate fields and contains only:

```json
{
  "schema_version": 1,
  "backup_id": "<generated ID>",
  "created_at_utc": "<canonical UTC timestamp>",
  "source_sha256": "<lowercase SHA-256>",
  "source_byte_count": 0,
  "operation": "apply",
  "target_profile_id": "maintenance",
  "target_backup_id": null
}
```

Rollback-created manifests use `operation: rollback`, a null profile target,
and the selected managed backup ID. No path, user, host, endpoint, YAML or
environment value, credential, reason text, or arbitrary metadata is stored.

## Atomic activation, recovery, and locking

The reusable writer creates an exclusive code-named temporary file beside the
destination, sets `0600`, writes all bytes, checks the count, flushes and fsyncs,
rechecks active identity, publishes with `os.replace`, and fsyncs the directory.
An unpublished temporary is removed after failure.

Aurora then reopens without following links, verifies exact bytes and SHA-256,
and repeats raw/effective validation. Any post-publication failure triggers one
atomic restoration and validation of the exact pre-operation bytes. Verified
restoration exits 4. If restoration cannot be completed and verified, exit 5
reports unknown active validity and retains backup evidence. There is no
indefinite retry or service action.

Apply and rollback share `.aurora-config.lock` in the explicit backup directory.
The empty `0600` lock contains no PID, user, path, configuration, or secret and
may remain after process exit. Contention exits 3 immediately without queuing.

| Exit | Meaning |
| --- | --- |
| `0` | Success, including byte-identical no-op. |
| `2` | Invalid argument, profile, backup, configuration, confirmation, capacity, or filesystem boundary. |
| `3` | Mutation lock busy. |
| `4` | Mutation failed; exact previous configuration restored and verified. |
| `5` | Mutation failed; automatic restoration incomplete or unverified. |

## Deployment, recovery, and trust

1. Explicitly create protected profile and backup directories with `0700`.
2. Install reviewed complete profiles as `0600` files whose filename and
   `application.configuration_profile` match.
3. Restrict active YAML to `0600` and one effective-operator-owned hard link.
4. Prepare the intended `AURORA_` environment, then validate and plan.
5. Apply with exact confirmation and review the generated backup listing.
6. Restart the externally managed service separately and perform normal health
   verification.

Exit 4 confirms exact recovery. Exit 5 requires stopping deployment, retaining
evidence, checking managed backup integrity, and choosing a known-valid rollback
under controlled conditions. To roll back the software feature itself, stop
using these commands, retain configuration/backups, and deploy the prior Aurora
release; there is no database or migration.

This boundary assumes a trusted local operator with filesystem access. It does
not defend against an administrator who can replace the running program.

## Controlled Linux filesystem validation

Controlled validation passed on the Linux deployment target using only
synthetic files in an isolated, temporary, operator-owned directory. It
confirmed:

- `0700` profile and backup directories and `0600` active, profile, backup, and
  manifest files;
- profile listing, raw and effective validation, and sanitized planning that
  disclosed only changed key paths and digests;
- atomic apply, an exact active/profile byte match, an exact previous-active
  backup, SHA-256 verification, strict manifest listing, and valid integrity;
- byte-identical no-op behavior without an additional backup, plus rollback to
  the exact original bytes with a new integrity-valid, reversible pre-rollback
  backup;
- backup-cap refusal with exit 2 and lock contention with exit 3, both leaving
  the active file and backup count unchanged; and
- corrupted-backup listing as integrity-invalid and rollback refusal with exit
  2, without changing the active file.

This was not a production configuration deployment. No production
configuration, service or systemd unit, dashboard process, environment file,
network endpoint, WLED device, HyperHDR service, capture device, DDP output,
MQTT service, or physical hardware state was read or changed.

## Explicit non-goals

Milestone 17 adds no dashboard/browser editing, HTTP route, environment-file or
environment-variable editing, service/systemd/process operation, self-reload,
WLED or HyperHDR configuration backup, device-state restoration, device
mutation, capture/DDP/MQTT/network operation, remote profile, Git action,
scheduled switching, automation, game/content profile, shell, or subprocess.
Milestone 25 remains the future game/content-profile milestone.

Milestone 17 is completed, merged, and deployed, and its controlled Linux
filesystem validation has passed. This status does not assert that production
profiles were created or activated.
