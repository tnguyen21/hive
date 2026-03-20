"""Tests for hive.cli._helpers — _build_refinery_info and _check_merge_blockers."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from hive.cli._helpers import _build_refinery_info, _check_merge_blockers
from hive.git import GitWorktreeError


# ── _build_refinery_info ──────────────────────────────────────────────────────


class TestBuildRefineryInfo:
    def test_no_running_merge_returns_inactive(self, temp_db):
        """When no merge is running, active is False and ids are None."""
        info = _build_refinery_info(temp_db, "my-project")
        assert info == {"active": False, "issue_id": None, "issue_title": None}

    def test_running_merge_returns_active(self, temp_db):
        """When a merge is running, active is True and ids are populated."""
        issue_id = temp_db.create_issue("Merge me", project="my-project")
        temp_db.try_transition_issue_status(issue_id, to_status="done")
        temp_db.conn.execute(
            """
            INSERT INTO merge_queue (issue_id, agent_id, project, worktree, branch_name, status)
            VALUES (?, ?, ?, ?, ?, 'running')
            """,
            (issue_id, "agent-1", "my-project", "/tmp/wt", "agent/agent-1"),
        )
        temp_db.conn.commit()

        info = _build_refinery_info(temp_db, "my-project")

        assert info["active"] is True
        assert info["issue_id"] == issue_id
        assert info["issue_title"] == "Merge me"

    def test_returns_inactive_on_db_error(self):
        """Any exception from db.get_running_merge is swallowed; returns inactive."""
        db = MagicMock()
        db.get_running_merge.side_effect = RuntimeError("DB gone")

        info = _build_refinery_info(db, "proj")

        assert info == {"active": False, "issue_id": None, "issue_title": None}

    def test_project_isolation(self, temp_db):
        """Running merge for a different project does not show as active."""
        issue_id = temp_db.create_issue("Other project", project="other")
        temp_db.try_transition_issue_status(issue_id, to_status="done")
        temp_db.conn.execute(
            """
            INSERT INTO merge_queue (issue_id, agent_id, project, worktree, branch_name, status)
            VALUES (?, ?, ?, ?, ?, 'running')
            """,
            (issue_id, "agent-1", "other", "/tmp/wt", "agent/agent-1"),
        )
        temp_db.conn.commit()

        info = _build_refinery_info(temp_db, "my-project")
        assert info["active"] is False


# ── _check_merge_blockers ─────────────────────────────────────────────────────


class TestCheckMergeBlockers:
    def _init_git_repo(self, path: Path) -> None:
        """Set up a minimal git repo with one commit."""
        subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=path, check=True, capture_output=True)
        (path / "README.md").write_text("init\n")
        subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)

    def test_clean_worktree_no_blockers(self, tmp_path):
        """Clean worktree produces no blockers and clean status."""
        self._init_git_repo(tmp_path)

        main_worktree, blockers = _check_merge_blockers(str(tmp_path), {"queued": 1})

        assert main_worktree["dirty"] is False
        assert main_worktree["status"] == "clean"
        assert blockers == []

    def test_dirty_with_active_merges_produces_blocker(self, tmp_path):
        """Dirty worktree + queued merges → blocker entry added."""
        self._init_git_repo(tmp_path)
        (tmp_path / "README.md").write_text("dirty content\n")

        main_worktree, blockers = _check_merge_blockers(str(tmp_path), {"queued": 1, "running": 0})

        assert main_worktree["dirty"] is True
        assert main_worktree["status"] == "dirty"
        assert len(blockers) == 1
        assert blockers[0]["type"] == "dirty_main_worktree"
        assert "Merges are paused" in blockers[0]["message"]

    def test_dirty_without_active_merges_no_blocker(self, tmp_path):
        """Dirty worktree with zero queued/running merges → no blocker, but still dirty status."""
        self._init_git_repo(tmp_path)
        (tmp_path / "README.md").write_text("dirty content\n")

        main_worktree, blockers = _check_merge_blockers(str(tmp_path), {"queued": 0, "running": 0})

        assert main_worktree["dirty"] is True
        assert blockers == []

    def test_running_merge_also_triggers_blocker(self, tmp_path):
        """A running (not just queued) merge also triggers a blocker when dirty."""
        self._init_git_repo(tmp_path)
        (tmp_path / "README.md").write_text("dirty\n")

        _, blockers = _check_merge_blockers(str(tmp_path), {"queued": 0, "running": 1})

        assert len(blockers) == 1

    def test_blocker_changes_capped_at_20_lines(self, tmp_path):
        """Changes list in blocker is truncated to 20 lines."""
        self._init_git_repo(tmp_path)
        # Create 30 tracked files so `git status` returns many lines
        for i in range(30):
            f = tmp_path / f"file_{i}.txt"
            f.write_text("x\n")
            subprocess.run(["git", "add", str(f)], cwd=tmp_path, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", f"add {i}"], cwd=tmp_path, check=True, capture_output=True)
            f.write_text("dirty\n")

        _, blockers = _check_merge_blockers(str(tmp_path), {"queued": 1})

        assert len(blockers[0]["changes"]) <= 20

    def test_git_error_returns_error_status(self):
        """GitWorktreeError in get_worktree_dirty_status → status=error, no blockers."""
        with patch("hive.cli._helpers.get_worktree_dirty_status", side_effect=GitWorktreeError("oops")):
            main_worktree, blockers = _check_merge_blockers("/nonexistent", {"queued": 1})

        assert main_worktree["status"] == "error"
        assert "error" in main_worktree
        assert blockers == []
