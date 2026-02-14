# Hive: Lightweight Multi-Agent Orchestrator

A simplified multi-agent orchestration system using OpenCode server mode as the agent runtime and SQLite as the work queue.

## Overview

Hive coordinates multiple AI coding agents working concurrently on a codebase. It handles:

- **Strategic decomposition**: The Queen Bee agent breaks down user requests into concrete work items
- **Parallel execution**: Multiple worker agents execute tasks concurrently in isolated git worktrees
- **Dependency management**: Issues are queued and dispatched based on dependency resolution
- **Multi-step workflows**: Molecules enable sequential workflows where one agent handles multiple related steps
- **Autonomous operation**: Permission unblocker keeps workers running without human intervention
- **Merge pipeline**: Two-tier done→finalized pipeline (mechanical fast-path + Refinery LLM for conflicts)
- **Triple completion detection**: SSE events + file-based `.hive-result.jsonl` + session polling fallback
- **Three-tier model config**: Queen (Opus), Workers (Sonnet), Refinery (Sonnet), with per-issue overrides

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

### Key Components

| Component | Role |
|-----------|------|
| **Queen Bee** | OpenCode custom agent that decomposes user requests into issues via `hive` CLI |
| **Daemon** | Background orchestrator that polls the ready queue, spawns workers, detects completion, processes merges |
| **Workers** | Ephemeral coding agents (Sonnet by default) that implement features, fix bugs, write tests |
| **Refinery** | LLM merge processor (Sonnet) for conflict resolution and test failure diagnosis |
| **SQLite DB** | Single source of truth for issues, dependencies, agents, events |
| **OpenCode Server** | Headless agent runtime that executes prompts and streams events |
| **Git Worktrees** | Per-agent sandboxes for isolated development |

## Installation

### Prerequisites

1. **Python 3.12+**
2. **[OpenCode](https://github.com/nicholasgriffintn/opencode)** installed and available in PATH
3. **Git repository** for your project

### Install Hive

```bash
cd hive

# Install with uv (recommended)
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e ".[dev]"
```

## Quick Start

### 1. Start the OpenCode server

```bash
opencode serve --port 4096

# Or with password authentication
OPENCODE_SERVER_PASSWORD=your-secret opencode serve --port 4096
```

### 2. Start the Hive daemon

In a separate terminal, from your project directory:

```bash
# Foreground (see logs directly)
hive daemon start -f

# Or as a background daemon
hive daemon start
```

The daemon polls the ready queue, spawns workers, monitors completion via triple detection (SSE + file-based + polling), processes the merge queue, and cleans up sessions on completion/cancellation/shutdown.

### 3. Launch the Queen Bee

```bash
hive queen
```

This attaches to the running OpenCode server and opens a TUI. Switch to the `@queen` agent to interact with Hive through natural language. The Queen Bee decomposes your requests into issues, wires dependencies, and monitors progress -- all through `hive` CLI commands.

### 4. Or manage issues directly

```bash
# Create issues
hive create "Add user authentication" "Implement JWT-based auth" --priority 1
hive create "Write auth tests" --priority 2

# Wire dependencies
hive dep add <test-issue-id> <auth-issue-id>

# Monitor
hive status
hive list
hive ready
hive logs -f
```

The daemon picks up ready issues automatically and assigns them to workers.

## CLI Reference

### Global Options

```
--db PATH          Database path (default: hive.db)
--project PATH     Project directory (default: .)
--json             Output JSON (for programmatic use)
```

### Issue Management

| Command | Description |
|---------|-------------|
| `hive create <title> [desc] [--priority 0-4] [--type task\|bug\|feature] [--model MODEL]` | Create a new issue |
| `hive list [--status S] [--sort FIELD] [-r] [--type T] [--assignee A] [--limit N]` | List issues with sorting/filtering |
| `hive show <id>` | Show issue details, deps, and events |
| `hive update <id> [--title T] [--description D] [--priority P] [--status S] [--model M]` | Update an issue |
| `hive cancel <id> [--reason TEXT]` | Cancel an issue |
| `hive finalize <id> [--resolution TEXT]` | Mark issue as done |
| `hive retry <id> [--notes TEXT]` | Reset a failed issue to open |
| `hive escalate <id> --reason TEXT` | Escalate for human attention |

Sort fields for `hive list --sort`: `priority` (default), `created`, `updated`, `status`, `title`.

### Workflows

| Command | Description |
|---------|-------------|
| `hive molecule <title> --steps '<JSON>' [--description D]` | Create multi-step workflow |
| `hive dep add <id> <depends_on> [--type blocks\|related]` | Add dependency |
| `hive dep remove <id> <depends_on>` | Remove dependency |

Steps JSON format for molecules:
```json
[
  {"title": "Step 1", "description": "..."},
  {"title": "Step 2", "needs": [0]}
]
```

### Monitoring

| Command | Description |
|---------|-------------|
| `hive status` | System overview (issue counts, workers, queues) |
| `hive ready` | Show ready queue (unblocked, unassigned) |
| `hive agents [--status S]` | List agents |
| `hive agent <id>` | Show agent details |
| `hive events [--issue ID] [--agent ID] [--type T] [--limit N]` | Query event log |
| `hive logs [-f] [-n N] [--issue ID] [--agent ID]` | Tail event log |
| `hive merges` | Show merge queue status |

All commands support `--json` for machine-readable output.

### Daemon

| Command | Description |
|---------|-------------|
| `hive daemon start [-f]` | Start daemon (`-f` for foreground) |
| `hive daemon stop` | Stop daemon |
| `hive daemon restart` | Restart daemon |
| `hive daemon status` | Show daemon status |
| `hive daemon logs [-f] [-n N]` | Show daemon logs |

### Queen Bee

| Command | Description |
|---------|-------------|
| `hive queen` | Launch Queen Bee TUI (attaches to OpenCode server) |

## Configuration

```bash
# Concurrency
export HIVE_MAX_AGENTS=10                   # Max concurrent workers (default: 10)

# Timing
export HIVE_POLL_INTERVAL=5                 # Ready queue poll interval in seconds
export HIVE_LEASE_DURATION=900              # Worker lease duration in seconds (default: 15min)
export HIVE_LEASE_EXTENSION=600             # Lease extension on activity (default: 10min)
export HIVE_PERMISSION_POLL_INTERVAL=0.5    # Permission check interval

# OpenCode
export OPENCODE_URL=http://127.0.0.1:4096  # OpenCode server URL
export OPENCODE_SERVER_PASSWORD=secret      # Server password (if auth enabled)

# Database
export HIVE_DB_PATH=hive.db

# Models (three-tier)
export HIVE_DEFAULT_MODEL=claude-opus-4-6              # Queen/system (default)
export HIVE_WORKER_MODEL=claude-sonnet-4-20250514      # Workers (cheaper for coding tasks)
export HIVE_REFINERY_MODEL=claude-sonnet-4-20250514    # Merge refinery

# Per-issue override (via CLI)
# hive create "title" "desc" --model claude-opus-4-6   # Use Opus for this specific issue

# Merge queue
export HIVE_TEST_COMMAND="pytest tests/"    # Test command for merge gate (optional)
export HIVE_MERGE_QUEUE_ENABLED=true        # Enable/disable merge queue
export HIVE_MERGE_POLL_INTERVAL=10          # Merge queue poll interval in seconds
```

## How It Works

### Workflow

1. **User talks to the Queen Bee** (or creates issues directly via CLI)
2. **Queen Bee decomposes requests** into issues with dependencies using `hive` CLI commands
3. **Daemon polls ready queue** for issues with no unresolved dependencies
4. **Worker spawned** for each ready issue:
   - Creates git worktree (`.worktrees/<agent-name>`)
   - Creates OpenCode session scoped to worktree
   - Sends worker prompt with task description
5. **Worker executes autonomously**:
   - Reads code, makes changes, runs tests
   - Commits work to branch (`agent/<agent-name>`)
   - Writes `.hive-result.jsonl` file to worktree root (the sole completion signal)
6. **Daemon detects completion** (triple strategy):
   - SSE event: `session.status → idle` (sub-second)
   - File-based: polls for `.hive-result.jsonl` in worktree (deterministic)
   - Session polling: calls `get_session_status()` as fallback
7. **Daemon assesses and merges**:
   - Reads `.hive-result.jsonl` for worker and refinery results
   - Success: marks issue `done`, enqueues to merge queue
   - Merge queue: mechanical rebase → test → ff-merge, or Refinery LLM for conflicts
   - On merge success: issue → `finalized`, worktree cleaned up, session killed
   - Failure: marks `failed`, retries or escalates
8. **Session cycling for molecules**:
   - After completing a step, checks for next ready step
   - Auto-advances through sequential workflow

### Issue States

```
open → in_progress → done → finalized
                      ↓
                    failed (retryable)

Special states:
- blocked: Waiting on dependencies
- escalated: Human intervention needed
- canceled: Abandoned
```

### Permission Policy

Workers run with restricted permissions:

| Permission | Policy | Reason |
|------------|--------|--------|
| `read`, `edit`, `write`, `bash` | Allow | Standard tool usage |
| `question`, `plan_enter` | Deny | No interactive prompts |
| `external_directory` | Deny | Sandbox enforcement |

## Development

### Setup

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e ".[dev]"
```

### Running Tests

```bash
# Unit tests (integration tests auto-skipped without server)
uv run pytest

# Integration tests (requires running OpenCode server)
uv run pytest -m integration
```

### Linting & Formatting

```bash
uvx ruff check src/ tests/
uvx ruff format --line-length 144 src/ tests/
```

### Exploring the Database

Use [Datasette](https://datasette.io/) to browse and query the Hive SQLite database in your browser:

```bash
datasette ~/.hive/hive.db
```

This opens a web UI at `http://127.0.0.1:8001` where you can inspect issues, agents, events, dependencies, and the merge queue. Datasette is included in the dev dependencies.

### Project Structure

```
hive/
├── .opencode/agents/
│   └── queen.md              # Queen Bee agent definition (system prompt + permissions)
├── src/hive/
│   ├── cli.py                # CLI interface (20+ commands, --json support, sort/filter)
│   ├── config.py             # Configuration (3 model tiers + env vars)
│   ├── daemon.py             # Background daemon with PID/log management
│   ├── db.py                 # SQLite database layer (6 tables + model column)
│   ├── git.py                # Git worktree + merge/rebase operations
│   ├── ids.py                # Hash-based ID generation
│   ├── merge.py              # Merge queue processor + Refinery LLM
│   ├── models.py             # Data models (AgentIdentity, CompletionResult)
│   ├── opencode.py           # OpenCode HTTP client (sessions, messages, SSE)
│   ├── orchestrator.py       # Orchestration engine (triple detection, session cleanup)
│   ├── prompts.py            # Prompt loader + completion assessment logic
│   ├── prompts/
│   │   ├── worker.md         # Worker behavioral contract + completion signals
│   │   ├── system.md         # System prompt template
│   │   └── refinery.md       # Merge refinery prompt template
│   ├── sse.py                # SSE event consumer + dispatch
│   └── tools.py              # ToolExecutor (CLI backend, sort/filter)
├── tests/                    # 250 unit tests + 13 integration tests
├── pyproject.toml
└── README.md
```
