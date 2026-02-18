"""Tests for mechanical merge preflight and ff-merge helpers.

Covers:
- INV-1: dirty-main pause/resume behavior
- INV-2: successful mechanical path finalizes and marks merge_queue merged
- INV-3: mechanical failures dispatch to refinery in mechanical-then-refinery mode

Critical paths:
- clean mechanical success path
- rebase conflict path to refinery
- test failure path to refinery

Failure modes:
- preflight git-status failure logs preflight error and avoids destructive actions
"""

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hive.db import Database
from hive.git import GitWorktreeError, create_worktree
from hive.merge import MergeProcessor


# ---------------------------------------------------------------------------
# Fixtures (mirrors test_merge.py conventions)
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    db = Database(db_path)
    db.connect()
    yield db
    db.close()


@pytest.fixture
def git_repo(tmp_path):
    repo_path = tmp_path / "test_repo"
    repo_path.mkdir()
    subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_path, check=True, capture_output=True)
    (repo_path / "README.md").write_text("# Test Repo\n")
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=repo_path, check=True, capture_output=True)
    return repo_path


@pytest.fixture
def mock_opencode():
    client = AsyncMock(
        spec=[
            "create_session",
            "send_message_async",
            "get_session_status",
            "get_messages",
            "abort_session",
            "cleanup_session",
        ]
    )
    return client


@pytest.fixture
def merge_entry_with_worktree(git_repo, temp_db):
    """Create a DB entry and worktree ready for merge processing."""
    agent_id = temp_db.create_agent(name="worker-test")
    issue_id = temp_db.create_issue(title="Test Feature", project="test")
    temp_db.update_issue_status(issue_id, "done")

    worktree_path = create_worktree(str(git_repo), "worker-test")
    (Path(worktree_path) / "feature.py").write_text("# new feature\n")
    subprocess.run(["git", "add", "."], cwd=worktree_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Add feature"], cwd=worktree_path, check=True, capture_output=True)

    branch_name = "agent/worker-test"
    temp_db.conn.execute(
        "INSERT INTO merge_queue (issue_id, agent_id, project, worktree, branch_name) VALUES (?, ?, ?, ?, ?)",
        (issue_id, agent_id, "test", worktree_path, branch_name),
    )
    temp_db.conn.commit()

    return {
        "git_repo": git_repo,
        "worktree_path": worktree_path,
        "issue_id": issue_id,
        "agent_id": agent_id,
        "branch_name": branch_name,
    }


# ---------------------------------------------------------------------------
# _mechanical_preflight: git-status failure avoids destructive actions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_git_error_logs_and_returns_false(temp_db, mock_opencode):
    """INV-1 / failure mode: git-status error logs preflight_error and returns False, no merge attempted."""
    mp = MergeProcessor(temp_db, mock_opencode, "/tmp/project", "test")

    with patch("hive.merge.get_worktree_dirty_status_async", side_effect=GitWorktreeError("git unavailable")):
        result = await mp._mechanical_preflight()

    assert result is False

    events = temp_db.get_events(None)
    preflight_errors = [e for e in events if e["event_type"] == "merge_preflight_error"]
    assert len(preflight_errors) == 1

    import json

    detail = json.loads(preflight_errors[0]["detail"])
    assert "git unavailable" in detail["error"]


@pytest.mark.asyncio
async def test_preflight_git_error_no_merge_attempted(merge_entry_with_worktree, temp_db, mock_opencode):
    """Preflight git-status failure avoids destructive actions (merge/rebase not called)."""
    info = merge_entry_with_worktree
    mp = MergeProcessor(temp_db, mock_opencode, str(info["git_repo"]), "test")

    with patch("hive.merge.get_worktree_dirty_status_async", side_effect=GitWorktreeError("oops")):
        with patch.object(mp, "_try_mechanical_merge", new_callable=AsyncMock) as mock_mech:
            with patch.object(mp, "_send_to_refinery", new_callable=AsyncMock) as mock_refinery:
                await mp.process_queue_once()
                mock_mech.assert_not_called()
                mock_refinery.assert_not_called()

    # Queue entry must remain queued — no destructive action was taken
    row = temp_db.conn.execute("SELECT status FROM merge_queue WHERE id = 1").fetchone()
    assert row["status"] == "queued"


# ---------------------------------------------------------------------------
# _mechanical_preflight: dirty-main pause/resume (INV-1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preflight_dirty_main_returns_false_and_logs(temp_db, mock_opencode, git_repo):
    """INV-1: dirty main causes preflight to return False and log merge_paused_dirty_main."""
    mp = MergeProcessor(temp_db, mock_opencode, str(git_repo), "test")

    # Dirty the main repo worktree
    (git_repo / "README.md").write_text("# Dirty\n")

    result = await mp._mechanical_preflight()

    assert result is False
    assert mp._main_dirty_blocked is True

    events = temp_db.get_events(None)
    paused = [e for e in events if e["event_type"] == "merge_paused_dirty_main"]
    assert len(paused) == 1

    import json

    detail = json.loads(paused[0]["detail"])
    assert "README.md" in detail["changes"]


@pytest.mark.asyncio
async def test_preflight_dirty_main_no_duplicate_log_on_repeat(temp_db, mock_opencode, git_repo):
    """INV-1: repeated dirty checks with the same snapshot do NOT re-log the paused event."""
    mp = MergeProcessor(temp_db, mock_opencode, str(git_repo), "test")

    (git_repo / "README.md").write_text("# Dirty\n")

    await mp._mechanical_preflight()
    await mp._mechanical_preflight()
    await mp._mechanical_preflight()

    events = temp_db.get_events(None)
    paused = [e for e in events if e["event_type"] == "merge_paused_dirty_main"]
    # Only the first check should produce a log entry
    assert len(paused) == 1


@pytest.mark.asyncio
async def test_preflight_resumes_when_main_clean(temp_db, mock_opencode, git_repo):
    """INV-1: once main is clean again, preflight logs merge_resumed_main_clean and returns True."""
    mp = MergeProcessor(temp_db, mock_opencode, str(git_repo), "test")

    # First: dirty
    (git_repo / "README.md").write_text("# Dirty\n")
    result_dirty = await mp._mechanical_preflight()
    assert result_dirty is False
    assert mp._main_dirty_blocked is True

    # Restore to clean
    subprocess.run(["git", "checkout", "README.md"], cwd=git_repo, check=True, capture_output=True)

    result_clean = await mp._mechanical_preflight()
    assert result_clean is True
    assert mp._main_dirty_blocked is False
    assert mp._main_dirty_snapshot is None

    events = temp_db.get_events(None)
    resumed = [e for e in events if e["event_type"] == "merge_resumed_main_clean"]
    assert len(resumed) == 1


@pytest.mark.asyncio
async def test_preflight_clean_main_returns_true(temp_db, mock_opencode, git_repo):
    """Preflight returns True and emits no pause events when main is clean."""
    mp = MergeProcessor(temp_db, mock_opencode, str(git_repo), "test")

    result = await mp._mechanical_preflight()

    assert result is True
    events = temp_db.get_events(None)
    paused = [e for e in events if e["event_type"] == "merge_paused_dirty_main"]
    assert len(paused) == 0


# ---------------------------------------------------------------------------
# _mechanical_ff_merge helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ff_merge_success_logs_merged(merge_entry_with_worktree, temp_db, mock_opencode):
    """_mechanical_ff_merge: successful ff-merge logs 'merged' event and returns (True, None)."""
    info = merge_entry_with_worktree
    mp = MergeProcessor(temp_db, mock_opencode, str(info["git_repo"]), "test")

    entry = {
        "id": 1,
        "issue_id": info["issue_id"],
        "agent_id": info["agent_id"],
        "branch_name": info["branch_name"],
        "worktree": info["worktree_path"],
    }

    # Rebase first so ff-merge works
    from hive.git import rebase_onto_main_async

    await rebase_onto_main_async(info["worktree_path"])

    success, err = await mp._mechanical_ff_merge(entry)

    assert success is True
    assert err is None

    events = temp_db.get_events(info["issue_id"])
    merged_events = [e for e in events if e["event_type"] == "merged"]
    assert len(merged_events) == 1


@pytest.mark.asyncio
async def test_ff_merge_failure_logs_merge_failed(temp_db, mock_opencode):
    """_mechanical_ff_merge: git error logs 'merge_failed' and returns (False, error_str)."""
    issue_id = temp_db.create_issue(title="Test", project="test")
    agent_id = temp_db.create_agent(name="worker")

    mp = MergeProcessor(temp_db, mock_opencode, "/tmp/fake-project", "test")

    entry = {
        "id": 1,
        "issue_id": issue_id,
        "agent_id": agent_id,
        "branch_name": "agent/nonexistent",
        "worktree": "/tmp/fake-worktree",
    }

    with patch("hive.merge.merge_to_main_async", side_effect=GitWorktreeError("ff-merge failed")):
        success, err = await mp._mechanical_ff_merge(entry)

    assert success is False
    assert "ff-merge failed" in err

    events = temp_db.get_events(issue_id)
    failed_events = [e for e in events if e["event_type"] == "merge_failed"]
    assert len(failed_events) == 1


# ---------------------------------------------------------------------------
# INV-2: successful mechanical path finalizes and marks merge_queue merged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clean_mechanical_success_path(merge_entry_with_worktree, temp_db, mock_opencode):
    """INV-2: clean mechanical success: issue finalized, queue entry marked merged."""
    info = merge_entry_with_worktree
    mp = MergeProcessor(temp_db, mock_opencode, str(info["git_repo"]), "test")

    with patch("hive.merge.Config") as mock_config:
        mock_config.TEST_COMMAND = None
        mock_config.MERGE_POLICY = "mechanical_then_refinery"
        await mp.process_queue_once()

    issue = temp_db.get_issue(info["issue_id"])
    assert issue["status"] == "finalized"

    row = temp_db.conn.execute("SELECT status FROM merge_queue WHERE id = 1").fetchone()
    assert row["status"] == "merged"


# ---------------------------------------------------------------------------
# INV-3: mechanical failures dispatch to refinery in mechanical_then_refinery mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rebase_conflict_dispatches_to_refinery(merge_entry_with_worktree, temp_db, mock_opencode):
    """INV-3: rebase conflict → dispatched to refinery (not silently dropped)."""
    info = merge_entry_with_worktree

    mock_opencode.create_session = AsyncMock(return_value={"id": "refinery-1"})
    mock_opencode.send_message_async = AsyncMock()
    mock_opencode.get_session_status = AsyncMock(side_effect=[{"type": "busy"}, {"type": "idle"}])
    mock_opencode.get_messages = AsyncMock(return_value=[{"parts": [{"type": "text", "text": "done"}]}])
    mock_opencode.cleanup_session = AsyncMock()

    mp = MergeProcessor(temp_db, mock_opencode, str(info["git_repo"]), "test")

    with patch("hive.merge.rebase_onto_main_async", new_callable=AsyncMock, return_value=False):
        with patch("hive.merge.asyncio.sleep", new_callable=AsyncMock):
            with patch("hive.merge.Config") as mock_config:
                mock_config.TEST_COMMAND = None
                mock_config.REFINERY_MODEL = "test-model"
                mock_config.LEASE_DURATION = 30
                mock_config.REFINERY_TOKEN_THRESHOLD = 100000
                mock_config.MERGE_POLICY = "mechanical_then_refinery"

                with patch("hive.merge.read_result_file", return_value={"status": "rejected", "summary": "conflict"}):
                    with patch("hive.merge.remove_result_file"):
                        await mp.process_queue_once()

    events = temp_db.get_events(info["issue_id"])
    dispatched = [e for e in events if e["event_type"] == "refinery_dispatched"]
    assert len(dispatched) == 1

    import json

    detail = json.loads(dispatched[0]["detail"])
    assert detail["reason"] == "rebase_conflict"


@pytest.mark.asyncio
async def test_test_failure_dispatches_to_refinery(merge_entry_with_worktree, temp_db, mock_opencode):
    """INV-3: test failure → dispatched to refinery with test_output."""
    info = merge_entry_with_worktree

    mock_opencode.create_session = AsyncMock(return_value={"id": "refinery-2"})
    mock_opencode.send_message_async = AsyncMock()
    mock_opencode.get_session_status = AsyncMock(side_effect=[{"type": "busy"}, {"type": "idle"}])
    mock_opencode.get_messages = AsyncMock(return_value=[{"parts": [{"type": "text", "text": "done"}]}])
    mock_opencode.cleanup_session = AsyncMock()

    mp = MergeProcessor(temp_db, mock_opencode, str(info["git_repo"]), "test")

    with patch("hive.merge.run_command_in_worktree_async", new_callable=AsyncMock, return_value=(False, "FAILED test_foo")):
        with patch("hive.merge.asyncio.sleep", new_callable=AsyncMock):
            with patch("hive.merge.Config") as mock_config:
                mock_config.TEST_COMMAND = "pytest"
                mock_config.REFINERY_MODEL = "test-model"
                mock_config.LEASE_DURATION = 30
                mock_config.REFINERY_TOKEN_THRESHOLD = 100000
                mock_config.MERGE_POLICY = "mechanical_then_refinery"

                with patch("hive.merge.read_result_file", return_value={"status": "rejected", "summary": "tests failed"}):
                    with patch("hive.merge.remove_result_file"):
                        await mp.process_queue_once()

    events = temp_db.get_events(info["issue_id"])
    dispatched = [e for e in events if e["event_type"] == "refinery_dispatched"]
    assert len(dispatched) == 1

    import json

    detail = json.loads(dispatched[0]["detail"])
    assert detail["reason"] == "test_failure"
