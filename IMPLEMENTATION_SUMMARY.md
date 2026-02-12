# Hive Implementation Summary

## What We Built

A lightweight multi-agent orchestration system with:
- **Mayor TUI** — primary user interface via OpenCode custom agent
- **Multi-worker pool** for concurrent execution in git worktrees
- **Daemon mode** for background orchestration
- **Event-driven architecture** with SSE
- **Molecule support** for multi-step workflows
- **Permission unblocker** for autonomous operation

## Architecture

```
Human ←→ Mayor TUI (opencode @mayor agent)
              ↓ (hive CLI commands)
         SQLite DB ←── Issues, deps, events
              ↓
         Daemon (orchestrator loop)
              ↓
         Worker Sessions (opencode) → git worktrees
              ↓
         Merge Queue → main branch
```

### Mayor as TUI

The Mayor is the primary interface. `hive mayor` attaches to the running OpenCode server with `--dir` scoping so sessions resolve correctly. Inside the TUI, switch to `@mayor` to interact with Hive via natural language. The Mayor has access to 16 tools through the `hive` CLI.

### Daemon

`hive daemon start` runs the orchestrator as a background process with PID file management (`~/.hive/pids/`), log files (`~/.hive/logs/`), and signal handling. Use `-f` for foreground mode.

## Stats

| Metric | Count |
|--------|-------|
| Production code | 3,800 lines across 13 modules |
| Test code | 2,200 lines |
| Unit tests | 97 passing |
| Integration tests | 14 (require OpenCode server) |

## Key Design Decisions

**Kept from Gas Town:** ready queue, three-layer agent lifecycle, push-based execution, molecules, capability ledger (events table), hash-based IDs, ZFC principle.

**Simplified:** single SQLite instead of distributed sync, OpenCode HTTP API instead of tmux, SQL instead of CRDTs.

**Innovations:** session cycling for molecules, permission unblocker (500ms polling), structured completion signals (`:::COMPLETION:::`), Mayor as persistent TUI with tool access, `--dir` scoping for session resolution.

## Phases

### Completed

- **Phase 1** — Single worker loop: SQLite schema, OpenCode client, SSE consumer, git worktrees, worker prompts, structured completion signals, lease-based staleness detection
- **Phase 2** — Mayor + multi-worker: Mayor TUI with 16 tools, concurrent worker pool, molecule session cycling, permission unblocker, daemon mode, full CLI

### Planned

- **Phase 3** — Refinery: merge queue processor, mechanical rebase, LLM conflict resolution, test verification gate
- **Phase 4** — Resilience: retry logic, agent switching on failure, crash recovery, degraded mode

## Project Structure

```
hive/
├── .opencode/agents/mayor.md   # Mayor agent definition
├── src/hive/
│   ├── cli.py                  # CLI interface
│   ├── config.py               # Configuration
│   ├── daemon.py               # Background daemon
│   ├── db.py                   # SQLite database layer
│   ├── git.py                  # Git worktree management
│   ├── ids.py                  # Hash-based ID generation
│   ├── models.py               # Data models
│   ├── opencode.py             # OpenCode HTTP client
│   ├── orchestrator.py         # Orchestration engine
│   ├── prompts.py              # Worker prompt templates
│   ├── sse.py                  # SSE event consumer
│   └── tools.py                # ToolExecutor (CLI backend)
├── tests/
└── pyproject.toml
```
