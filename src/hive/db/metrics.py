"""Metrics mixin: token usage, model performance, and event counts."""

import logging

from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class MetricsMixin:
    def _build_token_select(
        self,
        from_clause: str,
        where_clause: str,
        params: list,
        select_extra: str = "",
        extra_where: str = "",
        group_by: str = "",
    ) -> list:
        """Execute a token aggregation query with common SELECT columns.

        Args:
            from_clause: The FROM ... clause (table + joins)
            where_clause: The base WHERE conditions (joined with AND)
            params: Query parameters for the base WHERE clause
            select_extra: Optional extra SELECT expression (e.g. "e.issue_id")
            extra_where: Optional extra AND condition appended to WHERE
            group_by: Optional GROUP BY expression

        Returns:
            List of sqlite3.Row objects
        """
        extra_select = f"{select_extra}, " if select_extra else ""
        extra_cond = f" AND {extra_where}" if extra_where else ""
        group = f"\n            GROUP BY {group_by}" if group_by else ""
        query = f"""
            SELECT
                {extra_select}COALESCE(SUM(json_extract(e.detail, '$.input_tokens')), 0) as input_tokens,
                COALESCE(SUM(json_extract(e.detail, '$.output_tokens')), 0) as output_tokens
            {from_clause}
            WHERE {where_clause} AND json_valid(e.detail){extra_cond}{group}
        """
        cursor = self.conn.execute(query, params)
        return cursor.fetchall()

    def get_token_usage(self, issue_id: Optional[str] = None, agent_id: Optional[str] = None, project: Optional[str] = None) -> Dict[str, Any]:
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

        # Totals
        totals_rows = self._build_token_select(from_clause, where_clause, params)
        totals_row = totals_rows[0]
        total_input_tokens = totals_row["input_tokens"]
        total_output_tokens = totals_row["output_tokens"]
        total_tokens = total_input_tokens + total_output_tokens

        # Breakdown by issue
        issue_breakdown = {}
        if issue_id is None:
            rows = self._build_token_select(
                from_clause,
                where_clause,
                params,
                select_extra="e.issue_id",
                extra_where="e.issue_id IS NOT NULL",
                group_by="e.issue_id",
            )
            for row in rows:
                issue_breakdown[row["issue_id"]] = {"input_tokens": row["input_tokens"], "output_tokens": row["output_tokens"]}
        elif issue_id:
            issue_breakdown[issue_id] = {"input_tokens": total_input_tokens, "output_tokens": total_output_tokens}

        # Breakdown by agent
        agent_breakdown = {}
        if agent_id is None:
            rows = self._build_token_select(
                from_clause,
                where_clause,
                params,
                select_extra="e.agent_id",
                extra_where="e.agent_id IS NOT NULL",
                group_by="e.agent_id",
            )
            for row in rows:
                agent_breakdown[row["agent_id"]] = {"input_tokens": row["input_tokens"], "output_tokens": row["output_tokens"]}
        elif agent_id:
            agent_breakdown[agent_id] = {"input_tokens": total_input_tokens, "output_tokens": total_output_tokens}

        # Breakdown by model
        rows = self._build_token_select(
            from_clause,
            where_clause,
            params,
            select_extra="COALESCE(json_extract(e.detail, '$.model'), 'unknown') as model",
            group_by="json_extract(e.detail, '$.model')",
        )
        model_breakdown = {row["model"]: {"input_tokens": row["input_tokens"], "output_tokens": row["output_tokens"]} for row in rows}

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
                SUM(CASE WHEN i.status = 'escalated' THEN 1 ELSE 0 END) as escalations,
                (SELECT COUNT(*) FROM events e2 WHERE e2.issue_id = i.id AND e2.event_type = 'retry') as total_retries,
                COALESCE(SUM(CAST(json_extract(e.detail, '$.input_tokens') AS INTEGER)), 0) as total_input_tokens,
                COALESCE(SUM(CAST(json_extract(e.detail, '$.output_tokens') AS INTEGER)), 0) as total_output_tokens,
                ROUND(AVG(
                    (julianday(COALESCE(i.closed_at, datetime('now'))) - julianday(i.created_at)) * 24 * 60
                ), 1) as avg_duration_minutes
            {from_clause}
            WHERE 1=1
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
        return self._all(cursor)

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
            - incomplete_count: Number of incomplete runs
            - escalated_count: Number of escalated runs
            - success_rate: Success percentage
            - avg_duration_s: Average duration in seconds
            - avg_retries: Average retry count
            - merge_health: Percentage of runs with clean merge (tests_passed / (tests_passed + test_failure + rebase_conflict))
        """
        if not self.conn:
            raise RuntimeError("Database not connected")

        # Build outer query conditions
        outer_join = ""
        conditions: list = []
        params: list = []

        if project is not None:
            outer_join = " JOIN issues i ON ar.issue_id = i.id"
            if project:
                conditions.append("i.project = ?")
                params.append(project)
        if model:
            conditions.append("ar.model = ?")
            params.append(model)
        if tag:
            conditions.append("ar.tags LIKE ?")
            params.append(f'%"{tag}"%')
        if issue_type:
            conditions.append("ar.issue_type = ?")
            params.append(issue_type)

        where_clause = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        # Build merge-health subquery conditions (same project/tag/issue_type filters, no model filter)
        mh_join = ""
        mh_conditions: list = ["e.event_type IN ('tests_passed', 'test_failure', 'rebase_conflict')"]
        mh_params: list = []

        if project is not None:
            mh_join = " JOIN issues i2 ON ar2.issue_id = i2.id"
            if project:
                mh_conditions.append("i2.project = ?")
                mh_params.append(project)
        if tag:
            mh_conditions.append("ar2.tags LIKE ?")
            mh_params.append(f'%"{tag}"%')
        if issue_type:
            mh_conditions.append("ar2.issue_type = ?")
            mh_params.append(issue_type)

        mh_where = " AND ".join(mh_conditions)

        query = f"""
            SELECT
                ar.model,
                COUNT(*) as runs,
                SUM(CASE WHEN ar.outcome = 'done' THEN 1 ELSE 0 END) as success_count,
                SUM(CASE WHEN ar.outcome = 'incomplete' THEN 1 ELSE 0 END) as incomplete_count,
                SUM(CASE WHEN ar.outcome = 'escalated' THEN 1 ELSE 0 END) as escalated_count,
                ROUND(100.0 * SUM(CASE WHEN ar.outcome = 'done' THEN 1 ELSE 0 END) / COUNT(*), 1) as success_rate,
                ROUND(AVG(ar.duration_s), 1) as avg_duration_s,
                ROUND(AVG(ar.retry_count), 1) as avg_retries,
                COALESCE(mh.tests_passed, 0) as tests_passed,
                COALESCE(mh.test_failure, 0) as test_failure,
                COALESCE(mh.rebase_conflict, 0) as rebase_conflict
            FROM agent_runs ar{outer_join}
            LEFT JOIN (
                SELECT
                    ar2.model,
                    SUM(CASE WHEN e.event_type = 'tests_passed' THEN 1 ELSE 0 END) as tests_passed,
                    SUM(CASE WHEN e.event_type = 'test_failure' THEN 1 ELSE 0 END) as test_failure,
                    SUM(CASE WHEN e.event_type = 'rebase_conflict' THEN 1 ELSE 0 END) as rebase_conflict
                FROM events e
                JOIN agent_runs ar2 ON e.issue_id = ar2.issue_id{mh_join}
                WHERE {mh_where}
                GROUP BY ar2.model
            ) mh ON mh.model = ar.model
            {where_clause}
            GROUP BY ar.model
            ORDER BY runs DESC
        """

        # Subquery params come first in SQL text, then outer WHERE params
        cursor = self.conn.execute(query, mh_params + params)

        results = []
        for row in cursor.fetchall():
            r = dict(row)
            tests_passed = r.pop("tests_passed")
            test_failure = r.pop("test_failure")
            rebase_conflict = r.pop("rebase_conflict")
            total_merge_events = tests_passed + test_failure + rebase_conflict
            r["merge_health"] = round(100.0 * tests_passed / total_merge_events, 1) if total_merge_events > 0 else None
            results.append(r)

        return results

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
        return self._scalar(cursor, 0)

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
        return self._scalar(cursor, 0)

    def count_events_by_type_since_reset(self, issue_id: str, event_type: str) -> int:
        """Count events of a specific type for an issue since the last retry_reset.

        If no retry_reset event exists, counts all events (same as count_events_by_type).
        Uses event id (autoincrement) for ordering to avoid timestamp granularity issues.

        Args:
            issue_id: Issue ID to count events for
            event_type: Type of event to count (e.g., 'retry', 'agent_switch')

        Returns:
            Number of events of the specified type since the last retry_reset
        """
        cursor = self.conn.execute(
            """
            SELECT COUNT(*) FROM events
            WHERE issue_id = ? AND event_type = ?
              AND id > COALESCE(
                (SELECT MAX(id) FROM events
                 WHERE issue_id = ? AND event_type = 'retry_reset'),
                0
              )
            """,
            (issue_id, event_type, issue_id),
        )
        return self._scalar(cursor, 0)

    def count_events_in_window_after_reset(self, issue_id: str, event_type: str, minutes: int) -> int:
        """Count events within the last N minutes, but only after the most recent retry_reset.

        Combines the time-window filter with the reset watermark. If no retry_reset
        exists, behaves identically to count_events_since_minutes.

        Args:
            issue_id: Issue ID to count events for
            event_type: Type of event to count (e.g., 'incomplete')
            minutes: Look back this many minutes from now
        """
        cursor = self.conn.execute(
            """
            SELECT COUNT(*) FROM events
            WHERE issue_id = ? AND event_type = ?
              AND created_at >= datetime('now', ?)
              AND id > COALESCE(
                (SELECT MAX(id) FROM events
                 WHERE issue_id = ? AND event_type = 'retry_reset'),
                0
              )
            """,
            (issue_id, event_type, f"-{minutes} minutes", issue_id),
        )
        return self._scalar(cursor, 0)

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
        return self._scalar(cursor, 0)
