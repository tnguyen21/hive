"""Typer-backed CLI for Hive."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Annotated, Literal, NoReturn

import click
import typer
from rich.console import Console

from .runtime import do_setup, initialize_cli, initialize_global, resolve_project
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


def _fail(state: AppState, exc: Exception) -> NoReturn:
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


def _run(
    ctx: typer.Context,
    command_name: str,
    *args,
    json_mode: bool | None = None,
    **kwargs,
) -> None:
    """Run a CLI command using the ``AppState`` stored in the Typer context."""
    _run_cli_command(ctx.obj, command_name, *args, json_mode=json_mode, **kwargs)


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
    try:
        project_path, project_name = resolve_project(ctx.obj.project)
    except Exception as exc:
        _fail(ctx.obj, exc)
    do_setup(project_path, project_name, json_mode=ctx.obj.json_mode)


@app.command()
def init(
    ctx: typer.Context,
    analyze: Annotated[bool, typer.Option("--analyze", help="Run LLM-powered project analysis to generate .hive/project-context.md")] = False,
) -> None:
    """Initialize project for Hive. Use --analyze to generate project context via LLM."""
    try:
        project_path, project_name = resolve_project(ctx.obj.project)
    except Exception as exc:
        _fail(ctx.obj, exc)
    do_setup(project_path, project_name, json_mode=ctx.obj.json_mode)
    if analyze:
        from .runtime import do_analyze

        do_analyze(project_path, project_name, json_mode=ctx.obj.json_mode)


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
    _run(
        ctx,
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
    _run(
        ctx,
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
    _run(
        ctx,
        "show",
        issue_id,
        json_mode=ctx.obj.json_mode or show_format == "json",
    )


@app.command()
def review(
    ctx: typer.Context,
    issue_id: Annotated[str | None, typer.Argument(help="Optional issue ID to review a specific issue")] = None,
    limit: Annotated[int, typer.Option(help="Max issues to show")] = 20,
) -> None:
    """Review done issues before finalizing."""
    _run(ctx, "review", issue_id=issue_id, limit=limit)


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
    _run(
        ctx,
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
    _run(ctx, "cancel", issue_id, reason=reason)


@app.command()
def finalize(
    ctx: typer.Context,
    issue_id: Annotated[str, typer.Argument(help="Issue ID")],
    resolution: Annotated[str, typer.Option(help="Resolution description")] = "",
) -> None:
    """Finalize a done issue."""
    _run(ctx, "finalize", issue_id, resolution=resolution)


@app.command()
def retry(
    ctx: typer.Context,
    issue_id: Annotated[str, typer.Argument(help="Issue ID")],
    notes: Annotated[str, typer.Option(help="Notes about what to try differently")] = "",
    reset: Annotated[bool, typer.Option(help="Reset retry/escalation counters (watermark reset)")] = False,
) -> None:
    """Retry an escalated issue."""
    _run(ctx, "retry", issue_id, notes=notes, reset=reset)


@dep_app.command("add")
def dep_add(
    ctx: typer.Context,
    issue_id: Annotated[str, typer.Argument(help="Issue that depends on another")],
    depends_on: Annotated[str, typer.Argument(help="Issue that must be completed first")],
    dep_type: Annotated[str, typer.Option("--type", help="Dependency type (blocks, related)")] = "blocks",
) -> None:
    """Add a dependency."""
    _run(ctx, "dep_add", issue_id, depends_on, dep_type=dep_type)


@dep_app.command("remove")
def dep_remove(
    ctx: typer.Context,
    issue_id: Annotated[str, typer.Argument(help="Issue with the dependency")],
    depends_on: Annotated[str, typer.Argument(help="Dependency to remove")],
) -> None:
    """Remove a dependency."""
    _run(ctx, "dep_remove", issue_id, depends_on)


@app.command("agents")
def list_agents(
    ctx: typer.Context,
    agent_id: Annotated[str | None, typer.Argument(help="Agent ID (optional - if provided, show agent details)")] = None,
    status: Annotated[str | None, typer.Option(help="Filter by status (idle, working, stalled, failed)")] = None,
) -> None:
    """List agents."""
    _run(ctx, "list_agents", agent_id=agent_id, status=status)


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
    _run(
        ctx,
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
    _run(ctx, "merges", status=status)


def _has_project_context(project: str | None) -> bool:
    """Check if we're inside a git repo without printing errors."""
    from pathlib import Path

    if project:
        return Path(project).resolve().joinpath(".git").exists()
    current = Path.cwd().resolve()
    while True:
        if (current / ".git").exists():
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


@app.command()
def status(ctx: typer.Context) -> None:
    """Show orchestrator status."""
    if _has_project_context(ctx.obj.project):
        _run(ctx, "status")
        return

    # No project context — show global multi-project view
    db = initialize_global(db_override=ctx.obj.db_override)
    try:
        from .global_status import get_global_status

        result = get_global_status(db)
        if ctx.obj.json_mode:
            ctx.obj.console.print_json(json.dumps(result))
        else:
            from .rich_views import render_global_status

            ctx.obj.console.print(render_global_status(result))
    finally:
        db.close()


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
    _run(
        ctx,
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
    from ..daemon import HiveDaemon

    db = initialize_global(db_override=ctx.obj.db_override)
    try:
        if foreground:
            from ..daemon import run_daemon_foreground

            run_daemon_foreground(db)
            return

        daemon = HiveDaemon(db_path=db.db_path)
        status = daemon.status()
        if status["running"]:
            result = {"status": "already_running", "pid": status["pid"]}
        else:
            started = daemon.start()
            if started:
                ds = daemon.status()
                result = {"status": "started", "pid": ds["pid"], "log_file": ds.get("log_file")}
            else:
                log_tail = ""
                try:
                    if daemon.log_file.exists():
                        lines = daemon.log_file.read_text().strip().splitlines()
                        log_tail = "\n".join(lines[-10:])
                except OSError:
                    pass
                _fail(ctx.obj, RuntimeError(f"Failed to start daemon. Log: {daemon.log_file}\n{log_tail}".rstrip()))

        if ctx.obj.json_mode:
            ctx.obj.console.print_json(json.dumps(result))
        else:
            from .rich_views import render_start

            rendered = render_start(result)
            if rendered:
                ctx.obj.console.print(rendered)
    finally:
        db.close()


@app.command()
def stop(ctx: typer.Context) -> None:
    """Stop the hive daemon."""
    from ..daemon import HiveDaemon

    db = initialize_global(db_override=ctx.obj.db_override)
    try:
        daemon = HiveDaemon(db_path=db.db_path)
        status = daemon.status()
        if not status["running"]:
            result = {"status": "not_running"}
        else:
            pid = status["pid"]
            stopped = daemon.stop()
            if stopped:
                result = {"status": "stopped", "pid": pid}
            else:
                _fail(ctx.obj, RuntimeError(f"Failed to stop daemon (PID {pid})"))

        if ctx.obj.json_mode:
            ctx.obj.console.print_json(json.dumps(result))
        else:
            from .rich_views import render_stop

            rendered = render_stop(result)
            if rendered:
                ctx.obj.console.print(rendered)
    finally:
        db.close()


@app.command()
def queen(
    ctx: typer.Context,
    backend: Annotated[Literal["claude", "codex"] | None, typer.Option(help="Override backend (default: from config/HIVE_BACKEND)")] = None,
    dangerously_skip_permissions: Annotated[
        bool, typer.Option(help="Pass --dangerously-skip-permissions to Claude CLI (queen and workers)")
    ] = False,
    mcp_config: Annotated[list[str] | None, typer.Option(help="Claude MCP config(s); repeat the option for multiple configs")] = None,
    headless: Annotated[bool, typer.Option(help="Run non-interactively (requires --prompt)")] = False,
    prompt: Annotated[str | None, typer.Option("-p", "--prompt", help="Task prompt for headless mode")] = None,
) -> None:
    """Launch Queen Bee TUI."""
    if headless and not prompt:
        print_error(ctx.obj.console, "--headless requires --prompt / -p")
        raise typer.Exit(1)
    try:
        with _cli_session(ctx.obj) as cli:
            cli.queen(
                backend=backend,
                skip_permissions=dangerously_skip_permissions,
                mcp_configs=mcp_config,
                headless=headless,
                prompt=prompt,
            )
    except Exception as exc:
        _fail(ctx.obj, exc)


@app.command()
def forget(
    ctx: typer.Context,
    project_name: Annotated[str | None, typer.Argument(help="Project name to unregister")] = None,
    stale: Annotated[bool, typer.Option("--stale", help="Remove all projects whose paths no longer exist")] = False,
) -> None:
    """Unregister a project (or all stale projects with --stale)."""
    from pathlib import Path

    if not project_name and not stale:
        print_error(ctx.obj.console, "Provide a project name or use --stale")
        raise typer.Exit(1)

    db = initialize_global(db_override=ctx.obj.db_override)
    try:
        removed = []
        if stale:
            for proj in db.list_projects():
                if not Path(proj["path"]).exists():
                    db.unregister_project(proj["name"])
                    removed.append(proj["name"])
        if project_name:
            if db.unregister_project(project_name):
                removed.append(project_name)
            else:
                _fail(ctx.obj, ValueError(f"Project not found: {project_name}"))

        if ctx.obj.json_mode:
            ctx.obj.console.print_json(json.dumps({"removed": removed}))
        else:
            if removed:
                for name in removed:
                    ctx.obj.console.print(f"Removed [cyan]{name}[/cyan]")
            else:
                ctx.obj.console.print("Nothing to remove.", style="dim")
    finally:
        db.close()


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
    _run(ctx, "add_note", content, issue_id=issue_id, category=category)


@app.command()
def debug(ctx: typer.Context) -> None:
    """Print diagnostic report for debugging."""
    _run(ctx, "debug")


def run(argv: list[str] | None = None) -> None:
    """Run the Typer app with an explicit argv list."""
    try:
        app(args=argv, prog_name="hive", standalone_mode=False)
    except click.exceptions.NoArgsIsHelpError:
        # Click/Typer prints help for no-arg invocations before raising this
        # sentinel when standalone_mode=False. Swallow it so `hive` exits cleanly.
        return
