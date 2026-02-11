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
    subprocess.run(
        ["git", "branch", "-M", "main"], cwd=repo_path, check=True, capture_output=True
    )

    return repo_path
