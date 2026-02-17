# Technical Design: Hive Multi-Agent Orchestrator

_Implementation-synced design document for the current Python codebase._

**Last verified against code**: 2026-02-16

---

## 1. Scope and Source of Truth

This document describes the system as currently implemented in:

- `src/hive/db.py`
- `src/hive/orchestrator.py`
- `src/hive/merge.py`
- `src/hive/backends/`
- `src/hive/cli.py`
- `src/hive/prompts.py` and `src/hive/prompts/*.md`

When this document disagrees with code, code wins.

---

## 2. Architecture Overview

Hive is a single-process orchestrator that coordinates LLM workers against a SQLite queue, with git worktrees as execution sandboxes.

```text
Human
  |
  | hive CLI / hive queen
  v
SQLite DB (~/.hive/hive.db by default)
  |
  | ready-queue polling, claims, events, merge queue
  v
Orchestrator (async loop)
  |- spawns worker sessions
  |- monitors completion / staleness
  |- handles escalation policy
  |- runs merge processor
  v
Workers + Refinery
  |- per-worker git worktree + session
  |- persistent refinery session (project root)
```

### Core components

| Component | Responsibility |
|---|---|
| `Database` | Queue state, dependencies, events, notes, merge queue, metrics views |
| `Orchestrator` | Scheduler loop, worker lifecycle, monitoring, retry/escalation, degraded mode |
| `MergeProcessor` | `done -> finalized` pipeline (mechanical fast-path, refinery fallback) |
| Backend (`OpenCodeClient` + `SSEClient` OR `ClaudeWSBackend`) | Session lifecycle and event stream abstraction |
| `HiveCLI` | Human-facing control plane (issue management, monitoring, daemon/queen launch) |

### Design boundaries

- Deterministic orchestration logic is in Python (`orchestrator.py`, `merge.py`, SQL queries).
- Ambiguous coding/merge decisions are delegated to LLM sessions (workers, refinery).
- Durable coordination state lives in SQLite + git, not in model context windows.

---

## 3. Configuration Model

Config is layered in this order (later overrides earlier):

1. Built-in defaults
2. `~/.hive/config.toml` (`[hive]` section)
3. `<project>/.hive.toml` (`[hive]` section)
4. Environment variables

Implemented in `src/hive/config.py`.

### Key defaults

- `HIVE_BACKEND=claude`
- `HIVE_MAX_AGENTS=10`
- `HIVE_POLL_INTERVAL=5`
- `HIVE_LEASE_DURATION=900`
- `HIVE_LEASE_EXTENSION=600`
- `HIVE_PERMISSION_SAFETY_NET_INTERVAL=2.0`
- `HIVE_MERGE_QUEUE_ENABLED=true`
- `HIVE_WORKER_MODEL=claude-sonnet-4-5-20250929`
- `HIVE_REFINERY_MODEL=claude-opus-4-6`
- `HIVE_DEFAULT_MODEL=claude-opus-4-6`
- `HIVE_MAX_TOKENS_PER_ISSUE=200000`
- `HIVE_MAX_TOKENS_PER_RUN=2000000`
- `HIVE_ANOMALY_WINDOW_MINUTES=10`
- `HIVE_ANOMALY_FAILURE_THRESHOLD=3`

---

## 4. Data Model (SQLite)

Schema is defined in `db.py` (`SCHEMA`) and migrated by `_migrate_if_needed()`.

### Tables

- `issues`
  - Core work unit.
  - Important columns: `status`, `priority`, `type`, `assignee`, `parent_id`, `project`, `model`, `tags`, `metadata`.
- `dependencies`
  - DAG edges (`issue_id -> depends_on`, usually `type='blocks'`).
- `agents`
  - Ephemeral execution identity, session/worktree linkage while active.
- `events`
  - Append-only operational/event ledger.
- `notes`
  - Inter-worker knowledge transfer (`project`, `category`, `content`).
- `merge_queue`
  - Finalization queue from worker completion to merge processing.

### View

- `agent_runs`
  - Derived run-level metrics from `events` and `issues`.

### Issue statuses

Used in code paths:

- `open`
- `in_progress`
- `done`
- `finalized`
- `failed`
- `escalated`
- `canceled`

`blocked` may be set manually (e.g., `hive update --status blocked`) but is not an automatic orchestrator transition.

### ID generation

Implemented in `utils.generate_id()`:

- Prefix + 12 hex chars from `uuid4().hex`.
- Examples: `w-1a2b3c4d5e6f`, `agent-...`, `worker-...`, `ws-...`.

---

## 5. Scheduling and Claiming

### Ready queue query (`Database.get_ready_queue`)

Ready issues satisfy:

- `status = 'open'`
- `assignee IS NULL`
- `type != 'epic'`
- all `blocks` dependencies are in `done|finalized|canceled`

Sorted by `priority ASC, created_at ASC`.

### Atomic claim (`Database.claim_issue`)

Claim is a CAS-style update that succeeds only when:

- issue is still unassigned, and
- dependencies are still resolved.

On success:

- issue -> `in_progress`, `assignee=<agent_id>`
- agent -> `working`, `current_issue=<issue_id>`
- `claimed` event logged

---

## 6. Orchestrator Runtime

Implemented in `src/hive/orchestrator.py`.

### Startup sequence (`Orchestrator.start`)

1. Register event handlers (`session.status`, `session.error`, `permission.request`).
2. Start event stream (`sse_client.connect_with_reconnect()`).
3. Reconcile stale DB/remote session state (`_reconcile_stale_agents`).
4. Initialize merge processor (`merge_processor.initialize()`).
5. Start background loops:
   - `permission_unblocker_loop`
   - `merge_processor_loop`
6. Enter `main_loop`.

### Main loop behavior

Per cycle:

1. If backend unhealthy: run degraded-mode backoff checks (`_check_opencode_health`).
2. Enforce per-run token cap (`MAX_TOKENS_PER_RUN`) for spawn pausing.
3. If below agent cap, fetch one ready issue (`limit=1`) and try `spawn_worker`.
4. Run stalled-agent checks.

### Worker spawn (`spawn_worker`)

1. Create ephemeral agent row.
2. Create git worktree (`create_worktree_async`).
3. Claim issue atomically.
4. Create backend session with worker permissions.
5. Update agent DB row with `session_id`, `worktree`, lease fields.
6. Build prompt/system prompt and dispatch async message.
7. Start monitor task (`monitor_agent`).

### Completion detection (`monitor_agent`)

Detection strategy is event + fallback polling:

- Primary: wait for `session.status: idle` signal (`asyncio.Event`).
- Fallback: periodic polling via `get_session_status`.
- On idle: read `.hive-result.jsonl` from worktree.

Cancellation and lease-expiry checks run during monitor timeouts.

### Completion handling (`handle_agent_complete`)

Flow:

1. Remove stale `.hive-result.jsonl`.
2. Harvest `.hive-notes.jsonl` into `notes` table (best-effort).
3. Evaluate transition via `_decide_completion_transition`:
   - terminal issue skip,
   - per-issue token budget fail,
   - completion assessment fail,
   - no-diff validation fail,
   - success done,
   - success + epic cycle.
4. Success path:
   - issue -> `done`
   - enqueue `merge_queue` (including worker `test_command` if provided)
   - log `completed`
   - optionally `cycle_agent_to_next_step`
5. Failure path:
   - route through `_handle_agent_failure` retry chain

### Retry/escalation policy

Decision order (`_choose_escalation`):

1. Anomaly detection (`incomplete` bursts within configured window) -> `escalated`
2. Retry tier (`retry` count < `MAX_RETRIES`) -> reopen issue
3. Agent-switch tier (`agent_switch` count < `MAX_AGENT_SWITCHES`) -> reopen issue
4. Exhausted -> `escalated`

All failures log `incomplete` with reason and model context.

### Stall handling

- Stalled candidates are active agents whose DB lease expired.
- `get_session_status` re-check avoids false positives:
  - `idle` -> treat as missed completion
  - `busy` -> extend lease once per lease period (`lease_extended` event)
  - otherwise -> fail through stall handling
- True stall routes through normal failure escalation path.

### Degraded mode

Triggered when backend connectivity/5xx-style failures are detected.

Behavior:

- pause spawning
- exponential backoff health checks (up to 60s)
- resume normal operation once healthy; log system events

### Reconciliation on startup (`_reconcile_stale_agents`)

Phases:

1. Fetch live backend sessions.
2. Reconcile DB `working` agents from previous runs:
   - cleanup stale sessions/worktrees
   - mark agents `failed`
   - reopen issue or mark `failed` based on retry budget
3. Delete orphan backend sessions not referenced in DB.
4. Purge `idle|failed` agent rows (ephemeral cleanup).

---

## 7. Merge Pipeline (`done -> finalized`)

Implemented in `src/hive/merge.py`.

### Queue model

Worker completion inserts into `merge_queue` with:

- `issue_id`, `agent_id`, `project`
- `worktree`, `branch_name`
- optional `test_command`
- `status='queued'`

`merge_processor_loop` processes one queued entry at a time.

### Tier 1: Mechanical merge

`_try_mechanical_merge` executes:

1. `git rebase main` in worker worktree
2. test gate
   - worker command first (120s), then global command (300s) if both exist
   - or whichever exists
3. `git merge --ff-only <branch>` in main repo

If any step fails:

- events logged (`rebase_conflict`, `test_failure`, etc.)
- rejection note may be added
- fallback to refinery path

### Tier 2: Refinery LLM

`_send_to_refinery`:

1. ensure/create refinery session
2. clear stale result file in worktree
3. send refinery prompt
4. verify session became active (post-send check)
5. poll until idle (`_wait_for_refinery`), fence with message-count growth
6. parse `.hive-result.jsonl`

Refinery outcomes:

- `merged`
  - orchestrator performs final `git merge --ff-only`
  - finalize issue
- `rejected`
  - queue entry -> `failed`
  - issue -> `open` (for rework)
  - `merge_rejected` event + rejection note
- `needs_human` (or unknown)
  - queue entry -> `failed`
  - issue -> `escalated`

On refinery exceptions, queue entry is failed and refinery session is force-reset.

### Finalization and teardown

`_finalize_issue`:

- `merge_queue.status='merged'`
- issue -> `finalized`
- epic parent may be auto-finalized when all children are complete

`_teardown_after_finalize`:

- cleanup agent session (if still present)
- remove worktree
- delete branch
- delete agent row

Events/notes/merge queue keep `agent_id` as correlation key after agent deletion.

---

## 8. Backend Abstraction

Interface is defined in `src/hive/backends/base.py` (`HiveBackend`).

### Required capabilities

Session lifecycle:

- `list_sessions`, `create_session`, `send_message_async`
- `abort_session`, `delete_session`, `cleanup_session`
- `get_session_status`, `get_messages`
- `get_pending_permissions`, `reply_permission`

Event stream:

- `on`, `on_all`, `connect_with_reconnect`, `stop`

### OpenCode backend

- `OpenCodeClient`: REST session API client.
- `SSEClient`: event consumer.
- Session directory scoping uses `X-OpenCode-Directory` header.
- SSE supports `/global/event` (default in orchestrator) and `/event?directory=...`.

### Claude WS backend

`ClaudeWSBackend` implements both lifecycle and event interfaces in one class.

- Hive hosts an aiohttp WebSocket server.
- `create_session` spawns `claude` CLI with `--sdk-url ws://<host>:<port>/agent/<session_id>`.
- Status transitions are inferred from streamed messages:
  - assistant traffic -> busy
  - result message -> idle
- SSE-compatible `session.status` events are emitted to orchestrator handlers.
- Permission APIs return no pending requests; `can_use_tool` control requests are auto-allowed.

---

## 9. Prompt and File Protocols

Prompt engine is in `src/hive/prompts.py` with templates in `src/hive/prompts/`.

### Templates

- `worker.md`
- `system.md`
- `refinery.md`
- `queen.md`

Templates are loaded once and cached; `get_prompt_version()` hashes template content for event logging.

### Worker completion file

Workers must write `.hive-result.jsonl` in worktree root.

Expected status values consumed by code:

- `success` -> completion success
- any non-success (`failure`, `blocked`, etc.) -> treated as failure with blocker/reason context

### Refinery completion file

Refinery writes `.hive-result.jsonl` in target worktree.

Consumed statuses:

- `merged`
- `rejected`
- `needs_human`

### Notes protocol

Workers/refinery may write `.hive-notes.jsonl` (JSONL) in worktree root.

During execution (parallel coordination):

- orchestrator polls active workers' `.hive-notes.jsonl` and harvests appended notes incrementally
- harvested notes are persisted to the `notes` table immediately
- notes are best-effort relayed to other active workers as an FYI message (to close the "only on completion" gap)
- events:
  - `notes_harvested_live` (in-progress harvest)
  - `notes_harvested` (final flush at completion)

On completion merge/orchestrator paths:

- parse notes
- persist to `notes` table (`project`, `issue_id`, `agent_id`, `category`, `content`)
- cleanup notes file

Before worker dispatch, orchestrator injects:

- epic sibling notes (`get_notes_for_epic`)
- recent project notes (`get_notes(project=..., limit=10)`)

---

## 10. Permission Policy

Worker/refinery sessions are created with `WORKER_PERMISSIONS` from `config.py`:

- allow `*`
- deny `question`
- deny `plan_enter`
- deny `external_directory`

Runtime safety net (`evaluate_permission_policy`):

- reject: `question`, `plan_enter`, `plan_exit`, `external_directory`
- allow once: `read`, `edit`, `write`, `bash`
- unknown: leave unresolved (manual review)

Permission requests are handled in two ways:

- event-driven (`permission.request` handler)
- periodic safety-net poll loop

---

## 11. CLI Surface (Current)

Implemented in `src/hive/cli.py`.

### Project/work orchestration

- `hive create`
- `hive list`
- `hive show`
- `hive update`
- `hive cancel`
- `hive finalize`
- `hive retry`
- `hive review`
- `hive epic`
- `hive dep add|remove`

### Monitoring and analytics

- `hive status`
- `hive logs`
- `hive agents [agent_id]`
- `hive merges`
- `hive metrics`
- `hive doctor [--fix]`

### Runtime/session UX

- `hive start`
- `hive stop`
- `hive queen [--backend opencode|claude|codex]`
- `hive note`
- `hive setup` / `hive init`

`--json` mode is supported globally for programmatic use.

---

## 12. Doctor Invariants

`src/hive/doctor.py` currently registers:

- `INV-1`: exhausted retry budget left open
- `INV-2`: issue status/assignee inconsistency
- `INV-3`: unbounded agent loop indicators
- `INV-5`: retry count/state disagreement
- `INV-6`: orphaned agents (missing worktree)
- `INV-7`: stuck merge entries (`running` > 30 min)
- `INV-8`: ghost git worktrees with no active agent

Some checks include auto-fix callbacks (`--fix`).

---

## 13. Metrics and Cost Tracking

### Token usage

- `tokens_used` events are derived from backend message metadata.
- Aggregations:
  - per-issue (`get_issue_token_total`)
  - global (`get_run_token_total`)
  - detailed breakdown (`get_token_usage`)

### Performance

- `agent_runs` view + `get_metrics` / `get_model_performance` support model, tag, and type analytics.
- `hive metrics --costs` provides rough USD estimates from token totals.

---

## 14. Known Limitations and Follow-Ups

Current implementation trade-offs to keep in mind:

1. Per-run token cap uses all `tokens_used` rows in DB (no run-boundary partition key).
2. Automated model routing is not implemented; routing is config/per-issue override.
3. Merge processor is single-threaded by design (one queue entry at a time).
4. Several user-facing docs/prompts outside this file may still reference legacy commands; this document reflects actual CLI/parser behavior in `cli.py`.

---

## 15. Change Policy

When behavior changes in orchestration, schema, merge flow, backend interface, or CLI surface, update this doc in the same change set.

Minimum checklist for doc sync:

- schema/table/view changes (`db.py`)
- state transitions (`orchestrator.py`, `merge.py`)
- backend method contracts (`backends/base.py`)
- command surface (`cli.py`)
- file protocol changes (`prompts.py` / templates)
