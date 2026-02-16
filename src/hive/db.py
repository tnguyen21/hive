"""SQLite database layer for Hive orchestrator."""

import json
import logging
import sqlite3

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from .utils import generate_id

logger = logging.getLogger(__name__)


# Allowed tag values. Validated at creation time but stored as free-form JSON
# so the set can grow without a migration.
ALLOWED_TAGS = {
    # Task type
    "refactor",
    "bugfix",
    "feature",
    "test",
    "docs",
    "cleanup",
    "config",
    # Language
    "python",
    "typescript",
    "javascript",
    "sql",
    "shell",
    "markdown",
    # Complexity
    "small",
    "medium",
    "large",
}


def validate_tags(tags: list[str]) -> list[str]:
    """Validate and normalize tags. Raises ValueError for unknown tags."""
    normalized = [t.lower().strip() for t in tags]
    unknown = set(normalized) - ALLOWED_TAGS
    if unknown:
        raise ValueError(f"Unknown tags: {unknown}. Allowed: {sorted(ALLOWED_TAGS)}")
    return sorted(set(normalized))  # dedupe and sort for consistency


# SQL schema definition
SCHEMA = """
-- WAL mode for concurrent reads during writes
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
-- NOTE: Existing databases may still have agent_id FK constraints in events, notes, merge_queue.
-- Deletion code must use PRAGMA foreign_keys = OFF when deleting agents for backward compatibility.
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
    model       TEXT,
    tags        TEXT,  -- JSON array of tag strings
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
-- AGENTS: ephemeral execution identity (deleted after merge/cleanup)
----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agents (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'idle',
    session_id  TEXT,
    worktree    TEXT,
    current_issue TEXT REFERENCES issues(id),
    project     TEXT,
    model       TEXT,
    lease_expires_at TEXT,
    last_progress_at TEXT,
    metadata    TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

----------------------------------------------------------------------
-- EVENTS: append-only audit trail
-- agent_id is a correlation key, not a live FK (agents are deleted after merge)
----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id    TEXT REFERENCES issues(id),
    agent_id    TEXT,
    event_type  TEXT NOT NULL,
    detail      TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_events_issue ON events(issue_id);
CREATE INDEX IF NOT EXISTS idx_events_agent ON events(agent_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);

----------------------------------------------------------------------
-- NOTES: inter-agent knowledge transfer system
----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id    TEXT REFERENCES issues(id),
    agent_id    TEXT,
    project     TEXT,
    category    TEXT NOT NULL DEFAULT 'discovery',
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_notes_issue ON notes(issue_id);
CREATE INDEX IF NOT EXISTS idx_notes_category ON notes(category);
CREATE INDEX IF NOT EXISTS idx_notes_created ON notes(created_at);

----------------------------------------------------------------------
-- MERGE_QUEUE: dedicated finalizer queue
----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS merge_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id    TEXT NOT NULL REFERENCES issues(id),
    agent_id    TEXT,
    project     TEXT NOT NULL,
    worktree    TEXT NOT NULL,
    branch_name TEXT NOT NULL,
    test_command TEXT,
    status      TEXT NOT NULL DEFAULT 'queued',
    enqueued_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_mq_status ON merge_queue(status);
CREATE INDEX IF NOT EXISTS idx_mq_project ON merge_queue(project);

----------------------------------------------------------------------
-- AGENT_RUNS: Materialized view over events for per-agent-run metrics
----------------------------------------------------------------------
CREATE VIEW IF NOT EXISTS agent_runs AS
SELECT
    e_start.agent_id,
    e_start.issue_id,
    i.type as issue_type,
    COALESCE(i.model, 'unknown') as model,
    i.tags,
    CASE
        WHEN e_done.id IS NOT NULL THEN 'done'
        WHEN e_fail.id IS NOT NULL THEN 'failed'
        WHEN e_esc.id IS NOT NULL THEN 'escalated'
        ELSE 'unknown'
    END as outcome,
    ROUND((julianday(COALESCE(e_done.created_at, e_fail.created_at, e_esc.created_at)) - julianday(e_start.created_at)) * 86400, 1) as duration_s,
    (SELECT COUNT(*) FROM events er WHERE er.issue_id = e_start.issue_id AND er.event_type = 'retry') as retry_count,
    (SELECT COUNT(*) FROM events en WHERE en.agent_id = e_start.agent_id AND en.event_type = 'notes_harvested') as notes_produced,
    (SELECT COUNT(*) FROM events eni WHERE eni.agent_id = e_start.agent_id AND eni.event_type = 'notes_injected') as notes_injected,
    e_start.created_at as started_at,
    COALESCE(e_done.created_at, e_fail.created_at, e_esc.created_at) as ended_at
FROM events e_start
JOIN issues i ON e_start.issue_id = i.id
LEFT JOIN events e_done ON e_start.agent_id = e_done.agent_id AND e_done.event_type = 'completed'
LEFT JOIN events e_fail ON e_start.agent_id = e_fail.agent_id AND e_fail.event_type IN ('status_failed')
LEFT JOIN events e_esc ON e_start.issue_id = e_esc.issue_id AND e_esc.event_type = 'escalated'
WHERE e_start.event_type = 'worker_started';

----------------------------------------------------------------------

"""


class Database:
    """SQLite database wrapper for Hive orchestrator."""

    def __init__(self, db_path: str = None):
        """
        Initialize database connection.

        Args:
            db_path: Path to SQLite database file
        """
        from .config import Config

        self.db_path = db_path or Config.DB_PATH
        self.conn: Optional[sqlite3.Connection] = None

    def connect(self):
        """Open database connection and initialize schema."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate_if_needed()

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

    def _ensure_column(self, table: str, column: str, col_type: str):
        """Add column to table if it does not exist. Idempotent."""
        cursor = self.conn.execute(f"PRAGMA table_info({table})")
        columns = [row[1] for row in cursor.fetchall()]
        if column not in columns:
            try:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
                self.conn.commit()
                logger.info(f"Added {column} column to {table} table")
            except sqlite3.Error as e:
                if "duplicate column name" not in str(e).lower():
                    raise

    def _migrate_if_needed(self):
        """Apply any necessary database migrations."""
        if not self.conn:
            raise RuntimeError("Database not connected")

        # Add missing columns using helper
        self._ensure_column("issues", "model", "TEXT")
        self._ensure_column("issues", "tags", "TEXT")
        self._ensure_column("merge_queue", "test_command", "TEXT")
        self._ensure_column("notes", "project", "TEXT")
        self._ensure_column("agents", "project", "TEXT")

        # Create tags index (safe after column exists via CREATE TABLE or migration)
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_issues_tags ON issues(tags)")
        self.conn.commit()

        # Backfill notes.project from issues.project via issue_id FK (always run to catch any NULL values)
        self.conn.execute("""
            UPDATE notes
            SET project = (SELECT project FROM issues WHERE issues.id = notes.issue_id)
            WHERE issue_id IS NOT NULL AND project IS NULL
        """)
        if self.conn.total_changes > 0:
            self.conn.commit()
            logger.info(f"Backfilled {self.conn.total_changes} notes.project from issues.project")

        # Create index on notes.project if it doesn't exist
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_project ON notes(project)")
        self.conn.commit()

        # Create index on agents.project if it doesn't exist
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_agents_project ON agents(project)")
        self.conn.commit()

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
    ) -> str:
        """
        Create a new issue.

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
        query = """
            SELECT i.*
            FROM issues i
            WHERE i.status = 'open'
              AND i.assignee IS NULL
              AND i.type != 'epic'
              AND NOT EXISTS (
                SELECT 1 FROM dependencies d
                JOIN issues blocker ON d.depends_on = blocker.id
                WHERE d.issue_id = i.id
                  AND d.type = 'blocks'
                  AND blocker.status NOT IN ('done', 'finalized', 'canceled')
              )
        """

        params = []
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
                """
                UPDATE issues
                SET assignee = ?,
                    status = 'in_progress',
                    updated_at = datetime('now')
                WHERE id = ?
                  AND assignee IS NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM dependencies d
                    JOIN issues blocker ON d.depends_on = blocker.id
                    WHERE d.issue_id = ?
                      AND d.type = 'blocks'
                      AND blocker.status NOT IN ('done', 'finalized', 'canceled')
                  )
                """,
                (agent_id, issue_id, issue_id),
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

    def log_event(
        self,
        issue_id: Optional[str],
        agent_id: Optional[str],
        event_type: str,
        detail: Optional[Dict[str, Any]] = None,
        commit: bool = True,
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
        if commit:
            self.conn.commit()

    def create_agent(
        self,
        name: str,
        model: str = "claude-sonnet-4-5-20250929",
        metadata: Optional[Dict[str, Any]] = None,
        project: Optional[str] = None,
    ) -> str:
        """
        Create a new agent identity.

        Args:
            name: Human-readable agent name
            model: Model identifier
            metadata: Additional metadata dict
            project: Project identifier (optional)

        Returns:
            Generated agent ID
        """
        agent_id = generate_id("agent")
        metadata_json = json.dumps(metadata) if metadata else None

        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO agents (id, name, model, metadata, project)
                VALUES (?, ?, ?, ?, ?)
                """,
                (agent_id, name, model, metadata_json, project),
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
        """Update issue status. Clears assignee when setting to 'open' (INV-2)."""
        with self.transaction() as conn:
            conn.execute(
                """
                UPDATE issues
                SET status = ?,
                    assignee = CASE WHEN ? = 'open' THEN NULL ELSE assignee END,
                    updated_at = datetime('now'),
                    closed_at = CASE WHEN ? IN ('done', 'finalized', 'canceled', 'failed')
                                     THEN datetime('now')
                                     ELSE closed_at END
                WHERE id = ?
                """,
                (status, status, status, issue_id),
            )
            self.log_event(issue_id, None, f"status_{status}", {"status": status}, commit=False)

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

    def _query_events(
        self,
        *,
        after_id: int = 0,
        issue_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        event_type: Optional[str] = None,
        limit: Optional[int] = None,
        order: str = "DESC",
    ) -> List[Dict[str, Any]]:
        """Private helper for querying events with optional filtering and ordering."""
        conditions = []
        params = []
        if after_id:
            conditions.append("id > ?")
            params.append(after_id)
        if issue_id:
            conditions.append("issue_id = ?")
            params.append(issue_id)
        if agent_id:
            conditions.append("agent_id = ?")
            params.append(agent_id)
        if event_type:
            conditions.append("event_type = ?")
            params.append(event_type)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        query = f"SELECT * FROM events {where} ORDER BY id {order}"
        if limit:
            query += " LIMIT ?"
            params.append(limit)
        cursor = self.conn.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    def get_events(
        self,
        issue_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        event_type: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Get events filtered by issue, agent, or type."""
        return self._query_events(issue_id=issue_id, agent_id=agent_id, event_type=event_type, limit=limit, order="DESC")

    def get_events_since(
        self,
        after_id: int = 0,
        issue_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        event_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get events with id > after_id, ordered ascending (oldest first)."""
        return self._query_events(after_id=after_id, issue_id=issue_id, agent_id=agent_id, event_type=event_type, order="ASC")

    def get_max_event_id(self) -> int:
        """Return the current maximum event id, or 0 if no events."""
        cursor = self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM events")
        return cursor.fetchone()[0]

    def get_recent_events(
        self,
        n: int = 20,
        issue_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        event_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get the most recent n events, returned oldest-first."""
        results = self._query_events(issue_id=issue_id, agent_id=agent_id, event_type=event_type, limit=n, order="DESC")
        return list(reversed(results))

    def count_events_by_type(self, issue_id: str, event_type: str) -> int:
        """
        Count events of a specific type for an issue.

        Args:
            issue_id: Issue ID to count events for
            event_type: Type of event to count (e.g., 'retry', 'agent_switch')

        Returns:
            Number of events of the specified type for the issue
        """
        cursor = self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE issue_id = ? AND event_type = ?",
            (issue_id, event_type),
        )
        return cursor.fetchone()[0]

    def get_issue_token_total(self, issue_id: str) -> int:
        """Get total tokens (input + output) used for an issue across all agents."""
        cursor = self.conn.execute(
            """
            SELECT COALESCE(
                SUM(json_extract(detail, '$.input_tokens') + json_extract(detail, '$.output_tokens')),
                0
            )
            FROM events
            WHERE issue_id = ? AND event_type = 'tokens_used' AND json_valid(detail)
            """,
            (issue_id,),
        )
        return cursor.fetchone()[0]

    def get_run_token_total(self) -> int:
        """Get total tokens used across all issues in this daemon run.

        Uses all tokens_used events in the DB. For per-run isolation, the
        daemon should use a fresh DB or track a run_id boundary event.
        """
        cursor = self.conn.execute(
            """
            SELECT COALESCE(
                SUM(json_extract(detail, '$.input_tokens') + json_extract(detail, '$.output_tokens')),
                0
            )
            FROM events
            WHERE event_type = 'tokens_used' AND json_valid(detail)
            """
        )
        return cursor.fetchone()[0]

    def count_events_since_minutes(self, issue_id: str, event_type: str, minutes: int) -> int:
        """Count events of a given type for an issue within the last N minutes.

        Uses SQLite's datetime('now') for comparison to avoid timezone mismatches
        between Python's local time and SQLite's UTC-based created_at timestamps.

        Args:
            issue_id: Issue ID to count events for
            event_type: Type of event to count (e.g., 'incomplete')
            minutes: Look back this many minutes from now
        """
        cursor = self.conn.execute(
            "SELECT COUNT(*) FROM events WHERE issue_id = ? AND event_type = ? AND created_at >= datetime('now', ?)",
            (issue_id, event_type, f"-{minutes} minutes"),
        )
        return cursor.fetchone()[0]

    def get_next_ready_step(self, parent_id: str) -> Optional[Dict[str, Any]]:
        """
        Get the next ready step within a epic.

        Args:
            parent_id: Parent epic issue ID

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

    def get_active_agents(self, project: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get all currently active agents.

        Args:
            project: Filter by project (optional)

        Returns:
            List of active agent dicts
        """
        query = "SELECT * FROM agents WHERE status = 'working'"
        params = []

        if project is not None:
            query += " AND project = ?"
            params.append(project)

        query += " ORDER BY created_at ASC"

        cursor = self.conn.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    # --- Merge Queue Methods ---

    def get_queued_merges(self, project: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Get queued merge queue entries, oldest first.

        Args:
            project: Filter by project (optional)
            limit: Maximum entries to return

        Returns:
            List of merge queue entry dicts with joined issue/agent info
        """
        query = """
            SELECT mq.*, i.title as issue_title, a.name as agent_name
            FROM merge_queue mq
            JOIN issues i ON mq.issue_id = i.id
            LEFT JOIN agents a ON mq.agent_id = a.id
            WHERE mq.status = 'queued'
        """

        params = []
        if project is not None:
            query += " AND mq.project = ?"
            params.append(project)

        query += " ORDER BY mq.enqueued_at ASC LIMIT ?"
        params.append(limit)

        cursor = self.conn.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    def update_merge_queue_status(self, queue_id: int, status: str, completed_at: Optional[str] = None):
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

    def log_system_event(self, event_type: str, detail: Optional[Dict[str, Any]] = None, commit: bool = True):
        """
        Log a system-level event to the audit trail.

        Args:
            event_type: Type of system event (e.g., 'opencode_degraded', 'opencode_recovered')
            detail: Additional event details dict
        """
        detail_json = json.dumps(detail) if detail else None

        if not self.conn:
            raise RuntimeError("Database not connected")

        self.conn.execute(
            """
            INSERT INTO events (issue_id, agent_id, event_type, detail)
            VALUES (NULL, NULL, ?, ?)
            """,
            (event_type, detail_json),
        )
        if commit:
            self.conn.commit()

    def batch_log_events(self, events: list):
        """Log multiple events in a single transaction."""
        for event in events:
            self.log_event(event["issue_id"], event.get("agent_id"), event["event_type"], event.get("detail"), commit=False)
        self.conn.commit()

    def get_idle_agents(self) -> List[Dict[str, Any]]:
        """
        Get all idle agents.

        Returns:
            List of idle agent dicts
        """
        cursor = self.conn.execute(
            """
            SELECT * FROM agents
            WHERE status = 'idle'
            ORDER BY created_at ASC
            """
        )
        return [dict(row) for row in cursor.fetchall()]

    def get_token_usage(
        self,
        issue_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        project: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Get aggregated token usage from 'tokens_used' events.

        Args:
            issue_id: Filter by specific issue ID (optional)
            agent_id: Filter by specific agent ID (optional)
            project: Filter by project via JOIN to issues (optional)

        Returns:
            Dict with aggregated token counts and cost estimates
        """
        if not self.conn:
            raise RuntimeError("Database not connected")

        conditions = ["e.event_type = 'tokens_used'"]
        params = []
        from_clause = "FROM events e"

        if project is not None:
            from_clause += " JOIN issues i ON e.issue_id = i.id"
            conditions.append("i.project = ?")
            params.append(project)

        if issue_id:
            conditions.append("e.issue_id = ?")
            params.append(issue_id)
        if agent_id:
            conditions.append("e.agent_id = ?")
            params.append(agent_id)

        where_clause = " AND ".join(conditions)

        # Get totals using json_extract
        totals_query = f"""
            SELECT
                COALESCE(SUM(json_extract(e.detail, '$.input_tokens')), 0) as total_input,
                COALESCE(SUM(json_extract(e.detail, '$.output_tokens')), 0) as total_output
            {from_clause}
            WHERE {where_clause} AND json_valid(e.detail)
        """

        cursor = self.conn.execute(totals_query, params)
        totals_row = cursor.fetchone()
        total_input_tokens = totals_row["total_input"]
        total_output_tokens = totals_row["total_output"]
        total_tokens = total_input_tokens + total_output_tokens

        # Get breakdown by issue
        issue_breakdown = {}
        if issue_id is None:  # Only aggregate by issue if not filtering by specific issue
            issue_query = f"""
                SELECT
                    e.issue_id,
                    COALESCE(SUM(json_extract(e.detail, '$.input_tokens')), 0) as input_tokens,
                    COALESCE(SUM(json_extract(e.detail, '$.output_tokens')), 0) as output_tokens
                {from_clause}
                WHERE {where_clause} AND e.issue_id IS NOT NULL AND json_valid(e.detail)
                GROUP BY e.issue_id
            """
            cursor = self.conn.execute(issue_query, params)
            for row in cursor.fetchall():
                issue_breakdown[row["issue_id"]] = {"input_tokens": row["input_tokens"], "output_tokens": row["output_tokens"]}
        elif issue_id:
            # If filtering by specific issue, include it in breakdown
            issue_breakdown[issue_id] = {"input_tokens": total_input_tokens, "output_tokens": total_output_tokens}

        # Get breakdown by agent
        agent_breakdown = {}
        if agent_id is None:  # Only aggregate by agent if not filtering by specific agent
            agent_query = f"""
                SELECT
                    e.agent_id,
                    COALESCE(SUM(json_extract(e.detail, '$.input_tokens')), 0) as input_tokens,
                    COALESCE(SUM(json_extract(e.detail, '$.output_tokens')), 0) as output_tokens
                {from_clause}
                WHERE {where_clause} AND e.agent_id IS NOT NULL AND json_valid(e.detail)
                GROUP BY e.agent_id
            """
            cursor = self.conn.execute(agent_query, params)
            for row in cursor.fetchall():
                agent_breakdown[row["agent_id"]] = {"input_tokens": row["input_tokens"], "output_tokens": row["output_tokens"]}
        elif agent_id:
            # If filtering by specific agent, include it in breakdown
            agent_breakdown[agent_id] = {"input_tokens": total_input_tokens, "output_tokens": total_output_tokens}

        # Get breakdown by model
        model_query = f"""
            SELECT
                COALESCE(json_extract(e.detail, '$.model'), 'unknown') as model,
                COALESCE(SUM(json_extract(e.detail, '$.input_tokens')), 0) as input_tokens,
                COALESCE(SUM(json_extract(e.detail, '$.output_tokens')), 0) as output_tokens
            {from_clause}
            WHERE {where_clause} AND json_valid(e.detail)
            GROUP BY json_extract(e.detail, '$.model')
        """

        model_breakdown = {}
        cursor = self.conn.execute(model_query, params)
        for row in cursor.fetchall():
            model_breakdown[row["model"]] = {"input_tokens": row["input_tokens"], "output_tokens": row["output_tokens"]}

        # Estimate cost (rough approximation, varies by model)
        # Using Claude Sonnet pricing as baseline: ~$15/1M input, ~$75/1M output
        estimated_cost = (total_input_tokens / 1_000_000 * 15.0) + (total_output_tokens / 1_000_000 * 75.0)

        return {
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_tokens": total_tokens,
            "estimated_cost_usd": round(estimated_cost, 4),
            "issue_breakdown": issue_breakdown,
            "agent_breakdown": agent_breakdown,
            "model_breakdown": model_breakdown,
        }

    # --- Notes Methods ---

    def add_note(
        self,
        issue_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        content: str = "",
        category: str = "discovery",
        project: Optional[str] = None,
    ) -> int:
        """
        Insert a note and return its ID.

        Args:
            issue_id: Which issue the note was discovered during. None = project-wide note.
            agent_id: Which agent wrote it. None = Queen-authored or system note.
            content: The note text. Short — typically 1-3 sentences.
            category: One of 'discovery', 'gotcha', 'dependency', 'pattern', 'context'.
            project: Project identifier. If None and issue_id provided, backfilled via migration.

        Returns:
            The ID of the inserted note
        """
        if not self.conn:
            raise RuntimeError("Database not connected")

        cursor = self.conn.execute(
            "INSERT INTO notes (issue_id, agent_id, category, content, project) VALUES (?, ?, ?, ?, ?)",
            (issue_id, agent_id, category, content, project),
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

    def get_notes_for_epic(self, parent_id: str) -> List[Dict]:
        """
        Get all notes from issues that share a parent epic. For predecessor context.

        Args:
            parent_id: Parent epic issue ID

        Returns:
            List of note dicts from child issues, ordered by creation time (oldest first)
        """
        if not self.conn:
            raise RuntimeError("Database not connected")

        rows = self.conn.execute(
            """
            SELECT n.* FROM notes n
            JOIN issues i ON n.issue_id = i.id
            WHERE i.parent_id = ?
            ORDER BY n.created_at ASC
        """,
            (parent_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_completed_epic_steps(self, parent_id: str) -> List[Dict]:
        """
        Get completed/finalized sibling issues for a epic, ordered by creation time.

        Args:
            parent_id: Parent epic issue ID

        Returns:
            List of issue dicts with id, title, description, status
        """
        if not self.conn:
            raise RuntimeError("Database not connected")
        rows = self.conn.execute(
            """
            SELECT id, title, description, status FROM issues
            WHERE parent_id = ? AND status IN ('done', 'finalized')
            ORDER BY created_at ASC
            """,
            (parent_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def check_epic_complete(self, parent_id: str) -> bool:
        """
        Check if all child issues of a epic are complete.

        Args:
            parent_id: Parent epic issue ID

        Returns:
            True if all children are done/finalized/canceled, False otherwise
        """
        cursor = self.conn.execute(
            "SELECT COUNT(*) FROM issues WHERE parent_id = ? AND status NOT IN ('done', 'finalized', 'canceled')",
            (parent_id,),
        )
        return cursor.fetchone()[0] == 0

    def get_model_performance(self, model: Optional[str] = None, tag: Optional[str] = None, group_by: str = "tag") -> List[Dict[str, Any]]:
        """Get model performance stats, optionally filtered by model or tag.

        Args:
            model: Filter to a specific model name.
            tag: Filter to issues containing this tag.
            group_by: "tag" (default) groups by model × tag, "type" groups by model × type.

        Returns aggregated stats: model, group label, success/failure counts, retries, tokens, duration.
        """
        if group_by == "tag":
            group_col = "COALESCE(jt.value, 'untagged')"
            group_alias = "tag"
            from_clause = """FROM issues i
            LEFT JOIN json_each(i.tags) jt ON 1=1
            LEFT JOIN events e ON i.id = e.issue_id AND e.event_type = 'tokens_used'"""
        else:
            group_col = "i.type"
            group_alias = "type"
            from_clause = """FROM issues i
            LEFT JOIN events e ON i.id = e.issue_id AND e.event_type = 'tokens_used'"""

        query = f"""
            SELECT
                COALESCE(i.model, 'unknown') as model,
                {group_col} as {group_alias},
                COUNT(DISTINCT i.id) as issue_count,
                SUM(CASE WHEN i.status IN ('done', 'finalized') THEN 1 ELSE 0 END) as successes,
                SUM(CASE WHEN i.status = 'failed' THEN 1 ELSE 0 END) as failures,
                SUM(CASE WHEN i.status = 'escalated' THEN 1 ELSE 0 END) as escalations,
                (SELECT COUNT(*) FROM events e2 WHERE e2.issue_id = i.id AND e2.event_type = 'retry') as total_retries,
                COALESCE(SUM(CAST(json_extract(e.detail, '$.input_tokens') AS INTEGER)), 0) as total_input_tokens,
                COALESCE(SUM(CAST(json_extract(e.detail, '$.output_tokens') AS INTEGER)), 0) as total_output_tokens,
                ROUND(AVG(
                    (julianday(COALESCE(i.closed_at, datetime('now'))) - julianday(i.created_at)) * 24 * 60
                ), 1) as avg_duration_minutes
            {from_clause}
            WHERE i.type != 'epic'
        """
        params: list = []
        if model:
            query += " AND i.model = ?"
            params.append(model)
        if tag:
            query += " AND i.tags LIKE ?"
            params.append(f'%"{tag}"%')

        query += f" GROUP BY model, {group_alias} ORDER BY issue_count DESC"

        cursor = self.conn.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

    def get_metrics(
        self,
        model: Optional[str] = None,
        tag: Optional[str] = None,
        issue_type: Optional[str] = None,
        project: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Get aggregated metrics from agent_runs view.

        Args:
            model: Filter to a specific model name.
            tag: Filter to issues containing this tag.
            issue_type: Filter to a specific issue type.
            project: Filter to a specific project (optional).

        Returns:
            List of dicts with aggregated metrics per model:
            - model: Model name
            - runs: Total number of runs
            - success_count: Number of successful runs
            - failed_count: Number of failed runs
            - escalated_count: Number of escalated runs
            - success_rate: Success percentage
            - avg_duration_s: Average duration in seconds
            - avg_retries: Average retry count
            - merge_health: Percentage of runs with clean merge (tests_passed / (tests_passed + test_failure + rebase_conflict))
        """
        if not self.conn:
            raise RuntimeError("Database not connected")

        # Join with issues table to filter by project
        from_clause = "FROM agent_runs ar"
        if project is not None:
            from_clause += " JOIN issues i ON ar.issue_id = i.id"

        query = f"""
            SELECT
                ar.model,
                COUNT(*) as runs,
                SUM(CASE WHEN ar.outcome = 'done' THEN 1 ELSE 0 END) as success_count,
                SUM(CASE WHEN ar.outcome = 'failed' THEN 1 ELSE 0 END) as failed_count,
                SUM(CASE WHEN ar.outcome = 'escalated' THEN 1 ELSE 0 END) as escalated_count,
                ROUND(100.0 * SUM(CASE WHEN ar.outcome = 'done' THEN 1 ELSE 0 END) / COUNT(*), 1) as success_rate,
                ROUND(AVG(ar.duration_s), 1) as avg_duration_s,
                ROUND(AVG(ar.retry_count), 1) as avg_retries
            {from_clause}
            WHERE 1=1
        """
        params: list = []

        if project:
            query += " AND i.project = ?"
            params.append(project)
        if model:
            query += " AND ar.model = ?"
            params.append(model)
        if tag:
            query += " AND ar.tags LIKE ?"
            params.append(f'%"{tag}"%')
        if issue_type:
            query += " AND ar.issue_type = ?"
            params.append(issue_type)

        query += " GROUP BY ar.model ORDER BY runs DESC"

        cursor = self.conn.execute(query, params)
        results = [dict(row) for row in cursor.fetchall()]

        # Add merge health calculation per model
        for result in results:
            model_name = result["model"]
            # Count merge-related events for issues run by this model
            merge_from_clause = "FROM events e JOIN agent_runs ar ON e.issue_id = ar.issue_id"
            if project is not None:
                merge_from_clause += " JOIN issues i ON ar.issue_id = i.id"

            merge_query = f"""
                SELECT
                    SUM(CASE WHEN e.event_type = 'tests_passed' THEN 1 ELSE 0 END) as tests_passed,
                    SUM(CASE WHEN e.event_type = 'test_failure' THEN 1 ELSE 0 END) as test_failure,
                    SUM(CASE WHEN e.event_type = 'rebase_conflict' THEN 1 ELSE 0 END) as rebase_conflict
                {merge_from_clause}
                WHERE ar.model = ?
            """
            merge_params = [model_name]
            if project:
                merge_query += " AND i.project = ?"
                merge_params.append(project)
            if tag:
                merge_query += " AND ar.tags LIKE ?"
                merge_params.append(f'%"{tag}"%')
            if issue_type:
                merge_query += " AND ar.issue_type = ?"
                merge_params.append(issue_type)

            cursor = self.conn.execute(merge_query, merge_params)
            merge_row = cursor.fetchone()
            tests_passed = merge_row["tests_passed"] or 0
            test_failure = merge_row["test_failure"] or 0
            rebase_conflict = merge_row["rebase_conflict"] or 0
            total_merge_events = tests_passed + test_failure + rebase_conflict

            if total_merge_events > 0:
                result["merge_health"] = round(100.0 * tests_passed / total_merge_events, 1)
            else:
                result["merge_health"] = None

        return results
