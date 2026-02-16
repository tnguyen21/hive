## 2024-05-22 - [SSE Event DB Hammering]
**Learning:** SSE events (like token streaming) trigger `_renew_lease_for_session` which executes a DB UPDATE. This causes N+1 DB writes per message chunk.
**Action:** Always debounce high-frequency event handlers that touch the DB. Use `_session_last_lease_renewal` pattern.
