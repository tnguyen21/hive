"""Notes mixin: note CRUD."""

import logging


logger = logging.getLogger(__name__)


class NotesMixin:
    def add_note(
        self,
        issue_id: str | None = None,
        agent_id: str | None = None,
        content: str = "",
        category: str = "discovery",
        project: str | None = None,
        must_read: bool = False,
    ) -> int:
        """Insert a note and return its row ID."""
        if not self.conn:
            raise RuntimeError("Database not connected")

        cursor = self.conn.execute(
            "INSERT INTO notes (issue_id, agent_id, category, content, project, must_read) VALUES (?, ?, ?, ?, ?, ?)",
            (issue_id, agent_id, category, content, project, 1 if must_read else 0),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_notes(self, issue_id: str | None = None, category: str | None = None, project: str | None = None, limit: int = 20) -> list[dict]:
        """Retrieve notes newest-first. NULL-project notes match any project query (backward compat)."""
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
