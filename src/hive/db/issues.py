"""Issues mixin: create, query, claim, and transition issues."""

import json
import logging

from typing import Any, Dict, List, Optional

from ..status import CLOSED_ISSUE_STATUSES, IssueStatus, UNBLOCKING_ISSUE_STATUSES
from ..utils import generate_id
from .core import validate_tags

logger = logging.getLogger(__name__)


class IssuesMixin:
    def try_transition_issue_status(
        self,
        issue_id: str,
        *,
        from_status: IssueStatus | str,
        to_status: IssueStatus | str,
        expected_assignee: Optional[str] = None,
    ) -> bool:
        """CAS-style issue status transition.

        For transitions to 'open', clears assignee (INV-2).

        Returns:
            True if updated, False otherwise.
        """
        from_status_value = str(from_status)
        to_status_value = str(to_status)
        closed_status_placeholders = ", ".join("?" for _ in CLOSED_ISSUE_STATUSES)
        with self.transaction() as conn:
            cursor = conn.execute(
                f"""
                UPDATE issues
                SET status = ?,
                    assignee = CASE WHEN ? = ? THEN NULL ELSE assignee END,
                    updated_at = datetime('now'),
                    closed_at = CASE WHEN ? IN ({closed_status_placeholders})
                                     THEN datetime('now')
                                     ELSE closed_at END
                WHERE id = ?
                  AND status = ?
                  AND (? IS NULL OR assignee = ?)
                """,
                (
                    to_status_value,
                    to_status_value,
                    IssueStatus.OPEN,
                    to_status_value,
                    *CLOSED_ISSUE_STATUSES,
                    issue_id,
                    from_status_value,
                    expected_assignee,
                    expected_assignee,
                ),
            )
            if cursor.rowcount != 1:
                return False

            # commit=False so the status transition + audit event commit atomically via
            # the surrounding `transaction()` context manager.
            self.log_event(
                issue_id,
                None,
                f"status_{to_status_value}",
                {"status": to_status_value, "from": from_status_value, "to": to_status_value},
                commit=False,
            )
            return True

    def create_issue(
        self,
        title: str,
        description: str = "",
        priority: int = 2,
        issue_type: str = "task",
        project: str = "",
        parent_id: Optional[str] = None,
        model: Optional[str] = None,
        tags: Optional[list[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        depends_on: Optional[list[str]] = None,
    ) -> str:
        """
        Create a new issue.

        Dependencies are wired in the same transaction as the INSERT so
        the issue is never visible to get_ready_queue without its deps.

        Args:
            title: Issue title
            description: Issue description
            priority: Priority (0=critical, 4=low)
            issue_type: Type (task, bug, feature, step, epic)
            project: Project/repo name
            parent_id: Parent issue ID (for epics)
            model: Model to use for this issue (overrides global WORKER_MODEL)
            tags: List of tags for the issue (validated against ALLOWED_TAGS)
            metadata: Additional metadata dict
            depends_on: List of issue IDs this issue depends on (blocks type)

        Returns:
            Generated issue ID
        """
        issue_id = generate_id("w")
        metadata_json = json.dumps(metadata) if metadata else None

        # Validate and serialize tags
        tags_json = None
        if tags:
            validated_tags = validate_tags(tags)
            tags_json = json.dumps(validated_tags)

        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO issues (id, title, description, priority, type, project, parent_id, model, tags, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    issue_id,
                    title,
                    description,
                    priority,
                    issue_type,
                    project,
                    parent_id,
                    model,
                    tags_json,
                    metadata_json,
                ),
            )

            # Wire deps in the same transaction — the issue is never visible
            # to get_ready_queue without its blocking dependencies.
            if depends_on:
                for dep_id in depends_on:
                    conn.execute(
                        "INSERT OR IGNORE INTO dependencies (issue_id, depends_on, type) VALUES (?, ?, 'blocks')",
                        (issue_id, dep_id),
                    )

            self.log_event(issue_id, None, "created", {"title": title}, commit=False)

        return issue_id

    def get_ready_queue(self, project: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Query for ready work items.

        Returns issues that are:
        - status = 'open'
        - assignee IS NULL
        - All blocking dependencies are resolved (done/finalized/canceled)

        Args:
            project: Filter by project (optional)
            limit: Maximum number of items to return

        Returns:
            List of issue dicts, ordered by priority then creation time
        """
        unblocking_placeholders = ", ".join("?" for _ in UNBLOCKING_ISSUE_STATUSES)
        query = f"""
            SELECT i.*
            FROM issues i
            WHERE i.status = ?
              AND i.assignee IS NULL
              AND NOT EXISTS (
                SELECT 1 FROM dependencies d
                JOIN issues blocker ON d.depends_on = blocker.id
                WHERE d.issue_id = i.id
                  AND d.type = 'blocks'
                  AND blocker.status NOT IN ({unblocking_placeholders})
              )
        """

        params: List[Any] = [IssueStatus.OPEN, *UNBLOCKING_ISSUE_STATUSES]
        if project is not None:
            query += " AND i.project = ?"
            params.append(project)

        query += " ORDER BY i.priority ASC, i.created_at ASC"

        if limit:
            query += f" LIMIT {limit}"

        cursor = self.conn.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    def claim_issue(self, issue_id: str, agent_id: str) -> bool:
        """
        Atomically claim an issue using CAS (Compare-And-Set).

        Only succeeds if the issue is unclaimed AND all blocking dependencies
        are resolved. This prevents a race where the orchestrator grabs an issue
        from the ready queue before its dependencies have been wired.

        Args:
            issue_id: ID of issue to claim
            agent_id: ID of agent claiming the issue

        Returns:
            True if claim successful, False if already claimed or blocked
        """
        with self.transaction() as conn:
            cursor = conn.execute(
                f"""
                UPDATE issues
                SET assignee = ?,
                    status = ?,
                    updated_at = datetime('now')
                WHERE id = ?
                  AND assignee IS NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM dependencies d
                    JOIN issues blocker ON d.depends_on = blocker.id
                    WHERE d.issue_id = ?
                      AND d.type = 'blocks'
                      AND blocker.status NOT IN ({", ".join("?" for _ in UNBLOCKING_ISSUE_STATUSES)})
                  )
                """,
                (
                    agent_id,
                    IssueStatus.IN_PROGRESS,
                    issue_id,
                    issue_id,
                    *UNBLOCKING_ISSUE_STATUSES,
                ),
            )

            success = cursor.rowcount == 1

            if success:
                # Update agent's current issue
                conn.execute(
                    """
                    UPDATE agents
                    SET current_issue = ?,
                        status = 'working',
                        updated_at = datetime('now')
                    WHERE id = ?
                    """,
                    (issue_id, agent_id),
                )
                self.log_event(issue_id, agent_id, "claimed", {}, commit=False)

            return success

    def get_issue(self, issue_id: str) -> Optional[Dict[str, Any]]:
        """Get issue by ID."""
        cursor = self.conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def update_issue_status(self, issue_id: str, status: IssueStatus | str):
        """Update issue status. Clears assignee when setting to 'open' (INV-2)."""
        status_value = str(status)
        closed_status_placeholders = ", ".join("?" for _ in CLOSED_ISSUE_STATUSES)
        with self.transaction() as conn:
            conn.execute(
                f"""
                UPDATE issues
                SET status = ?,
                    assignee = CASE WHEN ? = ? THEN NULL ELSE assignee END,
                    updated_at = datetime('now'),
                    closed_at = CASE WHEN ? IN ({closed_status_placeholders})
                                     THEN datetime('now')
                                     ELSE closed_at END
                WHERE id = ?
                """,
                (status_value, status_value, IssueStatus.OPEN, status_value, *CLOSED_ISSUE_STATUSES, issue_id),
            )
            self.log_event(issue_id, None, f"status_{status_value}", {"status": status_value}, commit=False)

    def add_dependency(self, issue_id: str, depends_on: str, dep_type: str = "blocks"):
        """Add a dependency between issues."""
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO dependencies (issue_id, depends_on, type)
                VALUES (?, ?, ?)
                """,
                (issue_id, depends_on, dep_type),
            )
