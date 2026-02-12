"""Tests for the merge queue processor."""

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from hive.db import Database
from hive.git import create_worktree
from hive.merge import MergeProcessor


@pytest.fixture
def temp_db(tmp_path):
    """Create a temporary database for testing."""
    db_path = str(tmp_path / "test.db")
    db = Database(db_path)
    db.connect()
    yield db
    db.close()


@pytest.fixture
def git_repo(tmp_path):
    """Create a temporary git repository for testing."""
    repo_path = tmp_path / "test_repo"
    repo_path.mkdir()

    subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    (repo_path / "README.md").write_text("# Test Repo\n")
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "branch", "-M", "main"], cwd=repo_path, check=True, capture_output=True)

    return repo_path


@pytest.fixture
def merge_entry_with_worktree(git_repo, temp_db):
    """Create a DB entry and worktree ready for merge processing."""
    # Create agent
    agent_id = temp_db.create_agent(name="worker-test")

    # Create issue and mark done
    issue_id = temp_db.create_issue(title="Test Feature", project="test")
    temp_db.update_issue_status(issue_id, "done")

    # Create worktree with a commit
    worktree_path = create_worktree(str(git_repo), "worker-test")
    (Path(worktree_path) / "feature.py").write_text("# new feature\n")
    subprocess.run(["git", "add", "."], cwd=worktree_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Add feature"],
        cwd=worktree_path,
        check=True,
        capture_output=True,
    )

    # Insert merge queue entry
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


@pytest.fixture
def mock_opencode():
    """Create a mock OpenCode client."""
    client = AsyncMock(
        spec=[
            "create_session",
            "send_message_async",
            "get_session_status",
            "get_messages",
            "abort_session",
        ]
    )
    return client


# --- Unit tests ---


def test_merge_processor_init(temp_db, mock_opencode):
    """Test MergeProcessor initialization."""
    mp = MergeProcessor(temp_db, mock_opencode, "/tmp/project", "test")
    assert mp.db is temp_db
    assert mp.project_name == "test"
    assert mp.refinery_session_id is None


@pytest.mark.asyncio
async def test_process_queue_once_empty(temp_db, mock_opencode):
    """Test processing empty queue does nothing."""
    mp = MergeProcessor(temp_db, mock_opencode, "/tmp/project", "test")
    await mp.process_queue_once()
    # No errors, nothing happened


@pytest.mark.asyncio
async def test_mechanical_merge_success(merge_entry_with_worktree, temp_db, mock_opencode):
    """Test successful mechanical merge (rebase + ff-merge)."""
    info = merge_entry_with_worktree
    mp = MergeProcessor(temp_db, mock_opencode, str(info["git_repo"]), "test")

    await mp.process_queue_once()

    # Issue should be finalized
    issue = temp_db.get_issue(info["issue_id"])
    assert issue["status"] == "finalized"

    # Merge queue entry should be merged
    entry = temp_db.get_merge_queue_entry(1)
    assert entry["status"] == "merged"
    assert entry["completed_at"] is not None

    # Worktree should be cleaned up
    assert not Path(info["worktree_path"]).exists()

    # Main should have the feature file
    assert (info["git_repo"] / "feature.py").exists()


@pytest.mark.asyncio
async def test_mechanical_merge_rebase_conflict(merge_entry_with_worktree, temp_db, mock_opencode):
    """Test merge with rebase conflict falls through to refinery."""
    info = merge_entry_with_worktree

    # Create a conflicting commit on main
    (info["git_repo"] / "feature.py").write_text("# conflicting\n")
    subprocess.run(["git", "add", "."], cwd=info["git_repo"], check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Conflict on main"],
        cwd=info["git_repo"],
        check=True,
        capture_output=True,
    )

    # Mock refinery session
    mock_opencode.create_session = AsyncMock(return_value={"id": "refinery-session-1"})
    mock_opencode.send_message_async = AsyncMock()
    mock_opencode.get_session_status = AsyncMock(return_value={"type": "idle"})
    mock_opencode.get_messages = AsyncMock(
        return_value=[
            {
                "parts": [
                    {
                        "type": "text",
                        "text": """:::MERGE_RESULT
issue_id: test
status: rejected
summary: Could not resolve semantic conflict
tests_passed: false
conflicts_resolved: 0
:::""",
                    }
                ]
            }
        ]
    )

    mp = MergeProcessor(temp_db, mock_opencode, str(info["git_repo"]), "test")
    await mp.process_queue_once()

    # Issue should be reset to open (rejected)
    issue = temp_db.get_issue(info["issue_id"])
    assert issue["status"] == "open"

    # Merge queue should be failed
    entry = temp_db.get_merge_queue_entry(1)
    assert entry["status"] == "failed"


@pytest.mark.asyncio
async def test_mechanical_merge_test_failure(merge_entry_with_worktree, temp_db, mock_opencode):
    """Test merge with test failure falls through to refinery."""
    info = merge_entry_with_worktree

    # Mock refinery (will be called after test failure)
    mock_opencode.create_session = AsyncMock(return_value={"id": "refinery-session-1"})
    mock_opencode.send_message_async = AsyncMock()
    mock_opencode.get_session_status = AsyncMock(return_value={"type": "idle"})
    mock_opencode.get_messages = AsyncMock(
        return_value=[
            {
                "parts": [
                    {
                        "type": "text",
                        "text": """:::MERGE_RESULT
issue_id: test
status: merged
summary: Fixed test by updating import
tests_passed: true
conflicts_resolved: 0
:::""",
                    }
                ]
            }
        ]
    )

    mp = MergeProcessor(temp_db, mock_opencode, str(info["git_repo"]), "test")

    # Patch TEST_COMMAND to a failing command
    with patch.object(type(mp), "__module__", "hive.merge"):
        with patch("hive.merge.Config") as mock_config:
            mock_config.TEST_COMMAND = "false"  # Will always fail
            mock_config.REFINERY_MODEL = "test-model"
            mock_config.LEASE_DURATION = 30

            await mp.process_queue_once()

    # Should have been dispatched to refinery and then finalized
    issue = temp_db.get_issue(info["issue_id"])
    assert issue["status"] == "finalized"


def test_finalize_issue(merge_entry_with_worktree, temp_db, mock_opencode):
    """Test _finalize_issue updates DB correctly."""
    info = merge_entry_with_worktree

    mp = MergeProcessor(temp_db, mock_opencode, str(info["git_repo"]), "test")

    entry = dict(temp_db.get_merge_queue_entry(1))
    entry["issue_title"] = "Test Feature"
    entry["agent_name"] = "worker-test"

    mp._finalize_issue(entry)

    # Issue should be finalized
    issue = temp_db.get_issue(info["issue_id"])
    assert issue["status"] == "finalized"

    # Merge queue should be merged
    mq = temp_db.get_merge_queue_entry(1)
    assert mq["status"] == "merged"

    # Worktree should be cleaned up
    assert not Path(info["worktree_path"]).exists()

    # Agent should be idle
    agent = temp_db.get_agent(info["agent_id"])
    assert agent["status"] == "idle"
    assert agent["current_issue"] is None


def test_teardown_after_finalize(merge_entry_with_worktree, temp_db, mock_opencode):
    """Test _teardown_after_finalize cleans up worktree and agent."""
    info = merge_entry_with_worktree

    mp = MergeProcessor(temp_db, mock_opencode, str(info["git_repo"]), "test")

    entry = dict(temp_db.get_merge_queue_entry(1))
    mp._teardown_after_finalize(entry)

    # Worktree gone
    assert not Path(info["worktree_path"]).exists()

    # Agent idle
    agent = temp_db.get_agent(info["agent_id"])
    assert agent["status"] == "idle"


def test_teardown_missing_worktree(temp_db, mock_opencode):
    """Test teardown handles missing worktree gracefully."""
    agent_id = temp_db.create_agent(name="ghost")
    issue_id = temp_db.create_issue(title="Ghost", project="test")

    mp = MergeProcessor(temp_db, mock_opencode, "/tmp/nonexistent", "test")

    entry = {
        "id": 1,
        "issue_id": issue_id,
        "agent_id": agent_id,
        "worktree": "/tmp/nonexistent/worktree",
        "branch_name": "agent/ghost",
    }

    # Should not raise
    mp._teardown_after_finalize(entry)
