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
8. **Future, separate milestone:** bounded format, resolution, and frame-rate
   enumeration only after separate approval.
9. **Future, separate milestone:** frame acquisition only after separate approval.
10. **Future, separate milestone:** DDP transmission only after separate approval;
    runtime adapters remain deferred.
11. Multi-zone orchestration remains deferred until the baseline path is proven.
