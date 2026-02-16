# Implementation Notes

_Living document tracking implementation status, delivered features, post-mortems, and open questions._

**Last Updated**: 2026-02-14

---

## Implementation Status

### Completed (Phases 1-8)

- **Phase 1**: Database foundation, OpenCode client, SSE consumer, single worker loop
- **Phase 2**: Multi-worker pool, Queen Bee TUI, session cycling, permission unblocker, daemon mode
- **Phase 3**: Queen Bee as user-facing interface with 20+ CLI commands
- **Phase 4**: Merge queue processor with two-tier approach (mechanical + Refinery LLM)
- **Phase 5**: Session cleanup, triple completion detection, stale agent reconciliation, prompt templates, per-issue model config, CLI enhancements, retry escalation chain (3-tier: retry → agent switch → escalate), degraded mode with exponential backoff recovery, context cycling for Refinery sessions
- **Phase 6**: Structured logging (rotating file handler, all print() → logger.\*), capability-based routing (project/type/keyword scoring), cost tracking (`hive costs` command with token aggregation), `hive watch <issue_id>` for live worker monitoring
- **Phase 7**: Long-lived refinery session, dependency race fix, inter-agent knowledge transfer (Notes system), bidirectional reconciliation on startup, O(1) reverse lookup maps for session/issue routing, local counter tracking for refinery sessions
- **Phase 8** (complexity cleanup): Full audit of 11 unnecessary complexity items from `docs/COMPLEXITY.md` — all resolved. Removed ToolExecutor indirection (544 lines deleted), simplified completion detection (removed triple detection and heuristic fallbacks in favor of file-based + SSE), deduplicated session cleanup pattern, removed agent capability scoring, fixed error detection (`'5' in error_msg` bug), removed token estimation fallback, removed duplicate CLI entry points, DRY'd repetitive query patterns

**Status**: Fully functional multi-agent orchestrator with 250 passing unit tests across 15 modules + 3 prompt templates

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
- Session cycling for epic steps

### Merge Pipeline

- Two-tier done→finalized pipeline
- Tier 1: Mechanical rebase + test + ff-merge (no LLM)
- Tier 2: Refinery LLM for conflict resolution and test failure diagnosis
- File-based `.hive-result.jsonl` result signaling (same mechanism as workers)
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

- 25+ commands: create, list, ready, show, update, cancel, finalize, retry, escalate, epic, dep, agents, agent, events, logs, status, stats, merges, costs, watch, note, notes, daemon (start/stop/restart/logs), queen, init, ui
- All commands under a single `HiveCLI` class with direct DB calls (no ToolExecutor indirection)
- `hive --json` flag on all commands for programmatic/machine-readable output
- `hive --json logs -f` for live event tailing (JSONL in `--json` mode)
- `hive list --sort --reverse --type --assignee --limit` for flexible filtering
- `hive create --model` / `hive update --model` for per-issue model config
- `hive create --tags` / `hive update --tags` for issue tagging
- `hive merges` for merge queue visibility
- `hive status` with merge queue stats
- `hive watch <issue-id>`: Live SSE streaming from worker sessions
- `hive ui`: Datasette integration for browsing the DB in a web UI

### Resilience

- Retry escalation chain: 3-tier (retry same agent MAX_RETRIES=2 → switch agent MAX_AGENT_SWITCHES=2 → escalate to human)
- Stall detection routes through escalation chain (prevents infinite spawn loops — see post-mortem below)
- Degraded mode: `_opencode_healthy` flag, health check with exponential backoff (5s→60s cap)
- Context cycling for Refinery: token threshold (100K) or message count (>20)
- Long-lived refinery session with eager creation, health checks, auto-restart, stale-result fence
- Bidirectional reconciliation on daemon restart: cross-references DB agents with live OpenCode sessions, cleans up ghost agents and orphan sessions
- Git worktree retry logic: exponential backoff on transient `invalid reference` and `index.lock` errors

### Operational Maturity

- Structured logging: rotating file handler (10MB, 5 backups), HIVE_LOG_LEVEL env var
- Capability-based routing: project/type/keyword scoring, prefer experienced agents
- Cost tracking: `hive costs` CLI command with --issue/--agent filters
- Inter-agent knowledge transfer: Notes system with DB table, file convention, orchestrator harvest/inject

### Quality

- 250 unit tests (100% passing, 13 deselected integration tests)
- 15 modules + 3 prompt templates, ~6,500 lines production code
- 13 test files, ~5,300 lines test code
- Lint-clean with `ruff` (line-length=144)

---

## Open Questions

1. **Scale ceiling**: SQLite WAL mode is good for ~30 concurrent readers. If we need 100+ agents, when and how do we migrate to Postgres? The thin-server architecture makes this swap straightforward.

2. **Cost management**: With Queen + Refinery + N workers running concurrently, token costs can spike. Budget caps and graceful degradation when approaching limits are not yet implemented.

3. **Multiple OpenCode servers**: A single OpenCode server might bottleneck at many concurrent sessions. Should the orchestrator manage multiple server instances, or does OpenCode scale sufficiently within a single process?

4. **OpenCode is an external TypeScript dependency.** Hive requires a running OpenCode server (`opencode serve`) which is a TypeScript binary from the `anomalyco/opencode` repo — not a Python package. The Python SDK (`opencode-ai` on PyPI) is client-only. This means hive cannot be fully self-contained as a `uv tool install`. To make hive truly standalone, we'd need to reimplement OpenCode's headless LLM session management in pure Python: session lifecycle (create/abort/delete), multi-session multiplexing, SSE event streaming, permission handling, and the Anthropic API integration. Until then, users must install the `opencode` binary separately and run `opencode serve` (or have `hive start` auto-launch it as a managed subprocess).

5. **Git worktree contention**: Concurrent `git worktree add` commands occasionally fail with `fatal: invalid reference: main`. Current workaround is retry-with-backoff, but the root cause is not fully understood (possibly packed-refs rewriting, loose ref gc, or worktree metadata races). See TODO in `git.py`.

6. **Worker session visibility**: The Claude WS backend (`claude_ws.py`) holds live worker sessions in memory but doesn't expose them for inspection. There's no way to peek at a worker's conversation transcript, see what tools it's calling, or monitor progress beyond event-level signals (`worker_started`, `completed`). A `GET /sessions` HTTP endpoint on the backend's aiohttp app would allow `hive watch` to show live worker activity, and could feed a richer TUI dashboard. Currently the only observability into active workers is `hive agents` (DB state) and `hive logs -f` (events).

### Resolved Questions

- **Completion verification depth**: Simplified in Phase 8. Removed triple detection and heuristic fallbacks. Now uses file-based `.hive-result.jsonl` (deterministic) + SSE `session.status → idle` (real-time) + session polling fallback. The Refinery's test gate catches integration issues.

- **Agent-to-agent knowledge transfer**: Notes system implemented. Workers write `.hive-notes.jsonl`, orchestrator harvests on completion, injects relevant notes into future worker prompts. Queen can add project-wide notes via `hive note`.

- **Queen model selection**: Three-tier model config: `HIVE_DEFAULT_MODEL` (Opus, Queen), `HIVE_WORKER_MODEL` (Sonnet, workers), `HIVE_REFINERY_MODEL` (Sonnet, refinery). Per-issue override via `--model` flag.

- **Queen scope**: The Queen is the user-facing interface. Its scope is defined by the conversation, not by the orchestrator. Context managed by OpenCode's built-in compaction and fresh sessions (state is in the DB).

- **Refinery worktree management**: Refinery session is scoped to the main project directory but operates on worker worktrees by `cd`-ing into them. No dedicated worktree needed.

- **Infinite spawn loops**: Solved via universal retry budget enforcement. All code paths that transition issues back to `open` now check the retry budget (INV-1). See post-mortem below.

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

---

## Phase 8: Complexity Cleanup (2026-02-14)

A full audit of the codebase (documented in `docs/COMPLEXITY.md`) identified 11 items of unnecessary complexity. All were resolved in a single session:

| # | Item | Resolution |
|---|------|-----------|
| 1 | **ToolExecutor indirection** — `tools.py` (544 lines) was a needless abstraction layer between CLI and DB | Deleted `tools.py`. Inlined all operations into `HiveCLI` class with direct DB calls. CLI went from dispatch-through-tools to direct execution. |
| 2 | **Session cleanup deduplication** — 4 identical abort+delete patterns across orchestrator, merge, shutdown | Extracted `opencode.cleanup_session()` method, replaced all inline patterns |
| 3 | **Agent capability scoring** — `get_agent_capability_scores()` in db.py did complex SQL joins for routing that was never meaningfully used | Removed entirely. Worker routing is now simple: create new agent per issue. |
| 4/5 | **Completion detection** — Triple detection (SSE + file + polling) with complex heuristic fallbacks | Simplified to file-based `.hive-result.jsonl` + SSE idle detection + polling fallback. Removed heuristic message parsing. |
| 6 | **Permission constants** — Hardcoded permission lists duplicated across files | Consolidated into `config.py` as `WORKER_PERMISSIONS` |
| 7 | **Error detection bug** — `'5' in error_msg` matched any string containing '5', not just '5xx errors' | Fixed to check `error_msg.startswith('5')` on the status code |
| 8 | **Token estimation fallback** — Estimated tokens from `len(text) // 4` when metadata unavailable | Removed. If no metadata, no usage is logged. |
| 9 | **Duplicate CLI entry points** — `start`/`stop` existed as both top-level and under `daemon` subcommand | Removed top-level duplicates. All daemon ops under `hive daemon start/stop/restart/logs` |
| 10 | **Repetitive query patterns** — `get_events_since` and `get_recent_events` had near-identical SQL | DRY'd into shared `_query_events` helper with conditions pattern |
| 11 | **Worktree removal** — Complex path validation and error handling | Simplified to single `git worktree remove --force` with best-effort cleanup |

### Additional fixes during cleanup

- **Schema migration bug**: `CREATE INDEX ... ON issues(tags)` was in `SCHEMA` SQL, but `tags` column is added via migration. Moved index creation into `_migrate_if_needed()`.
- **Daemon start command**: `daemon.py` spawned `hive.cli ... start --foreground` but the CLI refactor moved `start` under `daemon start`. Fixed the Popen command.
- **Git worktree contention**: Added retry logic (4 attempts, 1s exponential backoff) for transient `invalid reference: main` errors during concurrent worktree creation.
- **Stale branch cleanup**: Removed 336 accumulated `agent/*` branches from previous sessions.

### Metrics

| Metric | Before | After |
|--------|--------|-------|
| `tools.py` | 544 lines | Deleted |
| `cli.py` | ~1200 lines (+ tools.py dispatch) | ~1820 lines (self-contained) |
| Test count | 167 passing | 250 passing |
| Test files | 12 | 13 |
| Production code | ~6,500 lines | ~6,500 lines (net neutral: deleted tools.py, expanded cli.py) |
