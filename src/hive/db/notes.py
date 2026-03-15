"""Notes mixin: note CRUD."""

import logging

from typing import Dict, List, Optional

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
        return self._all(self.conn.execute(query, params))
