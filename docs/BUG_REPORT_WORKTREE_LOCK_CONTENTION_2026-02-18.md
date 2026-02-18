# Bug Report: Worktree Lock Contention and Retry Instability

Date: 2026-02-18
Related issues:
- w-dd5888af93da (`Harden worktree create retry for ref-lock contention`)
- w-74c797ca8bfc (`Add spawn cooldown after repeated worktree errors`)

## 1) w-dd5888af93da

### Observed behavior
- Repeated worker spawn attempts fail during `git worktree add`.
- Error pattern: `cannot lock ref 'refs/heads/agent/...': ... .lock: Operation not permitted`.
- Issue repeatedly flips between `open/in_progress` and `failed` via `spawn_error`.

### Impact
- Worker never reaches stable coding phase reliably.
- High agent churn and noisy logs.
- Delays hardening work intended to fix the same class of failures.

### Investigation needed
- Determine why branch ref lock files intermittently fail under orchestrator spawn bursts even though manual local `git worktree add/remove` succeeds.
- Audit lock handling around concurrent spawn paths and branch creation retries.
- Add instrumentation for lock-failure frequency and time-window correlation with spawn bursts.

## 2) w-74c797ca8bfc

### Observed behavior
- Issue escalated after repeated failures in short window (anomaly detector).
- Failure reason included worktree index lock write failure during worker commit:
  `unable to create .git/worktrees/.../index.lock (Operation not permitted)`.
- Multiple stale workers from prior daemon run increased contention and retries.

### Impact
- Auto-escalation halted normal completion path.
- Partial progress exists but completion signal and commit path were disrupted.
- Retry loop pressure increased system instability.

### Investigation needed
- Verify whether stale worker/session reconciliation is sufficient under Codex backend restarts.
- Investigate why index lock operations fail intermittently inside worker worktrees.
- Validate whether per-issue spawn cooldown plus stronger lock-aware retry logic reduces escalation rate.

## Immediate mitigation applied
- Ran daemon cleanup/restart to reconcile stale agents.
- Retried both issues with explicit notes and model pinning to `gpt-5.2-codex`.
- Both issues are back in `in_progress` as of this report.
