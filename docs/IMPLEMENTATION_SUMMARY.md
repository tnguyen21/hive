# Hive Implementation Summary

## What We Built

A lightweight multi-agent orchestration system with:
- **Queen Bee TUI** — primary user interface via OpenCode custom agent
- **Multi-worker pool** for concurrent execution in git worktrees
- **Daemon mode** for background orchestration
- **Event-driven architecture** with SSE + file-based signaling + session polling
- **Molecule support** for multi-step workflows
- **Permission unblocker** for autonomous operation
- **Merge queue processor** — two-tier done→finalized pipeline (mechanical fast-path + Refinery LLM)
- **Three-tier model config** — Queen (Opus), Workers (Sonnet), Refinery (Sonnet), with per-issue overrides
- **Prompt templates** — `.md` files for easy hand-editing of agent behavioral contracts

## Architecture

```
Human ←→ Queen Bee TUI (opencode @queen agent, Opus)
              ↓ (hive CLI commands)
         SQLite DB ←── Issues, deps, events, model config
              ↓
         Daemon (orchestrator loop)
              ↓ triple detection: SSE + .hive-result.jsonl + polling
         Worker Sessions (opencode, Sonnet) → git worktrees
              ↓
         Merge Queue → Mechanical rebase/merge OR Refinery LLM (Sonnet) → main branch
```

### Queen Bee as TUI

The Queen Bee is the primary interface. `hive queen` attaches to the running OpenCode server with `--dir` scoping so sessions resolve correctly. Inside the TUI, switch to `@queen` to interact with Hive via natural language. The Queen Bee has access to 16+ tools through the `hive` CLI.

### Daemon

`hive daemon start` runs the orchestrator as a background process with PID file management (`~/.hive/pids/`), log files (`~/.hive/logs/`), and signal handling. Use `-f` for foreground mode. On startup, the daemon reconciles stale agents and aborts orphaned opencode sessions from previous runs.

### Completion Detection (Triple Strategy)

Workers are monitored via three independent mechanisms:
1. **SSE events** — `session.status → idle` for sub-second detection
2. **File-based** — Workers write `.hive-result.jsonl` to worktree root (deterministic, filesystem-based)
3. **Session polling** — Direct `get_session_status()` calls as fallback for missed SSE events

### Merge Queue

When a worker completes an issue (`done`), the orchestrator enqueues it to the merge queue. The merge processor runs as a background task:

1. **Tier 1 — Mechanical**: `git rebase main` → run tests (if `HIVE_TEST_COMMAND` set) → `git merge --ff-only`. No LLM needed.
2. **Tier 2 — Refinery LLM**: On rebase conflict or test failure, a persistent Refinery session resolves conflicts, diagnoses test failures, and either merges, rejects, or escalates.

After successful merge: issue → `finalized`, worktree cleaned up, session killed, agent marked idle.

### Session Cleanup

OpenCode sessions are aggressively killed (abort + delete) on: agent completion, issue cancellation, stall detection, daemon shutdown, and daemon restart reconciliation. This prevents the critical token-waste bug where sessions linger after their work is done.

## Stats

| Metric | Count |
|--------|-------|
| Production code | ~5,500 lines across 14 modules + 3 prompt templates |
| Test code | ~3,000 lines |
| Unit tests | 128 passing |
| Integration tests | 14 (require OpenCode server) |

## Key Design Decisions

**Kept from Gas Town:** ready queue, three-layer agent lifecycle, push-based execution, molecules, capability ledger (events table), hash-based IDs, ZFC principle.

**Simplified:** single SQLite instead of distributed sync, OpenCode HTTP API instead of tmux, SQL instead of CRDTs.

**Innovations:** session cycling for molecules, permission unblocker (500ms polling), triple completion detection (SSE + file-based + polling), structured completion signals (`:::COMPLETION:::`), Queen Bee as persistent TUI with tool access, `--dir` scoping for session resolution, two-tier merge processor (mechanical + LLM), prompt templates as `.md` files, three-tier model configuration with per-issue overrides, aggressive session cleanup on all exit paths.

## Model Configuration

Three independent model knobs:
- `HIVE_DEFAULT_MODEL` (default: `claude-opus-4-6`) — Queen/Mayor sessions
- `HIVE_WORKER_MODEL` (default: `claude-sonnet-4-20250514`) — Worker sessions
- `HIVE_REFINERY_MODEL` (default: `claude-sonnet-4-20250514`) — Merge refinery

Per-issue override: `hive create "title" "desc" --model claude-opus-4-6` stores in `issues.model` column. Resolution order: `issue.model > WORKER_MODEL > DEFAULT_MODEL`.

## Phases

### Completed

- **Phase 1** — Single worker loop: SQLite schema, OpenCode client, SSE consumer, git worktrees, worker prompts, structured completion signals, lease-based staleness detection
- **Phase 2** — Queen Bee + multi-worker: Queen Bee TUI with 16 tools, concurrent worker pool, molecule session cycling, permission unblocker, daemon mode, full CLI
- **Phase 3** — Merge queue + Refinery: merge queue processor, mechanical rebase/ff-merge, Refinery LLM for conflicts/test failures, done→finalized pipeline, worktree teardown after finalization, `hive merges` CLI command
- **Phase 4 (partial)** — Resilience: session cleanup on cancel/shutdown, stale agent reconciliation, triple completion detection, anti-stall worker prompt, per-issue model config, prompt templates, CLI enhancements (`--json`, sort/filter flags)

### Planned

- **Phase 4 (remainder)** — Resilience: retry logic with MAX_RETRIES enforcement, agent switching on failure, degraded mode, context cycling
- **Phase 5** — Operational maturity: capability-based routing, cost tracking, structured logging, web dashboard

## Project Structure

```
hive/
├── .opencode/agents/queen.md      # Queen Bee agent definition
├── src/hive/
│   ├── cli.py                     # CLI interface (20+ subcommands, --json support)
│   ├── config.py                  # Configuration (3 model tiers + env vars)
│   ├── daemon.py                  # Background daemon with PID/log management
│   ├── db.py                      # SQLite database layer (6 tables + model column)
│   ├── git.py                     # Git worktree + merge/rebase ops
│   ├── ids.py                     # Hash-based ID generation
│   ├── merge.py                   # Merge queue processor + Refinery
│   ├── models.py                  # Data models (AgentIdentity, CompletionResult)
│   ├── opencode.py                # OpenCode HTTP client (sessions, messages, SSE)
│   ├── orchestrator.py            # Orchestration engine (triple detection, cleanup)
│   ├── prompts.py                 # Prompt loader + completion assessment logic
│   ├── prompts/
│   │   ├── worker.md              # Worker behavioral contract + completion signals
│   │   ├── system.md              # System prompt template
│   │   └── refinery.md            # Merge refinery prompt template
│   ├── sse.py                     # SSE event consumer + dispatch
│   └── tools.py                   # ToolExecutor (CLI backend, sort/filter)
├── tests/                         # 128 unit tests + 14 integration tests
└── pyproject.toml
```
