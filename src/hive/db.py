"""SQLite database layer for Hive orchestrator."""

import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from .ids import generate_id


# SQL schema definition
SCHEMA = """
-- WAL mode for concurrent reads during writes
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

----------------------------------------------------------------------
-- ISSUES: the universal work unit
----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS issues (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    description TEXT,
    status      TEXT NOT NULL DEFAULT 'open',
    priority    INTEGER NOT NULL DEFAULT 2,
    type        TEXT NOT NULL DEFAULT 'task',
    assignee    TEXT,
    parent_id   TEXT REFERENCES issues(id),
    project     TEXT,
    metadata    TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_issues_status ON issues(status);
CREATE INDEX IF NOT EXISTS idx_issues_assignee ON issues(assignee);
CREATE INDEX IF NOT EXISTS idx_issues_parent ON issues(parent_id);
CREATE INDEX IF NOT EXISTS idx_issues_project ON issues(project);

----------------------------------------------------------------------
-- DEPENDENCIES: edges in the work DAG
----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dependencies (
    issue_id    TEXT NOT NULL REFERENCES issues(id),
    depends_on  TEXT NOT NULL REFERENCES issues(id),
    type        TEXT NOT NULL DEFAULT 'blocks',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (issue_id, depends_on)
);

----------------------------------------------------------------------
-- AGENTS: persistent identity layer
----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agents (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'idle',
    session_id  TEXT,
    worktree    TEXT,
    current_issue TEXT REFERENCES issues(id),
    model       TEXT,
    lease_expires_at TEXT,
    last_progress_at TEXT,
    metadata    TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

----------------------------------------------------------------------
-- EVENTS: append-only audit trail
----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id    TEXT REFERENCES issues(id),
    agent_id    TEXT REFERENCES agents(id),
    event_type  TEXT NOT NULL,
    detail      TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_events_issue ON events(issue_id);
CREATE INDEX IF NOT EXISTS idx_events_agent ON events(agent_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);

----------------------------------------------------------------------
-- MERGE_QUEUE: dedicated finalizer queue
----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS merge_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id    TEXT NOT NULL REFERENCES issues(id),
    agent_id    TEXT REFERENCES agents(id),
    project     TEXT NOT NULL,
    worktree    TEXT NOT NULL,
    branch_name TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued',
    enqueued_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_mq_status ON merge_queue(status);
CREATE INDEX IF NOT EXISTS idx_mq_project ON merge_queue(project);

----------------------------------------------------------------------
-- LABELS: denormalized tags for fast filtering
----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS labels (
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    label       TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (entity_type, entity_id, label)
);
"""


class Database:
    """SQLite database wrapper for Hive orchestrator."""

    def __init__(self, db_path: str = "hive.db"):
        """
        Initialize database connection.

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        self.conn: Optional[sqlite3.Connection] = None

    def connect(self):
        """Open database connection and initialize schema."""
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self):
        """Close database connection."""
        if self.conn:
            self.conn.close()
            self.conn = None

    @contextmanager
    def transaction(self):
        """Context manager for database transactions."""
        if not self.conn:
            raise RuntimeError("Database not connected")
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def create_issue(
        self,
        title: str,
        description: str = "",
        priority: int = 2,
        issue_type: str = "task",
        project: str = "",
        parent_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Create a new issue.

        Args:
            title: Issue title
            description: Issue description
            priority: Priority (0=critical, 4=low)
            issue_type: Type (task, bug, feature, step, molecule)
            project: Project/repo name
            parent_id: Parent issue ID (for molecules)
            metadata: Additional metadata dict

        Returns:
            Generated issue ID
        """
        issue_id = generate_id("w")
        metadata_json = json.dumps(metadata) if metadata else None

        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO issues (id, title, description, priority, type, project, parent_id, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    issue_id,
                    title,
                    description,
                    priority,
                    issue_type,
                    project,
                    parent_id,
                    metadata_json,
                ),
            )
            self.log_event(issue_id, None, "created", {"title": title})

        return issue_id

    def get_ready_queue(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Query for ready work items.

        Returns issues that are:
        - status = 'open'
        - assignee IS NULL
        - All blocking dependencies are resolved (done/finalized/canceled)

        Args:
            limit: Maximum number of items to return

        Returns:
            List of issue dicts, ordered by priority then creation time
        """
        query = """
            SELECT i.*
            FROM issues i
            WHERE i.status = 'open'
              AND i.assignee IS NULL
              AND i.type != 'molecule'
              AND NOT EXISTS (
                SELECT 1 FROM dependencies d
                JOIN issues blocker ON d.depends_on = blocker.id
                WHERE d.issue_id = i.id
                  AND d.type = 'blocks'
                  AND blocker.status NOT IN ('done', 'finalized', 'canceled')
              )
            ORDER BY i.priority ASC, i.created_at ASC
        """

        if limit:
            query += f" LIMIT {limit}"

        cursor = self.conn.execute(query)
        return [dict(row) for row in cursor.fetchall()]

    def claim_issue(self, issue_id: str, agent_id: str) -> bool:
        """
        Atomically claim an issue using CAS (Compare-And-Set).

        Args:
            issue_id: ID of issue to claim
            agent_id: ID of agent claiming the issue

        Returns:
            True if claim successful, False if already claimed
        """
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE issues
                SET assignee = ?,
                    status = 'in_progress',
                    updated_at = datetime('now')
                WHERE id = ?
                  AND assignee IS NULL
                """,
                (agent_id, issue_id),
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
                self.log_event(issue_id, agent_id, "claimed", {})

            return success

    def log_event(
        self,
        issue_id: Optional[str],
        agent_id: Optional[str],
        event_type: str,
        detail: Optional[Dict[str, Any]] = None,
    ):
        """
        Log an event to the audit trail.

        Args:
            issue_id: Related issue ID (optional)
            agent_id: Related agent ID (optional)
            event_type: Type of event (created, claimed, done, etc.)
            detail: Additional event details dict
        """
        detail_json = json.dumps(detail) if detail else None

        if not self.conn:
            raise RuntimeError("Database not connected")

        self.conn.execute(
            """
            INSERT INTO events (issue_id, agent_id, event_type, detail)
            VALUES (?, ?, ?, ?)
            """,
            (issue_id, agent_id, event_type, detail_json),
        )
        self.conn.commit()

    def create_agent(
        self,
        name: str,
        model: str = "claude-sonnet-4-5-20250929",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Create a new agent identity.

        Args:
            name: Human-readable agent name
            model: Model identifier
            metadata: Additional metadata dict

        Returns:
            Generated agent ID
        """
        agent_id = generate_id("agent")
        metadata_json = json.dumps(metadata) if metadata else None

        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO agents (id, name, model, metadata)
                VALUES (?, ?, ?, ?)
                """,
                (agent_id, name, model, metadata_json),
            )

        return agent_id

    def get_issue(self, issue_id: str) -> Optional[Dict[str, Any]]:
        """Get issue by ID."""
        cursor = self.conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_agent(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Get agent by ID."""
        cursor = self.conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def update_issue_status(self, issue_id: str, status: str):
        """Update issue status."""
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE issues
                SET status = ?,
                    updated_at = datetime('now'),
                    closed_at = CASE WHEN ? IN ('done', 'finalized', 'canceled', 'failed')
                                     THEN datetime('now')
                                     ELSE closed_at END
                WHERE id = ?
                """,
                (status, status, issue_id),
            )
            self.log_event(issue_id, None, f"status_{status}", {"status": status})

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

    def get_events(
        self,
        issue_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        event_type: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Get events filtered by issue, agent, or type."""
        conditions = []
        params = []

        if issue_id:
            conditions.append("issue_id = ?")
            params.append(issue_id)
        if agent_id:
            conditions.append("agent_id = ?")
            params.append(agent_id)
        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)

        if conditions:
            where_clause = " AND ".join(conditions)
            query = f"SELECT * FROM events WHERE {where_clause} ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
        else:
            query = "SELECT * FROM events ORDER BY created_at DESC LIMIT ?"
            params.append(limit)

        cursor = self.conn.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    def get_events_since(
        self,
        after_id: int = 0,
        issue_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get events with id > after_id, ordered ascending (oldest first)."""
        if issue_id:
            cursor = self.conn.execute(
                "SELECT * FROM events WHERE id > ? AND issue_id = ? ORDER BY id ASC",
                (after_id, issue_id),
            )
        elif agent_id:
            cursor = self.conn.execute(
                "SELECT * FROM events WHERE id > ? AND agent_id = ? ORDER BY id ASC",
                (after_id, agent_id),
            )
        else:
            cursor = self.conn.execute(
                "SELECT * FROM events WHERE id > ? ORDER BY id ASC",
                (after_id,),
            )
        return [dict(row) for row in cursor.fetchall()]

    def get_max_event_id(self) -> int:
        """Return the current maximum event id, or 0 if no events."""
        cursor = self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM events")
        return cursor.fetchone()[0]

    def get_recent_events(
        self,
        n: int = 20,
        issue_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get the most recent n events, returned oldest-first."""
        if issue_id:
            cursor = self.conn.execute(
                "SELECT * FROM events WHERE issue_id = ? ORDER BY id DESC LIMIT ?",
                (issue_id, n),
            )
        elif agent_id:
            cursor = self.conn.execute(
                "SELECT * FROM events WHERE agent_id = ? ORDER BY id DESC LIMIT ?",
                (agent_id, n),
            )
        else:
            cursor = self.conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?",
                (n,),
            )
        rows = [dict(row) for row in cursor.fetchall()]
        rows.reverse()
        return rows

    def get_next_ready_step(self, parent_id: str) -> Optional[Dict[str, Any]]:
        """
        Get the next ready step within a molecule.

        Args:
            parent_id: Parent molecule issue ID

        Returns:
            Next ready step issue dict, or None if no steps are ready
        """
        cursor = self.conn.execute(
            """
            SELECT i.*
            FROM issues i
            WHERE i.parent_id = ?
              AND i.status = 'open'
              AND NOT EXISTS (
                SELECT 1 FROM dependencies d
                JOIN issues blocker ON d.depends_on = blocker.id
                WHERE d.issue_id = i.id
                  AND d.type = 'blocks'
                  AND blocker.status NOT IN ('done', 'finalized', 'canceled')
              )
            ORDER BY i.created_at ASC
            LIMIT 1
            """,
            (parent_id,),
        )

        row = cursor.fetchone()
        return dict(row) if row else None

    def count_active_agents(self) -> int:
        """
        Count currently active agents.

        Returns:
            Number of agents with status 'working'
        """
        cursor = self.conn.execute(
            "SELECT COUNT(*) FROM agents WHERE status = 'working'"
        )
        return cursor.fetchone()[0]

    def get_active_agents(self) -> List[Dict[str, Any]]:
        """
        Get all currently active agents.

        Returns:
            List of active agent dicts
        """
        cursor = self.conn.execute(
            """
            SELECT * FROM agents
            WHERE status = 'working'
            ORDER BY created_at ASC
            """
        )
        return [dict(row) for row in cursor.fetchall()]

    # --- Merge Queue Methods ---

    def get_queued_merges(self, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Get queued merge queue entries, oldest first.

        Args:
            limit: Maximum entries to return

        Returns:
            List of merge queue entry dicts with joined issue/agent info
        """
        cursor = self.conn.execute(
            """
            SELECT mq.*, i.title as issue_title, a.name as agent_name
            FROM merge_queue mq
            JOIN issues i ON mq.issue_id = i.id
            LEFT JOIN agents a ON mq.agent_id = a.id
            WHERE mq.status = 'queued'
            ORDER BY mq.enqueued_at ASC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(row) for row in cursor.fetchall()]

    def update_merge_queue_status(
        self, queue_id: int, status: str, completed_at: Optional[str] = None
    ):
        """
        Update merge queue entry status.

        Args:
            queue_id: Merge queue entry ID
            status: New status (queued|running|merged|failed)
            completed_at: Completion timestamp (optional)
        """
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE merge_queue
                SET status = ?, completed_at = ?
                WHERE id = ?
                """,
                (status, completed_at, queue_id),
            )

    def get_merge_queue_entry(self, queue_id: int) -> Optional[Dict[str, Any]]:
        """
        Get a single merge queue entry by ID.

        Args:
            queue_id: Merge queue entry ID

        Returns:
            Merge queue entry dict, or None if not found
        """
        cursor = self.conn.execute(
            "SELECT * FROM merge_queue WHERE id = ?", (queue_id,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_merge_queue_stats(self) -> Dict[str, int]:
        """
        Get merge queue statistics by status.

        Returns:
            Dict mapping status to count, e.g. {"queued": 3, "merged": 10, ...}
        """
        cursor = self.conn.execute(
            """
            SELECT status, COUNT(*) as count
            FROM merge_queue
            GROUP BY status
            """
        )
        stats = {"queued": 0, "running": 0, "merged": 0, "failed": 0}
        for row in cursor.fetchall():
            stats[row["status"]] = row["count"]
        return stats
