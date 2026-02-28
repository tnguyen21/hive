"""Main CLI entry point: argparse setup and command dispatch."""

import argparse
import os

# Set CLI context for logging configuration — must happen before hive imports
os.environ["HIVE_CLI_CONTEXT"] = "1"

from pathlib import Path

from ..config import Config
from ..db import Database
from ..utils import detect_project
from .core import HiveCLI


def _do_setup(project_path: Path, project_name: str, *, json_mode: bool = False):
    """Write a default .hive.toml if one doesn't exist."""
    import json

    target = project_path / ".hive.toml"
    if target.exists():
        if json_mode:
            print(json.dumps({"config_exists": True, "path": str(target)}))
        else:
            print(f"{target} already exists.")
        return
    target.write_text(f'[project]\nname = "{project_name}"\n\n[hive]\nbackend = "claude"\nmerge_queue_enabled = false\n')
    if json_mode:
        print(json.dumps({"config_created": str(target)}))
    else:
        print(f"Created {target}")


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(description="Hive multi-agent orchestrator")

    # Global options
    parser.add_argument("--db", default=None, help="Database path (default: ~/.hive/hive.db)")
    parser.add_argument("--project", default=None, help="Project directory (auto-detected from git)")
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_mode",
        help="Output JSON (for programmatic use)",
    )

    subparsers = parser.add_subparsers(
        dest="command",
        metavar="command",
    )

    # create command
    create_parser = subparsers.add_parser("create", help="Create a new issue")
    create_parser.add_argument("title", help="Issue title")
    create_parser.add_argument("description", nargs="?", default="", help="Issue description")
    create_parser.add_argument("--priority", type=int, default=2, help="Priority (0-4)")
    create_parser.add_argument(
        "--type",
        default="task",
        dest="issue_type",
        help="Issue type (task, bug, feature)",
    )
    create_parser.add_argument(
        "--model",
        help="Model to use for this issue (overrides global WORKER_MODEL)",
    )
    create_parser.add_argument(
        "--depends-on",
        dest="depends_on",
        action="append",
        help="Issue ID this depends on (can be repeated: --depends-on w-abc --depends-on w-def)",
    )
    create_parser.add_argument(
        "--tags",
        type=str,
        default=None,
        help="Comma-separated tags (e.g. refactor,python,small)",
    )

    # list command
    list_parser = subparsers.add_parser("list", help="List all issues")
    list_parser.add_argument("--status", help="Filter by status")
    list_parser.add_argument(
        "--sort",
        choices=["priority", "created", "updated", "status", "title"],
        default="priority",
        help="Sort field (default: priority)",
    )
    list_parser.add_argument(
        "-r",
        "--reverse",
        action="store_true",
        help="Reverse sort order",
    )
    list_parser.add_argument(
        "--type",
        dest="issue_type",
        help="Filter by issue type (task, bug, feature)",
    )
    list_parser.add_argument("--todo", action="store_true", help="Show only actionable issues (excludes done/finalized/canceled)")
    list_parser.add_argument("--assignee", help="Filter by agent assignee")
    list_parser.add_argument("--limit", type=int, default=50, help="Max issues to show (default: 50)")

    # show command
    show_parser = subparsers.add_parser("show", help="Show issue details")
    show_parser.add_argument("issue_id", help="Issue ID")
    show_parser.add_argument(
        "--format",
        "-f",
        choices=["text", "json"],
        default="text",
        dest="show_format",
        help="Output format: text (default) or json",
    )

    # review command
    review_parser = subparsers.add_parser("review", help="Review done issues before finalizing")
    review_parser.add_argument("issue_id", nargs="?", default=None, help="Optional issue ID to review a specific issue")
    review_parser.add_argument("--limit", type=int, default=20, help="Max issues to show (default: 20)")

    # update command (hidden — advanced)
    update_parser = subparsers.add_parser("update", help="Update an issue")
    update_parser.add_argument("issue_id", help="Issue ID")
    update_parser.add_argument("--title", help="New title")
    update_parser.add_argument("--description", help="New description")
    update_parser.add_argument("--priority", type=int, help="New priority (0-4)")
    update_parser.add_argument("--status", help="New status")
    update_parser.add_argument("--model", help="New model")
    update_parser.add_argument("--tags", type=str, help="Comma-separated tags (e.g. refactor,python,small)")

    # cancel command (hidden — advanced)
    cancel_parser = subparsers.add_parser("cancel", help="Cancel an issue")
    cancel_parser.add_argument("issue_id", help="Issue ID")
    cancel_parser.add_argument("--reason", default="", help="Reason for cancellation")

    # finalize command (hidden — advanced)
    finalize_parser = subparsers.add_parser("finalize", help="Finalize a done issue")
    finalize_parser.add_argument("issue_id", help="Issue ID")
    finalize_parser.add_argument("--resolution", default="", help="Resolution description")

    # retry command (hidden — advanced)
    retry_parser = subparsers.add_parser("retry", help="Retry an escalated issue")
    retry_parser.add_argument("issue_id", help="Issue ID")
    retry_parser.add_argument("--notes", default="", help="Notes about what to try differently")

    # dep command (hidden — advanced)
    dep_parser = subparsers.add_parser("dep", help="Manage issue dependencies")
    dep_subparsers = dep_parser.add_subparsers(dest="dep_command", help="Dependency command")

    dep_add_parser = dep_subparsers.add_parser("add", help="Add a dependency")
    dep_add_parser.add_argument("issue_id", help="Issue that depends on another")
    dep_add_parser.add_argument("depends_on", help="Issue that must be completed first")
    dep_add_parser.add_argument(
        "--type",
        default="blocks",
        dest="dep_type",
        help="Dependency type (blocks, related)",
    )

    dep_remove_parser = dep_subparsers.add_parser("remove", help="Remove a dependency")
    dep_remove_parser.add_argument("issue_id", help="Issue with the dependency")
    dep_remove_parser.add_argument("depends_on", help="Dependency to remove")

    # agents command (hidden — advanced)
    agents_parser = subparsers.add_parser("agents", help="List agents")
    agents_parser.add_argument("agent_id", nargs="?", help="Agent ID (optional - if provided, show agent details)")
    agents_parser.add_argument("--status", help="Filter by status (idle, working, stalled, failed)")

    # logs command (hidden — advanced)
    logs_parser = subparsers.add_parser("logs", help="Show event log")
    logs_parser.add_argument("-f", "--follow", action="store_true", help="Follow new events in real time")
    logs_parser.add_argument(
        "-n",
        "--lines",
        type=int,
        default=20,
        help="Number of recent events to show (default: 20)",
    )
    logs_parser.add_argument("--issue", help="Filter by issue ID")
    logs_parser.add_argument("--agent", help="Filter by agent ID")
    logs_parser.add_argument("--type", dest="event_type", help="Filter by event type")
    logs_parser.add_argument("--daemon", action="store_true", help="Show daemon logs instead of event logs")

    # merges command (hidden — advanced)
    merges_parser = subparsers.add_parser("merges", help="Show merge queue")
    merges_parser.add_argument("--status", help="Filter by status (queued|running|merged|failed)")

    # status command
    subparsers.add_parser("status", help="Show orchestrator status")

    # metrics command (hidden — advanced)
    metrics_parser = subparsers.add_parser("metrics", help="Show metrics and analytics")
    metrics_parser.add_argument("--model", type=str, help="Filter by model name")
    metrics_parser.add_argument("--tag", type=str, help="Filter by tag")
    metrics_parser.add_argument("--type", dest="issue_type", type=str, help="Filter by issue type")
    metrics_parser.add_argument("--group-by", choices=["tag", "type"], help="Group results by tag or type")
    metrics_parser.add_argument("--costs", action="store_true", help="Show token usage and cost estimates")
    metrics_parser.add_argument("--issue", help="Filter costs by specific issue ID (use with --costs)")
    metrics_parser.add_argument("--agent", help="Filter costs by specific agent ID (use with --costs)")

    # top-level start/stop commands
    start_parser = subparsers.add_parser("start", help="Start the hive daemon")
    start_parser.add_argument("--foreground", action="store_true", help="Run in foreground (used by daemon spawner)")
    subparsers.add_parser("stop", help="Stop the hive daemon")

    # queen command
    queen_parser = subparsers.add_parser("queen", help="Launch Queen Bee TUI")
    queen_parser.add_argument(
        "--backend",
        choices=["claude", "codex"],
        default=None,
        help="Override backend (default: from config/HIVE_BACKEND)",
    )
    queen_parser.add_argument(
        "--dangerously-skip-permissions",
        action="store_true",
        default=False,
        help="Pass --dangerously-skip-permissions to Claude CLI (queen and workers)",
    )
    queen_parser.add_argument(
        "--mcp-config",
        nargs="+",
        default=None,
        help="Claude MCP config(s); bare names resolve from ~/.claude/",
    )

    # setup/init command
    subparsers.add_parser("setup", help="Create default .hive.toml config")
    subparsers.add_parser("init", help="Alias for setup")

    # note command (hidden — advanced)
    note_parser = subparsers.add_parser("note", help="Add a knowledge note")
    note_parser.add_argument("content", help="Note content")
    note_parser.add_argument("--issue", dest="issue_id", help="Associate note with an issue ID")
    note_parser.add_argument(
        "--category",
        choices=["discovery", "gotcha", "dependency", "pattern", "context"],
        default="discovery",
        help="Note category (default: discovery)",
    )
    note_parser.add_argument("--to-agent", dest="to_agents", action="append", help="Target agent ID (repeatable)")
    note_parser.add_argument("--to-issue", dest="to_issues", action="append", help="Target issue ID (repeatable)")
    note_parser.add_argument("--must-read", dest="must_read", action="store_true", help="Require acknowledgment")

    # mail subcommand group
    mail_parser = subparsers.add_parser("mail", help="Note delivery inbox and management")
    mail_subparsers = mail_parser.add_subparsers(dest="mail_command")

    mail_inbox_parser = mail_subparsers.add_parser("inbox", help="Show inbox deliveries")
    mail_inbox_parser.add_argument("--agent", dest="agent_id", required=True, help="Agent ID")
    mail_inbox_parser.add_argument("--issue", dest="issue_id", default=None, help="Issue ID")
    mail_inbox_parser.add_argument("--unread", action="store_true", help="Show only unread")

    mail_read_parser = mail_subparsers.add_parser("read", help="Mark delivery as read")
    mail_read_parser.add_argument("delivery_id", type=int, help="Delivery ID")
    mail_read_parser.add_argument("--agent", dest="agent_id", required=True, help="Agent ID")

    mail_ack_parser = mail_subparsers.add_parser("ack", help="Acknowledge a must_read delivery")
    mail_ack_parser.add_argument("delivery_id", type=int, help="Delivery ID")
    mail_ack_parser.add_argument("--agent", dest="agent_id", required=True, help="Agent ID")

    # debug command
    subparsers.add_parser("debug", help="Print diagnostic report for debugging")

    args = parser.parse_args()

    # ── Project auto-detection + layered config ──────────────────────
    if args.project:
        project_path = Path(args.project).resolve()
        project_name = project_path.name
    else:
        project_path, project_name = detect_project()

    # Load layered config: defaults → ~/.hive/config.toml → .hive.toml → env
    Config.load_global(project_root=project_path)

    # Ensure ~/.hive/ directory exists
    Config.HIVE_DIR.mkdir(parents=True, exist_ok=True)

    if args.command in ("setup", "init"):
        _do_setup(project_path, project_name, json_mode=args.json_mode)
        return

    # Resolve DB path: CLI flag > config
    db_path = args.db or Config.DB_PATH

    # Initialize database
    db = Database(db_path)
    db.connect()

    # Auto-register the current project so the daemon knows where it lives
    if project_name:
        db.register_project(project_name, str(project_path))

    # Create CLI
    cli = HiveCLI(db, str(project_path))
    json_mode = args.json_mode

    try:
        if args.command == "create":
            cli.create(
                args.title,
                args.description,
                args.priority,
                args.issue_type,
                model=getattr(args, "model", None),
                tags=getattr(args, "tags", None),
                depends_on=getattr(args, "depends_on", None),
                json_mode=json_mode,
            )

        elif args.command == "list":
            cli.list_issues(
                args.status,
                sort_by=args.sort,
                reverse=args.reverse,
                issue_type=args.issue_type,
                assignee=args.assignee,
                limit=args.limit,
                todo=args.todo,
                json_mode=json_mode,
            )

        elif args.command == "show":
            show_json = json_mode or getattr(args, "show_format", "text") == "json"
            cli.show(args.issue_id, json_mode=show_json)

        elif args.command == "review":
            cli.review(issue_id=args.issue_id, limit=args.limit, json_mode=json_mode)

        elif args.command == "update":
            cli.update(
                args.issue_id,
                title=args.title,
                description=args.description,
                priority=args.priority,
                status=args.status,
                model=getattr(args, "model", None),
                tags=getattr(args, "tags", None),
                json_mode=json_mode,
            )

        elif args.command == "cancel":
            cli.cancel(args.issue_id, reason=args.reason, json_mode=json_mode)

        elif args.command == "finalize":
            cli.finalize(args.issue_id, resolution=args.resolution, json_mode=json_mode)

        elif args.command == "retry":
            cli.retry(args.issue_id, notes=args.notes, json_mode=json_mode)

        elif args.command == "dep":
            if args.dep_command == "add":
                cli.dep_add(
                    args.issue_id,
                    args.depends_on,
                    dep_type=args.dep_type,
                    json_mode=json_mode,
                )
            elif args.dep_command == "remove":
                cli.dep_remove(args.issue_id, args.depends_on, json_mode=json_mode)
            else:
                dep_parser.print_help()

        elif args.command == "agents":
            cli.list_agents(agent_id=getattr(args, "agent_id", None), status=args.status, json_mode=json_mode)

        elif args.command == "logs":
            cli.logs(
                follow=args.follow,
                n=args.lines,
                issue_id=args.issue,
                agent_id=args.agent,
                event_type=getattr(args, "event_type", None),
                daemon=getattr(args, "daemon", False),
                json_mode=json_mode,
            )

        elif args.command == "merges":
            cli.merges(status=args.status, json_mode=json_mode)

        elif args.command == "status":
            cli.status(json_mode=json_mode)

        elif args.command == "metrics":
            cli.metrics(
                model=args.model,
                tag=args.tag,
                issue_type=getattr(args, "issue_type", None),
                group_by=getattr(args, "group_by", None),
                show_costs=getattr(args, "costs", False),
                issue_id=getattr(args, "issue", None),
                agent_id=getattr(args, "agent", None),
                json_mode=json_mode,
            )

        elif args.command == "start":
            cli.start(foreground=getattr(args, "foreground", False), json_mode=json_mode)

        elif args.command == "stop":
            cli.stop(json_mode=json_mode)

        elif args.command == "queen":
            cli.queen(
                backend=args.backend,
                skip_permissions=args.dangerously_skip_permissions,
                mcp_configs=args.mcp_config,
            )

        elif args.command == "note":
            to_agents = getattr(args, "to_agents", None)
            to_issues = getattr(args, "to_issues", None)
            must_read = getattr(args, "must_read", False)
            if to_agents or to_issues:
                cli.note_with_targets(
                    args.content,
                    issue_id=args.issue_id,
                    to_agents=to_agents,
                    to_issues=to_issues,
                    must_read=must_read,
                    json_mode=json_mode,
                )
            else:
                cli.add_note(
                    args.content,
                    issue_id=args.issue_id,
                    category=args.category,
                    json_mode=json_mode,
                )

        elif args.command == "mail":
            if args.mail_command == "inbox":
                cli.mail_inbox(
                    args.agent_id,
                    issue_id=args.issue_id,
                    unread_only=args.unread,
                    json_mode=json_mode,
                )
            elif args.mail_command == "read":
                cli.mail_read(args.delivery_id, args.agent_id, json_mode=json_mode)
            elif args.mail_command == "ack":
                cli.mail_ack(args.delivery_id, args.agent_id, json_mode=json_mode)
            else:
                mail_parser.print_help()

        elif args.command == "debug":
            cli.debug(json_mode=json_mode)

        else:
            parser.print_help()

    finally:
        db.close()


if __name__ == "__main__":
    main()
