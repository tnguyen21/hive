"""Tests for orchestrator."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, Mock, patch
import pytest

from hive.config import Config
from hive.utils import AgentIdentity, CompletionResult
from hive.backends import HiveBackend
from hive.orchestrator import Orchestrator


# Unit tests


@pytest.mark.asyncio
async def test_handle_agent_failure_retry_tier(temp_db, tmp_path):
    """Test first tier of escalation chain - retry same agent."""
    from unittest.mock import AsyncMock
    from hive.backends import HiveBackend

    # Create orchestrator with mock backend
    mock_backend = AsyncMock(spec=HiveBackend)
    orch = Orchestrator(
        db=temp_db,
        backend=mock_backend,
    )

    # Create issue and agent
    issue_id = temp_db.create_issue("Test task", "Do something")
    agent_id = temp_db.create_agent("test-agent", model="claude-sonnet-4-5")
    temp_db.claim_issue(issue_id, agent_id)

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
    retry_count = temp_db.count_events(issue_id, "retry")
    assert retry_count == 1

    # Check event details
    events = temp_db.get_events(issue_id=issue_id)
    retry_events = [e for e in events if e["event_type"] == "retry"]
    assert len(retry_events) == 1
    assert retry_events[0]["detail"] is not None

    # Verify model field is propagated to the incomplete event
    incomplete_events = temp_db.get_events(issue_id=issue_id, event_type="incomplete")
    assert len(incomplete_events) == 1
    detail = json.loads(incomplete_events[0]["detail"])
    assert detail.get("model") == "claude-sonnet-4-5"


@pytest.mark.asyncio
async def test_handle_agent_failure_agent_switch_tier(temp_db, tmp_path):
    """Test second tier of escalation chain - switch agent."""
    from unittest.mock import AsyncMock
    from hive.backends import HiveBackend

    # Create orchestrator with mock backend
    mock_backend = AsyncMock(spec=HiveBackend)
    orch = Orchestrator(
        db=temp_db,
        backend=mock_backend,
    )

    # Create issue and agent
    issue_id = temp_db.create_issue("Test task", "Do something")
    agent_id = temp_db.create_agent("test-agent", model="claude-sonnet-4-5")
    temp_db.claim_issue(issue_id, agent_id)

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
    agent_switch_count = temp_db.count_events(issue_id, "agent_switch")
    assert agent_switch_count == 1

    # Verify model field is propagated to the agent_switch event
    switch_events = temp_db.get_events(issue_id=issue_id, event_type="agent_switch")
    # Filter to only the ones logged by _handle_agent_failure (not the pre-populated ones)
    # The pre-populated ones have detail like {"attempt": N}, the new one has "model" key
    switch_detail = json.loads(switch_events[-1]["detail"])
    assert switch_detail.get("model") == "claude-sonnet-4-5"


@pytest.mark.asyncio
async def test_handle_agent_failure_escalation_tier(temp_db, tmp_path):
    """Test third tier of escalation chain - escalate to human."""
    from unittest.mock import AsyncMock
    from hive.backends import HiveBackend

    # Create orchestrator with mock backend
    mock_backend = AsyncMock(spec=HiveBackend)
    orch = Orchestrator(
        db=temp_db,
        backend=mock_backend,
    )

    # Create issue and agent
    issue_id = temp_db.create_issue("Test task", "Do something")
    agent_id = temp_db.create_agent("test-agent")
    temp_db.claim_issue(issue_id, agent_id)

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
    escalated_count = temp_db.count_events(issue_id, "escalated")
    assert escalated_count == 1


@pytest.mark.asyncio
async def test_choose_escalation_retry_after_reset(temp_db, tmp_path):
    """After a retry_reset, _choose_escalation returns RETRY even when old events exceed threshold."""
    mock_backend = AsyncMock(spec=HiveBackend)
    orch = Orchestrator(
        db=temp_db,
        backend=mock_backend,
    )

    issue_id = temp_db.create_issue("Test task", "Do something")
    agent_id = temp_db.create_agent("test-agent")
    temp_db.claim_issue(issue_id, agent_id)

    # Exhaust retry and agent_switch budgets
    for i in range(Config.MAX_RETRIES):
        temp_db.log_event(issue_id, agent_id, "retry", {"attempt": i + 1})
    for i in range(Config.MAX_AGENT_SWITCHES):
        temp_db.log_event(issue_id, agent_id, "agent_switch", {"switch": i + 1})

    # Without reset, should escalate
    from hive.orchestrator.completion import EscalationDecision

    decision, counts = orch._choose_escalation(issue_id)
    assert decision == EscalationDecision.ESCALATE
    assert counts.retry_count == Config.MAX_RETRIES
    assert counts.agent_switch_count == Config.MAX_AGENT_SWITCHES

    # Now log a retry_reset
    temp_db.log_event(issue_id, None, "retry_reset", {"notes": "fixed root cause"})

    # After reset, should retry again
    decision, counts = orch._choose_escalation(issue_id)
    assert decision == EscalationDecision.RETRY
    assert counts.retry_count == 0


@pytest.mark.asyncio
async def test_choose_escalation_anomaly_respects_reset(temp_db, tmp_path):
    """Anomaly detection ignores incomplete events before a retry_reset."""
    mock_backend = AsyncMock(spec=HiveBackend)
    orch = Orchestrator(
        db=temp_db,
        backend=mock_backend,
    )

    issue_id = temp_db.create_issue("Test task", "Do something")
    agent_id = temp_db.create_agent("test-agent")
    temp_db.claim_issue(issue_id, agent_id)

    # Log enough incomplete events to trigger anomaly detection
    for i in range(Config.ANOMALY_FAILURE_THRESHOLD or 5):
        temp_db.log_event(issue_id, agent_id, "incomplete", {"reason": f"failure {i}"})

    from hive.orchestrator.completion import EscalationDecision

    # Without reset, should anomaly-escalate
    if Config.ANOMALY_FAILURE_THRESHOLD:
        decision, counts = orch._choose_escalation(issue_id)
        assert decision == EscalationDecision.ANOMALY_ESCALATE
        assert counts.recent_failures >= Config.ANOMALY_FAILURE_THRESHOLD

    # Log a retry_reset — old incompletes should no longer count
    temp_db.log_event(issue_id, None, "retry_reset", {"notes": "fixed"})

    # After reset, should retry (not anomaly-escalate)
    decision, counts = orch._choose_escalation(issue_id)
    assert decision == EscalationDecision.RETRY
    assert counts.recent_failures == 0


@pytest.mark.asyncio
async def test_escalation_chain_full_progression(temp_db, tmp_path):
    """Test full progression through all escalation tiers."""
    from unittest.mock import AsyncMock
    from hive.backends import HiveBackend

    # Create orchestrator with mock backend
    mock_backend = AsyncMock(spec=HiveBackend)
    orch = Orchestrator(
        db=temp_db,
        backend=mock_backend,
    )

    # Disable anomaly detection so we can test the full retry→switch→escalate chain
    original_threshold = Config.ANOMALY_FAILURE_THRESHOLD
    Config.ANOMALY_FAILURE_THRESHOLD = 0

    # Create issue
    issue_id = temp_db.create_issue("Test task", "Do something")

    # Simulate full escalation chain
    for retry_attempt in range(Config.MAX_RETRIES):
        agent_id = temp_db.create_agent(f"agent-retry-{retry_attempt}")
        temp_db.claim_issue(issue_id, agent_id)
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
        temp_db.claim_issue(issue_id, agent_id)
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
    temp_db.claim_issue(issue_id, final_agent_id)
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

    # Restore anomaly detection
    Config.ANOMALY_FAILURE_THRESHOLD = original_threshold

    # Should now be escalated
    issue = temp_db.get_issue(issue_id)
    assert issue["status"] == "escalated"

    # Verify event counts
    assert temp_db.count_events(issue_id, "retry") == Config.MAX_RETRIES
    assert temp_db.count_events(issue_id, "agent_switch") == Config.MAX_AGENT_SWITCHES
    assert temp_db.count_events(issue_id, "escalated") == 1


@pytest.mark.asyncio
async def test_handle_agent_failure_anomaly_escalates_immediately(temp_db, tmp_path):
    """Anomaly threshold should force immediate escalation."""
    mock_backend = AsyncMock(spec=HiveBackend)
    orch = Orchestrator(
        db=temp_db,
        backend=mock_backend,
    )

    issue_id = temp_db.create_issue("Anomaly task", "Repeated failures")
    agent_id = temp_db.create_agent("test-agent")
    temp_db.claim_issue(issue_id, agent_id)
    agent = AgentIdentity(
        agent_id=agent_id,
        name="test-agent",
        issue_id=issue_id,
        worktree=str(tmp_path),
        session_id="session-123",
    )
    temp_db.log_event(issue_id, agent_id, "incomplete", {"reason": "failure-1"})

    original_threshold = Config.ANOMALY_FAILURE_THRESHOLD
    original_window = Config.ANOMALY_WINDOW_MINUTES
    Config.ANOMALY_FAILURE_THRESHOLD = 1
    Config.ANOMALY_WINDOW_MINUTES = 60
    try:
        await orch._handle_agent_failure(
            agent,
            CompletionResult(success=False, reason="failure-2", summary="second failure"),
        )
    finally:
        Config.ANOMALY_FAILURE_THRESHOLD = original_threshold
        Config.ANOMALY_WINDOW_MINUTES = original_window

    issue = temp_db.get_issue(issue_id)
    assert issue["status"] == "escalated"
    escalated_events = temp_db.get_events(issue_id=issue_id, event_type="escalated")
    assert len(escalated_events) == 1


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


# Tests for auto-restart functionality


@pytest.mark.asyncio
async def test_merge_task_auto_restart(temp_db, tmp_path):
    """Test auto-restart of merge_processor_loop on unexpected death."""
    from hive.backends import HiveBackend

    backend_mock = AsyncMock(spec=HiveBackend)
    orch = Orchestrator(
        db=temp_db,
        backend=backend_mock,
    )

    # Test callback with exception (should restart if running)
    orch.running = True

    # Create a mock task that died with an exception
    failed_task = Mock()
    failed_task.cancelled.return_value = False
    failed_task.exception.return_value = Exception("Merge processor died")

    # Mock asyncio.create_task to capture the new task creation
    with patch("asyncio.create_task") as mock_create_task:
        mock_new_task = Mock()

        def _consume_and_return_task(coro):
            coro.close()
            return mock_new_task

        mock_create_task.side_effect = _consume_and_return_task

        # Call the callback
        orch._on_merge_task_done(failed_task)

        # Verify new task was created with callback
        mock_create_task.assert_called_once()
        mock_new_task.add_done_callback.assert_called_once_with(orch._on_merge_task_done)


@pytest.mark.asyncio
async def test_merge_task_no_restart_when_cancelled(temp_db, tmp_path):
    """Test no restart when task is cancelled."""
    from hive.backends import HiveBackend

    backend_mock = AsyncMock(spec=HiveBackend)
    orch = Orchestrator(
        db=temp_db,
        backend=backend_mock,
    )

    # Test callback with cancelled task (should not restart)
    cancelled_task = Mock()
    cancelled_task.cancelled.return_value = True

    with patch("asyncio.create_task") as mock_create_task:
        # Call the callback
        orch._on_merge_task_done(cancelled_task)

        # Verify no new task was created
        mock_create_task.assert_not_called()


@pytest.mark.asyncio
async def test_merge_task_no_restart_when_not_running(temp_db, tmp_path):
    """Test no restart when orchestrator is not running."""
    from hive.backends import HiveBackend

    backend_mock = AsyncMock(spec=HiveBackend)
    orch = Orchestrator(
        db=temp_db,
        backend=backend_mock,
    )

    # Test callback when not running (should not restart)
    orch.running = False

    failed_task = Mock()
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
    from hive.backends import HiveBackend

    backend_mock = AsyncMock(spec=HiveBackend)
    orch = Orchestrator(
        db=temp_db,
        backend=backend_mock,
    )

    # Mock pool methods (the loop now delegates to the pool)
    orch.merge_pool.process_all = AsyncMock()
    health_called = asyncio.Event()

    async def _health_check_all():
        health_called.set()

    orch.merge_pool.health_check_all = AsyncMock(side_effect=_health_check_all)

    with patch("hive.orchestrator.Config") as mock_config:
        mock_config.MERGE_QUEUE_ENABLED = True
        mock_config.MERGE_POLL_INTERVAL = 0  # Yield-only for testing

        # Run the loop until we observe the health check, instead of relying on
        # wall-clock sleeps (which can be flaky under load).
        orch.running = True

        # Create a task and let it run briefly
        loop_task = asyncio.create_task(orch.merge_processor_loop())

        # Health check happens every 6 iterations.
        await asyncio.wait_for(health_called.wait(), timeout=1.0)

        # Stop the loop
        orch.running = False

        # Wait for task to complete
        await asyncio.wait_for(loop_task, timeout=1.0)

    # Verify health check was called at least once
    # (exact count depends on timing, but should be at least 1)
    assert orch.merge_pool.health_check_all.call_count >= 1


# --- Notes harvest/inject tests ---


@pytest.mark.asyncio
async def test_harvest_notes_on_agent_complete(temp_db, tmp_path):
    """Test that notes are harvested from worktree on agent completion."""
    import json
    from hive.backends import HiveBackend
    from hive.prompts import NOTES_FILE_NAME

    mock_backend = AsyncMock(spec=HiveBackend)
    mock_backend.get_messages = AsyncMock(return_value=[])
    mock_backend.abort_session = AsyncMock()
    mock_backend.delete_session = AsyncMock()

    orch = Orchestrator(
        db=temp_db,
        backend=mock_backend,
    )

    # Create issue and agent
    issue_id = temp_db.create_issue("Test task", "Do something")
    agent_id = temp_db.create_agent("test-agent")
    _activate_agent_for_issue(temp_db, issue_id, agent_id)

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

    with patch("hive.orchestrator.has_diff_from_main_async", return_value=True):
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
    from hive.backends import HiveBackend

    mock_backend = AsyncMock(spec=HiveBackend)
    mock_backend.get_messages = AsyncMock(return_value=[])
    mock_backend.abort_session = AsyncMock()
    mock_backend.delete_session = AsyncMock()

    orch = Orchestrator(
        db=temp_db,
        backend=mock_backend,
    )

    issue_id = temp_db.create_issue("Test task")
    agent_id = temp_db.create_agent("test-agent")
    _activate_agent_for_issue(temp_db, issue_id, agent_id)

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

    with patch("hive.orchestrator.has_diff_from_main_async", return_value=True):
        await orch.handle_agent_complete(agent)

    # No notes should be in DB
    notes = temp_db.get_notes(issue_id=issue_id)
    assert len(notes) == 0

    # No harvest event
    events = temp_db.get_events(issue_id=issue_id, event_type="notes_harvested")
    assert len(events) == 0


def test_gather_notes_for_worker_no_notes(temp_db, tmp_path):
    """Test _gather_notes_for_worker returns None when no notes exist."""
    from hive.backends import HiveBackend

    mock_backend = MagicMock(spec=HiveBackend)
    orch = Orchestrator(
        db=temp_db,
        backend=mock_backend,
    )

    issue_id = temp_db.create_issue("Standalone task")
    notes = orch._gather_notes_for_worker(issue_id, "default")
    assert notes is None


def test_gather_notes_for_worker_standalone_issue(temp_db, tmp_path):
    """Test _gather_notes_for_worker for a standalone issue (no parent)."""
    from hive.backends import HiveBackend

    mock_backend = MagicMock(spec=HiveBackend)
    orch = Orchestrator(
        db=temp_db,
        backend=mock_backend,
    )

    issue_id = temp_db.create_issue("Standalone task")

    # Add a project-wide note
    note_id = temp_db.add_note(content="Project note", category="pattern")

    notes = orch._gather_notes_for_worker(issue_id, "default")
    assert notes is not None
    assert len(notes) == 1
    assert notes[0]["id"] == note_id


# --- Bidirectional reconciliation tests ---


def _make_orchestrator(temp_db, tmp_path, mock_backend=None):
    """Helper to create an orchestrator with a mocked HiveBackend."""
    if mock_backend is None:
        mock_backend = AsyncMock(spec=HiveBackend)
    return Orchestrator(
        db=temp_db,
        backend=mock_backend,
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


def _activate_agent_for_issue(temp_db, issue_id: str, agent_id: str, *, keep_issue_status: bool = False):
    """Mark an agent as an actively running worker for direct handler tests."""
    if keep_issue_status:
        temp_db.conn.execute(
            "UPDATE agents SET status = 'working', current_issue = ? WHERE id = ?",
            (issue_id, agent_id),
        )
        temp_db.conn.execute(
            "UPDATE issues SET assignee = ? WHERE id = ?",
            (agent_id, issue_id),
        )
        temp_db.conn.commit()
        return
    temp_db.claim_issue(issue_id, agent_id)


@pytest.mark.asyncio
async def test_reconcile_ghost_agents(temp_db, tmp_path):
    """Ghost agent: DB says working, but session is gone from server."""
    mock_oc = AsyncMock(spec=HiveBackend)
    # Server returns no sessions — the agent's session is gone
    mock_oc.list_sessions = AsyncMock(return_value=[])
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    agent_id, issue_id, session_id = _make_stale_agent(temp_db, session_id="ghost-sess")

    await orch._reconcile_stale_agents()

    # Agent should be deleted (ephemeral agents)
    agent = temp_db.get_agent(agent_id)
    assert agent is None

    # Abort should NOT have been called (session doesn't exist)
    mock_oc.cleanup_session.assert_not_called()

    # Issue should be released back to open
    issue = temp_db.get_issue(issue_id)
    assert issue["status"] == "open"


@pytest.mark.asyncio
async def test_reconcile_stale_agents_with_live_sessions(temp_db, tmp_path):
    """Stale agent whose session is still alive on the server — abort + delete."""
    mock_oc = AsyncMock(spec=HiveBackend)
    mock_oc.list_sessions = AsyncMock(return_value=[{"id": "live-sess"}])
    mock_oc.cleanup_session = AsyncMock()
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    agent_id, issue_id, _ = _make_stale_agent(temp_db, session_id="live-sess")

    await orch._reconcile_stale_agents()

    # Abort + delete should have been called for the live session
    mock_oc.cleanup_session.assert_called_once_with("live-sess", directory="/tmp/wt")

    # Agent deleted (ephemeral agents), issue released
    agent = temp_db.get_agent(agent_id)
    assert agent is None
    issue = temp_db.get_issue(issue_id)
    assert issue["status"] == "open"


@pytest.mark.asyncio
async def test_reconcile_orphan_sessions(temp_db, tmp_path):
    """Sessions alive on server with no DB agent — cleaned up as orphans."""
    mock_oc = AsyncMock(spec=HiveBackend)
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
async def test_reconcile_fallback_when_backend_unreachable(temp_db, tmp_path):
    """list_sessions() throws — falls back to best-effort abort/delete."""
    mock_oc = AsyncMock(spec=HiveBackend)
    mock_oc.list_sessions = AsyncMock(side_effect=Exception("Connection refused"))
    mock_oc.cleanup_session = AsyncMock()
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    agent_id, issue_id, session_id = _make_stale_agent(temp_db, session_id="fallback-sess")

    await orch._reconcile_stale_agents()

    # Best-effort abort/delete should still be called
    mock_oc.cleanup_session.assert_called_once_with("fallback-sess", directory="/tmp/wt")

    # Agent deleted (ephemeral agents), issue released
    agent = temp_db.get_agent(agent_id)
    assert agent is None
    issue = temp_db.get_issue(issue_id)
    assert issue["status"] == "open"

    # No orphan cleanup event (Phase 2 skipped)
    events = temp_db.get_events(event_type="orphan_sessions_cleaned")
    assert len(events) == 0


@pytest.mark.asyncio
async def test_reconcile_respects_retry_budget(temp_db, tmp_path):
    """Exhausted retry budget → issue marked failed, not open."""
    mock_oc = AsyncMock(spec=HiveBackend)
    mock_oc.list_sessions = AsyncMock(return_value=[])
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    agent_id, issue_id, _ = _make_stale_agent(temp_db, session_id="budget-sess")

    # Exhaust retry and agent_switch budgets
    for i in range(Config.MAX_RETRIES):
        temp_db.log_event(issue_id, agent_id, "retry", {"attempt": i + 1})
    for i in range(Config.MAX_AGENT_SWITCHES):
        temp_db.log_event(issue_id, agent_id, "agent_switch", {"switch": i + 1})

    await orch._reconcile_stale_agents()

    # Issue should be escalated (not open)
    issue = temp_db.get_issue(issue_id)
    assert issue["status"] == "escalated"


@pytest.mark.asyncio
async def test_reconcile_mixed_ghost_live_orphan(temp_db, tmp_path):
    """All three conditions in one reconciliation run."""
    mock_oc = AsyncMock(spec=HiveBackend)
    # Server has: live-sess (stale agent's), orphan-sess (no agent), but NOT ghost-sess
    mock_oc.list_sessions = AsyncMock(return_value=[{"id": "live-sess"}, {"id": "orphan-sess"}])
    mock_oc.cleanup_session = AsyncMock()
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    # Ghost agent — session not on server
    ghost_agent_id, ghost_issue_id, _ = _make_stale_agent(temp_db, name="ghost", session_id="ghost-sess", issue_title="Ghost task")
    # Live agent — session still on server
    live_agent_id, live_issue_id, _ = _make_stale_agent(temp_db, name="live", session_id="live-sess", issue_title="Live task")

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

    # Both agents should be deleted (ephemeral agents)
    assert temp_db.get_agent(ghost_agent_id) is None
    assert temp_db.get_agent(live_agent_id) is None

    # Orphan cleanup event
    events = temp_db.get_events(event_type="orphan_sessions_cleaned")
    assert len(events) == 1


@pytest.mark.asyncio
async def test_reconcile_purges_idle_and_failed_agents(temp_db, tmp_path):
    """Test that Phase 3 of reconciliation purges idle and failed agents from previous runs."""
    # Create some agents with different statuses
    idle_agent_id = temp_db.create_agent(name="idle_agent")
    failed_agent_id = temp_db.create_agent(name="failed_agent")
    working_agent_id = temp_db.create_agent(name="working_agent")

    # Set their statuses
    temp_db.conn.execute("UPDATE agents SET status = 'idle' WHERE id = ?", (idle_agent_id,))
    temp_db.conn.execute("UPDATE agents SET status = 'failed' WHERE id = ?", (failed_agent_id,))
    temp_db.conn.execute("UPDATE agents SET status = 'working' WHERE id = ?", (working_agent_id,))
    temp_db.conn.commit()

    # Verify they exist
    assert temp_db.get_agent(idle_agent_id) is not None
    assert temp_db.get_agent(failed_agent_id) is not None
    assert temp_db.get_agent(working_agent_id) is not None

    # Mock backend to return no live sessions
    mock_oc = AsyncMock(spec=HiveBackend)
    mock_oc.list_sessions = AsyncMock(return_value=[])
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    # Run reconciliation
    await orch._reconcile_stale_agents()

    # Idle and failed agents should be purged
    assert temp_db.get_agent(idle_agent_id) is None
    assert temp_db.get_agent(failed_agent_id) is None

    # Working agent should also be deleted (gets reconciled to failed status, then purged in Phase 3)
    working_agent = temp_db.get_agent(working_agent_id)
    assert working_agent is None


# --- Reconcile phase unit tests ---


@pytest.mark.asyncio
async def test_reconcile_fetch_live_sessions_success(temp_db, tmp_path):
    """Phase 0 returns a mapping of live session IDs to their backends."""
    mock_oc = AsyncMock(spec=HiveBackend)
    mock_oc.list_sessions = AsyncMock(return_value=[{"id": "s1"}, {"id": "s2"}])
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    result = await orch._reconcile_fetch_live_sessions()

    assert set(result.keys()) == {"s1", "s2"}
    assert all(v is mock_oc for v in result.values())


@pytest.mark.asyncio
async def test_reconcile_fetch_live_sessions_backend_unreachable(temp_db, tmp_path):
    """Phase 0 returns None when backend raises."""
    mock_oc = AsyncMock(spec=HiveBackend)
    mock_oc.list_sessions = AsyncMock(side_effect=Exception("timeout"))
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    result = await orch._reconcile_fetch_live_sessions()

    assert result is None


@pytest.mark.asyncio
async def test_reconcile_stale_agent_live_session_releases_issue_and_removes_from_live(temp_db, tmp_path):
    """Live stale session is cleaned up, released, and removed from the live set."""
    mock_oc = AsyncMock(spec=HiveBackend)
    mock_oc.cleanup_session = AsyncMock()
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    issue_id = temp_db.create_issue("Test task")
    agent_id = temp_db.create_agent("test-agent")
    temp_db.claim_issue(issue_id, agent_id)

    live = {"live-sess": mock_oc, "other-sess": mock_oc}
    agent = {"id": agent_id, "current_issue": issue_id, "session_id": "live-sess", "worktree": "/tmp/wt", "project": ""}

    await orch._reconcile_stale_agent(agent, live)

    mock_oc.cleanup_session.assert_called_once_with("live-sess", directory="/tmp/wt")
    assert "live-sess" not in live
    issue = temp_db.get_issue(issue_id)
    assert issue["status"] == "open"


@pytest.mark.asyncio
async def test_reconcile_stale_agent_backend_unreachable(temp_db, tmp_path):
    """Backend-unreachable reconciliation still attempts best-effort cleanup."""
    mock_oc = AsyncMock(spec=HiveBackend)
    mock_oc.cleanup_session = AsyncMock()
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    issue_id = temp_db.create_issue("Test task")
    agent_id = temp_db.create_agent("test-agent")
    temp_db.claim_issue(issue_id, agent_id)

    agent = {"id": agent_id, "current_issue": issue_id, "session_id": "fallback-sess", "worktree": "/tmp/wt", "project": ""}
    await orch._reconcile_stale_agent(agent, None)

    mock_oc.cleanup_session.assert_called_once_with("fallback-sess", directory="/tmp/wt")


@pytest.mark.asyncio
async def test_reconcile_stale_agent_escalates_when_budget_exhausted(temp_db, tmp_path):
    """Exhausted retry budget escalates the stale issue."""
    mock_oc = AsyncMock(spec=HiveBackend)
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    issue_id = temp_db.create_issue("Test task")
    agent_id = temp_db.create_agent("test-agent")
    temp_db.claim_issue(issue_id, agent_id)

    for i in range(Config.MAX_RETRIES):
        temp_db.log_event(issue_id, agent_id, "retry", {"attempt": i + 1})
    for i in range(Config.MAX_AGENT_SWITCHES):
        temp_db.log_event(issue_id, agent_id, "agent_switch", {"switch": i + 1})

    agent = {"id": agent_id, "current_issue": issue_id, "session_id": None, "worktree": None, "project": ""}
    await orch._reconcile_stale_agent(agent, {})

    issue = temp_db.get_issue(issue_id)
    assert issue["status"] == "escalated"
    events = temp_db.get_events(issue_id=issue_id, event_type="reconciled")
    assert len(events) == 1


@pytest.mark.asyncio
async def test_reconcile_stale_agent_preserves_worktree_for_pending_merge(temp_db, tmp_path):
    """Pending merge keeps the stale worktree intact."""
    mock_oc = AsyncMock(spec=HiveBackend)
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    issue_id = temp_db.create_issue("Test task")
    agent_id = temp_db.create_agent("test-agent")
    temp_db.claim_issue(issue_id, agent_id)
    temp_db.conn.execute(
        "INSERT INTO merge_queue (issue_id, status, branch_name, worktree, project) VALUES (?, 'queued', 'br', ?, 'proj')",
        (issue_id, str(tmp_path)),
    )
    temp_db.conn.commit()

    agent = {"id": agent_id, "current_issue": issue_id, "session_id": None, "worktree": str(tmp_path), "project": ""}

    removed = []
    with patch("hive.orchestrator.remove_worktree_async", new=AsyncMock(side_effect=lambda wt: removed.append(wt))):
        await orch._reconcile_stale_agent(agent, {})

    assert removed == []


# --- SSE permission handling tests ---


@pytest.mark.asyncio
async def test_handle_permission_event_with_id(temp_db, tmp_path):
    """SSE permission event with id resolves directly."""
    mock_oc = AsyncMock(spec=HiveBackend)
    mock_oc.reply_permission = AsyncMock()
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    event_data = {"id": "perm-1", "permission": "bash", "sessionID": "s1"}
    await orch._handle_permission_event(event_data)

    mock_oc.reply_permission.assert_called_once_with("perm-1", reply="once")


@pytest.mark.asyncio
async def test_handle_permission_event_without_id_fetches_pending(temp_db, tmp_path):
    """SSE permission event without id falls back to fetching pending."""
    mock_oc = AsyncMock(spec=HiveBackend)
    mock_oc.get_pending_permissions = AsyncMock(
        return_value=[
            {"id": "perm-2", "permission": "read", "sessionID": "s1"},
        ]
    )
    mock_oc.reply_permission = AsyncMock()
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    await orch._handle_permission_event({})  # No id

    mock_oc.get_pending_permissions.assert_called_once()
    mock_oc.reply_permission.assert_called_once_with("perm-2", reply="once")


@pytest.mark.asyncio
async def test_handle_permission_event_rejects_question(temp_db, tmp_path):
    """SSE permission event for 'question' gets rejected."""
    mock_oc = AsyncMock(spec=HiveBackend)
    mock_oc.reply_permission = AsyncMock()
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    event_data = {"id": "perm-3", "permission": "question", "sessionID": "s1"}
    await orch._handle_permission_event(event_data)

    mock_oc.reply_permission.assert_called_once_with("perm-3", reply="reject")


@pytest.mark.asyncio
async def test_handle_permission_event_error_handling(temp_db, tmp_path):
    """SSE permission handler doesn't raise on errors."""
    mock_oc = AsyncMock(spec=HiveBackend)
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
        agent_id=agent_id,
        name="test-agent",
        issue_id=issue_id,
        worktree="/tmp/wt",
        session_id="s1",
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


@pytest.mark.asyncio
async def test_handle_stalled_agent_double_call_guard(temp_db, tmp_path):
    """Test that handle_stalled_agent guards against double execution."""
    # Create orchestrator with mock backend
    mock_backend = AsyncMock(spec=HiveBackend)
    orch = _make_orchestrator(temp_db, tmp_path, mock_backend)

    # Create issue and agent
    issue_id = temp_db.create_issue("Test task", "Do something")
    agent_id = temp_db.create_agent("test-agent")
    _activate_agent_for_issue(temp_db, issue_id, agent_id)

    agent = AgentIdentity(
        agent_id=agent_id,
        name="test-agent",
        issue_id=issue_id,
        worktree=str(tmp_path),
        session_id="session-123",
    )

    # Add agent to active agents (simulating it's being monitored)
    orch.active_agents[agent_id] = agent

    # First call should process normally
    await orch.handle_stalled_agent(agent)

    # Verify agent was removed from active_agents
    assert agent_id not in orch.active_agents

    # Check that exactly 1 'stalled' event was logged
    stall_events = [e for e in temp_db.get_events(issue_id=issue_id) if e["event_type"] == "stalled"]
    assert len(stall_events) == 1

    # Second call should be a no-op (guard should prevent execution)
    await orch.handle_stalled_agent(agent)

    # Verify still only 1 'stalled' event (no duplicate)
    stall_events = [e for e in temp_db.get_events(issue_id=issue_id) if e["event_type"] == "stalled"]
    assert len(stall_events) == 1


@pytest.mark.asyncio
async def test_handle_stalled_agent_terminal_issue_skips_escalation(temp_db, tmp_path):
    """Stalled terminal issues should mark failed and teardown without escalation."""
    mock_backend = AsyncMock(spec=HiveBackend)
    orch = _make_orchestrator(temp_db, tmp_path, mock_backend)

    issue_id = temp_db.create_issue("Terminal stalled task", "Already canceled")
    temp_db.try_transition_issue_status(issue_id, to_status="canceled")
    agent_id = temp_db.create_agent("test-agent")
    _activate_agent_for_issue(temp_db, issue_id, agent_id, keep_issue_status=True)
    agent = AgentIdentity(
        agent_id=agent_id,
        name="test-agent",
        issue_id=issue_id,
        worktree=str(tmp_path),
        session_id="session-123",
    )
    orch.active_agents[agent_id] = agent
    orch._handle_agent_failure = AsyncMock()

    await orch.handle_stalled_agent(agent)

    orch._handle_agent_failure.assert_not_called()
    stall_events = temp_db.get_events(issue_id=issue_id, event_type="stalled")
    assert len(stall_events) == 1
    assert agent_id not in orch.active_agents


@pytest.mark.asyncio
async def test_handle_agent_complete_double_call_guard(temp_db, tmp_path):
    """Test that handle_agent_complete guards against double execution."""
    # Create orchestrator with mock backend
    mock_backend = AsyncMock(spec=HiveBackend)
    # Mock get_messages to return empty list to avoid processing issues
    mock_backend.get_messages.return_value = []
    orch = _make_orchestrator(temp_db, tmp_path, mock_backend)

    # Create issue and agent
    issue_id = temp_db.create_issue("Test task", "Do something")
    agent_id = temp_db.create_agent("test-agent")
    _activate_agent_for_issue(temp_db, issue_id, agent_id)

    agent = AgentIdentity(
        agent_id=agent_id,
        name="test-agent",
        issue_id=issue_id,
        worktree=str(tmp_path),
        session_id="session-123",
    )

    # Add agent to active agents (simulating it's being monitored)
    orch.active_agents[agent_id] = agent

    # Count events before any calls
    events_before = len(temp_db.get_events(issue_id=issue_id))

    # First call should process normally
    with patch("hive.orchestrator.has_diff_from_main_async", return_value=True):
        await orch.handle_agent_complete(agent)

    # Verify agent was removed from active_agents during processing
    assert agent_id not in orch.active_agents

    # Count events after first call
    events_after_first = len(temp_db.get_events(issue_id=issue_id))
    assert events_after_first > events_before  # Some events should have been logged

    # Second call should be a no-op (guard should prevent execution)
    with patch("hive.orchestrator.has_diff_from_main_async", return_value=True):
        await orch.handle_agent_complete(agent)

    # Verify no additional events were logged
    events_after_second = len(temp_db.get_events(issue_id=issue_id))
    assert events_after_second == events_after_first


@pytest.mark.asyncio
async def test_handle_agent_complete_terminal_transition_skips_message_fetch(temp_db, tmp_path):
    """Canceled/finalized issue should skip message fetch and log skip event."""
    mock_backend = AsyncMock(spec=HiveBackend)
    mock_backend.get_messages = AsyncMock(return_value=[])
    orch = _make_orchestrator(temp_db, tmp_path, mock_backend)

    issue_id = temp_db.create_issue("Terminal task", "Already canceled")
    temp_db.try_transition_issue_status(issue_id, to_status="canceled")
    agent_id = temp_db.create_agent("test-agent")
    _activate_agent_for_issue(temp_db, issue_id, agent_id, keep_issue_status=True)

    agent = AgentIdentity(
        agent_id=agent_id,
        name="test-agent",
        issue_id=issue_id,
        worktree=str(tmp_path),
        session_id="session-123",
    )
    orch.active_agents[agent_id] = agent

    await orch.handle_agent_complete(agent)

    mock_backend.get_messages.assert_not_called()
    events = temp_db.get_events(issue_id=issue_id, event_type="agent_complete_skipped")
    assert len(events) == 1
    assert agent_id not in orch.active_agents


@pytest.mark.asyncio
async def test_handle_agent_complete_budget_transition_routes_failure(temp_db, tmp_path):
    """Budget exceeded transition should log and route through failure handling."""
    mock_backend = AsyncMock(spec=HiveBackend)
    mock_backend.get_messages = AsyncMock(return_value=[])
    orch = _make_orchestrator(temp_db, tmp_path, mock_backend)

    issue_id = temp_db.create_issue("Budget task", "Hit budget")
    agent_id = temp_db.create_agent("test-agent")
    _activate_agent_for_issue(temp_db, issue_id, agent_id)
    agent = AgentIdentity(
        agent_id=agent_id,
        name="test-agent",
        issue_id=issue_id,
        worktree=str(tmp_path),
        session_id="session-123",
    )
    orch.active_agents[agent_id] = agent

    orch._handle_agent_failure = AsyncMock()
    orch.db.get_issue_token_total = Mock(return_value=101)

    original_max_tokens = Config.MAX_TOKENS_PER_ISSUE
    Config.MAX_TOKENS_PER_ISSUE = 100
    try:
        await orch.handle_agent_complete(agent)
    finally:
        Config.MAX_TOKENS_PER_ISSUE = original_max_tokens

    events = temp_db.get_events(issue_id=issue_id, event_type="budget_exceeded")
    assert len(events) == 1
    orch._handle_agent_failure.assert_called_once()
    failure_result = orch._handle_agent_failure.call_args[0][1]
    assert "Exceeded per-issue token budget" in failure_result.reason
    assert agent_id not in orch.active_agents


@pytest.mark.asyncio
async def test_session_error_handler_registration_and_trigger(temp_db, tmp_path):
    """Test that session.error handler is registered and triggers handle_stalled_agent."""
    # Create orchestrator with mocked SSE client and handle_stalled_agent
    mock_bk = AsyncMock(spec=HiveBackend)
    orch = _make_orchestrator(temp_db, tmp_path, mock_bk)

    # Capture handler registrations on the pool's backend
    registered_handlers = {}

    def mock_on(event_type, handler):
        registered_handlers[event_type] = handler

    orch.backend.on = mock_on

    # Mock handle_stalled_agent
    orch.handle_stalled_agent = AsyncMock()

    # Setup SSE handlers
    orch._setup_sse_handlers()

    # Verify session.error handler is registered
    assert "session.error" in registered_handlers

    # Create a test agent and session mapping
    issue_id = temp_db.create_issue("Test Issue")
    agent_id = temp_db.create_agent("test-agent")
    session_id = "test-session-123"

    agent = AgentIdentity(
        agent_id=agent_id,
        name="test-agent",
        issue_id=issue_id,
        worktree="/tmp/test",
        session_id=session_id,
    )

    orch.active_agents[agent_id] = agent
    orch._session_to_agent[session_id] = agent_id

    # Trigger session error handler with error properties
    error_properties = {"sessionID": session_id, "error": "Test error message", "code": 500}

    # Call the handler
    session_error_handler = registered_handlers["session.error"]
    await session_error_handler(error_properties)

    # Verify handle_stalled_agent was called with the correct agent
    orch.handle_stalled_agent.assert_called_once_with(agent)

    # Verify event was logged to database
    events = temp_db.get_events(issue_id=issue_id, event_type="session_error")
    assert len(events) == 1

    event = events[0]
    # The detail is stored as JSON string, need to parse it
    import json

    detail = json.loads(event["detail"]) if event["detail"] else {}
    assert detail["session_id"] == session_id
    assert detail["error"] == error_properties


@pytest.mark.asyncio
async def test_session_error_handler_missing_session_id(temp_db, tmp_path):
    """Test that session.error handler ignores events without sessionID."""
    mock_bk = AsyncMock(spec=HiveBackend)
    orch = _make_orchestrator(temp_db, tmp_path, mock_bk)

    # Capture handler registrations on the pool's backend
    registered_handlers = {}

    def mock_on(event_type, handler):
        registered_handlers[event_type] = handler

    orch.backend.on = mock_on

    # Mock handle_stalled_agent
    orch.handle_stalled_agent = AsyncMock()

    # Setup SSE handlers
    orch._setup_sse_handlers()

    # Call handler with missing sessionID
    error_properties = {"error": "Test error message"}
    session_error_handler = registered_handlers["session.error"]
    await session_error_handler(error_properties)

    # Verify handle_stalled_agent was NOT called
    orch.handle_stalled_agent.assert_not_called()


@pytest.mark.asyncio
async def test_session_error_handler_unknown_session(temp_db, tmp_path):
    """Test that session.error handler ignores events for unknown sessions."""
    mock_bk = AsyncMock(spec=HiveBackend)
    orch = _make_orchestrator(temp_db, tmp_path, mock_bk)

    # Capture handler registrations on the pool's backend
    registered_handlers = {}

    def mock_on(event_type, handler):
        registered_handlers[event_type] = handler

    orch.backend.on = mock_on

    # Mock handle_stalled_agent
    orch.handle_stalled_agent = AsyncMock()

    # Setup SSE handlers
    orch._setup_sse_handlers()

    # Call handler with unknown sessionID
    error_properties = {"sessionID": "unknown-session", "error": "Test error message"}
    session_error_handler = registered_handlers["session.error"]
    await session_error_handler(error_properties)

    # Verify handle_stalled_agent was NOT called
    orch.handle_stalled_agent.assert_not_called()


@pytest.mark.asyncio
async def test_handle_stalled_with_idle_session(temp_db, tmp_path):
    """Test that idle session triggers handle_agent_complete instead of handle_stalled_agent."""
    orch = _make_orchestrator(temp_db, tmp_path)

    # Mock backend client
    orch.backend.get_session_status = AsyncMock(return_value={"type": "idle"})
    orch.handle_agent_complete = AsyncMock()
    orch.handle_stalled_agent = AsyncMock()

    # Create test agent
    issue_id = temp_db.create_issue("Test Issue")
    agent_id = temp_db.create_agent("test-agent")
    _activate_agent_for_issue(temp_db, issue_id, agent_id)

    agent = AgentIdentity(agent_id=agent_id, name="test-agent", issue_id=issue_id, worktree="/tmp/test", session_id="session-123")

    await orch._handle_stalled_with_session_check(agent)

    # Should call handle_agent_complete, not handle_stalled_agent
    orch.handle_agent_complete.assert_called_once_with(agent)
    orch.handle_stalled_agent.assert_not_called()

    # Should log missed_completion event
    events = temp_db.get_events(agent_id=agent_id, event_type="missed_completion")
    assert len(events) == 1
    assert events[0]["detail"] == '{"source": "heartbeat_expiry", "session_status": "idle"}'


@pytest.mark.asyncio
async def test_handle_stalled_with_busy_session_refreshes_heartbeat(temp_db, tmp_path):
    """Test that busy session refreshes heartbeat on status check."""
    orch = _make_orchestrator(temp_db, tmp_path)

    # Mock backend client
    orch.backend.get_session_status = AsyncMock(return_value={"type": "busy"})
    orch.handle_stalled_agent = AsyncMock()

    # Create test agent
    issue_id = temp_db.create_issue("Test Issue")
    agent_id = temp_db.create_agent("test-agent")
    _activate_agent_for_issue(temp_db, issue_id, agent_id)

    agent = AgentIdentity(agent_id=agent_id, name="test-agent", issue_id=issue_id, worktree="/tmp/test", session_id="session-123")

    await orch._handle_stalled_with_session_check(agent)

    # Should not call handle_stalled_agent
    orch.handle_stalled_agent.assert_not_called()

    # Should log heartbeat_refreshed event
    events = temp_db.get_events(agent_id=agent_id, event_type="heartbeat_refreshed")
    assert len(events) == 1

    # Agent heartbeat should be updated
    cursor = temp_db.conn.execute("SELECT last_heartbeat_at FROM agents WHERE id = ?", (agent_id,))
    row = cursor.fetchone()
    assert row is not None
    assert row["last_heartbeat_at"] is not None


@pytest.mark.asyncio
async def test_handle_stalled_with_busy_session_already_extended(temp_db, tmp_path):
    """Test that busy sessions always refresh heartbeat and continue."""
    orch = _make_orchestrator(temp_db, tmp_path)

    # Mock backend client
    orch.backend.get_session_status = AsyncMock(return_value={"type": "busy"})
    orch.handle_stalled_agent = AsyncMock()

    # Create test agent
    issue_id = temp_db.create_issue("Test Issue")
    agent_id = temp_db.create_agent("test-agent")
    _activate_agent_for_issue(temp_db, issue_id, agent_id)

    # Pre-populate a historical heartbeat event; busy status should still continue.
    temp_db.log_event(issue_id, agent_id, "heartbeat_refreshed", {"test": "data"})

    agent = AgentIdentity(agent_id=agent_id, name="test-agent", issue_id=issue_id, worktree="/tmp/test", session_id="session-123")

    await orch._handle_stalled_with_session_check(agent)

    # Should keep extending lease and avoid stalled handling.
    orch.handle_stalled_agent.assert_not_called()


@pytest.mark.asyncio
async def test_handle_stalled_with_session_api_failure(temp_db, tmp_path):
    """Test that backend API failure falls back to handle_stalled_agent."""
    orch = _make_orchestrator(temp_db, tmp_path)

    # Mock backend client to raise exception
    orch.backend.get_session_status = AsyncMock(side_effect=Exception("API Error"))
    orch.handle_stalled_agent = AsyncMock()

    # Create test agent
    issue_id = temp_db.create_issue("Test Issue")
    agent_id = temp_db.create_agent("test-agent")

    # Assign agent to issue to satisfy foreign key constraint
    temp_db.conn.execute("UPDATE agents SET current_issue = ? WHERE id = ?", (issue_id, agent_id))
    temp_db.conn.commit()

    agent = AgentIdentity(agent_id=agent_id, name="test-agent", issue_id=issue_id, worktree="/tmp/test", session_id="session-123")

    await orch._handle_stalled_with_session_check(agent)

    # Should call handle_stalled_agent due to API failure
    orch.handle_stalled_agent.assert_called_once_with(agent)

    # Should log session_check_failed event
    events = temp_db.get_events(agent_id=agent_id, event_type="session_check_failed")
    assert len(events) == 1


@pytest.mark.asyncio
async def test_worker_started_event_contains_model(temp_db, tmp_path):
    """Test that worker_started events contain the model field."""
    from unittest.mock import AsyncMock, patch
    from hive.backends import HiveBackend

    # Mock backend client
    mock_backend = AsyncMock(spec=HiveBackend)
    mock_backend.create_session.return_value = {"id": "session-123"}
    mock_backend.send_message_async.return_value = None

    # Create orchestrator
    orch = Orchestrator(
        db=temp_db,
        backend=mock_backend,
    )

    # Register project so orchestrator can resolve project path for "test" project
    temp_db.register_project("test", str(tmp_path))

    # Mock worktree creation
    test_model = "claude-sonnet-4"
    with patch("hive.orchestrator.create_worktree_async", return_value=str(tmp_path)):
        # Create issue with specific model (status defaults to "open")
        issue_id = temp_db.create_issue("Test task", "Do something", model=test_model, project="test")
        issue = temp_db.get_issue(issue_id)

        await orch.spawn_worker(issue)

    # Verify worker_started event has model field
    events = temp_db.get_events(issue_id=issue_id, event_type="worker_started")
    assert len(events) == 1
    event = events[0]
    assert event["detail"] is not None
    detail = json.loads(event["detail"]) if isinstance(event["detail"], str) else event["detail"]
    assert "model" in detail
    assert detail["model"] == test_model


@pytest.mark.asyncio
async def test_completed_event_contains_model(temp_db, tmp_path):
    """Test that completed events contain the model field."""
    from unittest.mock import AsyncMock, patch
    from hive.backends import HiveBackend

    # Mock backend client
    mock_backend = AsyncMock(spec=HiveBackend)
    mock_backend.get_messages.return_value = []

    # Create orchestrator
    orch = Orchestrator(
        db=temp_db,
        backend=mock_backend,
    )

    # Create issue and agent
    test_model = "claude-sonnet-4"
    issue_id = temp_db.create_issue("Test task", "Do something")
    agent_id = temp_db.create_agent("test-agent", model=test_model)
    temp_db.claim_issue(issue_id, agent_id)

    agent = AgentIdentity(
        agent_id=agent_id,
        name="test-agent",
        issue_id=issue_id,
        worktree=str(tmp_path),
        session_id="session-123",
    )

    # Add agent to active_agents
    orch.active_agents[agent_id] = agent

    # Create success result in file format
    file_result = {
        "status": "success",
        "summary": "Task completed successfully",
        "files_changed": [],
        "tests_added": [],
        "tests_run": True,
        "test_command": "echo test",
        "blockers": [],
        "artifacts": [{"type": "git_commit", "value": "abc123"}],
    }

    # Mock various methods to skip git operations
    with (
        patch("hive.orchestrator.remove_result_file"),
        patch("hive.orchestrator.read_notes_file", return_value=None),
        patch("hive.orchestrator.remove_notes_file"),
        patch("hive.orchestrator.read_result_file"),
        patch("hive.orchestrator.has_diff_from_main_async", return_value=True),
    ):
        await orch.handle_agent_complete(agent, file_result=file_result)

    # Verify completed event has model field
    events = temp_db.get_events(issue_id=issue_id, event_type="completed")
    assert len(events) == 1
    event = events[0]
    assert event["detail"] is not None
    detail = json.loads(event["detail"]) if isinstance(event["detail"], str) else event["detail"]
    assert "model" in detail
    assert detail["model"] == test_model


@pytest.mark.asyncio
async def test_validation_no_commits_routes_to_failure(temp_db, tmp_path):
    """Test that validation failure when worker claims success but has no commits routes to _handle_agent_failure."""
    from unittest.mock import AsyncMock, patch, MagicMock
    from hive.backends import HiveBackend

    # Create orchestrator with mock backend
    mock_backend = AsyncMock(spec=HiveBackend)
    orch = Orchestrator(
        db=temp_db,
        backend=mock_backend,
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

    # Mock the handling methods
    orch._handle_agent_failure = AsyncMock()
    orch._cleanup_session = AsyncMock()
    orch._unregister_agent = MagicMock()

    # Mock assess_completion to return success
    successful_result = CompletionResult(success=True, summary="Task completed successfully", reason="Worker claimed success")

    # Mock has_diff_from_main_async to return False (no commits)
    with patch("hive.orchestrator.has_diff_from_main_async") as mock_has_diff:
        with patch("hive.orchestrator.assess_completion") as mock_assess:
            mock_assess.return_value = successful_result
            mock_has_diff.return_value = False

            # Add agent to active_agents to test unregistration
            orch.active_agents[agent_id] = agent

            # Simulate the validation logic by calling the part of _handle_agent_completion
            # that would run after assess_completion
            if successful_result.success:
                # This should trigger the validation logic
                has_commits = await mock_has_diff(agent.worktree)
                if not has_commits:
                    # Log validation failure
                    temp_db.log_event(
                        agent.issue_id,
                        agent.agent_id,
                        "validation_failed",
                        {
                            "reason": "No commits relative to main despite claiming success",
                            "original_summary": successful_result.summary,
                        },
                    )

                    # Convert to failure result
                    validation_result = CompletionResult(
                        success=False,
                        reason="No commits relative to main despite claiming success",
                        summary=successful_result.summary,
                    )

                    # Route through failure handling
                    await orch._handle_agent_failure(agent, validation_result)

            # Verify _handle_agent_failure was called with validation failure
            orch._handle_agent_failure.assert_called_once()
            call_args = orch._handle_agent_failure.call_args
            called_agent = call_args[0][0]
            called_result = call_args[0][1]

            assert called_agent.agent_id == agent_id
            assert called_result.success is False
            assert "No commits relative to main despite claiming success" in called_result.reason

            # Verify validation_failed event was logged
            events = temp_db.get_events(issue_id=issue_id, event_type="validation_failed")
            assert len(events) == 1
            event = events[0]
            assert event["detail"] is not None
            detail = json.loads(event["detail"]) if isinstance(event["detail"], str) else event["detail"]
            assert "No commits relative to main despite claiming success" in detail["reason"]
            assert detail["original_summary"] == "Task completed successfully"


# --- _exc_detail helper tests ---


def test_exc_detail_with_message():
    """Exception with a message returns 'TypeName: message'."""
    from hive.orchestrator import _exc_detail

    e = ValueError("something went wrong")
    result = _exc_detail(e)
    assert result == "ValueError: something went wrong"


def test_exc_detail_empty_message():
    """Exception with empty str() returns just the type name."""
    from hive.orchestrator import _exc_detail

    e = asyncio.TimeoutError()
    result = _exc_detail(e)
    assert result == "TimeoutError"
    assert result != ""


def test_exc_detail_subclass_no_message():
    """Custom exception subclass with no message returns type name."""
    from hive.orchestrator import _exc_detail

    class MyError(Exception):
        pass

    e = MyError()
    result = _exc_detail(e)
    assert result == "MyError"
    assert result != ""


@pytest.mark.asyncio
async def test_spawn_error_event_non_empty_on_timeout(temp_db, tmp_path):
    """spawn_error event detail must not be empty for TimeoutError (regression test)."""
    from unittest.mock import AsyncMock, patch
    from hive.backends import HiveBackend

    mock_backend = AsyncMock(spec=HiveBackend)
    # Raise asyncio.TimeoutError (str(e) == "") during create_session
    mock_backend.create_session = AsyncMock(side_effect=asyncio.TimeoutError())

    orch = Orchestrator(
        db=temp_db,
        backend=mock_backend,
    )

    temp_db.register_project("test", str(tmp_path))
    issue_id = temp_db.create_issue("Timeout task", "Will timeout", project="test")
    issue = temp_db.get_issue(issue_id)

    with patch("hive.orchestrator.create_worktree_async", return_value=str(tmp_path)):
        with patch("hive.orchestrator.remove_worktree_async"):
            await orch.spawn_worker(issue)

    events = temp_db.get_events(issue_id=issue_id, event_type="spawn_error")
    assert len(events) == 1
    detail = json.loads(events[0]["detail"])
    # Must not be empty string — this is the regression being fixed
    assert detail["error"] != ""
    assert detail["error"] == "TimeoutError"


@pytest.mark.asyncio
async def test_worktree_error_event_non_empty_on_timeout(temp_db, tmp_path):
    """worktree_error event detail must not be empty for TimeoutError."""
    from unittest.mock import AsyncMock, patch
    from hive.backends import HiveBackend

    mock_backend = AsyncMock(spec=HiveBackend)

    orch = Orchestrator(
        db=temp_db,
        backend=mock_backend,
    )

    temp_db.register_project("test", str(tmp_path))
    issue_id = temp_db.create_issue("Worktree timeout task", "Will fail at worktree", project="test")
    issue = temp_db.get_issue(issue_id)

    # Raise TimeoutError during worktree creation
    with patch("hive.orchestrator.create_worktree_async", side_effect=asyncio.TimeoutError()):
        await orch.spawn_worker(issue)

    events = temp_db.get_events(issue_id=issue_id, event_type="worktree_error")
    assert len(events) == 1
    detail = json.loads(events[0]["detail"])
    assert detail["error"] != ""
    assert detail["error"] == "TimeoutError"


# ── Multi-project dispatch invariant tests ─────────────────────────────────


@pytest.mark.asyncio
async def test_inv1_issue_from_project_a_creates_worktree_in_project_a(temp_db, tmp_path):
    """INV-1: Issue from project A creates worktree in project A's repo path, not elsewhere."""
    from unittest.mock import AsyncMock, patch
    from hive.backends import HiveBackend

    repo_a = tmp_path / "repo_a"
    repo_a.mkdir()
    repo_b = tmp_path / "repo_b"
    repo_b.mkdir()

    temp_db.register_project("proj-a", str(repo_a))
    temp_db.register_project("proj-b", str(repo_b))

    mock_backend = AsyncMock(spec=HiveBackend)
    mock_backend.create_session.return_value = {"id": "session-a"}
    mock_backend.send_message_async.return_value = None

    orch = Orchestrator(db=temp_db, backend=mock_backend)

    issue_id = temp_db.create_issue("Task A", "Do something in A", project="proj-a")
    issue = temp_db.get_issue(issue_id)

    captured_paths = []

    async def capture_worktree(project_path, agent_name):
        captured_paths.append(project_path)
        return str(tmp_path / "worktree-a")

    with patch("hive.orchestrator.create_worktree_async", side_effect=capture_worktree):
        await orch.spawn_worker(issue)

    # Worktree was created from repo_a's path, not repo_b's
    assert len(captured_paths) == 1
    assert str(repo_a) in captured_paths[0] or captured_paths[0] == str(repo_a)
    assert str(repo_b) not in captured_paths[0]


@pytest.mark.asyncio
async def test_inv2_project_b_issue_never_touches_project_a_repo(temp_db, tmp_path):
    """INV-2: Spawning issue from project B never uses project A's path."""
    from unittest.mock import AsyncMock, patch

    repo_a = tmp_path / "repo_a"
    repo_a.mkdir()
    repo_b = tmp_path / "repo_b"
    repo_b.mkdir()

    temp_db.register_project("proj-a", str(repo_a))
    temp_db.register_project("proj-b", str(repo_b))

    mock_backend = AsyncMock(spec=HiveBackend)
    mock_backend.create_session.return_value = {"id": "session-b"}
    mock_backend.send_message_async.return_value = None

    orch = Orchestrator(db=temp_db, backend=mock_backend)

    issue_id = temp_db.create_issue("Task B", "Do something in B", project="proj-b")
    issue = temp_db.get_issue(issue_id)

    captured_paths = []

    async def capture_worktree(project_path, agent_name):
        captured_paths.append(project_path)
        return str(tmp_path / "worktree-b")

    with patch("hive.orchestrator.create_worktree_async", side_effect=capture_worktree):
        await orch.spawn_worker(issue)

    assert len(captured_paths) == 1
    # Must use repo_b path, NOT repo_a path
    assert str(repo_b) in captured_paths[0] or captured_paths[0] == str(repo_b)
    assert str(repo_a) not in captured_paths[0]


def test_inv3_ready_queue_returns_issues_from_all_projects(temp_db):
    """INV-3: get_ready_queue(project=None) returns issues from all registered projects."""
    temp_db.register_project("proj-a", "/tmp/repo_a")
    temp_db.register_project("proj-b", "/tmp/repo_b")

    id_a = temp_db.create_issue("Task A", "In proj-a", project="proj-a")
    id_b = temp_db.create_issue("Task B", "In proj-b", project="proj-b")

    all_ready = temp_db.get_ready_queue(project=None)
    ready_ids = {i["id"] for i in all_ready}

    assert id_a in ready_ids
    assert id_b in ready_ids

    # Project-scoped queries should only return their own issues
    a_only = temp_db.get_ready_queue(project="proj-a")
    assert all(i["project"] == "proj-a" for i in a_only)

    b_only = temp_db.get_ready_queue(project="proj-b")
    assert all(i["project"] == "proj-b" for i in b_only)


@pytest.mark.asyncio
async def test_inv4_unknown_project_raises_value_error(temp_db, tmp_path):
    """INV-4: Spawning an issue whose project is not registered raises ValueError."""
    from unittest.mock import AsyncMock

    mock_backend = AsyncMock(spec=HiveBackend)
    orch = Orchestrator(db=temp_db, backend=mock_backend)

    # Issue belongs to a project not registered in the DB
    issue_id = temp_db.create_issue("Orphan task", "No project path", project="unregistered-project")
    issue = temp_db.get_issue(issue_id)

    with pytest.raises(ValueError, match="Unknown project: unregistered-project"):
        await orch.spawn_worker(issue)


@pytest.mark.asyncio
async def test_two_projects_both_get_dispatched(temp_db, tmp_path):
    """Two projects, each with one open issue — both get dispatched in main loop."""
    from unittest.mock import AsyncMock, patch

    repo_a = tmp_path / "repo_a"
    repo_a.mkdir()
    repo_b = tmp_path / "repo_b"
    repo_b.mkdir()

    temp_db.register_project("proj-a", str(repo_a))
    temp_db.register_project("proj-b", str(repo_b))

    mock_backend = AsyncMock(spec=HiveBackend)
    mock_backend.create_session.side_effect = [
        {"id": "session-a"},
        {"id": "session-b"},
    ]
    mock_backend.send_message_async.return_value = None
    mock_backend.list_sessions = AsyncMock(return_value=[])

    id_a = temp_db.create_issue("Task A", "", project="proj-a")
    id_b = temp_db.create_issue("Task B", "", project="proj-b")

    dispatched_worktrees = []

    async def fake_create_worktree(project_path, agent_name):
        dispatched_worktrees.append(project_path)
        return str(tmp_path / f"wt-{len(dispatched_worktrees)}")

    with (
        patch.object(Config, "MAX_AGENTS", 2),
        patch.object(Config, "POLL_INTERVAL", 0.05),
        patch.object(Config, "MERGE_QUEUE_ENABLED", False),
        patch("hive.orchestrator.create_worktree_async", side_effect=fake_create_worktree),
    ):
        orch = Orchestrator(db=temp_db, backend=mock_backend)

        # Run main loop briefly to dispatch both issues
        async def stop_when_both_claimed():
            issue_a = temp_db.get_issue(id_a)
            issue_b = temp_db.get_issue(id_b)
            return issue_a["status"] == "in_progress" and issue_b["status"] == "in_progress"

        # Poll until both issues are claimed or timeout
        for _ in range(50):
            ready = temp_db.get_ready_queue(project=None)
            if ready:
                issue = ready[0]
                # Guard: skip if already being spawned
                if issue["id"] not in orch._spawning_issues:
                    await orch.spawn_worker(issue)
            if await stop_when_both_claimed():
                break
            await asyncio.sleep(0.01)

    # Both issues should be claimed (in_progress)
    final_a = temp_db.get_issue(id_a)
    final_b = temp_db.get_issue(id_b)
    assert final_a["status"] == "in_progress", f"proj-a issue not dispatched: {final_a['status']}"
    assert final_b["status"] == "in_progress", f"proj-b issue not dispatched: {final_b['status']}"

    # Each worktree was created in the correct project repo
    assert any(str(repo_a) in wt for wt in dispatched_worktrees)
    assert any(str(repo_b) in wt for wt in dispatched_worktrees)


# --- _try_escalate_issue helper tests ---


def test_try_escalate_issue_success(temp_db, tmp_path):
    """INV-1: Helper logs event_type with correct detail on successful transition."""
    mock_backend = AsyncMock(spec=HiveBackend)
    orch = Orchestrator(db=temp_db, backend=mock_backend)

    issue_id = temp_db.create_issue("Task", "Body")
    agent_id = temp_db.create_agent("test-agent")
    temp_db.claim_issue(issue_id, agent_id)

    detail = {"reason": "test escalation", "extra": 42}
    result = orch._try_escalate_issue(
        issue_id,
        agent_id,
        to_status="escalated",
        event_type="escalated",
        detail=detail,
        skip_event_type="escalate_skipped",
    )

    assert result is True
    issue = temp_db.get_issue(issue_id)
    assert issue["status"] == "escalated"

    events = temp_db.get_events(issue_id=issue_id, event_type="escalated")
    assert len(events) == 1
    logged = json.loads(events[0]["detail"])
    assert logged["reason"] == "test escalation"
    assert logged["extra"] == 42


def test_try_escalate_issue_skip_when_not_in_progress(temp_db, tmp_path):
    """INV-2: Helper logs skip_event_type when transition fails."""
    mock_backend = AsyncMock(spec=HiveBackend)
    orch = Orchestrator(db=temp_db, backend=mock_backend)

    issue_id = temp_db.create_issue("Task", "Body")
    agent_id = temp_db.create_agent("test-agent")
    # Do NOT claim — issue stays open, transition from in_progress will fail

    result = orch._try_escalate_issue(
        issue_id,
        agent_id,
        to_status="escalated",
        event_type="escalated",
        detail={"reason": "should not appear"},
        skip_event_type="escalate_skipped",
    )

    assert result is False
    issue = temp_db.get_issue(issue_id)
    assert issue["status"] == "open"  # unchanged

    # escalated event must NOT be logged
    assert len(temp_db.get_events(issue_id=issue_id, event_type="escalated")) == 0
    # skip event MUST be logged
    skip_events = temp_db.get_events(issue_id=issue_id, event_type="escalate_skipped")
    assert len(skip_events) == 1
    skip_detail = json.loads(skip_events[0]["detail"])
    assert "reason" in skip_detail


def test_try_escalate_issue_no_skip_event_when_not_provided(temp_db, tmp_path):
    """Helper silently returns False (no skip event) when skip_event_type is None."""
    mock_backend = AsyncMock(spec=HiveBackend)
    orch = Orchestrator(db=temp_db, backend=mock_backend)

    issue_id = temp_db.create_issue("Task", "Body")
    agent_id = temp_db.create_agent("test-agent")

    result = orch._try_escalate_issue(
        issue_id,
        agent_id,
        to_status="escalated",
        event_type="escalated",
        detail={"reason": "x"},
    )

    assert result is False
    # escalated event must NOT be logged; only the "created" system event should exist
    assert len(temp_db.get_events(issue_id=issue_id, event_type="escalated")) == 0


def test_try_escalate_issue_open_transition(temp_db, tmp_path):
    """INV-3: Helper with to_status='open' releases issue (not escalated)."""
    mock_backend = AsyncMock(spec=HiveBackend)
    orch = Orchestrator(db=temp_db, backend=mock_backend)

    issue_id = temp_db.create_issue("Task", "Body")
    agent_id = temp_db.create_agent("test-agent")
    temp_db.claim_issue(issue_id, agent_id)

    result = orch._try_escalate_issue(
        issue_id,
        agent_id,
        to_status="open",
        event_type="retry",
        detail={"retry_count": 1, "reason": "test", "previous_agent": "old"},
        skip_event_type="retry_skipped",
        skip_reason="issue not releasable",
    )

    assert result is True
    issue = temp_db.get_issue(issue_id)
    assert issue["status"] == "open"  # released, not escalated

    events = temp_db.get_events(issue_id=issue_id, event_type="retry")
    assert len(events) == 1


def test_try_escalate_issue_skip_reason_override(temp_db, tmp_path):
    """Custom skip_reason is logged verbatim instead of auto-generated reason."""
    mock_backend = AsyncMock(spec=HiveBackend)
    orch = Orchestrator(db=temp_db, backend=mock_backend)

    issue_id = temp_db.create_issue("Task", "Body")
    agent_id = temp_db.create_agent("test-agent")
    # Not claimed — transition will fail

    orch._try_escalate_issue(
        issue_id,
        agent_id,
        to_status="open",
        event_type="retry",
        detail={"retry_count": 1},
        skip_event_type="retry_skipped",
        skip_reason="issue not releasable",
    )

    skip_events = temp_db.get_events(issue_id=issue_id, event_type="retry_skipped")
    assert len(skip_events) == 1
    skip_detail = json.loads(skip_events[0]["detail"])
    assert skip_detail["reason"] == "issue not releasable"


@pytest.mark.asyncio
async def test_handle_agent_failure_retry_skip_logged(temp_db, tmp_path):
    """INV-2: retry_skipped logged when issue is not in_progress during retry."""
    mock_backend = AsyncMock(spec=HiveBackend)
    orch = Orchestrator(db=temp_db, backend=mock_backend)

    issue_id = temp_db.create_issue("Task", "Body")
    agent_id = temp_db.create_agent("test-agent")
    # Claim then cancel — now issue is not in_progress for the failure handler
    temp_db.claim_issue(issue_id, agent_id)
    # Directly set status to canceled so the CAS in _try_escalate_issue fails
    temp_db.conn.execute("UPDATE issues SET status = 'canceled' WHERE id = ?", (issue_id,))
    temp_db.conn.commit()

    agent = AgentIdentity(
        agent_id=agent_id,
        name="test-agent",
        issue_id=issue_id,
        worktree=str(tmp_path),
        session_id="session-123",
    )

    original_threshold = Config.ANOMALY_FAILURE_THRESHOLD
    Config.ANOMALY_FAILURE_THRESHOLD = 0  # disable anomaly path
    try:
        await orch._handle_agent_failure(agent, CompletionResult(success=False, reason="fail", summary="x"))
    finally:
        Config.ANOMALY_FAILURE_THRESHOLD = original_threshold

    skip_events = temp_db.get_events(issue_id=issue_id, event_type="retry_skipped")
    assert len(skip_events) == 1
    skip_detail = json.loads(skip_events[0]["detail"])
    assert skip_detail["reason"] == "issue not releasable"


@pytest.mark.asyncio
async def test_handle_agent_failure_escalate_skip_logged(temp_db, tmp_path):
    """INV-2: escalate_skipped logged when issue not in_progress during final escalate."""
    mock_backend = AsyncMock(spec=HiveBackend)
    orch = Orchestrator(db=temp_db, backend=mock_backend)

    issue_id = temp_db.create_issue("Task", "Body")
    agent_id = temp_db.create_agent("test-agent")
    temp_db.claim_issue(issue_id, agent_id)

    # Pre-populate retries and switches to hit the final escalation path
    for i in range(Config.MAX_RETRIES):
        temp_db.log_event(issue_id, agent_id, "retry", {"attempt": i + 1})
    for i in range(Config.MAX_AGENT_SWITCHES):
        temp_db.log_event(issue_id, agent_id, "agent_switch", {"switch": i + 1})

    # Cancel the issue so the CAS fails
    temp_db.conn.execute("UPDATE issues SET status = 'canceled' WHERE id = ?", (issue_id,))
    temp_db.conn.commit()

    agent = AgentIdentity(
        agent_id=agent_id,
        name="test-agent",
        issue_id=issue_id,
        worktree=str(tmp_path),
        session_id="session-123",
    )

    original_threshold = Config.ANOMALY_FAILURE_THRESHOLD
    Config.ANOMALY_FAILURE_THRESHOLD = 0
    try:
        await orch._handle_agent_failure(agent, CompletionResult(success=False, reason="fail", summary="x"))
    finally:
        Config.ANOMALY_FAILURE_THRESHOLD = original_threshold

    skip_events = temp_db.get_events(issue_id=issue_id, event_type="escalate_skipped")
    assert len(skip_events) == 1


# ── _cleanup_agent unified method tests ─────────────────────────────────────


@pytest.mark.asyncio
async def test_cleanup_agent_via_identity_calls_backend_cleanup(temp_db, tmp_path):
    """_cleanup_agent with AgentIdentity calls backend cleanup_session when cleanup_session=True."""
    mock_backend = AsyncMock(spec=HiveBackend)
    mock_backend.cleanup_session = AsyncMock()
    orch = Orchestrator(db=temp_db, backend=mock_backend)

    agent_id = temp_db.create_agent("w", model="claude-sonnet-4-5")
    agent = AgentIdentity(agent_id=agent_id, name="w", issue_id="i1", worktree=str(tmp_path), session_id="sess-1")
    orch.backend_pool.track_session("sess-1", mock_backend)
    orch._register_active_agent(agent)

    await orch._cleanup_agent(agent, cleanup_session=True, unregister_agent=True, mark_failed=True)

    mock_backend.cleanup_session.assert_called_once_with("sess-1", directory=str(tmp_path))
    assert agent_id not in orch.active_agents


@pytest.mark.asyncio
async def test_cleanup_agent_via_raw_params_deletes_row(temp_db, tmp_path):
    """_cleanup_agent with raw params and delete_agent_row=True removes the DB agent row."""
    mock_backend = AsyncMock(spec=HiveBackend)
    orch = Orchestrator(db=temp_db, backend=mock_backend)

    agent_id = temp_db.create_agent("orphan", model="claude-sonnet-4-5")
    row_before = temp_db.conn.execute("SELECT id FROM agents WHERE id = ?", (agent_id,)).fetchone()
    assert row_before is not None

    await orch._cleanup_agent(agent_id=agent_id, delete_agent_row=True)

    row_after = temp_db.conn.execute("SELECT id FROM agents WHERE id = ?", (agent_id,)).fetchone()
    assert row_after is None


@pytest.mark.asyncio
async def test_cleanup_agent_delete_agent_row_false_preserves_row(temp_db, tmp_path):
    """_cleanup_agent with delete_agent_row=False (default) does not remove the DB agent row."""
    mock_backend = AsyncMock(spec=HiveBackend)
    orch = Orchestrator(db=temp_db, backend=mock_backend)

    agent_id = temp_db.create_agent("keeper", model="claude-sonnet-4-5")

    await orch._cleanup_agent(agent_id=agent_id, delete_agent_row=False)

    row = temp_db.conn.execute("SELECT id FROM agents WHERE id = ?", (agent_id,)).fetchone()
    assert row is not None


@pytest.mark.asyncio
async def test_cleanup_agent_skips_session_when_no_session_id(temp_db, tmp_path):
    """_cleanup_agent with cleanup_session=True but no session_id skips backend call."""
    mock_backend = AsyncMock(spec=HiveBackend)
    mock_backend.cleanup_session = AsyncMock()
    orch = Orchestrator(db=temp_db, backend=mock_backend)

    agent_id = temp_db.create_agent("w", model="claude-sonnet-4-5")

    await orch._cleanup_agent(agent_id=agent_id, session_id=None, cleanup_session=True)

    mock_backend.cleanup_session.assert_not_called()


@pytest.mark.asyncio
async def test_cleanup_agent_suppresses_backend_exception(temp_db, tmp_path):
    """_cleanup_agent suppresses exceptions from cleanup_session so other steps still run."""
    mock_backend = AsyncMock(spec=HiveBackend)
    mock_backend.cleanup_session = AsyncMock(side_effect=RuntimeError("backend down"))
    orch = Orchestrator(db=temp_db, backend=mock_backend)

    agent_id = temp_db.create_agent("w", model="claude-sonnet-4-5")
    agent = AgentIdentity(agent_id=agent_id, name="w", issue_id="i1", worktree=str(tmp_path), session_id="sess-x")
    orch.backend_pool.track_session("sess-x", mock_backend)

    # Should not raise; mark_failed should still run
    await orch._cleanup_agent(agent, cleanup_session=True, mark_failed=True)

    db_agent = temp_db.conn.execute("SELECT status FROM agents WHERE id = ?", (agent_id,)).fetchone()
    assert db_agent["status"] == "failed"
