"""Global multi-project status view."""

from __future__ import annotations

from pathlib import Path

from ..daemon import HiveDaemon
from ..db import Database
from .helpers import _build_refinery_info, _check_merge_blockers, _enrich_agents_with_issues


def get_global_status(db: Database) -> dict:
    """Build a global status dict across all registered projects."""
    # Daemon status
    daemon = HiveDaemon(db_path=db.db_path)
    daemon_status = daemon.status()
    daemon_info = {
        "running": daemon_status.get("running", False),
        "pid": daemon_status.get("pid"),
        "log_file": daemon_status.get("log_file"),
    }

    projects_raw = db.list_projects()
    # Filter out stale worktree entries that were accidentally registered as projects
    projects_raw = [p for p in projects_raw if "/.worktrees/" not in p["path"] and not p["name"].startswith("worker-")]
    projects = []
    totals = {"open": 0, "in_progress": 0, "done": 0, "escalated": 0, "workers": 0}

    for proj in projects_raw:
        name = proj["name"]
        path = proj["path"]
        entry: dict = {"name": name, "path": path, "path_missing": False}

        if not Path(path).exists():
            entry["path_missing"] = True
            projects.append(entry)
            continue

        # Issue counts
        issue_counts = db.get_issue_status_counts(project=name)
        entry["issues"] = issue_counts
        entry["total_issues"] = sum(issue_counts.values())

        # Active agents with issue titles
        active_agents = db.get_active_agents(project=name)
        workers = _enrich_agents_with_issues(db, active_agents)
        entry["active_agents"] = len(active_agents)
        entry["workers"] = workers

        # Refinery status
        entry["refinery"] = _build_refinery_info(db, name)

        # Merge queue stats
        merge_stats = db.get_merge_queue_stats(project=name)
        entry["merge_queue"] = merge_stats

        # Merge blockers / worktree check
        entry["main_worktree"], entry["merge_blockers"] = _check_merge_blockers(path, merge_stats)

        # Escalated issues
        entry["attention_issues"] = db.get_escalated_issues(project=name)

        projects.append(entry)

        # Accumulate totals
        totals["open"] += issue_counts.get("open", 0)
        totals["in_progress"] += issue_counts.get("in_progress", 0)
        totals["done"] += issue_counts.get("done", 0)
        totals["escalated"] += issue_counts.get("escalated", 0)
        totals["workers"] += len(active_agents)

    return {
        "daemon": daemon_info,
        "totals": totals,
        "projects": projects,
    }
