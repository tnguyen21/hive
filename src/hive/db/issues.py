"""Issues mixin: create, query, claim, and transition issues."""

import json
import logging

from typing import Any

from ..status import CLOSED_ISSUE_STATUSES, IssueStatus, UNBLOCKING_ISSUE_STATUSES
from ..utils import generate_id
from .core import normalize_tags

logger = logging.getLogger(__name__)

# SQL fragment: "all blocking deps are resolved" — used by get_ready_queue and claim_issue.
_UNBLOCKING_PLACEHOLDERS = ", ".join("?" for _ in UNBLOCKING_ISSUE_STATUSES)
_DEPS_UNBLOCKED_SQL = f"""
    NOT EXISTS (
        SELECT 1 FROM dependencies d
        JOIN issues blocker ON d.depends_on = blocker.id
        WHERE d.issue_id = {{issue_ref}}
          AND d.type = 'blocks'
          AND blocker.status NOT IN ({_UNBLOCKING_PLACEHOLDERS})
    )
"""


class IssuesMixin:
    def try_transition_issue_status(
        self,
        issue_id: str,
        *,
        from_status: IssueStatus | str,
        to_status: IssueStatus | str,
        expected_assignee: str | None = None,
    ) -> bool:
        """CAS-style issue status transition. For transitions to 'open', clears assignee (INV-2)."""
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
        parent_id: str | None = None,
        model: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        depends_on: list[str] | None = None,
    ) -> str:
        """Create a new issue. Dependencies are wired in the same transaction so the issue is never visible to get_ready_queue without its deps."""
        issue_id = generate_id("w")
        metadata_json = json.dumps(metadata) if metadata else None

        # Validate and serialize tags
        tags_json = None
        if tags:
            tags_json = json.dumps(normalize_tags(tags))

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
                conn.executemany(
                    "INSERT OR IGNORE INTO dependencies (issue_id, depends_on, type) VALUES (?, ?, 'blocks')",
                    [(issue_id, dep_id) for dep_id in depends_on],
                )

            self.log_event(issue_id, None, "created", {"title": title}, commit=False)

        return issue_id

    def get_ready_queue(self, project: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        """Return open, unassigned issues with all blocking deps resolved, ordered by priority then creation time."""
        deps_check = _DEPS_UNBLOCKED_SQL.format(issue_ref="i.id")
        query = f"""
            SELECT i.*
            FROM issues i
            WHERE i.status = ?
              AND i.assignee IS NULL
              AND {deps_check}
        """

        params: list[Any] = [IssueStatus.OPEN, *UNBLOCKING_ISSUE_STATUSES]
        if project is not None:
            query += " AND i.project = ?"
            params.append(project)

        query += " ORDER BY i.priority ASC, i.created_at ASC"

        if limit:
            query += f" LIMIT {limit}"

        cursor = self.conn.execute(query, params)
        return self._all(cursor)

    def claim_issue(self, issue_id: str, agent_id: str) -> bool:
        """CAS claim: succeeds only if unclaimed and all blocking deps are resolved. Prevents race with get_ready_queue."""
        deps_check = _DEPS_UNBLOCKED_SQL.format(issue_ref="?")
        with self.transaction() as conn:
            cursor = conn.execute(
                f"""
                UPDATE issues
                SET assignee = ?,
                    status = ?,
                    updated_at = datetime('now')
                WHERE id = ?
                  AND assignee IS NULL
                  AND {deps_check}
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

    def get_issue(self, issue_id: str) -> dict[str, Any] | None:
        """Get issue by ID."""
        cursor = self.conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,))
        return self._one(cursor)

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

    def list_issues(
        self,
        project: str,
        status: str | None = None,
        assignee: str | None = None,
        issue_type: str | None = None,
        exclude_statuses: tuple[str, ...] | None = None,
        sort: str = "priority",
        reverse: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return issues for a project with optional filtering and sorting."""
        _SORT_COLUMNS = {
            "priority": "priority",
            "created": "created_at",
            "updated": "updated_at",
            "status": "status",
            "title": "title",
        }
        query = "SELECT * FROM issues WHERE project = ?"
        params: list[Any] = [project]

        if exclude_statuses:
            placeholders = ",".join("?" for _ in exclude_statuses)
            query += f" AND status NOT IN ({placeholders})"
            params.extend(exclude_statuses)
        elif status:
            query += " AND status = ?"
            params.append(status)
        if assignee:
            query += " AND assignee = ?"
            params.append(assignee)
        if issue_type:
            query += " AND type = ?"
            params.append(issue_type)

        sort_col = _SORT_COLUMNS.get(sort, "priority")
        direction = "DESC" if reverse else "ASC"
        query += f" ORDER BY {sort_col} {direction} LIMIT ?"
        params.append(limit)

        cursor = self.conn.execute(query, params)
        return self._all(cursor)

    def get_review_queue(self, project: str, issue_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """Return issues with their latest merge queue entry for review.

        If *issue_id* is given, returns that single issue (any status) with
        extra columns (description, status).  Otherwise returns done issues.
        """
        if issue_id:
            cursor = self.conn.execute(
                """
                SELECT
                    i.id, i.title, i.status, i.description, i.updated_at, i.assignee,
                    mq.status AS merge_status, mq.branch_name, mq.worktree, mq.enqueued_at
                FROM issues i
                LEFT JOIN merge_queue mq
                    ON mq.id = (SELECT mq2.id FROM merge_queue mq2 WHERE mq2.issue_id = i.id ORDER BY mq2.id DESC LIMIT 1)
                WHERE i.project = ? AND i.id = ?
                """,
                (project, issue_id),
            )
        else:
            cursor = self.conn.execute(
                """
                SELECT
                    i.id, i.title, i.updated_at, i.assignee,
                    mq.status AS merge_status, mq.branch_name, mq.worktree, mq.enqueued_at
                FROM issues i
                LEFT JOIN merge_queue mq
                    ON mq.id = (SELECT mq2.id FROM merge_queue mq2 WHERE mq2.issue_id = i.id ORDER BY mq2.id DESC LIMIT 1)
                WHERE i.project = ? AND i.status = ?
                ORDER BY i.updated_at DESC LIMIT ?
                """,
                (project, IssueStatus.DONE, limit),
            )
        return self._all(cursor)

    def get_dependencies(self, issue_id: str) -> list[dict[str, Any]]:
        """Return issues that *issue_id* depends on (its blockers)."""
        cursor = self.conn.execute(
            """
            SELECT i.id, i.title, i.status
            FROM dependencies d JOIN issues i ON d.depends_on = i.id
            WHERE d.issue_id = ?
            """,
            (issue_id,),
        )
        return self._all(cursor)

    def get_dependents(self, issue_id: str) -> list[dict[str, Any]]:
        """Return issues that are blocked by *issue_id*."""
        cursor = self.conn.execute(
            """
            SELECT i.id, i.title, i.status
            FROM dependencies d JOIN issues i ON d.issue_id = i.id
            WHERE d.depends_on = ?
            """,
            (issue_id,),
        )
        return self._all(cursor)

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
