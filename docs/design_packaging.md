# Design: Hive as a Packaged CLI Tool

## Problem

Using Hive today requires:
1. Being in the hive project directory (or having the right Python path)
2. Manually starting an OpenCode server
3. Knowing the right incantation to connect everything

We want `hive orchestrate` to work from any git repo, with zero manual setup.

## Target UX

```bash
# One-time install
uv tool install hive

# From any git repo
cd ~/projects/my-app
hive orchestrate          # auto-detects project, starts OpenCode if needed, runs
hive status               # works anywhere — queries ~/.hive/hive.db
hive create "Fix the auth bug"
```

## Architecture

```
hive (globally installed entry point)
  ├── project detection (git root, .hive.toml)
  ├── config resolution (~/.hive/config.toml + .hive.toml)
  ├── OpenCode lifecycle (start/stop/health-check)
  ├── DB connection (~/.hive/hive.db)
  └── orchestrator / CLI commands
```

## Components

### Package structure

Hive is already a proper Python package (`src/hive/`). The main changes:
- Add `[project.scripts]` entry point in `pyproject.toml`: `hive = "hive.cli:main"`
- Ensure all deps are declared (currently: sqlite3 is stdlib, httpx, etc.)
- `uv tool install .` from the repo, or publish to PyPI for `uv tool install hive`

### Config resolution

Two-tier config:

**`~/.hive/config.toml`** (global defaults)
```toml
[opencode]
binary = "opencode"        # or absolute path
host = "localhost"
port = 4873

[defaults]
model = "sonnet"
max_agents = 3
db_path = "~/.hive/hive.db"
```

**`.hive.toml`** (per-project, committed to repo)
```toml
[project]
name = "my-app"            # overrides git-repo-name inference
model = "opus"             # default model for this project

[orchestrator]
max_agents = 5
worker_timeout = 1800
```

Resolution order: CLI flags > env vars > `.hive.toml` > `~/.hive/config.toml` > built-in defaults.

### Project auto-detection

When `hive` runs without an explicit `--project`:
1. Walk up from cwd to find `.git/`
2. Check for `.hive.toml` at git root — use `project.name` if present
3. Fall back to git remote name or directory name
4. All DB queries scoped to this project name (already the case)

### OpenCode lifecycle management

The orchestrator needs a running OpenCode server. Rather than requiring the user to start it separately:

**`hive server start`** — starts OpenCode in the background
- Checks if already running (health check on configured port)
- Starts `opencode` process, waits for health endpoint
- Writes PID to `~/.hive/opencode.pid`
- Logs to `~/.hive/opencode.log`

**`hive server stop`** — graceful shutdown via PID file

**`hive server status`** — reports running/stopped, PID, uptime

**Auto-start in orchestrator:**
```python
async def ensure_opencode_running(self):
    """Start OpenCode if not already running."""
    if await self._health_check():
        return
    logger.info("Starting OpenCode server...")
    self._start_opencode()
    await self._wait_for_healthy(timeout=30)
```

This runs at the start of `orchestrate()`. If OpenCode is already running (e.g. user started it manually, or from a previous `hive orchestrate`), it's a no-op.

**Open question:** Should we stop OpenCode when the orchestrator exits? Probably not by default — another `hive orchestrate` in a different project might want it. But `hive server stop` gives explicit control.

### Directory structure

```
~/.hive/
  ├── config.toml          # global config
  ├── hive.db              # shared database (see design_global_db.md)
  ├── opencode.pid         # PID file for managed OpenCode process
  ├── opencode.log         # OpenCode stdout/stderr
  └── worktrees/           # git worktrees for agents (per-project subdirs)
      ├── my-app/
      │   ├── w-abc123/
      │   └── w-def456/
      └── other-project/
```

## Implementation sequence

1. **Add entry point** — `[project.scripts]` in pyproject.toml, verify `uv tool install .` works
2. **Config loading** — implement two-tier config resolution, replace hardcoded values
3. **Project auto-detection** — git root discovery, project name inference
4. **Global DB** — move DB path to `~/.hive/hive.db` (see design_global_db.md)
5. **OpenCode lifecycle** — `hive server` subcommands, auto-start in orchestrator
6. **Polish** — `hive init` to create `.hive.toml` in a repo, `hive doctor` to check environment

## Open questions

- Should `hive` manage multiple OpenCode instances (one per project) or share one? Sharing is simpler but limits parallelism across projects.
- Do we want `hive` to also wrap `opencode` config (model keys, etc.), or keep that as the user's responsibility?
- Worktree location: under `~/.hive/worktrees/` (centralized) or under the project's `.git/` (current approach)? Centralized is cleaner for global install but breaks locality.
