"""Notes mixin: note CRUD and delivery tracking."""

import logging

from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class NotesMixin:
    def add_note(
        self,
        issue_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        content: str = "",
        category: str = "discovery",
        project: Optional[str] = None,
        must_read: bool = False,
    ) -> int:
        """
        Insert a note and return its ID.

        Args:
            issue_id: Which issue the note was discovered during. None = project-wide note.
            agent_id: Which agent wrote it. None = Queen-authored or system note.
            content: The note text. Short — typically 1-3 sentences.
            category: One of 'discovery', 'gotcha', 'dependency', 'pattern', 'context'.
            project: Project identifier. If None and issue_id provided, backfilled via migration.
            must_read: If True, recipients must acknowledge this note before proceeding.

        Returns:
            The ID of the inserted note
        """
        if not self.conn:
            raise RuntimeError("Database not connected")

        cursor = self.conn.execute(
            "INSERT INTO notes (issue_id, agent_id, category, content, project, must_read) VALUES (?, ?, ?, ?, ?, ?)",
            (issue_id, agent_id, category, content, project, 1 if must_read else 0),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_notes(
        self, issue_id: Optional[str] = None, category: Optional[str] = None, project: Optional[str] = None, limit: int = 20
    ) -> List[Dict]:
        """
        Retrieve notes with optional filtering. Returns newest first.

        Args:
            issue_id: Filter by specific issue ID (optional)
            category: Filter by specific category (optional)
            project: Filter by project (optional). NULL-project notes match any query for backward compat.
            limit: Maximum number of notes to return

        Returns:
            List of note dicts, ordered by newest first
        """
        if not self.conn:
            raise RuntimeError("Database not connected")

        query = "SELECT * FROM notes WHERE 1=1"
        params = []
        if issue_id is not None:
            query += " AND issue_id = ?"
            params.append(issue_id)
        if category is not None:
            query += " AND category = ?"
            params.append(category)
        if project is not None:
            query += " AND (project = ? OR project IS NULL)"
            params.append(project)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def create_note_deliveries(
        self,
        note_id: int,
        to_agents: Optional[List[str]] = None,
        to_issues: Optional[List[str]] = None,
    ) -> int:
        """
        Create delivery rows for a note.

        For each agent in to_agents: inserts an agent-global delivery (recipient_issue_id=NULL).
        For each issue in to_issues: inserts an issue-following target row (recipient_agent_id=NULL).
        Uses INSERT OR IGNORE to deduplicate based on unique indexes.

        Returns:
            Count of rows actually inserted.
        """
        if not self.conn:
            raise RuntimeError("Database not connected")

        inserted = 0
        with self.transaction() as conn:
            for agent_id in to_agents or []:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO note_deliveries (note_id, recipient_agent_id, recipient_issue_id) VALUES (?, ?, NULL)",
                    (note_id, agent_id),
                )
                inserted += cursor.rowcount
            for issue_id in to_issues or []:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO note_deliveries (note_id, recipient_agent_id, recipient_issue_id) VALUES (?, NULL, ?)",
                    (note_id, issue_id),
                )
                inserted += cursor.rowcount
        return inserted

    def materialize_issue_deliveries(self, issue_id: str, agent_id: str, project: str) -> int:
        """
        Materialize issue-following target rows into concrete agent deliveries.

        Finds all note_deliveries rows with recipient_issue_id=issue_id and
        recipient_agent_id IS NULL (issue-following targets), then inserts a
        concrete delivery row for the given agent+issue pair for each.

        Returns:
            Count of rows actually inserted.
        """
        if not self.conn:
            raise RuntimeError("Database not connected")

        rows = self.conn.execute(
            "SELECT note_id FROM note_deliveries WHERE recipient_issue_id = ? AND recipient_agent_id IS NULL",
            (issue_id,),
        ).fetchall()

        inserted = 0
        with self.transaction() as conn:
            for row in rows:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO note_deliveries (note_id, recipient_agent_id, recipient_issue_id) VALUES (?, ?, ?)",
                    (row["note_id"], agent_id, issue_id),
                )
                inserted += cursor.rowcount
        return inserted

    def get_injectable_deliveries(
        self,
        agent_id: str,
        issue_id: str,
        project: str,
        max_normal: int = 5,
    ) -> Tuple[List[Dict[str, Any]], bool]:
        """
        Query deliveries eligible for worker turn injection.

        Includes:
        - must_read notes: while status != 'acked'
        - normal notes: while status IN ('queued', 'delivered')

        Ordering: must_read DESC, queued before delivered, then created_at ASC.
        Caps normal deliveries at max_normal; always includes all must_read.

        Returns:
            (deliveries_list, has_more_bool)
        """
        if not self.conn:
            raise RuntimeError("Database not connected")

        rows = self.conn.execute(
            """
            SELECT
                nd.id AS delivery_id,
                nd.note_id,
                n.content,
                n.must_read,
                nd.status,
                n.agent_id AS from_agent_id,
                CASE WHEN nd.recipient_issue_id IS NULL THEN 'agent' ELSE 'issue' END AS scope,
                nd.recipient_issue_id
            FROM note_deliveries nd
            JOIN notes n ON nd.note_id = n.id
            WHERE nd.recipient_agent_id = ?
              AND (nd.recipient_issue_id IS NULL OR nd.recipient_issue_id = ?)
              AND (
                (n.must_read = 1 AND nd.status != 'acked')
                OR
                (n.must_read = 0 AND nd.status IN ('queued', 'delivered'))
              )
            ORDER BY
                n.must_read DESC,
                CASE nd.status WHEN 'queued' THEN 0 WHEN 'delivered' THEN 1 ELSE 2 END ASC,
                nd.created_at ASC
            """,
            (agent_id, issue_id),
        ).fetchall()

        must_read_rows = [dict(r) for r in rows if r["must_read"]]
        normal_rows = [dict(r) for r in rows if not r["must_read"]]

        has_more = len(normal_rows) > max_normal
        selected = must_read_rows + normal_rows[:max_normal]
        return selected, has_more

    def mark_delivery_delivered(self, delivery_id: int) -> bool:
        """
        Transition a delivery from 'queued' to 'delivered'.

        Returns True if the row was updated (i.e., it was in 'queued' state).
        """
        if not self.conn:
            raise RuntimeError("Database not connected")

        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE note_deliveries SET status = 'delivered', delivered_at = datetime('now') WHERE id = ? AND status = 'queued'",
                (delivery_id,),
            )
        return cursor.rowcount == 1

    def mark_delivery_read(self, delivery_id: int, agent_id: str) -> bool:
        """
        Transition a delivery to 'read'. Only valid if status is 'queued' or 'delivered'.

        Returns True if the row was updated.
        """
        if not self.conn:
            raise RuntimeError("Database not connected")

        with self.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE note_deliveries
                SET status = 'read', read_at = datetime('now')
                WHERE id = ? AND recipient_agent_id = ? AND status IN ('queued', 'delivered')
                """,
                (delivery_id, agent_id),
            )
        return cursor.rowcount == 1

    def mark_delivery_acked(self, delivery_id: int, agent_id: str) -> bool:
        """
        Acknowledge a must_read delivery. Only valid for notes with must_read=1.

        Returns True if the row was updated. Returns False if the note is not must_read.
        """
        if not self.conn:
            raise RuntimeError("Database not connected")

        with self.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE note_deliveries
                SET status = 'acked', acked_at = datetime('now')
                WHERE id = ?
                  AND recipient_agent_id = ?
                  AND note_id IN (SELECT id FROM notes WHERE must_read = 1)
                """,
                (delivery_id, agent_id),
            )
        return cursor.rowcount == 1

    def get_required_unacked_deliveries(self, agent_id: str, issue_id: str) -> List[Dict[str, Any]]:
        """
        Return all must_read deliveries that have not been acked for the given agent/issue.

        Covers both agent-global (recipient_issue_id IS NULL) and issue-scoped deliveries.

        Returns:
            List of dicts with delivery_id, note_id, content, status.
        """
        if not self.conn:
            raise RuntimeError("Database not connected")

        rows = self.conn.execute(
            """
            SELECT
                nd.id AS delivery_id,
                nd.note_id,
                n.content,
                nd.status
            FROM note_deliveries nd
            JOIN notes n ON nd.note_id = n.id
            WHERE n.must_read = 1
              AND nd.status != 'acked'
              AND nd.recipient_agent_id = ?
              AND (nd.recipient_issue_id IS NULL OR nd.recipient_issue_id = ?)
            ORDER BY nd.created_at ASC
            """,
            (agent_id, issue_id),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_inbox_deliveries(
        self,
        agent_id: str,
        issue_id: Optional[str] = None,
        unread_only: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Query deliveries for CLI hive mail inbox.

        Args:
            agent_id: Filter deliveries for this agent.
            issue_id: If provided, further filter by issue scope.
            unread_only: If True, exclude status='read' and status='acked'.

        Returns:
            List of delivery dicts ordered by created_at DESC.
        """
        if not self.conn:
            raise RuntimeError("Database not connected")

        query = """
            SELECT
                nd.*,
                n.content,
                n.must_read,
                n.agent_id AS from_agent_id
            FROM note_deliveries nd
            JOIN notes n ON nd.note_id = n.id
            WHERE nd.recipient_agent_id = ?
        """
        params: List[Any] = [agent_id]

        if issue_id is not None:
            query += " AND nd.recipient_issue_id = ?"
            params.append(issue_id)

        if unread_only:
            query += " AND nd.status NOT IN ('read', 'acked')"

        query += " ORDER BY nd.created_at DESC"

        rows = self.conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
