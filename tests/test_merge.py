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
            "cleanup_session",
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
    cursor = temp_db.conn.execute("SELECT * FROM merge_queue WHERE id = 1")
    entry = cursor.fetchone()
    assert entry is not None
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

    # Mock refinery session with proper lifecycle:
    # get_session_status: first call returns "busy" (post-send check), then "idle" (wait loop)
    # get_messages: returns enough messages to pass the fence check
    mock_opencode.create_session = AsyncMock(return_value={"id": "refinery-session-1"})
    mock_opencode.send_message_async = AsyncMock()
    mock_opencode.get_session_status = AsyncMock(side_effect=[{"type": "busy"}, {"type": "idle"}])
    mock_opencode.get_messages = AsyncMock(return_value=[{"parts": [{"type": "text", "text": "done"}]}])
    mock_opencode.cleanup_session = AsyncMock()

    mp = MergeProcessor(temp_db, mock_opencode, str(info["git_repo"]), "test")

    # Patch rebase_onto_main_async to simulate rebase conflict
    with patch("hive.merge.rebase_onto_main_async", new_callable=AsyncMock, return_value=False):
        with patch("hive.merge.asyncio.sleep", new_callable=AsyncMock):
            with patch("hive.merge.Config") as mock_config:
                mock_config.TEST_COMMAND = None
                mock_config.REFINERY_MODEL = "test-model"
                mock_config.LEASE_DURATION = 30
                mock_config.REFINERY_TOKEN_THRESHOLD = 100000

                # Mock read_result_file to return a rejected result
                with patch(
                    "hive.merge.read_result_file",
                    return_value={
                        "status": "rejected",
                        "summary": "Could not resolve semantic conflict",
                        "tests_passed": False,
                        "conflicts_resolved": 0,
                    },
                ):
                    with patch("hive.merge.remove_result_file"):
                        await mp.process_queue_once()

    # Issue should be reset to open (rejected)
    issue = temp_db.get_issue(info["issue_id"])
    assert issue["status"] == "open"

    # Merge queue should be failed
    cursor = temp_db.conn.execute("SELECT * FROM merge_queue WHERE id = 1")
    entry = cursor.fetchone()
    assert entry is not None
    assert entry["status"] == "failed"


@pytest.mark.asyncio
async def test_mechanical_merge_test_failure(merge_entry_with_worktree, temp_db, mock_opencode):
    """Test merge with test failure falls through to refinery."""
    info = merge_entry_with_worktree

    # Mock refinery session with proper lifecycle:
    # get_session_status: first call returns "busy" (post-send check), then "idle" (wait loop)
    # get_messages: returns enough messages to pass the fence check
    mock_opencode.create_session = AsyncMock(return_value={"id": "refinery-session-1"})
    mock_opencode.send_message_async = AsyncMock()
    mock_opencode.get_session_status = AsyncMock(side_effect=[{"type": "busy"}, {"type": "idle"}])
    mock_opencode.get_messages = AsyncMock(return_value=[{"parts": [{"type": "text", "text": "done"}]}])
    mock_opencode.cleanup_session = AsyncMock()

    mp = MergeProcessor(temp_db, mock_opencode, str(info["git_repo"]), "test")

    # Patch run_command_in_worktree_async to simulate test failure and Config
    with patch("hive.merge.run_command_in_worktree_async", new_callable=AsyncMock, return_value=(False, "FAILED test_foo.py")):
        with patch("hive.merge.asyncio.sleep", new_callable=AsyncMock):
            with patch("hive.merge.Config") as mock_config:
                mock_config.TEST_COMMAND = "pytest"
                mock_config.REFINERY_MODEL = "test-model"
                mock_config.LEASE_DURATION = 30
                mock_config.REFINERY_TOKEN_THRESHOLD = 100000

                # Mock read_result_file to return a merged result
                with patch(
                    "hive.merge.read_result_file",
                    return_value={
                        "status": "merged",
                        "summary": "Fixed test by updating import",
                        "tests_passed": True,
                        "conflicts_resolved": 0,
                    },
                ):
                    with patch("hive.merge.remove_result_file"):
                        await mp.process_queue_once()

    # Should have been dispatched to refinery and then finalized
    issue = temp_db.get_issue(info["issue_id"])
    assert issue["status"] == "finalized"


@pytest.mark.asyncio
async def test_finalize_issue(merge_entry_with_worktree, temp_db, mock_opencode):
    """Test _finalize_issue updates DB correctly."""
    info = merge_entry_with_worktree

    mp = MergeProcessor(temp_db, mock_opencode, str(info["git_repo"]), "test")

    cursor = temp_db.conn.execute("SELECT * FROM merge_queue WHERE id = 1")
    row = cursor.fetchone()
    entry = dict(row) if row else {}
    entry["issue_title"] = "Test Feature"
    entry["agent_name"] = "worker-test"

    await mp._finalize_issue(entry)

    # Issue should be finalized
    issue = temp_db.get_issue(info["issue_id"])
    assert issue["status"] == "finalized"

    # Merge queue should be merged
    cursor = temp_db.conn.execute("SELECT * FROM merge_queue WHERE id = 1")
    mq = cursor.fetchone()
    assert mq is not None
    assert mq["status"] == "merged"

    # Worktree should be cleaned up
    assert not Path(info["worktree_path"]).exists()

    # Agent should be deleted
    agent = temp_db.get_agent(info["agent_id"])
    assert agent is None


@pytest.mark.asyncio
async def test_teardown_after_finalize(merge_entry_with_worktree, temp_db, mock_opencode):
    """Test _teardown_after_finalize cleans up worktree and agent."""
    info = merge_entry_with_worktree

    # Create some test events to verify they persist after agent deletion
    temp_db.log_event(info["issue_id"], info["agent_id"], "test_event", {"data": "test"})
    temp_db.conn.commit()

    mp = MergeProcessor(temp_db, mock_opencode, str(info["git_repo"]), "test")

    cursor = temp_db.conn.execute("SELECT * FROM merge_queue WHERE id = 1")
    row = cursor.fetchone()
    entry = dict(row) if row else {}
    await mp._teardown_after_finalize(entry)

    # Worktree gone
    assert not Path(info["worktree_path"]).exists()

    # Agent deleted
    agent = temp_db.get_agent(info["agent_id"])
    assert agent is None

    # But related records with agent_id still exist as correlation keys
    cursor = temp_db.conn.execute("SELECT * FROM events WHERE agent_id = ?", (info["agent_id"],))
    events = cursor.fetchall()
    assert len(events) > 0  # Events should still exist

    cursor = temp_db.conn.execute("SELECT * FROM merge_queue WHERE agent_id = ?", (info["agent_id"],))
    merge_entries = cursor.fetchall()
    assert len(merge_entries) > 0  # Merge queue entries should still exist


@pytest.mark.asyncio
async def test_teardown_missing_worktree(temp_db, mock_opencode):
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
    await mp._teardown_after_finalize(entry)


@pytest.mark.asyncio
async def test_refinery_session_cycling_by_token_count(temp_db, mock_opencode):
    """Test refinery session is cycled when token threshold is exceeded."""
    mp = MergeProcessor(temp_db, mock_opencode, "/tmp/project", "test")

    # Set up a refinery session with high token count
    mp.refinery_session_id = "test-session-123"
    mp._refinery_token_estimate = 120000  # Exceeds REFINERY_TOKEN_THRESHOLD (100000)
    mp._refinery_message_count = 10  # Under message threshold

    # Mock cleanup session call
    mock_opencode.cleanup_session = AsyncMock()

    # Call the token check method
    await mp._maybe_cycle_refinery_session()

    # Verify session was cycled
    assert mp.refinery_session_id is None
    assert mp._refinery_token_estimate == 0
    assert mp._refinery_message_count == 0
    mock_opencode.cleanup_session.assert_called_once_with("test-session-123", directory=mp.project_path)

    # Verify event was logged
    events = temp_db.get_events(None)  # Get all events
    cycling_events = [e for e in events if e["event_type"] == "refinery_session_cycled"]
    assert len(cycling_events) == 1

    event = cycling_events[0]
    import json

    details = json.loads(event["detail"])
    assert details["session_id"] == "test-session-123"
    assert details["token_count"] == 120000  # Local counter value
    assert details["threshold"] == 100000


@pytest.mark.asyncio
async def test_refinery_session_cycling_by_message_count(temp_db, mock_opencode):
    """Test refinery session is cycled when message count exceeds threshold (fallback)."""
    mp = MergeProcessor(temp_db, mock_opencode, "/tmp/project", "test")

    # Set up a refinery session with high message count
    mp.refinery_session_id = "test-session-456"
    mp._refinery_token_estimate = 50000  # Under token threshold
    mp._refinery_message_count = 25  # More than 20 messages (our fallback threshold)

    # Mock cleanup session call
    mock_opencode.cleanup_session = AsyncMock()

    # Call the token check method
    await mp._maybe_cycle_refinery_session()

    # Verify session was cycled
    assert mp.refinery_session_id is None
    assert mp._refinery_token_estimate == 0
    assert mp._refinery_message_count == 0
    mock_opencode.cleanup_session.assert_called_once_with("test-session-456", directory=mp.project_path)


@pytest.mark.asyncio
async def test_refinery_session_no_cycling_under_threshold(temp_db, mock_opencode):
    """Test refinery session is NOT cycled when under token threshold."""
    mp = MergeProcessor(temp_db, mock_opencode, "/tmp/project", "test")

    # Set up a refinery session
    mp.refinery_session_id = "test-session-789"

    # Mock get_messages to return low token count
    mock_opencode.get_messages = AsyncMock(
        return_value=[
            {
                "metadata": {"token_count": 50000},  # Under REFINERY_TOKEN_THRESHOLD (100000)
                "parts": [{"type": "text", "text": "message 1"}],
            }
        ]
    )

    mock_opencode.cleanup_session = AsyncMock()

    # Call the token check method
    await mp._maybe_cycle_refinery_session()

    # Verify session was NOT cycled
    assert mp.refinery_session_id == "test-session-789"
    mock_opencode.cleanup_session.assert_not_called()


# --- New tests for hardened refinery session ---


@pytest.mark.asyncio
async def test_force_reset_refinery_session(temp_db, mock_opencode):
    """Test _force_reset_refinery_session cleans up and resets session ID."""
    mp = MergeProcessor(temp_db, mock_opencode, "/tmp/project", "test")

    # Set up a session to reset
    original_session_id = "session-to-reset"
    mp.refinery_session_id = original_session_id

    mock_opencode.cleanup_session = AsyncMock()

    # Call force reset
    reason = "Test reset"
    await mp._force_reset_refinery_session(reason)

    # Verify session ID was cleared
    assert mp.refinery_session_id is None

    # Verify abort and delete were called
    mock_opencode.cleanup_session.assert_called_once_with(original_session_id, directory=mp.project_path)

    # Verify event was logged
    events = temp_db.get_events(None)
    reset_events = [e for e in events if e["event_type"] == "refinery_session_reset"]
    assert len(reset_events) == 1

    event = reset_events[0]
    import json

    details = json.loads(event["detail"])
    assert details["session_id"] == original_session_id
    assert details["reason"] == reason


@pytest.mark.asyncio
async def test_force_reset_no_session(temp_db, mock_opencode):
    """Test _force_reset_refinery_session handles no session gracefully."""
    mp = MergeProcessor(temp_db, mock_opencode, "/tmp/project", "test")

    # No session set
    assert mp.refinery_session_id is None

    mock_opencode.cleanup_session = AsyncMock()

    # Call force reset - should do nothing
    await mp._force_reset_refinery_session("No session to reset")

    # Verify no calls were made
    mock_opencode.cleanup_session.assert_not_called()


@pytest.mark.asyncio
async def test_wait_for_refinery_consecutive_errors(temp_db, mock_opencode):
    """Test _wait_for_refinery bails after consecutive errors."""
    mp = MergeProcessor(temp_db, mock_opencode, "/tmp/project", "test")

    # Mock get_session_status to always throw exceptions
    mock_opencode.get_session_status = AsyncMock(side_effect=Exception("Connection failed"))

    # Patch asyncio.sleep to be instant so the test doesn't take 25+ seconds
    with patch("hive.merge.asyncio.sleep", new_callable=AsyncMock):
        result = await mp._wait_for_refinery("test-session", worktree_path="/tmp/worktree", timeout=60)

    assert result["status"] == "needs_human"
    assert "consecutive errors" in result["summary"]
    assert result["tests_passed"] is False
    assert result["conflicts_resolved"] == 0

    # Verify get_session_status was called at least 5 times
    assert mock_opencode.get_session_status.call_count >= 5


@pytest.mark.asyncio
async def test_wait_for_refinery_message_count_fence(temp_db, mock_opencode):
    """Test _wait_for_refinery respects min_message_count fence."""
    mp = MergeProcessor(temp_db, mock_opencode, "/tmp/project", "test")

    # First call: session is idle but no new messages
    mock_opencode.get_session_status = AsyncMock(return_value={"type": "idle"})
    mock_opencode.get_messages = AsyncMock(
        side_effect=[
            # First call: only old messages, should continue waiting
            [{"parts": [{"type": "text", "text": "old message"}]}],  # Only 1 message
            # Second call: new message arrived
            [
                {"parts": [{"type": "text", "text": "old message"}]},
                {"parts": [{"type": "text", "text": "new response"}]},
            ],  # 2 messages
        ]
    )

    # Mock read_result_file to return a merged result
    with patch(
        "hive.merge.read_result_file",
        return_value={
            "status": "merged",
            "summary": "Success",
            "tests_passed": True,
            "conflicts_resolved": 0,
        },
    ):
        with patch("hive.merge.remove_result_file"), patch("hive.merge.asyncio.sleep", new_callable=AsyncMock):
            # Call with min_message_count=1 (fence)
            result = await mp._wait_for_refinery("test-session", worktree_path="/tmp/worktree", timeout=30, min_message_count=1)

    assert result["status"] == "merged"
    # Should have been called twice - once for fence check, once for actual result
    assert mock_opencode.get_messages.call_count == 2


@pytest.mark.asyncio
async def test_health_check_recreates_dead_session(temp_db, mock_opencode):
    """Test health_check recreates dead session."""
    mp = MergeProcessor(temp_db, mock_opencode, "/tmp/project", "test")

    # Set up a "dead" session
    mp.refinery_session_id = "dead-session"

    # Mock get_session_status to return None (dead session)
    mock_opencode.get_session_status = AsyncMock(return_value=None)

    # Mock session creation for recreation
    mock_opencode.create_session = AsyncMock(return_value={"id": "new-session"})

    # Call health check
    healthy = await mp.health_check()

    assert healthy is True
    assert mp.refinery_session_id == "new-session"

    # Verify get_session_status was called for the dead session
    mock_opencode.get_session_status.assert_called_once_with("dead-session", directory=mp.project_path)

    # Verify new session was created
    mock_opencode.create_session.assert_called_once()


@pytest.mark.asyncio
async def test_health_check_healthy_session(temp_db, mock_opencode):
    """Test health_check returns True for healthy session."""
    mp = MergeProcessor(temp_db, mock_opencode, "/tmp/project", "test")

    # Set up a healthy session
    mp.refinery_session_id = "healthy-session"

    # Mock get_session_status to return valid status
    mock_opencode.get_session_status = AsyncMock(return_value={"type": "idle"})

    # Call health check
    healthy = await mp.health_check()

    assert healthy is True
    assert mp.refinery_session_id == "healthy-session"  # Unchanged

    # Verify session was checked
    mock_opencode.get_session_status.assert_called_once_with("healthy-session", directory=mp.project_path)


@pytest.mark.asyncio
async def test_health_check_no_session_creates_new(temp_db, mock_opencode):
    """Test health_check creates session when none exists."""
    mp = MergeProcessor(temp_db, mock_opencode, "/tmp/project", "test")

    # No session initially
    assert mp.refinery_session_id is None

    # Mock session creation
    mock_opencode.create_session = AsyncMock(return_value={"id": "new-session"})

    # Call health check
    healthy = await mp.health_check()

    assert healthy is True
    assert mp.refinery_session_id == "new-session"

    # Verify session was created
    mock_opencode.create_session.assert_called_once()


@pytest.mark.asyncio
async def test_initialize_creates_session(temp_db, mock_opencode):
    """Test initialize creates refinery session eagerly."""
    mp = MergeProcessor(temp_db, mock_opencode, "/tmp/project", "test")

    # Mock session creation
    mock_opencode.create_session = AsyncMock(return_value={"id": "eager-session"})

    # Call initialize
    await mp.initialize()

    assert mp.refinery_session_id == "eager-session"
    mock_opencode.create_session.assert_called_once()


@pytest.mark.asyncio
async def test_initialize_handles_failure_gracefully(temp_db, mock_opencode):
    """Test initialize handles session creation failure gracefully."""
    mp = MergeProcessor(temp_db, mock_opencode, "/tmp/project", "test")

    # Mock session creation to fail
    mock_opencode.create_session = AsyncMock(side_effect=Exception("Creation failed"))

    # Call initialize - should not raise
    await mp.initialize()

    # Session should remain None, will fall back to lazy creation
    assert mp.refinery_session_id is None


@pytest.mark.asyncio
async def test_send_to_refinery_status_verification(temp_db, mock_opencode):
    """Test status verification after send_message_async detects unprocessed messages."""
    mp = MergeProcessor(temp_db, mock_opencode, "/tmp/project", "test")

    # Create actual DB rows so update_merge_queue_status and log_event work
    issue_id = temp_db.create_issue(title="Test Issue", project="test")
    agent_id = temp_db.create_agent(name="test-agent")
    temp_db.conn.execute(
        "INSERT INTO merge_queue (issue_id, agent_id, project, worktree, branch_name) VALUES (?, ?, ?, ?, ?)",
        (issue_id, agent_id, "test", "/tmp/worktree", "test-branch"),
    )
    temp_db.conn.commit()
    queue_id = temp_db.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    entry = {
        "id": queue_id,
        "issue_id": issue_id,
        "agent_id": agent_id,
        "branch_name": "test-branch",
        "worktree": "/tmp/worktree",
        "issue_title": "Test Issue",
    }

    # Mock session creation and message retrieval
    mock_opencode.create_session = AsyncMock(return_value={"id": "test-session"})
    mock_opencode.get_messages = AsyncMock(return_value=[])  # No existing messages
    mock_opencode.send_message_async = AsyncMock()

    # Mock session status to remain idle after message send (message not picked up)
    mock_opencode.get_session_status = AsyncMock(return_value={"type": "idle"})

    mock_opencode.cleanup_session = AsyncMock()

    # Patch config
    with patch("hive.merge.Config") as mock_config, patch("hive.merge.asyncio.sleep", new_callable=AsyncMock):
        mock_config.REFINERY_MODEL = "test-model"
        mock_config.TEST_COMMAND = None

        # Should raise RuntimeError and reset session
        await mp._send_to_refinery(entry)

    # Verify session was reset due to unprocessed message
    assert mp.refinery_session_id is None
    mock_opencode.cleanup_session.assert_called()

    # Verify merge queue was marked failed
    cursor = temp_db.conn.execute("SELECT status FROM merge_queue WHERE id = ?", (queue_id,))
    row = cursor.fetchone()
    assert row is not None
    assert row["status"] == "failed"


@pytest.mark.asyncio
async def test_send_to_refinery_harvests_notes(tmp_path, temp_db, mock_opencode):
    """Test that _send_to_refinery harvests notes from the worktree after refinery completes."""
    worktree_path = str(tmp_path / "worktree")
    Path(worktree_path).mkdir()

    # Write a notes file in the worktree
    notes_file = Path(worktree_path) / ".hive-notes.jsonl"
    notes_file.write_text(
        '{"category": "gotcha", "content": "Import block conflicts with worker-2"}\n'
        '{"category": "pattern", "content": "Tests require DB fixtures to be reset"}\n'
    )

    # Create DB rows
    issue_id = temp_db.create_issue(title="Test Issue", project="test")
    agent_id = temp_db.create_agent(name="test-agent")
    temp_db.conn.execute(
        "INSERT INTO merge_queue (issue_id, agent_id, project, worktree, branch_name) VALUES (?, ?, ?, ?, ?)",
        (issue_id, agent_id, "test", worktree_path, "test-branch"),
    )
    temp_db.conn.commit()
    queue_id = temp_db.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    entry = {
        "id": queue_id,
        "issue_id": issue_id,
        "agent_id": agent_id,
        "branch_name": "test-branch",
        "worktree": worktree_path,
        "issue_title": "Test Issue",
    }

    # Mock refinery session lifecycle
    mock_opencode.create_session = AsyncMock(return_value={"id": "test-session"})
    mock_opencode.send_message_async = AsyncMock()
    mock_opencode.get_session_status = AsyncMock(side_effect=[{"type": "busy"}, {"type": "idle"}])
    mock_opencode.get_messages = AsyncMock(return_value=[{"parts": [{"type": "text", "text": "done"}]}])
    mock_opencode.cleanup_session = AsyncMock()

    mp = MergeProcessor(temp_db, mock_opencode, "/tmp/project", "test")

    with patch("hive.merge.asyncio.sleep", new_callable=AsyncMock):
        with patch("hive.merge.Config") as mock_config:
            mock_config.TEST_COMMAND = None
            mock_config.REFINERY_MODEL = "test-model"
            mock_config.LEASE_DURATION = 30
            mock_config.REFINERY_TOKEN_THRESHOLD = 100000

            with patch(
                "hive.merge.read_result_file",
                return_value={
                    "status": "rejected",
                    "summary": "Conflicts too complex",
                    "tests_passed": False,
                    "conflicts_resolved": 0,
                },
            ):
                with patch("hive.merge.remove_result_file"):
                    await mp._send_to_refinery(entry)

    # Verify notes were saved to DB (2 harvested + 1 rejection note)
    notes = temp_db.get_notes()
    assert len(notes) == 3
    contents = [n["content"] for n in notes]
    assert "Import block conflicts with worker-2" in contents
    assert "Tests require DB fixtures to be reset" in contents
    categories = [n["category"] for n in notes]
    assert "gotcha" in categories
    assert "pattern" in categories
    assert "rejection" in categories  # New rejection note

    # Verify notes_harvested event was logged with refinery source
    events = temp_db.get_events(issue_id)
    harvest_events = [e for e in events if e["event_type"] == "notes_harvested"]
    assert len(harvest_events) == 1
    import json

    detail = json.loads(harvest_events[0]["detail"])
    assert detail["count"] == 2
    assert detail["source"] == "refinery"

    # Verify notes file was cleaned up
    assert not notes_file.exists()


@pytest.mark.asyncio
async def test_finalize_issue_molecule_completion(temp_db, mock_opencode, git_repo):
    """Test that finalizing all steps of a molecule marks the parent as finalized."""
    # Create a parent molecule issue
    parent_id = temp_db.create_issue(title="Molecule Task", project="test")

    # Create two child issues (steps)
    step1_id = temp_db.create_issue(title="Step 1", project="test", parent_id=parent_id)
    step2_id = temp_db.create_issue(title="Step 2", project="test", parent_id=parent_id)

    # Mark only the first step as done initially
    temp_db.update_issue_status(step1_id, "done")

    # Create agents for the steps
    agent1_id = temp_db.create_agent(name="worker-test-1")
    agent2_id = temp_db.create_agent(name="worker-test-2")

    # Create worktrees for both steps
    worktree1_path = create_worktree(str(git_repo), "worker-test-1")
    worktree2_path = create_worktree(str(git_repo), "worker-test-2")

    # Add commits to both worktrees
    (Path(worktree1_path) / "step1.py").write_text("# step 1\n")
    subprocess.run(["git", "add", "."], cwd=worktree1_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Step 1"], cwd=worktree1_path, check=True, capture_output=True)

    (Path(worktree2_path) / "step2.py").write_text("# step 2\n")
    subprocess.run(["git", "add", "."], cwd=worktree2_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Step 2"], cwd=worktree2_path, check=True, capture_output=True)

    # Insert merge queue entries for both steps
    temp_db.conn.execute(
        "INSERT INTO merge_queue (issue_id, agent_id, project, worktree, branch_name) VALUES (?, ?, ?, ?, ?)",
        (step1_id, agent1_id, "test", worktree1_path, "agent/worker-test-1"),
    )
    temp_db.conn.execute(
        "INSERT INTO merge_queue (issue_id, agent_id, project, worktree, branch_name) VALUES (?, ?, ?, ?, ?)",
        (step2_id, agent2_id, "test", worktree2_path, "agent/worker-test-2"),
    )
    temp_db.conn.commit()

    # Get merge queue entries
    cursor1 = temp_db.conn.execute("SELECT * FROM merge_queue WHERE issue_id = ?", (step1_id,))
    cursor2 = temp_db.conn.execute("SELECT * FROM merge_queue WHERE issue_id = ?", (step2_id,))
    entry1 = dict(cursor1.fetchone())
    entry2 = dict(cursor2.fetchone())
    entry1["issue_title"] = "Step 1"
    entry1["agent_name"] = "worker-test-1"
    entry2["issue_title"] = "Step 2"
    entry2["agent_name"] = "worker-test-2"

    # Create merge processor
    mp = MergeProcessor(temp_db, mock_opencode, str(git_repo), "test")

    # Parent should be in 'open' status initially
    parent = temp_db.get_issue(parent_id)
    assert parent["status"] == "open"

    # Finalize first step - parent should still be open
    await mp._finalize_issue(entry1)
    parent = temp_db.get_issue(parent_id)
    assert parent["status"] == "open"

    # Mark second step as done before finalizing
    temp_db.update_issue_status(step2_id, "done")

    # Finalize second step - parent should now be finalized (nothing left to merge)
    await mp._finalize_issue(entry2)
    parent = temp_db.get_issue(parent_id)
    assert parent["status"] == "finalized"

    # Verify molecule_complete event was logged for the parent
    cursor = temp_db.conn.execute("SELECT * FROM events WHERE issue_id = ? AND event_type = 'molecule_complete'", (parent_id,))
    event = cursor.fetchone()
    assert event is not None

    # Verify both steps are finalized
    step1 = temp_db.get_issue(step1_id)
    step2 = temp_db.get_issue(step2_id)
    assert step1["status"] == "finalized"
    assert step2["status"] == "finalized"


# --- Tests for structured rejection notes ---


@pytest.mark.asyncio
async def test_mechanical_merge_rebase_conflict_creates_rejection_note(merge_entry_with_worktree, temp_db, mock_opencode):
    """Test that rebase conflicts create a structured rejection note."""
    info = merge_entry_with_worktree

    mp = MergeProcessor(temp_db, mock_opencode, str(info["git_repo"]), "test")

    # Patch rebase_onto_main_async to simulate rebase conflict
    with patch("hive.merge.rebase_onto_main_async", new_callable=AsyncMock, return_value=False):
        with patch("hive.merge.abort_rebase_async", new_callable=AsyncMock):
            entry = {
                "id": 1,
                "issue_id": info["issue_id"],
                "agent_id": info["agent_id"],
                "worktree": info["worktree_path"],
                "branch_name": info["branch_name"],
            }
            success, _ = await mp._try_mechanical_merge(entry)

    assert success is False

    # Verify rejection note was created
    notes = temp_db.get_notes(issue_id=info["issue_id"], category="rejection")
    assert len(notes) == 1

    note = notes[0]
    assert note["category"] == "rejection"
    assert "[Merge conflict]" in note["content"]
    assert "Rebase onto main failed" in note["content"]
    assert info["branch_name"] in note["content"]
    assert note["issue_id"] == info["issue_id"]
    assert note["agent_id"] == info["agent_id"]


@pytest.mark.asyncio
async def test_mechanical_merge_test_failure_creates_rejection_note(merge_entry_with_worktree, temp_db, mock_opencode):
    """Test that test failures create a structured rejection note with test output."""
    info = merge_entry_with_worktree

    mp = MergeProcessor(temp_db, mock_opencode, str(info["git_repo"]), "test")

    test_output = "FAILED tests/test_foo.py::test_bar - AssertionError: expected 42, got 41\n" * 20

    # Patch run_command_in_worktree_async to simulate test failure
    with patch("hive.merge.run_command_in_worktree_async", new_callable=AsyncMock, return_value=(False, test_output)):
        with patch("hive.merge.Config") as mock_config:
            mock_config.TEST_COMMAND = "pytest tests/"

            entry = {
                "id": 1,
                "issue_id": info["issue_id"],
                "agent_id": info["agent_id"],
                "worktree": info["worktree_path"],
                "branch_name": info["branch_name"],
            }
            success, _ = await mp._try_mechanical_merge(entry)

    assert success is False

    # Verify rejection note was created
    notes = temp_db.get_notes(issue_id=info["issue_id"], category="rejection")
    assert len(notes) == 1

    note = notes[0]
    assert note["category"] == "rejection"
    assert "[Test failure]" in note["content"]
    assert "Tests failed after rebase" in note["content"]
    assert "pytest tests/" in note["content"]
    assert "FAILED tests/test_foo.py" in note["content"]
    # Verify output is truncated to 500 chars
    assert len(note["content"]) < len(test_output) + 200  # Content should be much shorter than full output


@pytest.mark.asyncio
async def test_refinery_rejection_creates_structured_note(tmp_path, temp_db, mock_opencode):
    """Test that refinery rejections create structured notes with rejection reason."""
    worktree_path = str(tmp_path / "worktree")
    Path(worktree_path).mkdir()

    # Create DB rows
    issue_id = temp_db.create_issue(title="Test Issue", project="test")
    agent_id = temp_db.create_agent(name="test-agent")
    temp_db.conn.execute(
        "INSERT INTO merge_queue (issue_id, agent_id, project, worktree, branch_name) VALUES (?, ?, ?, ?, ?)",
        (issue_id, agent_id, "test", worktree_path, "test-branch"),
    )
    temp_db.conn.commit()
    queue_id = temp_db.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    entry = {
        "id": queue_id,
        "issue_id": issue_id,
        "agent_id": agent_id,
        "branch_name": "test-branch",
        "worktree": worktree_path,
        "issue_title": "Test Issue",
    }

    test_output = "ERROR: Module not found: 'foo'\nTraceback..." * 10

    # Mock refinery session lifecycle
    mock_opencode.create_session = AsyncMock(return_value={"id": "test-session"})
    mock_opencode.send_message_async = AsyncMock()
    mock_opencode.get_session_status = AsyncMock(side_effect=[{"type": "busy"}, {"type": "idle"}])
    mock_opencode.get_messages = AsyncMock(return_value=[{"parts": [{"type": "text", "text": "done"}]}])
    mock_opencode.cleanup_session = AsyncMock()

    mp = MergeProcessor(temp_db, mock_opencode, "/tmp/project", "test")

    with patch("hive.merge.asyncio.sleep", new_callable=AsyncMock):
        with patch("hive.merge.Config") as mock_config:
            mock_config.TEST_COMMAND = "pytest"
            mock_config.REFINERY_MODEL = "test-model"
            mock_config.LEASE_DURATION = 30
            mock_config.REFINERY_TOKEN_THRESHOLD = 100000

            with patch(
                "hive.merge.read_result_file",
                return_value={
                    "status": "rejected",
                    "summary": "Could not resolve semantic conflict in imports",
                    "tests_passed": False,
                    "conflicts_resolved": 0,
                },
            ):
                with patch("hive.merge.remove_result_file"), patch("hive.merge.remove_notes_file"):
                    await mp._send_to_refinery(entry, test_output=test_output)

    # Verify rejection note was created
    notes = temp_db.get_notes(issue_id=issue_id, category="rejection")
    assert len(notes) == 1

    note = notes[0]
    assert note["category"] == "rejection"
    assert "[Refinery rejection]" in note["content"]
    assert "Could not resolve semantic conflict in imports" in note["content"]
    assert "test-branch" in note["content"]
    assert "Test output (truncated):" in note["content"]
    assert "ERROR: Module not found" in note["content"]
    # Verify test output is truncated
    assert len(note["content"]) < len(test_output) + 300

    # Verify issue was reset to open
    issue = temp_db.get_issue(issue_id)
    assert issue["status"] == "open"


@pytest.mark.asyncio
async def test_refinery_rejection_note_without_test_output(tmp_path, temp_db, mock_opencode):
    """Test that refinery rejection notes work when there's no test output (rebase conflict)."""
    worktree_path = str(tmp_path / "worktree")
    Path(worktree_path).mkdir()

    # Create DB rows
    issue_id = temp_db.create_issue(title="Test Issue", project="test")
    agent_id = temp_db.create_agent(name="test-agent")
    temp_db.conn.execute(
        "INSERT INTO merge_queue (issue_id, agent_id, project, worktree, branch_name) VALUES (?, ?, ?, ?, ?)",
        (issue_id, agent_id, "test", worktree_path, "test-branch"),
    )
    temp_db.conn.commit()
    queue_id = temp_db.conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    entry = {
        "id": queue_id,
        "issue_id": issue_id,
        "agent_id": agent_id,
        "branch_name": "test-branch",
        "worktree": worktree_path,
        "issue_title": "Test Issue",
    }

    # Mock refinery session lifecycle
    mock_opencode.create_session = AsyncMock(return_value={"id": "test-session"})
    mock_opencode.send_message_async = AsyncMock()
    mock_opencode.get_session_status = AsyncMock(side_effect=[{"type": "busy"}, {"type": "idle"}])
    mock_opencode.get_messages = AsyncMock(return_value=[{"parts": [{"type": "text", "text": "done"}]}])
    mock_opencode.cleanup_session = AsyncMock()

    mp = MergeProcessor(temp_db, mock_opencode, "/tmp/project", "test")

    with patch("hive.merge.asyncio.sleep", new_callable=AsyncMock):
        with patch("hive.merge.Config") as mock_config:
            mock_config.TEST_COMMAND = None
            mock_config.REFINERY_MODEL = "test-model"
            mock_config.LEASE_DURATION = 30
            mock_config.REFINERY_TOKEN_THRESHOLD = 100000

            with patch(
                "hive.merge.read_result_file",
                return_value={
                    "status": "rejected",
                    "summary": "Rebase conflict too complex to auto-resolve",
                    "tests_passed": False,
                    "conflicts_resolved": 0,
                },
            ):
                with patch("hive.merge.remove_result_file"), patch("hive.merge.remove_notes_file"):
                    await mp._send_to_refinery(entry, test_output=None)

    # Verify rejection note was created
    notes = temp_db.get_notes(issue_id=issue_id, category="rejection")
    assert len(notes) == 1

    note = notes[0]
    assert note["category"] == "rejection"
    assert "[Refinery rejection]" in note["content"]
    assert "Rebase conflict too complex to auto-resolve" in note["content"]
    assert "test-branch" in note["content"]
    # Should NOT have test output section
    assert "Test output" not in note["content"]


@pytest.mark.asyncio
async def test_get_notes_filters_by_rejection_category(temp_db):
    """Test that get_notes correctly filters by rejection category."""
    issue_id = temp_db.create_issue(title="Test Issue", project="test")
    agent_id = temp_db.create_agent(name="test-agent")

    # Add notes with different categories
    temp_db.add_note(issue_id=issue_id, agent_id=agent_id, category="discovery", content="Found a pattern")
    temp_db.add_note(issue_id=issue_id, agent_id=agent_id, category="rejection", content="[Test failure] Tests failed")
    temp_db.add_note(issue_id=issue_id, agent_id=agent_id, category="gotcha", content="Watch out for X")
    temp_db.add_note(issue_id=issue_id, agent_id=agent_id, category="rejection", content="[Merge conflict] Rebase failed")

    # Get only rejection notes
    rejection_notes = temp_db.get_notes(issue_id=issue_id, category="rejection")
    assert len(rejection_notes) == 2
    for note in rejection_notes:
        assert note["category"] == "rejection"

    # Get all notes for issue
    all_notes = temp_db.get_notes(issue_id=issue_id)
    assert len(all_notes) == 4


@pytest.mark.asyncio
async def test_multiple_rejection_notes_accumulate(merge_entry_with_worktree, temp_db, mock_opencode):
    """Test that multiple rejection attempts create multiple notes."""
    info = merge_entry_with_worktree

    mp = MergeProcessor(temp_db, mock_opencode, str(info["git_repo"]), "test")

    # First rejection: rebase conflict
    with patch("hive.merge.rebase_onto_main_async", new_callable=AsyncMock, return_value=False):
        entry = {
            "id": 1,
            "issue_id": info["issue_id"],
            "agent_id": info["agent_id"],
            "worktree": info["worktree_path"],
            "branch_name": info["branch_name"],
        }
        await mp._try_mechanical_merge(entry)

    # Second rejection: test failure (simulating a retry)
    with patch("hive.merge.run_command_in_worktree_async", new_callable=AsyncMock, return_value=(False, "Test failed")):
        with patch("hive.merge.Config") as mock_config:
            mock_config.TEST_COMMAND = "pytest"
            await mp._try_mechanical_merge(entry)

    # Verify both rejection notes exist
    notes = temp_db.get_notes(issue_id=info["issue_id"], category="rejection")
    assert len(notes) == 2

    # Verify they have different content
    contents = [n["content"] for n in notes]
    assert any("[Merge conflict]" in c for c in contents)
    assert any("[Test failure]" in c for c in contents)


@pytest.mark.asyncio
async def test_worker_test_command_only(merge_entry_with_worktree, temp_db, mock_opencode):
    """Test merge with worker test command only — verify it runs."""
    info = merge_entry_with_worktree

    # Update merge queue entry to include worker test_command
    temp_db.conn.execute("UPDATE merge_queue SET test_command = ? WHERE id = 1", ("python -m pytest tests/specific_test.py",))
    temp_db.conn.commit()

    mp = MergeProcessor(temp_db, mock_opencode, str(info["git_repo"]), "test")

    # Mock run_command to succeed
    with patch("hive.merge.run_command_in_worktree_async", new_callable=AsyncMock, return_value=(True, "Tests passed")):
        with patch("hive.merge.Config") as mock_config:
            mock_config.TEST_COMMAND = None  # No global test command
            await mp.process_queue_once()

    # Issue should be finalized
    issue = temp_db.get_issue(info["issue_id"])
    assert issue["status"] == "finalized"

    # Verify test_passed event was logged with worker type
    events = temp_db.get_events(info["issue_id"])
    test_events = [e for e in events if e["event_type"] == "tests_passed"]
    assert len(test_events) == 1
    import json

    detail = json.loads(test_events[0]["detail"])
    assert detail["type"] == "worker"
    assert detail["command"] == "python -m pytest tests/specific_test.py"


@pytest.mark.asyncio
async def test_both_test_commands_run_worker_first(merge_entry_with_worktree, temp_db, mock_opencode):
    """Test merge with both worker and global test commands — verify both run, worker first."""
    info = merge_entry_with_worktree

    # Update merge queue entry to include worker test_command
    temp_db.conn.execute("UPDATE merge_queue SET test_command = ? WHERE id = 1", ("python -m pytest tests/worker.py",))
    temp_db.conn.commit()

    mp = MergeProcessor(temp_db, mock_opencode, str(info["git_repo"]), "test")

    # Track call order
    call_count = {"count": 0}

    async def mock_run_command(worktree, cmd, timeout=300):
        call_count["count"] += 1
        return (True, f"Test {call_count['count']} passed")

    # Mock run_command to succeed for both calls
    with patch("hive.merge.run_command_in_worktree_async", new_callable=AsyncMock) as mock_run:
        mock_run.side_effect = mock_run_command
        with patch("hive.merge.Config") as mock_config:
            mock_config.TEST_COMMAND = "python -m pytest tests/"
            await mp.process_queue_once()

    # Issue should be finalized
    issue = temp_db.get_issue(info["issue_id"])
    assert issue["status"] == "finalized"

    # Verify both test commands were run
    assert call_count["count"] == 2

    # Verify both test commands were run in order
    events = temp_db.get_events(info["issue_id"])
    test_events = [e for e in events if e["event_type"] == "tests_passed"]
    assert len(test_events) == 2

    import json

    # Events are returned newest first, so reverse to get chronological order
    test_events.reverse()

    worker_event = test_events[0]
    global_event = test_events[1]

    worker_detail = json.loads(worker_event["detail"])
    global_detail = json.loads(global_event["detail"])

    # Verify worker test ran first
    assert worker_detail["type"] == "worker"
    assert worker_detail["command"] == "python -m pytest tests/worker.py"

    # Verify global test ran second
    assert global_detail["type"] == "global"
    assert global_detail["command"] == "python -m pytest tests/"


@pytest.mark.asyncio
async def test_worker_test_fails_global_not_run(merge_entry_with_worktree, temp_db, mock_opencode):
    """Test worker test fails — verify merge fails fast without running global tests."""
    info = merge_entry_with_worktree

    # Update merge queue entry to include worker test_command
    temp_db.conn.execute("UPDATE merge_queue SET test_command = ? WHERE id = 1", ("python -m pytest tests/worker.py",))
    temp_db.conn.commit()

    # Mock refinery session
    mock_opencode.create_session = AsyncMock(return_value={"id": "refinery-session"})
    mock_opencode.send_message_async = AsyncMock()
    mock_opencode.get_session_status = AsyncMock(side_effect=[{"type": "busy"}, {"type": "idle"}])
    mock_opencode.get_messages = AsyncMock(return_value=[{"parts": [{"type": "text", "text": "done"}]}])
    mock_opencode.cleanup_session = AsyncMock()

    mp = MergeProcessor(temp_db, mock_opencode, str(info["git_repo"]), "test")

    # Mock run_command: worker fails, global should not be called
    call_count = {"count": 0}

    async def mock_run_command(worktree, cmd, timeout=300):
        call_count["count"] += 1
        if call_count["count"] == 1:
            # Worker test fails
            return (False, "Worker test failed")
        else:
            # Global test should never be called
            raise AssertionError("Global test should not run when worker test fails")

    with patch("hive.merge.run_command_in_worktree_async", new_callable=AsyncMock) as mock_run:
        mock_run.side_effect = mock_run_command
        with patch("hive.merge.asyncio.sleep", new_callable=AsyncMock):
            with patch("hive.merge.Config") as mock_config:
                mock_config.TEST_COMMAND = "python -m pytest tests/"
                mock_config.REFINERY_MODEL = "test-model"
                mock_config.LEASE_DURATION = 30
                mock_config.REFINERY_TOKEN_THRESHOLD = 100000

                with patch("hive.merge.read_result_file", return_value={"status": "rejected", "summary": "Tests failed"}):
                    with patch("hive.merge.remove_result_file"):
                        await mp.process_queue_once()

    # Verify only one test command was run (worker)
    assert call_count["count"] == 1

    # Verify test_failure event was logged for worker test only
    events = temp_db.get_events(info["issue_id"])
    failure_events = [e for e in events if e["event_type"] == "test_failure"]
    assert len(failure_events) == 1

    import json

    detail = json.loads(failure_events[0]["detail"])
    assert detail["type"] == "worker"
    assert detail["command"] == "python -m pytest tests/worker.py"


@pytest.mark.asyncio
async def test_no_test_commands_merge_succeeds(merge_entry_with_worktree, temp_db, mock_opencode):
    """Test merge with no test commands — verify merge proceeds without tests."""
    info = merge_entry_with_worktree

    mp = MergeProcessor(temp_db, mock_opencode, str(info["git_repo"]), "test")

    # Mock run_command should never be called
    with patch("hive.merge.run_command_in_worktree_async", new_callable=AsyncMock) as mock_run:
        mock_run.side_effect = AssertionError("run_command should not be called when no test commands")
        with patch("hive.merge.Config") as mock_config:
            mock_config.TEST_COMMAND = None  # No global test command
            # Merge queue entry already has no test_command
            await mp.process_queue_once()

    # Issue should be finalized
    issue = temp_db.get_issue(info["issue_id"])
    assert issue["status"] == "finalized"

    # Verify no test events were logged
    events = temp_db.get_events(info["issue_id"])
    test_events = [e for e in events if e["event_type"] in ("tests_passed", "test_failure")]
    assert len(test_events) == 0


@pytest.mark.asyncio
async def test_refinery_uses_worker_test_command(merge_entry_with_worktree, temp_db, mock_opencode):
    """Test refinery prompt uses worker test_command when available."""
    info = merge_entry_with_worktree

    # Update merge queue entry to include worker test_command
    temp_db.conn.execute("UPDATE merge_queue SET test_command = ? WHERE id = 1", ("python -m pytest tests/worker.py",))
    temp_db.conn.commit()

    # Mock refinery session
    mock_opencode.create_session = AsyncMock(return_value={"id": "refinery-session"})
    mock_opencode.send_message_async = AsyncMock()
    mock_opencode.get_session_status = AsyncMock(side_effect=[{"type": "busy"}, {"type": "idle"}])
    mock_opencode.get_messages = AsyncMock(return_value=[{"parts": [{"type": "text", "text": "done"}]}])
    mock_opencode.cleanup_session = AsyncMock()

    mp = MergeProcessor(temp_db, mock_opencode, str(info["git_repo"]), "test")

    # Mock test failure to trigger refinery
    with patch("hive.merge.run_command_in_worktree_async", new_callable=AsyncMock, return_value=(False, "Worker test failed")):
        with patch("hive.merge.asyncio.sleep", new_callable=AsyncMock):
            with patch("hive.merge.Config") as mock_config:
                mock_config.TEST_COMMAND = "python -m pytest tests/"  # Global test command
                mock_config.REFINERY_MODEL = "test-model"
                mock_config.LEASE_DURATION = 30
                mock_config.REFINERY_TOKEN_THRESHOLD = 100000

                with patch("hive.merge.build_refinery_prompt") as mock_build_prompt:
                    mock_build_prompt.return_value = "Refinery prompt"

                    with patch("hive.merge.read_result_file", return_value={"status": "merged", "summary": "Fixed"}):
                        with patch("hive.merge.remove_result_file"):
                            await mp.process_queue_once()

                    # Verify build_refinery_prompt was called with worker test_command
                    mock_build_prompt.assert_called_once()
                    call_kwargs = mock_build_prompt.call_args[1]
                    assert call_kwargs["test_command"] == "python -m pytest tests/worker.py"


@pytest.mark.asyncio
async def test_refinery_merged_actually_lands_on_main(merge_entry_with_worktree, temp_db, mock_opencode):
    """Regression: refinery "merged" must call merge_to_main_async so the
    branch actually lands on main, not just get finalized in the DB.

    Before the fix, the refinery success path called _finalize_issue()
    without merge_to_main_async(), leaving worker commits as dangling
    objects that never reached the main branch.
    """
    info = merge_entry_with_worktree

    # Set up refinery mock lifecycle
    mock_opencode.create_session = AsyncMock(return_value={"id": "refinery-session-1"})
    mock_opencode.send_message_async = AsyncMock()
    mock_opencode.get_session_status = AsyncMock(side_effect=[{"type": "busy"}, {"type": "idle"}])
    mock_opencode.get_messages = AsyncMock(return_value=[{"parts": [{"type": "text", "text": "done"}]}])
    mock_opencode.cleanup_session = AsyncMock()

    mp = MergeProcessor(temp_db, mock_opencode, str(info["git_repo"]), "test")

    # Simulate: tests fail → refinery dispatched → refinery reports "merged"
    with patch("hive.merge.run_command_in_worktree_async", new_callable=AsyncMock, return_value=(False, "FAILED test_foo.py")):
        with patch("hive.merge.asyncio.sleep", new_callable=AsyncMock):
            with patch("hive.merge.Config") as mock_config:
                mock_config.TEST_COMMAND = "pytest"
                mock_config.REFINERY_MODEL = "test-model"
                mock_config.LEASE_DURATION = 30
                mock_config.REFINERY_TOKEN_THRESHOLD = 100000

                with patch(
                    "hive.merge.read_result_file",
                    return_value={
                        "status": "merged",
                        "summary": "Fixed failing test",
                        "tests_passed": True,
                        "conflicts_resolved": 0,
                    },
                ):
                    with patch("hive.merge.remove_result_file"):
                        await mp.process_queue_once()

    # DB should show finalized
    issue = temp_db.get_issue(info["issue_id"])
    assert issue["status"] == "finalized"

    # CRITICAL: the worker's file must actually exist on main.
    # Before the fix, the branch was cleaned up but never merged,
    # leaving the commit as a dangling git object.
    assert (info["git_repo"] / "feature.py").exists(), (
        "Worker's feature.py not found on main after refinery merge! merge_to_main_async was not called in the refinery success path."
    )
