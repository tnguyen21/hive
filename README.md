# Hive: Multi-Agent Coding Orchestrator

**Alpha software. Expect rough edges.**

Hive coordinates parallel coding workers against a shared issue queue, then helps you review and finalize work.

## When To Use Hive vs Claude Code

| Task                                     | Just use Claude Code | Use Hive |
| ---------------------------------------- | -------------------- | -------- |
| Small bug fix in one area                | Yes                  | -        |
| Single focused feature                   | Yes                  | -        |
| Feature + tests + docs + migration       | -                    | Yes      |
| Refactor across multiple modules         | -                    | Yes      |
| Spec that naturally splits into subtasks | -                    | Yes      |

Rule of thumb: if you'd split the work into separate tasks anyway, Hive is the better fit.

## How It Works

Hive has three moving parts:

**Issues** are your task board, stored in SQLite. Each issue is a unit of work: a bug fix, a feature, a refactor step. You create them, and the system tracks their lifecycle from `open` through `in_progress`, `done`, and `finalized`.

**Workers** are Claude Code sessions that pick up issues and implement them. Each worker gets its own git worktree, a full isolated copy of the repo. Workers run in parallel, up to your configured concurrency limit. When a worker finishes, its branch goes through merge validation (rebase, tests, optional refinery review) before anything touches main.

**The Queen** is how you drive Hive. It's a Claude Code session that acts as your project manager: it reads your spec, explores the codebase, decomposes work into issues, monitors progress, and handles failures. The Queen doesn't write code. It plans and coordinates, and you approve the plan before any issues are created. The Queen also supports a headless mode for non-interactive dispatch (see below).

The typical flow:

1. You describe what you want to the Queen
2. The Queen explores the codebase, proposes a plan, and waits for your approval
3. On approval, the Queen creates issues (with dependencies if needed)
4. The daemon picks up issues and spawns workers in parallel
5. Workers implement, the merge pipeline validates
6. You review completed work and finalize what lands on main

## What It Looks Like

![Hive demo: Queen session and live status](docs/demo.png)

**Left pane**: The Queen session in Claude Code. Here it's exploring the codebase, reading project files, and responding to an escalated issue that a worker couldn't resolve on its own.

**Right pane**: `watch -n 1 hive status` giving a live dashboard — issue counts, active workers, merge queue throughput, and any items that need your attention. In this snapshot, 74 issues have been finalized, 115 branches merged, and one escalation is flagged for the operator.

This is the typical working setup: you interact with the Queen on the left while the right pane gives you a constant read on system state.

## Monitoring with `hive status`

`hive status` is the single most useful command for keeping tabs on a run. It shows:

- **Issue breakdown**: how many are open, in progress, done, finalized, escalated, or cancelled
- **Active workers**: how many are running vs your configured concurrency limit
- **Merge queue**: pending merges, total merged, total failed
- **Needs attention**: escalated issues or failures that require your intervention
- **Daemon state**: whether the daemon is running and where its log lives

Pair it with `watch` for a live dashboard:

```bash
watch -n 1 hive status
```

This lets you monitor progress without interrupting your Queen session. When something needs attention — an escalation, a stuck worker, a merge failure — you'll see it immediately.

## Quick Start

### Install

If you want `hive` available across all repos:

```bash
cd hive
uv tool install -e .
uv tool update-shell   # ensures uv's tool bin dir is on PATH
```

Or install into a project venv:

```bash
cd hive
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e ".[dev]"
```

Notes:

- Use `uv tool install -e ".[dev]"` if you want the dev extras too.
- If you previously ran `uv tool install .`, you may need `--force` to refresh: `uv tool install --force .`

### First Run

```bash
cd your-project

# 1. One-time setup, creates .hive.toml with your project config
hive setup

# 2. Seed project conventions so workers know your norms
hive note "Always run pytest before committing" --category context
hive note "Use ruff for linting, line-length 144" --category context

# 3. Launch the Queen, your main interface
hive queen
```

The Queen auto-starts the daemon if it isn't running. From here, describe what you want built. The Queen will explore the code, propose a decomposition, and wait for your go-ahead before creating issues and dispatching workers.

If you prefer to manage issues manually instead of using the Queen:

```bash
hive create "Add user auth" "Implement JWT login flow"
hive create "Add auth tests" "Unit tests for the JWT flow" --depends-on <auth-issue-id>
hive start       # foreground with live status
hive start -d    # or detached as a background daemon
```

## Reviewing and Finalizing Work

By default, Hive runs in manual review mode. Completed work sits in `done` status until you explicitly finalize it. Nothing merges to main without your approval.

```bash
# See what's ready for review
hive review

# Inspect a branch (review command prints these hints per issue)
git diff main...agent/<agent-id>

# Finalize: merges to main and cleans up the worktree
hive finalize <issue-id> --resolution "reviewed, looks good"
```

If you enable `merge_queue_enabled` in your config, Hive will auto-merge branches that pass rebase + tests. A refinery LLM session handles conflict resolution and integration checks. Issues that the refinery can't resolve cleanly get escalated for your attention.

## Teaching Workers Your Conventions

Notes are persistent context that every worker receives in its prompt. Use them to encode project norms:

```bash
hive note "Run ruff check and pytest before committing"
hive note "All API endpoints need OpenAPI docstrings"
hive note "Use factory pattern for test fixtures" --category pattern
```

Categories are optional. The default is `context`; use `--category pattern`, `--category gotcha`, or `--category dependency` for more specific classification.

Workers also write notes during execution (discoveries, gotchas, dependency observations) which get relayed to sibling workers on the same epic. This is how parallel workers stay loosely coordinated without sharing a context window.

## Headless Queen Mode

The Queen can run non-interactively with `--headless`. Instead of proposing a plan and waiting for approval, headless mode creates issues directly from your prompt and exits.

```bash
# Dispatch a task without interaction (run from inside the project directory)
hive queen --headless -p "Bump all Python dependencies and update the lockfile"

# Target a specific project from anywhere (useful for cron, scripts, PM agents)
hive --project /path/to/myrepo queen --headless -p "Add rate limiting to all API endpoints"

# Combine with backend override
hive --project ~/projects/myapp queen --headless -p "Fix flaky test suite" --backend codex
```

Headless mode:
- Skips the plan-approval step — issues are created directly
- Uses `--dangerously-skip-permissions` (no human to approve tool calls)
- Reads `.hive/project-context.md` and `.hive/queen-context.md` for project knowledge
- Updates `.hive/queen-context.md` with any new learnings
- Prints a summary of created issues before exiting

This is useful for scripting, cron jobs, or piping tasks from external systems. The `--prompt` / `-p` flag is required when `--headless` is set. Use the global `--project` flag to target a specific project directory when running from outside it (e.g. `hive --project /path/to/repo queen --headless -p "..."`). Without `--project`, hive auto-detects the project from the current directory's git root.

## Cost and Performance

Hive spawns multiple Claude Code sessions. Costs scale linearly with concurrency and task complexity.

For your first run, set `max_agents = 3` to keep concurrency low. Validate that the review loop works for your project, then scale up once you're comfortable.

Hive tracks token usage per issue and per run. Use `hive metrics --costs` for rough USD estimates, and configure `max_tokens_per_issue` / `max_tokens_per_run` as guardrails.

## Essential Commands

| Command                    | What it does                                  |
| -------------------------- | --------------------------------------------- |
| `hive queen`               | Launch the Queen (main interface)              |
| `hive queen --headless -p` | Non-interactive dispatch (see below)           |
| `hive setup`               | One-time project config                       |
| `hive create`              | Create an issue manually                      |
| `hive start` / `hive stop` | Start/stop the daemon                         |
| `hive status`              | System overview: issues, workers, merge queue |
| `hive list`                | List issues (filter with `--status`)          |
| `hive show <id>`           | Issue details, deps, recent events            |
| `hive review`              | Review completed work before finalizing       |
| `hive finalize <id>`       | Finalize and merge to main                    |
| `hive retry <id>`          | Retry a failed issue                          |
| `hive note`                | Add persistent context for workers            |
| `hive doctor`              | Health checks (use `--fix` to auto-repair)    |
| `hive debug`               | Full diagnostic dump for bug reports          |

See `hive -h` for the full list, including `logs`, `agents`, `merges`, `metrics`, `epic`, and `dep`.

## Configuration

Config is layered (later overrides earlier):

1. Built-in defaults
2. `~/.hive/config.toml` (global)
3. `.hive.toml` (per-project)
4. Environment variables

Key settings:

| Setting                     | Default                      | What it controls                         |
| --------------------------- | ---------------------------- | ---------------------------------------- |
| `HIVE_BACKEND`              | `claude`                     | Backend: `claude` or `codex`             |
| `HIVE_MAX_AGENTS`           | `10`                         | Max concurrent workers                   |
| `HIVE_TEST_COMMAND`         | —                            | Test command run during merge validation |
| `HIVE_MERGE_QUEUE_ENABLED`  | `true`                       | Auto-merge vs manual review mode         |
| `HIVE_WORKER_MODEL`         | `claude-sonnet-4-5-20250929` | Model for workers                        |
| `HIVE_REFINERY_MODEL`       | `claude-opus-4-6`            | Model for merge refinery                 |
| `HIVE_MAX_TOKENS_PER_ISSUE` | `200000`                     | Per-issue token budget                   |
| `HIVE_MAX_TOKENS_PER_RUN`   | `2000000`                    | Per-run token budget                     |

### Codex backend sandbox/approval flags

Hive can run workers via the Codex CLI app-server backend.

- Enable: `HIVE_BACKEND=codex`
- Sandbox mode: `HIVE_CODEX_SANDBOX` (`read-only`, `workspace-write`, `danger-full-access`)
- Approval policy: `HIVE_CODEX_APPROVAL_POLICY` (`untrusted`, `on-request`, `never`)
- App-server command override: `HIVE_CODEX_CMD` (default: `codex app-server --listen stdio://`)

Notes:

- `workspace-write` is the default and is usually sufficient; Hive adds write access to the parent repo `.git/` so `git commit` works inside git worktrees.
- `danger-full-access` removes path restrictions (use with care).
- If you truly want **no** approvals + **no** sandboxing, you can start app-server as: `codex --dangerously-bypass-approvals-and-sandbox app-server --listen stdio://` (extremely risky; only do this in an externally sandboxed environment).

## Architecture and Internals

For orchestration design, schema details, merge pipeline internals, and backend abstraction, see `docs/TECHNICAL_DESIGN_DOC.md`.

## Bug Reports and Feedback

Please send me bug reports, feature requests, and other cool ideas!

Run `hive debug` and include the output in your report. It collects system info, config, daemon state, doctor checks, and recent logs into a single pasteable bundle:

```bash
hive debug          # human-readable
hive debug --json   # machine-readable
```

## Development

```bash
# tests
uv run pytest

# lint + format
uvx ruff check src/ tests/
uvx ruff format --line-length 144 src/ tests/

# mutation testing (requires dev deps: uv sync --dev)
./scripts/mutate.sh              # full run across all target modules
./scripts/mutate.sh status.py    # single module
./scripts/mutate.sh db/core.py   # subpackage module
./scripts/mutate.sh --results    # show surviving mutants
uv run mutmut show <mutant-id>   # inspect a specific mutant
```

Target modules are configured in `pyproject.toml` under `[tool.mutmut]`.
