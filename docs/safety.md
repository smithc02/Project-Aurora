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
