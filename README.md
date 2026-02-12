# Hive: Lightweight Multi-Agent Orchestrator

A simplified multi-agent orchestration system using OpenCode server mode as the agent runtime and SQLite as the work queue.

## Overview

Hive coordinates multiple AI coding agents working concurrently on a codebase. It handles:

- **Strategic decomposition**: A Mayor agent breaks down user requests into concrete work items
- **Parallel execution**: Multiple worker agents execute tasks concurrently in isolated git worktrees
- **Dependency management**: Issues are queued and dispatched based on dependency resolution
- **Multi-step workflows**: Molecules enable sequential workflows where one agent handles multiple related steps
- **Autonomous operation**: Permission unblocker keeps workers running without human intervention

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

### Key Components

| Component | Role |
|-----------|------|
| **Mayor** | OpenCode custom agent that decomposes user requests into issues via `hive` CLI |
| **Daemon** | Background orchestrator that polls the ready queue and spawns workers |
| **Workers** | Ephemeral coding agents that implement features, fix bugs, write tests |
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

The daemon polls the ready queue, spawns workers, monitors completion, and handles retries.

### 3. Launch the Mayor

```bash
hive mayor
```

This attaches to the running OpenCode server and opens a TUI. Switch to the `@mayor` agent to interact with Hive through natural language. The Mayor decomposes your requests into issues, wires dependencies, and monitors progress -- all through `hive` CLI commands.

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
| `hive create <title> [desc] [--priority 0-4] [--type task\|bug\|feature]` | Create a new issue |
| `hive list [--status STATUS]` | List issues |
| `hive show <id>` | Show issue details, deps, and events |
| `hive update <id> [--title T] [--description D] [--priority P] [--status S]` | Update an issue |
| `hive cancel <id> [--reason TEXT]` | Cancel an issue |
| `hive finalize <id> [--resolution TEXT]` | Mark issue as done |
| `hive retry <id> [--notes TEXT]` | Reset a failed issue to open |
| `hive escalate <id> --reason TEXT` | Escalate for human attention |

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

### Daemon

| Command | Description |
|---------|-------------|
| `hive daemon start [-f]` | Start daemon (`-f` for foreground) |
| `hive daemon stop` | Stop daemon |
| `hive daemon restart` | Restart daemon |
| `hive daemon status` | Show daemon status |
| `hive daemon logs [-f] [-n N]` | Show daemon logs |

### Mayor

| Command | Description |
|---------|-------------|
| `hive mayor` | Launch Mayor TUI (attaches to OpenCode server) |

## Configuration

```bash
# Concurrency
export HIVE_MAX_AGENTS=3                    # Max concurrent workers (default: 3)

# Timing
export HIVE_POLL_INTERVAL=5                 # Ready queue poll interval in seconds
export HIVE_LEASE_DURATION=300              # Worker lease duration in seconds
export HIVE_PERMISSION_POLL_INTERVAL=0.5    # Permission check interval

# OpenCode
export OPENCODE_URL=http://127.0.0.1:4096  # OpenCode server URL
export OPENCODE_SERVER_PASSWORD=secret      # Server password (if auth enabled)
export OPENCODE_CMD=opencode               # Path to opencode binary

# Database
export HIVE_DB_PATH=hive.db

# Model
export HIVE_DEFAULT_MODEL=claude-sonnet-4-5-20250929
```

## How It Works

### Workflow

1. **User talks to the Mayor** (or creates issues directly via CLI)
2. **Mayor decomposes requests** into issues with dependencies using `hive` CLI commands
3. **Daemon polls ready queue** for issues with no unresolved dependencies
4. **Worker spawned** for each ready issue:
   - Creates git worktree (`.worktrees/<agent-name>`)
   - Creates OpenCode session scoped to worktree
   - Sends worker prompt with task description
5. **Worker executes autonomously**:
   - Reads code, makes changes, runs tests
   - Commits work to branch (`agent/<agent-name>`)
   - Signals completion with `:::COMPLETION:::` block
6. **Daemon assesses completion**:
   - Parses structured completion signal (or uses heuristics)
   - Success: marks issue `done`, enqueues to merge queue
   - Failure: marks `failed`, retries or escalates
7. **Session cycling for molecules**:
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

### Project Structure

```
hive/
├── .opencode/agents/
│   └── mayor.md         # Mayor agent definition (system prompt + permissions)
├── src/hive/
│   ├── cli.py           # CLI interface (all commands route through ToolExecutor)
│   ├── config.py        # Configuration
│   ├── daemon.py        # Background daemon management
│   ├── db.py            # SQLite database layer
│   ├── git.py           # Git worktree management
│   ├── ids.py           # Hash-based ID generation
│   ├── models.py        # Data models
│   ├── opencode.py      # OpenCode HTTP client
│   ├── orchestrator.py  # Main orchestration engine
│   ├── prompts.py       # Worker prompt templates
│   ├── sse.py           # SSE event consumer
│   └── tools.py         # ToolExecutor (shared backend for all CLI commands)
├── tests/               # Test suite (97 unit tests)
├── pyproject.toml
└── README.md
```
