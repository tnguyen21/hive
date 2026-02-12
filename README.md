# Hive: Lightweight Multi-Agent Orchestrator

A simplified multi-agent orchestration system inspired by Gas Town, using OpenCode server mode as the agent runtime and SQLite as the work queue.

## Overview

Hive coordinates multiple AI coding agents working concurrently on a codebase. It handles:

- **Strategic decomposition**: A Mayor agent breaks down user requests into concrete work items
- **Parallel execution**: Multiple worker agents execute tasks concurrently in isolated git worktrees
- **Dependency management**: Issues are queued and dispatched based on dependency resolution
- **Multi-step workflows**: Molecules enable sequential workflows where one agent handles multiple related steps
- **Autonomous operation**: Permission unblocker keeps workers running without human intervention

## Architecture

```
Human (CLI)
    ↓
Mayor (LLM) ────→ Decomposes into Issues → SQLite DB
    ↓
Orchestrator ───→ Queries Ready Queue
    ↓
Worker Sessions ─→ Execute Issues (in git worktrees)
    ↓
Merge Queue ─────→ Refinery (future) or Manual Merge
    ↓
Finalized (main branch updated)
```

### Key Components

| Component | Role |
|-----------|------|
| **Mayor** | Strategic brain that decomposes user requests into concrete issues |
| **Orchestrator** | Schedules work, manages agent lifecycles, handles health checks |
| **Workers** | Ephemeral coding agents that implement features, fix bugs, write tests |
| **SQLite DB** | Single source of truth for issues, dependencies, agents, events |
| **OpenCode Server** | Headless agent runtime that executes prompts and streams events |
| **Git Worktrees** | Per-agent sandboxes for isolated development |

## Installation

### Prerequisites

1. **Python 3.11+**
2. **OpenCode server** running at `http://127.0.0.1:4096`
3. **Git repository** for your project

### Install Hive

```bash
# Clone or navigate to the hive directory
cd hive

# Install with uv (recommended) or pip
uv venv
source .venv/bin/activate  # or: .venv/Scripts/activate on Windows
uv pip install -e ".[dev]"

# Or with pip
pip install -e ".[dev]"
```

### Start OpenCode Server

In a separate terminal:

```bash
# Navigate to your opencode installation
cd /path/to/opencode

# Start the server
bun run --cwd packages/opencode --conditions=browser src/index.ts serve --port 4096

# Or with password authentication
OPENCODE_SERVER_PASSWORD=your-secret \
  bun run --cwd packages/opencode --conditions=browser src/index.ts serve --port 4096
```

## Quick Start

### 1. Initialize the Database

The database is created automatically on first use:

```bash
# Check status (creates hive.db if it doesn't exist)
hive status
```

### 2. Create an Issue Manually

```bash
# Create a simple task
hive create "Add README documentation" "Create a comprehensive README.md file" --priority 1

# Create multiple issues
hive create "Write unit tests" "Add tests for authentication module" --priority 2
hive create "Fix login bug" "Users can't login with email addresses" --priority 0
```

### 3. View Issues

```bash
# List all issues
hive list

# Filter by status
hive list --status open
hive list --status done

# Show ready queue (unblocked, unassigned issues)
hive ready

# Show issue details
hive show w-abc123
```

### 4. Start the Orchestrator

```bash
# Start orchestrator in current project directory
hive start

# Or specify project path
hive start --project /path/to/your/project

# Use custom database
hive start --db /path/to/custom.db
```

The orchestrator will:
1. Create a Mayor session for strategic planning
2. Poll the ready queue for work
3. Spawn worker agents (up to MAX_AGENTS)
4. Monitor completion via SSE events
5. Handle failures and retries

### 5. Monitor Status

While the orchestrator is running (in another terminal):

```bash
# Show current status
hive status

# Output:
# === Hive Status ===
# Project: my-project
# Issues:
#   open: 5
#   in_progress: 2
#   done: 3
# Active workers: 2/3
#   - worker-a3f8b1: Add README documentation
#   - worker-c7e2d9: Write unit tests
# Ready queue: 3 issues
# Merge queue: 1 pending
```

## CLI Reference

### Global Options

```bash
--db PATH          # Database path (default: hive.db)
--project PATH     # Project directory (default: .)
```

### Commands

#### `create` - Create a new issue

```bash
hive create "Title" ["Description"] [--priority 0-4]

# Examples:
hive create "Implement auth" "Add JWT authentication" --priority 1
hive create "Quick fix"  # Minimal (priority defaults to 2)
```

#### `list` - List all issues

```bash
hive list [--status STATUS]

# Examples:
hive list                    # All issues
hive list --status open      # Only open issues
hive list --status in_progress
```

#### `ready` - Show ready queue

```bash
hive ready

# Shows unblocked, unassigned issues ordered by priority
```

#### `show` - Show issue details

```bash
hive show ISSUE_ID

# Shows:
# - Issue metadata (title, status, priority, type, assignee)
# - Description
# - Dependencies
# - Event history
```

#### `close` - Mark issue as canceled

```bash
hive close ISSUE_ID

# Sets status to 'canceled'
```

#### `status` - Show orchestrator status

```bash
hive status

# Shows:
# - Issue counts by status
# - Active workers and their current tasks
# - Ready queue size
# - Merge queue size
```

#### `start` - Start orchestrator

```bash
hive start [--project PATH] [--db PATH]

# Starts the orchestrator main loop
# Press Ctrl+C to stop
```

## Configuration

Set environment variables to configure Hive:

```bash
# Concurrency
export HIVE_MAX_AGENTS=3              # Maximum concurrent workers (default: 3)

# Timing
export HIVE_POLL_INTERVAL=5           # Ready queue poll interval in seconds (default: 5)
export HIVE_LEASE_DURATION=300        # Worker lease duration in seconds (default: 300)
export HIVE_PERMISSION_POLL_INTERVAL=0.5  # Permission check interval (default: 0.5)

# OpenCode
export OPENCODE_URL=http://127.0.0.1:4096  # OpenCode server URL
export OPENCODE_SERVER_PASSWORD=secret      # Server password (if auth enabled)

# Database
export HIVE_DB_PATH=hive.db           # Database file path (default: hive.db)

# Context cycling thresholds (token counts)
export HIVE_MAYOR_TOKEN_THRESHOLD=120000     # Mayor context cycling (default: 120k)
export HIVE_WORKER_TOKEN_THRESHOLD=150000    # Worker context cycling (default: 150k)

# Model
export HIVE_DEFAULT_MODEL=claude-sonnet-4-5-20250929  # Default model
```

## How It Works

### Workflow

1. **User creates issues** via CLI or Mayor decomposes a request
2. **Orchestrator polls ready queue** (issues with no unresolved dependencies)
3. **Worker spawned** for each ready issue:
   - Creates git worktree (`<project>/.worktrees/<agent-name>`)
   - Creates OpenCode session scoped to worktree
   - Sends worker prompt with task description
4. **Worker executes autonomously**:
   - Reads code, makes changes, runs tests
   - Commits work to branch (`agent/<agent-name>`)
   - Signals completion with `:::COMPLETION:::` block
5. **Orchestrator assesses completion**:
   - Parses structured completion signal (or uses heuristics)
   - If successful: marks issue `done`, enqueues to merge queue
   - If failed: marks `failed`, retries or escalates
6. **Session cycling for molecules**:
   - After completing a step, checks for next ready step in molecule
   - Aborts old session, creates new session (same worktree)
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
| `read`, `edit`, `write`, `bash` | ✅ Allow | Standard tool usage |
| `question`, `plan_enter` | ❌ Deny | No interactive prompts |
| `external_directory` | ❌ Deny | Sandbox enforcement |
| Unknown permissions | ⏸️ Block | Human review required |

The permission unblocker polls every 500ms to auto-resolve permissions and keep workers running.

### Lease-Based Staleness

Workers have a lease duration (default: 5 minutes). If no progress is detected:
1. Orchestrator aborts the session
2. Issue is unassigned and returned to ready queue
3. Another worker can pick it up

## Advanced Features

### Molecules (Multi-Step Workflows)

Create complex workflows with dependencies:

```python
# Via Mayor (natural language)
# User: "Implement authentication system with design, implementation, and tests"

# Mayor creates:
# - Issue 1: "Design auth architecture" (priority 1)
# - Issue 2: "Implement JWT middleware" (depends on Issue 1)
# - Issue 3: "Write auth tests" (depends on Issue 2)
```

Workers auto-advance through steps in the same worktree.

### Mayor Integration

The Mayor is a persistent LLM session that:
- Receives user requests in natural language
- Decomposes them into concrete work items
- Creates issues with dependencies
- Handles escalations from failed workers

(Direct Mayor integration via CLI is planned for future phases)

## Development

### Running Tests

```bash
# Run all tests
pytest

# Run specific test file
pytest tests/test_orchestrator.py

# Run with verbose output
pytest -v

# Run integration tests (requires OpenCode server)
pytest -m integration
```

### Test Coverage

- **78 unit tests** across 11 test files
- Database operations, git worktrees, prompts, orchestrator, CLI
- Mock-based unit tests + integration tests with live OpenCode server

### Project Structure

```
hive/
├── src/hive/
│   ├── __init__.py
│   ├── cli.py           # Human CLI interface
│   ├── config.py        # Configuration
│   ├── db.py            # SQLite database layer
│   ├── git.py           # Git worktree management
│   ├── ids.py           # Hash-based ID generation
│   ├── models.py        # Data models
│   ├── opencode.py      # OpenCode HTTP client
│   ├── orchestrator.py  # Main orchestration engine
│   ├── prompts.py       # Prompt templates
│   └── sse.py           # SSE event consumer
├── tests/               # Test suite
├── pyproject.toml       # Project metadata
└── README.md            # This file
```

## Examples

### Example 1: Simple Task

```bash
# Create a task
hive create "Add logging to API" "Add structured logging to all API endpoints"

# Start orchestrator
hive start

# Monitor (in another terminal)
hive status
```

### Example 2: Multiple Independent Tasks

```bash
# Create several tasks
hive create "Update dependencies" --priority 2
hive create "Fix type errors" --priority 1
hive create "Add docstrings" --priority 3

# Start orchestrator (will run up to MAX_AGENTS concurrently)
hive start
```

### Example 3: Tasks with Dependencies

```bash
# Create parent task
DESIGN=$(hive create "Design database schema" --priority 1)

# Create dependent task
hive create "Implement data models" --priority 2

# Wire dependency manually via Python:
# from hive.db import Database
# db = Database("hive.db")
# db.connect()
# db.add_dependency(impl_id, design_id)
```

## Troubleshooting

### OpenCode server not responding

```bash
# Check if server is running
curl http://127.0.0.1:4096/session

# Restart server
# (See Installation section)
```

### Workers getting stuck

Check permission logs:

```bash
hive show <issue-id>
# Look for permission_resolved events
```

Increase lease duration:

```bash
export HIVE_LEASE_DURATION=600  # 10 minutes
```

### Database locked errors

SQLite uses WAL mode for concurrency, but:
- Don't run multiple orchestrators on same database
- Check for orphaned connections

```bash
# Reset if needed
rm hive.db hive.db-wal hive.db-shm
hive status  # Recreates database
```

## Limitations (Current Implementation)

- ✅ Phase 1-2 complete (core functionality)
- ⏳ Phase 3 pending: Refinery for automated merge processing
- ⏳ Phase 4 pending: Escalation chain and crash recovery
- Manual merge queue processing (no automatic rebase/conflict resolution yet)
- No distributed operation (single SQLite database)
- No web UI (CLI only)

## Roadmap

See the [Technical Design Document](../CLAUDE_TECHNICAL_DESIGN_DOC.md) for full planned features.

**Completed:**
- ✅ Database foundation with ready queue
- ✅ OpenCode HTTP client and SSE consumer
- ✅ Single worker loop with lease-based staleness
- ✅ Mayor session for strategic decomposition
- ✅ Multi-worker pool with session cycling
- ✅ Permission unblocker and human CLI

**Planned:**
- ⏳ Merge queue processor (Refinery agent)
- ⏳ Escalation chain (retry → agent switch → Mayor → human)
- ⏳ Crash recovery and degraded mode
- ⏳ Context cycling for long-running sessions
- ⏳ Human question surfacing

## License

This project is part of the multi-agent orchestration research. See parent directory for license information.

## Contributing

This is a research implementation. For production use, consider:
- Adding authentication
- Implementing proper error recovery
- Adding metrics and monitoring
- Creating a web UI
- Distributed database support

## Credits

Inspired by [Gas Town](../gastown) and [Beads](../beads).

Built with [OpenCode](../opencode) and Claude 4.5 Sonnet.
