"""HiveCLI class with all command methods."""

import json
import shlex
import sys
import time
from functools import wraps
from pathlib import Path

from rich.console import Console

from ..daemon import HiveDaemon
from ..db import Database, normalize_tags
from ..status import IssueStatus, UNBLOCKING_ISSUE_STATUSES
from ._helpers import _build_refinery_info, _check_merge_blockers
from .formatters import (
    _fmt_add_note,
    _fmt_create,
    _fmt_debug,
    _fmt_list_agents,
    _fmt_list_issues,
    _fmt_logs,
    _fmt_merges,
    _fmt_message,
    _fmt_metrics,
    _fmt_review,
    _fmt_show,
    _fmt_start,
    _fmt_status,
    _fmt_stop,
)
from .helpers import _enrich_agents_with_issues
from .queen import QueenMixin

_CONSOLE = Console()


def cli_command(*, formatter):
    """Decorator that separates data-gathering from output formatting.

    Decorated methods return a JSON-serialisable dict — nothing else.
    The decorator routes the result to either ``json.dumps`` (when ``--json``
    is active) or the provided *formatter* function (for human-readable text).

    ``json_mode`` is popped from kwargs by the decorator so the wrapped
    method never sees it.  Methods that handle output inline (e.g.
    streaming follow mode) can read ``self._json_mode`` instead.
    """

    def decorator(fn):
        @wraps(fn)
        def wrapper(self, *args, **kwargs):
            json_mode = kwargs.pop("json_mode", False)
            return self.run_command(fn.__name__, *args, json_mode=json_mode, **kwargs)

        setattr(wrapper, "_cli_raw", fn)
        setattr(wrapper, "_cli_formatter", formatter)

        return wrapper

    return decorator


class HiveCLI(QueenMixin):
    """Command-line interface for Hive orchestrator."""

    def __init__(self, db: Database, project_path: str):
        self.db = db
        self.project_path = Path(project_path).resolve()
        self.project_name = self.project_path.name

    def _handle_command_error(self, exc: Exception, *, json_mode: bool = False):
        """Print a CLI-friendly error and exit."""
        if json_mode:
            print(json.dumps({"error": str(exc)}))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    @classmethod
    def _get_command_handler(cls, command_name: str):
        """Return the decorated handler method for a command."""
        method = getattr(cls, command_name, None)
        if method is None or not callable(method):
            raise ValueError(f"Unknown CLI command: {command_name}")
        return method

    def invoke_raw(self, command_name: str, *args, json_mode: bool = False, **kwargs):
        """Run a command's data-gathering function without formatting or error trapping."""
        self._json_mode = json_mode
        method = self._get_command_handler(command_name)
        raw = getattr(method, "_cli_raw", None)
        if raw is None:
            raise ValueError(f"Command does not support raw invocation: {command_name}")
        return raw(self, *args, **kwargs)

    def render_result(self, command_name: str, result: dict | None, *, json_mode: bool = False, formatter=None):
        """Emit a command result using JSON or the registered formatter."""
        if result is None:
            return None
        if json_mode:
            print(json.dumps(result, default=str))
            return result

        method = self._get_command_handler(command_name)
        render = formatter or getattr(method, "_cli_formatter", None)
        if render is None:
            raise ValueError(f"Command does not define a formatter: {command_name}")
        output = render(result)
        if output:
            if isinstance(output, str):
                print(output)
            else:
                _CONSOLE.print(output)
        return result

    def run_command(self, command_name: str, *args, json_mode: bool = False, formatter=None, **kwargs):
        """Run a command through the standard CLI error/formatting pipeline."""
        try:
            result = self.invoke_raw(command_name, *args, json_mode=json_mode, **kwargs)
            return self.render_result(command_name, result, json_mode=json_mode, formatter=formatter)
        except Exception as exc:
            self._handle_command_error(exc, json_mode=json_mode)

    def _error(self, msg: str, *, json_mode: bool = False):
        """Print error and exit."""
        if json_mode:
            print(json.dumps({"error": msg}))
        else:
            print(f"Error: {msg}", file=sys.stderr)
        sys.exit(1)

    def _parse_tags(self, issue_dict: dict) -> dict:
        """Parse tags JSON string into a list in-place."""
        if issue_dict.get("tags"):
            try:
                issue_dict["tags"] = json.loads(issue_dict["tags"])
            except (json.JSONDecodeError, TypeError):
                issue_dict["tags"] = []
        else:
            issue_dict["tags"] = []
        return issue_dict

    def _require_issue(self, issue_id: str) -> dict:
        """Fetch issue by ID or raise ValueError if not found."""
        issue = self.db.get_issue(issue_id)
        if not issue:
            raise ValueError(f"Issue not found: {issue_id}")
        return issue

    # Map user-facing sort names to SQL column names
    _SORT_COLUMNS = {
        "priority": "priority",
        "created": "created_at",
        "updated": "updated_at",
        "status": "status",
        "title": "title",
    }

    # ── Issue management ─────────────────────────────────────────────

    @cli_command(formatter=_fmt_create)
    def create(
        self,
        title: str,
        description: str = "",
        priority: int = 2,
        issue_type: str = "task",
        model: str | None = None,
        tags: str | None = None,
        depends_on: list | None = None,
    ):
        """Create a new issue."""
        tag_list = [t.strip() for t in tags.split(",")] if tags else None
        issue_id = self.db.create_issue(
            title=title,
            description=description,
            priority=priority,
            issue_type=issue_type,
            project=self.project_name,
            model=model,
            tags=tag_list,
            depends_on=depends_on,
        )

        return {
            "id": issue_id,
            "issue_id": issue_id,  # compat alias
            "title": title,
            "priority": priority,
            "status": IssueStatus.OPEN.value,
            "tags": tag_list or [],
            "depends_on": depends_on or [],
            "message": f"Created issue {issue_id}: {title}",
        }

    _DONE_STATUSES = UNBLOCKING_ISSUE_STATUSES

    @cli_command(formatter=_fmt_list_issues)
    def list_issues(
        self,
        status: str | None = None,
        sort_by: str = "priority",
        reverse: bool = False,
        issue_type: str | None = None,
        assignee: str | None = None,
        limit: int = 50,
        todo: bool = False,
    ):
        """List all issues."""
        exclude = self._DONE_STATUSES if todo else None
        rows = self.db.list_issues(
            project=self.project_name,
            status=None if todo else status,
            assignee=assignee,
            issue_type=issue_type,
            exclude_statuses=exclude,
            sort=sort_by,
            reverse=reverse,
            limit=limit,
        )
        issues = [self._parse_tags(issue) for issue in rows]
        return {"count": len(issues), "issues": issues}

    @cli_command(formatter=_fmt_show)
    def show(self, issue_id: str):
        """Show issue details and events."""
        issue = self._require_issue(issue_id)

        dependencies = self.db.get_dependencies(issue_id)
        dependents = self.db.get_dependents(issue_id)
        events = self.db.get_events(issue_id=issue_id, limit=10)

        issue_dict = dict(issue)
        self._parse_tags(issue_dict)

        return {
            **issue_dict,
            "dependencies": dependencies,
            "dependents": dependents,
            "recent_events": events,
        }

    @cli_command(formatter=_fmt_message)
    def update(
        self,
        issue_id: str,
        title: str | None = None,
        description: str | None = None,
        priority: int | None = None,
        status: str | None = None,
        model: str | None = None,
        tags: str | None = None,
    ):
        """Update an issue."""
        self._require_issue(issue_id)

        tag_list = [t.strip() for t in tags.split(",")] if tags is not None else None

        updates = []
        params = []

        if title is not None:
            updates.append("title = ?")
            params.append(title)
        if description is not None:
            updates.append("description = ?")
            params.append(description)
        if priority is not None:
            updates.append("priority = ?")
            params.append(priority)
        if status is not None:
            updates.append("status = ?")
            params.append(status)
            # When re-opening an issue, clear the assignee so it
            # re-enters the ready queue and can be claimed by a new agent.
            if status == IssueStatus.OPEN:
                updates.append("assignee = NULL")
        if model is not None:
            updates.append("model = ?")
            params.append(model)
        if tag_list is not None:
            validated_tags = normalize_tags(tag_list)
            updates.append("tags = ?")
            params.append(json.dumps(validated_tags))

        if updates:
            query = f"UPDATE issues SET {', '.join(updates)}, updated_at = datetime('now') WHERE id = ?"
            params.append(issue_id)
            with self.db.transaction() as conn:
                conn.execute(query, params)

            self.db.log_event(
                issue_id,
                None,
                "updated",
                {
                    "fields": [
                        k
                        for k, v in [
                            ("title", title),
                            ("description", description),
                            ("priority", priority),
                            ("status", status),
                            ("model", model),
                            ("tags", tags),
                        ]
                        if v is not None
                    ]
                },
            )

        return {"issue_id": issue_id, "message": f"Updated issue {issue_id}"}

    @cli_command(formatter=_fmt_message)
    def cancel(self, issue_id: str, reason: str = ""):
        """Cancel an issue."""
        self._require_issue(issue_id)

        self.db.try_transition_issue_status(issue_id, to_status=IssueStatus.CANCELED)
        self.db.log_event(issue_id, None, "canceled", {"reason": reason})

        return {
            "issue_id": issue_id,
            "status": IssueStatus.CANCELED.value,
            "reason": reason,
            "message": f"Canceled issue {issue_id}",
        }

    @cli_command(formatter=_fmt_message)
    def finalize(self, issue_id: str, resolution: str = ""):
        """Finalize/close an issue."""
        self._require_issue(issue_id)

        self.db.try_transition_issue_status(issue_id, to_status=IssueStatus.FINALIZED)
        # If this issue was sitting in the merge queue (manual review mode),
        # mark those entries complete so they don't get processed later.
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE merge_queue
                SET status = 'merged', completed_at = datetime('now')
                WHERE issue_id = ? AND status IN ('queued', 'running')
                """,
                (issue_id,),
            )
        self.db.log_event(issue_id, None, "finalized", {"resolution": resolution})

        return {
            "issue_id": issue_id,
            "status": IssueStatus.FINALIZED.value,
            "resolution": resolution,
            "message": f"Finalized issue {issue_id}",
        }

    @cli_command(formatter=_fmt_review)
    def review(self, issue_id: str | None = None, limit: int = 20):
        """List done issues that are pending finalization with review hints.

        If issue_id is given, show review info for that specific issue regardless of status.
        """
        raw_rows = self.db.get_review_queue(project=self.project_name, issue_id=issue_id, limit=limit)

        rows = []
        project_q = shlex.quote(str(self.project_path))
        for item in raw_rows:
            branch = item.get("branch_name")
            worktree = item.get("worktree")
            iid = item["id"]

            item["diff_hint"] = None
            item["merge_hint"] = None
            item["finalize_hint"] = f'hive finalize {iid} --resolution "manual review complete"'

            if branch:
                branch_q = shlex.quote(branch)
                item["diff_hint"] = f"git -C {project_q} diff main...{branch_q}"
                item["merge_hint"] = f"git -C {project_q} merge --ff-only {branch_q}"
            if worktree:
                item["worktree_hint"] = f"git -C {shlex.quote(worktree)} log --oneline -n 5"
            else:
                item["worktree_hint"] = None

            rows.append(item)

        if issue_id and not rows:
            raise ValueError(f"Issue {issue_id} not found in project {self.project_name}")

        return {"count": len(rows), "detail": bool(issue_id), "review": rows}

    @cli_command(formatter=_fmt_message)
    def retry(self, issue_id: str, notes: str = "", reset: bool = False):
        """Retry an escalated/blocked issue."""
        self._require_issue(issue_id)

        # Reset to open and unassign
        self.db.try_transition_issue_status(issue_id, to_status=IssueStatus.OPEN)

        if reset:
            self.db.log_event(issue_id, None, "retry_reset", {"notes": notes})

        self.db.log_event(issue_id, None, "manual_retry", {"notes": notes})

        msg = f"Reset issue {issue_id} to '{IssueStatus.OPEN.value}' for retry"
        if reset:
            msg += " (counters reset)"

        return {
            "issue_id": issue_id,
            "status": IssueStatus.OPEN.value,
            "notes": notes,
            "reset": reset,
            "message": msg,
        }

    @cli_command(formatter=_fmt_message)
    def dep_add(self, issue_id: str, depends_on: str, dep_type: str = "blocks"):
        """Add a dependency between issues."""
        # Verify both issues exist
        if not self.db.get_issue(issue_id):
            raise ValueError(f"Issue not found: {issue_id}")
        if not self.db.get_issue(depends_on):
            raise ValueError(f"Dependency not found: {depends_on}")

        self.db.add_dependency(issue_id, depends_on, dep_type)

        return {
            "issue_id": issue_id,
            "depends_on": depends_on,
            "type": dep_type,
            "message": f"Added {dep_type} dependency: {issue_id} depends on {depends_on}",
        }

    @cli_command(formatter=_fmt_message)
    def dep_remove(self, issue_id: str, depends_on: str):
        """Remove a dependency between issues."""
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM dependencies WHERE issue_id = ? AND depends_on = ?",
                (issue_id, depends_on),
            )

        return {
            "issue_id": issue_id,
            "depends_on": depends_on,
            "message": f"Removed dependency: {issue_id} no longer depends on {depends_on}",
        }

    @cli_command(formatter=_fmt_merges)
    def merges(self, status: str | None = None):
        """List merge queue entries."""
        entries = self.db.list_merge_entries(project=self.project_name, status=status, limit=50)

        # Compute per-status counts from the result set
        status_counts: dict[str, int] = {}
        for e in entries:
            s = e.get("status") or "unknown"
            status_counts[s] = status_counts.get(s, 0) + 1

        return {"count": len(entries), "status_counts": status_counts, "merges": entries}

    @cli_command(formatter=_fmt_status)
    def status(self):
        """Show orchestrator status."""
        status_counts = self.db.get_issue_status_counts(project=self.project_name)

        # Get active agents with issue titles
        active_agents = self.db.get_active_agents(project=self.project_name)
        workers_detail = _enrich_agents_with_issues(self.db, active_agents)

        # Get running merge entry (refinery status proxy)
        refinery_info = _build_refinery_info(self.db, self.project_name)

        # Get ready queue
        ready = self.db.get_ready_queue(limit=10)

        # Get merge queue stats
        merge_stats = self.db.get_merge_queue_stats(project=self.project_name)

        # Merge preflight visibility: report when dirty main worktree blocks merges.
        main_worktree, merge_blockers = _check_merge_blockers(str(self.project_path), merge_stats)

        # Get daemon status
        daemon = self._make_daemon()
        daemon_status = daemon.status()

        # Surface issues needing human attention
        attention_issues = self.db.get_escalated_issues(project=self.project_name)

        return {
            "project": self.project_name,
            "issues": status_counts,
            "total_issues": sum(status_counts.values()),
            "active_agents": len(active_agents),
            "workers": workers_detail,
            "refinery": refinery_info,
            "ready_queue": len(ready),
            "merge_queue": merge_stats,
            "merge_blockers": merge_blockers,
            "main_worktree": main_worktree,
            "ready_issues": [{"id": i["id"], "title": i["title"]} for i in ready[:5]],
            "attention_issues": attention_issues,
            "daemon": {
                "running": daemon_status.get("running", False),
                "pid": daemon_status.get("pid"),
                "log_file": daemon_status.get("log_file"),
            },
        }

    @cli_command(formatter=_fmt_list_agents)
    def list_agents(self, agent_id: str | None = None, status: str | None = None):
        """List agents, or show details for a specific agent if agent_id is provided."""
        # If agent_id is provided, show that agent's details
        if agent_id:
            agent = self.db.get_agent(agent_id)
            if not agent:
                raise ValueError(f"Agent not found: {agent_id}")

            # Get current issue details
            if agent.get("current_issue"):
                issue = self.db.get_issue(agent["current_issue"])
                agent["current_issue_details"] = issue

            # Get recent events for this agent
            agent["recent_events"] = self.db.get_events(agent_id=agent_id, limit=10)

            return agent

        # Otherwise, list all agents
        agents = self.db.list_agents(project=self.project_name, status=status)

        # Enrich with current issue info
        for agent, worker in zip(agents, _enrich_agents_with_issues(self.db, agents)):
            if worker["issue_title"]:
                agent["current_issue_title"] = worker["issue_title"]

        return {"count": len(agents), "agents": agents}

    # ── Notes ─────────────────────────────────────────────────────────

    @cli_command(formatter=_fmt_add_note)
    def add_note(self, content: str, issue_id: str | None = None, category: str = "discovery"):
        """Add a note to the knowledge base."""
        note_id = self.db.add_note(agent_id=None, issue_id=issue_id, content=content, category=category, project=self.project_name)

        return {
            "note_id": note_id,
            "content": content,
            "category": category,
            "issue_id": issue_id,
            "message": f"Added note #{note_id}",
        }

    # ── Event log (tail-style, not tool-backed) ─────────────────────

    def _format_event(self, event: dict) -> str:
        """Format a single event as a log line."""
        ts = event["created_at"]
        etype = event["event_type"]
        issue = event["issue_id"] or "-"
        agent = event["agent_id"] or "-"

        line = f"{ts}  {etype:<24s}  issue={issue:<10s}  agent={agent:<10s}"

        if event["detail"]:
            try:
                detail = json.loads(event["detail"])
                parts = [f"{k}={v}" for k, v in detail.items()]
                line += "  " + " ".join(parts)
            except (json.JSONDecodeError, TypeError):
                line += f"  {event['detail']}"

        return line

    @staticmethod
    def _event_to_json(event: dict) -> dict:
        """Prepare an event dict for JSON serialisation.

        Parses the ``detail`` field from a JSON string into a real object
        so the output is a proper nested structure rather than an escaped
        string.
        """
        out = dict(event)
        if out.get("detail"):
            try:
                out["detail"] = json.loads(out["detail"]) if isinstance(out["detail"], str) else out["detail"]
            except (json.JSONDecodeError, TypeError):
                pass  # keep as-is
        return out

    @cli_command(formatter=_fmt_logs)
    def logs(
        self,
        follow: bool = False,
        n: int = 20,
        issue_id: str | None = None,
        agent_id: str | None = None,
        event_type: str | None = None,
        daemon: bool = False,
    ):
        """Show event log, optionally tailing for new events."""
        # --daemon flag: delegate to daemon log handler (handles output itself)
        if daemon:
            daemon_obj = self._make_daemon()
            daemon_obj.logs(lines=n, follow=follow)
            return None

        recent = self.db.get_recent_events(n=n, issue_id=issue_id, agent_id=agent_id, event_type=event_type)

        # Follow/streaming mode: handle output inline, return None to skip decorator output.
        # Uses self._json_mode (set by @cli_command decorator) to choose text vs JSONL.
        if follow:
            json_mode = self._json_mode
            for event in recent:
                if json_mode:
                    print(json.dumps(self._event_to_json(event), default=str))
                else:
                    print(self._format_event(event))
            cursor = recent[-1]["id"] if recent else self.db.get_max_event_id()
            try:
                while True:
                    time.sleep(0.5)
                    new_events = self.db.get_events_since(after_id=cursor, issue_id=issue_id, agent_id=agent_id, event_type=event_type)
                    for event in new_events:
                        if json_mode:
                            print(json.dumps(self._event_to_json(event), default=str))
                        else:
                            print(self._format_event(event))
                        cursor = event["id"]
            except KeyboardInterrupt:
                pass
            return None

        # Non-follow: return events for decorator to format/serialize
        return {"events": [dict(e) for e in recent]}

    @cli_command(formatter=_fmt_debug)
    def debug(self):
        """Print a full diagnostic report."""
        from ..diag import gather_report

        return gather_report(self.db, str(self.project_path))

    @cli_command(formatter=_fmt_metrics)
    def metrics(self, model=None, tag=None, issue_type=None, group_by=None, show_costs=False, issue_id=None, agent_id=None):
        """Show aggregated agent run metrics."""
        # If --group-by is specified, use the stats-style output
        if group_by:
            results = self.db.get_model_performance(model=model, tag=tag, group_by=group_by)
            group_label = "Tag" if group_by == "tag" else "Type"
            group_key = "tag" if group_by == "tag" else "type"
            return {"view": "group_by", "results": results, "group_label": group_label, "group_key": group_key}

        # If --costs is specified, show token usage data
        if show_costs:
            usage = self.db.get_token_usage(issue_id=issue_id, agent_id=agent_id, project=self.project_name)
            return {"view": "costs", "issue_id": issue_id, "agent_id": agent_id, **usage}

        # Default metrics output
        results = self.db.get_metrics(model=model, tag=tag, issue_type=issue_type, project=self.project_name)

        total_runs = sum(r["runs"] for r in results)
        total_escalations = sum(r["escalated_count"] for r in results)
        escalation_rate = round(100.0 * total_escalations / total_runs, 1) if total_runs > 0 else 0
        total_duration_weighted = sum(r["avg_duration_s"] * r["runs"] for r in results if r["avg_duration_s"])
        mean_duration_s = total_duration_weighted / total_runs if total_runs > 0 else 0
        mean_duration_m = round(mean_duration_s / 60, 1)

        return {
            "view": "default",
            "metrics": results,
            "summary": {
                "escalation_rate": escalation_rate,
                "mean_time_to_resolution_minutes": mean_duration_m,
                "total_runs": total_runs,
            },
        }

    # ── Daemon management ────────────────────────────────────────────

    def _make_daemon(self) -> HiveDaemon:
        return HiveDaemon(db_path=self.db.db_path)

    @cli_command(formatter=_fmt_start)
    def start(self, foreground: bool = False):
        """Start the hive daemon."""
        if foreground:
            from ..daemon import run_daemon_foreground

            run_daemon_foreground(self.db)
            return None

        daemon = self._make_daemon()
        status = daemon.status()
        if status["running"]:
            return {"status": "already_running", "pid": status["pid"]}

        started = daemon.start()
        if started:
            ds = daemon.status()
            return {"status": "started", "pid": ds["pid"], "log_file": ds.get("log_file")}

        log_tail = ""
        try:
            if daemon.log_file.exists():
                lines = daemon.log_file.read_text().strip().splitlines()
                log_tail = "\n".join(lines[-10:])
        except OSError:
            pass
        raise RuntimeError(f"Failed to start daemon. Log: {daemon.log_file}\n{log_tail}".rstrip())

    @cli_command(formatter=_fmt_stop)
    def stop(self):
        """Stop the hive daemon."""
        daemon = self._make_daemon()
        status = daemon.status()
        if not status["running"]:
            return {"status": "not_running"}
        pid = status["pid"]
        stopped = daemon.stop()
        if stopped:
            return {"status": "stopped", "pid": pid}
        raise RuntimeError(f"Failed to stop daemon (PID {pid})")
