## 2026-02-14 - ID Collision in Worker Generation
**Learning:** The previous implementation of worker IDs (`worker-{generate_id('')[2:]}`) reduced entropy to ~5 hex characters ($16^5 \approx 1M$), making collisions highly probable (~50% chance at ~1200 workers).
**Action:** Always verify ID generation logic for hidden entropy reduction (like slicing) and use sufficient length (12+ hex chars) for distributed systems.
