# Technical Design: Lightweight Multi-Agent Orchestrator

_A simplified multi-agent orchestration system inspired by Gas Town, using OpenCode server mode as the agent runtime and a single SQLite database as the work queue._

---

> **Implementation tracking** has moved to [`IMPL_PLAN.md`](IMPL_PLAN.md) (roadmap/checklist) and [`IMPLEMENTATION_NOTES.md`](IMPLEMENTATION_NOTES.md) (delivered features, post-mortems, open questions).

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
        │  │ Queen Bee session (user-facing TUI/web)  │   │
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
| **Queen Bee (LLM)** | The **user-facing** strategic brain. The human chats with the Queen Bee in an OpenCode TUI/web session. The Queen Bee interprets requests, decomposes them into issues/molecules, monitors worker progress, handles escalations, and answers questions — all via tool calls to the `hive` CLI. The Queen Bee is the primary interface to the system. |
| **Orchestrator**    | The **headless** worker pool manager. Polls the ready queue, spawns workers in git worktrees, monitors completion via SSE, handles permissions, processes the merge queue. Handles the _deterministic_ parts: ready queue, CAS claims, health checks, session lifecycle. The orchestrator does NOT interact with the user.               |
| **Refinery (LLM)**  | The merge processor. Easy rebases go through mechanically. Complex merges, conflicts, and integration failures get reasoned about by the Refinery agent. A persistent OpenCode session.                                                                                                                                                  |
| **Workers (LLM)**   | Ephemeral coding agents. One per issue. Implement, test, commit. Spawned on demand, destroyed on completion.                                                                                                                                                                                                                             |
| **SQLite DB**       | Single source of truth for all work items, dependencies, agent state, and events. Shared by the Queen Bee (via CLI tools) and the orchestrator.                                                                                                                                                                                          |
| **Agent Backend**   | Pluggable agent runtime. OpenCode server (HTTP/SSE, API billing) or Claude WS (CLI via `--sdk-url`, subscription billing). See Section 6.                                                                                                                                                                                               |
| **Git Worktrees**   | Per-agent sandboxes, scoped to the backend's session directory                                                                                                                                                                                                                                                                           |

### The Key Split: Deterministic vs. Ambiguous

The system has a clear separation of concerns:

| Concern                                                   | Who Handles It                          | Why                                                          |
| --------------------------------------------------------- | --------------------------------------- | ------------------------------------------------------------ |
| Ready queue computation                                   | Orchestrator (SQL)                      | Deterministic graph query — no judgment needed               |
| Atomic task claiming                                      | Orchestrator (SQL CAS)                  | Database operation — no judgment needed                      |
| Session lifecycle (create, abort, teardown)               | Orchestrator (HTTP)                     | Mechanical — no judgment needed                              |
| Health checks, staleness detection                        | Orchestrator (timer + SSE)              | Threshold-based — no judgment needed                         |
| SSE event dispatch                                        | Orchestrator (event loop)               | Routing — no judgment needed                                 |
| "Build me an auth system" → concrete issues               | **Queen Bee** (LLM, via `hive create`)      | User chats with Queen Bee; Queen uses CLI tools to create issues |
| Monitoring system state and progress                      | **Queen Bee** (LLM, via `hive status/logs`) | Queen Bee proactively checks on workers, reports back to user    |
| Prioritizing competing work items                         | **Queen Bee** (LLM)                         | Requires understanding urgency, dependencies, context            |
| Handling escalations from stuck workers                   | **Queen Bee** (LLM, via `hive` tools)       | Reads failure details, decides to retry/rephrase/ask user        |
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

### 3.2 Ephemeral Agent Lifecycle

Gas Town decomposed agent state into three layers (Identity, Sandbox, Session) with independent lifecycles. We originally kept the same decomposition, but in practice **agent identity proved unnecessary**.

`spawn_worker()` always creates a fresh agent with a random ID — no agent is ever reused. The result was thousands of idle agent rows accumulating as garbage. The original intent (long-lived identities that build a CV over time) never materialized because the unit of analysis for performance tracking is **model × issue type → outcome**, not agent identity.

**Current design:** Agents are ephemeral. They exist only while executing work.

| Layer        | Gas Town                                | This System                                                    |
| ------------ | --------------------------------------- | -------------------------------------------------------------- |
| **Identity** | Agent bead in Dolt, CV chain            | Ephemeral row in `agents` table; deleted after merge/cleanup   |
| **Sandbox**  | Git worktree, managed by `gt` CLI       | Git worktree, managed by orchestrator                          |
| **Session**  | Claude Code in tmux, managed by Witness | Backend session via HTTP API                                   |

The agent ID serves as a **correlation key** during execution (linking events, notes, and merge entries within a single run) but has no meaningful identity beyond that. The `model` field is denormalized onto key events (`worker_started`, `completed`, `incomplete`, `agent_switch`) so the events table is self-contained for all analytics queries — no join to `agents` needed.

Agent rows are deleted after successful merge and purged on daemon startup (idle/failed leftovers from previous runs). The `agent_id` columns in `events`, `notes`, and `merge_queue` are retained as correlation keys but have no FK constraint — events outlive agents by design.

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

Every issue completion is recorded as an event with the `model` denormalized into the event detail JSON. "Which model is best at Go work?" becomes a SQL query over the events table joined with issue metadata — no join to the (ephemeral) agents table needed.

```sql
-- Model performance by issue type
SELECT
    json_extract(e.detail, '$.model') as model,
    i.type,
    COUNT(*) FILTER (WHERE e.event_type = 'completed') as successes,
    COUNT(*) FILTER (WHERE e.event_type IN ('incomplete', 'failed')) as failures
FROM events e
JOIN issues i ON e.issue_id = i.id
WHERE e.event_type IN ('completed', 'incomplete')
  AND json_extract(e.detail, '$.model') IS NOT NULL
GROUP BY model, i.type;
```

This is an emergent property of the event log — no special infrastructure needed.

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
| **Mail protocol between agents**                    | Orchestrator mediates; Queen Bee/Refinery communicate via DB, not mail                                                                                                                                                 |
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

Seven core tables (including `notes`). Everything else is derived.

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
-- AGENTS: ephemeral execution identity (deleted after merge/cleanup)
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
-- agent_id is a correlation key, not a live FK — events outlive agents
----------------------------------------------------------------------
CREATE TABLE events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id    TEXT REFERENCES issues(id),
    agent_id    TEXT,              -- correlation key (no FK — events outlive agents)
    event_type  TEXT NOT NULL,    -- created|claimed|done|finalized|failed|escalated|retry|merged|...
    detail      TEXT,             -- JSON: old/new values, comments, artifacts, model, etc.
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
    agent_id    TEXT,                          -- correlation key (no FK — may outlive agent)
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
-- NOTES: inter-agent knowledge transfer (see Section 16)
----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id    TEXT REFERENCES issues(id),
    agent_id    TEXT,              -- correlation key (no FK — notes outlive agents)
    category    TEXT NOT NULL DEFAULT 'discovery',  -- discovery|gotcha|dependency|pattern|context
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_notes_issue ON notes(issue_id);
CREATE INDEX IF NOT EXISTS idx_notes_category ON notes(category);
CREATE INDEX IF NOT EXISTS idx_notes_created ON notes(created_at);
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

## 6. Agent Backend Architecture

The orchestrator does not talk directly to any specific LLM runtime. Instead, it programs against a **backend interface** — a set of methods for session lifecycle, message dispatch, and event monitoring. This allows different agent runtimes to be swapped in without changing the orchestrator or merge processor.

### 6.0 The Backend Interface

Any backend must implement two interfaces that the orchestrator consumes:

**Session lifecycle** (consumed as `self.opencode`):

| Method | Purpose |
|--------|---------|
| `create_session(directory, title, permissions)` | Create an agent session scoped to a worktree |
| `send_message_async(session_id, parts, model, system, directory)` | Send a prompt (fire-and-forget) |
| `get_session_status(session_id)` | Return `{"type": "idle"\|"busy"\|"error"}` |
| `get_messages(session_id, limit)` | Return messages (for token usage logging) |
| `abort_session(session_id)` | Stop a running session |
| `delete_session(session_id)` | Remove a session entirely |
| `cleanup_session(session_id)` | Best-effort abort + delete |
| `list_sessions()` | List active sessions (for health checks, reconciliation) |
| `get_pending_permissions(directory)` | Get blocked permission requests |
| `reply_permission(request_id, reply)` | Resolve a permission request |

**Event monitoring** (consumed as `self.sse_client`):

| Method | Purpose |
|--------|---------|
| `on(event_type, handler)` | Register handler for `session.status`, `session.error`, `permission.request` |
| `connect_with_reconnect()` | Start the event stream (runs as background task) |
| `stop()` | Shut down the event stream |

The orchestrator is wired up in `daemon.py`:

```python
if Config.BACKEND == "claude":
    backend = ClaudeWSBackend(host=..., port=...)
    orchestrator = Orchestrator(
        opencode_client=backend,
        sse_client=backend,   # same object serves both roles
    )
else:
    orchestrator = Orchestrator(
        opencode_client=OpenCodeClient(...),
        sse_client=SSEClient(...),   # separate objects
    )
```

A backend can serve as both the session client AND the event emitter (like `ClaudeWSBackend` does), or they can be separate objects (like OpenCode, which has a distinct `SSEClient`).

**Adding a new backend** requires implementing the methods above. The orchestrator, merge processor, and all monitoring logic work unchanged. Key constraints:

- `get_session_status` must return `{"type": "idle"}` when a session finishes its prompt — this triggers completion detection.
- `get_messages` must return messages with `metadata.input_tokens` and `metadata.output_tokens` fields — this is what `_log_token_usage` reads.
- The event emitter must fire `session.status` events with `{"sessionID": ..., "status": {"type": "idle"}}` — this wakes up `monitor_agent` via asyncio.Event.
- Completion detection is primarily file-based (`.hive-result.jsonl`), so the backend only needs to signal when the session goes idle, not parse output.

### 6.0.1 Current Backends

| Backend | Config | Runtime | Billing | Dependencies |
|---------|--------|---------|---------|-------------|
| **OpenCode** (`opencode`) | `HIVE_BACKEND=opencode` | OpenCode server (HTTP + SSE) | Anthropic API key | OpenCode binary, running server |
| **Claude WS** (`claude`) | `HIVE_BACKEND=claude` | Claude Code CLI via `--sdk-url` | Subscription credits (Pro/Max) | `claude` binary, active subscription |

The OpenCode backend is the original and default. The Claude WS backend was added to let users run Hive on their existing Claude Code subscription without needing an API key or running a separate server.

### 6.0.2 Claude WS Backend Details

Instead of talking to a server, Hive **becomes** the server. `ClaudeWSBackend` runs an aiohttp WebSocket server that Claude CLI processes connect to:

```
Hive (WS Server on :8765)
  ├── /agent/{session_id}  ← Claude CLI connects here
  │
  ├── create_session() → spawns: claude --sdk-url ws://127.0.0.1:8765/agent/ID
  │                               --print --output-format stream-json
  │                               --permission-mode bypassPermissions
  │                               --model claude-sonnet-4-20250514
  │                               -p "" --cwd /path/to/worktree
  │
  ├── send_message_async() → sends {"type": "user", "message": {...}} over WS
  │
  ├── _route_message() ← receives assistant/result/control_request messages
  │   └── on "result" → emits session.status idle event (SSE-compatible)
  │
  └── delete_session() → terminates the CLI process
```

Key design decisions:

- **`bypassPermissions` mode**: Workers are trusted to operate freely in their worktrees. No permission prompts are sent over the wire, so `get_pending_permissions` returns `[]`.
- **Conservative concurrency**: Default max 3 concurrent CLI processes (`HIVE_CLAUDE_WS_MAX_CONCURRENT`), enforced by asyncio.Semaphore. Subscription rate limits are different from API limits.
- **Message format translation**: The WS protocol uses Anthropic message format (`message.usage.input_tokens`). `get_messages()` translates to OpenCode format (`metadata.input_tokens`) so `_log_token_usage` works unchanged.
- **Process = session**: Each session is a CLI process. `create_session` spawns it, `delete_session` kills it. No external server to manage.
- **System prompt via `initialize`**: On first `send_message_async` with a `system` param, sends an `initialize` control request with `appendSystemPrompt` before the user message. This adds to (rather than replaces) Claude Code's built-in system prompt.

---

### 6.1 OpenCode Backend

_The remainder of Section 6 describes the OpenCode backend specifically._

### 6.1.1 Server Lifecycle

The orchestrator starts a single OpenCode server instance. All agent sessions run within it.

```bash
OPENCODE_SERVER_PASSWORD=$SECRET \
  bun run --cwd packages/opencode --conditions=browser src/index.ts serve \
  --port 4096 --hostname 127.0.0.1
```

One server, many sessions. Each session is scoped to a directory (git worktree) via the `?directory=` query parameter.

### 6.1.2 Session-as-Agent Mapping

Each active agent maps 1:1 to an OpenCode session:

```
Agent "toast"
  ├── Identity: agents table row (ephemeral — deleted after merge)
  ├── Sandbox:  ~/work/polecats/toast/ (git worktree)
  └── Session:  backend session (ephemeral)
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

### 6.1.3 Event-Driven Monitoring

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

### 6.1.4 Session Cycling (Handoff)

When an agent completes a molecule step and needs a fresh context for the next step:

1. Orchestrator observes step completion (via SSE or polling session messages)
2. Orchestrator aborts the old session: `POST /session/:id/abort`
3. Orchestrator creates a new session scoped to the same worktree
4. Orchestrator sends the next step as a prompt

The sandbox (git worktree) persists across session cycles. Only the LLM context resets. This is the same three-layer lifecycle as Gas Town, mediated by HTTP instead of tmux.

### 6.1.5 Directory Scoping for Multi-Project

OpenCode's `?directory=` parameter maps directly to per-agent git worktrees:

```
POST /session?directory=/home/user/work/polecats/toast    # Agent toast
POST /session?directory=/home/user/work/polecats/shadow   # Agent shadow
POST /session?directory=/home/user/work/polecats/copper   # Agent copper
```

Each session gets its own isolated LSP server, file watcher, and tool permissions — scoped to that worktree. Agents cannot interfere with each other's sandboxes.

---

## 7. Orchestrator Design

The orchestrator is a **headless** long-running daemon. It does NOT interact with the user — that's the Queen Bee's job. The orchestrator polls the DB for ready work (created by the Queen Bee via `hive create`), spawns workers, monitors them via SSE, and processes the merge queue.

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
                db.log_event(agent.current_issue, agent.id, "stalled",
                             detail={"last_progress": str(agent.last_progress_at),
                                     "lease_expired": str(agent.lease_expires_at)})
                # IMPORTANT: route through escalation chain (INV-1).
                # Do NOT unconditionally unassign — that causes infinite
                # spawn loops. See Section 18.
                handle_agent_failure(agent, reason="stalled")

        else:
            # Unknown/error state — kill and escalate via retry chain
            opencode.abort_session(agent.session_id)
            db.mark_agent_stalled(agent.id)
            db.log_event(agent.current_issue, agent.id, "stalled")
            handle_agent_failure(agent, reason="unknown_state")


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
Your work is recorded in the capability ledger. Every completion builds
your model's track record. Execute with care — but execute. Do not over-engineer or
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

2. **File-based signaling** (deterministic): Workers write a `.hive-result.jsonl` file to their worktree root as the sole completion signal. The `monitor_agent` loop polls for this file on every `check_interval` timeout. This is the most reliable path — filesystem-based, no dependency on SSE or session status APIs.

3. **Session polling fallback** (catch-all): On every `check_interval` timeout, the orchestrator also calls `get_session_status()` directly to check if the session went idle. This catches cases where the SSE event was missed due to reconnect gaps.

The `monitor_agent` loop runs all three in parallel. Whichever fires first breaks the loop and triggers `handle_agent_complete()`. `assess_completion()` reads the file-based result directly — no message parsing or heuristics.

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

### 9.5 File-Based Completion Signal

All agents (workers and refinery) use the same file-based completion mechanism: write a `.hive-result.jsonl` file to the worktree root. This is the **sole** completion signal — no text-embedded signals, no regex parsing, no heuristic fallbacks.

**Worker result schema:**

```json
{"status": "success", "summary": "Added auth middleware", "files_changed": ["src/auth.py"], "tests_added": ["tests/test_auth.py::test_login"], "tests_run": true, "test_command": "pytest tests/", "blockers": [], "artifacts": [{"type": "git_commit", "value": "abc1234"}]}
```

**Refinery result schema:**

```json
{"status": "merged", "summary": "Rebased and resolved 2 conflicts, all tests pass", "tests_passed": true, "tests_added": true, "conflicts_resolved": 2, "warnings": ""}
```

**Completion assessment** reads the file directly — no message parsing:

```python
def assess_completion(messages, file_result=None) -> CompletionResult:
    """Assess completion based on file-based result only."""
    if file_result is not None:
        return CompletionResult(
            success=(file_result.get("status") == "success"),
            reason=...,
            summary=file_result.get("summary", ""),
            artifacts=...,
        )
    # No file result = worker didn't write completion signal = failure
    return CompletionResult(success=False, reason="Worker did not write completion signal")
```

This consolidation eliminates the previous `:::COMPLETION` and `:::MERGE_RESULT:::` text-embedded signals, the YAML-in-free-text regex parsing, and the heuristic fallback code. One mechanism, one file name, deterministic parsing.

---

## 10. The Three Agent Types

The system has three kinds of LLM agent, each with a distinct lifecycle and purpose. The orchestrator is the mechanical runtime that manages them all.

### 10.1 Agent Taxonomy

| Agent        | Lifecycle                                | User-Facing?               | Purpose                                                                                                | Gas Town Equivalent |
| ------------ | ---------------------------------------- | -------------------------- | ------------------------------------------------------------------------------------------------------ | ------------------- |
| **Queen Bee** | Persistent, user-managed session        | **Yes** — human chats here | Interpret user intent, decompose into issues via `hive` CLI tools, monitor workers, handle escalations | Mayor (Gas Town)    |
| **Refinery** | Persistent session, orchestrator-managed | No                         | Process merge queue, resolve conflicts, verify integration, reason about test failures                 | Refinery            |
| **Workers**  | Ephemeral, one per issue                 | No                         | Implement features, fix bugs, write tests — the actual coding                                          | Polecats            |

```
Human ←→ Queen Bee (interactive OpenCode TUI/web session)
            │
            │ "build me an auth system with JWT and rate limiting"
            │
            │ Queen Bee explores codebase, asks clarifying questions, then:
            │   $ hive create "Design auth middleware architecture" "..." --priority 1
            │   $ hive create "Implement JWT validation middleware" "..." --priority 2
            │   $ hive create "Add rate limiting to auth endpoints" "..." --priority 2
            │   $ hive create "Write integration tests for auth flow" "..." --priority 2
            │
            │ Queen Bee confirms to human: "Created 4 issues, workers will pick them up."
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
            │ Meanwhile, human asks Queen Bee: "how's it going?"
            │ Queen Bee runs: $ hive status / $ hive logs -n 10
            │ Queen Bee: "2 of 4 issues done, 1 in progress, 1 queued."
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

### 10.2 The Queen Bee: User-Facing Strategic Brain

The Queen Bee is a **user-facing** OpenCode session. The human interacts with hive by chatting with the Queen Bee in an OpenCode TUI or web session. The Queen Bee has tool access to the `hive` CLI, which it uses to create issues, monitor workers, check status, and manage the system.

This is a fundamental architectural choice: **the Queen Bee is the interface, not the orchestrator CLI.** The orchestrator is a headless daemon. The Queen Bee is where the human sits.

**What the Queen Bee does:**

- Chats directly with the human in a conversational OpenCode session
- Explores the codebase to understand requests (read files, git log, etc.)
- Decomposes requests into concrete issues via `hive create`
- Monitors worker progress via `hive status`, `hive logs`, `hive show`
- Handles escalations — reads failure events, decides to retry/rephrase/ask human
- Answers questions about system state by querying the DB through CLI tools
- Cancels issues via `hive cancel`, finalizes via `hive finalize`

**What the Queen Bee does NOT do:**

- Write application code (workers do that)
- BUT if the change is trivial enough (<5 minutes, then the Queen Bee can go ahead and make the change)
- Merge branches (refinery does that)
- Manage sessions, health checks, or worker spawning (orchestrator does that)

**Why tools instead of structured output?**

The previous design had the Queen Bee emit `:::WORK_PLAN:::` blocks that the orchestrator parsed. This created a rigid, fragile interface — the Queen Bee had to format output perfectly, and the orchestrator had to parse it. With tool access to the CLI, the Queen Bee can:

- Create issues one at a time, with full control over titles and descriptions
- Check the result of each operation (`hive show <id>`)
- Correct mistakes immediately (`hive cancel <id>`, then re-create)
- Query system state naturally as part of the conversation
- Handle complex workflows (molecules, dependencies) incrementally

The `hive` CLI is the Queen Bee's API. The DB is the shared contract between Queen Bee and orchestrator.

**Queen Bee Session Setup:**

The Queen Bee session is created by the user via `opencode attach` or the web UI, **not** by the orchestrator. The orchestrator no longer owns the Queen Bee.

```bash
# User starts/attaches to the Queen Bee session
opencode attach http://localhost:4096 --dir /path/to/project
```

The session's CLAUDE.md (or system prompt) should contain the Queen Bee prompt. Permissions are permissive — the Queen Bee is user-supervised:

```python
# Queen Bee session permissions (set at session creation or via CLAUDE.md)
QUEEN_PERMISSIONS = [
    # Queen Bee can read the codebase
    {"permission": "read", "pattern": "*", "action": "allow"},
    # Queen Bee can run hive CLI, git, and other read-only commands
    {"permission": "bash", "pattern": "*", "action": "allow"},
    # Queen Bee can edit files if the diff is trivial (but the happy path is to defer to workers)
    {"permission": "edit", "pattern": "*", "action": "allow"},
    {"permission": "write", "pattern": "*", "action": "allow"},
    # Queen Bee CAN ask the human questions (it's user-facing!)
    # (no deny rule for "question")
    # Queen Bee should stay in the project directory
    {"permission": "external_directory", "pattern": "*", "action": "deny"},
]
```

**Queen Bee Prompt Template (CLAUDE.md or system prompt):**

```
You are the Queen Bee — the strategic coordinator of a multi-agent coding system called Hive.

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
  hive cancel <issue-id>         # Cancel an issue
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

**The Orchestrator-Queen Bee Relationship:**

The orchestrator and Queen Bee are **decoupled**. They share the SQLite DB but never communicate directly:

```
Human ←→ Queen Bee session (interactive chat)
              │
              │ hive create / hive status / hive logs (bash tool calls)
              ▼
         SQLite DB ←→ Orchestrator (headless daemon)
                           │
                           │ spawns/monitors
                           ▼
                      Worker sessions
```

- The Queen Bee writes to the DB (via `hive create`, `hive cancel`)
- The orchestrator reads from the DB (ready queue poll) and writes back (status updates, events)
- The Queen Bee reads the orchestrator's state (via `hive status`, `hive logs`, `hive show`)
- No RPC, no message passing, no structured output parsing — just shared DB state

This is simpler and more robust than the previous design where the orchestrator
drove the Queen Bee programmatically and parsed `:::WORK_PLAN:::` blocks.

**Escalation Flow:**

When workers fail, the orchestrator logs events to the DB. The Queen Bee sees these
via `hive logs` or `hive show`. The human can ask "why did that fail?" and the
Queen Bee diagnoses by reading the event trail. No special escalation protocol needed —
it's just a conversation between human and Queen Bee, informed by DB state.

For proactive monitoring, the Queen Bee can periodically run `hive status` during
a conversation to check on worker progress and surface issues early.

**Queen Bee Context Management:**

The Queen Bee session is long-lived but context is finite. Since the Queen Bee's state
lives in the DB (not in the conversation), the user can start a fresh session
at any time without losing work. The Queen Bee just runs `hive status` to catch up.

OpenCode's built-in context compaction also helps — the TUI will compress older
turns automatically as the context fills up.

### 10.3 The Refinery: Long-Lived Merge Processor

The Refinery is a **persistent, long-lived** OpenCode session that processes the merge queue. Unlike workers (which are ephemeral, one per issue), the Refinery session stays alive across merges, accumulating project context. Each merge is a new message in the same conversation.

**Why long-lived?**
- Session creation is expensive (~2-3s per OpenCode session)
- The Refinery accumulates context about the project — it sees what changed across merges
- Merges are sequential anyway (each rebases on latest main), so there's no parallelism benefit to separate sessions

#### Two-Tier Architecture

The merge queue is a dedicated table (`merge_queue`) populated by `handle_agent_complete()` when a worker finishes. The merge processor pulls from this queue one at a time.

**Tier 1: Mechanical merge (no LLM).** The processor attempts a pure-git workflow:

1. `git rebase main` in the worker's worktree
2. Run tests (if `Config.TEST_COMMAND` is set)
3. `git merge --ff-only` to main

If all three succeed — done. Issue finalized, worktree torn down, branch deleted. No LLM cost.

**Tier 2: Refinery LLM (when mechanical fails).** If rebase has conflicts or tests fail, the merge is handed to the Refinery session. The Refinery gets a prompt explaining the branch, the problem (conflicts or test failures), and instructions to resolve and write a `.hive-result.jsonl` file with status `merged`, `rejected`, or `needs_human`.

#### Session Lifecycle

```
Orchestrator.start()
  └─ MergeProcessor.initialize()     # Eager session creation (warm start)
      └─ _ensure_refinery_session()  # Creates OpenCode session, stores session_id

merge_processor_loop (background asyncio task, every MERGE_POLL_INTERVAL):
  ├─ process_queue_once()            # Pop next queued merge entry
  │   ├─ _try_mechanical_merge()     # Tier 1: rebase → test → ff-merge
  │   │   └─ SUCCESS → _finalize_issue() → teardown worktree/branch/agent
  │   │   └─ FAILURE → fall through to Tier 2
  │   └─ _send_to_refinery()         # Tier 2: LLM-assisted merge
  │       ├─ _ensure_refinery_session()   # Reuse existing or create new
  │       ├─ Record pre-send message count (stale-result fence)
  │       ├─ build_refinery_prompt()      # Context about the branch + problem
  │       ├─ send_message_async()         # New message in existing conversation
  │       ├─ Post-send status check (0.5s — verify session became active)
  │       ├─ _wait_for_refinery()         # Poll until idle, read .hive-result.jsonl
  │       ├─ Process result:
  │       │   merged     → _finalize_issue()
  │       │   rejected   → reopen issue for rework
  │       │   needs_human → escalate
  │       └─ _maybe_cycle_refinery_session()  # Check context size
  │
  └─ health_check() (every ~60s)     # Verify session alive, recreate if dead

Orchestrator.shutdown()
  └─ MergeProcessor.shutdown()       # Abort + delete refinery session
```

#### Hardening Mechanisms

The refinery session has seven protections against failure modes:

**1. Force-reset on any exception** (`_force_reset_refinery_session`): If `_send_to_refinery` throws, the session ID is immediately invalidated — abort, delete, set to `None`. The next merge gets a clean session. Prevents a corrupted session from poisoning subsequent merges.

**2. Consecutive error bail-out** in `_wait_for_refinery`: If polling the session status fails 5 times in a row, it returns `needs_human` instead of blocking for the full timeout. Prevents silent hangs when OpenCode is in a bad state.

**3. Eager creation at startup** (`initialize()`): The session is pre-created when the orchestrator boots so it's warm for the first merge. If this fails, falls back to lazy creation on first use — non-fatal.

**4. Periodic health checks** (`health_check()`): Every ~60 seconds, the merge loop checks if the session is still alive via `get_session_status`. If dead, it recreates. Catches OpenCode server restarts.

**5. Message count fence** (stale-result race prevention): Before sending a message, the processor records the current message count. When the session goes idle, it verifies there are new messages beyond that count. This prevents the race where the session was already idle before the prompt was processed — without the fence, the processor would read stale results from the previous merge.

**6. Post-send status verification**: After sending the message, waits 0.5s and checks if the session transitioned from idle to active. If not, the message wasn't picked up — raises an error which triggers the force-reset.

**7. Auto-restart of the loop itself** (`_on_merge_task_done`): The `merge_processor_loop` runs as an asyncio task with a done callback. If the task dies from any exception (including `BaseException`), and the orchestrator is still running, it automatically creates a new task. This makes the merge loop self-healing even against unexpected crashes.

#### Context Cycling

The session accumulates context with each merge. `_maybe_cycle_refinery_session()` runs after each successful merge and checks:
- Token usage from message metadata (if available)
- Message count (>20 as a fallback threshold)

If usage exceeds `Config.REFINERY_TOKEN_THRESHOLD` (100K tokens), the session is killed and the next merge creates a fresh one. Each merge is independent (state lives in git, not in the context), so cycling is safe.

#### Cleanup Boundary

When an issue is finalized (`_finalize_issue` → `_teardown_after_finalize`):
- The **worker's** OpenCode session is aborted and deleted
- The worker's worktree is removed
- The worker's branch is deleted
- The agent is marked idle

The **refinery** session stays alive — it's shared across all merges. It's only killed on orchestrator shutdown, context cycling, or force-reset after an error.

#### Refinery Session Permissions

```python
session = opencode.create_session(
    directory=project_path,   # Main repo — NOT a worktree
    title="refinery",
    permissions=[
        {"permission": "*", "pattern": "*", "action": "allow"},
        {"permission": "question", "pattern": "*", "action": "deny"},
        {"permission": "plan_enter", "pattern": "*", "action": "deny"},
        {"permission": "external_directory", "pattern": "*", "action": "deny"},
    ],
)
```

The Refinery gets full tool access (it needs to rebase, edit conflicting files, run tests) but is denied question/plan mode (must be autonomous) and external directory access (scoped to the project).

**Note:** The Refinery session is scoped to the **main project directory** (not a worktree), but it operates on worker worktrees by `cd`-ing into them. This is because the Refinery needs to run `git merge --ff-only` on main and also work inside worktrees — it straddles both.

#### Refinery Prompt Template

The prompt template lives at `src/hive/prompts/refinery.md`. Each merge gets a fresh prompt injected as a new message. Key variables:

- `${issue_id}`, `${issue_title}` — what's being merged
- `${branch_name}`, `${worktree_path}` — where the code lives
- `${problem}` — "rebase conflict" or "test failure" with details
- `${test_step}` — the test command to run (from `Config.TEST_COMMAND`)

The prompt instructs the Refinery to:
1. Check worktree state, abort any in-progress rebase
2. Run `git rebase main` and resolve conflicts
3. Run tests
4. Write a `.hive-result.jsonl` file with status, summary, and conflict count

The orchestrator reads `.hive-result.jsonl` (via `read_result_file()`) and dispatches on the status field. This is the same file-based mechanism used by workers — one unified inter-agent signal pattern. The actual `git merge --ff-only` to main is done by the orchestrator after the Refinery succeeds — the Refinery just gets the branch into a mergeable state.

After reading the result, the orchestrator also harvests any `.hive-notes.jsonl` the Refinery wrote (conflict patterns, integration gotchas) and saves them to the notes DB for future workers.

### 10.4 Gas Town Role Mapping (Updated)

| Gas Town Role | This System                         | How                                                                              |
| ------------- | ----------------------------------- | -------------------------------------------------------------------------------- |
| **Mayor**     | **Queen Bee LLM session (user-facing)** | User chats in OpenCode TUI/web; Queen Bee uses `hive` CLI tools to manage the system |
| **Witness**   | **Orchestrator code**               | SSE event consumer + staleness checker (deterministic)                           |
| **Deacon**    | **Orchestrator code**               | The orchestrator process itself — always running                                 |
| **Boot**      | **Orchestrator code**               | `reconcile_on_startup()` (deterministic)                                         |
| **Refinery**  | **Refinery LLM session**            | Persistent OpenCode session for merge processing                                 |
| **Dogs**      | **Not needed**                      | Queen Bee can spawn ad-hoc issues via `hive create` for cross-cutting work       |
| **Polecats**  | **Worker LLM sessions**             | Ephemeral OpenCode sessions, one per issue                                       |

### 10.5 Context Cycling Strategy

All three agent types need context cycling, but with different strategies:

| Agent        | Cycling Trigger                                                                          | State Survives In                                                       |
| ------------ | ---------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| **Queen Bee** | User starts a fresh session when context feels stale; OpenCode auto-compacts older turns | DB (issues, events, agent state) — Queen Bee runs `hive status` to catch up |
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
        "queen": 120_000,
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

Gas Town has a well-defined escalation chain: Polecat → Witness → Mayor → Human. Ours is simpler but preserves the same guarantees.

### 11.1 Escalation Chain

```
Worker (blocked) → Orchestrator (deterministic) → Queen Bee (LLM) → Human
```

1. **Worker signals blocker** via structured completion signal
2. **Orchestrator** applies mechanical retries (same issue, fresh session)
3. After N mechanical retries, **orchestrator escalates to Queen Bee**
4. **Queen Bee** reasons about the failure — rephrases the issue, breaks it down, or asks the human
5. If the Queen Bee can't resolve it, **Queen Bee asks the human** directly

### 11.2 Worker-Side Escalation

Workers signal blockers via `.hive-result.jsonl` (Section 9.5):

```json
{"status": "blocked", "summary": "Cannot determine correct auth token format", "blockers": ["Docs are ambiguous about JWT vs API key. Tried both, neither works."], "files_changed": [], "tests_run": false, "artifacts": []}
```

### 11.3 Orchestrator-Side Escalation (Deterministic)

```python
ESCALATION_THRESHOLDS = {
    "max_retries": 2,          # Retry with fresh session after failure
    "max_agent_switches": 2,   # Try a different worker agent
    "escalate_to_queen": True, # Then hand to Queen Bee for reasoning
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
        teardown_agent(agent)

    else:
        # Multiple workers failed — mark issue as 'failed'
        teardown_agent(agent)
        db.update_issue(issue.id, status="failed")
        db.log_event(issue.id, agent.id, "escalated",
                     detail={"reason": "max retries exceeded"})
        # The Queen Bee will see this via `hive logs` or `hive show`
        # and can discuss next steps with the human
```

### 11.4 Queen Bee-Side Escalation (Conversational)

With the Queen Bee as the user-facing interface, escalation is natural conversation rather than structured message passing. When a worker fails after retries:

1. The orchestrator marks the issue as `failed` and logs the event
2. The Queen Bee sees this when checking `hive status` or `hive logs`
3. The Queen Bee tells the human: "Issue w-a3f8 failed after 2 retries. Here's what happened..."
4. The human and Queen Bee discuss next steps:
   - Rephrase the issue → `hive cancel w-a3f8` + `hive create "better title" "clearer description"`
   - Break it down → create multiple smaller issues
   - Give up → `hive cancel w-a3f8`

No `:::HUMAN_QUESTION:::` blocks, no special escalation protocol. The Queen Bee is already in conversation with the human — it just brings up the failure naturally.

---

## 12. Concurrency Model

### SQLite Concurrency

- **WAL mode**: Multiple readers, single writer. At <30 agents, most operations are reads (checking ready queue). Writes are infrequent (claim, status transition, create).
- **Busy timeout**: 5 seconds handles any write contention from the single orchestrator process.
- **Two writers**: The orchestrator and the Queen Bee (via `hive` CLI) both write to the DB. WAL mode handles this cleanly — writes are serialized by SQLite's busy timeout. The Queen Bee's writes are infrequent (creating/closing issues), while the orchestrator writes more often (claiming, status transitions, events).

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
| **Worker hangs (no progress)** | Lease expiry + no progress signals (Section 7.3) | Abort session, route through escalation chain (Section 18)      |
| **Queen Bee session crashes**   | `session.error` SSE event                        | Recreate Queen Bee session, prime with DB state                 |
| **Refinery session crashes**   | `session.error` SSE event                        | Recreate Refinery session; pending merges remain in merge_queue |
| **OpenCode server dies**       | Health check on main loop                        | Enter degraded mode; recovery loop attempts reconnect           |
| **Orchestrator crashes**       | External process monitor (systemd, etc.)         | On restart: reconcile DB state with OpenCode sessions           |
| **LLM rate limit**             | `session.status { type: "retry" }`               | OpenCode auto-retries; orchestrator extends leases and waits    |
| **Worker produces bad work**   | Completion assessment or Refinery test gate      | Escalate to Queen Bee for re-decomposition or re-assignment     |
| **Merge conflict (complex)**   | Refinery reports `needs_human`                   | Escalate to Queen Bee → human                                   |
| **Permission blocks agent**    | Permission unblocker loop (Section 7.5)          | Auto-resolve via policy, or log for human review                |

### Degraded Mode

When OpenCode (or another critical dependency) becomes unreachable, the orchestrator enters **degraded mode** rather than crashing or spinning in error loops.

**Entering degraded mode:**

```python
def enter_degraded_mode(reason: str):
    """Transition the engine to degraded mode."""
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
async def degraded_mode_recovery_loop(degraded: bool):
    """Attempt to recover from degraded mode."""
    backoff = 1.0  # seconds
    while degraded:
        try:
            # No dedicated health endpoint — GET /session returns JSON if alive
            sessions = opencode.list_sessions()
            if sessions is not None:
                # OpenCode is back — reconcile and resume
                reconcile_on_startup()
                degraded = False
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

## 14. Capability Ledger (Model-Based Analytics)

Agents are ephemeral — they're created for a single issue and deleted after merge. The unit of analysis for performance tracking is **model × issue type → outcome**, not agent identity.

The `model` is denormalized onto key events (`worker_started`, `completed`, `incomplete`, `agent_switch`) so the events table is self-contained for all analytics queries. No join to the agents table is needed.

```sql
-- Model performance by issue type
SELECT
    json_extract(e.detail, '$.model') as model,
    i.type,
    COUNT(*) FILTER (WHERE e.event_type = 'completed') as successes,
    COUNT(*) FILTER (WHERE e.event_type IN ('incomplete', 'failed')) as failures
FROM events e
JOIN issues i ON e.issue_id = i.id
WHERE e.event_type IN ('completed', 'incomplete')
  AND json_extract(e.detail, '$.model') IS NOT NULL
GROUP BY model, i.type;
```

```sql
-- Which model is best at tasks tagged 'python'?
SELECT
    json_extract(e.detail, '$.model') as model,
    COUNT(*) as completed,
    AVG(julianday(e.created_at) - julianday(e_claim.created_at)) * 24 as avg_hours
FROM events e
JOIN issues i ON e.issue_id = i.id
LEFT JOIN events e_claim ON e_claim.issue_id = i.id
    AND e_claim.agent_id = e.agent_id
    AND e_claim.event_type = 'claimed'
WHERE e.event_type = 'completed'
  AND i.tags LIKE '%python%'
  AND json_extract(e.detail, '$.model') IS NOT NULL
GROUP BY model
ORDER BY completed DESC, avg_hours ASC;
```

No special infrastructure. The CV is an emergent property of the event log, keyed by model rather than agent identity.

---

> **Implementation results** have moved to [`IMPLEMENTATION_NOTES.md`](IMPLEMENTATION_NOTES.md).

---

## 15. Comparison with Gas Town

| Dimension                 | Gas Town                          | This System                                               |
| ------------------------- | --------------------------------- | --------------------------------------------------------- |
| **Agent runtime**         | Claude Code in tmux               | OpenCode server (HTTP API)                                |
| **Work queue**            | Beads (Dolt + JSONL + Git)        | Single SQLite DB                                          |
| **Scheduling**            | `bd ready` CLI query              | Same SQL query, run by orchestrator                       |
| **Strategic brain**       | Mayor (LLM in tmux)               | Queen Bee (LLM via OpenCode session)                      |
| **Agent monitoring**      | Witness patrol + Deacon heartbeat | Lease-based staleness + SSE events + permission unblocker |
| **Session management**    | tmux sessions, `gt prime`         | OpenCode session lifecycle API                            |
| **Inter-agent comms**     | Mail protocol in beads            | Orchestrator mediates via DB + structured signals         |
| **Crash recovery**        | Witness detects stalled, respawns | Lease expiry + degraded mode + reconciliation             |
| **Merge queue**           | Refinery agent (LLM in tmux)      | `merge_queue` table + Refinery LLM + mechanical fast-path |
| **Multi-project**         | Two-level beads (Town/Rig)        | `project` column + OpenCode `?directory=`                 |
| **Infrastructure agents** | Deacon, Boot, Witness, Dogs       | Orchestrator code (deterministic)                         |
| **Lines of Go/Rust**      | Thousands                         | Zero (Python orchestrator + SQLite)                       |

### What We Gain

- **Simpler infrastructure**: One process, one database, one HTTP API — but with the same strategic capabilities (Queen Bee + Refinery are LLMs, not stripped out)
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

> **Implementation roadmap**: [`IMPL_PLAN.md`](IMPL_PLAN.md) | **Implementation notes, post-mortems, open questions**: [`IMPLEMENTATION_NOTES.md`](IMPLEMENTATION_NOTES.md)

---

## 16. Notes System (Inter-Agent Knowledge Transfer)

### Purpose

Workers are ephemeral — each gets a fresh session with no memory of what previous workers discovered. The notes system bridges this gap by letting workers share knowledge across sessions. A worker writes discoveries, gotchas, and patterns to `.hive-notes.jsonl`, and future workers get those notes injected into their prompts.

This is the 80/20 of Gas Town's mail protocol: **context injection without routing, addressing, or inbox complexity.** No agent-to-agent messaging, no channels, no subscriptions — just a shared knowledge base that the orchestrator reads from and writes to on the workers' behalf.

### Data Flow

```
Worker writes .hive-notes.jsonl     Orchestrator harvests     Future worker gets notes
┌─────────────────────┐         ┌──────────────────────┐    ┌──────────────────────┐
│ {"content": "...",  │ ──────> │ handle_agent_complete │ -> │ _gather_notes_for_   │
│  "category": "..."}│         │   read_notes_file()   │    │   worker(issue_id)   │
│ {"content": "..."}  │         │   db.add_note(...)    │    │ build_worker_prompt(  │
└─────────────────────┘         │   remove_notes_file() │    │   notes=...)          │
                                └──────────────────────┘    └──────────────────────┘
```

### DB Schema

```sql
CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id    TEXT REFERENCES issues(id),
    agent_id    TEXT REFERENCES agents(id),
    category    TEXT NOT NULL DEFAULT 'discovery',
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_notes_issue ON notes(issue_id);
CREATE INDEX IF NOT EXISTS idx_notes_category ON notes(category);
CREATE INDEX IF NOT EXISTS idx_notes_created ON notes(created_at);
```

- `issue_id`: Which issue the note was discovered during. `NULL` = project-wide note (e.g., added by Queen via `hive note`).
- `agent_id`: Which agent wrote it. `NULL` = human-authored or system note.
- `category`: One of `discovery`, `gotcha`, `dependency`, `pattern`, `context`.

### Note Categories

| Category | Meaning | Example |
|---|---|---|
| `discovery` | New finding about the codebase | "The API uses JWT tokens stored in httpOnly cookies" |
| `gotcha` | Pitfall or non-obvious behavior | "Don't use `datetime.now()` — use `datetime.utcnow()` for DB timestamps" |
| `dependency` | External dependency or blocker | "Requires Redis >= 7.0 for stream support" |
| `pattern` | Established pattern to follow | "All API endpoints use the `@require_auth` decorator" |
| `context` | Background information | "This repo was migrated from a monorepo; some imports still use old paths" |

### File Convention

Workers write notes to `.hive-notes.jsonl` in their worktree root. JSONL format — one JSON object per line:

```jsonl
{"content": "The API requires auth tokens in the X-Auth header", "category": "discovery"}
{"content": "Tests fail if Redis is not running locally", "category": "gotcha"}
```

### Harvest Flow

In `handle_agent_complete()`, **before** the canceled/finalized check (so even failed workers' discoveries are saved):

1. `read_notes_file(agent.worktree)` — parse `.hive-notes.jsonl`
2. For each note: `db.add_note(issue_id=..., agent_id=..., content=..., category=...)`
3. `db.log_event(..., "notes_harvested", {"count": N})`
4. `remove_notes_file(agent.worktree)` — always clean up (in `finally` block)

The entire harvest is wrapped in `try/except/finally` — note harvesting is best-effort and must never block agent completion.

### Inject Flow

In `spawn_worker()` and `cycle_agent_to_next_step()`, before `build_worker_prompt()`:

1. `_gather_notes_for_worker(issue_id)` assembles relevant notes:
   - If the issue is a molecule step: `db.get_notes_for_molecule(parent_id)` (sibling notes)
   - Always: `db.get_recent_project_notes(limit=10)` (recent cross-project notes)
   - Deduplicates by note ID (a note from a sibling step might also appear in recent project notes)
   - Returns `None` if no notes found (so `build_worker_prompt` skips the section)
2. `build_worker_prompt(..., notes=worker_notes)` renders the `### Project Notes` section

For `cycle_agent_to_next_step()`, also populates `completed_steps` via `db.get_completed_molecule_steps(parent_id)` — giving the next step context about what siblings have already finished.

### CLI Commands

| Command | Description |
|---|---|
| `hive note "content"` | Add a project-wide note |
| `hive note --issue w-abc --category gotcha "content"` | Add a note tied to an issue |
| `hive notes` | List all notes |
| `hive notes --category gotcha` | Filter by category |
| `hive notes --issue w-abc` | Filter by issue |
| `hive --json notes` | JSON output for programmatic use |

### Comparison with Gas Town Mail

Gas Town's inter-agent communication uses a full mail protocol: channels, addressing, inbox/outbox, delivery confirmation. This is powerful but complex — it requires agents to know about each other's existence and manage their own mailboxes.

The Hive notes system takes a simpler approach:

| Feature | Gas Town Mail | Hive Notes |
|---|---|---|
| Addressing | Agent-to-agent | Broadcast (no addressing) |
| Routing | Channel-based | Category-based filtering |
| Delivery | Push (agent checks inbox) | Pull (orchestrator injects) |
| Lifetime | Message expires | Permanent (DB-backed) |
| Agent awareness | Agents know about each other | Agents are unaware of each other |
| Complexity | High (routing, inbox, ACK) | Low (write file, read DB) |

The notes system is sufficient for the current use case: sharing discoveries and gotchas across sequential workers on related issues. If real-time inter-agent messaging becomes needed (e.g., two workers coordinating on a shared resource), a Gas Town-style channel system can be layered on top without replacing the notes infrastructure.

### Design Decisions

**Why harvest before the canceled check?** A worker that gets canceled externally may still have written useful notes about what it discovered before cancellation. By harvesting first, we capture all knowledge regardless of the issue's terminal state.

**Why a shared `_gather_notes_for_worker` helper?** Both `spawn_worker` and `cycle_agent_to_next_step` need the same logic: molecule notes + project notes, deduped. A single helper prevents divergence and makes the injection logic easy to evolve.

**Why no note TTL/expiration?** Notes could accumulate over time, but the `limit` parameter on queries already prevents unbounded growth in prompt injection. Cleanup can be added later if the table gets large.

**Why no content-based deduplication?** ID-based dedup (when combining molecule + project notes) is sufficient. Content-based dedup adds complexity for minimal gain.
