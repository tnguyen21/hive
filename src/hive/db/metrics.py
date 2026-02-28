"""Metrics mixin: token usage, model performance, and event counts."""

import logging

from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class MetricsMixin:
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
            - incomplete_count: Number of incomplete runs
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
                SUM(CASE WHEN ar.outcome = 'incomplete' THEN 1 ELSE 0 END) as incomplete_count,
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
