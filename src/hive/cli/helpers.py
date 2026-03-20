"""Shared helpers for CLI command implementations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..git import GitWorktreeError, get_worktree_dirty_status

if TYPE_CHECKING:
    from ..db import Database


def _enrich_agents_with_issues(db: Database, agents: list[dict]) -> list[dict]:
    """Return a worker-summary list with current issue titles resolved.

    For each agent dict, looks up the current_issue in the database and
    extracts its title, returning a list of ``{"name", "issue_id", "issue_title"}``
    dicts suitable for ``workers`` fields in status / global-status responses.
    """
    result = []
    for agent in agents:
        issue_title = ""
        if agent.get("current_issue"):
            issue_row = db.get_issue(agent["current_issue"])
            if issue_row:
                issue_title = issue_row.get("title", "")
        result.append(
            {
                "name": agent.get("name", "") or "",
                "issue_id": agent.get("current_issue") or "",
                "issue_title": issue_title,
            }
        )
    return result


def _build_refinery_info(db: Database, project: str) -> dict:
    """Return refinery status dict for *project*, falling back to inactive on any error."""
    try:
        running_merge = db.get_running_merge(project=project)
        return {
            "active": running_merge is not None,
            "issue_id": running_merge["issue_id"] if running_merge else None,
            "issue_title": running_merge["issue_title"] if running_merge else None,
        }
    except Exception:
        return {"active": False, "issue_id": None, "issue_title": None}


def _check_merge_blockers(path: str, merge_stats: dict) -> tuple[dict, list[dict]]:
    """Check worktree dirty status and build merge blocker list.

    Returns ``(main_worktree_info, merge_blockers)``.  A blocker entry is added
    only when the worktree is dirty *and* there are queued or running merges.
    """
    merge_blockers: list[dict] = []
    try:
        dirty, dirty_output = get_worktree_dirty_status(path)
        changes = dirty_output.splitlines()[:20] if dirty else []
        main_worktree: dict = {
            "dirty": dirty,
            "changes": changes,
            "status": "dirty" if dirty else "clean",
        }
        if dirty and (merge_stats.get("queued", 0) > 0 or merge_stats.get("running", 0) > 0):
            merge_blockers.append(
                {
                    "type": "dirty_main_worktree",
                    "message": "Merges are paused: main worktree has uncommitted tracked changes",
                    "changes": changes,
                }
            )
    except GitWorktreeError as e:
        main_worktree = {"dirty": False, "changes": [], "status": "error", "error": str(e)}
    return main_worktree, merge_blockers
