"""Human CLI interface for Hive orchestrator."""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Optional

from .config import Config
from .db import Database
from .opencode import OpenCodeClient
from .orchestrator import Orchestrator


class HiveCLI:
    """Command-line interface for Hive orchestrator."""

    def __init__(self, db: Database, project_path: str):
        """
        Initialize CLI.

        Args:
            db: Database instance
            project_path: Path to the project repository
        """
        self.db = db
        self.project_path = Path(project_path).resolve()
        self.project_name = self.project_path.name

    def create(self, title: str, description: str = "", priority: int = 2):
        """
        Create a new issue manually.

        Args:
            title: Issue title
            description: Issue description
            priority: Priority (0=critical, 4=low)
        """
        issue_id = self.db.create_issue(
            title=title,
            description=description,
            priority=priority,
            project=self.project_name,
        )
        print(f"Created issue: {issue_id}")
        print(f"  Title: {title}")
        print(f"  Priority: {priority}")
        return issue_id

    def list_issues(self, status: Optional[str] = None):
        """
        List all issues.

        Args:
            status: Filter by status (optional)
        """
        if status:
            cursor = self.db.conn.execute(
                "SELECT * FROM issues WHERE project = ? AND status = ? ORDER BY priority, created_at",
                (self.project_name, status),
            )
        else:
            cursor = self.db.conn.execute(
                "SELECT * FROM issues WHERE project = ? ORDER BY status, priority, created_at",
                (self.project_name,),
            )

        issues = [dict(row) for row in cursor.fetchall()]

        if not issues:
            print("No issues found.")
            return

        print(f"\n{'ID':<12} {'Status':<12} {'Pri':<4} {'Title':<40}")
        print("-" * 70)

        for issue in issues:
            print(
                f"{issue['id']:<12} {issue['status']:<12} {issue['priority']:<4} {issue['title'][:40]}"
            )

        print(f"\nTotal: {len(issues)} issues")

    def show_ready(self):
        """Show ready queue (unblocked, unassigned issues)."""
        ready = self.db.get_ready_queue(limit=50)

        if not ready:
            print("No ready issues.")
            return

        print(f"\n{'ID':<12} {'Priority':<8} {'Title':<50}")
        print("-" * 70)

        for issue in ready:
            print(f"{issue['id']:<12} {issue['priority']:<8} {issue['title'][:50]}")

        print(f"\nTotal: {len(ready)} ready issues")

    def show(self, issue_id: str):
        """
        Show issue details and events.

        Args:
            issue_id: Issue ID to display
        """
        issue = self.db.get_issue(issue_id)

        if not issue:
            print(f"Issue not found: {issue_id}")
            return

        print(f"\nIssue: {issue['id']}")
        print(f"Title: {issue['title']}")
        print(f"Status: {issue['status']}")
        print(f"Priority: {issue['priority']}")
        print(f"Type: {issue['type']}")
        print(f"Assignee: {issue['assignee'] or 'None'}")
        print(f"Created: {issue['created_at']}")

        if issue['description']:
            print(f"\nDescription:\n{issue['description']}")

        # Show dependencies
        cursor = self.db.conn.execute(
            """
            SELECT i.id, i.title, i.status
            FROM dependencies d
            JOIN issues i ON d.depends_on = i.id
            WHERE d.issue_id = ?
            """,
            (issue_id,),
        )
        deps = [dict(row) for row in cursor.fetchall()]

        if deps:
            print("\nDepends on:")
            for dep in deps:
                print(f"  - {dep['id']}: {dep['title']} ({dep['status']})")

        # Show events
        events = self.db.get_events(issue_id=issue_id)

        if events:
            print(f"\nEvents ({len(events)}):")
            for event in events[:10]:  # Show last 10 events
                print(f"  [{event['created_at']}] {event['event_type']}")
                if event['detail']:
                    import json
                    detail = json.loads(event['detail'])
                    for key, value in detail.items():
                        print(f"    {key}: {value}")

    def close(self, issue_id: str):
        """
        Mark an issue as canceled.

        Args:
            issue_id: Issue ID to close
        """
        issue = self.db.get_issue(issue_id)

        if not issue:
            print(f"Issue not found: {issue_id}")
            return

        self.db.update_issue_status(issue_id, "canceled")
        print(f"Closed issue: {issue_id}")

    def _format_event(self, event: dict) -> str:
        """Format a single event as a log line."""
        ts = event["created_at"]
        etype = event["event_type"]
        issue = event["issue_id"] or "-"
        agent = event["agent_id"] or "-"

        line = f"{ts}  {etype:<24s}  issue={issue:<10s}  agent={agent:<10s}"

        if event["detail"]:
            try:
                detail = json.loads(event["detail"])
                parts = [f"{k}={v}" for k, v in detail.items()]
                line += "  " + " ".join(parts)
            except (json.JSONDecodeError, TypeError):
                line += f"  {event['detail']}"

        return line

    def logs(self, follow: bool = False, n: int = 20, issue_id: Optional[str] = None, agent_id: Optional[str] = None):
        """
        Show event log, optionally tailing for new events.

        Args:
            follow: If True, keep polling for new events (like tail -f)
            n: Number of recent events to show initially
            issue_id: Filter by issue ID
            agent_id: Filter by agent ID
        """
        recent = self.db.get_recent_events(n=n, issue_id=issue_id, agent_id=agent_id)

        for event in recent:
            print(self._format_event(event))

        if not follow:
            return

        # Track the last id we've seen
        cursor = recent[-1]["id"] if recent else self.db.get_max_event_id()

        try:
            while True:
                time.sleep(0.5)
                new_events = self.db.get_events_since(after_id=cursor, issue_id=issue_id, agent_id=agent_id)
                for event in new_events:
                    print(self._format_event(event))
                    cursor = event["id"]
        except KeyboardInterrupt:
            pass

    def status(self):
        """Show orchestrator status."""
        # Count issues by status
        cursor = self.db.conn.execute(
            """
            SELECT status, COUNT(*) as count
            FROM issues
            WHERE project = ?
            GROUP BY status
            """,
            (self.project_name,),
        )
        status_counts = {row[0]: row[1] for row in cursor.fetchall()}

        # Get active agents
        active_agents = self.db.get_active_agents()

        print("\n=== Hive Status ===")
        print(f"\nProject: {self.project_name}")
        print(f"Database: {self.db.db_path}")

        print("\nIssues:")
        for status in ["open", "in_progress", "done", "finalized", "failed", "blocked", "canceled"]:
            count = status_counts.get(status, 0)
            if count > 0:
                print(f"  {status}: {count}")

        print(f"\nActive workers: {len(active_agents)}/{Config.MAX_AGENTS}")
        for agent in active_agents:
            issue = self.db.get_issue(agent["current_issue"]) if agent["current_issue"] else None
            issue_title = issue["title"] if issue else "unknown"
            print(f"  - {agent['name']}: {issue_title}")

        # Ready queue
        ready = self.db.get_ready_queue(limit=10)
        print(f"\nReady queue: {len(ready)} issues")

        # Merge queue
        cursor = self.db.conn.execute(
            "SELECT COUNT(*) FROM merge_queue WHERE status = 'queued'"
        )
        merge_count = cursor.fetchone()[0]
        print(f"Merge queue: {merge_count} pending")


async def run_orchestrator(db: Database, project_path: str):
    """
    Run orchestrator in background.

    Args:
        db: Database instance
        project_path: Path to the project repository
    """
    async with OpenCodeClient(Config.OPENCODE_URL, Config.OPENCODE_PASSWORD) as opencode:
        orchestrator = Orchestrator(
            db=db,
            opencode_client=opencode,
            project_path=project_path,
            project_name=Path(project_path).name,
        )

        await orchestrator.start()


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(description="Hive multi-agent orchestrator")

    # Global options
    parser.add_argument("--db", default="hive.db", help="Database path")
    parser.add_argument("--project", default=".", help="Project directory")

    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # create command
    create_parser = subparsers.add_parser("create", help="Create a new issue")
    create_parser.add_argument("title", help="Issue title")
    create_parser.add_argument("description", nargs="?", default="", help="Issue description")
    create_parser.add_argument("--priority", type=int, default=2, help="Priority (0-4)")

    # list command
    list_parser = subparsers.add_parser("list", help="List all issues")
    list_parser.add_argument("--status", help="Filter by status")

    # ready command
    subparsers.add_parser("ready", help="Show ready queue")

    # show command
    show_parser = subparsers.add_parser("show", help="Show issue details")
    show_parser.add_argument("issue_id", help="Issue ID")

    # close command
    close_parser = subparsers.add_parser("close", help="Close an issue")
    close_parser.add_argument("issue_id", help="Issue ID")

    # logs command
    logs_parser = subparsers.add_parser("logs", help="Show event log (tail -f style)")
    logs_parser.add_argument("-f", "--follow", action="store_true", help="Follow new events in real time")
    logs_parser.add_argument("-n", "--lines", type=int, default=20, help="Number of recent events to show (default: 20)")
    logs_parser.add_argument("--issue", help="Filter by issue ID")
    logs_parser.add_argument("--agent", help="Filter by agent ID")

    # status command
    subparsers.add_parser("status", help="Show orchestrator status")

    # start command
    subparsers.add_parser("start", help="Start orchestrator")

    args = parser.parse_args()

    # Initialize database
    db = Database(args.db)
    db.connect()

    # Create CLI
    cli = HiveCLI(db, args.project)

    try:
        if args.command == "create":
            cli.create(args.title, args.description, args.priority)

        elif args.command == "list":
            cli.list_issues(args.status)

        elif args.command == "ready":
            cli.show_ready()

        elif args.command == "show":
            cli.show(args.issue_id)

        elif args.command == "close":
            cli.close(args.issue_id)

        elif args.command == "logs":
            cli.logs(follow=args.follow, n=args.lines, issue_id=args.issue, agent_id=args.agent)

        elif args.command == "status":
            cli.status()

        elif args.command == "start":
            print(f"Starting Hive orchestrator for project: {args.project}")
            print("Press Ctrl+C to stop")
            try:
                asyncio.run(run_orchestrator(db, args.project))
            except KeyboardInterrupt:
                print("\nStopping orchestrator...")

        else:
            parser.print_help()

    finally:
        db.close()


if __name__ == "__main__":
    main()
