# Roadmap

1. **Milestone 1 (completed):** repository and development environment scaffold.
2. **Milestone 2 (completed):** validated configuration without hardware I/O.
3. **Milestone 3 (completed):** runtime planning and lifecycle contracts without adapters.
4. **Milestone 4 (completed):** explicit one-shot WLED read-only validation using GET `/json/info`.
5. **Milestone 5 (completed):** explicit one-shot HyperHDR read-only
   serverinfo validation using GET `/json-rpc` only.
6. **Milestone 6 (completed):** explicit non-invasive Linux capture-device
   presence and V4L2 registration metadata validation.
7. **Milestone 7 (completed):** explicit query-only Linux V4L2 capability
   validation using one `VIDIOC_QUERYCAP` ioctl.
8. **Milestone 8 (completed):** explicit bounded query-only Linux V4L2 format, size, and frame-interval enumeration.
9. **Milestone 9 (completed):** bounded single-frame V4L2
   read/write validation with mandatory buffer wiping.
10. **Milestone 10 (completed):** explicit operator-only bounded
    DDP output validation using one low-intensity frame followed by one blackout
    frame. Runtime DDP integration and continuous transmission remain deferred.
11. **Milestone 11 (planned):** operator-controlled single-zone baseline-path
    proof and deployment runbook using the existing validation boundaries and
    HyperHDR-owned live color path. Completion requires recorded operator
    evidence and does not claim the physical path has already passed. See the
    [single-zone baseline proof](single-zone-baseline-proof.md).

Multi-zone configuration and orchestration remain deferred to separately
approved future work after the single-zone baseline is proven. They are not part
of Milestone 11.
