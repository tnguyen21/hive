# Session Context

## User Prompts

### Prompt 1

Implement the following plan:

# Plan: Global Multi-Project Status View

## Context

`hive status` currently only works inside a project (git repo). Since start/stop
now work globally via `initialize_global()`, we should extend `status` to show a
multi-project dashboard when run outside a project. The daemon is global and
serves all registered projects — users need visibility into all of them from
anywhere.

## Approach

The `status` command in `typer_app.py` tries `resolve_project()`. If it ...

### Prompt 2

Path not found: /Users/tau/projects/kairos                                              │
╰─────────────────────────────────────────────────────────────────────────────────────────╯
╭────────────────────────────────── worker-002862c551fb ──────────────────────────────────╮
│ Path not found: /Users/tau/projects/takeoff-protocol/.worktrees/worker-002862c551fb     │
╰─────────────────────────────────────────────────────────────────────────────────────────╯
╭────────────────────────────────── wor...

### Prompt 3

so there are lingering projects that i'd like to rm from hive (no longer maintained so i removed the dir, or the dir name was changed)╭──────────────────────────────────────── kairos ─────────────────────────────────────────╮
│ Path not found: /Users/tau/projects/kairos                                              │
╰─────────────────────────────────────────────────────────────────────────────────────────╯
╭──────────────────────────────────────── labrat ──────────────────────────────────────...

### Prompt 4

can we have the layout be a little more amenable to fitting all information on a single screen

I prefer using watch to refresh status and see all the projects at once

### Prompt 5

fatal: not a git repository (searched up from /Users/tau/projects)

where is this from?

