# Single-zone baseline-path proof and deployment runbook

Milestone 11 defines an operator-controlled proof procedure and evidence record
for the intended baseline path:

```text
PS5
└─ EZCOO HDMI splitter
   ├─ Output 1: direct high-bandwidth HDMI
   │  └─ LG C9
   │     └─ eARC / audio equipment
   └─ Output 2: scaled 1080p60
      └─ Hagibis MS2130 capture device
         └─ Raspberry Pi 5 / HyperHDR
            └─ DDP over Ethernet
               └─ QuinLED Dig-Quad / WLED
                  └─ one configured WS2815 lighting zone
```

This document does not claim that the physical baseline has passed. Complete it
only during an attended deployment session, using operator-controlled private
records for deployment-specific values and evidence.

Creating, reviewing, or testing this documentation performs no capture, network,
DDP, WLED, HyperHDR, electrical, or other hardware operation. The commands below
are references for a later informed operator session; they are never run by this
runbook or by CI.

## Purpose and completion meaning

Milestone 11 defines how an operator proves the baseline path through recorded
command results and observed physical behavior. Passing this runbook means that
the single-zone architecture was demonstrated under the recorded hardware,
software, content, configuration, and environmental conditions.

Passing does not prove universal compatibility, long-term reliability, safe
electrical installation, guaranteed UDP delivery or blackout, or multi-zone
readiness. It does not turn the existing validation commands into a runtime
adapter, and it does not establish performance outside the recorded conditions.

## Scope and prohibited scope

Included:

- one source device;
- one direct HDMI display and audio path;
- one secondary scaled capture path;
- one explicitly configured capture device;
- one HyperHDR instance;
- one WLED controller;
- exactly one enabled lighting zone;
- existing configuration, read-only/query-only, and bounded validation
  commands;
- operator-observed live HyperHDR-to-WLED behavior; and
- bounded recovery and blackout checks.

Excluded:

- multiple zones or multi-zone orchestration;
- custom runtime adapters or continuous Project Aurora DDP output;
- replacement of HyperHDR or custom image processing;
- MQTT frame data;
- automated device mutation, remote discovery, or scanning;
- electrical installation approval;
- unattended soak automation;
- performance claims beyond the recorded test conditions; and
- animation, polling services, background transmission, watchdogs, signal
  handlers, threads, or subprocesses added by Project Aurora.

Project Aurora must remain outside the direct PS5-to-TV high-bandwidth path.
HyperHDR remains responsible for capture, screen-color extraction, and the
operator-controlled live real-time color test. WLED remains responsible for
physical LED control. Project Aurora does not mutate WLED in this runbook.

## Validation boundaries

The stages intentionally use different evidence types:

| Evidence type | Project Aurora action | What it does not establish |
| --- | --- | --- |
| Configuration validation | Parses and validates one explicitly selected local settings file | Connectivity, hardware state, or physical correctness |
| Capture-device metadata | Checks one configured identifier without opening the node | Capture capability or signal |
| Query-only V4L2 checks | Opens one configured node for bounded capability or mode ioctls | Streaming stability, visual correctness, or HDMI properties |
| Read-only information checks | Requests sanitized HyperHDR or WLED information | Live capture, color accuracy, DDP receipt, or LED output |
| Bounded one-shot I/O | Attempts one wiped capture frame or one low-intensity DDP frame plus blackout | Continuous operation, physical delivery guarantees, or long-term stability |
| Operator-observed live behavior | Uses HyperHDR's supported configuration and controls | New Project Aurora runtime functionality or universal compatibility |

Follow the detailed safety and interpretation limits in
[capture-device validation](capture-device-validation.md),
[capture capability validation](capture-capability-validation.md),
[capture mode enumeration](capture-mode-enumeration.md),
[capture-frame validation](capture-frame-validation.md),
[HyperHDR validation](hyperhdr-validation.md),
[WLED validation](wled-validation.md), and
[bounded DDP output validation](ddp-output-validation.md).

## Required operator-supplied facts

Record actual observations at test time. Keep endpoint values, capture paths,
credentials, screenshots, and other private deployment details in an
operator-controlled private record, not in tracked repository files.

| Required fact | Operator record |
| --- | --- |
| Test date and operator | `<record at test time>` |
| Source device and tested application/content | `<record at test time>` |
| TV/display model | `<record at test time>` |
| Audio equipment | `<record at test time>` |
| Splitter model and relevant operator-selected mode | `<record at test time>` |
| Capture-device identifier | `<record privately at test time>` |
| Observed capture format | `<record at test time>` |
| Observed capture dimensions | `<record at test time>` |
| Observed capture frame rate | `<record at test time>` |
| Observed capture pixel format | `<record at test time>` |
| Raspberry Pi model and OS version | `<record at test time>` |
| HyperHDR version | `<record at test time>` |
| WLED version | `<record at test time>` |
| Controller model | `<record at test time>` |
| Measured LED count | `<record at test time>` |
| Zone name and physical orientation | `<record at test time>` |
| Network transport type | `<record at test time>` |
| Observed direct-path video and audio modes | `<record at test time>` |
| Relevant cable or adapter identifiers | `<record privately at test time>` |
| Failures, deviations, and unresolved observations | `<record at test time>` |

## Preconditions and stop conditions

All preconditions must be confirmed before Stage 0:

- The electrical installation has been completed and inspected separately by
  appropriately qualified people.
- Fusing, wire sizing, grounding, enclosure, strain relief, ventilation, and
  power injection have been handled outside this software runbook.
- The LEDs and controller begin in a known safe off/black state.
- The operator has physical access to the installation, its normal safe power
  control, and a manual shutdown method.
- Deployment configuration is untracked, contains no secrets in tracked files,
  and identifies devices explicitly without discovery.
- Exactly one lighting zone is enabled.
- The installed LED count has been measured. It is from 1 through 512 inclusive
  before the existing bounded DDP command is considered.
- DDP remains disabled until the operator is ready for Stage 8 and directly
  observing the intended zone.
- The test is attended throughout; no command or live output is left running
  unattended.
- The intended direct HDMI and audio modes, test content, and evidence location
  have been selected and recorded before testing.

Stop immediately if any of the following occurs:

- unexpected heat, smell, smoke, noise, sparking, unstable power, or wiring
  movement;
- degradation of the direct HDMI path;
- loss of expected HDR, VRR, eARC, Atmos, or other intended audio behavior;
- unexpected LED intensity, color, pattern, or persistent output;
- the wrong zone or any unintended device responds;
- repeated reboot, network instability, or a controller fault; or
- a result cannot be confidently attributed to the explicitly configured
  device.

On a stop condition, do not continue to the next stage. Use the conservative
[recovery and rollback](#recovery-and-rollback) sequence, classify the run as
`BLOCKED` or `NOT PROVEN`, and record the observation before retrying.

## Staged proof procedure

Each stage has an explicit gate. A failed or unknown stage prevents a `PROVEN`
result.

### Stage 0 — Configuration and safety review

**Entry criteria:** All runbook preconditions are confirmed. The local
deployment configuration is untracked, DDP is disabled, and no hardware command
is running.

**Operator action:** Review the untracked local configuration. Confirm exactly
one enabled lighting zone, its measured LED count, and an explicitly configured
DDP destination without copying that destination into evidence. Confirm that no
secret or personal network value will be committed. Run configuration validation
only: `uv run aurora config validate --config <untracked-local-config>`.

**Expected evidence:** A sanitized configuration-validation result, a private
configuration revision reference, confirmation of one enabled zone, and the
recorded measured count. Configuration validation performs no hardware or
network I/O.

**Pass criteria:** Configuration is valid, the single-zone facts agree with the
measurement, DDP remains disabled, and the safety review has no unknown item.

**Fail criteria:** Invalid configuration, zero or multiple enabled zones,
missing or unmeasured count, tracked secrets or deployment values, or an
unresolved safety precondition.

**Recovery action:** Correct only the untracked configuration or installation
record. Do not proceed until the review can be repeated cleanly.

### Stage 1 — Direct HDMI path independence

**Entry criteria:** Stage 0 passed. Aurora capture, network-output, and lighting
equipment is disconnected from the secondary path or inactive. The direct
splitter output remains connected normally to the TV and audio equipment.

**Operator action:** With representative PS5 content, observe the TV and audio
equipment directly. Verify the intended 4K120, VRR, HDR, eARC, and Atmos behavior
where supported by the selected content and equipment. Record resolution,
refresh rate, HDR state, VRR state, eARC/audio state, audio format when available,
and every dropout, handshake loop, delay, or degradation.

**Expected evidence:** Operator observations or private display/audio status
records captured while Aurora is inactive. Project Aurora software cannot
automatically verify any of these properties.

**Pass criteria:** The intended direct-path video and audio modes operate without
an unexplained dropout, handshake loop, delay, or degradation while Aurora is
outside the path.

**Fail criteria:** Any intended mode is lost or degraded, or its state cannot be
observed confidently.

**Recovery action:** Stop the runbook and restore the known-safe direct path.
Resolve splitter, display, audio, content, or cabling behavior outside Project
Aurora before repeating this stage.

### Stage 2 — Capture-device identity and presence

**Entry criteria:** Stages 0–1 passed. The intended capture identifier is set in
the untracked configuration.

**Operator action:** Run `uv run aurora hardware validate capture-device
--config <untracked-local-config>`. Record the configured identifier only in the
private evidence record and retain only the sanitized command result in shared
notes.

**Expected evidence:** One sanitized result for the explicitly configured
identifier. The command performs bounded metadata checks and does not open the
node.

**Pass criteria:** The command reports `healthy`, and the operator can attribute
the result to the intended capture device.

**Fail criteria:** `disabled` or `unhealthy`, an ambiguous identity, or any need
to commit a machine-specific path.

**Recovery action:** Stop and reconcile the configured identifier through normal
OS and device administration. Do not scan or auto-select a replacement.

### Stage 3 — V4L2 capability validation

**Entry criteria:** Stage 2 passed and the same configured capture identifier is
still selected.

**Operator action:** Run `uv run aurora hardware validate capture-capability
--config <untracked-local-config>`. Record the sanitized capability result.

**Expected evidence:** The selected node reports ordinary video capture and
read/write I/O suitable for the planned bounded frame path. Record whether the
command reports single-planar or multi-planar capture and read/write capability.

**Pass criteria:** The result is `healthy`, capture capability is present, and
read/write I/O is explicitly reported for the later bounded frame check.

**Fail criteria:** A non-healthy result, no ordinary capture capability, no
read/write I/O for the planned path, or an unattributable result.

**Recovery action:** Stop and reconcile driver, permissions, or device selection
through normal supported administration. `VIDIOC_QUERYCAP` does not prove a
signal, usable format, frame acquisition, or HyperHDR compatibility.

### Stage 4 — Capture mode enumeration

**Entry criteria:** Stage 3 passed.

**Operator action:** Run `uv run aurora hardware validate capture-modes --config
<untracked-local-config>`. From the sanitized report, identify an actually
reported scaled capture mode suitable for the baseline. Record 1080p60 only if
the device reports that dimensions/frame-rate combination; do not assume it.

**Expected evidence:** A reported format, dimensions, exact interval or frame
rate, pixel format, queue type, and any bounded-enumeration limit or gap.

**Pass criteria:** A suitable complete reported mode is identified without an
unresolved enumeration gap.

**Fail criteria:** No suitable mode, an unknown required field, a limit that
prevents confident selection, or a driver report that cannot be attributed to
the selected device.

**Recovery action:** Stop and resolve capture-device, splitter-scaling, or driver
compatibility outside Project Aurora. Enumeration is a driver claim and does
not prove that a frame can be acquired.

#### Pre-Stage 5 reconnection and signal-present gate

This attended gate is mandatory. Passing device identity, capability, or
advertised-mode checks does not establish that a live HDMI input is present and
is not sufficient to proceed to Stage 5.

**Operator action:**

1. If it was disconnected or disabled during Stage 1, restore the splitter's
   secondary scaled 1080p60 output to the Hagibis MS2130 capture input.
2. Confirm that the physical secondary branch is connected in the documented
   order: splitter secondary output, capture device, then Raspberry Pi 5 /
   HyperHDR. Aurora remains outside the direct LG C9/eARC branch.
3. Confirm that the source and splitter are producing the intended scaled
   capture signal.
4. Confirm that the capture device reports a live signal, or otherwise
   demonstrate that input is present using the supported read-only inspection
   method available for the deployment. Record the method and sanitized
   observation. Device metadata and advertised modes alone do not satisfy this
   gate.
5. Confirm that restoring the secondary branch did not alter or interrupt the
   direct high-bandwidth LG C9/eARC path.

**Expected evidence:** An attended record that the secondary branch was
restored in the documented order, the intended scaled input was present, and
the direct branch remained independent and uninterrupted. Record this evidence
before running `capture-frame`.

**Pass criteria:** The secondary branch and live input are confirmed, and the
direct branch continues to provide its intended video and audio behavior.

**Fail criteria:** The branch or input cannot be restored or confidently
confirmed, restoring it affects the direct branch, or the observed input cannot
be attributed to the intended source and splitter output.

**Recovery action:** Do not run `capture-frame`. Stop and classify the run as
BLOCKED when a safety, hardware, compatibility, or environmental issue prevents
continuation, or NOT PROVEN when the gate fails. Resolve and document the issue
before retrying this attended gate.

This gate introduces no continuous capture, network transmission, WLED
mutation, or uncontrolled hardware operation.

### Stage 5 — Bounded single-frame validation

**Entry criteria:** Stage 4 and the mandatory reconnection and signal-present
gate passed. The device's current format is recorded; no streaming capture
process is using the selected node.

**Operator action:** Run `uv run aurora hardware validate capture-frame --config
<untracked-local-config>`. Record only the sanitized result and reported current
format metadata.

**Expected evidence:** One bounded read/write frame attempt under the reported
current format, followed by mandatory transient-buffer wiping and descriptor
cleanup.

**Pass criteria:** The command reports `healthy`, confirms a bounded frame was
received, confirms buffer wiping, and confirms descriptor closure.

**Fail criteria:** `disabled`, `degraded`, or `unhealthy`; a current-format
mismatch; an unconfirmed wipe or close; or any result that cannot be attributed
to the selected node.

**Recovery action:** Stop capture work and resolve the reported condition before
retrying. This stage does not prove streaming stability, visual correctness,
HDMI properties, or HyperHDR operation, and it retains no frame data.

### Stage 6 — HyperHDR read-only validation

**Entry criteria:** Stage 5 passed. The intended HyperHDR endpoint is enabled in
the untracked configuration, and no live output test is active.

**Operator action:** Run `uv run aurora hardware validate hyperhdr --config
<untracked-local-config>`. Record the sanitized result and only the version or
status information that the existing command supports. Keep endpoint details
private.

**Expected evidence:** One sanitized read-only `serverinfo` validation result and
any supported reported status metadata.

**Pass criteria:** The command reports `healthy` and the operator can attribute
the response to the intended HyperHDR instance.

**Fail criteria:** `disabled` or `unhealthy`, authentication or schema failure,
or uncertain endpoint attribution.

**Recovery action:** Stop and use HyperHDR's normal supported administration to
resolve availability. This command does not validate live capture, color
accuracy, WLED receipt, LED output, or the complete path.

### Stage 7 — WLED read-only validation

**Entry criteria:** Stage 6 passed. The intended WLED endpoint is enabled in the
untracked configuration. LEDs remain off/black.

**Operator action:** Run `uv run aurora hardware validate wled --config
<untracked-local-config>`. Record the sanitized firmware/status result and
reported LED count. Compare the reported count with both the measured count and
the configured single-zone count.

**Expected evidence:** One sanitized read-only WLED information result and a
three-way count reconciliation without recording the endpoint.

**Pass criteria:** The result is `healthy`, all three counts agree, and the
response is attributable to the intended controller.

**Fail criteria:** `disabled`, `degraded`, or `unhealthy`; any LED-count mismatch;
or uncertain controller attribution. A mismatch fails this stage until it is
reconciled.

**Recovery action:** Stop and reconcile measurement, untracked configuration,
and WLED's own supported configuration. Do not mutate WLED through Project
Aurora.

### Stage 8 — Bounded DDP output validation

**Entry criteria:** Stages 0–7 passed. The operator is physically present and
watching the intended zone, a manual safe shutdown is available, the count is
1–512, and the intended DDP endpoint is explicit. Enable DDP in the untracked
configuration only for this attended step and re-run configuration validation.

**Operator action:** Run `uv run aurora hardware validate ddp-output --config
<untracked-local-config>` exactly once while observing the installation. The
fixed command attempts at most one static RGB(0, 0, 16) frame immediately
followed by one blackout frame. It permits at most 512 LEDs, two packets per
frame, four total send attempts, and one shared 2.0-second monotonic
transmission/send deadline established before resolution. It performs no retry,
discovery, broadcast, multicast, animation, or runtime operation.

Record:

- whether the intended zone responded;
- whether another zone or device responded;
- whether the visible output was low intensity;
- whether blackout was visually observed; and
- whether the CLI reported `healthy`, `degraded`, `unhealthy`, or `disabled`.

**Expected evidence:** The sanitized CLI result plus direct operator observation
of the intended single zone and its return to black/off.

**Pass criteria:** The CLI result and physical observation are consistent with
one low-intensity test on the intended zone followed by observed blackout; no
other device responds. The unavoidable lack of UDP acknowledgment is explicitly
recorded.

**Fail criteria:** A non-healthy CLI result, missing or uncertain visual
observation, wrong or additional output, unexpected intensity or pattern, or
unconfirmed blackout.

**Recovery action:** Apply the recovery sequence immediately. UDP submission
cannot prove WLED receipt, physical output, or successful blackout, even when
the CLI reports `healthy`.

### Stage 9 — Manual HyperHDR live-path proof

**Entry criteria:** Stage 8 passed, the LEDs are confirmed black/off, and an
attended observation window and manual stop method have been recorded. The
direct path remains available for simultaneous observation.

**Operator action:** Using HyperHDR's own supported configuration and interface:

1. select the intended capture input;
2. configure one WLED/DDP destination through HyperHDR's supported configuration
   mechanism;
3. apply the measured single-zone LED layout and orientation;
4. begin with conservative brightness and controlled static or slowly changing
   visual content;
5. observe mapping, orientation, latency, stability, and blackout/off behavior;
   and
6. stop the test manually at the end of the predeclared observation window.

Do not infer firmware-specific UI labels, CLI flags, JSON payloads, or file
paths from this runbook.

**Expected evidence:** Operator records showing the correct physical zone, edge
mapping, and orientation; no unintended device response; reasonable visual
correspondence under the recorded content; no persistent output after stopping;
no direct-path video/audio degradation; and no controller reset or network
instability.

**Pass criteria:** Every expected observation is affirmative under the recorded
conditions, the manual stop works, and the LEDs are observed black/off after the
test.

**Fail criteria:** Incorrect mapping or orientation, unintended output,
persistent output, unacceptable correspondence, direct-path degradation,
controller reset, network instability, or an unknown observation.

**Recovery action:** Stop HyperHDR output through its supported operator control,
confirm the direct path, and follow the recovery sequence. Do not add Project
Aurora code to compensate during this milestone.

### Stage 10 — Failure, disconnect, and recovery checks

**Entry criteria:** Stage 9 passed and the operator can restore the known-good
configuration after each manual check. No unsafe electrical access is required.

**Operator action:** One at a time, with observation and restoration between
checks:

- stop HyperHDR output through its supported control;
- disconnect or disable the capture input through normal supported means;
- disconnect or disable network transport through normal supported means;
- restart HyperHDR through its normal supported operator process;
- restart WLED/the controller only through its normal supported operator process;
- restore LEDs to off/black through supported controls; and
- confirm that all Project Aurora validation commands have exited and no Project
  Aurora background process is transmitting.

Do not require destructive power cycling, open an enclosure, or touch unsafe
electrical conductors.

**Expected evidence:** For each check, record the action, observed output state,
recovery behavior, direct-path status, controller status, and whether manual
intervention was required.

**Pass criteria:** Output stops or recovers predictably, LEDs are observed
off/black after stopping, the direct video/audio path remains unaffected, the
controller remains stable, and no Project Aurora transmitter remains active.

**Fail criteria:** Persistent or unintended output, unexplained restart behavior,
direct-path degradation, instability, unsafe access, or any unknown state.

**Recovery action:** Stop the runbook, restore the last known-safe state through
normal supported controls, and document the failure before any retry.

## Objective completion gate

Every item must be checked before the baseline may be classified `PROVEN`:

- [ ] Direct video/audio path passed, including the intended 4K120, VRR, HDR,
      eARC, and Atmos observations where applicable.
- [ ] Capture-node identity was confirmed.
- [ ] Capture and read/write capabilities were confirmed.
- [ ] A usable actually reported capture mode was selected.
- [ ] Bounded frame validation completed with confirmed wiping and cleanup.
- [ ] HyperHDR read-only validation completed.
- [ ] WLED read-only validation completed.
- [ ] Measured, configured, and WLED-reported LED counts were reconciled.
- [ ] The bounded DDP test and blackout were observed, or any UDP uncertainty
      was explicitly recorded.
- [ ] The manual HyperHDR live-path test passed.
- [ ] The correct single zone, edge mapping, and orientation were observed.
- [ ] Stopping, disconnect, restart, and recovery behavior was verified.
- [ ] No unexplained reset, heat, smell, instability, or unintended output
      occurred.
- [ ] Every deviation and unresolved observation was documented.
- [ ] No secret or private deployment value was committed.

A failed or unknown item prevents `PROVEN` status. Recording UDP uncertainty
preserves the evidence but does not convert a failed or unknown observation into
a pass. Do not waive a mandatory gate by relabeling a component command result.

## Evidence record

Use private operator-controlled records, screenshots, or logs for evidence
locations. Redact credentials, endpoints, capture identifiers, and other private
deployment data before sharing. Do not commit screenshots or logs containing
secrets or personal network details.

| Stage | Date/time | Configuration or version reference | Sanitized result | Operator observation | Pass/fail/unknown | Evidence location | Notes/deviation |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 0 — Configuration and safety | `<record at test time>` | `<private reference>` | `<record at test time>` | `<record at test time>` | `<record at test time>` | `<private evidence location>` | `<record at test time>` |
| 1 — Direct HDMI independence | `<record at test time>` | `<private reference>` | `operator observation` | `<record at test time>` | `<record at test time>` | `<private evidence location>` | `<record at test time>` |
| 2 — Capture identity | `<record at test time>` | `<private reference>` | `<sanitized result>` | `<record at test time>` | `<record at test time>` | `<private evidence location>` | `<record at test time>` |
| 3 — V4L2 capabilities | `<record at test time>` | `<private reference>` | `<sanitized result>` | `<record at test time>` | `<record at test time>` | `<private evidence location>` | `<record at test time>` |
| 4 — Capture modes | `<record at test time>` | `<private reference>` | `<sanitized result>` | `<record at test time>` | `<record at test time>` | `<private evidence location>` | `<record at test time>` |
| 5 — Bounded frame | `<record at test time>` | `<private reference>` | `<sanitized result>` | `<record at test time>` | `<record at test time>` | `<private evidence location>` | `<record at test time>` |
| 6 — HyperHDR information | `<record at test time>` | `<private reference>` | `<sanitized result>` | `<record at test time>` | `<record at test time>` | `<private evidence location>` | `<record at test time>` |
| 7 — WLED information | `<record at test time>` | `<private reference>` | `<sanitized result>` | `<record at test time>` | `<record at test time>` | `<private evidence location>` | `<record at test time>` |
| 8 — Bounded DDP | `<record at test time>` | `<private reference>` | `<sanitized result>` | `<record at test time>` | `<record at test time>` | `<private evidence location>` | `<record at test time>` |
| 9 — HyperHDR live path | `<record at test time>` | `<private reference>` | `operator observation` | `<record at test time>` | `<record at test time>` | `<private evidence location>` | `<record at test time>` |
| 10 — Recovery checks | `<record at test time>` | `<private reference>` | `operator observation` | `<record at test time>` | `<record at test time>` | `<private evidence location>` | `<record at test time>` |

## Result classifications

- `PROVEN` — every mandatory gate passed under the recorded conditions.
- `NOT PROVEN` — one or more mandatory gates failed.
- `INCOMPLETE` — evidence is missing or a stage was not executed.
- `BLOCKED` — a safety, hardware, compatibility, or environmental issue prevents
  continuation.

Do not use `healthy` as the overall runbook result. `healthy`, `degraded`,
`unhealthy`, and `disabled` belong to individual component validation reports.

## Recovery and rollback

Use this conservative sequence without assuming that software guarantees
physical darkness:

1. Stop HyperHDR output through its supported operator control.
2. Confirm by direct observation that the LEDs return to black/off.
3. Use the existing bounded DDP command only when appropriate, under direct
   observation, and within its documented safety gate.
4. Stop Project Aurora commands and confirm they have exited.
5. Disconnect or disable the configured output path through normal supported
   controls.
6. Remove power only through the installation's normal safe shutdown process.
7. Document the failure and resulting state before retrying.

If the LEDs cannot be confirmed black/off, retain the safer physical state,
stop testing, and seek appropriate hardware or electrical assistance.

## Promotion criteria for later milestones

A `PROVEN` Milestone 11 result may justify proposing separately reviewed future
work. It does not authorize that work automatically. Separate approval remains
required for:

- long-duration stability testing;
- runtime-adapter design;
- continuous Project Aurora DDP transmission;
- service management;
- automatic restart, watchdog, or unattended behavior;
- multi-zone configuration and orchestration;
- performance or latency optimization;
- MQTT automation or telemetry; and
- replacement of HyperHDR.

Until separately approved, Project Aurora continues to provide validation
commands and documentation only; HyperHDR owns live real-time color operation.
