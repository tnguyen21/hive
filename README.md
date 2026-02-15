# Hive: Lightweight Multi-Agent Orchestrator

A multi-agent orchestration system that coordinates parallel AI coding agents using SQLite as the work queue. Uses Claude Code CLI with your Pro/Max subscription — no API key needed.

## Quick Start

```bash
# Install
cd your-project && pip install -e path/to/hive

# Set up and run
hive setup                                          # configure project
hive create "Add user auth" "Implement JWT login"   # create an issue
hive start                                          # start the daemon
hive status                                         # check progress
```

The daemon spawns worker agents that implement tasks in isolated git worktrees, then merges results back to main.

## Overview

Hive coordinates multiple AI coding agents working concurrently on a codebase. It handles:

- **Strategic decomposition**: The Queen Bee agent breaks down user requests into concrete work items
- **Parallel execution**: Multiple worker agents execute tasks concurrently in isolated git worktrees
- **Dependency management**: Issues are queued and dispatched based on dependency resolution
- **Multi-step workflows**: Molecules enable sequential workflows where one agent handles multiple related steps
- **Autonomous operation**: Permission unblocker keeps workers running without human intervention
- **Merge pipeline**: Two-tier done→finalized pipeline (mechanical fast-path + Refinery LLM for conflicts)
- **Three-tier model config**: Queen (Opus), Workers (Sonnet), Refinery (Sonnet), with per-issue overrides

## Architecture

```
Human ←→ Queen Bee TUI (interactive Claude CLI session, Opus)
              ↓ (hive CLI commands)
         SQLite DB ←── Issues, deps, events, model config
              ↓
         Daemon (orchestrator loop)
              ↓ pluggable backend:
              ↓   claude (default): WebSocket to Claude CLI processes (subscription billing)
              ↓   opencode: HTTP/SSE to OpenCode server (API billing)
              ↓
         Worker Sessions (Sonnet) → git worktrees
              ↓
         Merge Queue → Mechanical rebase/merge OR Refinery LLM (Sonnet) → main branch
```

### Key Components

| Component | Role |
|-----------|------|
| **Queen Bee** | Interactive Claude CLI session that decomposes user requests into issues via `hive` CLI |
| **Daemon** | Background orchestrator that polls the ready queue, spawns workers, detects completion, processes merges |
| **Workers** | Ephemeral coding agents (Sonnet by default) that implement features, fix bugs, write tests |
| **Refinery** | LLM merge processor (Sonnet) for conflict resolution and test failure diagnosis |
| **SQLite DB** | Single source of truth for issues, dependencies, agents, events |
| **Git Worktrees** | Per-agent sandboxes for isolated development |

## Installation

### Prerequisites

1. **Git 2.20+** — worktrees require a reasonably modern git
2. **[Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)** with an active Pro/Max subscription
3. **Python 3.12+** and [uv](https://docs.astral.sh/uv/) (recommended)
4. **A git repository** to run Hive in

```bash
# Install Claude Code (if you haven't already)
# Native installer (recommended):
curl -fsSL https://claude.ai/install.sh | bash
# Or via npm: npm install -g @anthropic-ai/claude-code
```

### Install Hive

```bash
cd hive

# Install with uv (recommended)
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e ".[dev]"
```

## Usage

### Launch the Queen Bee

The Queen Bee is an interactive session that decomposes your requests into issues, wires dependencies, and monitors progress:

```bash
hive queen
```

### Or manage issues directly

```bash
# Create issues
hive create "Add user authentication" "Implement JWT-based auth" --priority 1
hive create "Write auth tests" --priority 2

# Wire dependencies
hive dep add <test-issue-id> <auth-issue-id>

# Monitor
hive status
hive list
hive logs -f
```

The daemon picks up ready issues automatically and assigns them to workers.

## CLI Reference

Essential commands (run `hive <command> -h` for details on any command):

| Command | Description |
|---------|-------------|
| `hive setup` | Interactive project setup wizard |
| `hive create <title> [desc]` | Create a new issue |
| `hive list [--status S]` | List issues with sorting/filtering |
| `hive show <id>` | Show issue details, deps, and events |
| `hive status` | System overview (issue counts, workers, queues) |
| `hive start` | Start the hive daemon |
| `hive stop` | Stop the hive daemon |
| `hive queen` | Launch Queen Bee interactive session |
| `hive doctor` | Run system health checks |

### Global Options

```
--db PATH          Database path (default: ~/.hive/hive.db)
--project PATH     Project directory (auto-detected from git)
--json             Output JSON (for programmatic use)
```

### Advanced Commands

All commands below are fully functional — they're just not shown in `hive -h` to keep it clean:

`update`, `cancel`, `finalize`, `retry`, `escalate`, `molecule`, `dep`, `ready`, `agents`, `agent`, `events`, `logs`, `merges`, `costs`, `stats`, `metrics`, `daemon`, `watch`, `note`, `notes`, `ui`

## Configuration

```bash
# Backend selection (default: claude)
export HIVE_BACKEND=claude                 # "claude" (default) or "opencode"

# Concurrency
export HIVE_MAX_AGENTS=10                  # Max concurrent workers (default: 10)

# Claude backend
export HIVE_CLAUDE_WS_HOST=127.0.0.1      # WS server bind address (default: 127.0.0.1)
export HIVE_CLAUDE_WS_PORT=8765           # WS server port (default: 8765)
export HIVE_CLAUDE_WS_MAX_CONCURRENT=3    # Max concurrent CLI processes (default: 3)

# Models (three-tier)
export HIVE_DEFAULT_MODEL=claude-opus-4-6              # Queen/system (default)
export HIVE_WORKER_MODEL=claude-sonnet-4-5-20250929    # Workers
export HIVE_REFINERY_MODEL=claude-opus-4-6             # Merge refinery

# Per-issue override (via CLI)
# hive create "title" "desc" --model claude-opus-4-6

# Merge queue
export HIVE_TEST_COMMAND="pytest tests/"   # Test command for merge gate (optional)
export HIVE_MERGE_QUEUE_ENABLED=true       # Enable/disable merge queue
```

Configuration is layered: defaults → `~/.hive/config.toml` → `.hive.toml` → environment variables. Use `hive setup` to create a project config, or set env vars for quick overrides.

### Alternative: OpenCode Backend

For API billing via an OpenCode server instead of subscription credits:

```bash
export HIVE_BACKEND=opencode
export OPENCODE_URL=http://127.0.0.1:4096
export OPENCODE_SERVER_PASSWORD=secret     # if auth enabled

# Start OpenCode server first, then:
hive start
```

## How It Works

### Workflow

1. **User talks to the Queen Bee** (or creates issues directly via CLI)
2. **Queen Bee decomposes requests** into issues with dependencies using `hive` CLI commands
3. **Daemon polls ready queue** for issues with no unresolved dependencies
4. **Worker spawned** for each ready issue:
   - Creates git worktree (`.worktrees/<agent-name>`)
   - Spawns Claude CLI process connected via WebSocket
   - Sends worker prompt with task description
5. **Worker executes autonomously**:
   - Reads code, makes changes, runs tests
   - Commits work to branch (`agent/<agent-name>`)
   - Writes `.hive-result.jsonl` file to worktree root (structured completion data)
6. **Daemon detects completion** (dual strategy):
   - WS event: `session.status → idle` (sub-second)
   - Session polling: fallback (catches missed events)
7. **Daemon assesses and merges**:
   - Reads `.hive-result.jsonl` for structured completion data
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

## Development

### Setup

```bash
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e ".[dev]"
```

### Running Tests

```bash
uv run pytest                  # unit tests
uv run pytest -m integration   # integration tests (requires backend)
```

### Linting & Formatting

```bash
uvx ruff check src/ tests/
uvx ruff format --line-length 144 src/ tests/
```
