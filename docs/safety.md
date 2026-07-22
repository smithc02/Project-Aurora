# Safety

> **Electrical hazard:** The Mean Well LRS-150-12 exposes mains-voltage
> terminals. Software documentation is not a substitute for qualified
> electrical work.

AC wiring must remain isolated from low-voltage wiring. A final printed
enclosure must provide separate high-voltage and low-voltage compartments.
Appropriate strain relief, terminal covers, grounding, fusing, ventilation, and
wire gauges are required. This milestone deliberately provides no wiring
instructions, fuse ratings, enclosure design, or mains/power-control code.

Stop work and seek qualified electrical guidance when any safety-critical
hardware detail is uncertain.


## Software validation boundary

Milestone 4's WLED command is read-only: it requests only `/json/info`, sends no LED frames, and changes no device or power state. It does not validate electrical installation, wiring, power supplies, HyperHDR, capture hardware, or the full lighting path.

Milestone 5's HyperHDR command is also read-only: it requests only
`/json-rpc` server information, changes no HyperHDR component, capture process,
or HDR setting, and does not send images, DDP, MQTT, or LED data.

Milestone 7's capture-capability command is query-only, not read-only file
access: V4L2 specifies `O_RDWR` for opening devices. It performs one
non-streaming capability query and closes the descriptor. It does not acquire
a frame, change capture settings, validate signal presence, or validate the
lighting path.


Milestone 8's `capture-modes` command is query-only and non-streaming. It can
open one configured V4L2 node as required by V4L2, but configures no format and
acquires no frame; it does not test HDMI signal, HyperHDR, DDP, WLED, LEDs, or
the electrical installation.

Milestone 9's `capture-frame` command is an explicit bounded read/write test,
not runtime capture. It uses one transient buffer, wipes it before release, and
does not use V4L2 streaming or network operations.

Milestone 10's `ddp-output` command is an explicit operator-only output action,
not a read-only check. When DDP is enabled and exactly one 1–512 LED zone is
configured, it can cause connected LEDs to display one static low-intensity blue
frame. It immediately enters a best-effort full-blackout phase, including after
a test-frame failure, but UDP delivery and blackout are not acknowledged. Do not
assume the LEDs are dark merely because the command returned.

One two-second monotonic transmission/send deadline is established before
resolution, so resolution and socket creation consume the remaining send budget
and no DDP `sendto` call is permitted after it expires. The standard-library
resolver is blocking and cannot be forcibly interrupted; neither resolution nor
total command wall-clock duration is claimed to be capped at two seconds. If the
resolver returns after the deadline, no test packet is sent after socket creation
and blackout is still entered under the expired deadline. The command permits at
most two datagrams per frame and four sends total, and no retry, loop, discovery,
broadcast, multicast, HTTP mutation, MQTT, HyperHDR, capture, or
runtime-controller operation. Socket acquisition establishes ownership and the
blackout-and-close responsibility before the socket is exposed to any test-frame
operation. Even Ctrl-C immediately after acquisition and before the test phase
enters bounded best-effort blackout cleanup before the sole close attempt. Ctrl-C
during transmission defers the original
`KeyboardInterrupt` only long enough to stop further test packets, make bounded
best-effort attempts for remaining distinct blackout segments, and attempt
socket close once; the interrupt is then re-raised. Interrupted sends count
against the four-send budget and are never retried. This cleanup does not
guarantee blackout delivery or physical darkness. A blackout failure always
produces an unhealthy result and takes precedence over test or socket-close
failures. Successful UDP submission does not validate WLED receipt, physical LED
output, wiring, power delivery, or the complete lighting path. Observe the
installation safely and retain an independent safe means to remove low-voltage
LED power; Aurora still contains no mains-power control.
