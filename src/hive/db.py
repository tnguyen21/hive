"""SQLite database layer for Hive orchestrator."""

import json
import logging
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from .ids import generate_id

logger = logging.getLogger(__name__)


# Stop words for keyword extraction - moved from get_agent_capability_scores
# to avoid reconstruction on every call
_STOP_WORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "should",
        "could",
        "can",
        "may",
        "might",
        "must",
    }
)


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
    model       TEXT,
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
-- NOTES: inter-agent knowledge transfer system
----------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id    TEXT REFERENCES issues(id),
    agent_id    TEXT REFERENCES agents(id),
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

    def _migrate_if_needed(self):
        """Apply any necessary database migrations."""
        if not self.conn:
            raise RuntimeError("Database not connected")

        # Check if model column exists in issues table
        cursor = self.conn.execute("PRAGMA table_info(issues)")
        columns = [row[1] for row in cursor.fetchall()]

        if "model" not in columns:
            try:
                self.conn.execute("ALTER TABLE issues ADD COLUMN model TEXT")
                self.conn.commit()
                logger.info("Added model column to issues table")
            except sqlite3.Error as e:
                if "duplicate column name" not in str(e).lower():
                    raise

    def create_issue(
        self,
        title: str,
        description: str = "",
        priority: int = 2,
        issue_type: str = "task",
        project: str = "",
        parent_id: Optional[str] = None,
        model: Optional[str] = None,
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
            model: Model to use for this issue (overrides global WORKER_MODEL)
            metadata: Additional metadata dict

        Returns:
            Generated issue ID
        """
        issue_id = generate_id("w")
        metadata_json = json.dumps(metadata) if metadata else None

        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO issues (id, title, description, priority, type, project, parent_id, model, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    metadata_json,
                ),
            )
            self.log_event(issue_id, None, "created", {"title": title}, commit=False)

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

        # Optimization: Sort by ID (primary key) instead of created_at.
        # 1. Performance: Uses the PK index to avoid a full table scan/sort.
        # 2. Correctness: Ensures strict ordering for events within the same second.
        if conditions:
            where_clause = " AND ".join(conditions)
            query = f"SELECT * FROM events WHERE {where_clause} ORDER BY id DESC LIMIT ?"
            params.append(limit)
        else:
            query = "SELECT * FROM events ORDER BY id DESC LIMIT ?"
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
                "SELECT * FROM (SELECT * FROM events WHERE issue_id = ? ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
                (issue_id, n),
            )
        elif agent_id:
            cursor = self.conn.execute(
                "SELECT * FROM (SELECT * FROM events WHERE agent_id = ? ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
                (agent_id, n),
            )
        else:
            cursor = self.conn.execute(
                "SELECT * FROM (SELECT * FROM events ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
                (n,),
            )
        rows = [dict(row) for row in cursor.fetchall()]
        return rows

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

    def get_agent_capability_scores(self, issue_dict: Dict[str, Any]) -> Dict[str, float]:
        """
        Calculate capability scores for idle agents based on their track record with similar issues.

        Similarity is determined by:
        - Same project (exact match)
        - Same issue type (task/bug/feature)
        - Title keyword overlap (basic keyword matching)

        Scoring formula:
        Score = (same_project_completions * 3) + (same_type_completions * 2) + (keyword_overlap_completions * 1)

        Args:
            issue_dict: Issue dictionary containing 'project', 'type', 'title' fields

        Returns:
            Dict mapping agent_id to capability score (float)
        """
        if not self.conn:
            raise RuntimeError("Database not connected")

        # Get idle agents
        idle_agents = self.get_idle_agents()
        if not idle_agents:
            return {}

        # Extract issue characteristics
        issue_project = issue_dict.get("project", "")
        issue_type = issue_dict.get("type", "")
        issue_title = issue_dict.get("title", "")

        # Simple keyword extraction - use module-level constant to avoid per-call allocation
        issue_keywords = set(
            [word.lower().strip(".,!?;:()[]{}\"'") for word in issue_title.split() if len(word) > 2 and word.lower() not in _STOP_WORDS]
        )

        # Batch query: get completed issues for ALL idle agents in one query
        idle_agent_ids = [a["id"] for a in idle_agents]
        placeholders = ",".join("?" * len(idle_agent_ids))
        cursor = self.conn.execute(
            f"""SELECT DISTINCT e.agent_id, i.project, i.type, i.title
               FROM events e JOIN issues i ON e.issue_id = i.id
               WHERE e.agent_id IN ({placeholders})
                 AND e.event_type IN ('done', 'finalized', 'completed')""",
            idle_agent_ids,
        )

        # Group results by agent_id
        agent_completed = defaultdict(list)
        for row in cursor:
            agent_completed[row["agent_id"]].append(dict(row))

        scores = {}

        # Process each idle agent using the batched data
        for agent in idle_agents:
            agent_id = agent["id"]
            completed_issues = agent_completed[agent_id]

            if not completed_issues:
                # No track record, score = 0
                scores[agent_id] = 0.0
                continue

            same_project_count = 0
            same_type_count = 0
            keyword_overlap_count = 0

            for completed in completed_issues:
                completed_project = completed["project"] or ""
                completed_type = completed["type"] or ""
                completed_title = completed["title"] or ""

                # Same project check (exact match)
                if issue_project and completed_project == issue_project:
                    same_project_count += 1

                # Same type check (exact match)
                if issue_type and completed_type == issue_type:
                    same_type_count += 1

                # Keyword overlap check - use module-level constant
                completed_keywords = set(
                    [
                        word.lower().strip(".,!?;:()[]{}\"'")
                        for word in completed_title.split()
                        if len(word) > 2 and word.lower() not in _STOP_WORDS
                    ]
                )

                if issue_keywords and completed_keywords:
                    overlap = len(issue_keywords.intersection(completed_keywords))
                    if overlap > 0:
                        keyword_overlap_count += 1

            # Calculate final score
            score = (same_project_count * 3.0) + (same_type_count * 2.0) + (keyword_overlap_count * 1.0)
            scores[agent_id] = score

        return scores

    def get_token_usage(
        self,
        issue_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Get aggregated token usage from 'tokens_used' events.

        Args:
            issue_id: Filter by specific issue ID (optional)
            agent_id: Filter by specific agent ID (optional)

        Returns:
            Dict with aggregated token counts and cost estimates
        """
        if not self.conn:
            raise RuntimeError("Database not connected")

        conditions = ["event_type = 'tokens_used'"]
        params = []

        if issue_id:
            conditions.append("issue_id = ?")
            params.append(issue_id)
        if agent_id:
            conditions.append("agent_id = ?")
            params.append(agent_id)

        where_clause = " AND ".join(conditions)

        # Get totals using json_extract
        totals_query = f"""
            SELECT
                COALESCE(SUM(json_extract(detail, '$.input_tokens')), 0) as total_input,
                COALESCE(SUM(json_extract(detail, '$.output_tokens')), 0) as total_output
            FROM events
            WHERE {where_clause} AND json_valid(detail)
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
                    issue_id,
                    COALESCE(SUM(json_extract(detail, '$.input_tokens')), 0) as input_tokens,
                    COALESCE(SUM(json_extract(detail, '$.output_tokens')), 0) as output_tokens
                FROM events
                WHERE {where_clause} AND issue_id IS NOT NULL AND json_valid(detail)
                GROUP BY issue_id
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
                    agent_id,
                    COALESCE(SUM(json_extract(detail, '$.input_tokens')), 0) as input_tokens,
                    COALESCE(SUM(json_extract(detail, '$.output_tokens')), 0) as output_tokens
                FROM events
                WHERE {where_clause} AND agent_id IS NOT NULL AND json_valid(detail)
                GROUP BY agent_id
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
                COALESCE(json_extract(detail, '$.model'), 'unknown') as model,
                COALESCE(SUM(json_extract(detail, '$.input_tokens')), 0) as input_tokens,
                COALESCE(SUM(json_extract(detail, '$.output_tokens')), 0) as output_tokens
            FROM events
            WHERE {where_clause} AND json_valid(detail)
            GROUP BY json_extract(detail, '$.model')
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

    def add_note(self, issue_id: Optional[str] = None, agent_id: Optional[str] = None, content: str = "", category: str = "discovery") -> int:
        """
        Insert a note and return its ID.

        Args:
            issue_id: Which issue the note was discovered during. None = project-wide note.
            agent_id: Which agent wrote it. None = Queen-authored or system note.
            content: The note text. Short — typically 1-3 sentences.
            category: One of 'discovery', 'gotcha', 'dependency', 'pattern', 'context'.

        Returns:
            The ID of the inserted note
        """
        if not self.conn:
            raise RuntimeError("Database not connected")

        cursor = self.conn.execute(
            "INSERT INTO notes (issue_id, agent_id, category, content) VALUES (?, ?, ?, ?)", (issue_id, agent_id, category, content)
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_notes(self, issue_id: Optional[str] = None, category: Optional[str] = None, limit: int = 20) -> List[Dict]:
        """
        Retrieve notes with optional filtering. Returns newest first.

        Args:
            issue_id: Filter by specific issue ID (optional)
            category: Filter by specific category (optional)
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
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def get_notes_for_molecule(self, parent_id: str) -> List[Dict]:
        """
        Get all notes from issues that share a parent molecule. For predecessor context.

        Args:
            parent_id: Parent molecule issue ID

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

    def get_completed_molecule_steps(self, parent_id: str) -> List[Dict]:
        """
        Get completed/finalized sibling issues for a molecule, ordered by creation time.

        Args:
            parent_id: Parent molecule issue ID

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

    def get_recent_project_notes(self, limit: int = 10) -> List[Dict]:
        """
        Get recent project-wide notes plus recent cross-issue notes.

        Args:
            limit: Maximum number of notes to return

        Returns:
            List of note dicts, ordered by newest first
        """
        if not self.conn:
            raise RuntimeError("Database not connected")

        rows = self.conn.execute(
            """
            SELECT * FROM notes 
            ORDER BY created_at DESC 
            LIMIT ?
        """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]
