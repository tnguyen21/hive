# Implementation Notes

_Living document tracking implementation status, delivered features, post-mortems, and open questions._

**Last Updated**: 2026-02-14

---

## Implementation Status

### Completed (Phases 1-7)

- **Phase 1**: Database foundation, OpenCode client, SSE consumer, single worker loop
- **Phase 2**: Multi-worker pool, Queen Bee TUI, session cycling, permission unblocker, daemon mode
- **Phase 3**: Queen Bee as user-facing interface with 20+ CLI commands
- **Phase 4**: Merge queue processor with two-tier approach (mechanical + Refinery LLM)
- **Phase 5**: Session cleanup, triple completion detection, stale agent reconciliation, prompt templates, per-issue model config, CLI enhancements, retry escalation chain (3-tier: retry → agent switch → escalate), degraded mode with exponential backoff recovery, context cycling for Refinery sessions
- **Phase 6**: Structured logging (rotating file handler, all print() → logger.\*), capability-based routing (project/type/keyword scoring), cost tracking (`hive costs` command with token aggregation), `hive watch <issue_id>` for live worker monitoring
- **Phase 7** (partial): Long-lived refinery session, dependency race fix, inter-agent knowledge transfer (Notes system)

**Status**: Fully functional multi-agent orchestrator with 167+ passing unit tests across 15 modules + 3 prompt templates

See `src/hive/` directory for implementation. See `IMPL_PLAN.md` for the phase-by-phase roadmap checklist.

---

## Delivered Features

**Implementation completed**: 2026-02-12 (Phases 1-6), ongoing (Phase 7)
**Code location**: `src/hive/`

### Core Infrastructure

- SQLite database with WAL mode (7 tables including `notes`)
- Ready queue with dependency resolution
- OpenCode HTTP client (full API coverage)
- SSE event stream consumer
- Git worktree management + merge/rebase operations
- Hash-based ID generation

### Orchestration Engine

- Main event loop with worker pool
- Atomic issue claiming (CAS)
- Lease-based staleness (15min default)
- Permission unblocker (500ms polling + 2s safety net)
- Session lifecycle management (create, abort, delete, cleanup)
- Merge queue processor (background task)
- Triple completion detection: SSE events + file-based `.hive-result.jsonl` + session polling fallback
- Aggressive session cleanup on cancel, shutdown, stale detection, and daemon restart
- Stale agent reconciliation on daemon startup (abort orphaned sessions)
- Per-issue model configuration with three-tier resolution

### Agent Types

- **Queen Bee**: Strategic decomposition (user-facing TUI, default: Opus)
- **Workers**: Autonomous execution in git worktrees (default: Sonnet)
- **Refinery**: LLM merge processor for conflicts and test failures (default: Sonnet)
- Session cycling for molecule steps

### Merge Pipeline

- Two-tier done→finalized pipeline
- Tier 1: Mechanical rebase + test + ff-merge (no LLM)
- Tier 2: Refinery LLM for conflict resolution and test failure diagnosis
- `:::MERGE_RESULT:::` structured signal parsing
- Post-finalization worktree + branch + session teardown
- Configurable test gate (`HIVE_TEST_COMMAND`)
- Feature flag (`HIVE_MERGE_QUEUE_ENABLED`)

### Prompt System

- Prompts stored as `.md` template files in `src/hive/prompts/`
- `string.Template` substitution (no Jinja2 dependency)
- Templates: `worker.md`, `system.md`, `refinery.md`
- Cached on first load for performance
- Anti-stall behavioral conditioning ("NEVER STOP MID-WORKFLOW")
- File-based completion signal instructions embedded in worker prompt

### Human Interface (CLI)

- 20+ commands: create, list, ready, show, update, cancel, finalize, retry, escalate, molecule, dep, agents, agent, events, close, logs, status, merges, costs, watch, note, notes, start, daemon, queen
- `hive --json logs -f` for live event tailing (JSONL in `--json` mode)
- `hive list --sort --reverse --type --assignee --limit` for flexible filtering
- `hive create --model` / `hive update --model` for per-issue model config
- `hive merges` for merge queue visibility
- `hive status` with merge queue stats
- `hive watch <issue-id>`: Live SSE streaming from worker sessions

### Resilience

- Retry escalation chain: 3-tier (retry same agent MAX_RETRIES=2 → switch agent MAX_AGENT_SWITCHES=2 → escalate to human)
- Degraded mode: `_opencode_healthy` flag, health check with exponential backoff (5s→60s cap)
- Context cycling for Refinery: token threshold (100K) or message count (>20)
- Long-lived refinery session with eager creation, health checks, auto-restart, stale-result fence

### Operational Maturity

- Structured logging: rotating file handler (10MB, 5 backups), HIVE_LOG_LEVEL env var
- Capability-based routing: project/type/keyword scoring, prefer experienced agents
- Cost tracking: `hive costs` CLI command with --issue/--agent filters
- Inter-agent knowledge transfer: Notes system with DB table, file convention, orchestrator harvest/inject

### Quality

- 167+ unit tests (100% passing)
- 15 modules + 3 prompt templates, ~6,500 lines production code
- 12 test files, ~4,500 lines test code

---

## Open Questions

1. **Completion verification depth**: The structured completion signal (Section 9.5 of design doc) and the Refinery's test gate catch many issues, but not semantic errors. Options: secondary review agent, git diff size heuristics, mandatory test coverage.

2. **Scale ceiling**: SQLite WAL mode is good for ~30 concurrent readers. If we need 100+ agents, when and how do we migrate to Postgres? The thin-server architecture makes this swap straightforward.

3. **Cost management**: With Queen + Refinery + N workers running concurrently, token costs can spike. Budget caps and graceful degradation when approaching limits are not yet implemented.

4. **Multiple OpenCode servers**: A single OpenCode server might bottleneck at many concurrent sessions. Should the orchestrator manage multiple server instances, or does OpenCode scale sufficiently within a single process?

### Resolved Questions

- **Agent-to-agent knowledge transfer**: Notes system implemented. Workers write `.hive-notes.jsonl`, orchestrator harvests on completion, injects relevant notes into future worker prompts. Queen can add project-wide notes via `hive note`.

- **Queen model selection**: Three-tier model config: `HIVE_DEFAULT_MODEL` (Opus, Queen), `HIVE_WORKER_MODEL` (Sonnet, workers), `HIVE_REFINERY_MODEL` (Sonnet, refinery). Per-issue override via `--model` flag.

- **Queen scope**: The Queen is the user-facing interface. Its scope is defined by the conversation, not by the orchestrator. Context managed by OpenCode's built-in compaction and fresh sessions (state is in the DB).

- **Refinery worktree management**: Refinery session is scoped to the main project directory but operates on worker worktrees by `cd`-ing into them. No dedicated worktree needed.

---

## Post-Mortem: Infinite Spawn Loop (2026-02-14)

### The Bug

Two issues (`w-49ea04`, `w-f3c2b8`) entered an infinite loop where the orchestrator spawned a worker, the worker stalled, the orchestrator reset the issue to open, and the cycle repeated — for **11 hours**. This created 319 failed agents, consumed 636 stall/claim cycles, and burned significant API credits with no useful work produced.

### Root Cause

**`handle_stalled_agent` bypassed the retry escalation chain.** When a worker stalled (lease expired), the function unconditionally reset the issue to `status='open', assignee=NULL`:

```python
# THE BUG — unconditional reset, no escalation check
UPDATE issues SET assignee = NULL, status = 'open'
WHERE id = ? AND status = 'in_progress'
```

Meanwhile, `_handle_agent_failure` (the normal completion-failure path) had a proper 3-tier escalation chain: retry (up to MAX_RETRIES) → agent switch (up to MAX_AGENT_SWITCHES) → escalate. But stalled agents **never entered this code path**. They went straight back to the ready queue.

The same unconditional-reset existed in `_reconcile_stale_agents` (daemon restart) and `_shutdown_all_sessions` (daemon shutdown).

### Secondary Bug

`_handle_agent_failure` set the issue status to `open` for retries but **did not clear the `assignee` field**. Since `get_ready_queue` requires `assignee IS NULL`, retried issues through the normal failure path would never be picked up again.

### The Fix

1. **`handle_stalled_agent`**: Routes through `_handle_agent_failure` with a synthetic `CompletionResult`. Stalls now count against the retry budget.
2. **`_handle_agent_failure`**: Clears `assignee = NULL` when setting status back to `open`.
3. **`_reconcile_stale_agents`**: Checks retry budget before resetting to open on daemon restart.

### Invariants (System-Wide Contracts)

These invariants must be respected by **every code path** that touches issue or agent state:

**INV-1: Retry Budget is Universal.** Every transition from a non-terminal state back to `open` must check the retry budget. If exhausted, the issue must move to a terminal state (`failed`, `escalated`, `canceled`) — never back to `open`.

```sql
-- Diagnostic: issues that are open but have exhausted their retry budget
SELECT i.id, i.title, i.status,
       (SELECT COUNT(*) FROM events WHERE issue_id = i.id AND event_type = 'retry') as retries,
       (SELECT COUNT(*) FROM events WHERE issue_id = i.id AND event_type = 'stalled') as stalls
FROM issues i
WHERE i.status = 'open'
  AND (SELECT COUNT(*) FROM events WHERE issue_id = i.id AND event_type = 'retry') >= 2;
```

**INV-2: `assignee` and `status` Must Be Consistent.** `open` requires `assignee IS NULL`. `in_progress` requires `assignee IS NOT NULL`. Any code that sets `status = 'open'` must also clear `assignee`.

```sql
-- Diagnostic: invalid assignee/status combinations
SELECT id, title, status, assignee FROM issues
WHERE (status = 'open' AND assignee IS NOT NULL)
   OR (status = 'in_progress' AND assignee IS NULL);
```

**INV-3: No Unbounded Loops.** Every spawn attempt must increment a counter. After a finite number of attempts, the issue must reach a terminal state. All issues eventually terminate.

**INV-4: State Transitions Are Funneled Through Shared Logic.** All "release issue back to queue" operations should go through `_handle_agent_failure` — not ad-hoc SQL.

**INV-5: Events Are the Source of Truth for Retry Budgets.** The retry budget is computed by counting events (`retry`, `agent_switch`), not by maintaining a counter column. `count_events_by_type` is the single source of truth.

### Lessons

This bug reveals a class of error in orchestration systems: **multiple code paths that transition the same state machine, with different subsets of the transition logic.** The escalation logic was implemented correctly in one place but not applied uniformly across stall, reconciliation, and shutdown paths.
