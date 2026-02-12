"""Tool handlers for Hive orchestrator operations.

The ToolExecutor class provides all database operations used by the CLI.
"""

from typing import Any, Dict, List, Optional

from .db import Database


class ToolExecutor:
    """Executes tool calls against the Hive database and orchestrator state."""

    def __init__(self, db: Database, project_name: str):
        """
        Initialize tool executor.

        Args:
            db: Database instance
            project_name: Current project name
        """
        self.db = db
        self.project_name = project_name

    def execute(self, tool_name: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute a tool call and return the result.

        Args:
            tool_name: Name of the tool to execute
            params: Parameters for the tool

        Returns:
            Dict with either 'result' or 'error' key
        """
        try:
            handler = getattr(self, f"handle_{tool_name}", None)
            if not handler:
                return {"error": f"Unknown tool: {tool_name}"}

            result = handler(**params)
            return {"result": result}
        except Exception as e:
            return {"error": str(e)}

    def handle_hive_create_issue(
        self,
        title: str,
        description: str = "",
        priority: int = 2,
        type: str = "task",
        project: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new issue."""
        issue_id = self.db.create_issue(
            title=title,
            description=description,
            priority=priority,
            issue_type=type,
            project=project or self.project_name,
        )
        return {
            "issue_id": issue_id,
            "title": title,
            "status": "open",
            "message": f"Created issue {issue_id}: {title}",
        }

    def handle_hive_list_issues(
        self,
        status: Optional[str] = None,
        assignee: Optional[str] = None,
        priority: Optional[int] = None,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """List issues with filtering."""
        query = "SELECT * FROM issues WHERE project = ?"
        params: List[Any] = [self.project_name]

        if status:
            query += " AND status = ?"
            params.append(status)
        if assignee:
            query += " AND assignee = ?"
            params.append(assignee)
        if priority is not None:
            query += " AND priority = ?"
            params.append(priority)

        query += " ORDER BY priority, created_at LIMIT ?"
        params.append(str(limit))

        cursor = self.db.conn.execute(query, params)
        issues = [dict(row) for row in cursor.fetchall()]

        return {"count": len(issues), "issues": issues}

    def handle_hive_get_issue(self, issue_id: str) -> Dict[str, Any]:
        """Get detailed issue information."""
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

        return {
            "issue": issue,
            "dependencies": dependencies,
            "dependents": dependents,
            "recent_events": events,
        }

    def handle_hive_update_issue(
        self,
        issue_id: str,
        title: Optional[str] = None,
        description: Optional[str] = None,
        priority: Optional[int] = None,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Update issue fields."""
        issue = self.db.get_issue(issue_id)
        if not issue:
            raise ValueError(f"Issue not found: {issue_id}")

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
                        ]
                        if v is not None
                    ]
                },
            )

        return {"issue_id": issue_id, "message": f"Updated issue {issue_id}"}

    def handle_hive_create_molecule(
        self,
        title: str,
        steps: List[Dict[str, Any]],
        description: str = "",
        project: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a multi-step molecule workflow."""
        # Create parent molecule issue
        parent_id = self.db.create_issue(
            title=title,
            description=description,
            issue_type="molecule",
            project=project or self.project_name,
        )

        # Map of step indices to issue IDs
        step_map = {}
        created_steps = []

        # Create all step issues
        for i, step in enumerate(steps):
            step_id = self.db.create_issue(
                title=step["title"],
                description=step.get("description", ""),
                priority=step.get("priority", 2),
                issue_type="step",
                project=project or self.project_name,
                parent_id=parent_id,
            )
            step_map[i] = step_id
            created_steps.append({"index": i, "id": step_id, "title": step["title"]})

        # Wire up dependencies
        for i, step in enumerate(steps):
            needs = step.get("needs", [])
            for dep_idx in needs:
                if isinstance(dep_idx, int) and dep_idx in step_map:
                    self.db.add_dependency(step_map[i], step_map[dep_idx], "blocks")

        return {
            "molecule_id": parent_id,
            "title": title,
            "steps_count": len(steps),
            "steps": created_steps,
            "message": f"Created molecule {parent_id} with {len(steps)} steps",
        }

    def handle_hive_add_dependency(self, issue_id: str, depends_on: str, type: str = "blocks") -> Dict[str, Any]:
        """Add a dependency between issues."""
        # Verify both issues exist
        if not self.db.get_issue(issue_id):
            raise ValueError(f"Issue not found: {issue_id}")
        if not self.db.get_issue(depends_on):
            raise ValueError(f"Dependency not found: {depends_on}")

        self.db.add_dependency(issue_id, depends_on, type)
        return {
            "issue_id": issue_id,
            "depends_on": depends_on,
            "type": type,
            "message": f"Added {type} dependency: {issue_id} depends on {depends_on}",
        }

    def handle_hive_remove_dependency(self, issue_id: str, depends_on: str) -> Dict[str, Any]:
        """Remove a dependency."""
        self.db.conn.execute(
            "DELETE FROM dependencies WHERE issue_id = ? AND depends_on = ?",
            (issue_id, depends_on),
        )
        self.db.conn.commit()

        return {
            "issue_id": issue_id,
            "depends_on": depends_on,
            "message": f"Removed dependency: {issue_id} no longer depends on {depends_on}",
        }

    def handle_hive_cancel_issue(self, issue_id: str, reason: str = "") -> Dict[str, Any]:
        """Cancel an issue."""
        issue = self.db.get_issue(issue_id)
        if not issue:
            raise ValueError(f"Issue not found: {issue_id}")

        self.db.update_issue_status(issue_id, "canceled")
        self.db.log_event(issue_id, None, "canceled", {"reason": reason})

        return {
            "issue_id": issue_id,
            "status": "canceled",
            "reason": reason,
            "message": f"Canceled issue {issue_id}",
        }

    def handle_hive_retry_issue(self, issue_id: str, notes: str = "") -> Dict[str, Any]:
        """Retry a failed/blocked issue."""
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

        self.db.log_event(issue_id, None, "retry", {"notes": notes})

        return {
            "issue_id": issue_id,
            "status": "open",
            "notes": notes,
            "message": f"Reset issue {issue_id} to 'open' for retry",
        }

    def handle_hive_escalate_issue(self, issue_id: str, reason: str) -> Dict[str, Any]:
        """Escalate an issue."""
        issue = self.db.get_issue(issue_id)
        if not issue:
            raise ValueError(f"Issue not found: {issue_id}")

        self.db.update_issue_status(issue_id, "escalated")
        self.db.log_event(issue_id, None, "escalated", {"reason": reason})

        return {
            "issue_id": issue_id,
            "status": "escalated",
            "reason": reason,
            "message": f"Escalated issue {issue_id}: {reason}",
        }

    def handle_hive_close_issue(self, issue_id: str, resolution: str = "") -> Dict[str, Any]:
        """Close/finalize an issue."""
        issue = self.db.get_issue(issue_id)
        if not issue:
            raise ValueError(f"Issue not found: {issue_id}")

        self.db.update_issue_status(issue_id, "finalized")
        self.db.log_event(issue_id, None, "finalized", {"resolution": resolution})

        return {
            "issue_id": issue_id,
            "status": "finalized",
            "resolution": resolution,
            "message": f"Finalized issue {issue_id}",
        }

    def handle_hive_get_status(self) -> Dict[str, Any]:
        """Get overall system status."""
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

        # Get ready queue
        ready = self.db.get_ready_queue(limit=10)

        # Get merge queue stats
        merge_stats = self.db.get_merge_queue_stats()

        return {
            "project": self.project_name,
            "issues": status_counts,
            "total_issues": sum(status_counts.values()),
            "active_agents": len(active_agents),
            "ready_queue": len(ready),
            "merge_queue": merge_stats,
            "ready_issues": [{"id": i["id"], "title": i["title"]} for i in ready[:5]],
        }

    def handle_hive_list_agents(self, status: Optional[str] = None) -> Dict[str, Any]:
        """List agents."""
        query = "SELECT * FROM agents"
        params = []

        if status:
            query += " WHERE status = ?"
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

        return {"count": len(agents), "agents": agents}

    def handle_hive_get_agent(self, agent_id: str) -> Dict[str, Any]:
        """Get detailed agent information."""
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

        return agent

    def handle_hive_show_ready(self, limit: int = 20) -> Dict[str, Any]:
        """Show ready queue."""
        ready = self.db.get_ready_queue(limit=limit)

        return {
            "count": len(ready),
            "ready_issues": [
                {
                    "id": i["id"],
                    "title": i["title"],
                    "priority": i["priority"],
                    "type": i["type"],
                }
                for i in ready
            ],
        }

    def handle_hive_get_events(
        self,
        issue_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        event_type: Optional[str] = None,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """Get events."""
        events = self.db.get_events(issue_id=issue_id, agent_id=agent_id, event_type=event_type, limit=limit)

        return {"count": len(events), "events": events}
