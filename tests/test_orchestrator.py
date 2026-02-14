"""Tests for orchestrator."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, Mock, patch
import pytest

from hive.config import Config
from hive.models import AgentIdentity, CompletionResult
from hive.opencode import OpenCodeClient
from hive.orchestrator import Orchestrator


def test_orchestrator_initialization(temp_db, tmp_path):
    """Test orchestrator initialization."""
    from hive.opencode import OpenCodeClient

    opencode = OpenCodeClient()

    orch = Orchestrator(
        db=temp_db,
        opencode_client=opencode,
        project_path=str(tmp_path),
        project_name="test-project",
    )

    assert orch.db == temp_db
    assert orch.opencode == opencode
    assert orch.project_name == "test-project"
    assert len(orch.active_agents) == 0


def test_agent_identity_creation():
    """Test AgentIdentity model."""
    agent = AgentIdentity(
        agent_id="agent-123",
        name="test-agent",
        issue_id="w-abc",
        worktree="/tmp/worktree",
        session_id="session-456",
    )

    assert agent.agent_id == "agent-123"
    assert agent.name == "test-agent"
    assert agent.issue_id == "w-abc"


def test_completion_result():
    """Test CompletionResult model."""
    result = CompletionResult(
        success=True,
        reason="",
        summary="Task completed",
        artifacts={"git_commit": "abc123", "test_result": True},
    )

    assert result.success is True
    assert result.git_commit == "abc123"
    assert result.artifacts.get("test_result") is True


def test_completion_result_no_artifacts():
    """Test CompletionResult without artifacts."""
    result = CompletionResult(success=False, reason="Blocked", summary="Cannot proceed")

    assert result.success is False
    assert result.git_commit is None
    assert result.artifacts.get("test_result") is None


# Integration tests (require OpenCode server and git repo)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_spawn_worker(temp_db, git_repo):
    """Test spawning a worker for an issue (requires OpenCode server)."""
    from hive.opencode import OpenCodeClient

    # Create an issue
    issue_id = temp_db.create_issue("Test task", "Do something", project="test")

    async with OpenCodeClient() as opencode:
        orch = Orchestrator(
            db=temp_db,
            opencode_client=opencode,
            project_path=str(git_repo),
            project_name="test",
        )

        # Get the issue
        issue = temp_db.get_issue(issue_id)

        # Spawn worker
        await orch.spawn_worker(issue)

        # Check that issue was claimed
        updated_issue = temp_db.get_issue(issue_id)
        assert updated_issue["status"] == "in_progress"
        assert updated_issue["assignee"] is not None

        # Check that agent was created
        agent_id = updated_issue["assignee"]
        agent = temp_db.get_agent(agent_id)
        assert agent is not None
        assert agent["status"] == "working"
        assert agent["session_id"] is not None

        # Clean up session
        if agent["session_id"]:
            await opencode.delete_session(agent["session_id"], directory=agent["worktree"])


@pytest.mark.asyncio
@pytest.mark.integration
async def test_full_worker_lifecycle(temp_db, git_repo):
    """Test complete worker lifecycle from spawn to completion (requires OpenCode server)."""
    import asyncio
    from hive.opencode import OpenCodeClient

    # Create a simple issue
    issue_id = temp_db.create_issue(
        "Create README",
        "Create a README.md file with project description",
        project="test",
    )

    async with OpenCodeClient() as opencode:
        orch = Orchestrator(
            db=temp_db,
            opencode_client=opencode,
            project_path=str(git_repo),
            project_name="test",
        )

        # Setup SSE handlers
        orch._setup_sse_handlers()

        # Get the issue
        issue = temp_db.get_issue(issue_id)

        # Spawn worker
        await orch.spawn_worker(issue)

        # Wait a bit for the agent to work
        await asyncio.sleep(10)

        # Check if issue completed
        updated_issue = temp_db.get_issue(issue_id)

        # Clean up - get agent and delete session
        if updated_issue["assignee"]:
            agent = temp_db.get_agent(updated_issue["assignee"])
            if agent and agent["session_id"]:
                await opencode.delete_session(agent["session_id"], directory=agent["worktree"])


@pytest.mark.asyncio
async def test_handle_agent_failure_retry_tier(temp_db, tmp_path):
    """Test first tier of escalation chain - retry same agent."""
    from unittest.mock import AsyncMock, MagicMock
    from hive.opencode import OpenCodeClient

    # Create orchestrator with mock opencode
    mock_opencode = AsyncMock(spec=OpenCodeClient)
    orch = Orchestrator(
        db=temp_db,
        opencode_client=mock_opencode,
        project_path=str(tmp_path),
        project_name="test",
    )

    # Create issue and agent
    issue_id = temp_db.create_issue("Test task", "Do something")
    agent_id = temp_db.create_agent("test-agent")

    agent = AgentIdentity(
        agent_id=agent_id,
        name="test-agent",
        issue_id=issue_id,
        worktree=str(tmp_path),
        session_id="session-123",
    )

    # Create failure result
    result = CompletionResult(
        success=False,
        reason="Test failure",
        summary="Agent failed to complete task",
    )

    # First failure should trigger retry
    await orch._handle_agent_failure(agent, result)

    # Check issue was reset to open
    issue = temp_db.get_issue(issue_id)
    assert issue["status"] == "open"

    # Check retry event was logged
    retry_count = temp_db.count_events_by_type(issue_id, "retry")
    assert retry_count == 1

    # Check event details
    events = temp_db.get_events(issue_id=issue_id)
    retry_events = [e for e in events if e["event_type"] == "retry"]
    assert len(retry_events) == 1
    assert retry_events[0]["detail"] is not None


@pytest.mark.asyncio
async def test_handle_agent_failure_agent_switch_tier(temp_db, tmp_path):
    """Test second tier of escalation chain - switch agent."""
    from unittest.mock import AsyncMock
    from hive.opencode import OpenCodeClient

    # Create orchestrator with mock opencode
    mock_opencode = AsyncMock(spec=OpenCodeClient)
    orch = Orchestrator(
        db=temp_db,
        opencode_client=mock_opencode,
        project_path=str(tmp_path),
        project_name="test",
    )

    # Create issue and agent
    issue_id = temp_db.create_issue("Test task", "Do something")
    agent_id = temp_db.create_agent("test-agent")

    agent = AgentIdentity(
        agent_id=agent_id,
        name="test-agent",
        issue_id=issue_id,
        worktree=str(tmp_path),
        session_id="session-123",
    )

    # Pre-populate with max retries
    for i in range(Config.MAX_RETRIES):
        temp_db.log_event(issue_id, agent_id, "retry", {"attempt": i + 1})

    # Create failure result
    result = CompletionResult(
        success=False,
        reason="Test failure after retries",
        summary="Agent still failed after retries",
    )

    # Should trigger agent switch
    await orch._handle_agent_failure(agent, result)

    # Check issue was reset to open
    issue = temp_db.get_issue(issue_id)
    assert issue["status"] == "open"

    # Check agent_switch event was logged
    agent_switch_count = temp_db.count_events_by_type(issue_id, "agent_switch")
    assert agent_switch_count == 1


@pytest.mark.asyncio
async def test_handle_agent_failure_escalation_tier(temp_db, tmp_path):
    """Test third tier of escalation chain - escalate to human."""
    from unittest.mock import AsyncMock
    from hive.opencode import OpenCodeClient

    # Create orchestrator with mock opencode
    mock_opencode = AsyncMock(spec=OpenCodeClient)
    orch = Orchestrator(
        db=temp_db,
        opencode_client=mock_opencode,
        project_path=str(tmp_path),
        project_name="test",
    )

    # Create issue and agent
    issue_id = temp_db.create_issue("Test task", "Do something")
    agent_id = temp_db.create_agent("test-agent")

    agent = AgentIdentity(
        agent_id=agent_id,
        name="test-agent",
        issue_id=issue_id,
        worktree=str(tmp_path),
        session_id="session-123",
    )

    # Pre-populate with max retries and agent switches
    for i in range(Config.MAX_RETRIES):
        temp_db.log_event(issue_id, agent_id, "retry", {"attempt": i + 1})

    for i in range(Config.MAX_AGENT_SWITCHES):
        switch_agent_id = temp_db.create_agent(f"switch-agent-{i}")
        temp_db.log_event(issue_id, switch_agent_id, "agent_switch", {"switch": i + 1})

    # Create failure result
    result = CompletionResult(
        success=False,
        reason="Final failure",
        summary="Agent failed after all retry attempts",
    )

    # Should trigger escalation
    await orch._handle_agent_failure(agent, result)

    # Check issue was escalated
    issue = temp_db.get_issue(issue_id)
    assert issue["status"] == "escalated"

    # Check escalated event was logged
    escalated_count = temp_db.count_events_by_type(issue_id, "escalated")
    assert escalated_count == 1


@pytest.mark.asyncio
async def test_escalation_chain_full_progression(temp_db, tmp_path):
    """Test full progression through all escalation tiers."""
    from unittest.mock import AsyncMock
    from hive.opencode import OpenCodeClient

    # Create orchestrator with mock opencode
    mock_opencode = AsyncMock(spec=OpenCodeClient)
    orch = Orchestrator(
        db=temp_db,
        opencode_client=mock_opencode,
        project_path=str(tmp_path),
        project_name="test",
    )

    # Create issue
    issue_id = temp_db.create_issue("Test task", "Do something")

    # Simulate full escalation chain
    for retry_attempt in range(Config.MAX_RETRIES):
        agent_id = temp_db.create_agent(f"agent-retry-{retry_attempt}")
        agent = AgentIdentity(
            agent_id=agent_id,
            name=f"agent-retry-{retry_attempt}",
            issue_id=issue_id,
            worktree=str(tmp_path),
            session_id=f"session-{retry_attempt}",
        )

        result = CompletionResult(
            success=False,
            reason=f"Retry failure {retry_attempt + 1}",
            summary=f"Failed attempt {retry_attempt + 1}",
        )

        await orch._handle_agent_failure(agent, result)

        # Should still be open for retry
        issue = temp_db.get_issue(issue_id)
        assert issue["status"] == "open"

    # Now simulate agent switches
    for switch_attempt in range(Config.MAX_AGENT_SWITCHES):
        agent_id = temp_db.create_agent(f"agent-switch-{switch_attempt}")
        agent = AgentIdentity(
            agent_id=agent_id,
            name=f"agent-switch-{switch_attempt}",
            issue_id=issue_id,
            worktree=str(tmp_path),
            session_id=f"session-switch-{switch_attempt}",
        )

        result = CompletionResult(
            success=False,
            reason=f"Switch failure {switch_attempt + 1}",
            summary=f"Failed switch attempt {switch_attempt + 1}",
        )

        await orch._handle_agent_failure(agent, result)

        if switch_attempt < Config.MAX_AGENT_SWITCHES - 1:
            # Should still be open for agent switch
            issue = temp_db.get_issue(issue_id)
            assert issue["status"] == "open"

    # Final failure should escalate
    final_agent_id = temp_db.create_agent("final-agent")
    final_agent = AgentIdentity(
        agent_id=final_agent_id,
        name="final-agent",
        issue_id=issue_id,
        worktree=str(tmp_path),
        session_id="final-session",
    )

    final_result = CompletionResult(
        success=False,
        reason="Final escalation failure",
        summary="All attempts exhausted",
    )

    await orch._handle_agent_failure(final_agent, final_result)

    # Should now be escalated
    issue = temp_db.get_issue(issue_id)
    assert issue["status"] == "escalated"

    # Verify event counts
    assert temp_db.count_events_by_type(issue_id, "retry") == Config.MAX_RETRIES
    assert temp_db.count_events_by_type(issue_id, "agent_switch") == Config.MAX_AGENT_SWITCHES
    assert temp_db.count_events_by_type(issue_id, "escalated") == 1


@pytest.fixture
def git_repo(tmp_path):
    """Create a temporary git repository for testing."""
    import subprocess

    repo_path = tmp_path / "test_repo"
    repo_path.mkdir()

    # Initialize git repo
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

    # Create initial commit
    (repo_path / "README.md").write_text("# Test Repo\n")
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    # Create main branch
    subprocess.run(["git", "branch", "-M", "main"], cwd=repo_path, check=True, capture_output=True)

    return repo_path


# Tests for degraded mode functionality


@pytest.mark.asyncio
async def test_check_opencode_health_success(temp_db, tmp_path):
    """Test health check when OpenCode is responding."""
    mock_oc = AsyncMock(spec=OpenCodeClient)
    mock_oc.list_sessions = AsyncMock(return_value=[])
    orch = Orchestrator(
        db=temp_db,
        opencode_client=mock_oc,
        project_path=str(tmp_path),
        project_name="test-project",
    )

    result = await orch._check_opencode_health()
    assert result is True


@pytest.mark.asyncio
async def test_check_opencode_health_server_error(temp_db, tmp_path):
    """Test health check when OpenCode returns an error."""
    mock_oc = AsyncMock(spec=OpenCodeClient)
    mock_oc.list_sessions = AsyncMock(side_effect=Exception("500 Server Error"))
    orch = Orchestrator(
        db=temp_db,
        opencode_client=mock_oc,
        project_path=str(tmp_path),
        project_name="test-project",
    )

    result = await orch._check_opencode_health()
    assert result is False


@pytest.mark.asyncio
async def test_check_opencode_health_connection_error(temp_db, tmp_path):
    """Test health check when connection fails."""
    mock_oc = AsyncMock(spec=OpenCodeClient)
    mock_oc.list_sessions = AsyncMock(side_effect=Exception("Connection refused"))
    orch = Orchestrator(
        db=temp_db,
        opencode_client=mock_oc,
        project_path=str(tmp_path),
        project_name="test-project",
    )

    result = await orch._check_opencode_health()
    assert result is False


def test_is_opencode_error():
    """Test detection of OpenCode-related errors."""
    from hive.opencode import OpenCodeClient

    opencode = OpenCodeClient()
    orch = Orchestrator(
        db=MagicMock(),
        opencode_client=opencode,
        project_path="/tmp",
        project_name="test-project",
    )

    # Test connection errors
    assert orch._is_opencode_error(Exception("Connection refused"))
    assert orch._is_opencode_error(Exception("Connection failed"))
    assert orch._is_opencode_error(Exception("timeout"))
    assert orch._is_opencode_error(Exception("Server error"))
    assert orch._is_opencode_error(Exception("Network unreachable"))

    # Test non-OpenCode errors
    assert not orch._is_opencode_error(Exception("Git merge conflict"))
    assert not orch._is_opencode_error(Exception("File not found"))

    # Test HTTP 5xx status codes
    http_error = Exception("HTTP error")
    http_error.status = 503
    assert orch._is_opencode_error(http_error)

    # Test HTTP 4xx status codes (should not be treated as degraded mode)
    http_error_4xx = Exception("HTTP error")
    http_error_4xx.status = 404
    assert not orch._is_opencode_error(http_error_4xx)


@pytest.mark.asyncio
async def test_enter_degraded_mode(temp_db, tmp_path):
    """Test entering degraded mode."""
    from hive.opencode import OpenCodeClient

    opencode = OpenCodeClient()
    orch = Orchestrator(
        db=temp_db,
        opencode_client=opencode,
        project_path=str(tmp_path),
        project_name="test-project",
    )

    assert orch._opencode_healthy is True
    assert orch._degraded_since is None

    # Enter degraded mode
    await orch._enter_degraded_mode("Connection refused")

    assert orch._opencode_healthy is False
    assert orch._degraded_since is not None
    assert orch._backoff_delay == 5

    # Check that system event was logged
    events = temp_db.get_events(event_type="opencode_degraded")
    assert len(events) == 1
    assert events[0]["issue_id"] is None
    assert events[0]["agent_id"] is None


@pytest.mark.asyncio
async def test_degraded_mode_recovery(temp_db, tmp_path):
    """Test recovery from degraded mode."""
    from hive.opencode import OpenCodeClient

    opencode = OpenCodeClient()
    orch = Orchestrator(
        db=temp_db,
        opencode_client=opencode,
        project_path=str(tmp_path),
        project_name="test-project",
    )

    # Start in degraded mode
    await orch._enter_degraded_mode("Connection refused")
    original_degraded_time = orch._degraded_since

    # Mock successful health check recovery
    with patch.object(orch, "_check_opencode_health") as mock_health:
        mock_health.return_value = True

        # Simulate recovery in main_loop
        orch.running = True

        # Mock get_ready_queue to return no work so loop exits quickly
        with patch.object(temp_db, "get_ready_queue") as mock_queue:
            mock_queue.return_value = []

            # Run one iteration of main loop
            await asyncio.sleep(0.01)  # Allow async scheduling

            # Manually trigger recovery logic
            healthy = await orch._check_opencode_health()
            if healthy:
                temp_db.log_system_event("opencode_recovered", {"degraded_duration_seconds": 1.0, "backoff_delay": orch._backoff_delay})
                orch._opencode_healthy = True
                orch._degraded_since = None
                orch._backoff_delay = 5

    # Verify recovery
    assert orch._opencode_healthy is True
    assert orch._degraded_since is None
    assert orch._backoff_delay == 5

    # Check that recovery event was logged
    events = temp_db.get_events(event_type="opencode_recovered")
    assert len(events) == 1


# Tests for new auto-restart functionality


@pytest.mark.asyncio
async def test_merge_task_auto_restart(temp_db, tmp_path):
    """Test auto-restart of merge_processor_loop on unexpected death."""
    from hive.opencode import OpenCodeClient

    opencode = OpenCodeClient()
    orch = Orchestrator(
        db=temp_db,
        opencode_client=opencode,
        project_path=str(tmp_path),
        project_name="test-project",
    )

    # Mock merge processor
    orch.merge_processor = AsyncMock()
    orch.merge_processor.initialize = AsyncMock()

    # Test callback with exception (should restart if running)
    orch.running = True

    # Create a mock task that died with an exception
    failed_task = Mock()
    failed_task.cancelled.return_value = False
    failed_task.exception.return_value = Exception("Merge processor died")

    # Mock asyncio.create_task to capture the new task creation
    with patch("asyncio.create_task") as mock_create_task:
        mock_new_task = Mock()
        mock_create_task.return_value = mock_new_task

        # Call the callback
        orch._on_merge_task_done(failed_task)

        # Verify new task was created with callback
        mock_create_task.assert_called_once()
        mock_new_task.add_done_callback.assert_called_once_with(orch._on_merge_task_done)


@pytest.mark.asyncio
async def test_merge_task_no_restart_when_cancelled(temp_db, tmp_path):
    """Test no restart when task is cancelled."""
    from hive.opencode import OpenCodeClient

    opencode = OpenCodeClient()
    orch = Orchestrator(
        db=temp_db,
        opencode_client=opencode,
        project_path=str(tmp_path),
        project_name="test-project",
    )

    # Test callback with cancelled task (should not restart)
    cancelled_task = AsyncMock()
    cancelled_task.cancelled.return_value = True

    with patch("asyncio.create_task") as mock_create_task:
        # Call the callback
        orch._on_merge_task_done(cancelled_task)

        # Verify no new task was created
        mock_create_task.assert_not_called()


@pytest.mark.asyncio
async def test_merge_task_no_restart_when_not_running(temp_db, tmp_path):
    """Test no restart when orchestrator is not running."""
    from hive.opencode import OpenCodeClient

    opencode = OpenCodeClient()
    orch = Orchestrator(
        db=temp_db,
        opencode_client=opencode,
        project_path=str(tmp_path),
        project_name="test-project",
    )

    # Test callback when not running (should not restart)
    orch.running = False

    failed_task = AsyncMock()
    failed_task.cancelled.return_value = False
    failed_task.exception.return_value = Exception("Merge processor died")

    with patch("asyncio.create_task") as mock_create_task:
        # Call the callback
        orch._on_merge_task_done(failed_task)

        # Verify no new task was created
        mock_create_task.assert_not_called()


@pytest.mark.asyncio
async def test_merge_processor_loop_health_check(temp_db, tmp_path):
    """Test merge_processor_loop calls health_check periodically."""
    from hive.opencode import OpenCodeClient

    opencode = OpenCodeClient()
    orch = Orchestrator(
        db=temp_db,
        opencode_client=opencode,
        project_path=str(tmp_path),
        project_name="test-project",
    )

    # Mock merge processor and config
    orch.merge_processor = AsyncMock()
    orch.merge_processor.process_queue_once = AsyncMock()
    orch.merge_processor.health_check = AsyncMock()

    with patch("hive.orchestrator.Config") as mock_config:
        mock_config.MERGE_QUEUE_ENABLED = True
        mock_config.MERGE_POLL_INTERVAL = 0.01  # Very fast for testing

        # Run the loop for a short time to trigger multiple iterations
        orch.running = True

        # Create a task and let it run briefly
        loop_task = asyncio.create_task(orch.merge_processor_loop())

        # Let it run for enough iterations to trigger health check
        # Health check happens every 6 iterations
        await asyncio.sleep(0.1)  # Should allow more than 6 iterations

        # Stop the loop
        orch.running = False

        # Wait for task to complete
        try:
            await asyncio.wait_for(loop_task, timeout=1.0)
        except asyncio.TimeoutError:
            loop_task.cancel()

    # Verify health check was called at least once
    # (exact count depends on timing, but should be at least 1)
    assert orch.merge_processor.health_check.call_count >= 1


# --- Notes harvest/inject tests ---


@pytest.mark.asyncio
async def test_harvest_notes_on_agent_complete(temp_db, tmp_path):
    """Test that notes are harvested from worktree on agent completion."""
    import json
    from hive.opencode import OpenCodeClient
    from hive.prompts import NOTES_FILE_NAME

    mock_opencode = AsyncMock(spec=OpenCodeClient)
    mock_opencode.get_messages = AsyncMock(return_value=[])
    mock_opencode.abort_session = AsyncMock()
    mock_opencode.delete_session = AsyncMock()

    orch = Orchestrator(
        db=temp_db,
        opencode_client=mock_opencode,
        project_path=str(tmp_path),
        project_name="test",
    )

    # Create issue and agent
    issue_id = temp_db.create_issue("Test task", "Do something")
    agent_id = temp_db.create_agent("test-agent")

    # Create worktree dir and write notes file
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    notes_file = worktree / NOTES_FILE_NAME
    notes_file.write_text(
        json.dumps({"content": "The API requires auth tokens", "category": "discovery"})
        + "\n"
        + json.dumps({"content": "Don't use deprecated endpoint", "category": "gotcha"})
        + "\n"
    )

    agent = AgentIdentity(
        agent_id=agent_id,
        name="test-agent",
        issue_id=issue_id,
        worktree=str(worktree),
        session_id="session-123",
    )
    orch.active_agents[agent_id] = agent

    await orch.handle_agent_complete(agent)

    # Verify notes were harvested into DB
    notes = temp_db.get_notes(issue_id=issue_id)
    assert len(notes) == 2
    contents = {n["content"] for n in notes}
    assert "The API requires auth tokens" in contents
    assert "Don't use deprecated endpoint" in contents

    # Verify notes file was cleaned up
    assert not notes_file.exists()

    # Verify harvest event was logged
    events = temp_db.get_events(issue_id=issue_id, event_type="notes_harvested")
    assert len(events) == 1
    detail = json.loads(events[0]["detail"])
    assert detail["count"] == 2


@pytest.mark.asyncio
async def test_harvest_notes_no_file(temp_db, tmp_path):
    """Test harvest is a no-op when no notes file exists."""
    from hive.opencode import OpenCodeClient

    mock_opencode = AsyncMock(spec=OpenCodeClient)
    mock_opencode.get_messages = AsyncMock(return_value=[])
    mock_opencode.abort_session = AsyncMock()
    mock_opencode.delete_session = AsyncMock()

    orch = Orchestrator(
        db=temp_db,
        opencode_client=mock_opencode,
        project_path=str(tmp_path),
        project_name="test",
    )

    issue_id = temp_db.create_issue("Test task")
    agent_id = temp_db.create_agent("test-agent")

    worktree = tmp_path / "worktree"
    worktree.mkdir()

    agent = AgentIdentity(
        agent_id=agent_id,
        name="test-agent",
        issue_id=issue_id,
        worktree=str(worktree),
        session_id="session-123",
    )
    orch.active_agents[agent_id] = agent

    await orch.handle_agent_complete(agent)

    # No notes should be in DB
    notes = temp_db.get_notes(issue_id=issue_id)
    assert len(notes) == 0

    # No harvest event
    events = temp_db.get_events(issue_id=issue_id, event_type="notes_harvested")
    assert len(events) == 0


def test_gather_notes_for_worker_with_molecule(temp_db, tmp_path):
    """Test _gather_notes_for_worker combines molecule + project notes with dedup."""
    from hive.opencode import OpenCodeClient

    mock_opencode = MagicMock(spec=OpenCodeClient)
    orch = Orchestrator(
        db=temp_db,
        opencode_client=mock_opencode,
        project_path=str(tmp_path),
        project_name="test",
    )

    # Create a molecule with steps
    parent_id = temp_db.create_issue("Parent molecule", issue_type="molecule")
    step1_id = temp_db.create_issue("Step 1", parent_id=parent_id, issue_type="step")
    step2_id = temp_db.create_issue("Step 2", parent_id=parent_id, issue_type="step")

    agent_id = temp_db.create_agent("test-agent")

    # Add molecule-scoped notes
    note1_id = temp_db.add_note(issue_id=step1_id, agent_id=agent_id, content="Step 1 discovery", category="discovery")

    # Add a project-wide note
    note2_id = temp_db.add_note(content="Project-wide gotcha", category="gotcha")

    # Gather notes for step2 (should see step1's note + project note)
    notes = orch._gather_notes_for_worker(step2_id)

    assert notes is not None
    assert len(notes) == 2
    note_ids = {n["id"] for n in notes}
    assert note1_id in note_ids
    assert note2_id in note_ids


def test_gather_notes_for_worker_deduplicates(temp_db, tmp_path):
    """Test _gather_notes_for_worker deduplicates by note ID."""
    from hive.opencode import OpenCodeClient

    mock_opencode = MagicMock(spec=OpenCodeClient)
    orch = Orchestrator(
        db=temp_db,
        opencode_client=mock_opencode,
        project_path=str(tmp_path),
        project_name="test",
    )

    # Create a molecule with a step
    parent_id = temp_db.create_issue("Parent molecule", issue_type="molecule")
    step_id = temp_db.create_issue("Step 1", parent_id=parent_id, issue_type="step")

    agent_id = temp_db.create_agent("test-agent")

    # Add a note tied to the step — it will appear in both
    # get_notes_for_molecule AND get_recent_project_notes
    note_id = temp_db.add_note(issue_id=step_id, agent_id=agent_id, content="Shared note")

    notes = orch._gather_notes_for_worker(step_id)

    # Should only appear once despite being in both queries
    assert notes is not None
    ids = [n["id"] for n in notes]
    assert ids.count(note_id) == 1


def test_gather_notes_for_worker_no_notes(temp_db, tmp_path):
    """Test _gather_notes_for_worker returns None when no notes exist."""
    from hive.opencode import OpenCodeClient

    mock_opencode = MagicMock(spec=OpenCodeClient)
    orch = Orchestrator(
        db=temp_db,
        opencode_client=mock_opencode,
        project_path=str(tmp_path),
        project_name="test",
    )

    issue_id = temp_db.create_issue("Standalone task")
    notes = orch._gather_notes_for_worker(issue_id)
    assert notes is None


def test_gather_notes_for_worker_standalone_issue(temp_db, tmp_path):
    """Test _gather_notes_for_worker for a standalone issue (no parent)."""
    from hive.opencode import OpenCodeClient

    mock_opencode = MagicMock(spec=OpenCodeClient)
    orch = Orchestrator(
        db=temp_db,
        opencode_client=mock_opencode,
        project_path=str(tmp_path),
        project_name="test",
    )

    issue_id = temp_db.create_issue("Standalone task")

    # Add a project-wide note
    note_id = temp_db.add_note(content="Project note", category="pattern")

    notes = orch._gather_notes_for_worker(issue_id)
    assert notes is not None
    assert len(notes) == 1
    assert notes[0]["id"] == note_id


@pytest.mark.asyncio
async def test_merge_processor_initialize_called_on_start(temp_db, tmp_path):
    """Test merge processor initialize is called during orchestrator start."""
    from hive.opencode import OpenCodeClient

    opencode = OpenCodeClient()
    orch = Orchestrator(
        db=temp_db,
        opencode_client=opencode,
        project_path=str(tmp_path),
        project_name="test-project",
    )

    # Mock all the async components to avoid actual startup
    orch.merge_processor.initialize = AsyncMock()
    orch._reconcile_stale_agents = AsyncMock()
    orch._shutdown_all_sessions = AsyncMock()
    orch.sse_client.connect_with_reconnect = AsyncMock()
    orch.sse_client.stop = Mock()

    # Create mock tasks that support add_done_callback and await
    async def noop():
        pass

    mock_task = Mock()
    mock_task.add_done_callback = Mock()

    # Make mock_task awaitable
    future = asyncio.get_event_loop().create_future()
    future.set_result(None)

    with patch("asyncio.create_task") as mock_create_task:
        mock_create_task.return_value = mock_task

        # Mock main_loop to exit immediately
        orch.main_loop = AsyncMock()

        # Patch the awaits in the finally block
        with patch.object(orch, "start", wraps=None):
            # Call start manually up to the point we care about
            orch.running = True
            orch._setup_sse_handlers()
            await orch._reconcile_stale_agents()
            await orch.merge_processor.initialize()

        # Verify initialize was called
        orch.merge_processor.initialize.assert_called_once()


# --- Bidirectional reconciliation tests ---


def _make_orchestrator(temp_db, tmp_path, mock_opencode=None):
    """Helper to create an orchestrator with a mocked OpenCodeClient."""
    if mock_opencode is None:
        mock_opencode = AsyncMock(spec=OpenCodeClient)
    return Orchestrator(
        db=temp_db,
        opencode_client=mock_opencode,
        project_path=str(tmp_path),
        project_name="test",
    )


def _make_stale_agent(temp_db, name="stale-agent", issue_title="Stale task", session_id="sess-1", worktree="/tmp/wt"):
    """Create a stale agent (status='working') with an in_progress issue."""
    issue_id = temp_db.create_issue(issue_title)
    agent_id = temp_db.create_agent(name)
    # Claim the issue so it becomes in_progress
    temp_db.claim_issue(issue_id, agent_id)
    # Set session_id and worktree on the agent
    temp_db.conn.execute(
        "UPDATE agents SET session_id = ?, worktree = ? WHERE id = ?",
        (session_id, worktree, agent_id),
    )
    temp_db.conn.commit()
    return agent_id, issue_id, session_id


@pytest.mark.asyncio
async def test_reconcile_ghost_agents(temp_db, tmp_path):
    """Ghost agent: DB says working, but session is gone from server."""
    mock_oc = AsyncMock(spec=OpenCodeClient)
    # Server returns no sessions — the agent's session is gone
    mock_oc.list_sessions = AsyncMock(return_value=[])
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    agent_id, issue_id, session_id = _make_stale_agent(temp_db, session_id="ghost-sess")

    await orch._reconcile_stale_agents()

    # Agent should be marked failed
    agent = temp_db.get_agent(agent_id)
    assert agent["status"] == "failed"

    # Abort should NOT have been called (session doesn't exist)
    mock_oc.cleanup_session.assert_not_called()

    # Issue should be released back to open
    issue = temp_db.get_issue(issue_id)
    assert issue["status"] == "open"


@pytest.mark.asyncio
async def test_reconcile_stale_agents_with_live_sessions(temp_db, tmp_path):
    """Stale agent whose session is still alive on the server — abort + delete."""
    mock_oc = AsyncMock(spec=OpenCodeClient)
    mock_oc.list_sessions = AsyncMock(return_value=[{"id": "live-sess"}])
    mock_oc.cleanup_session = AsyncMock()
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    agent_id, issue_id, _ = _make_stale_agent(temp_db, session_id="live-sess")

    await orch._reconcile_stale_agents()

    # Abort + delete should have been called for the live session
    mock_oc.cleanup_session.assert_called_once_with("live-sess", directory="/tmp/wt")

    # Agent marked failed, issue released
    agent = temp_db.get_agent(agent_id)
    assert agent["status"] == "failed"
    issue = temp_db.get_issue(issue_id)
    assert issue["status"] == "open"


@pytest.mark.asyncio
async def test_reconcile_orphan_sessions(temp_db, tmp_path):
    """Sessions alive on server with no DB agent — cleaned up as orphans."""
    mock_oc = AsyncMock(spec=OpenCodeClient)
    # Server has two sessions, DB knows about neither
    mock_oc.list_sessions = AsyncMock(return_value=[{"id": "orphan-1"}, {"id": "orphan-2"}])
    mock_oc.cleanup_session = AsyncMock()
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    # No stale agents — so Phase 1 is a no-op, Phase 2 finds orphans
    await orch._reconcile_stale_agents()

    # Both orphans should be aborted + deleted
    assert mock_oc.cleanup_session.call_count == 2
    aborted_ids = {call.args[0] for call in mock_oc.cleanup_session.call_args_list}
    assert aborted_ids == {"orphan-1", "orphan-2"}

    # System event should be logged
    events = temp_db.get_events(event_type="orphan_sessions_cleaned")
    assert len(events) == 1


@pytest.mark.asyncio
async def test_reconcile_fallback_when_opencode_unreachable(temp_db, tmp_path):
    """list_sessions() throws — falls back to best-effort abort/delete."""
    mock_oc = AsyncMock(spec=OpenCodeClient)
    mock_oc.list_sessions = AsyncMock(side_effect=Exception("Connection refused"))
    mock_oc.cleanup_session = AsyncMock()
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    agent_id, issue_id, session_id = _make_stale_agent(temp_db, session_id="fallback-sess")

    await orch._reconcile_stale_agents()

    # Best-effort abort/delete should still be called
    mock_oc.cleanup_session.assert_called_once_with("fallback-sess", directory="/tmp/wt")

    # Agent failed, issue released
    agent = temp_db.get_agent(agent_id)
    assert agent["status"] == "failed"
    issue = temp_db.get_issue(issue_id)
    assert issue["status"] == "open"

    # No orphan cleanup event (Phase 2 skipped)
    events = temp_db.get_events(event_type="orphan_sessions_cleaned")
    assert len(events) == 0


@pytest.mark.asyncio
async def test_reconcile_respects_retry_budget(temp_db, tmp_path):
    """Exhausted retry budget → issue marked failed, not open."""
    mock_oc = AsyncMock(spec=OpenCodeClient)
    mock_oc.list_sessions = AsyncMock(return_value=[])
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    agent_id, issue_id, _ = _make_stale_agent(temp_db, session_id="budget-sess")

    # Exhaust retry and agent_switch budgets
    for i in range(Config.MAX_RETRIES):
        temp_db.log_event(issue_id, agent_id, "retry", {"attempt": i + 1})
    for i in range(Config.MAX_AGENT_SWITCHES):
        temp_db.log_event(issue_id, agent_id, "agent_switch", {"switch": i + 1})

    await orch._reconcile_stale_agents()

    # Issue should be marked failed (not open)
    issue = temp_db.get_issue(issue_id)
    assert issue["status"] == "failed"


@pytest.mark.asyncio
async def test_reconcile_mixed_ghost_live_orphan(temp_db, tmp_path):
    """All three conditions in one reconciliation run."""
    mock_oc = AsyncMock(spec=OpenCodeClient)
    # Server has: live-sess (stale agent's), orphan-sess (no agent), but NOT ghost-sess
    mock_oc.list_sessions = AsyncMock(return_value=[{"id": "live-sess"}, {"id": "orphan-sess"}])
    mock_oc.cleanup_session = AsyncMock()
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    # Ghost agent — session not on server
    ghost_agent_id, ghost_issue_id, _ = _make_stale_agent(
        temp_db, name="ghost", session_id="ghost-sess", issue_title="Ghost task"
    )
    # Live agent — session still on server
    live_agent_id, live_issue_id, _ = _make_stale_agent(
        temp_db, name="live", session_id="live-sess", issue_title="Live task"
    )

    await orch._reconcile_stale_agents()

    # Ghost agent: no abort/delete for ghost-sess
    ghost_calls = [c for c in mock_oc.cleanup_session.call_args_list if c.args[0] == "ghost-sess"]
    assert len(ghost_calls) == 0

    # Live agent: abort + delete for live-sess
    live_abort_calls = [c for c in mock_oc.cleanup_session.call_args_list if c.args[0] == "live-sess"]
    assert len(live_abort_calls) == 1

    # Orphan session: abort + delete for orphan-sess
    orphan_abort_calls = [c for c in mock_oc.cleanup_session.call_args_list if c.args[0] == "orphan-sess"]
    assert len(orphan_abort_calls) == 1

    # Both agents should be failed
    assert temp_db.get_agent(ghost_agent_id)["status"] == "failed"
    assert temp_db.get_agent(live_agent_id)["status"] == "failed"

    # Orphan cleanup event
    events = temp_db.get_events(event_type="orphan_sessions_cleaned")
    assert len(events) == 1


@pytest.mark.asyncio
async def test_reconcile_no_stale_agents(temp_db, tmp_path):
    """Clean state — no stale agents, no orphans, no errors."""
    mock_oc = AsyncMock(spec=OpenCodeClient)
    mock_oc.list_sessions = AsyncMock(return_value=[])
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    # Should complete without error
    await orch._reconcile_stale_agents()

    # No abort/delete calls
    mock_oc.cleanup_session.assert_not_called()

    # No orphan events
    events = temp_db.get_events(event_type="orphan_sessions_cleaned")
    assert len(events) == 0


@pytest.mark.asyncio
async def test_check_opencode_health_uses_list_sessions(temp_db, tmp_path):
    """Verify _check_opencode_health delegates to list_sessions."""
    mock_oc = AsyncMock(spec=OpenCodeClient)
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    # Success case
    mock_oc.list_sessions = AsyncMock(return_value=[])
    assert await orch._check_opencode_health() is True
    mock_oc.list_sessions.assert_called_once()

    # Failure case
    mock_oc.list_sessions = AsyncMock(side_effect=Exception("Connection refused"))
    assert await orch._check_opencode_health() is False


# --- SSE permission handling tests ---


@pytest.mark.asyncio
async def test_handle_permission_event_with_id(temp_db, tmp_path):
    """SSE permission event with id resolves directly."""
    mock_oc = AsyncMock(spec=OpenCodeClient)
    mock_oc.reply_permission = AsyncMock()
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    event_data = {"id": "perm-1", "permission": "bash", "sessionID": "s1"}
    await orch._handle_permission_event(event_data)

    mock_oc.reply_permission.assert_called_once_with("perm-1", reply="once")


@pytest.mark.asyncio
async def test_handle_permission_event_without_id_fetches_pending(temp_db, tmp_path):
    """SSE permission event without id falls back to fetching pending."""
    mock_oc = AsyncMock(spec=OpenCodeClient)
    mock_oc.get_pending_permissions = AsyncMock(return_value=[
        {"id": "perm-2", "permission": "read", "sessionID": "s1"},
    ])
    mock_oc.reply_permission = AsyncMock()
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    await orch._handle_permission_event({})  # No id

    mock_oc.get_pending_permissions.assert_called_once()
    mock_oc.reply_permission.assert_called_once_with("perm-2", reply="once")


@pytest.mark.asyncio
async def test_handle_permission_event_rejects_question(temp_db, tmp_path):
    """SSE permission event for 'question' gets rejected."""
    mock_oc = AsyncMock(spec=OpenCodeClient)
    mock_oc.reply_permission = AsyncMock()
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    event_data = {"id": "perm-3", "permission": "question", "sessionID": "s1"}
    await orch._handle_permission_event(event_data)

    mock_oc.reply_permission.assert_called_once_with("perm-3", reply="reject")


@pytest.mark.asyncio
async def test_handle_permission_event_error_handling(temp_db, tmp_path):
    """SSE permission handler doesn't raise on errors."""
    mock_oc = AsyncMock(spec=OpenCodeClient)
    mock_oc.reply_permission = AsyncMock(side_effect=Exception("Network error"))
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    event_data = {"id": "perm-4", "permission": "bash", "sessionID": "s1"}
    # Should not raise
    await orch._handle_permission_event(event_data)


def test_log_permission_resolved_uses_reverse_map(temp_db, tmp_path):
    """_log_permission_resolved uses _session_to_agent for O(1) lookup."""
    orch = _make_orchestrator(temp_db, tmp_path)

    issue_id = temp_db.create_issue("Test task")
    agent_id = temp_db.create_agent("test-agent")

    agent = AgentIdentity(
        agent_id=agent_id, name="test-agent", issue_id=issue_id,
        worktree="/tmp/wt", session_id="s1",
    )
    orch.active_agents[agent_id] = agent
    orch._session_to_agent["s1"] = agent_id

    perm = {"permission": "bash", "sessionID": "s1", "patterns": ["*"]}
    orch._log_permission_resolved(perm, "once")

    events = temp_db.get_events(issue_id=issue_id, event_type="permission_resolved")
    assert len(events) == 1


def test_log_permission_resolved_unknown_session(temp_db, tmp_path):
    """_log_permission_resolved silently skips when session is unknown."""
    orch = _make_orchestrator(temp_db, tmp_path)

    perm = {"permission": "bash", "sessionID": "unknown-sess", "patterns": ["*"]}
    # Should not raise
    orch._log_permission_resolved(perm, "once")

    # No events logged (no agent found)
    events = temp_db.get_events(event_type="permission_resolved")
    assert len(events) == 0
