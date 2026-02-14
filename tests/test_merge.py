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

    refinery_messages = [
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

    # Mock refinery session with proper lifecycle:
    # get_session_status: first call returns "busy" (post-send check), then "idle" (wait loop)
    # get_messages: first call returns [] (pre-send count), then refinery result
    mock_opencode.create_session = AsyncMock(return_value={"id": "refinery-session-1"})
    mock_opencode.send_message_async = AsyncMock()
    mock_opencode.get_session_status = AsyncMock(side_effect=[{"type": "busy"}, {"type": "idle"}])
    mock_opencode.get_messages = AsyncMock(side_effect=[[], refinery_messages])
    mock_opencode.abort_session = AsyncMock()
    mock_opencode.delete_session = AsyncMock()

    mp = MergeProcessor(temp_db, mock_opencode, str(info["git_repo"]), "test")

    # Patch rebase_onto_main to simulate rebase conflict
    with patch("hive.merge.rebase_onto_main", return_value=False):
        with patch("hive.merge.asyncio.sleep", new_callable=AsyncMock):
            with patch("hive.merge.Config") as mock_config:
                mock_config.TEST_COMMAND = None
                mock_config.REFINERY_MODEL = "test-model"
                mock_config.LEASE_DURATION = 30
                mock_config.REFINERY_TOKEN_THRESHOLD = 100000

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

    refinery_messages = [
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

    # Mock refinery session with proper lifecycle:
    # get_session_status: first call returns "busy" (post-send check), then "idle" (wait loop)
    # get_messages: first call returns [] (pre-send count), then refinery result
    mock_opencode.create_session = AsyncMock(return_value={"id": "refinery-session-1"})
    mock_opencode.send_message_async = AsyncMock()
    mock_opencode.get_session_status = AsyncMock(side_effect=[{"type": "busy"}, {"type": "idle"}])
    mock_opencode.get_messages = AsyncMock(side_effect=[[], refinery_messages])
    mock_opencode.abort_session = AsyncMock()
    mock_opencode.delete_session = AsyncMock()

    mp = MergeProcessor(temp_db, mock_opencode, str(info["git_repo"]), "test")

    # Patch run_command_in_worktree to simulate test failure and Config
    with patch("hive.merge.run_command_in_worktree", return_value=(False, "FAILED test_foo.py")):
        with patch("hive.merge.asyncio.sleep", new_callable=AsyncMock):
            with patch("hive.merge.Config") as mock_config:
                mock_config.TEST_COMMAND = "pytest"
                mock_config.REFINERY_MODEL = "test-model"
                mock_config.LEASE_DURATION = 30
                mock_config.REFINERY_TOKEN_THRESHOLD = 100000

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

    # Agent should be idle
    agent = temp_db.get_agent(info["agent_id"])
    assert agent["status"] == "idle"
    assert agent["current_issue"] is None


@pytest.mark.asyncio
async def test_teardown_after_finalize(merge_entry_with_worktree, temp_db, mock_opencode):
    """Test _teardown_after_finalize cleans up worktree and agent."""
    info = merge_entry_with_worktree

    mp = MergeProcessor(temp_db, mock_opencode, str(info["git_repo"]), "test")

    cursor = temp_db.conn.execute("SELECT * FROM merge_queue WHERE id = 1")
    row = cursor.fetchone()
    entry = dict(row) if row else {}
    await mp._teardown_after_finalize(entry)

    # Worktree gone
    assert not Path(info["worktree_path"]).exists()

    # Agent idle
    agent = temp_db.get_agent(info["agent_id"])
    assert agent["status"] == "idle"


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

    # Set up a refinery session
    mp.refinery_session_id = "test-session-123"

    # Mock get_messages to return high token count
    mock_opencode.get_messages = AsyncMock(
        return_value=[
            {
                "metadata": {"token_count": 120000},  # Exceeds REFINERY_TOKEN_THRESHOLD (100000)
                "parts": [{"type": "text", "text": "message 1"}],
            },
            {"metadata": {"token_count": 30000}, "parts": [{"type": "text", "text": "message 2"}]},
        ]
    )

    # Mock abort and delete session calls
    mock_opencode.abort_session = AsyncMock()
    mock_opencode.delete_session = AsyncMock()

    # Call the token check method
    await mp._maybe_cycle_refinery_session()

    # Verify session was cycled
    assert mp.refinery_session_id is None
    mock_opencode.abort_session.assert_called_once_with("test-session-123", directory=mp.project_path)
    mock_opencode.delete_session.assert_called_once_with("test-session-123", directory=mp.project_path)

    # Verify event was logged
    events = temp_db.get_events(None)  # Get all events
    cycling_events = [e for e in events if e["event_type"] == "refinery_session_cycled"]
    assert len(cycling_events) == 1

    event = cycling_events[0]
    import json

    details = json.loads(event["detail"])
    assert details["session_id"] == "test-session-123"
    assert details["token_count"] == 150000  # 120000 + 30000
    assert details["threshold"] == 100000


@pytest.mark.asyncio
async def test_refinery_session_cycling_by_message_count(temp_db, mock_opencode):
    """Test refinery session is cycled when message count exceeds threshold (fallback)."""
    mp = MergeProcessor(temp_db, mock_opencode, "/tmp/project", "test")

    # Set up a refinery session
    mp.refinery_session_id = "test-session-456"

    # Mock get_messages to return many messages without token metadata
    messages = []
    for i in range(25):  # More than 20 messages (our fallback threshold)
        messages.append({"parts": [{"type": "text", "text": f"message {i}"}]})

    mock_opencode.get_messages = AsyncMock(return_value=messages)
    mock_opencode.abort_session = AsyncMock()
    mock_opencode.delete_session = AsyncMock()

    # Call the token check method
    await mp._maybe_cycle_refinery_session()

    # Verify session was cycled
    assert mp.refinery_session_id is None
    mock_opencode.abort_session.assert_called_once_with("test-session-456", directory=mp.project_path)
    mock_opencode.delete_session.assert_called_once_with("test-session-456", directory=mp.project_path)


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

    mock_opencode.abort_session = AsyncMock()
    mock_opencode.delete_session = AsyncMock()

    # Call the token check method
    await mp._maybe_cycle_refinery_session()

    # Verify session was NOT cycled
    assert mp.refinery_session_id == "test-session-789"
    mock_opencode.abort_session.assert_not_called()
    mock_opencode.delete_session.assert_not_called()


# --- New tests for hardened refinery session ---


@pytest.mark.asyncio
async def test_force_reset_refinery_session(temp_db, mock_opencode):
    """Test _force_reset_refinery_session cleans up and resets session ID."""
    mp = MergeProcessor(temp_db, mock_opencode, "/tmp/project", "test")

    # Set up a session to reset
    original_session_id = "session-to-reset"
    mp.refinery_session_id = original_session_id

    mock_opencode.abort_session = AsyncMock()
    mock_opencode.delete_session = AsyncMock()

    # Call force reset
    reason = "Test reset"
    await mp._force_reset_refinery_session(reason)

    # Verify session ID was cleared
    assert mp.refinery_session_id is None

    # Verify abort and delete were called
    mock_opencode.abort_session.assert_called_once_with(original_session_id, directory=mp.project_path)
    mock_opencode.delete_session.assert_called_once_with(original_session_id, directory=mp.project_path)

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

    mock_opencode.abort_session = AsyncMock()
    mock_opencode.delete_session = AsyncMock()

    # Call force reset - should do nothing
    await mp._force_reset_refinery_session("No session to reset")

    # Verify no calls were made
    mock_opencode.abort_session.assert_not_called()
    mock_opencode.delete_session.assert_not_called()


@pytest.mark.asyncio
async def test_wait_for_refinery_consecutive_errors(temp_db, mock_opencode):
    """Test _wait_for_refinery bails after consecutive errors."""
    mp = MergeProcessor(temp_db, mock_opencode, "/tmp/project", "test")

    # Mock get_session_status to always throw exceptions
    mock_opencode.get_session_status = AsyncMock(side_effect=Exception("Connection failed"))

    # Patch asyncio.sleep to be instant so the test doesn't take 25+ seconds
    with patch("hive.merge.asyncio.sleep", new_callable=AsyncMock):
        result = await mp._wait_for_refinery("test-session", timeout=60)

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

    # Mock parse_merge_result
    with patch("hive.merge.parse_merge_result") as mock_parse:
        mock_parse.return_value = {"status": "merged", "summary": "Success"}

        # Call with min_message_count=1 (fence)
        result = await mp._wait_for_refinery("test-session", timeout=30, min_message_count=1)

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

    mock_opencode.abort_session = AsyncMock()
    mock_opencode.delete_session = AsyncMock()

    # Patch config
    with patch("hive.merge.Config") as mock_config:
        mock_config.REFINERY_MODEL = "test-model"
        mock_config.TEST_COMMAND = None

        # Should raise RuntimeError and reset session
        await mp._send_to_refinery(entry)

    # Verify session was reset due to unprocessed message
    assert mp.refinery_session_id is None
    mock_opencode.abort_session.assert_called()
    mock_opencode.delete_session.assert_called()

    # Verify merge queue was marked failed
    cursor = temp_db.conn.execute("SELECT status FROM merge_queue WHERE id = ?", (queue_id,))
    row = cursor.fetchone()
    assert row is not None
    assert row["status"] == "failed"
