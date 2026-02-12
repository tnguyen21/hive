# Hive Implementation Summary

## What We Built

A lightweight multi-agent orchestration system with:
- **Queen Bee TUI** — primary user interface via OpenCode custom agent
- **Multi-worker pool** for concurrent execution in git worktrees
- **Daemon mode** for background orchestration
- **Event-driven architecture** with SSE
- **Molecule support** for multi-step workflows
- **Permission unblocker** for autonomous operation
- **Merge queue processor** — two-tier done→finalized pipeline (mechanical fast-path + Refinery LLM)

## Architecture

```
Human ←→ Queen Bee TUI (opencode @queen agent)
              ↓ (hive CLI commands)
         SQLite DB ←── Issues, deps, events
              ↓
         Daemon (orchestrator loop)
              ↓
         Worker Sessions (opencode) → git worktrees
              ↓
         Merge Queue → Mechanical rebase/merge OR Refinery LLM → main branch
```

### Queen Bee as TUI

The Queen Bee is the primary interface. `hive queen` attaches to the running OpenCode server with `--dir` scoping so sessions resolve correctly. Inside the TUI, switch to `@queen` to interact with Hive via natural language. The Queen Bee has access to 16+ tools through the `hive` CLI.

### Daemon

`hive daemon start` runs the orchestrator as a background process with PID file management (`~/.hive/pids/`), log files (`~/.hive/logs/`), and signal handling. Use `-f` for foreground mode.

### Merge Queue

When a worker completes an issue (`done`), the orchestrator enqueues it to the merge queue. The merge processor runs as a background task:

1. **Tier 1 — Mechanical**: `git rebase main` → run tests (if `HIVE_TEST_COMMAND` set) → `git merge --ff-only`. No LLM needed.
2. **Tier 2 — Refinery LLM**: On rebase conflict or test failure, a persistent Refinery session resolves conflicts, diagnoses test failures, and either merges, rejects, or escalates.

After successful merge: issue → `finalized`, worktree cleaned up, agent marked idle.

## Stats

| Metric | Count |
|--------|-------|
| Production code | ~4,800 lines across 14 modules |
| Test code | ~3,000 lines |
| Unit tests | 116 passing |
| Integration tests | 14 (require OpenCode server) |

## Key Design Decisions

**Kept from Gas Town:** ready queue, three-layer agent lifecycle, push-based execution, molecules, capability ledger (events table), hash-based IDs, ZFC principle.

**Simplified:** single SQLite instead of distributed sync, OpenCode HTTP API instead of tmux, SQL instead of CRDTs.

**Innovations:** session cycling for molecules, permission unblocker (500ms polling), structured completion signals (`:::COMPLETION:::`), Queen Bee as persistent TUI with tool access, `--dir` scoping for session resolution, two-tier merge processor (mechanical + LLM).

## Phases

### Completed

- **Phase 1** — Single worker loop: SQLite schema, OpenCode client, SSE consumer, git worktrees, worker prompts, structured completion signals, lease-based staleness detection
- **Phase 2** — Queen Bee + multi-worker: Queen Bee TUI with 16 tools, concurrent worker pool, molecule session cycling, permission unblocker, daemon mode, full CLI
- **Phase 3** — Merge queue + Refinery: merge queue processor, mechanical rebase/ff-merge, Refinery LLM for conflicts/test failures, done→finalized pipeline, worktree teardown after finalization, `hive merges` CLI command

### Planned

- **Phase 4** — Resilience: retry logic with MAX_RETRIES enforcement, agent switching on failure, crash recovery, degraded mode, context cycling

## Project Structure

```
hive/
├── .opencode/agents/queen.md   # Queen Bee agent definition
├── src/hive/
│   ├── cli.py                  # CLI interface (20+ subcommands)
│   ├── config.py               # Configuration
│   ├── daemon.py               # Background daemon
│   ├── db.py                   # SQLite database layer
│   ├── git.py                  # Git worktree + merge/rebase ops
│   ├── ids.py                  # Hash-based ID generation
│   ├── merge.py                # Merge queue processor + Refinery
│   ├── models.py               # Data models
│   ├── opencode.py             # OpenCode HTTP client
│   ├── orchestrator.py         # Orchestration engine
│   ├── prompts.py              # Worker + Refinery prompt templates
│   ├── sse.py                  # SSE event consumer
│   └── tools.py                # ToolExecutor (CLI backend)
├── tests/
└── pyproject.toml
```
