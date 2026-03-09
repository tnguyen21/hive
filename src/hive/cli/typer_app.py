"""Typer-backed CLI for Hive."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Annotated, Literal

import typer
from rich.console import Console

from .runtime import do_setup, initialize_cli, resolve_project
from .rich_views import print_error

app = typer.Typer(
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
    rich_markup_mode="rich",
    no_args_is_help=True,
)
dep_app = typer.Typer(no_args_is_help=True, help="Manage issue dependencies")
app.add_typer(dep_app, name="dep")


@dataclass
class AppState:
    console: Console
    json_mode: bool = False
    project: str | None = None
    db_override: str | None = None


def _fail(state: AppState, exc: Exception) -> None:
    """Render a CLI error for commands that don't go through ``HiveCLI``."""
    if state.json_mode:
        state.console.print_json(json=json.dumps({"error": str(exc)}))
    else:
        print_error(state.console, str(exc))
    raise typer.Exit(1)


@contextmanager
def _cli_session(state: AppState) -> Iterator:
    """Create and clean up a ``HiveCLI`` session for a single command."""
    db, cli, _, _ = initialize_cli(db_override=state.db_override, project=state.project)
    try:
        yield cli
    finally:
        db.close()


def _run_cli_command(
    state: AppState,
    command_name: str,
    *args,
    json_mode: bool | None = None,
    **kwargs,
) -> None:
    """Run a ``HiveCLI`` command through the shared execution pipeline."""
    use_json = state.json_mode if json_mode is None else json_mode
    with _cli_session(state) as cli:
        cli.run_command(command_name, *args, json_mode=use_json, **kwargs)


@app.callback()
def main(
    ctx: typer.Context,
    db: Annotated[str | None, typer.Option("--db", help="Database path (default: ~/.hive/hive.db)")] = None,
    project: Annotated[str | None, typer.Option("--project", help="Project directory (auto-detected from git)")] = None,
    json_mode: Annotated[bool, typer.Option("--json", help="Output JSON (for programmatic use)")] = False,
) -> None:
    """Hive multi-agent orchestrator."""
    ctx.obj = AppState(console=Console(), json_mode=json_mode, project=project, db_override=db)


@app.command()
def setup(ctx: typer.Context) -> None:
    """Create default .hive.toml config."""
    state: AppState = ctx.obj
    try:
        project_path, project_name = resolve_project(state.project)
    except Exception as exc:
        _fail(state, exc)
    do_setup(project_path, project_name, json_mode=state.json_mode)


@app.command()
def init(ctx: typer.Context) -> None:
    """Alias for setup."""
    setup(ctx)


@app.command()
def create(
    ctx: typer.Context,
    title: Annotated[str, typer.Argument(help="Issue title")],
    description: Annotated[str, typer.Argument(help="Issue description")] = "",
    priority: Annotated[int, typer.Option(help="Priority (0-4)")] = 2,
    issue_type: Annotated[str, typer.Option("--type", help="Issue type (task, bug, feature)")] = "task",
    model: Annotated[str | None, typer.Option(help="Model to use for this issue (overrides global WORKER_MODEL)")] = None,
    depends_on: Annotated[
        list[str] | None, typer.Option("--depends-on", help="Issue ID this depends on; repeat the option to add more")
    ] = None,
    tags: Annotated[str | None, typer.Option(help="Comma-separated tags (e.g. refactor,python,small)")] = None,
) -> None:
    """Create a new issue."""
    state: AppState = ctx.obj
    _run_cli_command(
        state,
        "create",
        title,
        description,
        priority,
        issue_type,
        model=model,
        tags=tags,
        depends_on=depends_on,
    )


@app.command("list")
def list_issues(
    ctx: typer.Context,
    status: Annotated[str | None, typer.Option(help="Filter by status")] = None,
    sort_by: Annotated[Literal["priority", "created", "updated", "status", "title"], typer.Option("--sort", help="Sort field")] = "priority",
    reverse: Annotated[bool, typer.Option("--reverse", "-r", help="Reverse sort order")] = False,
    issue_type: Annotated[str | None, typer.Option("--type", help="Filter by issue type (task, bug, feature)")] = None,
    todo: Annotated[bool, typer.Option(help="Show only actionable issues (excludes done/finalized/canceled)")] = False,
    assignee: Annotated[str | None, typer.Option(help="Filter by agent assignee")] = None,
    limit: Annotated[int, typer.Option(help="Max issues to show")] = 50,
) -> None:
    """List all issues."""
    state: AppState = ctx.obj
    _run_cli_command(
        state,
        "list_issues",
        status,
        sort_by=sort_by,
        reverse=reverse,
        issue_type=issue_type,
        assignee=assignee,
        limit=limit,
        todo=todo,
    )


@app.command()
def show(
    ctx: typer.Context,
    issue_id: Annotated[str, typer.Argument(help="Issue ID")],
    show_format: Annotated[Literal["text", "json"], typer.Option("--format", "-f", help="Output format: text (default) or json")] = "text",
) -> None:
    """Show issue details."""
    state: AppState = ctx.obj
    _run_cli_command(
        state,
        "show",
        issue_id,
        json_mode=state.json_mode or show_format == "json",
    )


@app.command()
def review(
    ctx: typer.Context,
    issue_id: Annotated[str | None, typer.Argument(help="Optional issue ID to review a specific issue")] = None,
    limit: Annotated[int, typer.Option(help="Max issues to show")] = 20,
) -> None:
    """Review done issues before finalizing."""
    state: AppState = ctx.obj
    _run_cli_command(state, "review", issue_id=issue_id, limit=limit)


@app.command()
def update(
    ctx: typer.Context,
    issue_id: Annotated[str, typer.Argument(help="Issue ID")],
    title: Annotated[str | None, typer.Option(help="New title")] = None,
    description: Annotated[str | None, typer.Option(help="New description")] = None,
    priority: Annotated[int | None, typer.Option(help="New priority (0-4)")] = None,
    status: Annotated[str | None, typer.Option(help="New status")] = None,
    model: Annotated[str | None, typer.Option(help="New model")] = None,
    tags: Annotated[str | None, typer.Option(help="Comma-separated tags (e.g. refactor,python,small)")] = None,
) -> None:
    """Update an issue."""
    state: AppState = ctx.obj
    _run_cli_command(
        state,
        "update",
        issue_id,
        title=title,
        description=description,
        priority=priority,
        status=status,
        model=model,
        tags=tags,
    )


@app.command()
def cancel(
    ctx: typer.Context,
    issue_id: Annotated[str, typer.Argument(help="Issue ID")],
    reason: Annotated[str, typer.Option(help="Reason for cancellation")] = "",
) -> None:
    """Cancel an issue."""
    state: AppState = ctx.obj
    _run_cli_command(state, "cancel", issue_id, reason=reason)


@app.command()
def finalize(
    ctx: typer.Context,
    issue_id: Annotated[str, typer.Argument(help="Issue ID")],
    resolution: Annotated[str, typer.Option(help="Resolution description")] = "",
) -> None:
    """Finalize a done issue."""
    state: AppState = ctx.obj
    _run_cli_command(state, "finalize", issue_id, resolution=resolution)


@app.command()
def retry(
    ctx: typer.Context,
    issue_id: Annotated[str, typer.Argument(help="Issue ID")],
    notes: Annotated[str, typer.Option(help="Notes about what to try differently")] = "",
    reset: Annotated[bool, typer.Option(help="Reset retry/escalation counters (watermark reset)")] = False,
) -> None:
    """Retry an escalated issue."""
    state: AppState = ctx.obj
    _run_cli_command(state, "retry", issue_id, notes=notes, reset=reset)


@dep_app.command("add")
def dep_add(
    ctx: typer.Context,
    issue_id: Annotated[str, typer.Argument(help="Issue that depends on another")],
    depends_on: Annotated[str, typer.Argument(help="Issue that must be completed first")],
    dep_type: Annotated[str, typer.Option("--type", help="Dependency type (blocks, related)")] = "blocks",
) -> None:
    """Add a dependency."""
    state: AppState = ctx.obj
    _run_cli_command(state, "dep_add", issue_id, depends_on, dep_type=dep_type)


@dep_app.command("remove")
def dep_remove(
    ctx: typer.Context,
    issue_id: Annotated[str, typer.Argument(help="Issue with the dependency")],
    depends_on: Annotated[str, typer.Argument(help="Dependency to remove")],
) -> None:
    """Remove a dependency."""
    state: AppState = ctx.obj
    _run_cli_command(state, "dep_remove", issue_id, depends_on)


@app.command("agents")
def list_agents(
    ctx: typer.Context,
    agent_id: Annotated[str | None, typer.Argument(help="Agent ID (optional - if provided, show agent details)")] = None,
    status: Annotated[str | None, typer.Option(help="Filter by status (idle, working, stalled, failed)")] = None,
) -> None:
    """List agents."""
    state: AppState = ctx.obj
    _run_cli_command(state, "list_agents", agent_id=agent_id, status=status)


@app.command()
def logs(
    ctx: typer.Context,
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Follow new events in real time")] = False,
    lines: Annotated[int, typer.Option("--lines", "-n", help="Number of recent events to show")] = 20,
    issue: Annotated[str | None, typer.Option(help="Filter by issue ID")] = None,
    agent: Annotated[str | None, typer.Option(help="Filter by agent ID")] = None,
    event_type: Annotated[str | None, typer.Option("--type", help="Filter by event type")] = None,
    daemon: Annotated[bool, typer.Option(help="Show daemon logs instead of event logs")] = False,
) -> None:
    """Show event log."""
    state: AppState = ctx.obj
    _run_cli_command(
        state,
        "logs",
        follow=follow,
        n=lines,
        issue_id=issue,
        agent_id=agent,
        event_type=event_type,
        daemon=daemon,
    )


@app.command()
def merges(
    ctx: typer.Context,
    status: Annotated[str | None, typer.Option(help="Filter by status (queued|running|merged|failed)")] = None,
) -> None:
    """Show merge queue."""
    state: AppState = ctx.obj
    _run_cli_command(state, "merges", status=status)


@app.command()
def status(ctx: typer.Context) -> None:
    """Show orchestrator status."""
    state: AppState = ctx.obj
    _run_cli_command(state, "status")


@app.command()
def metrics(
    ctx: typer.Context,
    model: Annotated[str | None, typer.Option(help="Filter by model name")] = None,
    tag: Annotated[str | None, typer.Option(help="Filter by tag")] = None,
    issue_type: Annotated[str | None, typer.Option("--type", help="Filter by issue type")] = None,
    group_by: Annotated[Literal["tag", "type"] | None, typer.Option(help="Group results by tag or type")] = None,
    costs: Annotated[bool, typer.Option("--costs", help="Show token usage and cost estimates")] = False,
    issue: Annotated[str | None, typer.Option(help="Filter costs by specific issue ID (use with --costs)")] = None,
    agent: Annotated[str | None, typer.Option(help="Filter costs by specific agent ID (use with --costs)")] = None,
) -> None:
    """Show metrics and analytics."""
    state: AppState = ctx.obj
    _run_cli_command(
        state,
        "metrics",
        model=model,
        tag=tag,
        issue_type=issue_type,
        group_by=group_by,
        show_costs=costs,
        issue_id=issue,
        agent_id=agent,
    )


@app.command()
def start(
    ctx: typer.Context,
    foreground: Annotated[bool, typer.Option(help="Run in foreground (used by daemon spawner)")] = False,
) -> None:
    """Start the hive daemon."""
    state: AppState = ctx.obj
    _run_cli_command(state, "start", foreground=foreground)


@app.command()
def stop(ctx: typer.Context) -> None:
    """Stop the hive daemon."""
    state: AppState = ctx.obj
    _run_cli_command(state, "stop")


@app.command()
def queen(
    ctx: typer.Context,
    backend: Annotated[Literal["claude", "codex"] | None, typer.Option(help="Override backend (default: from config/HIVE_BACKEND)")] = None,
    dangerously_skip_permissions: Annotated[
        bool, typer.Option(help="Pass --dangerously-skip-permissions to Claude CLI (queen and workers)")
    ] = False,
    mcp_config: Annotated[list[str] | None, typer.Option(help="Claude MCP config(s); repeat the option for multiple configs")] = None,
) -> None:
    """Launch Queen Bee TUI."""
    state: AppState = ctx.obj
    try:
        with _cli_session(state) as cli:
            cli.queen(
                backend=backend,
                skip_permissions=dangerously_skip_permissions,
                mcp_configs=mcp_config,
            )
    except Exception as exc:
        _fail(state, exc)


@app.command("note")
def add_note(
    ctx: typer.Context,
    content: Annotated[str, typer.Argument(help="Note content")],
    issue_id: Annotated[str | None, typer.Option("--issue", help="Associate note with an issue ID")] = None,
    category: Annotated[
        Literal["discovery", "gotcha", "dependency", "pattern", "context"],
        typer.Option(help="Note category"),
    ] = "discovery",
) -> None:
    """Add a knowledge note."""
    state: AppState = ctx.obj
    _run_cli_command(state, "add_note", content, issue_id=issue_id, category=category)


@app.command()
def debug(ctx: typer.Context) -> None:
    """Print diagnostic report for debugging."""
    state: AppState = ctx.obj
    _run_cli_command(state, "debug")


def run(argv: list[str] | None = None) -> None:
    """Run the Typer app with an explicit argv list."""
    app(args=argv, prog_name="hive", standalone_mode=False)
