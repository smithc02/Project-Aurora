# Bounded DDP output validation

Milestone 10 provides one explicit operator-only command:

```bash
uv run aurora hardware validate ddp-output --config configs/aurora.local.yaml
```

The command never runs automatically and is not used by runtime planning or the
runtime controller. Its only output operation is one static low-intensity DDP
RGB frame followed immediately by one full-blackout frame. It has no animation,
timed sequence, repetition, background process, continuous output, discovery,
HTTP mutation, MQTT, HyperHDR, or capture operation.

## Configuration gate

DDP remains disabled by default. It inherits Aurora's endpoint host validation,
uses port 4048 by default, and requires a configured host when enabled. The
command uses `lighting_zones` as the sole LED-count source; it introduces no
parallel count. Exactly one zone must be enabled, that zone must have
`led_count`, and the count must be from 1 through 512 inclusive.

Only the normal `--config` and `--log-level` options are accepted. Host, port,
LED count, color, destination ID, packet size, and timeout cannot be overridden
on this command line. Reports and logs exclude the configured hostname, resolved
address, port, socket address or descriptor, packet and RGB bytes, exception and
errno text, credentials, and secrets.

Configuration failures stop before resolution or socket creation. Sanitized
reasons include `ddp_disabled`, `enabled_zone_required`,
`multiple_enabled_zones_not_supported`, `led_count_required`, and
`led_count_exceeds_limit`.

## Fixed safety limits

The following Milestone 10 values are fixed and are not configurable:

- DDP destination ID: 1
- DDP RGB24 datatype: `0x0B`
- DDP header size: 10 bytes
- maximum data payload per UDP datagram: 1400 bytes
- maximum LEDs: 512
- maximum RGB frame payload: 1536 bytes
- maximum datagrams per frame: 2
- maximum total send attempts: 4
- one 2.0-second monotonic transmission/send deadline, established before
  resolution
- test value: RGB(0, 0, 16), sequence 1
- blackout value: RGB(0, 0, 0), sequence 2

The test payload is exactly `led_count` repetitions of bytes `00 00 10`; the
blackout payload is exactly the same count of `00 00 00`. Both complete payloads
and all packet metadata are built and validated before destination resolution or
socket creation.

## Exact packet structure

Each version-1 DDP data packet uses the network-order layout `!BBBBIH`:

| Bytes | Value |
| --- | --- |
| 0 | `0x40`, plus PUSH `0x01` only on the final packet of the frame |
| 1 | sequence 1 for every test packet; sequence 2 for every blackout packet |
| 2 | RGB24 datatype `0x0B` |
| 3 | destination ID 1 |
| 4–7 | unsigned 32-bit big-endian byte offset into the complete frame |
| 8–9 | unsigned 16-bit big-endian length of the appended packet payload |
| 10 onward | RGB payload; there is no optional timecode |

Every non-final packet has flags `0x40`; every final packet has flags `0x41`.
The first payload offset is 0. If a second packet is present, its offset is
1400. Packet length always equals the bytes actually appended. At the 512-LED
maximum, each frame is 1536 bytes and segments into 1400 bytes without PUSH,
then 136 bytes with PUSH.

## Resolution and send boundary

Resolution is invoked exactly once and requests only UDP-compatible IPv4 or
IPv6 results. Duplicate results are normalized, and exactly one unique usable
destination is required. Multicast, unspecified, and IPv4 limited-broadcast
destinations are rejected. There is no scanning, mDNS browsing, broadcast,
multicast, discovery, address fallback, or attempt to use a second address.

The transmission/send deadline is established before resolution. Time spent in
resolution and socket creation therefore consumes the remaining send budget.
The standard-library `getaddrinfo` call is blocking and this implementation
cannot forcibly interrupt it; the two-second deadline is not a claim that
resolution or total command wall-clock duration is capped at two seconds. If a
successful resolver returns after the deadline, the command creates the socket,
skips every test `sendto`, and still enters the blackout phase, which observes
the same expired deadline. No DDP `sendto` call is permitted after the deadline.

After successful resolution, the command creates exactly one `SOCK_DGRAM`,
`IPPROTO_UDP` socket in the selected family. It uses that socket and the exact
same destination for every send, does not bind a listening socket, and performs
no receive. Socket acquisition uses an ownership lease that registers the sole
blackout plan and close attempt before the acquired socket is exposed to the
probe and before any test-frame operation begins. The lease exit path owns both
cleanup phases, so an interruption immediately after socket acquisition and
before the test phase still enters the bounded best-effort blackout phase before
the sole close attempt. Blackout delivery is not guaranteed. Immediately before
each possible send the probe checks the shared transmission/send deadline. A
positive remaining duration becomes the socket timeout; then exactly one
`sendto` is issued. Interrupted, failed, timed-out, partial, and unexpected sends
are never retried. A send counts as complete only if its return equals the
complete datagram size.

Test packets are attempted in order and stop after the first failure. Once the
socket exists, the blackout phase is always entered—even if the deadline stopped
the first test send or a test packet failed or was partial. Each blackout segment
gets at most one best-effort attempt while time remains. A failed blackout
segment is not retried; the next distinct segment may still be attempted if the
transmission/send deadline permits it. No test, resolution, socket creation, or
other network work occurs after blackout begins.

If Ctrl-C raises `KeyboardInterrupt` during a `sendto`, that send counts against
the four-attempt budget and is not retried. The probe stops any remaining test
packets, performs bounded best-effort blackout attempts for later distinct
segments while deadline and budget remain, and attempts socket close exactly
once before re-raising the original interrupt. An interruption during blackout
is likewise not retried; a later distinct blackout segment may still receive one
best-effort attempt. An interruption during close is re-raised after that sole
close attempt, with no later network operation. This cleanup does not guarantee
that any blackout datagram was delivered or displayed.

## Cleanup and result meaning

The socket is closed exactly once after blackout, and the immutable metadata-only
result is constructed after that close attempt. Result precedence is:

1. Any blackout failure is unhealthy and overrides test or cleanup failures.
2. Fully submitted test and blackout frames with unconfirmed socket closure are
   degraded with `ddp_output_cleanup_unconfirmed`.
3. A test failure with a fully submitted blackout is unhealthy and retains the
   test reason, regardless of close outcome.
4. Complete test and blackout submission plus confirmed close is healthy with
   `validated`.
5. Configuration, resolution, and socket-creation failures are unhealthy, except
   disabled DDP is disabled.

The public service rejects inconsistent injected metadata as
`unexpected_ddp_output_failure`. It never masks a true discovery, broadcast,
multicast, or retry flag.

UDP submission is not an acknowledgment. A healthy result confirms only that
the bounded datagrams were submitted and the socket close was confirmed. It does
not verify WLED receipt, any physical LED change, blackout at the strip, wiring,
power delivery, HyperHDR integration, or the complete lighting path.

All automated tests use injected synthetic resolver, clock, and socket seams.
CI performs no DNS lookup and makes no WLED, HyperHDR, MQTT, capture, DDP endpoint,
or runtime-controller connection. Real invocation is exclusively an informed
operator action against their explicitly configured endpoint.
