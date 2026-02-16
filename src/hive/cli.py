"""Human CLI interface for Hive orchestrator."""

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# Set CLI context for logging configuration
os.environ["HIVE_CLI_CONTEXT"] = "1"

from .config import Config
from .daemon import HiveDaemon
from .db import Database, validate_tags
from .utils import detect_project


class HiveCLI:
    """Command-line interface for Hive orchestrator."""

    def __init__(self, db: Database, project_path: str):
        self.db = db
        self.project_path = Path(project_path).resolve()
        self.project_name = self.project_path.name

    def _error(self, msg: str, *, json_mode: bool = False):
        """Print error and exit."""
        if json_mode:
            print(json.dumps({"error": msg}))
        else:
            print(f"Error: {msg}", file=sys.stderr)
        sys.exit(1)

    def _parse_tags(self, issue_dict: dict) -> dict:
        """Parse tags JSON string into a list in-place."""
        if issue_dict.get("tags"):
            try:
                issue_dict["tags"] = json.loads(issue_dict["tags"])
            except (json.JSONDecodeError, TypeError):
                issue_dict["tags"] = []
        else:
            issue_dict["tags"] = []
        return issue_dict

    # Map user-facing sort names to SQL column names
    _SORT_COLUMNS = {
        "priority": "priority",
        "created": "created_at",
        "updated": "updated_at",
        "status": "status",
        "title": "title",
    }

    # ── Issue management ─────────────────────────────────────────────

    def create(
        self,
        title: str,
        description: str = "",
        priority: int = 2,
        issue_type: str = "task",
        model: Optional[str] = None,
        tags: Optional[str] = None,
        depends_on: Optional[list] = None,
        *,
        json_mode: bool = False,
    ):
        """Create a new issue."""
        try:
            tag_list = [t.strip() for t in tags.split(",")] if tags else None
            issue_id = self.db.create_issue(
                title=title,
                description=description,
                priority=priority,
                issue_type=issue_type,
                project=self.project_name,
                model=model,
                tags=tag_list,
            )
            # Wire dependencies immediately so the issue can't be claimed before they exist
            deps_added = []
            if depends_on:
                for dep_id in depends_on:
                    self.db.add_dependency(issue_id, dep_id, "blocks")
                    deps_added.append(dep_id)

            result = {
                "issue_id": issue_id,
                "title": title,
                "status": "open",
                "tags": tag_list or [],
                "depends_on": deps_added,
                "message": f"Created issue {issue_id}: {title}",
            }
        except Exception as e:
            self._error(str(e), json_mode=json_mode)

        if json_mode:
            print(json.dumps(result, default=str))
        else:
            print(f"Created issue: {result['issue_id']}")
            print(f"  Title: {title}")
            print(f"  Priority: {priority}")
            if result.get("tags"):
                print(f"  Tags: {', '.join(result['tags'])}")
            if depends_on:
                print(f"  Depends on: {', '.join(depends_on)}")
        return result.get("issue_id")

    _DONE_STATUSES = ("done", "finalized", "canceled")

    def list_issues(
        self,
        status: Optional[str] = None,
        sort_by: str = "priority",
        reverse: bool = False,
        issue_type: Optional[str] = None,
        assignee: Optional[str] = None,
        limit: int = 50,
        todo: bool = False,
        *,
        json_mode: bool = False,
    ):
        """List all issues."""
        try:
            query = "SELECT * FROM issues WHERE project = ?"
            params: List[Any] = [self.project_name]

            if todo:
                placeholders = ",".join("?" for _ in self._DONE_STATUSES)
                query += f" AND status NOT IN ({placeholders})"
                params.extend(self._DONE_STATUSES)
            elif status:
                query += " AND status = ?"
                params.append(status)
            if assignee:
                query += " AND assignee = ?"
                params.append(assignee)
            if issue_type:
                query += " AND type = ?"
                params.append(issue_type)

            # Resolve sort column (default to priority if unknown)
            sort_col = self._SORT_COLUMNS.get(sort_by, "priority")
            direction = "DESC" if reverse else "ASC"
            query += f" ORDER BY {sort_col} {direction}"

            query += " LIMIT ?"
            params.append(str(limit))

            cursor = self.db.conn.execute(query, params)
            issues = []
            for row in cursor.fetchall():
                issue_dict = dict(row)
                self._parse_tags(issue_dict)
                issues.append(issue_dict)

            result = {"count": len(issues), "issues": issues}
        except Exception as e:
            self._error(str(e), json_mode=json_mode)

        if json_mode:
            print(json.dumps(result, default=str))
        else:
            issues = result.get("issues", [])
            if not issues:
                print("No issues found.")
                print("  Create one with: hive create 'title' 'description'")
                return
            print(f"\n{'ID':<12} {'Status':<12} {'Pri':<4} {'Type':<10} {'Title':<40}")
            print("-" * 80)
            for issue in issues:
                itype = issue.get("type", "")[:10]
                print(f"{issue['id']:<12} {issue['status']:<12} {issue['priority']:<4} {itype:<10} {issue['title'][:40]}")
            print(f"\nTotal: {len(issues)} issues")

    def show(self, issue_id: str, *, json_mode: bool = False):
        """Show issue details and events."""
        try:
            issue = self.db.get_issue(issue_id)
            if not issue:
                raise ValueError(f"Issue not found: {issue_id}")

            # Get dependencies
            cursor = self.db.conn.execute(
                """
                SELECT i.id, i.title, i.status
                FROM dependencies d
                JOIN issues i ON d.depends_on = i.id
                WHERE d.issue_id = ?
                """,
                (issue_id,),
            )
            dependencies = [dict(row) for row in cursor.fetchall()]

            # Get dependents (issues blocked by this one)
            cursor = self.db.conn.execute(
                """
                SELECT i.id, i.title, i.status
                FROM dependencies d
                JOIN issues i ON d.issue_id = i.id
                WHERE d.depends_on = ?
                """,
                (issue_id,),
            )
            dependents = [dict(row) for row in cursor.fetchall()]

            # Get recent events
            events = self.db.get_events(issue_id=issue_id, limit=10)

            # Parse tags from JSON
            issue_dict = dict(issue)
            self._parse_tags(issue_dict)

            result = {
                "issue": issue_dict,
                "dependencies": dependencies,
                "dependents": dependents,
                "recent_events": events,
            }
        except Exception as e:
            self._error(str(e), json_mode=json_mode)

        if json_mode:
            print(json.dumps(result, default=str))
        else:
            issue = result["issue"]
            print(f"\nIssue: {issue['id']}")
            print(f"Title: {issue['title']}")
            print(f"Status: {issue['status']}")
            print(f"Priority: {issue['priority']}")
            print(f"Type: {issue['type']}")
            print(f"Assignee: {issue['assignee'] or 'None'}")
            if issue.get("tags"):
                print(f"Tags: {', '.join(issue['tags'])}")
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

    def update(
        self,
        issue_id: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
        priority: Optional[int] = None,
        status: Optional[str] = None,
        model: Optional[str] = None,
        tags: Optional[str] = None,
        *,
        json_mode: bool = False,
    ):
        """Update an issue."""
        try:
            issue = self.db.get_issue(issue_id)
            if not issue:
                raise ValueError(f"Issue not found: {issue_id}")

            tag_list = [t.strip() for t in tags.split(",")] if tags is not None else None

            updates = []
            params = []

            if title is not None:
                updates.append("title = ?")
                params.append(title)
            if description is not None:
                updates.append("description = ?")
                params.append(description)
            if priority is not None:
                updates.append("priority = ?")
                params.append(priority)
            if status is not None:
                updates.append("status = ?")
                params.append(status)
            if model is not None:
                updates.append("model = ?")
                params.append(model)
            if tag_list is not None:
                validated_tags = validate_tags(tag_list)
                updates.append("tags = ?")
                params.append(json.dumps(validated_tags))

            if updates:
                query = f"UPDATE issues SET {', '.join(updates)}, updated_at = datetime('now') WHERE id = ?"
                params.append(issue_id)
                self.db.conn.execute(query, params)
                self.db.conn.commit()

                self.db.log_event(
                    issue_id,
                    None,
                    "updated",
                    {
                        "fields": [
                            k
                            for k, v in [
                                ("title", title),
                                ("description", description),
                                ("priority", priority),
                                ("status", status),
                                ("model", model),
                            ]
                            if v is not None
                        ]
                    },
                )

            result = {"issue_id": issue_id, "message": f"Updated issue {issue_id}"}
        except Exception as e:
            self._error(str(e), json_mode=json_mode)

        if json_mode:
            print(json.dumps(result, default=str))
        else:
            print(result.get("message", f"Updated issue {issue_id}"))

    def cancel(self, issue_id: str, reason: str = "", *, json_mode: bool = False):
        """Cancel an issue."""
        try:
            issue = self.db.get_issue(issue_id)
            if not issue:
                raise ValueError(f"Issue not found: {issue_id}")

            self.db.update_issue_status(issue_id, "canceled")
            self.db.log_event(issue_id, None, "canceled", {"reason": reason})

            result = {
                "issue_id": issue_id,
                "status": "canceled",
                "reason": reason,
                "message": f"Canceled issue {issue_id}",
            }
        except Exception as e:
            self._error(str(e), json_mode=json_mode)

        if json_mode:
            print(json.dumps(result, default=str))
        else:
            print(result.get("message", f"Canceled issue {issue_id}"))

    def finalize(self, issue_id: str, resolution: str = "", *, json_mode: bool = False):
        """Finalize/close an issue."""
        try:
            issue = self.db.get_issue(issue_id)
            if not issue:
                raise ValueError(f"Issue not found: {issue_id}")

            self.db.update_issue_status(issue_id, "finalized")
            # If this issue was sitting in the merge queue (manual review mode),
            # mark those entries complete so they don't get processed later.
            self.db.conn.execute(
                """
                UPDATE merge_queue
                SET status = 'merged', completed_at = datetime('now')
                WHERE issue_id = ? AND status IN ('queued', 'running')
                """,
                (issue_id,),
            )
            self.db.conn.commit()
            self.db.log_event(issue_id, None, "finalized", {"resolution": resolution})

            result = {
                "issue_id": issue_id,
                "status": "finalized",
                "resolution": resolution,
                "message": f"Finalized issue {issue_id}",
            }
        except Exception as e:
            self._error(str(e), json_mode=json_mode)

        if json_mode:
            print(json.dumps(result, default=str))
        else:
            print(result.get("message", f"Finalized issue {issue_id}"))

    def review(self, limit: int = 20, *, json_mode: bool = False):
        """List done issues that are pending finalization with review hints."""
        try:
            cursor = self.db.conn.execute(
                """
                SELECT
                    i.id,
                    i.title,
                    i.updated_at,
                    i.assignee,
                    mq.status AS merge_status,
                    mq.branch_name,
                    mq.worktree,
                    mq.enqueued_at
                FROM issues i
                LEFT JOIN merge_queue mq
                    ON mq.id = (
                        SELECT mq2.id
                        FROM merge_queue mq2
                        WHERE mq2.issue_id = i.id
                        ORDER BY mq2.id DESC
                        LIMIT 1
                    )
                WHERE i.project = ? AND i.status = 'done'
                ORDER BY i.updated_at DESC
                LIMIT ?
                """,
                (self.project_name, limit),
            )

            rows = []
            project_q = shlex.quote(str(self.project_path))
            for row in cursor.fetchall():
                item = dict(row)
                branch = item.get("branch_name")
                worktree = item.get("worktree")
                issue_id = item["id"]

                item["diff_hint"] = None
                item["merge_hint"] = None
                item["finalize_hint"] = f'hive finalize {issue_id} --resolution "manual review complete"'

                if branch:
                    branch_q = shlex.quote(branch)
                    item["diff_hint"] = f"git -C {project_q} diff main...{branch_q}"
                    item["merge_hint"] = f"git -C {project_q} merge --ff-only {branch_q}"
                if worktree:
                    item["worktree_hint"] = f"git -C {shlex.quote(worktree)} log --oneline -n 5"
                else:
                    item["worktree_hint"] = None

                rows.append(item)

            result = {"count": len(rows), "review": rows}
        except Exception as e:
            self._error(str(e), json_mode=json_mode)

        if json_mode:
            print(json.dumps(result, default=str))
            return

        rows = result["review"]
        if not rows:
            print("No done issues pending review.")
            return

        print(f"\n{'Issue':<14} {'Merge':<10} {'Assignee':<16} {'Title':<40}")
        print("-" * 88)
        for item in rows:
            merge_state = item.get("merge_status") or "-"
            assignee = item.get("assignee") or "-"
            print(f"{item['id']:<14} {merge_state:<10} {assignee:<16} {item['title'][:40]}")

        print(f"\nTotal: {len(rows)} issue(s) pending finalization")
        print("\nPer-issue review commands:")
        for item in rows:
            print(f"\n{item['id']}:")
            if item.get("diff_hint"):
                print(f"  Diff:     {item['diff_hint']}")
            if item.get("worktree_hint"):
                print(f"  Worktree: {item['worktree_hint']}")
            if item.get("merge_hint"):
                print(f"  Merge:    {item['merge_hint']}")
            print(f"  Finalize: {item['finalize_hint']}")

    def retry(self, issue_id: str, notes: str = "", *, json_mode: bool = False):
        """Retry a failed/blocked issue."""
        try:
            issue = self.db.get_issue(issue_id)
            if not issue:
                raise ValueError(f"Issue not found: {issue_id}")

            # Reset to open and unassign
            self.db.conn.execute(
                """
                UPDATE issues
                SET status = 'open', assignee = NULL, updated_at = datetime('now')
                WHERE id = ?
                """,
                (issue_id,),
            )
            self.db.conn.commit()

            self.db.log_event(issue_id, None, "manual_retry", {"notes": notes})

            result = {
                "issue_id": issue_id,
                "status": "open",
                "notes": notes,
                "message": f"Reset issue {issue_id} to 'open' for retry",
            }
        except Exception as e:
            self._error(str(e), json_mode=json_mode)

        if json_mode:
            print(json.dumps(result, default=str))
        else:
            print(result.get("message", f"Retrying issue {issue_id}"))

    def epic(
        self,
        title: str,
        description: str = "",
        steps_json: str = "[]",
        model: Optional[str] = None,
        tags: Optional[str] = None,
        *,
        json_mode: bool = False,
    ):
        """Create a epic (multi-step workflow)."""
        try:
            steps = json.loads(steps_json)
        except json.JSONDecodeError as e:
            if json_mode:
                print(json.dumps({"error": f"Invalid steps JSON: {e}"}))
            else:
                print(f"Error: Invalid steps JSON: {e}", file=sys.stderr)
            sys.exit(1)

        try:
            tag_list = [t.strip() for t in tags.split(",")] if tags else None

            # Create parent epic issue
            parent_id = self.db.create_issue(
                title=title,
                description=description,
                issue_type="epic",
                project=self.project_name,
                tags=tag_list,
            )

            # Map of step indices to issue IDs
            step_map: Dict[int, str] = {}
            created_steps = []

            # Create all step issues
            for i, step in enumerate(steps):
                step_id = self.db.create_issue(
                    title=step["title"],
                    description=step.get("description", ""),
                    priority=step.get("priority", 2),
                    issue_type="step",
                    project=self.project_name,
                    parent_id=parent_id,
                    model=model,
                )
                step_map[i] = step_id
                created_steps.append({"index": i, "id": step_id, "title": step["title"]})

            # Wire up dependencies
            for i, step in enumerate(steps):
                needs = step.get("needs", [])
                for dep_idx in needs:
                    if isinstance(dep_idx, int) and dep_idx in step_map:
                        self.db.add_dependency(step_map[i], step_map[dep_idx], "blocks")

            result = {
                "epic_id": parent_id,
                "title": title,
                "steps_count": len(steps),
                "steps": created_steps,
                "message": f"Created epic {parent_id} with {len(steps)} steps",
            }
        except Exception as e:
            self._error(str(e), json_mode=json_mode)

        if json_mode:
            print(json.dumps(result, default=str))
        else:
            print(result.get("message", f"Created epic {result.get('epic_id', '')}"))
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
        try:
            # Verify both issues exist
            if not self.db.get_issue(issue_id):
                raise ValueError(f"Issue not found: {issue_id}")
            if not self.db.get_issue(depends_on):
                raise ValueError(f"Dependency not found: {depends_on}")

            self.db.add_dependency(issue_id, depends_on, dep_type)

            result = {
                "issue_id": issue_id,
                "depends_on": depends_on,
                "type": dep_type,
                "message": f"Added {dep_type} dependency: {issue_id} depends on {depends_on}",
            }
        except Exception as e:
            self._error(str(e), json_mode=json_mode)

        if json_mode:
            print(json.dumps(result, default=str))
        else:
            print(result.get("message", "Added dependency"))

    def dep_remove(self, issue_id: str, depends_on: str, *, json_mode: bool = False):
        """Remove a dependency between issues."""
        try:
            self.db.conn.execute(
                "DELETE FROM dependencies WHERE issue_id = ? AND depends_on = ?",
                (issue_id, depends_on),
            )
            self.db.conn.commit()

            result = {
                "issue_id": issue_id,
                "depends_on": depends_on,
                "message": f"Removed dependency: {issue_id} no longer depends on {depends_on}",
            }
        except Exception as e:
            self._error(str(e), json_mode=json_mode)

        if json_mode:
            print(json.dumps(result, default=str))
        else:
            print(result.get("message", "Removed dependency"))

    def merges(self, status: Optional[str] = None, *, json_mode: bool = False):
        """List merge queue entries."""
        query = "SELECT mq.*, i.title as issue_title, a.name as agent_name FROM merge_queue mq JOIN issues i ON mq.issue_id = i.id LEFT JOIN agents a ON mq.agent_id = a.id WHERE i.project = ?"
        params = [self.project_name]
        if status:
            query += " AND mq.status = ?"
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
        try:
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
            active_agents = self.db.get_active_agents(project=self.project_name)

            # Get ready queue
            ready = self.db.get_ready_queue(limit=10)

            # Get merge queue stats
            merge_stats = self.db.get_merge_queue_stats()

            # Get daemon status
            daemon = self._make_daemon()
            daemon_status = daemon.status()

            result = {
                "project": self.project_name,
                "issues": status_counts,
                "total_issues": sum(status_counts.values()),
                "active_agents": len(active_agents),
                "ready_queue": len(ready),
                "merge_queue": merge_stats,
                "ready_issues": [{"id": i["id"], "title": i["title"]} for i in ready[:5]],
                "daemon": {
                    "running": daemon_status.get("running", False),
                    "pid": daemon_status.get("pid"),
                    "log_file": daemon_status.get("log_file"),
                },
            }
        except Exception as e:
            self._error(str(e), json_mode=json_mode)

        if json_mode:
            print(json.dumps(result, default=str))
        else:
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

            # Daemon info
            daemon_info = result.get("daemon", {})
            if daemon_info.get("running"):
                print(f"\nDaemon: running (PID {daemon_info.get('pid')})")
                if daemon_info.get("log_file"):
                    print(f"  Log: {daemon_info.get('log_file')}")
            else:
                print("\nDaemon: not running")

            total = result.get("total_issues", 0)
            if total == 0:
                print("\n  No issues yet. Create one with: hive create 'title' 'description'")

    def list_agents(self, agent_id: Optional[str] = None, status: Optional[str] = None, *, json_mode: bool = False):
        """List agents, or show details for a specific agent if agent_id is provided."""
        # If agent_id is provided, show that agent's details
        if agent_id:
            try:
                cursor = self.db.conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,))
                row = cursor.fetchone()
                if not row:
                    raise ValueError(f"Agent not found: {agent_id}")

                agent = dict(row)

                # Get current issue details
                if agent.get("current_issue"):
                    issue = self.db.get_issue(agent["current_issue"])
                    agent["current_issue_details"] = issue

                # Get recent events for this agent
                agent["recent_events"] = self.db.get_events(agent_id=agent_id, limit=10)

                result = agent
            except Exception as e:
                self._error(str(e), json_mode=json_mode)

            if json_mode:
                print(json.dumps(result, default=str))
            else:
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
            return

        # Otherwise, list all agents
        try:
            query = "SELECT * FROM agents WHERE project = ?"
            params = [self.project_name]

            if status:
                query += " AND status = ?"
                params.append(status)

            query += " ORDER BY created_at DESC"

            cursor = self.db.conn.execute(query, params)
            agents = [dict(row) for row in cursor.fetchall()]

            # Enrich with current issue info
            for agent in agents:
                if agent.get("current_issue"):
                    issue = self.db.get_issue(agent["current_issue"])
                    if issue:
                        agent["current_issue_title"] = issue.get("title", "unknown")

            result = {"count": len(agents), "agents": agents}
        except Exception as e:
            self._error(str(e), json_mode=json_mode)

        if json_mode:
            print(json.dumps(result, default=str))
        else:
            agents = result.get("agents", [])
            if not agents:
                print("No agents found.")
                return
            print(f"\n{'ID':<16} {'Name':<16} {'Status':<10} {'Current Issue':<30}")
            print("-" * 72)
            for agent in agents:
                issue_title = agent.get("current_issue_title", agent.get("current_issue", "")) or "-"
                print(f"{agent['id']:<16} {agent['name']:<16} {agent['status']:<10} {str(issue_title)[:30]}")

    # ── Notes ─────────────────────────────────────────────────────────

    def add_note(
        self,
        content: str,
        issue_id: Optional[str] = None,
        category: str = "discovery",
        *,
        json_mode: bool = False,
    ):
        """Add a note to the knowledge base."""
        try:
            note_id = self.db.add_note(agent_id=None, issue_id=issue_id, content=content, category=category, project=self.project_name)

            result = {
                "note_id": note_id,
                "content": content,
                "category": category,
                "issue_id": issue_id,
                "message": f"Added note #{note_id}",
            }
        except Exception as e:
            self._error(str(e), json_mode=json_mode)

        if json_mode:
            print(json.dumps(result, default=str))
        else:
            print(f"Added note #{result['note_id']} [{category}]")

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
        event_type: Optional[str] = None,
        daemon: bool = False,
        *,
        json_mode: bool = False,
    ):
        """Show event log, optionally tailing for new events."""
        # If --daemon flag is set, show daemon logs instead of event logs
        if daemon:
            daemon_obj = self._make_daemon()
            daemon_obj.logs(lines=n, follow=follow)
            return

        recent = self.db.get_recent_events(n=n, issue_id=issue_id, agent_id=agent_id, event_type=event_type)

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
                        new_events = self.db.get_events_since(after_id=cursor, issue_id=issue_id, agent_id=agent_id, event_type=event_type)
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
                new_events = self.db.get_events_since(after_id=cursor, issue_id=issue_id, agent_id=agent_id, event_type=event_type)
                for event in new_events:
                    print(self._format_event(event))
                    cursor = event["id"]
        except KeyboardInterrupt:
            pass

    def doctor(self, fix: bool = False, *, json_mode: bool = False):
        """Run system health checks."""
        from .doctor import run_all_checks

        results = run_all_checks(self.db)

        # Apply fixes if requested
        fixed_checks = []
        if fix:
            for result in results:
                if result.status in ("fail", "warn") and result.fix is not None:
                    try:
                        result.fix(self.db)
                        fixed_checks.append(result.id)
                    except Exception as e:
                        if json_mode:
                            print(json.dumps({"error": f"Failed to fix {result.id}: {e}"}), file=sys.stderr)
                        else:
                            print(f"Error fixing {result.id}: {e}", file=sys.stderr)

        if json_mode:
            output = [
                {
                    "id": r.id,
                    "status": r.status,
                    "description": r.description,
                    "details": r.details,
                    "fixable": r.fix is not None,
                    "fixed": r.id in fixed_checks,
                }
                for r in results
            ]
            print(json.dumps(output, indent=2))
            return

        # Table output
        print(f"\n{'ID':<8} {'Status':<8} {'Description':<60}")
        print("-" * 76)
        for result in results:
            status_display = result.status.upper()
            description = result.description[:60]
            if result.id in fixed_checks:
                description += " [FIXED]"
            print(f"{result.id:<8} {status_display:<8} {description}")

        # Summary
        failures = sum(1 for r in results if r.status == "fail")
        warnings = sum(1 for r in results if r.status == "warn")

        print()
        if fix and fixed_checks:
            print(f"Fixed {len(fixed_checks)} check(s): {', '.join(fixed_checks)}")
        if failures > 0 or warnings > 0:
            parts = []
            if failures > 0:
                parts.append(f"{failures} failure(s)")
            if warnings > 0:
                parts.append(f"{warnings} warning(s)")
            print(f"Summary: {', '.join(parts)}")
        else:
            print("Summary: All checks passed")

    def metrics(self, model=None, tag=None, issue_type=None, group_by=None, show_costs=False, issue_id=None, agent_id=None, json_mode=False):
        """Show aggregated agent run metrics."""
        # If --group-by is specified, use the stats-style output
        if group_by:
            results = self.db.get_model_performance(model=model, tag=tag, group_by=group_by)
            if json_mode:
                print(json.dumps(results, default=str))
                return

            if not results:
                print("No performance data yet.")
                return

            group_label = "Tag" if group_by == "tag" else "Type"
            group_key = "tag" if group_by == "tag" else "type"
            print(f"{'Model':<35} {group_label:<15} {'Issues':>6} {'OK':>4} {'Fail':>4} {'Retries':>7} {'Avg Min':>8}")
            print("-" * 85)
            for r in results:
                model_name = (r.get("model") or "unknown")[:34]
                group_val = str(r.get(group_key, ""))[:14]
                print(
                    f"{model_name:<35} {group_val:<15} {r.get('issue_count', 0):>6} {r.get('successes', 0):>4} {r.get('failures', 0):>4} {r.get('total_retries', 0):>7} {r.get('avg_duration_minutes', 0):>8}"
                )
            return

        # If --costs is specified, show token usage data
        if show_costs:
            usage = self.db.get_token_usage(issue_id=issue_id, agent_id=agent_id, project=self.project_name)

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
            return

        # Default metrics output
        results = self.db.get_metrics(model=model, tag=tag, issue_type=issue_type, project=self.project_name)

        if json_mode:
            # Calculate summary stats for JSON output
            total_runs = sum(r["runs"] for r in results)
            total_escalations = sum(r["escalated_count"] for r in results)
            escalation_rate = round(100.0 * total_escalations / total_runs, 1) if total_runs > 0 else 0

            # Calculate mean time to resolution (weighted average by run count)
            total_duration_weighted = sum(r["avg_duration_s"] * r["runs"] for r in results if r["avg_duration_s"])
            mean_duration_s = total_duration_weighted / total_runs if total_runs > 0 else 0
            mean_duration_m = round(mean_duration_s / 60, 1)

            output = {
                "metrics": results,
                "summary": {
                    "escalation_rate": escalation_rate,
                    "mean_time_to_resolution_minutes": mean_duration_m,
                    "total_runs": total_runs,
                },
            }
            print(json.dumps(output, default=str))
            return

        if not results:
            print("No metrics data yet.")
            return

        # Format duration in minutes for display
        for r in results:
            if r["avg_duration_s"]:
                r["avg_duration_m"] = round(r["avg_duration_s"] / 60, 1)
            else:
                r["avg_duration_m"] = 0

        print(f"{'Model':<35} {'Runs':>5} {'Success%':>9} {'Avg Duration':>12} {'Avg Retries':>12} {'Merge Health':>12}")
        print("-" * 95)
        for r in results:
            model_name = (r.get("model") or "unknown")[:34]
            success_pct = f"{r.get('success_rate', 0):.1f}%"
            avg_dur = f"{r.get('avg_duration_m', 0):.1f}m"
            avg_ret = f"{r.get('avg_retries', 0):.1f}"
            merge_health = f"{r.get('merge_health', 0):.1f}%" if r.get("merge_health") is not None else "N/A"
            print(f"{model_name:<35} {r.get('runs', 0):>5} {success_pct:>9} {avg_dur:>12} {avg_ret:>12} {merge_health:>12}")

        # Summary line
        total_runs = sum(r["runs"] for r in results)
        total_escalations = sum(r["escalated_count"] for r in results)
        escalation_rate = round(100.0 * total_escalations / total_runs, 1) if total_runs > 0 else 0

        # Calculate mean time to resolution (weighted average by run count)
        total_duration_weighted = sum(r["avg_duration_s"] * r["runs"] for r in results if r["avg_duration_s"])
        mean_duration_s = total_duration_weighted / total_runs if total_runs > 0 else 0
        mean_duration_m = round(mean_duration_s / 60, 1)

        print()
        print(f"Escalation rate: {escalation_rate}% | Mean time to resolution: {mean_duration_m}m")

    # ── Daemon management ────────────────────────────────────────────

    def _make_daemon(self) -> HiveDaemon:
        return HiveDaemon(self.project_name, str(self.project_path))

    def start(self, foreground: bool = False, *, json_mode: bool = False):
        """Start the hive daemon."""
        if foreground:
            from .daemon import run_daemon_foreground

            run_daemon_foreground(self.db, str(self.project_path), self.project_name)
            return

        daemon = self._make_daemon()
        status = daemon.status()
        if status["running"]:
            if json_mode:
                print(json.dumps({"status": "already_running", "pid": status["pid"]}))
            else:
                print(f"Hive daemon already running (PID {status['pid']})")
            return

        started = daemon.start(db_path=self.db.db_path)
        if started:
            ds = daemon.status()
            if json_mode:
                print(json.dumps({"status": "started", "pid": ds["pid"], "log_file": ds.get("log_file")}))
            else:
                print(f"Hive daemon started (PID {ds['pid']})")
                print(f"  Log: {ds.get('log_file')}")
        else:
            log_tail = ""
            try:
                if daemon.log_file.exists():
                    lines = daemon.log_file.read_text().strip().splitlines()
                    log_tail = "\n".join(lines[-10:])
            except OSError:
                pass
            if json_mode:
                print(json.dumps({"error": "Failed to start daemon", "log_file": str(daemon.log_file), "log_tail": log_tail}))
                sys.exit(1)
            else:
                print(f"Failed to start daemon. Log: {daemon.log_file}")
                if log_tail:
                    print(f"\nLast output:\n{log_tail}")

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

    # ── Queen Bee TUI ─────────────────────────────────────────────────

    def queen(self, *, backend: str | None = None):
        """Launch Queen Bee TUI using the configured backend."""
        daemon = self._make_daemon()
        daemon_status = daemon.status()
        if not daemon_status["running"]:
            print("Starting daemon... ", end="", flush=True)
            daemon.start(db_path=self.db.db_path)
            daemon_status = daemon.status()
            if daemon_status["running"]:
                print(f"done (PID {daemon_status['pid']})")
            else:
                print("failed")
                self._error("Failed to start daemon. Check `hive daemon logs`.")

        effective = backend or Config.BACKEND
        if effective == "claude":
            self._queen_claude()
        else:
            self._queen_opencode()

    _OPENCODE_QUEEN_FRONTMATTER = """\
---
description: Strategic coordinator for Hive multi-agent orchestration
mode: primary
tools:
  write: true
  edit: true
permission:
  bash:
    "hive *": allow
    "git *": allow
    "ls *": allow
    "find *": allow
    "rg *": allow
  read: allow
---

"""

    def _queen_write_opencode_agent(self):
        """Generate .opencode/agents/queen.md from source template."""
        from .prompts import _load_template

        queen_prompt = _load_template("queen")
        agents_dir = self.project_path / ".opencode" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        agent_file = agents_dir / "queen.md"
        agent_file.write_text(self._OPENCODE_QUEEN_FRONTMATTER + queen_prompt)

    def _queen_opencode(self):
        """Launch Queen Bee via OpenCode TUI."""
        self._queen_write_opencode_agent()

        opencode_cmd = os.environ.get("OPENCODE_CMD", "opencode")
        cmd = [
            opencode_cmd,
            "attach",
            Config.OPENCODE_URL,
            "--dir",
            str(self.project_path),
        ]

        print("Launching Queen Bee TUI (OpenCode)...\n")
        os.execvp(cmd[0], cmd)

    # Sentinel markers for the Queen identity block in CLAUDE.md
    _QUEEN_SENTINEL_START = "<!-- HIVE-QUEEN-SESSION-START -->"
    _QUEEN_SENTINEL_END = "<!-- HIVE-QUEEN-SESSION-END -->"

    def _queen_write_identity_files(self) -> tuple[Path, Path]:
        """Write Queen identity files for compaction persistence.

        Returns (claude_md_path, instructions_path) for cleanup.
        """
        from .prompts import _load_template

        queen_prompt = _load_template("queen")

        # Write full instructions to .hive/ so Queen can re-read after compaction
        hive_dir = self.project_path / ".hive"
        hive_dir.mkdir(exist_ok=True)
        instructions_path = hive_dir / "queen-instructions.md"
        instructions_path.write_text(queen_prompt)

        # Write condensed identity anchor to .claude/CLAUDE.md (re-read every turn)
        claude_dir = self.project_path / ".claude"
        claude_dir.mkdir(exist_ok=True)
        claude_md = claude_dir / "CLAUDE.md"

        queen_block = (
            f"\n{self._QUEEN_SENTINEL_START}\n"
            "# HIVE QUEEN BEE — ACTIVE SESSION\n"
            "You are the Queen Bee coordinator. You do NOT write code — you plan, decompose, and monitor.\n"
            "Full instructions: `.hive/queen-instructions.md` — re-read if your context feels incomplete.\n"
            "Operational state: `.hive/queen-state.md` — re-read to recall what you were working on.\n"
            "Always use `hive --json` for CLI commands. The daemon runs in background.\n"
            f"{self._QUEEN_SENTINEL_END}\n"
        )

        existing = claude_md.read_text() if claude_md.exists() else ""
        if self._QUEEN_SENTINEL_START not in existing:
            claude_md.write_text(existing + queen_block)

        return claude_md, instructions_path

    def _queen_cleanup_identity_files(self, claude_md: Path, instructions_path: Path):
        """Remove Queen identity files written for the session."""
        # Strip queen block from CLAUDE.md
        if claude_md.exists():
            content = claude_md.read_text()
            start = content.find(self._QUEEN_SENTINEL_START)
            if start != -1:
                end = content.find(self._QUEEN_SENTINEL_END)
                if end != -1:
                    end += len(self._QUEEN_SENTINEL_END)
                    # Consume trailing newline if present
                    if end < len(content) and content[end] == "\n":
                        end += 1
                    cleaned = (content[:start] + content[end:]).rstrip("\n")
                    if cleaned.strip():
                        claude_md.write_text(cleaned + "\n")
                    else:
                        claude_md.unlink()

        # Remove instructions file
        instructions_path.unlink(missing_ok=True)

        # Remove state file (ephemeral per-session)
        state_file = self.project_path / ".hive" / "queen-state.md"
        state_file.unlink(missing_ok=True)

    def _queen_claude(self):
        """Launch Queen Bee as an interactive Claude CLI session."""
        # Claude CLI refuses to launch inside another Claude Code session.
        # Since the queen is a top-level interactive session (not nested), clear the guard.
        os.environ.pop("CLAUDECODE", None)

        # Write identity files for compaction persistence
        claude_md, instructions_path = self._queen_write_identity_files()

        # Short system prompt — full instructions are in the file
        short_prompt = "You are the Hive Queen Bee coordinator. Read .hive/queen-instructions.md for your full instructions now."

        claude_cmd = os.environ.get("CLAUDE_CMD", "claude")
        cmd = [
            claude_cmd,
            "--model",
            Config.DEFAULT_MODEL,
            "--append-system-prompt",
            short_prompt,
            "--allowedTools",
            "Bash(hive:*) Bash(git:*) Bash(ls:*) Bash(find:*) Bash(rg:*) Read Edit Write",
        ]

        print("Launching Queen Bee TUI (Claude CLI)...\n")

        # Use subprocess.run (not os.execvp) so we can clean up identity files on exit
        try:
            result = subprocess.run(cmd)
            sys.exit(result.returncode)
        except KeyboardInterrupt:
            pass
        finally:
            self._queen_cleanup_identity_files(claude_md, instructions_path)


def _do_setup(project_path: Path, project_name: str, *, json_mode: bool = False):
    """Write a default .hive.toml if one doesn't exist."""
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
        help="Issue type (task, bug, feature, step, epic)",
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
        help="Filter by issue type (task, bug, feature, step, epic)",
    )
    list_parser.add_argument("--todo", action="store_true", help="Show only actionable issues (excludes done/finalized/canceled)")
    list_parser.add_argument("--assignee", help="Filter by agent assignee")
    list_parser.add_argument("--limit", type=int, default=50, help="Max issues to show (default: 50)")

    # show command
    show_parser = subparsers.add_parser("show", help="Show issue details")
    show_parser.add_argument("issue_id", help="Issue ID")

    # review command
    review_parser = subparsers.add_parser("review", help="Review done issues before finalizing")
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
    retry_parser = subparsers.add_parser("retry", help="Retry a failed issue")
    retry_parser.add_argument("issue_id", help="Issue ID")
    retry_parser.add_argument("--notes", default="", help="Notes about what to try differently")

    # epic command (hidden — advanced)
    epic_parser = subparsers.add_parser("epic", help="Create a multi-step epic")
    epic_parser.add_argument("title", help="Epic title")
    epic_parser.add_argument("--description", default="", help="Epic description")
    epic_parser.add_argument("--steps", required=True, help="Steps as JSON array")
    epic_parser.add_argument(
        "--model",
        help="Model to use for this epic (overrides global WORKER_MODEL)",
    )
    epic_parser.add_argument("--tags", type=str, help="Comma-separated tags (e.g. refactor,python,small)")

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
        choices=["opencode", "claude"],
        default=None,
        help="Override backend (default: from config/HIVE_BACKEND)",
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

    # doctor command
    doctor_parser = subparsers.add_parser("doctor", help="Run system health checks")
    doctor_parser.add_argument("--fix", action="store_true", help="Auto-fix issues where possible")

    args = parser.parse_args()

    # ── Project auto-detection + layered config ──────────────────────
    if args.project:
        project_path = Path(args.project).resolve()
        project_name = project_path.name
    else:
        project_path, project_name = detect_project()

    # Load layered config: defaults → ~/.hive/config.toml → .hive.toml → env
    Config.load(project_root=project_path)

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
            cli.show(args.issue_id, json_mode=json_mode)

        elif args.command == "review":
            cli.review(limit=args.limit, json_mode=json_mode)

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

        elif args.command == "epic":
            cli.epic(
                args.title,
                description=args.description,
                steps_json=args.steps,
                model=getattr(args, "model", None),
                tags=getattr(args, "tags", None),
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
            cli.queen(backend=args.backend)

        elif args.command == "note":
            cli.add_note(
                args.content,
                issue_id=args.issue_id,
                category=args.category,
                json_mode=json_mode,
            )

        elif args.command == "doctor":
            cli.doctor(fix=getattr(args, "fix", False), json_mode=json_mode)

        else:
            parser.print_help()

    finally:
        db.close()


if __name__ == "__main__":
    main()
