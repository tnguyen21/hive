## 2025-05-24 - High-Frequency DB Writes on SSE Events
**Learning:** SSE event handlers in `Orchestrator` fire very frequently (e.g., token streaming). Direct database writes in these handlers cause massive I/O thrashing. Always debounce DB updates in high-frequency event loops, especially for lease renewals where precision is not critical (e.g., updating every 60s is fine for a 10m lease).
**Action:** When adding new event handlers, check frequency and add debounce logic for any side effects involving I/O or DB writes.
