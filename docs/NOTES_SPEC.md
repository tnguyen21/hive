# Notes Messaging Spec (No Categories)

_Status: proposed_

## 1. Problem

Parallel workers currently miss important in-progress context from other workers. This causes duplicated or conflicting groundwork (for example, one worker changes a schema while another independently re-implements a similar change).

Prompt reminders alone are not deterministic enough to prevent this.

## 2. Goals

1. Keep notes simple: notes are notes (no required taxonomy).
2. Route notes to the right workers/issues instead of global broadcast.
3. Make delivery deterministic at orchestrator turn boundaries.
4. Require acknowledgment only when explicitly marked required.
5. Prevent finalization when required notes were not acknowledged.

## 3. Non-Goals

1. Recreating full Gas Town mail complexity (lists/channels/queues) in v1.
2. Requiring workers to manually run `hive mail` as primary behavior.
3. Adding mandatory note categories.

## 4. Core Principles

1. **Orchestrator-enforced delivery**: notes are injected before turns; workers do not need to remember to check mail.
2. **Addressed routing over categorization**: use issue graph + explicit targets.
3. **Persistent per-recipient state**: queued/delivered/read/acked tracking per worker.
4. **Hard gate for required notes**: no completion while required unread/unacked notes exist.

## 5. Data Model

### 5.1 Notes

`notes` is the canonical note content table.

Required fields:
- `id`
- `project`
- `from_agent_id` (nullable for system/queen)
- `from_issue_id` (nullable)
- `content`
- `must_read` (bool, default `false`)
- `created_at`

Optional addressing fields:
- `to_agent_id` (nullable)
- `to_issue_id` (nullable, repeatable via mapping table if needed)

Dedup fields:
- `content_hash`

### 5.2 Deliveries

Add `note_deliveries` table (one row per recipient agent):

- `id`
- `note_id`
- `recipient_agent_id`
- `recipient_issue_id`
- `status` enum: `queued | delivered | read | acked`
- `delivered_at` (nullable)
- `read_at` (nullable)
- `acked_at` (nullable)
- `created_at`

`acked` is required only when `must_read=true`.

## 6. Routing Rules (Deterministic)

Given a new note `N`, recipients are resolved in this order:

1. Explicit `to_agent_id` (if present).
2. Active worker on explicit `to_issue_id` (if present).
3. Active workers on sibling issues of `from_issue_id` within the same epic.
4. Active workers on dependency neighbors of `from_issue_id`:
   - upstream (`depends_on`)
   - downstream (reverse dependencies)

Rules:
- Recipients are deduplicated by `agent_id`.
- Sender is excluded unless explicitly targeted.
- If no active recipient exists for an explicitly targeted issue, keep pending delivery for the next assignee of that issue.

## 7. Delivery Semantics

### 7.1 Turn-Boundary Injection

Before each orchestrator `send_message_async(...)` to a worker:

1. Query unread routed notes for `(agent_id, issue_id, project)`.
2. Build a compact “Notes Inbox Update” section.
3. Prepend that section to the worker turn payload.
4. Mark included deliveries as `delivered`.

This makes note visibility deterministic per turn without requiring worker polling.

### 7.2 Safe Injection Policy

Inject notes only at:
- task start turn,
- retry/cycle turns,
- orchestrator follow-up turns,
- completion-check follow-up turn (if blocked by required notes).

Do not inject after every tool call.

## 8. Read/Ack Semantics

Workers can mark notes:
- `read` for normal notes,
- `acked` for required notes.

APIs:
- `hive mail read <note_delivery_id>`
- `hive mail ack <note_delivery_id>`
 
Ack is valid only through CLI/API mail commands. Orchestrator must not treat completion payload text as acknowledgment.

## 9. Completion Gate

When worker signals completion:

1. Query required note deliveries for this worker/issue where `status != acked`.
2. If any exist:
   - reject completion transition,
   - send a forced follow-up turn containing pending required notes,
   - require acknowledgment,
   - re-check.
3. Only proceed to `done/finalized` path once required notes are acknowledged.

This gate is the main control preventing “missed critical context” regressions.

## 10. Commands (Minimal Surface)

### 10.1 Send

```bash
hive note send "text..." \
  [--issue <from_issue>] \
  [--to-agent <agent_id>] \
  [--to-issue <issue_id>] \
  [--must-read]
```

`--to-issue` may be repeated to target multiple issues.

### 10.2 Inbox

```bash
hive mail inbox [--agent <agent_id>] [--issue <issue_id>] [--unread]
```

### 10.3 Read / Ack

```bash
hive mail read <delivery_id>
hive mail ack <delivery_id>
```

## 11. Prompt and Agent Contract

This section defines what every execution agent instance must know about inbox notes.

### 11.1 Required Prompt Updates

Apply to worker/refinery/system prompts:

1. Notes may be injected by orchestrator under a fixed section header (see 11.2).
2. Injected notes are authoritative coordination context for the current turn.
3. `must_read` notes must be acknowledged via CLI command before completion.
4. Acknowledgment is command-driven only (`hive mail ack <delivery_id>`), never prose-only.
5. If a note conflicts with current plan, worker should adapt plan and continue (or explicitly report blocker).

Add to completion instructions:

1. Before writing completion signal, ensure required note deliveries are acknowledged.
2. Use `hive mail inbox --unread` and `hive mail ack <delivery_id>` when required note IDs are present.

### 11.2 Canonical Injected Inbox Format

Use a stable text format so models can parse it reliably:

```text
### Notes Inbox Update (3 unread)
- [delivery:d-102][note:n-44][must_read] from agent=agent-7 issue=w-a1b2
  Replace legacy migration path with `db/migrations/v2`.
- [delivery:d-103][note:n-45] from agent=agent-9 issue=w-c3d4
  Reuse shared parser in `src/foo/parser.py`; do not duplicate.

Required actions:
1. Acknowledge required notes via: hive mail ack <delivery_id>
2. Proceed with implementation using the updates above.
```

### 11.3 Scope of Prompt Updates

1. Worker prompts: mandatory.
2. Refinery/finalizer prompts: recommended when merge/finalization logic may be affected by project notes.
3. Queen/operator prompts: optional; useful for authoring targeted notes (`--to-agent`, `--to-issue`, `--must-read`).

## 12. Idempotency and Deduplication

Deduplication is required.

1. **Transport dedupe (required)**:
   - enforce unique delivery row per `(note_id, recipient_agent_id)`.
   - if a delivery exists, update state; do not insert duplicate.
2. **Content dedupe (required)**:
   - compute `content_hash = sha256(normalized_content)` for every note.
   - treat `(project, from_issue_id, content_hash)` as duplicate candidate in a bounded window.

Dedup must run on both note creation and delivery creation.

## 13. Observability

Add events:
- `note_sent`
- `note_routed`
- `note_delivered`
- `note_read`
- `note_acked`
- `completion_blocked_unacked_notes`
- `completion_unblocked_after_ack`

Metrics:
- delivery latency (send -> delivered),
- required-note ack latency,
- completion blocks due to required notes,
- missed-note incidents (manual audit signal).

## 14. Security and Scope

1. Route only within same `project` by default.
2. No cross-project note delivery unless explicit future support is added.
3. Do not expose hidden/system notes unless recipient is authorized.

## 15. Rollout Plan

### 15.1 Phase 1 Scope (smallest effective)

Deliver deterministic inbox + ack gate with minimal surface area changes.

### 15.2 Schema and Migration Plan

Implement in `src/hive/db.py` (`SCHEMA` + `_migrate_if_needed`).

| Change | Type | Notes |
|---|---|---|
| `notes.must_read` | new column | `INTEGER NOT NULL DEFAULT 0` |
| `notes.to_agent_id` | new column | nullable target agent |
| `notes.to_issue_id` | new column | nullable target issue |
| `notes.content_hash` | new column | required for content dedupe |
| `note_deliveries` | new table | per-recipient delivery state |
| `idx_note_deliveries_note` | index | lookup by note |
| `idx_note_deliveries_agent_status` | index | inbox query fast path |
| `uidx_note_deliveries_note_agent` | unique index | transport dedupe for agent recipients |
| `uidx_note_deliveries_note_issue_pending` | unique index (partial) | one pending issue-target row per note/issue |

`note_deliveries` fields:
- `id INTEGER PRIMARY KEY AUTOINCREMENT`
- `note_id INTEGER NOT NULL REFERENCES notes(id)`
- `recipient_agent_id TEXT` (nullable for pending issue-target delivery)
- `recipient_issue_id TEXT` (nullable)
- `status TEXT NOT NULL DEFAULT 'queued'` (`queued|delivered|read|acked`)
- `delivered_at TEXT`
- `read_at TEXT`
- `acked_at TEXT`
- `created_at TEXT NOT NULL DEFAULT (datetime('now'))`

### 15.3 Files and Components to Touch

| File | Phase 1 work |
|---|---|
| `src/hive/db.py` | schema migration + delivery CRUD/query methods + dedupe logic |
| `src/hive/orchestrator.py` | routing/materialization, turn-boundary inbox injection, completion ack gate |
| `src/hive/cli.py` | `hive note send` target flags + `hive mail inbox/read/ack` commands |
| `src/hive/prompts.py` | helper to render deterministic “Notes Inbox Update” block |
| `src/hive/prompts/worker.md` | explicit inbox/ack behavior contract |
| `src/hive/prompts/system.md` | global rule: required notes must be acked via CLI |
| `src/hive/prompts/refinery.md` | optional but recommended inbox awareness |
| `src/hive/prompts/queen.md` | command docs for targeting and must-read notes |
| `docs/TECHNICAL_DESIGN_DOC.md` | reflect protocol and completion gate behavior |

### 15.4 Implementation Steps (Ordered)

1. **DB migration and APIs**
   - add columns/table/indexes.
   - add methods:
     - `create_note(...)` (or extend `add_note`) with `must_read`, `to_agent_id`, `to_issue_id`, `content_hash`
     - `create_note_deliveries(...)`
     - `get_unread_deliveries(agent_id, issue_id, project)`
     - `mark_delivery_read(delivery_id, agent_id)`
     - `mark_delivery_acked(delivery_id, agent_id)`
     - `get_required_unacked_deliveries(agent_id, issue_id)`
     - `materialize_issue_target_deliveries(issue_id, agent_id)`
2. **CLI control plane**
   - implement:
     - `hive note send ... [--to-agent] [--to-issue] [--must-read]`
     - `hive mail inbox [--unread]`
     - `hive mail read <delivery_id>`
     - `hive mail ack <delivery_id>`
   - keep backward compatibility:
     - existing `hive note "text"` remains an alias to `hive note send`.
3. **Orchestrator routing/injection**
   - replace broad live broadcast with routed delivery creation.
   - materialize pending issue-target deliveries on claim/assignment.
   - inject unread notes before each worker turn (per section 7.1 and 11.2).
4. **Completion gate**
   - in completion handler, block if required deliveries for worker/issue are not `acked`.
   - send follow-up turn containing pending required deliveries + explicit ack command.
5. **Prompt updates**
   - update worker/system text to match command and gate semantics.
6. **Observability**
   - emit delivery and gate events from orchestrator and CLI actions.

### 15.5 Test Plan by File

| Test file | Required Phase 1 coverage |
|---|---|
| `tests/test_db.py` | migration adds columns/table/indexes; dedupe; delivery state transitions; pending issue-target rows |
| `tests/test_cli.py` | note send targeting flags; inbox/read/ack command behavior; auth checks on read/ack |
| `tests/test_orchestrator.py` | turn-boundary injection includes routed notes; completion blocked on unacked required notes; unblock after ack |
| `tests/test_prompts.py` | canonical injected inbox format rendering |
| `tests/test_multiworker.py` | parallel workers receive routed notes; required note gate prevents unsafe completion |

### 15.6 Phase 2 Scope

1. Improve targeting ergonomics:
   - repeatable `--to-issue` fanout UX,
   - convenience expansions (epic/dependency selectors).
2. Inbox UX improvements:
   - richer filters (`--required`, `--since`, `--from-issue`),
   - compact vs verbose output modes.
3. Operational visibility:
   - dashboard/metrics for ack latency and blocked completions.

## 16. Why This Over Categories

This design commits to one idea: **routing and protocol state are the value**.

Categories can be added later if they prove useful, but they are not required to:
- deliver notes to the right workers,
- guarantee those workers saw required updates,
- block unsafe completion when they did not.

## 17. Related Future Work

Historical note retrieval (vector embeddings + issue-bootstrap RAG) is intentionally out of scope for this v1 messaging protocol.

See `docs/NOTES_RAG_DESIGN.md` for a separate design and benchmark plan.
