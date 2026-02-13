"""Human CLI interface for Hive orchestrator."""

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import Config
from .daemon import HiveDaemon, run_daemon_foreground
from .db import Database

from .sse import SSEClient
from .tools import ToolExecutor


class HiveCLI:
    """Command-line interface for Hive orchestrator."""

    def __init__(self, db: Database, project_path: str):
        self.db = db
        self.project_path = Path(project_path).resolve()
        self.project_name = self.project_path.name
        self._executor = ToolExecutor(db, self.project_name)

    def _run_tool(self, tool_name: str, params: dict, *, json_mode: bool = False):
        """Execute a tool and print the result.

        Args:
            tool_name: Name of the tool handler (e.g. "hive_create_issue")
            params: Parameters dict for the tool
            json_mode: If True, print JSON output; otherwise human-readable
        """
        result = self._executor.execute(tool_name, params)

        if "error" in result:
            if json_mode:
                print(json.dumps({"error": result["error"]}))
            else:
                print(f"Error: {result['error']}", file=sys.stderr)
            sys.exit(1)

        if json_mode:
            print(json.dumps(result["result"], default=str))
        return result["result"]

    # ── Issue management ─────────────────────────────────────────────

    def create(
        self,
        title: str,
        description: str = "",
        priority: int = 2,
        issue_type: str = "task",
        model: Optional[str] = None,
        depends_on: Optional[list] = None,
        *,
        json_mode: bool = False,
    ):
        """Create a new issue."""
        params = {
            "title": title,
            "description": description,
            "priority": priority,
            "type": issue_type,
        }
        if model:
            params["model"] = model
        if depends_on:
            params["depends_on"] = depends_on

        result = self._run_tool(
            "hive_create_issue",
            params,
            json_mode=json_mode,
        )
        if not json_mode and result:
            print(f"Created issue: {result['issue_id']}")
            print(f"  Title: {title}")
            print(f"  Priority: {priority}")
            if depends_on:
                print(f"  Depends on: {', '.join(depends_on)}")
        return result.get("issue_id") if result else None

    def list_issues(
        self,
        status: Optional[str] = None,
        sort_by: str = "priority",
        reverse: bool = False,
        issue_type: Optional[str] = None,
        assignee: Optional[str] = None,
        limit: int = 50,
        *,
        json_mode: bool = False,
    ):
        """List all issues."""
        params: dict = {"sort_by": sort_by, "reverse": reverse, "limit": limit}
        if status:
            params["status"] = status
        if issue_type:
            params["issue_type"] = issue_type
        if assignee:
            params["assignee"] = assignee
        result = self._run_tool("hive_list_issues", params, json_mode=json_mode)
        if not json_mode and result:
            issues = result.get("issues", [])
            if not issues:
                print("No issues found.")
                return
            print(f"\n{'ID':<12} {'Status':<12} {'Pri':<4} {'Type':<10} {'Title':<40}")
            print("-" * 80)
            for issue in issues:
                itype = issue.get("type", "")[:10]
                print(f"{issue['id']:<12} {issue['status']:<12} {issue['priority']:<4} {itype:<10} {issue['title'][:40]}")
            print(f"\nTotal: {len(issues)} issues")

    def show(self, issue_id: str, *, json_mode: bool = False):
        """Show issue details and events."""
        result = self._run_tool("hive_get_issue", {"issue_id": issue_id}, json_mode=json_mode)
        if not json_mode and result:
            issue = result["issue"]
            print(f"\nIssue: {issue['id']}")
            print(f"Title: {issue['title']}")
            print(f"Status: {issue['status']}")
            print(f"Priority: {issue['priority']}")
            print(f"Type: {issue['type']}")
            print(f"Assignee: {issue['assignee'] or 'None'}")
            if issue.get("model"):
                print(f"Model: {issue['model']}")
            print(f"Created: {issue['created_at']}")
            if issue["description"]:
                print(f"\nDescription:\n{issue['description']}")
            deps = result.get("dependencies", [])
            if deps:
                print("\nDepends on:")
                for dep in deps:
                    print(f"  - {dep['id']}: {dep['title']} ({dep['status']})")
            events = result.get("recent_events", [])
            if events:
                print(f"\nEvents ({len(events)}):")
                for event in events[:10]:
                    print(f"  [{event['created_at']}] {event['event_type']}")
                    if event["detail"]:
                        detail = json.loads(event["detail"]) if isinstance(event["detail"], str) else event["detail"]
                        for key, value in detail.items():
                            print(f"    {key}: {value}")

    def show_ready(self, *, json_mode: bool = False):
        """Show ready queue."""
        result = self._run_tool("hive_show_ready", {}, json_mode=json_mode)
        if not json_mode and result:
            ready = result.get("ready_issues", [])
            if not ready:
                print("No ready issues.")
                return
            print(f"\n{'ID':<12} {'Priority':<8} {'Title':<50}")
            print("-" * 70)
            for issue in ready:
                print(f"{issue['id']:<12} {issue['priority']:<8} {issue['title'][:50]}")
            print(f"\nTotal: {len(ready)} ready issues")

    def update(
        self,
        issue_id: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
        priority: Optional[int] = None,
        status: Optional[str] = None,
        model: Optional[str] = None,
        *,
        json_mode: bool = False,
    ):
        """Update an issue."""
        params = {"issue_id": issue_id}
        if title is not None:
            params["title"] = title
        if description is not None:
            params["description"] = description
        if priority is not None:
            params["priority"] = priority
        if status is not None:
            params["status"] = status
        if model is not None:
            params["model"] = model
        result = self._run_tool("hive_update_issue", params, json_mode=json_mode)
        if not json_mode and result:
            print(result.get("message", f"Updated issue {issue_id}"))

    def cancel(self, issue_id: str, reason: str = "", *, json_mode: bool = False):
        """Cancel an issue."""
        result = self._run_tool(
            "hive_cancel_issue",
            {"issue_id": issue_id, "reason": reason},
            json_mode=json_mode,
        )
        if not json_mode and result:
            print(result.get("message", f"Canceled issue {issue_id}"))

    def finalize(self, issue_id: str, resolution: str = "", *, json_mode: bool = False):
        """Finalize/close an issue."""
        result = self._run_tool(
            "hive_close_issue",
            {"issue_id": issue_id, "resolution": resolution},
            json_mode=json_mode,
        )
        if not json_mode and result:
            print(result.get("message", f"Finalized issue {issue_id}"))

    def retry(self, issue_id: str, notes: str = "", *, json_mode: bool = False):
        """Retry a failed/blocked issue."""
        result = self._run_tool(
            "hive_retry_issue",
            {"issue_id": issue_id, "notes": notes},
            json_mode=json_mode,
        )
        if not json_mode and result:
            print(result.get("message", f"Retrying issue {issue_id}"))

    def escalate(self, issue_id: str, reason: str = "", *, json_mode: bool = False):
        """Escalate an issue."""
        result = self._run_tool(
            "hive_escalate_issue",
            {"issue_id": issue_id, "reason": reason},
            json_mode=json_mode,
        )
        if not json_mode and result:
            print(result.get("message", f"Escalated issue {issue_id}"))

    def molecule(
        self,
        title: str,
        description: str = "",
        steps_json: str = "[]",
        model: Optional[str] = None,
        *,
        json_mode: bool = False,
    ):
        """Create a molecule (multi-step workflow)."""
        try:
            steps = json.loads(steps_json)
        except json.JSONDecodeError as e:
            if json_mode:
                print(json.dumps({"error": f"Invalid steps JSON: {e}"}))
            else:
                print(f"Error: Invalid steps JSON: {e}", file=sys.stderr)
            sys.exit(1)
        params = {"title": title, "description": description, "steps": steps}
        if model:
            params["model"] = model

        result = self._run_tool(
            "hive_create_molecule",
            params,
            json_mode=json_mode,
        )
        if not json_mode and result:
            print(result.get("message", f"Created molecule {result.get('molecule_id', '')}"))
            for step in result.get("steps", []):
                print(f"  Step {step['index']}: {step['id']} - {step['title']}")

    def dep_add(
        self,
        issue_id: str,
        depends_on: str,
        dep_type: str = "blocks",
        *,
        json_mode: bool = False,
    ):
        """Add a dependency between issues."""
        result = self._run_tool(
            "hive_add_dependency",
            {"issue_id": issue_id, "depends_on": depends_on, "type": dep_type},
            json_mode=json_mode,
        )
        if not json_mode and result:
            print(result.get("message", "Added dependency"))

    def dep_remove(self, issue_id: str, depends_on: str, *, json_mode: bool = False):
        """Remove a dependency between issues."""
        result = self._run_tool(
            "hive_remove_dependency",
            {"issue_id": issue_id, "depends_on": depends_on},
            json_mode=json_mode,
        )
        if not json_mode and result:
            print(result.get("message", "Removed dependency"))

    def merges(self, status: Optional[str] = None, *, json_mode: bool = False):
        """List merge queue entries."""
        query = "SELECT mq.*, i.title as issue_title, a.name as agent_name FROM merge_queue mq JOIN issues i ON mq.issue_id = i.id LEFT JOIN agents a ON mq.agent_id = a.id"
        params = []
        if status:
            query += " WHERE mq.status = ?"
            params.append(status)
        query += " ORDER BY mq.enqueued_at DESC LIMIT 50"

        cursor = self.db.conn.execute(query, params)
        entries = [dict(row) for row in cursor.fetchall()]

        if json_mode:
            print(json.dumps({"count": len(entries), "merges": entries}, indent=2, default=str))
        else:
            if not entries:
                print("No merge queue entries found.")
                return
            print(f"\n{'ID':<6} {'Status':<10} {'Issue':<14} {'Title':<30} {'Branch':<25} {'Enqueued'}")
            print("-" * 100)
            for e in entries:
                title = (e.get("issue_title") or "")[:30]
                branch = (e.get("branch_name") or "")[:25]
                print(f"{e['id']:<6} {e['status']:<10} {e['issue_id']:<14} {title:<30} {branch:<25} {e.get('enqueued_at', '')}")
            print(f"\nTotal: {len(entries)} entries")

    def status(self, *, json_mode: bool = False):
        """Show orchestrator status."""
        result = self._run_tool("hive_get_status", {}, json_mode=json_mode)
        if not json_mode and result:
            print("\n=== Hive Status ===")
            print(f"\nProject: {result.get('project', self.project_name)}")
            print("\nIssues:")
            for s in [
                "open",
                "in_progress",
                "done",
                "finalized",
                "failed",
                "blocked",
                "canceled",
            ]:
                count = result.get("issues", {}).get(s, 0)
                if count > 0:
                    print(f"  {s}: {count}")
            print(f"\nActive workers: {result.get('active_agents', 0)}/{Config.MAX_AGENTS}")
            print(f"Ready queue: {result.get('ready_queue', 0)} issues")
            mq = result.get("merge_queue", {})
            if isinstance(mq, dict):
                parts = []
                for k in ["queued", "running", "merged", "failed"]:
                    v = mq.get(k, 0)
                    if v > 0:
                        parts.append(f"{v} {k}")
                print(f"Merge queue: {', '.join(parts) if parts else 'empty'}")
            else:
                print(f"Merge queue: {mq} pending")

    def list_agents(self, status: Optional[str] = None, *, json_mode: bool = False):
        """List agents."""
        params = {}
        if status:
            params["status"] = status
        result = self._run_tool("hive_list_agents", params, json_mode=json_mode)
        if not json_mode and result:
            agents = result.get("agents", [])
            if not agents:
                print("No agents found.")
                return
            print(f"\n{'ID':<16} {'Name':<16} {'Status':<10} {'Current Issue':<30}")
            print("-" * 72)
            for agent in agents:
                issue_title = agent.get("current_issue_title", agent.get("current_issue", "")) or "-"
                print(f"{agent['id']:<16} {agent['name']:<16} {agent['status']:<10} {str(issue_title)[:30]}")

    def show_agent(self, agent_id: str, *, json_mode: bool = False):
        """Show agent details."""
        result = self._run_tool("hive_get_agent", {"agent_id": agent_id}, json_mode=json_mode)
        if not json_mode and result:
            print(f"\nAgent: {result.get('id', agent_id)}")
            print(f"Name: {result.get('name', '')}")
            print(f"Status: {result.get('status', '')}")
            if result.get("current_issue"):
                print(f"Current issue: {result['current_issue']}")
            events = result.get("recent_events", [])
            if events:
                print(f"\nRecent events ({len(events)}):")
                for event in events[:5]:
                    print(f"  [{event['created_at']}] {event['event_type']}")

    def get_events(
        self,
        issue_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        event_type: Optional[str] = None,
        limit: int = 20,
        *,
        json_mode: bool = False,
    ):
        """Get events."""
        params = {"limit": limit}
        if issue_id:
            params["issue_id"] = issue_id
        if agent_id:
            params["agent_id"] = agent_id
        if event_type:
            params["event_type"] = event_type
        result = self._run_tool("hive_get_events", params, json_mode=json_mode)
        if not json_mode and result:
            events = result.get("events", [])
            if not events:
                print("No events found.")
                return
            for event in events:
                ts = event.get("created_at", "")
                etype = event.get("event_type", "")
                iss = event.get("issue_id") or "-"
                agent = event.get("agent_id") or "-"
                line = f"{ts}  {etype:<24s}  issue={iss:<10s}  agent={agent:<10s}"
                if event.get("detail"):
                    try:
                        detail = json.loads(event["detail"]) if isinstance(event["detail"], str) else event["detail"]
                        parts = [f"{k}={v}" for k, v in detail.items()]
                        line += "  " + " ".join(parts)
                    except (json.JSONDecodeError, TypeError, AttributeError):
                        line += f"  {event['detail']}"
                print(line)

    # ── Legacy commands kept for backward compat ─────────────────────

    def close(self, issue_id: str, *, json_mode: bool = False):
        """Mark an issue as canceled (alias for cancel)."""
        self.cancel(issue_id, json_mode=json_mode)

    # ── Event log (tail-style, not tool-backed) ─────────────────────

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

    @staticmethod
    def _event_to_json(event: dict) -> dict:
        """Prepare an event dict for JSON serialisation.

        Parses the ``detail`` field from a JSON string into a real object
        so the output is a proper nested structure rather than an escaped
        string.
        """
        out = dict(event)
        if out.get("detail"):
            try:
                out["detail"] = json.loads(out["detail"]) if isinstance(out["detail"], str) else out["detail"]
            except (json.JSONDecodeError, TypeError):
                pass  # keep as-is
        return out

    def logs(
        self,
        follow: bool = False,
        n: int = 20,
        issue_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        *,
        json_mode: bool = False,
    ):
        """Show event log, optionally tailing for new events."""
        recent = self.db.get_recent_events(n=n, issue_id=issue_id, agent_id=agent_id)

        if json_mode:
            if follow:
                # Streaming JSON: one object per line (JSONL) so callers
                # can consume incrementally.
                for event in recent:
                    print(json.dumps(self._event_to_json(event), default=str))

                cursor = recent[-1]["id"] if recent else self.db.get_max_event_id()
                try:
                    while True:
                        time.sleep(0.5)
                        new_events = self.db.get_events_since(after_id=cursor, issue_id=issue_id, agent_id=agent_id)
                        for event in new_events:
                            print(json.dumps(self._event_to_json(event), default=str))
                            cursor = event["id"]
                except KeyboardInterrupt:
                    pass
            else:
                events = [self._event_to_json(e) for e in recent]
                print(json.dumps(events, default=str))
            return

        for event in recent:
            print(self._format_event(event))

        if not follow:
            return

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

    def costs(
        self,
        issue_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        *,
        json_mode: bool = False,
    ):
        """Show token usage and cost estimates."""
        usage = self.db.get_token_usage(issue_id=issue_id, agent_id=agent_id)

        if json_mode:
            print(json.dumps(usage, indent=2))
            return

        print("\n=== Token Usage & Costs ===")
        if issue_id:
            print(f"Issue: {issue_id}")
        elif agent_id:
            print(f"Agent: {agent_id}")
        else:
            print("Project-wide")

        print(f"\nTotal tokens: {usage['total_tokens']:,}")
        print(f"  Input tokens: {usage['total_input_tokens']:,}")
        print(f"  Output tokens: {usage['total_output_tokens']:,}")
        print(f"Estimated cost: ${usage['estimated_cost_usd']:.4f}")

        # Show breakdowns if not filtered
        if not issue_id and not agent_id:
            issue_breakdown = usage.get("issue_breakdown", {})
            if issue_breakdown:
                print("\n=== Top Issues by Token Usage ===")
                sorted_issues = sorted(
                    issue_breakdown.items(),
                    key=lambda x: x[1]["input_tokens"] + x[1]["output_tokens"],
                    reverse=True,
                )
                for issue, tokens in sorted_issues[:10]:  # Top 10
                    total = tokens["input_tokens"] + tokens["output_tokens"]
                    print(f"{issue}: {total:,} tokens")

            agent_breakdown = usage.get("agent_breakdown", {})
            if agent_breakdown:
                print("\n=== Top Agents by Token Usage ===")
                sorted_agents = sorted(
                    agent_breakdown.items(),
                    key=lambda x: x[1]["input_tokens"] + x[1]["output_tokens"],
                    reverse=True,
                )
                for agent, tokens in sorted_agents[:10]:  # Top 10
                    total = tokens["input_tokens"] + tokens["output_tokens"]
                    print(f"{agent}: {total:,} tokens")

            model_breakdown = usage.get("model_breakdown", {})
            if model_breakdown:
                print("\n=== Usage by Model ===")
                for model, tokens in model_breakdown.items():
                    total = tokens["input_tokens"] + tokens["output_tokens"]
                    print(f"{model}: {total:,} tokens")

    # ── Daemon management ────────────────────────────────────────────

    def _make_daemon(self) -> HiveDaemon:
        return HiveDaemon(self.project_name, str(self.project_path))

    def start(self, foreground: bool = False, *, json_mode: bool = False):
        """Start the hive daemon (or run in foreground for debugging)."""
        if foreground:
            if not json_mode:
                print(f"Starting Hive orchestrator in foreground for project: {self.project_name}")
                print("Press Ctrl+C to stop\n")
            try:
                run_daemon_foreground(self.db, str(self.project_path), self.project_name)
            except KeyboardInterrupt:
                if not json_mode:
                    print("\nStopping orchestrator...")
            return

        daemon = self._make_daemon()

        # Check if already running
        status = daemon.status()
        if status["running"]:
            if json_mode:
                print(json.dumps({"status": "already_running", "pid": status["pid"]}))
            else:
                print(f"Hive daemon already running (PID {status['pid']})")
                print(f"  Log file: {status.get('log_file', 'N/A')}")
                print("\n  hive stop        — stop the daemon")
                print("  hive daemon logs — view daemon logs")
            return

        # Start daemon as a detached subprocess
        started = daemon.start(db_path=self.db.db_path)

        if started:
            ds = daemon.status()
            if json_mode:
                print(json.dumps({"status": "started", "pid": ds["pid"]}))
            else:
                print(f"Hive daemon started (PID {ds['pid']})")
                print(f"  Log file: {ds.get('log_file', 'N/A')}")
                print("\n  hive status      — check system status")
                print("  hive stop        — stop the daemon")
                print("  hive daemon logs — view daemon logs")
        else:
            if json_mode:
                print(json.dumps({"error": "Failed to start daemon"}))
                sys.exit(1)
            else:
                print("Failed to start daemon. Check logs:")
                print(f"  {daemon.log_file}")

    def stop(self, *, json_mode: bool = False):
        """Stop the hive daemon."""
        daemon = self._make_daemon()
        status = daemon.status()
        if not status["running"]:
            if json_mode:
                print(json.dumps({"status": "not_running"}))
            else:
                print("Hive daemon is not running.")
            return
        pid = status["pid"]
        stopped = daemon.stop()
        if stopped:
            if json_mode:
                print(json.dumps({"status": "stopped"}))
            else:
                print(f"Hive daemon stopped (was PID {pid})")
        else:
            if json_mode:
                print(json.dumps({"error": f"Failed to stop daemon (PID {pid})"}))
                sys.exit(1)
            else:
                print(f"Failed to stop daemon (PID {pid})")

    def daemon_start(self, foreground: bool = False, *, json_mode: bool = False):
        """Start the orchestrator daemon (legacy, delegates to start)."""
        self.start(foreground=foreground, json_mode=json_mode)

    def daemon_stop(self, *, json_mode: bool = False):
        """Stop the orchestrator daemon."""
        self.stop(json_mode=json_mode)

    def daemon_restart(self, *, json_mode: bool = False):
        """Restart the orchestrator daemon."""
        daemon = self._make_daemon()
        status = daemon.status()

        if json_mode:
            # For JSON mode, perform restart silently and return final result
            if status["running"]:
                stopped = daemon.stop()
                if not stopped:
                    print(json.dumps({"error": "Failed to stop daemon for restart"}))
                    sys.exit(1)
                time.sleep(0.5)

            # Start daemon
            started = daemon.start(db_path=self.db.db_path)
            if started:
                final_status = daemon.status()
                print(json.dumps({"status": "restarted", "pid": final_status["pid"]}))
            else:
                print(json.dumps({"error": "Failed to restart daemon"}))
                sys.exit(1)
        else:
            # For regular mode, use existing stop/start with output
            if status["running"]:
                self.stop(json_mode=False)
                time.sleep(0.5)
            self.start(json_mode=False)

    def daemon_status(self, *, json_mode: bool = False):
        """Show daemon status."""
        daemon = self._make_daemon()
        status = daemon.status()
        if json_mode:
            result = {
                "running": status["running"],
                "pid": status.get("pid"),
                "log_file": status.get("log_file"),
            }
            print(json.dumps(result))
        else:
            print(status["message"])
            if status["running"]:
                print(f"Log file: {status.get('log_file', 'N/A')}")

    def daemon_logs(self, lines: int = 50, follow: bool = False):
        """Show daemon logs."""
        daemon = self._make_daemon()
        daemon.logs(lines=lines, follow=follow)

    # ── Queen Bee TUI ─────────────────────────────────────────────────

    def queen(self):
        """Launch Queen Bee TUI attached to the opencode server."""
        opencode_cmd = os.environ.get("OPENCODE_CMD", "opencode")
        cmd = [
            opencode_cmd,
            "attach",
            Config.OPENCODE_URL,
            "--dir",
            str(self.project_path),
        ]

        print("Launching Queen Bee TUI...\n")
        os.execvp(cmd[0], cmd)

    def watch(self, issue_id: str, *, json_mode: bool = False):
        """Watch live events from a worker's OpenCode session."""
        # First, get the issue and find its assignee
        cursor = self.db.conn.execute("SELECT assignee FROM issues WHERE id = ?", (issue_id,))
        result = cursor.fetchone()
        if not result:
            if json_mode:
                print(json.dumps({"error": f"Issue {issue_id} not found"}))
            else:
                print(f"Error: Issue {issue_id} not found", file=sys.stderr)
            sys.exit(1)

        assignee = result[0]
        if not assignee:
            if json_mode:
                print(json.dumps({"error": f"Issue {issue_id} is not assigned to any agent"}))
            else:
                print(f"Error: Issue {issue_id} is not assigned to any agent", file=sys.stderr)
            sys.exit(1)

        # Get the agent's session_id and worktree
        cursor = self.db.conn.execute("SELECT session_id, worktree FROM agents WHERE id = ?", (assignee,))
        result = cursor.fetchone()
        if not result:
            if json_mode:
                print(json.dumps({"error": f"Agent {assignee} not found"}))
            else:
                print(f"Error: Agent {assignee} not found", file=sys.stderr)
            sys.exit(1)

        session_id, worktree = result
        if not session_id:
            if json_mode:
                print(json.dumps({"error": f"Agent {assignee} has no active session"}))
            else:
                print(f"Error: Agent {assignee} has no active session", file=sys.stderr)
            sys.exit(1)

        if not worktree:
            if json_mode:
                print(json.dumps({"error": f"Agent {assignee} has no worktree"}))
            else:
                print(f"Error: Agent {assignee} has no worktree", file=sys.stderr)
            sys.exit(1)

        # Run the async event streaming
        asyncio.run(self._watch_events(assignee, worktree, issue_id, json_mode))

    async def _watch_events(self, agent_id: str, worktree: str, issue_id: str, json_mode: bool):
        """Stream events from the agent's OpenCode session."""
        sse_client = SSEClient(
            base_url=Config.OPENCODE_URL,
            password=Config.OPENCODE_PASSWORD,
            global_events=False,
            directory=worktree,
        )

        if not json_mode:
            # Get issue title for display
            cursor = self.db.conn.execute("SELECT title FROM issues WHERE id = ?", (issue_id,))
            title_result = cursor.fetchone()
            issue_title = title_result[0] if title_result else issue_id
            print(f"[{datetime.now().strftime('%H:%M:%S')}] {agent_id} working on {issue_id}: {issue_title}")

        def format_timestamp():
            return datetime.now().strftime("%H:%M:%S")

        def handle_event(event_type: str, properties: dict):
            """Handle incoming SSE events and format them for display."""
            if json_mode:
                print(json.dumps({"timestamp": datetime.now().isoformat(), "event_type": event_type, "properties": properties}))
                return

            # Format events based on type
            timestamp = format_timestamp()

            if event_type == "session.status":
                status = properties.get("status", "unknown")
                if status == "idle":
                    print(f"[{timestamp}] SESSION IDLE — work complete")
                elif status == "active":
                    print(f"[{timestamp}] SESSION ACTIVE — agent working")
                else:
                    print(f"[{timestamp}] SESSION STATUS: {status}")

            elif event_type == "tool.call":
                tool_name = properties.get("name", "unknown")
                if tool_name == "bash":
                    command = properties.get("arguments", {}).get("command", "")
                    print(f"[{timestamp}] bash: {command}")
                elif tool_name == "edit":
                    file_path = properties.get("arguments", {}).get("filePath", "")
                    print(f"[{timestamp}] edit: {file_path}")
                elif tool_name == "write":
                    file_path = properties.get("arguments", {}).get("filePath", "")
                    print(f"[{timestamp}] write: {file_path}")
                elif tool_name == "read":
                    file_path = properties.get("arguments", {}).get("filePath", "")
                    print(f"[{timestamp}] read: {file_path}")
                else:
                    print(f"[{timestamp}] {tool_name}: {json.dumps(properties.get('arguments', {}))}")

            elif event_type == "tool.result":
                tool_name = properties.get("tool_name", "unknown")
                result = properties.get("result", "")
                if isinstance(result, str) and result:
                    # Truncate long results
                    if len(result) > 200:
                        result = result[:200] + "..."
                    # Show first line of result
                    first_line = result.split("\n")[0] if result else ""
                    print(f"[{timestamp}] -> {first_line}")

            elif event_type == "assistant.message":
                message = properties.get("content", "")
                if isinstance(message, str) and message.strip():
                    # Show first line of assistant message
                    first_line = message.split("\n")[0] if message else ""
                    if len(first_line) > 100:
                        first_line = first_line[:100] + "..."
                    print(f"[{timestamp}] text: {first_line}")

            else:
                # Show other events as raw JSON
                print(f"[{timestamp}] {event_type}: {json.dumps(properties)}")

        # Register the event handler
        sse_client.on_all(handle_event)

        try:
            # Connect and stream events
            await sse_client.connect_with_reconnect(max_retries=3, retry_delay=2)
        except KeyboardInterrupt:
            if not json_mode:
                print("\n[CTRL+C] Stopping watch...")
        except Exception as e:
            if json_mode:
                print(json.dumps({"error": str(e)}))
            else:
                print(f"Error: {e}", file=sys.stderr)
        finally:
            sse_client.stop()


def main():
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(description="Hive multi-agent orchestrator")

    # Global options
    parser.add_argument("--db", default="hive.db", help="Database path")
    parser.add_argument("--project", default=".", help="Project directory")
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_mode",
        help="Output JSON (for programmatic use)",
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # create command
    create_parser = subparsers.add_parser("create", help="Create a new issue")
    create_parser.add_argument("title", help="Issue title")
    create_parser.add_argument("description", nargs="?", default="", help="Issue description")
    create_parser.add_argument("--priority", type=int, default=2, help="Priority (0-4)")
    create_parser.add_argument(
        "--type",
        default="task",
        dest="issue_type",
        help="Issue type (task, bug, feature, step, molecule)",
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
        help="Filter by issue type (task, bug, feature, step, molecule)",
    )
    list_parser.add_argument("--assignee", help="Filter by agent assignee")
    list_parser.add_argument("--limit", type=int, default=50, help="Max issues to show (default: 50)")

    # ready command
    subparsers.add_parser("ready", help="Show ready queue")

    # show command
    show_parser = subparsers.add_parser("show", help="Show issue details")
    show_parser.add_argument("issue_id", help="Issue ID")

    # update command
    update_parser = subparsers.add_parser("update", help="Update an issue")
    update_parser.add_argument("issue_id", help="Issue ID")
    update_parser.add_argument("--title", help="New title")
    update_parser.add_argument("--description", help="New description")
    update_parser.add_argument("--priority", type=int, help="New priority (0-4)")
    update_parser.add_argument("--status", help="New status")
    update_parser.add_argument("--model", help="New model")

    # cancel command
    cancel_parser = subparsers.add_parser("cancel", help="Cancel an issue")
    cancel_parser.add_argument("issue_id", help="Issue ID")
    cancel_parser.add_argument("--reason", default="", help="Reason for cancellation")

    # finalize command
    finalize_parser = subparsers.add_parser("finalize", help="Finalize/close an issue")
    finalize_parser.add_argument("issue_id", help="Issue ID")
    finalize_parser.add_argument("--resolution", default="", help="Resolution description")

    # retry command
    retry_parser = subparsers.add_parser("retry", help="Retry a failed/blocked issue")
    retry_parser.add_argument("issue_id", help="Issue ID")
    retry_parser.add_argument("--notes", default="", help="Notes about what to try differently")

    # escalate command
    escalate_parser = subparsers.add_parser("escalate", help="Escalate an issue")
    escalate_parser.add_argument("issue_id", help="Issue ID")
    escalate_parser.add_argument("--reason", default="", help="Reason for escalation")

    # molecule command
    molecule_parser = subparsers.add_parser("molecule", help="Create a multi-step workflow")
    molecule_parser.add_argument("title", help="Molecule title")
    molecule_parser.add_argument("--description", default="", help="Molecule description")
    molecule_parser.add_argument("--steps", required=True, help="Steps as JSON array")
    molecule_parser.add_argument(
        "--model",
        help="Model to use for this molecule (overrides global WORKER_MODEL)",
    )

    # dep command
    dep_parser = subparsers.add_parser("dep", help="Manage dependencies")
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

    # agents command
    agents_parser = subparsers.add_parser("agents", help="List agents")
    agents_parser.add_argument("--status", help="Filter by status (idle, working, stalled, failed)")

    # agent command
    agent_parser = subparsers.add_parser("agent", help="Show agent details")
    agent_parser.add_argument("agent_id", help="Agent ID")

    # events command
    events_parser = subparsers.add_parser("events", help="Show events")
    events_parser.add_argument("--issue", help="Filter by issue ID")
    events_parser.add_argument("--agent", help="Filter by agent ID")
    events_parser.add_argument("--type", dest="event_type", help="Filter by event type")
    events_parser.add_argument("--limit", type=int, default=20, help="Number of events (default: 20)")

    # close command (legacy alias for cancel)
    close_parser = subparsers.add_parser("close", help="Close/cancel an issue (alias for cancel)")
    close_parser.add_argument("issue_id", help="Issue ID")

    # logs command
    logs_parser = subparsers.add_parser("logs", help="Show event log (tail -f style)")
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

    # merges command
    merges_parser = subparsers.add_parser("merges", help="List merge queue entries")
    merges_parser.add_argument("--status", help="Filter by status (queued|running|merged|failed)")

    # costs command
    costs_parser = subparsers.add_parser("costs", help="Show token usage and cost estimates")
    costs_parser.add_argument("--issue", help="Filter by specific issue ID")
    costs_parser.add_argument("--agent", help="Filter by specific agent ID")

    # status command
    subparsers.add_parser("status", help="Show orchestrator status")

    # start command
    start_parser = subparsers.add_parser("start", help="Start the hive daemon")
    start_parser.add_argument(
        "--foreground",
        "-f",
        action="store_true",
        help="Run in foreground instead of daemon mode",
    )

    # stop command
    subparsers.add_parser("stop", help="Stop the hive daemon")

    # daemon command
    daemon_parser = subparsers.add_parser("daemon", help="Manage orchestrator daemon")
    daemon_subparsers = daemon_parser.add_subparsers(dest="daemon_command", help="Daemon command")

    daemon_start = daemon_subparsers.add_parser("start", help="Start daemon")
    daemon_start.add_argument(
        "--foreground",
        "-f",
        action="store_true",
        help="Run in foreground (don't daemonize)",
    )

    daemon_subparsers.add_parser("stop", help="Stop daemon")
    daemon_subparsers.add_parser("restart", help="Restart daemon")
    daemon_subparsers.add_parser("status", help="Show daemon status")

    daemon_logs = daemon_subparsers.add_parser("logs", help="Show daemon logs")
    daemon_logs.add_argument("-f", "--follow", action="store_true", help="Follow log output")
    daemon_logs.add_argument(
        "-n",
        "--lines",
        type=int,
        default=50,
        help="Number of lines to show (default: 50)",
    )

    # queen command
    subparsers.add_parser("queen", help="Launch Queen Bee TUI")

    # watch command
    watch_parser = subparsers.add_parser("watch", help="Stream live events from a worker's OpenCode session")
    watch_parser.add_argument("issue_id", help="Issue ID to watch")

    args = parser.parse_args()

    # Initialize database
    db = Database(args.db)
    db.connect()

    # Create CLI
    cli = HiveCLI(db, args.project)
    json_mode = args.json_mode

    try:
        if args.command == "create":
            cli.create(
                args.title,
                args.description,
                args.priority,
                args.issue_type,
                model=getattr(args, "model", None),
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
                json_mode=json_mode,
            )

        elif args.command == "ready":
            cli.show_ready(json_mode=json_mode)

        elif args.command == "show":
            cli.show(args.issue_id, json_mode=json_mode)

        elif args.command == "update":
            cli.update(
                args.issue_id,
                title=args.title,
                description=args.description,
                priority=args.priority,
                status=args.status,
                model=getattr(args, "model", None),
                json_mode=json_mode,
            )

        elif args.command == "cancel":
            cli.cancel(args.issue_id, reason=args.reason, json_mode=json_mode)

        elif args.command == "finalize":
            cli.finalize(args.issue_id, resolution=args.resolution, json_mode=json_mode)

        elif args.command == "retry":
            cli.retry(args.issue_id, notes=args.notes, json_mode=json_mode)

        elif args.command == "escalate":
            cli.escalate(args.issue_id, reason=args.reason, json_mode=json_mode)

        elif args.command == "molecule":
            cli.molecule(
                args.title,
                description=args.description,
                steps_json=args.steps,
                model=getattr(args, "model", None),
                json_mode=json_mode,
            )

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
            cli.list_agents(status=args.status, json_mode=json_mode)

        elif args.command == "agent":
            cli.show_agent(args.agent_id, json_mode=json_mode)

        elif args.command == "events":
            cli.get_events(
                issue_id=args.issue,
                agent_id=args.agent,
                event_type=args.event_type,
                limit=args.limit,
                json_mode=json_mode,
            )

        elif args.command == "close":
            cli.close(args.issue_id, json_mode=json_mode)

        elif args.command == "logs":
            cli.logs(
                follow=args.follow,
                n=args.lines,
                issue_id=args.issue,
                agent_id=args.agent,
                json_mode=json_mode,
            )

        elif args.command == "merges":
            cli.merges(status=args.status, json_mode=json_mode)

        elif args.command == "costs":
            cli.costs(
                issue_id=args.issue,
                agent_id=args.agent,
                json_mode=json_mode,
            )

        elif args.command == "status":
            cli.status(json_mode=json_mode)

        elif args.command == "start":
            cli.start(foreground=args.foreground, json_mode=json_mode)

        elif args.command == "stop":
            cli.stop(json_mode=json_mode)

        elif args.command == "daemon":
            if args.daemon_command == "start":
                cli.daemon_start(foreground=args.foreground, json_mode=json_mode)
            elif args.daemon_command == "stop":
                cli.daemon_stop(json_mode=json_mode)
            elif args.daemon_command == "restart":
                cli.daemon_restart(json_mode=json_mode)
            elif args.daemon_command == "status":
                cli.daemon_status(json_mode=json_mode)
            elif args.daemon_command == "logs":
                cli.daemon_logs(lines=args.lines, follow=args.follow)
            else:
                daemon_parser.print_help()

        elif args.command == "queen":
            cli.queen()

        elif args.command == "watch":
            cli.watch(args.issue_id, json_mode=json_mode)

        else:
            parser.print_help()

    finally:
        db.close()


if __name__ == "__main__":
    main()
