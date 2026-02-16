# Hive: Multi-Agent Coding Orchestrator

Hive coordinates parallel coding workers against a shared issue queue, then helps you review and finalize work.

## Quick Start (5 minutes)

```bash
# Install
cd hive
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e ".[dev]"

# In your project repo
hive setup
hive create "Add user auth" "Implement JWT login flow"
hive start            # live dashboard by default
```

If you prefer background mode:

```bash
hive start -d
```

## When To Use Hive vs Claude Code

| Task | Just use Claude Code | Use Hive |
|---|---|---|
| Small bug fix in one area | Yes | - |
| Single focused feature | Yes | - |
| Feature + tests + docs + migration | - | Yes |
| Refactor across multiple modules | - | Yes |
| Spec that naturally splits into subtasks | - | Yes |

Rule of thumb: if you would split the work anyway, Hive is usually the better fit.

## Three Core Concepts

| Concept | What it means |
|---|---|
| Queen | Your project manager session (`hive queen`) |
| Workers | Parallel implementers running in isolated worktrees |
| Issues | The task board stored in SQLite |

## Setup Defaults (Safety First)

`hive setup` now guides you through:

- test command for merge/review validation
- auto-merge setting (`merge_queue_enabled`), defaulting to manual review mode
- optional project context note seeding (test command, lint command, repo conventions)

Manual review mode keeps completed issues in `done` until you explicitly finalize.

## Core Workflow

```bash
# create work
hive create "Title" "Description"

# run orchestrator
hive start          # foreground live dashboard
hive start -d       # detached background daemon

# inspect progress
hive status
hive review         # done but not finalized, with diff/merge/finalize hints
hive finalize <issue-id> --resolution "manual review complete"

# optional Queen workflow
hive queen          # auto-starts daemon if needed
```

## Notes Are First-Class Context

Use notes to encode project conventions for all future workers:

```bash
hive note "Run ruff check and pytest before committing" --category context
hive notes
```

Workers automatically receive relevant project notes in their prompt context.

## Cost Visibility

Before startup, Hive shows a short cost preview based on your configured worker concurrency.

Guideline for first runs:

- start with lower concurrency (for example, 3 workers)
- validate your workflow and review loop
- then scale up

## Essential Commands

- `hive setup` - interactive setup wizard
- `hive create` - create an issue
- `hive list` - list issues
- `hive show` - show issue details
- `hive status` - system overview
- `hive review` - review done issues before finalize
- `hive start` - start daemon + live dashboard
- `hive stop` - stop daemon
- `hive queen` - launch Queen session
- `hive doctor` - health checks

Monitoring commands are also visible in `hive -h`: `logs`, `watch`, `events`, `agents`, `merges`.

## Configuration

Configuration resolution order:

1. built-in defaults
2. `~/.hive/config.toml`
3. `.hive.toml`
4. environment variables

Key settings:

- `HIVE_BACKEND` (`claude` or `opencode`)
- `HIVE_MAX_AGENTS`
- `HIVE_TEST_COMMAND`
- `HIVE_MERGE_QUEUE_ENABLED`
- model settings (`HIVE_DEFAULT_MODEL`, `HIVE_WORKER_MODEL`, `HIVE_REFINERY_MODEL`)

Global DB default remains `~/.hive/hive.db` unless overridden.

## Architecture And Internals

For deep internals, orchestration design, schema, and implementation details, see:

- `docs/TECHNICAL_DESIGN_DOC.md`

## Development

```bash
# tests
uv run pytest

# lint + format
uvx ruff check src/ tests/
uvx ruff format --line-length 144 src/ tests/
```
