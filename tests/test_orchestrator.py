"""Tests for orchestrator."""

import pytest

from hive.config import Config
from hive.models import AgentIdentity, CompletionResult
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
        project="test",
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
    assert result.test_result is True


def test_completion_result_no_artifacts():
    """Test CompletionResult without artifacts."""
    result = CompletionResult(success=False, reason="Blocked", summary="Cannot proceed")

    assert result.success is False
    assert result.git_commit is None
    assert result.test_result is None


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
        project="test",
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
        project="test",
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
        project="test",
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
            project="test",
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
            project="test",
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
        project="test",
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
