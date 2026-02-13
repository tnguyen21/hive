# Technical Design: Lightweight Multi-Agent Orchestrator

_A simplified multi-agent orchestration system inspired by Gas Town, using OpenCode server mode as the agent runtime and a single SQLite database as the work queue._

---

## 🎯 Implementation Status

**Last Updated**: 2026-02-13

### ✅ Completed (Phases 1-6)

- ✅ **Phase 1**: Database foundation, OpenCode client, SSE consumer, single worker loop
- ✅ **Phase 2**: Multi-worker pool, Queen Bee TUI, session cycling, permission unblocker, daemon mode
- ✅ **Phase 3**: Queen Bee as user-facing interface with 20+ CLI commands
- ✅ **Phase 4**: Merge queue processor with two-tier approach (mechanical + Refinery LLM)
- ✅ **Phase 5**: Session cleanup, triple completion detection, stale agent reconciliation, prompt templates, per-issue model config, CLI enhancements, retry escalation chain (3-tier: retry → agent switch → escalate), degraded mode with exponential backoff recovery, context cycling for Refinery sessions
- ✅ **Phase 6**: Structured logging (rotating file handler, all print() → logger.\*), capability-based routing (project/type/keyword scoring), cost tracking (`hive costs` command with token aggregation), `hive watch <issue_id>` for live worker monitoring

**Status**: Fully functional multi-agent orchestrator with 167 passing unit tests across 15 modules + 3 prompt templates

See `src/hive/` directory for implementation.

### ⏳ Planned (Phase 7+)

- ⏳ **Phase 7**: Long-lived refinery session (eager creation, periodic health checks, auto-restart on death), dead code cleanup, web dashboard, webhook notifications

---

## 1. Motivation

Gas Town solves the hard problem of coordinating 20-30 AI coding agents across multiple git repositories. It does this with a sophisticated stack: Go CLI, Dolt SQL server, git-backed JSONL sync, distributed hash-based IDs, multi-level beads databases, and Claude Code instances managed via tmux.

That stack is powerful, but it's designed for a world where:

- Work data must be conflict-free and local-first across disconnected clones
- Multiple tiers of beads databases must merge without coordination
- Agents interact with the system exclusively through CLI shelling

**This design trades those constraints for simpler ones:**

- A single SQLite database is the source of truth (no distributed sync)
- OpenCode's HTTP server API replaces CLI-driven agent management
- The orchestrator is a single process that owns the DB and the agent lifecycle

The result is a system that preserves Gas Town's best abstractions — the ready queue, the three-layer agent lifecycle, push-based execution, molecules, and the capability ledger — while dramatically reducing infrastructure complexity.

---

## 2. Architecture Overview

```
        ┌───────────────────────────────────────────────┐
        │              OpenCode Server                   │
        │                                                │
        │  ┌─────────────────────────────────────────┐   │
        │  │ Mayor session (user-facing TUI/web)     │   │
        │  │   ← human chats here                    │   │
        │  │   ← has tool access to `hive` CLI       │   │
        │  └────────────┬────────────────────────────┘   │
        │               │ hive create / hive status / …  │
        │               ▼                                │
        │  ┌─────────────────────────────────────────┐   │
        │  │         SQLite DB (WAL mode)            │   │
        │  └────────────┬────────────────────────────┘   │
        │               │                                │
        │  ┌────────────┴────────────────────────────┐   │
        │  │         Orchestrator (headless)          │   │
        │  │  Work Scheduler · Agent Manager · SSE    │   │
        │  │  Permission Unblocker · Merge Queue      │   │
        │  └────────────┬────────────────────────────┘   │
        │               │ spawns/monitors                │
        │  ┌────────────┴────────────────────────────┐   │
        │  │ Worker session A   (ephemeral, per-issue)│  │
        │  │ Worker session B   (ephemeral, per-issue)│  │
        │  │ Refinery session   (persistent, merges)  │  │
        │  │ ...                                      │  │
        │  └──────────────────────────────────────────┘  │
        └────────────────────────────────────────────────┘
                    │              │
               git worktree   git worktree
               (worker A)     (worker B)
```

### Component Responsibilities

| Component           | Responsibility                                                                                                                                                                                                                                                                                                                           |
| ------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Mayor (LLM)**     | The **user-facing** strategic brain. The human chats with the Mayor in an OpenCode TUI/web session. The Mayor interprets requests, decomposes them into issues/molecules, monitors worker progress, handles escalations, and answers questions — all via tool calls to the `hive` CLI. The Mayor is the primary interface to the system. |
| **Orchestrator**    | The **headless** worker pool manager. Polls the ready queue, spawns workers in git worktrees, monitors completion via SSE, handles permissions, processes the merge queue. Handles the _deterministic_ parts: ready queue, CAS claims, health checks, session lifecycle. The orchestrator does NOT interact with the user.               |
| **Refinery (LLM)**  | The merge processor. Easy rebases go through mechanically. Complex merges, conflicts, and integration failures get reasoned about by the Refinery agent. A persistent OpenCode session.                                                                                                                                                  |
| **Workers (LLM)**   | Ephemeral coding agents. One per issue. Implement, test, commit. Spawned on demand, destroyed on completion.                                                                                                                                                                                                                             |
| **SQLite DB**       | Single source of truth for all work items, dependencies, agent state, and events. Shared by the Mayor (via CLI tools) and the orchestrator.                                                                                                                                                                                              |
| **OpenCode Server** | Agent runtime — hosts the Mayor session (user-facing), worker sessions (headless), and refinery session.                                                                                                                                                                                                                                 |
| **Git Worktrees**   | Per-agent sandboxes, scoped via OpenCode's `X-OpenCode-Directory` header                                                                                                                                                                                                                                                                 |

### The Key Split: Deterministic vs. Ambiguous

The system has a clear separation of concerns:

| Concern                                                   | Who Handles It                          | Why                                                          |
| --------------------------------------------------------- | --------------------------------------- | ------------------------------------------------------------ |
| Ready queue computation                                   | Orchestrator (SQL)                      | Deterministic graph query — no judgment needed               |
| Atomic task claiming                                      | Orchestrator (SQL CAS)                  | Database operation — no judgment needed                      |
| Session lifecycle (create, abort, teardown)               | Orchestrator (HTTP)                     | Mechanical — no judgment needed                              |
| Health checks, staleness detection                        | Orchestrator (timer + SSE)              | Threshold-based — no judgment needed                         |
| SSE event dispatch                                        | Orchestrator (event loop)               | Routing — no judgment needed                                 |
| "Build me an auth system" → concrete issues               | **Mayor** (LLM, via `hive create`)      | User chats with Mayor; Mayor uses CLI tools to create issues |
| Monitoring system state and progress                      | **Mayor** (LLM, via `hive status/logs`) | Mayor proactively checks on workers, reports back to user    |
| Prioritizing competing work items                         | **Mayor** (LLM)                         | Requires understanding urgency, dependencies, context        |
| Handling escalations from stuck workers                   | **Mayor** (LLM, via `hive` tools)       | Reads failure details, decides to retry/rephrase/ask user    |
| Resolving merge conflicts                                 | **Refinery** (LLM)                      | Requires understanding code semantics                        |
| Deciding if a test failure is pre-existing vs. introduced | **Refinery** (LLM)                      | Requires reading test output and understanding context       |
| Implementing a feature / fixing a bug                     | **Worker** (LLM)                        | The actual coding work                                       |

This is Gas Town's **ZFC principle** (Zero decisions in code, all judgment calls go to models) applied selectively. The orchestrator handles what SQL and HTTP can handle. Everything that requires _reasoning about ambiguity_ goes to an LLM agent.

---

## 3. What We Keep from Gas Town

### 3.1 The Ready Queue (DAG-Based Scheduling)

This is the single most important idea to preserve. The dependency graph _is_ the scheduler.

```sql
SELECT i.*
FROM issues i
WHERE i.status = 'open'
  AND i.assignee IS NULL
  AND NOT EXISTS (
    SELECT 1 FROM dependencies d
    JOIN issues blocker ON d.depends_on = blocker.id
    WHERE d.issue_id = i.id
      AND d.type = 'blocks'
      AND blocker.status NOT IN ('done', 'finalized', 'canceled')
  )
ORDER BY i.priority ASC, i.created_at ASC;
```

No scheduler service. No priority queue. No task router. When a blocking issue reaches `done` (worker finished) or `finalized` (merged), its dependents automatically become ready on the next query.

### 3.2 Three-Layer Agent Lifecycle

Gas Town's decomposition of agent state into three layers with independent lifecycles is the key to crash recovery. We keep it, but adapt the implementation:

| Layer        | Gas Town                                | This System                                    |
| ------------ | --------------------------------------- | ---------------------------------------------- |
| **Identity** | Agent bead in Dolt, CV chain            | Row in `agents` table, events accumulate as CV |
| **Sandbox**  | Git worktree, managed by `gt` CLI       | Git worktree, managed by orchestrator          |
| **Session**  | Claude Code in tmux, managed by Witness | OpenCode session via HTTP API                  |

The invariant is the same: sessions are ephemeral and cycle frequently. Sandboxes survive session restarts. Identity is permanent.

### 3.3 Push-Based Execution (Propulsion Principle)

> If you find something on your hook, YOU RUN IT.

Gas Town's insight: there is no idle worker pool. Workers exist because work exists. When work completes, the worker is destroyed.

In this system, the propulsion loop becomes:

```
1. Orchestrator queries ready queue
2. Work found → create git worktree + OpenCode session
3. POST /session/:id/prompt_async with work instructions
4. SSE: session.status → idle (work complete)
5. Orchestrator marks issue 'done', enqueues to merge_queue; worktree persists until finalized
```

No idle sessions. No "are you still working?" heartbeats. The SSE event stream replaces Gas Town's Witness patrol cycle. Polling fallback and file-based signaling (`.hive-result.jsonl`) provide redundancy when SSE events are missed.

### 3.4 Molecules (Multi-Step Workflows)

Molecules as data — multi-step workflows where each step is a trackable work item with explicit dependencies — transfer directly. The `--continue` pattern (complete step, auto-advance) maps to: orchestrator observes step completion (`done`), queries for next ready step within the molecule, sends the next prompt.

### 3.5 Capability Ledger

Every issue an agent closes is an event in the `events` table. Over time this accumulates into a CV. "Which agent is best at Go work?" becomes a SQL query over the events table joined with issue metadata. This is an emergent property of the work ledger — no special infrastructure needed.

---

## 4. What We Drop (and Why)

| Gas Town Feature                                    | Why We Drop It                                                                                                                                                                                                     |
| --------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Distributed beads (JSONL + Dolt + Git sync)**     | Single SQLite DB; no offline or multi-writer sync needed                                                                                                                                                           |
| **3-way merge with field-specific strategies**      | Single writer (the orchestrator); no merge conflicts possible                                                                                                                                                      |
| **Prefix-based routing across multiple DBs**        | One database; IDs are just rows                                                                                                                                                                                    |
| **Two-level data architecture (Town/Rig)**          | Single-level; projects are a column, not a namespace                                                                                                                                                               |
| **Dolt SQL server with branch-per-agent**           | SQLite WAL mode handles concurrent reads; orchestrator serializes writes                                                                                                                                           |
| **Beads redirect files and worktree beads routing** | Agents don't touch the DB directly; orchestrator mediates                                                                                                                                                          |
| **Tmux session management**                         | OpenCode HTTP API manages sessions                                                                                                                                                                                 |
| **Witness/Deacon/Boot/Dog agent roles**             | Functions absorbed into orchestrator code (liveness → lease-based staleness, watchdog → health checks, cleanup → teardown after finalization). The roles aren't dropped — the work persists in deterministic form. |
| **Mail protocol between agents**                    | Orchestrator mediates; Mayor/Refinery communicate via DB, not mail                                                                                                                                                 |
| **Convoy cross-rig tracking**                       | Single DB makes cross-project queries trivial                                                                                                                                                                      |

### What Gets Simpler

With a central DB and single orchestrator process:

- **Claiming a task** is an atomic `UPDATE ... WHERE assignee IS NULL` — real CAS, no optimistic locking
- **Dependency cycle detection** is a live query, not something hoped to be consistent across clones
- **Event/audit trail** is append-only into one DB, no reconciliation
- **"What's in flight?"** is `SELECT * FROM issues WHERE status = 'in_progress'` — no convoy abstraction needed
- **Agent state** is a row in a table, not a bead in a distributed store

---

## 5. SQLite Schema

Six core tables. Everything else is derived.

**Key principle: Events are source of truth, status columns are cache.** Operational transitions are recorded as immutable events in the `events` table. The `status` column on `issues` and `agents` is a denormalized cache for fast queries. On recovery, you can always rebuild current state by replaying the event log. This means the `status` columns are optimistic — if they drift (e.g., crash during a transition), the event log is authoritative.

### Issue Status State Machine

```
open → in_progress → done → finalized
                      ↓
                    failed (retryable)
open|in_progress → blocked (explicit)
* → canceled
* → escalated (human needed)
```

- **`open`**: Ready to be claimed (if no blockers) or waiting on dependencies
- **`in_progress`**: Assigned to a worker, actively being worked on
- **`done`**: Worker finished, waiting for finalizer (merge/verify). This is NOT the end state.
- **`finalized`**: Merged to main and verified. This IS the terminal success state.
- **`failed`**: Worker failed, retryable by orchestrator
- **`blocked`**: Explicitly blocked (dependency or external)
- **`escalated`**: Human intervention required; no automatic retries
- **`canceled`**: Abandoned

The `done` → `finalized` split (borrowed from Codex design) is important: it creates a clean handoff boundary between workers and the refinery. "Done" means "I'm finished coding." "Finalized" means "it's merged and verified on main."

```sql
-- WAL mode for concurrent reads during writes
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

----------------------------------------------------------------------
-- ISSUES: the universal work unit
----------------------------------------------------------------------
CREATE TABLE issues (
    id          TEXT PRIMARY KEY,              -- hash-based: "w-a3f8"
    title       TEXT NOT NULL,
    description TEXT,
    status      TEXT NOT NULL DEFAULT 'open',  -- open|in_progress|done|finalized|failed|blocked|escalated|canceled
    priority    INTEGER NOT NULL DEFAULT 2,    -- 0 = critical, 4 = low
    type        TEXT NOT NULL DEFAULT 'task',  -- task | bug | feature | step | molecule
    assignee    TEXT,                          -- agent ID or NULL
    parent_id   TEXT REFERENCES issues(id),    -- molecule parent (nullable)
    project     TEXT,                          -- project/repo name
    metadata    TEXT,                          -- JSON blob for extension
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at   TEXT
);

CREATE INDEX idx_issues_status ON issues(status);
CREATE INDEX idx_issues_assignee ON issues(assignee);
CREATE INDEX idx_issues_parent ON issues(parent_id);
CREATE INDEX idx_issues_project ON issues(project);

----------------------------------------------------------------------
-- DEPENDENCIES: edges in the work DAG
----------------------------------------------------------------------
CREATE TABLE dependencies (
    issue_id    TEXT NOT NULL REFERENCES issues(id),
    depends_on  TEXT NOT NULL REFERENCES issues(id),
    type        TEXT NOT NULL DEFAULT 'blocks',  -- blocks | related
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (issue_id, depends_on)
);

----------------------------------------------------------------------
-- AGENTS: persistent identity layer
----------------------------------------------------------------------
CREATE TABLE agents (
    id          TEXT PRIMARY KEY,              -- "agent-toast"
    name        TEXT NOT NULL,                 -- human-readable name
    status      TEXT NOT NULL DEFAULT 'idle',  -- idle | working | stalled
    session_id  TEXT,                          -- current OpenCode session ID
    worktree    TEXT,                          -- path to git worktree
    current_issue TEXT REFERENCES issues(id),  -- what they're working on
    model       TEXT,                          -- e.g. "claude-sonnet-4-5-20250929"
    lease_expires_at TEXT,                     -- when orchestrator may reclaim
    last_progress_at TEXT,                     -- last observed progress
    metadata    TEXT,                          -- JSON blob
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

----------------------------------------------------------------------
-- EVENTS: append-only audit trail / capability ledger (source of truth)
----------------------------------------------------------------------
CREATE TABLE events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id    TEXT REFERENCES issues(id),
    agent_id    TEXT REFERENCES agents(id),
    event_type  TEXT NOT NULL,    -- created|claimed|done|finalized|failed|escalated|retry|merged|...
    detail      TEXT,             -- JSON: old/new values, comments, artifacts, etc.
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_events_issue ON events(issue_id);
CREATE INDEX idx_events_agent ON events(agent_id);
CREATE INDEX idx_events_type ON events(event_type);

----------------------------------------------------------------------
-- MERGE_QUEUE: dedicated finalizer queue
----------------------------------------------------------------------
CREATE TABLE merge_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id    TEXT NOT NULL REFERENCES issues(id),
    agent_id    TEXT REFERENCES agents(id),   -- worker who completed the work
    project     TEXT NOT NULL,
    worktree    TEXT NOT NULL,                 -- path to the branch worktree
    branch_name TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued', -- queued|running|merged|failed
    enqueued_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT
);

CREATE INDEX idx_mq_status ON merge_queue(status);
CREATE INDEX idx_mq_project ON merge_queue(project);

----------------------------------------------------------------------
-- LABELS: denormalized tags for fast filtering
----------------------------------------------------------------------
CREATE TABLE labels (
    entity_type TEXT NOT NULL,                -- issue | agent | engine
    entity_id   TEXT NOT NULL,
    label       TEXT NOT NULL,                -- e.g. "failed-by:agent-toast", "mode:degraded", "lang:go"
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (entity_type, entity_id, label)
);
```

### ID Generation

We keep Gas Town's hash-based ID scheme for human-readability:

```python
import hashlib, uuid

def generate_id(prefix: str = "w") -> str:
    raw = hashlib.sha256(uuid.uuid4().bytes).hexdigest()
    return f"{prefix}-{raw[:6]}"

# "w-a3f8b1", "w-c7e2d9", etc.
```

Short, human-friendly, collision-free at <10k issues without coordination. No sequential IDs means no contention.

### The Ready Queue Query

```sql
-- Ready work: open, unassigned, all blockers resolved (done/finalized/canceled)
SELECT i.*
FROM issues i
WHERE i.status = 'open'
  AND i.assignee IS NULL
  AND NOT EXISTS (
    SELECT 1 FROM dependencies d
    JOIN issues blocker ON d.depends_on = blocker.id
    WHERE d.issue_id = i.id
      AND d.type = 'blocks'
      AND blocker.status NOT IN ('done', 'finalized', 'canceled')
  )
ORDER BY i.priority ASC, i.created_at ASC;
```

### Atomic Claim

```sql
-- CAS: claim only if still unassigned
UPDATE issues
SET assignee = :agent_id,
    status = 'in_progress',
    updated_at = datetime('now')
WHERE id = :issue_id
  AND assignee IS NULL;
-- Check rows_affected == 1, else someone beat you
```

---

## 6. OpenCode Integration

### 6.1 Server Lifecycle

The orchestrator starts a single OpenCode server instance. All agent sessions run within it.

```bash
OPENCODE_SERVER_PASSWORD=$SECRET \
  bun run --cwd packages/opencode --conditions=browser src/index.ts serve \
  --port 4096 --hostname 127.0.0.1
```

One server, many sessions. Each session is scoped to a directory (git worktree) via the `?directory=` query parameter.

### 6.2 Session-as-Agent Mapping

Each active agent maps 1:1 to an OpenCode session:

```
Agent "toast"
  ├── Identity: agents table row (permanent)
  ├── Sandbox:  ~/work/polecats/toast/ (git worktree)
  └── Session:  OpenCode session_01JM... (ephemeral)
```

**Creating an agent session:**

```http
POST /session?directory=/home/user/work/polecats/toast
Content-Type: application/json

{
  "title": "toast: implement auth middleware",
  "permission": [
    { "permission": "*", "pattern": "*", "action": "allow" },
    { "permission": "question", "pattern": "*", "action": "deny" },
    { "permission": "plan_enter", "pattern": "*", "action": "deny" },
    { "permission": "external_directory", "pattern": "*", "action": "deny" }
  ]
}
```

**Sending work:**

```http
POST /session/:sessionID/prompt_async
Content-Type: application/json

{
  "parts": [
    {
      "type": "text",
      "text": "You are agent 'toast'. Your task:\n\nTitle: Implement auth middleware\nDescription: Add JWT validation middleware to the Express app...\n\nWhen done, commit your changes with a descriptive message and report completion."
    }
  ]
}
```

The `prompt_async` endpoint returns immediately. The orchestrator monitors progress via the SSE event stream.

### 6.3 Event-Driven Monitoring

OpenCode exposes two SSE event endpoints:

| Endpoint                          | Scope                                                                                                                             | Use Case                                       |
| --------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------- |
| `GET /event?directory=<worktree>` | **Instance-scoped** — only events from that specific worktree/directory                                                           | Per-agent monitoring, scoped dashboards        |
| `GET /global/event`               | **Global** — all events from all sessions across all directories. Each event includes a `directory` field identifying its source. | Orchestrator main loop, cross-agent monitoring |

The orchestrator connects to **`/global/event`** — a single SSE stream that carries events from every agent session. Events are dispatched internally by `sessionID` and `directory`:

```
GET /global/event
Accept: text/event-stream
```

Key events the orchestrator reacts to:

| Event                                   | Orchestrator Action                                                     |
| --------------------------------------- | ----------------------------------------------------------------------- |
| `session.status { type: "idle" }`       | Agent finished. Check work result, mark `done`, enqueue to merge_queue. |
| `session.status { type: "retry" }`      | Transient failure. Log it, wait for auto-retry.                         |
| `session.error`                         | Permanent failure. Mark agent as stalled, reassign work.                |
| `message.part.updated { tool: "bash" }` | Optional: log tool usage for audit trail.                               |

This replaces Gas Town's Witness patrol cycle. Instead of polling tmux sessions for liveness, the orchestrator receives real-time status updates. The per-directory `/event` endpoint exists for narrower use cases (e.g., a UI focused on one agent), but the orchestrator doesn't need it.

### 6.4 Session Cycling (Handoff)

When an agent completes a molecule step and needs a fresh context for the next step:

1. Orchestrator observes step completion (via SSE or polling session messages)
2. Orchestrator aborts the old session: `POST /session/:id/abort`
3. Orchestrator creates a new session scoped to the same worktree
4. Orchestrator sends the next step as a prompt

The sandbox (git worktree) persists across session cycles. Only the LLM context resets. This is the same three-layer lifecycle as Gas Town, mediated by HTTP instead of tmux.

### 6.5 Directory Scoping for Multi-Project

OpenCode's `?directory=` parameter maps directly to per-agent git worktrees:

```
POST /session?directory=/home/user/work/polecats/toast    # Agent toast
POST /session?directory=/home/user/work/polecats/shadow   # Agent shadow
POST /session?directory=/home/user/work/polecats/copper   # Agent copper
```

Each session gets its own isolated LSP server, file watcher, and tool permissions — scoped to that worktree. Agents cannot interfere with each other's sandboxes.

---

## 7. Orchestrator Design

The orchestrator is a **headless** long-running daemon. It does NOT interact with the user — that's the Mayor's job. The orchestrator polls the DB for ready work (created by the Mayor via `hive create`), spawns workers, monitors them via SSE, and processes the merge queue.

### 7.1 Main Loop

```python
async def main_loop():
    """The orchestrator's core loop."""
    while True:
        # 1. Check for ready work
        ready = db.query(READY_QUEUE_SQL)

        for issue in ready:
            if count_active_agents() >= MAX_AGENTS:
                break

            # 2. Spawn agent
            agent = create_agent(issue)
            worktree = create_worktree(issue.project, agent.name)
            # Directory is set via X-OpenCode-Directory header (or ?directory= param),
            # NOT as a body field. Permissions go in the body.
            session = opencode.create_session(
                directory=worktree,  # → X-OpenCode-Directory header
                title=f"{agent.name}: {issue.title}",
                permissions=AUTONOMOUS_PERMISSIONS,  # → body.permission[]
            )

            # 3. Claim and dispatch
            db.claim_issue(issue.id, agent.id)
            opencode.prompt_async(session.id, build_prompt(issue))

            # 4. Record event
            db.log_event(issue.id, agent.id, "claimed")

        # 5. Process SSE events (non-blocking)
        await process_events()

        # 6. Health check: detect stalled agents
        check_stalled_agents()

        await asyncio.sleep(POLL_INTERVAL)  # 5-10 seconds
```

### 7.2 Event Processing

```python
async def process_events():
    """Consume OpenCode SSE events and update DB state."""
    for event in sse_stream.drain():
        match event.type:
            case "session.status":
                session_id = event.properties["sessionID"]
                status = event.properties["status"]["type"]
                agent = db.get_agent_by_session(session_id)

                if status == "idle":
                    await handle_agent_complete(agent)
                elif status == "retry":
                    db.log_event(agent.current_issue, agent.id, "retry",
                                 detail=event.properties["status"])

            case "session.error":
                session_id = event.properties["sessionID"]
                agent = db.get_agent_by_session(session_id)
                await handle_agent_failure(agent, event.properties["error"])


async def handle_agent_complete(agent):
    """Agent finished its prompt. Decide what to do next."""
    issue = db.get_issue(agent.current_issue)

    # Check if agent actually completed the work (inspect last message)
    messages = opencode.get_messages(agent.session_id)
    result = assess_completion(messages)

    if result.success:
        # Transition to 'done' — NOT finalized yet.
        # The refinery/merge_queue handles the done→finalized transition.
        db.update_issue(issue.id, status="done")
        db.log_event(issue.id, agent.id, "done",
                     detail=result.artifacts if result.artifacts else None)

        # Enqueue for finalization (rebase, test, merge to main)
        db.enqueue_merge(
            issue_id=issue.id,
            agent_id=agent.id,
            project=issue.project,
            worktree=agent.worktree,
            branch_name=agent.branch,
        )

        # Check for next step in molecule
        next_step = db.get_next_ready_step(issue.parent_id)
        if next_step:
            # Session cycling: fresh context, same sandbox
            opencode.abort_session(agent.session_id)
            new_session = opencode.create_session(
                directory=agent.worktree,
                title=f"{agent.name}: {next_step.title}",
            )
            db.claim_issue(next_step.id, agent.id)
            db.update_agent(agent.id, session_id=new_session.id,
                            current_issue=next_step.id)
            opencode.prompt_async(new_session.id, build_prompt(next_step))
        else:
            # No more steps — session done, but DON'T tear down worktree yet.
            # The worktree stays alive until the merge_queue entry is finalized.
            opencode.delete_session(agent.session_id)
            db.update_agent(agent.id, status="idle", session_id=None,
                            current_issue=None)
    else:
        # Work incomplete — retry or escalate
        db.log_event(issue.id, agent.id, "incomplete", detail=result.reason)
        retry_or_escalate(agent, issue)
```

### 7.3 Health Checks (Lease-Based Staleness)

Replaces Gas Town's Witness + Deacon + Boot watchdog chain with a **lease-based** staleness model. Every agent assignment gets a `lease_expires_at` timestamp. The orchestrator extends the lease whenever it observes progress. If the lease expires, the agent is considered stalled.

**Progress signals** (ranked by reliability):

| Signal                                       | Source                            | Reliability                             |
| -------------------------------------------- | --------------------------------- | --------------------------------------- |
| Session status transitions (`busy` → `idle`) | `GET /session/:id/status` or SSE  | High — definitive state change          |
| New assistant tokens                         | `GET /session/:id/message` growth | Medium — proves the model is generating |
| Tool completions (bash, edit results)        | Message parts inspection          | Medium — proves tools are executing     |
| Diff size growth                             | `GET /session/:id/diff`           | Low — only indicates file changes       |

```python
LEASE_DURATION = timedelta(minutes=15)     # initial lease
LEASE_EXTENSION = timedelta(minutes=10)    # on observed progress

def check_stalled_agents():
    """Detect agents whose leases have expired without observed progress."""
    expired = db.query("""
        SELECT a.* FROM agents a
        WHERE a.status = 'working'
          AND a.lease_expires_at < datetime('now')
    """)
    for agent in expired:
        # Gather evidence before deciding
        status = opencode.get_session_status(agent.session_id)

        if status["type"] == "idle":
            # Session finished but we missed the event — process now
            handle_agent_complete(agent)

        elif status["type"] == "busy":
            # Session is active — check for real progress
            messages = opencode.get_messages(agent.session_id)
            if has_new_progress(messages, agent.last_progress_at):
                # Extend the lease — agent is making progress
                db.extend_lease(agent.id, LEASE_EXTENSION)
                db.update_agent(agent.id,
                                last_progress_at=datetime.utcnow())
            else:
                # Lease expired AND no progress — truly stalled
                opencode.abort_session(agent.session_id)
                db.mark_agent_stalled(agent.id)
                db.unassign_issue(agent.current_issue)
                db.log_event(agent.current_issue, agent.id, "stalled",
                             detail={"last_progress": str(agent.last_progress_at),
                                     "lease_expired": str(agent.lease_expires_at)})

        else:
            # Unknown/error state — kill and reassign
            opencode.abort_session(agent.session_id)
            db.mark_agent_stalled(agent.id)
            db.unassign_issue(agent.current_issue)
            db.log_event(agent.current_issue, agent.id, "stalled")


def has_new_progress(messages, last_progress_at) -> bool:
    """Check if there are new assistant tokens or tool completions since last check."""
    if not messages:
        return False
    last_msg = messages[-1]
    msg_time = parse_datetime(last_msg.get("created_at", ""))
    return msg_time and msg_time > last_progress_at
```

### 7.4 Agent Teardown and Session Cleanup

Teardown happens **after finalization**, not immediately after a worker marks an issue `done`. The worktree must survive until the merge_queue entry has been processed (rebased, tested, merged). The flow is:

1. Worker finishes → issue transitions to `done`, enqueued to `merge_queue`
2. Refinery/mechanical merge processes the queue entry
3. On success → issue transitions to `finalized`, **then** teardown runs
4. On failure → issue may re-open or escalate; worktree stays for retry

**Session cleanup is aggressive**: OpenCode sessions are killed (abort + delete) whenever:

- An agent completes work (`handle_agent_complete`)
- An issue is canceled (`cancel_agent_for_issue`)
- An agent is detected as stalled (`handle_stalled_agent`)
- The daemon shuts down (`_shutdown_all_sessions` in the `start()` finally block)
- The daemon starts and finds orphaned sessions from a previous run (`_reconcile_stale_agents`)

This prevents the critical bug where opencode sessions linger and consume tokens after their work is done or canceled.

```python
def teardown_agent(agent):
    """Clean up agent AFTER finalization. Called by the merge_queue processor."""
    # 1. Delete OpenCode session (if still alive)
    if agent.session_id:
        opencode.delete_session(agent.session_id)

    # 2. Remove worktree (branch is already merged to main)
    git_worktree_remove(agent.worktree)

    # 3. Mark agent idle (identity persists, session + sandbox gone)
    db.update_agent(agent.id, status="idle", session_id=None,
                    worktree=None, current_issue=None)
```

### 7.5 Permission Unblocker Loop

A blocked permission request stalls an agent's entire session indefinitely — the LLM is waiting for a tool to execute, and the tool is waiting for permission approval. If the orchestrator doesn't handle this, agents hang silently.

The permission unblocker runs on a **fast poll** (~500ms), separate from the slower scheduling loop (5-10s):

```python
PERMISSION_POLL_INTERVAL = 0.5  # seconds — fast, to prevent agent stalls

async def permission_unblocker_loop():
    """Fast loop: auto-resolve pending permission requests based on policy."""
    while True:
        if not db.has_running_agents():
            await asyncio.sleep(PERMISSION_POLL_INTERVAL * 4)  # slow down when idle
            continue

        pending = opencode.get_pending_permissions()
        for perm in pending:
            decision = evaluate_permission_policy(perm)
            if decision:
                opencode.reply_permission(perm["id"], decision)
                db.log_event(
                    issue_id=get_issue_for_session(perm["sessionID"]),
                    agent_id=get_agent_for_session(perm["sessionID"]),
                    event_type="permission_resolved",
                    detail={"permission": perm["permission"],
                            "pattern": perm["pattern"],
                            "decision": decision},
                )

        await asyncio.sleep(PERMISSION_POLL_INTERVAL)


def evaluate_permission_policy(perm) -> str | None:
    """Apply policy rules to decide allow/deny. Returns None if no rule matches."""
    # Session-level permissions already handle most cases (set at session creation).
    # This catches runtime permission requests that slip through.
    rules = [
        # Workers should never ask questions or enter plan mode
        (lambda p: p["permission"] in ("question", "plan_enter", "plan_exit"), "deny"),
        # Workers should never leave their worktree
        (lambda p: p["permission"] == "external_directory", "deny"),
        # Allow standard tool usage within the session's directory scope
        (lambda p: p["permission"] in ("read", "edit", "write", "bash"), "allow"),
    ]
    for predicate, action in rules:
        if predicate(perm):
            return action
    return None  # Unknown permission — log and let it block (human reviews)
```

This is the OpenCode equivalent of Gas Town's hooks guards. Without it, a single unanticipated permission prompt can stall an agent forever.

---

## 8. Molecules (Multi-Step Workflows)

Molecules are parent issues with child step-issues linked by blocking dependencies.

### Creating a Molecule

```python
def create_molecule(title, steps, project):
    """
    Create a molecule from a list of steps with dependency ordering.

    steps = [
        {"id": "design",    "title": "Design the auth module"},
        {"id": "implement", "title": "Implement auth middleware", "needs": ["design"]},
        {"id": "test",      "title": "Write tests",             "needs": ["implement"]},
        {"id": "review",    "title": "Self-review and cleanup",  "needs": ["test"]},
    ]
    """
    # Create parent issue
    parent = db.create_issue(title=title, type="molecule", project=project)

    # Create child step issues
    step_map = {}
    for step in steps:
        child = db.create_issue(
            title=step["title"],
            type="step",
            parent_id=parent.id,
            project=project,
        )
        step_map[step["id"]] = child.id

    # Wire up dependencies
    for step in steps:
        for dep in step.get("needs", []):
            db.add_dependency(step_map[step["id"]], step_map[dep], type="blocks")

    return parent
```

### Molecule Execution

The orchestrator manages molecule traversal. When an agent completes a step:

1. Mark the step issue `done` and enqueue to `merge_queue`
2. Query for the next ready step within the molecule (`parent_id = molecule.id`)
3. If found: session-cycle the agent onto the next step
4. If not found: all steps done — mark the molecule `done`, release agent (worktree persists until finalization)

```sql
-- Next ready step in a molecule (blocker resolved when done/finalized/canceled)
SELECT i.*
FROM issues i
WHERE i.parent_id = :molecule_id
  AND i.status = 'open'
  AND NOT EXISTS (
    SELECT 1 FROM dependencies d
    JOIN issues blocker ON d.depends_on = blocker.id
    WHERE d.issue_id = i.id
      AND d.type = 'blocks'
      AND blocker.status NOT IN ('done', 'finalized', 'canceled')
  )
ORDER BY i.created_at ASC
LIMIT 1;
```

---

## 9. Prompt Engineering

The orchestrator constructs prompts for agents. The prompt replaces Gas Town's `CLAUDE.md` + `gt prime` system prompt injection.

**Implementation note**: All prompt templates are stored as `.md` files in `src/hive/prompts/` (worker.md, system.md, refinery.md) and loaded at runtime via `string.Template` substitution. This makes prompts easy to hand-edit without touching Python code. The template cache (`_template_cache`) loads each file once on first use.

Gas Town's role prompts reveal that effective agent prompts need much more than just a task description. They encode behavioral contracts, anti-patterns to avoid, and operational procedures. The prompts below incorporate these lessons.

### 9.1 Lessons from Gas Town Role Prompts

Gas Town's polecat prompt is ~540 lines. Most of that isn't task description — it's behavioral conditioning. The key patterns:

| Pattern                          | What It Does                                                 | Why It Matters                                                                                                       |
| -------------------------------- | ------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------- |
| **The Idle Heresy**              | Hammers home that agents must NEVER wait for approval        | LLMs naturally want to pause and confirm. Without strong anti-idle conditioning, agents stall after completing work. |
| **The Approval Fallacy**         | "There is no approval step. When work is done, you act."     | Same root cause — LLMs seek confirmation. The prompt explicitly forbids it.                                          |
| **Directory Discipline**         | "Stay in YOUR worktree. NEVER edit files in the rig root."   | Agents drift out of their sandbox, edit in shared directories, and lose work.                                        |
| **Propulsion Principle**         | "If you find something on your hook, YOU RUN IT."            | Prevents the agent from announcing itself and waiting for instructions.                                              |
| **Escalation Protocol**          | "When blocked, escalate. Do NOT wait for human input."       | Without this, agents hang indefinitely on ambiguous requirements.                                                    |
| **Capability Ledger Motivation** | "Your work is visible. Your CV grows with every completion." | Gives the agent a reason to care about quality — appeal to self-interest in reputation.                              |
| **ZFC Principle**                | "Zero decisions in code. All judgment calls go to models."   | The orchestrator is dumb transport; the agent reasons about edge cases.                                              |

### 9.2 Work Prompt Template

```
You are agent '{agent_name}', working on project '{project}'.

## YOUR TASK

**{issue.title}**

{issue.description}

## CONTEXT

- You are working in a git worktree at: {worktree_path}
- Branch: {branch_name}
- This is step {step_number} of {total_steps} in the workflow "{molecule.title}"

### Previous Steps (already completed)
{completed_steps_summary}

## BEHAVIORAL CONTRACT

### No Approval Step
There is NO approval step. There is NO confirmation. When your implementation is
complete and tests pass, you commit and signal completion. Do NOT:
- Output a summary and wait for "looks good"
- Ask "should I commit this?"
- Sit idle at the prompt after finishing work
- Wait for a human to press enter

### Directory Discipline
**Stay in your worktree: {worktree_path}**
- ALL file edits must be within this directory
- NEVER cd to parent directories to edit files there
- If your worktree lacks dependencies, install them here
- Verify with `pwd` if uncertain

### Escalation Protocol
If you are blocked for more than 2-3 attempts at the same problem:
1. Describe the blocker clearly in your final message
2. Include what you tried and what failed
3. Do NOT wait for human input — signal the blocker and stop

### Quality Contract
Your work is recorded in a permanent capability ledger. Every completion builds
your track record. Execute with care — but execute. Do not over-engineer or
gold-plate. Implement what was asked, verify it works, commit, and stop.

## INSTRUCTIONS

1. Implement the task described above
2. Run tests/linting relevant to your changes
3. Make atomic, well-described git commits as you work
4. When finished, ensure ALL changes are committed and git status is clean
5. Do NOT push — the orchestrator handles that
6. Do NOT create pull requests — the orchestrator handles that

## CONSTRAINTS

- Stay within your worktree directory ({worktree_path})
- Do not modify files outside the project
- Do not access external services unless the task requires it
- If you encounter an issue outside your scope, note it in your final message
```

### 9.3 System Prompt (OpenCode `system` Field)

OpenCode sessions accept a `system` field appended to the system prompt. Use this for project-level context that applies across all steps:

```python
def build_system_prompt(project, agent):
    """Build the system prompt for an agent session."""
    parts = [
        f"You are agent '{agent.name}' working autonomously on the '{project}' project.",
        "You execute tasks to completion without human interaction.",
        "When you finish, ensure all changes are committed with clean git status.",
    ]

    # Inject project-specific CLAUDE.md if it exists
    claude_md = Path(agent.worktree) / "CLAUDE.md"
    if claude_md.exists():
        parts.append(f"\n## Project Instructions\n\n{claude_md.read_text()}")

    return "\n\n".join(parts)
```

### 9.4 Completion Detection (Triple Detection Strategy)

Completion detection uses a **triple detection strategy** to ensure no worker completion is missed:

1. **SSE events** (real-time): The orchestrator listens for `session.status → idle` events via the global SSE stream. This is the fastest path — sub-second detection.

2. **File-based signaling** (deterministic): Workers write a `.hive-result.jsonl` file to their worktree root before emitting the `:::COMPLETION` text signal. The `monitor_agent` loop polls for this file on every `check_interval` timeout. This is the most reliable path — filesystem-based, no dependency on SSE or session status APIs.

3. **Session polling fallback** (catch-all): On every `check_interval` timeout, the orchestrator also calls `get_session_status()` directly to check if the session went idle. This catches cases where the SSE event was missed due to reconnect gaps.

The `monitor_agent` loop runs all three in parallel. Whichever fires first breaks the loop and triggers `handle_agent_complete()`. File-based results take priority over message-parsing heuristics in `assess_completion()`.

**File-based result format** (`.hive-result.jsonl`):

```json
{
  "status": "success",
  "summary": "Added auth middleware",
  "files_changed": ["src/auth.py"],
  "tests_run": true,
  "blockers": [],
  "artifacts": [{ "type": "git_commit", "value": "abc1234" }]
}
```

### 9.5 Structured Completion Signal (Recommended)

Rather than parsing natural language, instruct agents to emit a structured completion block with **artifacts** — machine-readable records of what was produced:

```
When you are finished, output a completion signal as the LAST thing in your response:

:::COMPLETION
status: success | blocked | failed
summary: <one-line summary of what was done>
files_changed: <number of files modified>
tests_run: <yes/no>
blockers: <description if blocked, otherwise "none">
artifacts:
  - type: git_commit
    value: <sha>
  - type: test_command
    value: <command-run>
  - type: test_result
    value: pass | fail | unknown
:::
```

The `artifacts` section provides structured data the orchestrator and refinery can use downstream: commit SHAs for merge queue processing, test commands for re-verification, and test results for the verification gate.

**Completion detection with artifact parsing:**

```python
import re, yaml

COMPLETION_RE = re.compile(r":::COMPLETION\s*\n(.*?):::", re.DOTALL)

def assess_completion(messages) -> CompletionResult:
    """Parse structured completion signal. Fall back to heuristics."""
    last = messages[-1]
    text = " ".join(p["text"] for p in last["parts"] if p["type"] == "text")

    match = COMPLETION_RE.search(text)
    if match:
        payload = yaml.safe_load(match.group(1))
        return CompletionResult(
            success=(payload.get("status") == "success"),
            reason=payload.get("blockers"),
            summary=payload.get("summary"),
            artifacts=payload.get("artifacts", []),
        )

    # Fallback: heuristic assessment (no structured signal found)
    blocker_signals = ["blocked by", "cannot proceed", "need help",
                       "unable to", "escalating", "stuck on"]
    if any(signal in text.lower() for signal in blocker_signals):
        return CompletionResult(success=False, reason=text)

    tool_errors = [p for p in last["parts"]
                   if p["type"] == "tool" and p["state"]["status"] == "error"]
    if tool_errors:
        return CompletionResult(success=False, reason="Tool errors in final turn")

    return CompletionResult(success=True)
```

If no `:::COMPLETION` block is found, the orchestrator falls back to heuristic assessment. In propulsive mode, the finalizer can also **reprompt** the worker with a corrective prompt demanding the structured signal — no indefinite waiting.

---

## 10. The Three Agent Types

The system has three kinds of LLM agent, each with a distinct lifecycle and purpose. The orchestrator is the mechanical runtime that manages them all.

### 10.1 Agent Taxonomy

| Agent        | Lifecycle                                | User-Facing?               | Purpose                                                                                                | Gas Town Equivalent |
| ------------ | ---------------------------------------- | -------------------------- | ------------------------------------------------------------------------------------------------------ | ------------------- |
| **Mayor**    | Persistent, user-managed session         | **Yes** — human chats here | Interpret user intent, decompose into issues via `hive` CLI tools, monitor workers, handle escalations | Mayor               |
| **Refinery** | Persistent session, orchestrator-managed | No                         | Process merge queue, resolve conflicts, verify integration, reason about test failures                 | Refinery            |
| **Workers**  | Ephemeral, one per issue                 | No                         | Implement features, fix bugs, write tests — the actual coding                                          | Polecats            |

```
Human ←→ Mayor (interactive OpenCode TUI/web session)
            │
            │ "build me an auth system with JWT and rate limiting"
            │
            │ Mayor explores codebase, asks clarifying questions, then:
            │   $ hive create "Design auth middleware architecture" "..." --priority 1
            │   $ hive create "Implement JWT validation middleware" "..." --priority 2
            │   $ hive create "Add rate limiting to auth endpoints" "..." --priority 2
            │   $ hive create "Write integration tests for auth flow" "..." --priority 2
            │
            │ Mayor confirms to human: "Created 4 issues, workers will pick them up."
            │
         SQLite DB (issues table)
            │
            ▼
Orchestrator (headless daemon)
  │ Queries ready queue → w-a3f8 is ready
  │ Creates worktree, spawns worker session, claims issue
  ▼
Worker "toast" (headless LLM session)
  │ Implements the design doc
  │ Commits, signals completion
  ▼
Orchestrator
  │ Observes completion via SSE
  │ Marks issue 'done', enqueues to merge_queue
  │ Queries ready queue → w-c7e2 now unblocked
  │ Spawns next worker (or reuses toast on next step)
  ... cycle continues ...
  ▼
            │ Meanwhile, human asks Mayor: "how's it going?"
            │ Mayor runs: $ hive status / $ hive logs -n 10
            │ Mayor: "2 of 4 issues done, 1 in progress, 1 queued."
            │
Orchestrator
  │ All issues done → merge_queue entries queued
  ▼
Refinery (headless LLM session)
  │ Picks up merge_queue entries
  │ Clean rebase? Push through mechanically.
  │ Conflict? Reason about the code, resolve it.
  │ Tests fail? Diagnose: pre-existing or introduced?
  │ On success: issue → 'finalized', worktree torn down
  ▼
main branch updated
```

### 10.2 The Mayor: User-Facing Strategic Brain

The Mayor is a **user-facing** OpenCode session. The human interacts with hive by chatting with the Mayor in an OpenCode TUI or web session. The Mayor has tool access to the `hive` CLI, which it uses to create issues, monitor workers, check status, and manage the system.

This is a fundamental architectural choice: **the Mayor is the interface, not the orchestrator CLI.** The orchestrator is a headless daemon. The Mayor is where the human sits.

**What the Mayor does:**

- Chats directly with the human in a conversational OpenCode session
- Explores the codebase to understand requests (read files, git log, etc.)
- Decomposes requests into concrete issues via `hive create`
- Monitors worker progress via `hive status`, `hive logs`, `hive show`
- Handles escalations — reads failure events, decides to retry/rephrase/ask human
- Answers questions about system state by querying the DB through CLI tools
- Closes/cancels issues via `hive close`

**What the Mayor does NOT do:**

- Write application code (workers do that)
- BUT if the change is trivial enough (<5 minutes, then the Mayor can go ahead and make the change)
- Merge branches (refinery does that)
- Manage sessions, health checks, or worker spawning (orchestrator does that)

**Why tools instead of structured output?**

The previous design had the Mayor emit `:::WORK_PLAN:::` blocks that the orchestrator parsed. This created a rigid, fragile interface — the Mayor had to format output perfectly, and the orchestrator had to parse it. With tool access to the CLI, the Mayor can:

- Create issues one at a time, with full control over titles and descriptions
- Check the result of each operation (`hive show <id>`)
- Correct mistakes immediately (`hive close <id>`, then re-create)
- Query system state naturally as part of the conversation
- Handle complex workflows (molecules, dependencies) incrementally

The `hive` CLI is the Mayor's API. The DB is the shared contract between Mayor and orchestrator.

**Mayor Session Setup:**

The Mayor session is created by the user via `opencode attach` or the web UI, **not** by the orchestrator. The orchestrator no longer owns the Mayor.

```bash
# User starts/attaches to the Mayor session
opencode attach http://localhost:4096 --dir /path/to/project
```

The session's CLAUDE.md (or system prompt) should contain the Mayor prompt. Permissions are permissive — the Mayor is user-supervised:

```python
# Mayor session permissions (set at session creation or via CLAUDE.md)
MAYOR_PERMISSIONS = [
    # Mayor can read the codebase
    {"permission": "read", "pattern": "*", "action": "allow"},
    # Mayor can run hive CLI, git, and other read-only commands
    {"permission": "bash", "pattern": "*", "action": "allow"},
    # Mayor can edit files if the diff is trivial (but the happy path is to defer to workers)
    {"permission": "edit", "pattern": "*", "action": "allow"},
    {"permission": "write", "pattern": "*", "action": "allow"},
    # Mayor CAN ask the human questions (it's user-facing!)
    # (no deny rule for "question")
    # Mayor should stay in the project directory
    {"permission": "external_directory", "pattern": "*", "action": "deny"},
]
```

**Mayor Prompt Template (CLAUDE.md or system prompt):**

```
You are the Mayor — the strategic coordinator of a multi-agent coding system called Hive.

## YOUR ROLE

You are the human's interface to the system. They chat with you to request work,
ask questions, and monitor progress. You decompose their requests into concrete
issues that worker agents will implement.

You do NOT write application code yourself. You plan, decompose, coordinate, and
monitor. Workers do the coding.

## YOUR TOOLS

You have access to the `hive` CLI to manage the system:

### Creating work
  hive create "Issue title" "Detailed description of what to implement" --priority 1
  # Priority: 0=critical, 1=high, 2=normal(default), 3=low, 4=backlog

### Monitoring
  hive status                    # Overview: issue counts, active workers, queues
  hive list                      # All issues
  hive list --status in_progress # Filter by status
  hive show <issue-id>           # Issue details + events
  hive logs -f                   # Live event stream (like tail -f)
  hive logs -n 50                # Last 50 events
  hive logs --issue <id>         # Events for specific issue
  hive logs --agent <id>         # Events for specific agent

### Managing work
  hive close <issue-id>          # Cancel an issue
  hive ready                     # Show the ready queue (unblocked issues)

## WORKFLOW

When the human makes a request:

1. **Understand**: Ask clarifying questions if the request is ambiguous
2. **Explore**: Read relevant code/docs to understand the current state
3. **Decompose**: Break the request into issues, each completable by one worker
4. **Create**: Use `hive create` for each issue. Include enough context in the
   description that a worker can implement it without asking questions.
5. **Confirm**: Tell the human what you've queued and what to expect
6. **Monitor**: Proactively check `hive status` and `hive logs` to report progress

For multi-step workflows, create issues with clear ordering in descriptions.
The orchestrator handles dependency resolution and worker scheduling.

## GUIDELINES

- Each issue should be self-contained. Workers don't see other issues.
- Include relevant file paths, function names, and expected behavior in descriptions.
- Don't over-decompose: a single coherent change is better as one issue.
- Don't under-decompose: if a task touches 5+ files across different domains, split it.
- When workers fail, check `hive show <id>` and `hive logs --issue <id>` to diagnose.
  Then decide: rephrase and re-create the issue, or ask the human for guidance.
- Be honest about what you don't know. Ask the human rather than guessing.
- When the human asks "what's happening?", run `hive status` and `hive logs -n 10`.
```

**The Orchestrator-Mayor Relationship:**

The orchestrator and Mayor are **decoupled**. They share the SQLite DB but never communicate directly:

```
Human ←→ Mayor session (interactive chat)
              │
              │ hive create / hive status / hive logs (bash tool calls)
              ▼
         SQLite DB ←→ Orchestrator (headless daemon)
                           │
                           │ spawns/monitors
                           ▼
                      Worker sessions
```

- The Mayor writes to the DB (via `hive create`, `hive close`)
- The orchestrator reads from the DB (ready queue poll) and writes back (status updates, events)
- The Mayor reads the orchestrator's state (via `hive status`, `hive logs`, `hive show`)
- No RPC, no message passing, no structured output parsing — just shared DB state

This is simpler and more robust than the previous design where the orchestrator
drove the Mayor programmatically and parsed `:::WORK_PLAN:::` blocks.

**Escalation Flow:**

When workers fail, the orchestrator logs events to the DB. The Mayor sees these
via `hive logs` or `hive show`. The human can ask "why did that fail?" and the
Mayor diagnoses by reading the event trail. No special escalation protocol needed —
it's just a conversation between human and Mayor, informed by DB state.

For proactive monitoring, the Mayor can periodically run `hive status` during
a conversation to check on worker progress and surface issues early.

**Mayor Context Management:**

The Mayor session is long-lived but context is finite. Since the Mayor's state
lives in the DB (not in the conversation), the user can start a fresh session
at any time without losing work. The Mayor just runs `hive status` to catch up.

OpenCode's built-in context compaction also helps — the TUI will compress older
turns automatically as the context fills up.

### 10.3 The Refinery: Merge Processor

The Refinery is a persistent OpenCode session that processes the merge queue. Unlike Gas Town's Refinery (which is a full patrol agent running wisps), ours is simpler: the orchestrator notifies it when there's work, and it processes one branch at a time.

**The two-tier approach: mechanical first, LLM for hard cases.**

The merge queue is a dedicated table (`merge_queue`) populated by `handle_agent_complete()` when a worker finishes. The refinery processor pulls from this queue, not from the issues table directly.

```python
async def process_merge_queue():
    """Process the merge_queue. Try mechanical merge first, escalate to Refinery LLM."""
    pending = db.query("""
        SELECT mq.id as queue_id, mq.issue_id, mq.agent_id,
               mq.project, mq.worktree, mq.branch_name,
               i.title, a.name as agent_name
        FROM merge_queue mq
        JOIN issues i ON mq.issue_id = i.id
        LEFT JOIN agents a ON mq.agent_id = a.id
        WHERE mq.status = 'queued'
        ORDER BY mq.enqueued_at ASC
    """)

    for item in pending:
        # Mark queue entry as running
        db.update_merge_queue(item.queue_id, status="running")

        # Tier 1: Try mechanical rebase (no LLM)
        rebase_ok = try_mechanical_rebase(item.worktree)

        if rebase_ok:
            # Tier 1 continued: Run tests
            test_ok = run_tests(item.worktree, item.project)
            if test_ok:
                # Clean merge — push it through
                git_merge_to_main(item.worktree)
                db.update_merge_queue(item.queue_id, status="merged",
                                      completed_at=datetime.utcnow())
                db.update_issue(item.issue_id, status="finalized")
                db.log_event(item.issue_id, item.agent_id, "finalized")
                # NOW tear down the worktree (issue is finalized)
                agent = db.get_agent(item.agent_id)
                if agent:
                    teardown_agent(agent)
                continue

        # Tier 2: Something went wrong — hand to the Refinery LLM
        await send_to_refinery(item, rebase_ok)
```

**Refinery Session Setup:**

```python
def create_refinery_session(project_dir: str) -> str:
    """Create the Refinery's persistent OpenCode session."""
    session = opencode.create_session(
        directory=project_dir,
        title="refinery",
        permission=[
            # Refinery needs full access — it resolves conflicts and runs tests
            {"permission": "*", "pattern": "*", "action": "allow"},
            {"permission": "question", "pattern": "*", "action": "deny"},
            {"permission": "plan_enter", "pattern": "*", "action": "deny"},
            # But scoped to the project directory
            {"permission": "external_directory", "pattern": "*", "action": "deny"},
        ],
    )
    return session["id"]
```

**Refinery Prompt Template:**

```
You are the Refinery — the merge processor for a multi-agent coding system.

## YOUR ROLE

You process branches that workers have completed. Your job:
1. Rebase each branch onto the latest main
2. Resolve any merge conflicts
3. Run tests and verify the integration
4. Merge to main and push

You are NOT a developer. You do not re-implement features. You integrate
completed work. If a branch is fundamentally incompatible with main, you
reject it back to the queue — you don't rewrite it.

## CARDINAL RULES

1. **Sequential processing**: One branch at a time. After every merge, main
   moves. The next branch MUST rebase on the new baseline.

2. **The Verification Gate**: You CANNOT merge without:
   - Tests passing, OR
   - A clear determination that test failures are pre-existing (not introduced
     by this branch)
   If tests fail and you can't determine the cause, REJECT the branch.

3. **No silent failures**: Every merge attempt is logged. Every conflict is
   recorded. Every test failure is attributed.

## CONFLICT RESOLUTION APPROACH

When you hit a rebase conflict:
1. Read the conflicting files — understand what both sides changed
2. If the conflict is mechanical (e.g., both sides added imports): resolve it
3. If the conflict is semantic (both sides changed the same logic differently):
   resolve it if the intent is clear, reject if ambiguous
4. After resolving, run tests to verify the resolution didn't break anything

## COMPLETION SIGNAL

After processing each branch, output:

:::MERGE_RESULT
issue_id: {id}
status: merged | rejected | needs_human
summary: <what happened>
tests_passed: true | false
conflicts_resolved: <count>
:::
```

**Orchestrator-Refinery Interface:**

```python
async def send_to_refinery(item, rebase_succeeded: bool):
    """Hand a merge to the Refinery LLM for processing."""
    if not rebase_succeeded:
        # Abort the failed rebase so the refinery can try its own approach
        subprocess.run(["git", "rebase", "--abort"], cwd=item.worktree)

    context = f"""
Process this branch for merge to main.

Issue: {item.issue_id} — {item.title}
Branch: {item.branch_name}
Branch worktree: {item.worktree}
Agent: {item.agent_name}

{"Mechanical rebase FAILED — conflicts detected. Please resolve them." if not rebase_succeeded
 else "Mechanical rebase succeeded but TESTS FAILED. Please diagnose."}

Steps:
1. cd {item.worktree}
2. git rebase origin/main  (resolve conflicts if any)
3. Run tests: {get_test_command(item.project)}
4. If tests pass: git checkout main && git merge --ff-only {item.branch_name} && git push origin main
5. Output a :::MERGE_RESULT::: block
"""
    opencode.prompt_async(refinery_session_id, context)
    await wait_for_idle(refinery_session_id)

    # Parse result
    messages = opencode.get_messages(refinery_session_id)
    result = parse_merge_result(messages[-1])

    if result.status == "merged":
        db.update_merge_queue(item.queue_id, status="merged",
                              completed_at=datetime.utcnow())
        db.update_issue(item.issue_id, status="finalized")
        db.log_event(item.issue_id, item.agent_id, "finalized",
                     detail=f"conflicts_resolved={result.conflicts_resolved}")
        # Tear down the worktree now that the issue is finalized
        agent = db.get_agent(item.agent_id)
        if agent:
            teardown_agent(agent)
    elif result.status == "rejected":
        db.update_merge_queue(item.queue_id, status="failed")
        db.update_issue(item.issue_id, status="open")  # re-queue for rework
        db.log_event(item.issue_id, item.agent_id, "merge_rejected",
                     detail=result.summary)
    elif result.status == "needs_human":
        db.update_merge_queue(item.queue_id, status="failed")
        db.update_issue(item.issue_id, status="escalated")
        db.log_event(item.issue_id, item.agent_id, "merge_escalated",
                     detail=result.summary)
```

**Refinery Context Cycling:**

Same pattern as the Mayor — state lives in the DB and git, not in the context window. The Refinery can be cycled freely because each merge is independent.

### 10.4 Gas Town Role Mapping (Updated)

| Gas Town Role | This System                         | How                                                                              |
| ------------- | ----------------------------------- | -------------------------------------------------------------------------------- |
| **Mayor**     | **Mayor LLM session (user-facing)** | User chats in OpenCode TUI/web; Mayor uses `hive` CLI tools to manage the system |
| **Witness**   | **Orchestrator code**               | SSE event consumer + staleness checker (deterministic)                           |
| **Deacon**    | **Orchestrator code**               | The orchestrator process itself — always running                                 |
| **Boot**      | **Orchestrator code**               | `reconcile_on_startup()` (deterministic)                                         |
| **Refinery**  | **Refinery LLM session**            | Persistent OpenCode session for merge processing                                 |
| **Dogs**      | **Not needed**                      | Mayor can spawn ad-hoc issues via `hive create` for cross-cutting work           |
| **Polecats**  | **Worker LLM sessions**             | Ephemeral OpenCode sessions, one per issue                                       |

### 10.5 Context Cycling Strategy

All three agent types need context cycling, but with different strategies:

| Agent        | Cycling Trigger                                                                          | State Survives In                                                       |
| ------------ | ---------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| **Mayor**    | User starts a fresh session when context feels stale; OpenCode auto-compacts older turns | DB (issues, events, agent state) — Mayor runs `hive status` to catch up |
| **Refinery** | Token count > threshold, or after each extraordinary merge                               | Git state + DB                                                          |
| **Workers**  | Between molecule steps (always); mid-step if context fills                               | Git worktree (sandbox)                                                  |

The key insight from Gas Town: **state does not live in the context window**. The context is disposable working memory. Durable state lives in the DB (issues, events) and git (code, branches). When any agent cycles, it reads its state from the DB on startup.

```python
async def maybe_cycle_session(agent_type: str, session_id: str):
    """Check if a session needs cycling and do it if so."""
    messages = opencode.get_messages(session_id)
    total_tokens = sum(
        m["info"]["tokens"]["input"] + m["info"]["tokens"]["output"]
        for m in messages
    )

    threshold = {
        "mayor": 120_000,
        "refinery": 100_000,
        "worker": 150_000,
    }[agent_type]

    if total_tokens > threshold:
        # Try compaction first
        opencode.summarize(session_id)
        # Re-check after compaction
        # If still too large, hard cycle (create new session, prime with DB state)
```

---

## 11. Escalation Protocol

Gas Town has a well-defined escalation chain: Polecat → Witness → Mayor → Human. Our chain is simpler but preserves the same guarantees.

### 11.1 Escalation Chain

```
Worker (blocked) → Orchestrator (deterministic) → Mayor (LLM) → Human
```

1. **Worker signals blocker** via structured completion signal
2. **Orchestrator** applies mechanical retries (same issue, fresh session)
3. After N mechanical retries, **orchestrator escalates to Mayor**
4. **Mayor** reasons about the failure — rephrases the issue, breaks it down, or asks the human
5. If the Mayor can't resolve it, **Mayor asks the human** directly

### 11.2 Worker-Side Escalation

Workers signal blockers via the structured completion signal (Section 9.5):

```
:::COMPLETION
status: blocked
summary: Cannot determine correct auth token format
blockers: Docs are ambiguous about JWT vs API key. Tried both, neither works.
:::
```

### 11.3 Orchestrator-Side Escalation (Deterministic)

```python
ESCALATION_THRESHOLDS = {
    "max_retries": 2,          # Retry with fresh session after failure
    "max_agent_switches": 2,   # Try a different worker agent
    "escalate_to_mayor": True, # Then hand to Mayor for reasoning
}

async def retry_or_escalate(agent, issue):
    """Decide whether to retry, reassign, or escalate."""
    retry_count = db.count_events(issue.id, event_type="incomplete")

    if retry_count < ESCALATION_THRESHOLDS["max_retries"]:
        # Retry with same agent, fresh session
        teardown_agent(agent)
        # Issue is now unassigned; main loop will pick it up

    elif retry_count < (ESCALATION_THRESHOLDS["max_retries"]
                        + ESCALATION_THRESHOLDS["max_agent_switches"]):
        # Try a different worker
        db.add_label(issue.id, f"failed-by:{agent.id}")
        teardown_agent(agent)

    else:
        # Multiple workers failed — mark issue as 'failed'
        teardown_agent(agent)
        db.update_issue(issue.id, status="failed")
        db.log_event(issue.id, agent.id, "escalated",
                     detail={"reason": "max retries exceeded"})
        # The Mayor will see this via `hive logs` or `hive show`
        # and can discuss next steps with the human
```

### 11.4 Mayor-Side Escalation (Conversational)

With the Mayor as the user-facing interface, escalation is natural conversation rather than structured message passing. When a worker fails after retries:

1. The orchestrator marks the issue as `failed` and logs the event
2. The Mayor sees this when checking `hive status` or `hive logs`
3. The Mayor tells the human: "Issue w-a3f8 failed after 2 retries. Here's what happened..."
4. The human and Mayor discuss next steps:
   - Rephrase the issue → `hive close w-a3f8` + `hive create "better title" "clearer description"`
   - Break it down → create multiple smaller issues
   - Give up → `hive close w-a3f8`

No `:::HUMAN_QUESTION:::` blocks, no special escalation protocol. The Mayor is already in conversation with the human — it just brings up the failure naturally.

---

## 12. Concurrency Model

### SQLite Concurrency

- **WAL mode**: Multiple readers, single writer. At <30 agents, most operations are reads (checking ready queue). Writes are infrequent (claim, status transition, create).
- **Busy timeout**: 5 seconds handles any write contention from the single orchestrator process.
- **Two writers**: The orchestrator and the Mayor (via `hive` CLI) both write to the DB. WAL mode handles this cleanly — writes are serialized by SQLite's busy timeout. The Mayor's writes are infrequent (creating/closing issues), while the orchestrator writes more often (claiming, status transitions, events).

### OpenCode Concurrency

- **One server, many sessions**: OpenCode handles session isolation internally. Each session has its own LLM context, tool permissions, and directory scope.
- **SSE multiplexing**: The `/global/event` endpoint streams events from all sessions across all directories. The orchestrator dispatches by `sessionID`. Per-directory `/event` endpoints also exist for narrower monitoring.
- **Rate limiting**: OpenCode handles provider rate limits internally with automatic retry (visible via `session.status { type: "retry" }`).

### Agent Concurrency

- **No shared state between agents**: Each agent has its own git worktree. No file conflicts.
- **DB is the coordination point**: Agents don't coordinate with each other. The orchestrator mediates all state via the DB.
- **MAX_AGENTS cap**: The orchestrator limits concurrent agents to avoid overwhelming the LLM provider.

---

## 13. Failure Modes and Recovery

| Failure                        | Detection                                        | Recovery                                                        |
| ------------------------------ | ------------------------------------------------ | --------------------------------------------------------------- |
| **Worker session crashes**     | `session.error` SSE event                        | Unassign issue, retry with fresh worker                         |
| **Worker hangs (no progress)** | Lease expiry + no progress signals (Section 7.3) | Abort session, unassign, reassign                               |
| **Mayor session crashes**      | `session.error` SSE event                        | Recreate Mayor session, prime with DB state                     |
| **Refinery session crashes**   | `session.error` SSE event                        | Recreate Refinery session; pending merges remain in merge_queue |
| **OpenCode server dies**       | Health check on main loop                        | Enter degraded mode; recovery loop attempts reconnect           |
| **Orchestrator crashes**       | External process monitor (systemd, etc.)         | On restart: reconcile DB state with OpenCode sessions           |
| **LLM rate limit**             | `session.status { type: "retry" }`               | OpenCode auto-retries; orchestrator extends leases and waits    |
| **Worker produces bad work**   | Completion assessment or Refinery test gate      | Escalate to Mayor for re-decomposition or re-assignment         |
| **Merge conflict (complex)**   | Refinery reports `needs_human`                   | Escalate to Mayor → human                                       |
| **Permission blocks agent**    | Permission unblocker loop (Section 7.5)          | Auto-resolve via policy, or log for human review                |

### Degraded Mode

When OpenCode (or another critical dependency) becomes unreachable, the orchestrator enters **degraded mode** rather than crashing or spinning in error loops.

**Entering degraded mode:**

```python
def enter_degraded_mode(reason: str):
    """Transition the engine to degraded mode."""
    db.add_label("engine", "orchestrator", f"mode:degraded")
    db.log_event(None, None, "engine.degraded", detail={"reason": reason})
    log.warning(f"Entering degraded mode: {reason}")
```

**Behavior in degraded mode:**

- **Stop dispatching**: No new workers are spawned, no new issues are claimed
- **Keep recovery loop**: A tight loop attempts to reconnect to OpenCode (exponential backoff, capped at 60s)
- **Continue DB sweeps**: Periodic sweeps still run to reconcile DB state — mark sessions as stale, update lease expirations, process any pure-DB operations
- **Preserve in-flight state**: Running agents are not killed. If OpenCode comes back, they may still be executing. The reconciliation step handles this.

**Recovery:**

```python
async def degraded_mode_recovery_loop():
    """Attempt to recover from degraded mode."""
    backoff = 1.0  # seconds
    while db.has_label("engine", "orchestrator", "mode:degraded"):
        try:
            # No dedicated health endpoint — GET /session returns JSON if alive
            sessions = opencode.list_sessions()
            if sessions is not None:
                # OpenCode is back — reconcile and resume
                reconcile_on_startup()
                db.remove_label("engine", "orchestrator", "mode:degraded")
                db.log_event(None, None, "engine.recovered")
                log.info("Exited degraded mode — resuming normal operation")
                return
        except Exception:
            pass

        await asyncio.sleep(min(backoff, 60.0))
        backoff *= 2
```

### Orchestrator Restart Recovery

On restart, the orchestrator reconciles:

```python
def reconcile_on_startup():
    """Bring DB state in sync with OpenCode reality."""
    # 1. Get all sessions OpenCode knows about
    oc_sessions = opencode.list_sessions()
    oc_session_ids = {s["id"] for s in oc_sessions}

    # 2. Find agents whose sessions are gone
    for agent in db.get_working_agents():
        if agent.session_id not in oc_session_ids:
            # Session was lost — mark stalled, unassign work
            db.mark_agent_stalled(agent.id)
            db.unassign_issue(agent.current_issue)

    # 3. Find orphan sessions (OpenCode sessions with no DB agent)
    known_sessions = {a.session_id for a in db.get_all_agents() if a.session_id}
    for session in oc_sessions:
        if session["id"] not in known_sessions:
            opencode.delete_session(session["id"])
```

---

## 14. Capability Ledger (Emergent CVs)

Every event is recorded with an `agent_id`. Over time, this builds a per-agent work history:

```sql
-- Agent CV: what has agent "toast" done?
SELECT
    i.type,
    i.project,
    COUNT(*) as completed,
    AVG(julianday(e.created_at) - julianday(e_claim.created_at)) * 24 as avg_hours
FROM events e
JOIN issues i ON e.issue_id = i.id
LEFT JOIN events e_claim ON e_claim.issue_id = i.id
    AND e_claim.agent_id = e.agent_id
    AND e_claim.event_type = 'claimed'
WHERE e.agent_id = 'agent-toast'
  AND e.event_type = 'done'
GROUP BY i.type, i.project;
```

```sql
-- Capability-based routing: who's best at Go work?
SELECT
    e.agent_id,
    COUNT(*) as go_tasks_completed,
    AVG(julianday(e.created_at) - julianday(e_claim.created_at)) * 24 as avg_hours
FROM events e
JOIN issues i ON e.issue_id = i.id
JOIN labels l ON l.entity_id = i.id AND l.label = 'go'
LEFT JOIN events e_claim ON e_claim.issue_id = i.id
    AND e_claim.agent_id = e.agent_id
    AND e_claim.event_type = 'claimed'
WHERE e.event_type = 'done'
GROUP BY e.agent_id
ORDER BY go_tasks_completed DESC, avg_hours ASC;
```

No special infrastructure. The CV is an emergent property of the event log.

---

## 15. Implementation Results

### What Was Built (Phases 1-4 + Phase 5 partial)

**Implementation completed**: 2026-02-12
**Code location**: `hive/` directory
**Status**: Fully functional with 128 passing unit tests

#### Delivered Features

**Core Infrastructure** ✅

- SQLite database with WAL mode (6 tables + `model` column on issues)
- Ready queue with dependency resolution
- OpenCode HTTP client (full API coverage)
- SSE event stream consumer
- Git worktree management + merge/rebase operations
- Hash-based ID generation

**Orchestration Engine** ✅

- Main event loop with worker pool
- Atomic issue claiming (CAS)
- Lease-based staleness (15min default)
- Permission unblocker (500ms polling)
- Session lifecycle management (create, abort, delete, cleanup)
- Merge queue processor (background task)
- Triple completion detection: SSE events + file-based `.hive-result.jsonl` + session polling fallback
- Aggressive session cleanup on cancel, shutdown, stale detection, and daemon restart
- Stale agent reconciliation on daemon startup (abort orphaned sessions)
- Per-issue model configuration with three-tier resolution

**Agent Types** ✅

- ✅ Queen Bee: Strategic decomposition (user-facing TUI)
- ✅ Workers: Autonomous execution in git worktrees (default: Sonnet)
- ✅ Session cycling for molecules
- ✅ Refinery: LLM merge processor for conflicts and test failures (default: Sonnet)

**Merge Pipeline** ✅

- Two-tier done→finalized pipeline
- Tier 1: Mechanical rebase + test + ff-merge (no LLM)
- Tier 2: Refinery LLM for conflict resolution and test failure diagnosis
- `:::MERGE_RESULT:::` structured signal parsing
- Post-finalization worktree + branch + session teardown
- Configurable test gate (`HIVE_TEST_COMMAND`)
- Feature flag (`HIVE_MERGE_QUEUE_ENABLED`)

**Prompt System** ✅

- Prompts stored as `.md` template files in `src/hive/prompts/`
- `string.Template` substitution (no Jinja2 dependency)
- Templates: `worker.md`, `system.md`, `refinery.md`
- Cached on first load for performance
- Anti-stall behavioral conditioning ("NEVER STOP MID-WORKFLOW")
- File-based completion signal instructions embedded in worker prompt

**Human Interface** ✅

- CLI: 20+ commands (create, list, ready, show, update, cancel, finalize, retry, escalate, molecule, dep, agents, agent, events, close, logs, status, merges, start, daemon, queen)
- `hive logs -f` for live event tailing (JSONL in `--json` mode)
- `hive logs --json` for machine-readable output
- `hive list --sort --reverse --type --assignee --limit` for flexible filtering
- `hive create --model` / `hive update --model` for per-issue model config
- `hive start/stop/daemon status --json` for scripting
- `hive merges` for merge queue visibility
- `hive status` with merge queue stats
- Real-time status monitoring

**Phase 5 Resilience** ✅

- Retry escalation chain: 3-tier (retry same agent up to MAX_RETRIES=2 → switch agent up to MAX_AGENT_SWITCHES=2 → escalate to human)
- Degraded mode: `_opencode_healthy` flag, health check with exponential backoff (5s→60s cap), `log_system_event` for system-level events
- Context cycling for Refinery: `_maybe_cycle_refinery_session()` checks token count after each merge, cycles at REFINERY_TOKEN_THRESHOLD (100K tokens) or >20 messages
- `count_events_by_type()` helper for clean retry/switch counting

**Phase 6 Operational Maturity** ✅

- Structured logging: `src/hive/logging_config.py`, rotating file handler (10MB, 5 backups), HIVE_LOG_LEVEL env var, all print() converted to logger.\*
- Capability-based routing: `get_idle_agents()`, `get_agent_capability_scores()` with project/type/keyword scoring, prefer experienced agents for similar work
- Cost tracking: `get_token_usage()` aggregation from 'tokens_used' events, `hive costs` CLI command with --issue/--agent filters
- `hive watch <issue_id>`: Live SSE streaming from worker sessions, formatted terminal output

**Quality** ✅

- 167 unit tests (100% passing)
- 15 modules + 3 prompt templates, ~6,500 lines production code
- 12 test files, ~4,500 lines test code

---

## 16. Comparison with Gas Town

| Dimension                 | Gas Town                          | This System                                               |
| ------------------------- | --------------------------------- | --------------------------------------------------------- |
| **Agent runtime**         | Claude Code in tmux               | OpenCode server (HTTP API)                                |
| **Work queue**            | Beads (Dolt + JSONL + Git)        | Single SQLite DB                                          |
| **Scheduling**            | `bd ready` CLI query              | Same SQL query, run by orchestrator                       |
| **Strategic brain**       | Mayor (LLM in tmux)               | Mayor (LLM via OpenCode session)                          |
| **Agent monitoring**      | Witness patrol + Deacon heartbeat | Lease-based staleness + SSE events + permission unblocker |
| **Session management**    | tmux sessions, `gt prime`         | OpenCode session lifecycle API                            |
| **Inter-agent comms**     | Mail protocol in beads            | Orchestrator mediates via DB + structured signals         |
| **Crash recovery**        | Witness detects stalled, respawns | Lease expiry + degraded mode + reconciliation             |
| **Merge queue**           | Refinery agent (LLM in tmux)      | `merge_queue` table + Refinery LLM + mechanical fast-path |
| **Multi-project**         | Two-level beads (Town/Rig)        | `project` column + OpenCode `?directory=`                 |
| **Infrastructure agents** | Deacon, Boot, Witness, Dogs       | Orchestrator code (deterministic)                         |
| **Lines of Go/Rust**      | Thousands                         | Zero (Python orchestrator + SQLite)                       |

### What We Gain

- **Simpler infrastructure**: One process, one database, one HTTP API — but with the same strategic capabilities (Mayor + Refinery are LLMs, not stripped out)
- **Real-time observability**: SSE event stream replaces periodic tmux polling
- **Easier debugging**: All state in one SQLite file, queryable with any SQL tool
- **Cleaner separation of concerns**: Deterministic logic in code, ambiguity resolution in LLMs — the ZFC boundary is explicit
- **Lower barrier to entry**: No Dolt, no Go, no custom CLI, no tmux management

### What We Lose

- **Offline/disconnected work**: If the orchestrator is down, nothing runs
- **Git-native work history**: No `git log` of issue state changes (but events table compensates)
- **Zero-infrastructure mode**: Beads needs nothing running; this needs the orchestrator + OpenCode
- **Distributed agent autonomy**: Gas Town agents self-organize via mail; our agents only communicate through the orchestrator. More centralized, but simpler.
- **Battle-tested agent protocols**: Gas Town's propulsion principle, handoff contracts, and polecat lifecycle are production-proven. We're reinterpreting them via OpenCode sessions + structured signals.

---

## 16. Implementation Roadmap

### ✅ Phase 1: Single Worker Loop (COMPLETED)

- [x] SQLite schema + migrations (including `merge_queue` table)
- [x] ID generation (hash-based)
- [x] Ready queue query (with `NOT IN ('done', 'finalized', 'canceled')` blocker check)
- [x] OpenCode server startup/health check
- [x] Single-worker loop: create worktree → create session → prompt → wait → mark done → enqueue merge
- [x] Lease-based staleness tracking (`lease_expires_at`, `last_progress_at`)
- [x] Basic event logging
- [x] Worker prompt template with behavioral contract (Section 9.2)
- [x] Structured completion signal parsing with artifacts (Section 9.5)

### ✅ Phase 2: Multi-Worker + CLI (COMPLETED)

- [x] Worker pool management (spawn, teardown, MAX_AGENTS)
- [x] SSE event consumer with session dispatch (+ payload envelope fix)
- [x] Atomic claim (CAS on issue assignment)
- [x] Concurrent worker execution
- [x] Permission unblocker loop — fast-poll auto-resolve (Section 7.5)
- [x] Session cycling for molecules
- [x] Auto-advance through molecule steps
- [x] Human CLI: 8 commands (create, list, ready, show, close, status, logs, start)
- [x] `hive logs -f` — live event tailing
- [x] Model passthrough — `HIVE_DEFAULT_MODEL` sent to OpenCode on every request
- [x] Stalled agent cleanup — agents marked `failed`, worktrees cleaned up

**Note**: Phase 2 originally included an orchestrator-driven Mayor with `:::WORK_PLAN:::` parsing. This is now **deprecated** in favor of the Mayor-as-interface design (Phase 3). The orchestrator-side Mayor code (`create_mayor_session`, `send_to_mayor`, `handle_user_request`, `maybe_cycle_mayor`) still exists but will be removed in Phase 3.

### ✅ Phase 3: Queen Bee as Interface (COMPLETED)

The Queen Bee (formerly Mayor) is the user-facing interface via OpenCode custom agent. Completed as part of Phase 2 refactor.

- [x] Queen Bee agent definition (`.opencode/agents/queen.md`) with full CLI reference
- [x] `hive queen` launcher command
- [x] 20+ CLI commands for Queen Bee to use
- [x] Orchestrator is purely headless — Queen Bee drives via CLI tools

### ✅ Phase 4: Merge Queue / Refinery (COMPLETED)

The merge queue processor closes the loop from `done` → `finalized` → worktree cleanup.

- [x] Merge queue consumer loop (background task in orchestrator, `MERGE_POLL_INTERVAL` configurable)
- [x] Tier 1 — mechanical fast-path: `git rebase main`, run tests (`HIVE_TEST_COMMAND`), `git merge --ff-only`
- [x] Tier 2 — Refinery LLM session for conflict resolution and test failure diagnosis
- [x] `:::MERGE_RESULT:::` parsing with heuristic fallback
- [x] Refinery prompt template with behavioral contract
- [x] `done` → `finalized` status transition on successful merge
- [x] Worktree + branch teardown after finalization (not after `done`)
- [x] Sequential processing — one merge at a time, each rebased on latest main
- [x] `hive merges` CLI command to view merge queue
- [x] `hive status` enriched with merge queue stats
- [x] `HIVE_MERGE_QUEUE_ENABLED` feature flag
- [x] Lazy refinery session creation (only when needed)

### ✅ Phase 5: Resilience (COMPLETED)

- [x] Session cleanup on cancel/abort/shutdown — sessions are killed (abort + delete) in all exit paths
- [x] Stale agent reconciliation on daemon restart — orphaned sessions aborted, agents reset
- [x] Triple completion detection — SSE events + file-based `.hive-result.jsonl` + session polling fallback
- [x] Anti-stall worker prompt — "NEVER STOP MID-WORKFLOW" behavioral conditioning
- [x] Per-issue model configuration — `HIVE_WORKER_MODEL`, `HIVE_REFINERY_MODEL`, `--model` flag
- [x] Prompt templates — `.md` files in `src/hive/prompts/` for easy hand-editing
- [x] CLI enhancements — `--json` for logs/daemon, `--sort`/`--reverse`/`--type`/`--assignee`/`--limit` for list
- [x] Retry escalation chain — 3-tier: retry same agent (MAX_RETRIES=2) → switch agent (MAX_AGENT_SWITCHES=2) → escalate to human
- [x] Degraded mode — `_opencode_healthy` flag, exponential backoff health check (5s→60s), auto-recovery
- [x] Context cycling for Refinery session — token threshold (100K) or message count (>20)

**Note**: Mayor escalation is now conversational (the human is already chatting with the Mayor), so the old structured escalation chain (worker → orchestrator → Mayor → `:::HUMAN_QUESTION:::`) is no longer needed.

### ✅ Phase 6: Operational Maturity (COMPLETED)

- [x] Capability-based routing — `get_agent_capability_scores()` with project/type/keyword scoring, prefer experienced agents
- [x] Structured logging — `src/hive/logging_config.py`, rotating file handler, all print() → logger.\*, HIVE_LOG_LEVEL
- [x] Cost tracking — `get_token_usage()` aggregation, `hive costs` CLI command with --issue/--agent filters
- [x] `hive watch <issue-id>` — live SSE streaming from worker sessions with formatted terminal output

### Phase 7: Resilience & Extensions

- [ ] Long-lived refinery session — eager creation at startup, periodic health checks, auto-restart on death, stale-result race prevention
- [ ] Dead code cleanup — remove orphaned functions, unused DB tables, unused config values
- [ ] Web dashboard (read-only view of SQLite)
- [ ] Formula/template system for reusable molecule definitions

### Maybe Eventualy

- [ ] Multi-project support (multiple orchestrator instances sharing one OpenCode server)

---

## 17. Open Questions

1. **Completion verification depth**: Section 9.5 proposes a structured completion signal. But how do we handle agents that claim success but produced incorrect output? The Refinery's test gate catches some of this, but not semantic errors. Options: secondary review agent, git diff size heuristics, mandatory test coverage.

2. **Agent-to-agent knowledge transfer**: Gas Town's mail protocol lets agents share findings. Our workers are more isolated — they only communicate through the orchestrator. How does worker B learn what worker A discovered? Options: shared notes in issue metadata, Mayor-mediated context injection into worker prompts, shared scratch files in the repo.

3. **Mayor model selection**: ~~The Mayor reasons about architecture and decomposition — should it use a stronger model (e.g., Opus) while workers use a cheaper model (e.g., Sonnet)?~~ **Resolved**: Three-tier model configuration implemented:
   - `HIVE_DEFAULT_MODEL` (default: `claude-opus-4-6`) — used by the Queen/Mayor
   - `HIVE_WORKER_MODEL` (default: `claude-sonnet-4-20250514`) — used by workers (cheaper for coding tasks)
   - `HIVE_REFINERY_MODEL` (default: `claude-sonnet-4-20250514`) — used by the merge refinery
   - Per-issue model override via `--model` flag on `hive create` / `hive update` (stored in `issues.model` column)
   - Resolution order: `issue.model > Config.WORKER_MODEL > Config.DEFAULT_MODEL`

4. **Scale ceiling**: SQLite WAL mode is good for ~30 concurrent readers. If we need 100+ agents, when and how do we migrate to Postgres? The thin-server architecture makes this swap straightforward.

5. **Cost management**: With Mayor + Refinery + N workers running concurrently, token costs can spike. OpenCode reports token usage per message. How do we implement budget caps, per-agent cost tracking, and graceful degradation when approaching limits?

6. **Mayor scope**: ~~How much should the Mayor do?~~ **Resolved**: The Mayor is the user-facing interface. It does whatever the user asks — from creating issues to monitoring progress to diagnosing failures. Its scope is defined by the conversation, not by the orchestrator. Context management is handled by OpenCode's built-in compaction and the user's ability to start fresh sessions (state is in the DB, not the context).

7. **Refinery worktree management**: The Refinery needs to work in git worktrees to resolve conflicts. Should it have its own persistent worktree, or should the orchestrator create temporary ones per merge? Persistent is simpler; temporary is cleaner.

8. **Multiple OpenCode servers**: A single OpenCode server might bottleneck at many concurrent sessions (Mayor + Refinery + N workers). Should the orchestrator manage multiple server instances, or does OpenCode scale sufficiently within a single process?
