"""Core database class: connection, schema, migrations, agents, merge queue, projects, events."""

import json
import logging
import sqlite3

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..utils import generate_id, _normalize_project_name

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
    last_heartbeat_at TEXT,
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
-- NOTE_DELIVERIES: delivery tracking for notes
----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS note_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER NOT NULL REFERENCES notes(id),
    recipient_agent_id TEXT,
    recipient_issue_id TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    delivered_at TEXT,
    read_at TEXT,
    acked_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_note_deliveries_note ON note_deliveries(note_id);
CREATE INDEX IF NOT EXISTS idx_note_deliveries_inbox ON note_deliveries(recipient_agent_id, recipient_issue_id, status, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS uidx_note_deliveries_note_agent_global ON note_deliveries(note_id, recipient_agent_id) WHERE recipient_issue_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uidx_note_deliveries_note_agent_issue ON note_deliveries(note_id, recipient_agent_id, recipient_issue_id) WHERE recipient_issue_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uidx_note_deliveries_note_issue_target ON note_deliveries(note_id, recipient_issue_id) WHERE recipient_agent_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_note_deliveries_issue_targets ON note_deliveries(recipient_issue_id) WHERE recipient_agent_id IS NULL;

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
-- PROJECTS: registry of all known projects and their disk paths
----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS projects (
    name        TEXT PRIMARY KEY,
    path        TEXT NOT NULL,
    registered_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);

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
        WHEN e_inc.id IS NOT NULL THEN 'incomplete'
        WHEN e_esc.id IS NOT NULL THEN 'escalated'
        ELSE 'unknown'
    END as outcome,
    ROUND((julianday(COALESCE(e_done.created_at, e_inc.created_at, e_esc.created_at)) - julianday(e_start.created_at)) * 86400, 1) as duration_s,
    (SELECT COUNT(*) FROM events er WHERE er.issue_id = e_start.issue_id AND er.event_type = 'retry') as retry_count,
    (SELECT COUNT(*) FROM events en WHERE en.agent_id = e_start.agent_id AND en.event_type = 'notes_harvested') as notes_produced,
    (SELECT COUNT(*) FROM events eni WHERE eni.agent_id = e_start.agent_id AND eni.event_type = 'notes_injected') as notes_injected,
    e_start.created_at as started_at,
    COALESCE(e_done.created_at, e_inc.created_at, e_esc.created_at) as ended_at
FROM events e_start
JOIN issues i ON e_start.issue_id = i.id
LEFT JOIN events e_done ON e_start.agent_id = e_done.agent_id AND e_done.event_type = 'completed'
LEFT JOIN events e_inc ON e_start.agent_id = e_inc.agent_id AND e_inc.event_type = 'incomplete'
LEFT JOIN events e_esc ON e_start.issue_id = e_esc.issue_id AND e_esc.event_type = 'escalated'
WHERE e_start.event_type = 'worker_started';

----------------------------------------------------------------------

"""


class DatabaseCore:
    """SQLite database wrapper for Hive orchestrator."""

    def __init__(self, db_path: str | None = None):
        """
        Initialize database connection.

        Args:
            db_path: Path to SQLite database file
        """
        from ..config import Config

        self.db_path = db_path or Config.DB_PATH
        self.conn: Optional[sqlite3.Connection] = None

    def connect(self):
        """Open database connection and initialize schema."""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        """Create tables/views and run migrations.

        When the schema already exists and the write lock is held (e.g. by the
        daemon), all writes are skipped immediately — no blocking, no timeout.
        Read-only CLI commands work fine with the existing schema.
        """
        cursor = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='issues'")
        schema_exists = cursor.fetchone() is not None

        if not schema_exists:
            # First-time init — no daemon can be running yet. Must succeed.
            self.conn.executescript(SCHEMA)
            self._migrate_if_needed()
            self.conn.execute("PRAGMA busy_timeout = 5000")
            return

        # Schema exists — refresh views + migrations only if we can grab the
        # write lock instantly.  Zero timeout prevents CLI hangs.
        self.conn.execute("PRAGMA busy_timeout = 0")
        try:
            self.conn.execute("DROP VIEW IF EXISTS agent_runs")
            self.conn.executescript(SCHEMA)
            self._migrate_if_needed()
        except sqlite3.OperationalError:
            pass  # daemon has the lock — schema is already current
        finally:
            self.conn.execute("PRAGMA busy_timeout = 5000")

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
        self._ensure_column("notes", "must_read", "INTEGER NOT NULL DEFAULT 0")
        self._ensure_column("agents", "last_heartbeat_at", "TEXT")

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

        # Backfill heartbeat for pre-migration agent rows.
        # Use a local delta so logging isn't polluted by prior statements on this connection.
        before_changes = self.conn.total_changes
        self.conn.execute(
            """
            UPDATE agents
            SET last_heartbeat_at = COALESCE(last_progress_at, datetime('now'))
            WHERE last_heartbeat_at IS NULL
            """
        )
        backfilled = self.conn.total_changes - before_changes
        if backfilled > 0:
            self.conn.commit()
            logger.info(f"Backfilled {backfilled} agents.last_heartbeat_at")

        # Collapse 'failed' issue status into 'escalated'
        self.conn.execute("UPDATE issues SET status = 'escalated' WHERE status = 'failed'")
        self.conn.commit()

        self._ensure_merge_queue_idempotency()

        # Create index on agents.project if it doesn't exist
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_agents_project ON agents(project)")
        self.conn.commit()

    def _ensure_merge_queue_idempotency(self) -> None:
        """Ensure merge queue constraints that make enqueueing idempotent.

        Invariant: there can be at most one active merge entry (queued|running)
        per issue. This prevents duplicate merge enqueues when completion
        handling is replayed.
        """
        if not self.conn:
            raise RuntimeError("Database not connected")

        index_sql = """
            CREATE UNIQUE INDEX IF NOT EXISTS uidx_merge_queue_active_issue
            ON merge_queue(issue_id)
            WHERE status IN ('queued', 'running')
        """

        try:
            self.conn.execute(index_sql)
            self.conn.commit()
            return
        except sqlite3.Error as e:
            if "unique constraint failed" not in str(e).lower():
                raise

        logger.warning("Deduping merge_queue active entries before adding unique index")
        self._dedupe_active_merge_queue_entries()
        self.conn.execute(index_sql)
        self.conn.commit()

    def _dedupe_active_merge_queue_entries(self) -> None:
        """Best-effort: collapse duplicate active merge entries per issue."""
        if not self.conn:
            raise RuntimeError("Database not connected")

        dup_rows = self.conn.execute(
            """
            SELECT issue_id
            FROM merge_queue
            WHERE status IN ('queued', 'running')
            GROUP BY issue_id
            HAVING COUNT(*) > 1
            """
        ).fetchall()

        if not dup_rows:
            return

        for row in dup_rows:
            issue_id = row["issue_id"]
            entries = self.conn.execute(
                """
                SELECT id, status, enqueued_at
                FROM merge_queue
                WHERE issue_id = ?
                  AND status IN ('queued', 'running')
                ORDER BY
                  CASE status WHEN 'running' THEN 0 ELSE 1 END,
                  enqueued_at ASC,
                  id ASC
                """,
                (issue_id,),
            ).fetchall()

            if len(entries) <= 1:
                continue

            keep_id = entries[0]["id"]
            drop_ids = [e["id"] for e in entries[1:]]
            logger.warning(f"merge_queue dedupe: keeping {keep_id} and failing {drop_ids} for issue {issue_id}")
            self.conn.executemany(
                "UPDATE merge_queue SET status = 'failed', completed_at = datetime('now') WHERE id = ?",
                [(merge_id,) for merge_id in drop_ids],
            )

        self.conn.commit()

    def enqueue_merge(
        self,
        *,
        issue_id: str,
        agent_id: Optional[str],
        project: str,
        worktree: str,
        branch_name: str,
        test_command: Optional[str] = None,
    ) -> bool:
        """Enqueue an issue for merge processing.

        Returns:
            True if a new merge_queue row was inserted, False if it already existed.
        """
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO merge_queue (issue_id, agent_id, project, worktree, branch_name, test_command)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (issue_id, agent_id, project, worktree, branch_name, test_command),
            )
            return cursor.rowcount == 1

    def try_transition_merge_queue_status(
        self,
        queue_id: int,
        *,
        from_status: str,
        to_status: str,
        completed_at: Optional[str] = None,
    ) -> bool:
        """CAS-style merge_queue status transition.

        Returns:
            True if updated, False if the entry was not in from_status.
        """
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE merge_queue
                SET status = ?, completed_at = ?
                WHERE id = ? AND status = ?
                """,
                (to_status, completed_at, queue_id, from_status),
            )
            return cursor.rowcount == 1

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

    def get_agent(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Get agent by ID."""
        cursor = self.conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def try_transition_agent_status(
        self,
        agent_id: str,
        *,
        from_status: str,
        to_status: str,
    ) -> bool:
        """CAS-style agent status transition."""
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE agents
                SET status = ?,
                    updated_at = datetime('now')
                WHERE id = ? AND status = ?
                """,
                (to_status, agent_id, from_status),
            )
            return cursor.rowcount == 1

    def try_touch_agent_heartbeat(
        self,
        agent_id: str,
        *,
        required_status: str = "working",
    ) -> bool:
        """Update an agent heartbeat if it is in the expected status."""
        with self.transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE agents
                SET last_heartbeat_at = datetime('now'),
                    last_progress_at = datetime('now'),
                    updated_at = datetime('now')
                WHERE id = ? AND status = ?
                """,
                (agent_id, required_status),
            )
            return cursor.rowcount == 1

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

    def get_merge_queue_stats(self, project: Optional[str] = None) -> Dict[str, int]:
        """
        Get merge queue statistics by status.

        Args:
            project: Filter by project (optional)

        Returns:
            Dict mapping status to count, e.g. {"queued": 3, "merged": 10, ...}
        """
        if project:
            cursor = self.conn.execute(
                "SELECT status, COUNT(*) as count FROM merge_queue WHERE project = ? GROUP BY status",
                (project,),
            )
        else:
            cursor = self.conn.execute("SELECT status, COUNT(*) as count FROM merge_queue GROUP BY status")
        stats = {"queued": 0, "running": 0, "merged": 0, "failed": 0}
        for row in cursor.fetchall():
            stats[row["status"]] = row["count"]
        return stats

    def log_system_event(self, event_type: str, detail: Optional[Dict[str, Any]] = None, commit: bool = True):
        """
        Log a system-level event to the audit trail.

        Args:
            event_type: Type of system event (e.g., 'daemon_started')
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

    # ── Projects ──────────────────────────────────────────────────────

    def register_project(self, name: str, path: str) -> None:
        """Register a project name→path mapping. Idempotent via INSERT OR REPLACE.

        INV-2: Normalizes *name* to the bare repo form — if a caller passes
        "org/repo", it is stored as "repo" to match what detect_project() returns.
        """
        if not name:
            raise ValueError("Project name must not be empty")
        if not path:
            raise ValueError("Project path must not be empty")
        name = _normalize_project_name(name)
        with self.transaction():
            self.conn.execute(
                "INSERT OR REPLACE INTO projects (name, path) VALUES (?, ?)",
                (name, path),
            )

    def list_projects(self) -> list[dict]:
        """Return all registered projects as [{name, path, registered_at}]."""
        rows = self.conn.execute("SELECT name, path, registered_at FROM projects ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    def unregister_project(self, name: str) -> bool:
        """Remove a project from the registry. Returns True if a row was deleted."""
        with self.transaction() as conn:
            cursor = conn.execute("DELETE FROM projects WHERE name = ?", (name,))
            return cursor.rowcount > 0

    def get_project_path(self, name: str) -> Optional[str]:
        """Return the disk path for a project by name, or None if not found."""
        row = self.conn.execute("SELECT path FROM projects WHERE name = ?", (name,)).fetchone()
        return row["path"] if row else None
