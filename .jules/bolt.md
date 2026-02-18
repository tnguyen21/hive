## 2025-05-15 - [Orchestrator Lease Debounce]
**Learning:** High-frequency SSE events (like token streaming) can trigger excessive database writes if lease renewal is tied directly to event processing without debouncing. The default `LEASE_EXTENSION` is 600s, so updating on every event is wasteful.
**Action:** When implementing lease renewal or heartbeat mechanisms, always add a debounce or throttling logic (e.g., `interval / 10`) to reduce DB I/O.
