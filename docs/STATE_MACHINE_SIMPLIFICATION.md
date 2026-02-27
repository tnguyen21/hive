# Hive Orchestrator: State Machine & Simplification Notes

**Last reviewed against code**: 2026-02-27  
**Primary code refs**: `src/hive/orchestrator.py`, `src/hive/db.py`, `src/hive/merge.py`

This doc captures:
1) the *current* durable state machines (what exists in SQLite and what the orchestrator enforces),  
2) the “core essence” of Hive, and  
3) the main sources of race-condition complexity + concrete ways to simplify the system.

When this doc disagrees with code, code wins.

**Update (2026-02-27)**: Epic *session cycling* (reusing the same agent/worktree across epic steps) has been removed. Epic steps now execute as normal issues via dependencies + the ready queue.

---

## 1) Core Essence (What Hive Is Really Doing)

At its core, Hive is a **durable work queue + isolated execution sandboxes + deterministic integration gate**:

1. **Queue** (SQLite): issues are the durable “to-do list” and event log is the audit trail.
2. **Execute** (workers): each issue is executed in an isolated **git worktree** with an LLM session driving edits.
3. **Integrate** (merge pipeline): “done” work is validated/merged (optionally with a refinery LLM), then finalized.
4. **Human control plane** (CLI/queen): humans can create, cancel, retry, review, and finalize.

The *product* is reliable parallelization and traceability, not clever in-memory orchestration.

---

## 2) Durable State Machines (SQLite is the source of truth)

Hive has three coupled state machines:

- **Issues** (`issues.status`)
- **Agents** (`agents.status`, plus `session_id/worktree/current_issue/lease_*`)
- **Merge queue** (`merge_queue.status`)

### 2.1 Issue state machine (`issues.status`)

Observed statuses:
- `open`: ready to be claimed (must have `assignee IS NULL`)
- `in_progress`: claimed by an agent (`assignee` set)
- `done`: worker claims success and produced a diff; merge queue entry created
- `finalized`: merged/accepted (either via merge processor or manual finalize)
- `failed`: terminal failure (various paths set this; not always used consistently)
- `escalated`: requires human intervention
- `canceled`: user canceled

“`blocked`” exists as a manual status but is not an automatic transition.

Typical happy path:

```text
open -> in_progress -> done -> finalized
```

Typical failure path (with budgets):

```text
open -> in_progress -> (incomplete/stalled) -> open (retry/agent_switch) -> ... -> escalated
```

Important: **the retry budget is computed from `events`** (e.g. `retry`, `agent_switch`, `incomplete`), not from counters on the issue row.

### 2.2 Agent state machine (`agents.status`)

Agents are intentionally ephemeral.

Observed statuses:
- `idle`: mostly a transient default; the daemon later purges `idle|failed` rows
- `working`: agent currently owns a session and (usually) a worktree
- `failed`: terminal marker used as a “tombstone” before deletion/cleanup

Important: the merge processor often **deletes the agent row** after finalization/cleanup. `events.agent_id` is a correlation key, not a durable FK.

### 2.3 Merge queue state machine (`merge_queue.status`)

Observed statuses:
- `queued`: inserted by orchestrator when an issue is marked `done`
- `running`: merge processor is actively processing the entry
- `merged`: merge succeeded and issue finalized (or user manually finalized)
- `failed`: merge/refinery rejected/escalated/error

Happy path:

```text
queued -> running -> merged
```

Failure paths include:
- `rejected` by refinery: re-open issue (`open`) for rework
- `needs_human`: escalate issue (`escalated`)
- unexpected exceptions: mark entry `failed` and clean up resources

---

## 3) Runtime Model (What the daemon actually does)

The orchestrator is an async loop with a few background tasks:

- **Scheduler loop** (`main_loop`): polls for ready issues and spawns workers.
- **Per-worker monitor** (`monitor_agent`): waits for session idle via SSE, with polling fallback.
- **Stall handling** (`check_stalled_agents` / `handle_stalled_agent`): lease expiry + session-status re-check + failure routing.
- **Merge loop** (`merge_processor_loop`): processes `merge_queue` sequentially.
- **Permission unblocker**: resolves backend permission prompts (mostly a safety net).
- **Startup reconciliation**: cleans up stale DB agents and orphan backend sessions.

This architecture is reasonable; the hard part is **making all side effects idempotent** and **making ownership boundaries explicit**.

---

## 4) Why the code needs so many branches today

Most “weird hacks/branches” exist to paper over *multi-source truth* and *ownership ambiguity*:

### 4.1 Multi-source truth

Completion and liveness are inferred from:
- backend event stream (SSE / WS)
- backend polling (`get_session_status`)
- local filesystem file protocol (`.hive-result.jsonl`, `.hive-notes.jsonl`)
- DB lease fields (`lease_expires_at`, `last_progress_at`)
- in-memory guards (`_handling_agents`, `_spawning_issues`, reverse maps)

When these disagree (missed SSE, reconnect gap, daemon restart, slow workers), the code must branch heavily to reconcile.

### 4.2 Ownership ambiguity (the big one)

The system treats **a worktree** as:
- a worker’s execution sandbox, and
- the merge pipeline’s integration target.

If you don’t define *exclusive ownership* of the worktree, the merge processor and worker lifecycle will naturally race each other.

---

## 5) Historical race: epic session cycling vs merge queue (removed)

This is the highest-leverage simplification target because it’s structural, not incidental.

### 5.1 What used to happen

Previously, on worker success for an epic step the orchestrator could:
1) mark the step `done` and **enqueue `merge_queue` with the current worktree path**, then
2) **reuse the same agent/worktree** for the next epic step (“session cycling”).

Meanwhile:
- the merge processor may pick up the queued merge and run a refinery cycle,
- and on completion may **remove the worktree and delete the branch/agent row**.

### 5.2 Why this was a “hard” race

This isn’t a missing lock — it’s an ownership conflict:
- The orchestrator says: “keep using this worktree for more work”
- The merge processor says: “this worktree is now merge-owned and may be rebased/merged/deleted”

No amount of “double-handling guards” fixes that cleanly; you need a design rule.

---

## 6) Simplification principles (what to optimize for)

### Principle A: One owner per resource

At any moment, a worktree (and session) must be owned by exactly one subsystem:
- **worker** owns it while editing/running tests,
- **merge processor** owns it while rebasing/integrating,
- or it’s garbage and can be deleted.

### Principle B: Make transitions idempotent in SQLite

If a daemon restarts mid-transition, replaying should be safe.
Prefer DB-level “fences” (CAS updates / unique constraints / status checks) over in-memory sets.

### Principle C: Reduce “status surface area”

Every additional status introduces:
- more transition cases,
- more reconciliation code,
- and more “impossible” mixed states.

Keep only the statuses you truly need for operator UX.

---

## 7) Concrete simplifications (recommended options)

### 7.1 Remove epic session cycling (simplest, highest payoff)

Epic steps execute as normal issues via dependencies + the ready queue (each step gets its own worker/worktree/session).

Benefits:
- Removes the biggest ownership race.
- Restores a clean pipeline: *worker completes once → merge pipeline runs → resources cleaned*.
- Makes correctness “local” again: a step is self-contained.

Cost:
- More session/worktree churn for multi-step epics.
  (Often acceptable; correctness and simplicity tend to win here.)

### 7.2 If you keep epic cycling, decouple merge from the live worker worktree

If you truly want long-lived agent context across steps, the merge pipeline cannot operate on the same worktree.

Two viable patterns:

**Option 1: Snapshot-and-merge**
- On step success, create an immutable snapshot ref (e.g. commit hash + tag/branch).
- Enqueue merge work referencing the snapshot ref, not the mutable worktree.
- Merge processor checks out that ref into its own integration worktree.

**Option 2: Merge uses main repo only**
- Worker pushes commits to a branch.
- Merge processor never touches worker worktrees; it operates only in the project root by fetching/merging that branch.

Either approach makes “merge-owned” and “worker-owned” sandboxes distinct.

### 7.3 Collapse completion detection into a single truth source (optional but helpful)

Today completion is “session idle + file exists”.

A simpler rule set:
- The *only* completion signal is “`.hive-result.jsonl` exists and parses”.
- Session idle is only a performance optimization to decide when to check for the file.
- On lease expiry, poll status once; if still busy, extend; if error/not_found, fail.

This reduces special cases where “idle but no file” or “file but missed idle” creates branching.

### 7.4 Move more anti-race logic into DB constraints

Examples of DB-level fences that reduce orchestrator branching:
- “At most one merge queue entry in `queued|running` per issue”
- “Only one working agent may claim an issue”
- “Issue transitions are CAS-style: update only if current status matches expected”

The less the orchestrator must remember in-memory, the fewer TOCTOU guards you need.

### 7.5 Reduce status taxonomy

One simplification that tends to pay off:
- Replace `failed` vs `escalated` ambiguity with a single terminal “needs_attention” (or keep `escalated` only),
- and keep the operator-relevant detail in `events.detail`.

This also makes post-mortems easier: the reason is an event, not encoded in many terminal states.

---

## 8) Minimal “design rules” to adopt (invariants)

If you want the codebase to get simpler, these invariants have to be treated as contracts:

1. **Worktree exclusivity**: a worktree is owned by *exactly one* of {worker, merge} at a time.
2. **No merge on mutable sandboxes**: merge processing never runs on a worktree that may be reused for additional worker tasks.
3. **Idempotent completion handling**: running completion handling twice must not enqueue twice, double-delete, or corrupt state.
4. **CAS transitions**: critical state transitions (claim, complete, finalize) should be conditional updates, not blind updates.
5. **Single completion signal**: completion is driven by one authoritative artifact (recommend: the file protocol).

---

## 9) Suggested next step (smallest refactor with biggest simplification)

With epic session cycling removed, the next simplification lever is to push more idempotency/fencing into SQLite (unique merge entries, CAS-style transitions) and reduce liveness branching by consolidating lease/progress signals.
