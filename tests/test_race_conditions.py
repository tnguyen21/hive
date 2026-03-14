"""Regression tests for race conditions fixed in the race-condition audit.

Each test in this file validates a specific bug that was found and fixed.
The test is designed to FAIL against the old code and PASS against the fix.
"""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from hive.utils import AgentIdentity
from hive.backends import HiveBackend
from hive.orchestrator import Orchestrator
from hive.config import Config


# --- Helpers ---


def _make_orchestrator(temp_db, tmp_path, mock_backend=None):
    """Helper to create an orchestrator with a mocked HiveBackend."""
    if mock_backend is None:
        mock_backend = AsyncMock(spec=HiveBackend)
    # Register a "test" project so spawn_worker can resolve project paths
    temp_db.register_project("test", str(tmp_path))
    return Orchestrator(
        db=temp_db,
        backend=mock_backend,
    )


def _make_agent(temp_db, orch, name="test-agent", issue_title="Test task", session_id="session-123", worktree="/tmp/wt"):
    """Create an agent identity and register it in the orchestrator."""
    issue_id = temp_db.create_issue(issue_title)
    agent_id = temp_db.create_agent(name)
    temp_db.claim_issue(issue_id, agent_id)
    temp_db.conn.execute(
        "UPDATE agents SET session_id = ?, worktree = ? WHERE id = ?",
        (session_id, worktree, agent_id),
    )
    temp_db.conn.commit()

    agent = AgentIdentity(
        agent_id=agent_id,
        name=name,
        issue_id=issue_id,
        worktree=worktree,
        session_id=session_id,
    )
    orch.active_agents[agent_id] = agent
    orch._session_to_agent[session_id] = agent_id
    orch._issue_to_agent[issue_id] = agent_id
    return agent


# =============================================================================
# BUG-1: monitor_agent finally block deletes new session's event after
#         agent.session_id is mutated during completion handling
# =============================================================================


@pytest.mark.asyncio
async def test_bug1_monitor_agent_preserves_new_session_event_after_cycling(temp_db, tmp_path):
    """Verify monitor_agent's finally block cleans up its OWN session event,
    not a new session's event created when agent.session_id is mutated.

    Before the fix, the finally block used agent.session_id which could get
    mutated during handle_agent_complete, causing it to delete the new session's event.

    We mock handle_agent_complete to simulate session_id mutation and creation
    of a new Event in session_status_events. This isolates the BUG-1 fix
    (snapshotting my_session_id) from side effects of the full flow.
    """
    mock_oc = AsyncMock(spec=HiveBackend)
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    old_session_id = "old-session-111"
    new_session_id = "new-session-222"

    agent_id = temp_db.create_agent("mol-worker")
    issue_id = temp_db.create_issue("Step 1")

    agent = AgentIdentity(
        agent_id=agent_id,
        name="mol-worker",
        issue_id=issue_id,
        worktree=str(tmp_path),
        session_id=old_session_id,
    )
    orch.active_agents[agent_id] = agent
    orch._session_to_agent[old_session_id] = agent_id
    orch._issue_to_agent[issue_id] = agent_id

    # Set up the old session's event (what monitor_agent waits on)
    old_event = asyncio.Event()
    orch.session_status_events[old_session_id] = old_event

    # Simulate: set the event (SSE said idle) → monitor_agent wakes up
    old_event.set()

    # Mock handle_agent_complete to simulate session_id mutation:
    # it mutates agent.session_id and creates a new event for the new session.
    async def mock_handle_agent_complete(agent, file_result=None):
        agent.session_id = new_session_id
        orch.session_status_events[new_session_id] = asyncio.Event()

    orch.handle_agent_complete = mock_handle_agent_complete

    # Run monitor_agent — it will:
    # 1. Snapshot my_session_id = "old-session-111"
    # 2. Wake up from old_event.set()
    # 3. Call mock handle_agent_complete which mutates agent.session_id
    # 4. finally block should clean up OLD session event, not new one
    await orch.monitor_agent(agent)

    # The critical assertion: the NEW session's event must still exist.
    # Before the fix, monitor_agent's finally block used agent.session_id
    # (which was mutated to new_session_id) and would delete it.
    assert new_session_id in orch.session_status_events, (
        "New session's asyncio.Event was deleted by monitor_agent's finally block! "
        "This means the new monitor_agent task can never be woken by SSE events."
    )

    # The old session's event should be cleaned up
    assert old_session_id not in orch.session_status_events, "Old session's event should have been cleaned up by monitor_agent's finally block"


@pytest.mark.asyncio
async def test_bug1_monitor_poll_uses_snapshotted_session_id(temp_db, tmp_path):
    """Verify polling fallback checks the monitor's original session_id.

    If agent.session_id mutates during a monitor timeout (cycle in flight),
    monitor must still poll old_session_id to avoid cross-session false idle.
    """
    mock_oc = AsyncMock(spec=HiveBackend)
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    old_session_id = "old-session-333"
    new_session_id = "new-session-444"
    agent = _make_agent(temp_db, orch, name="poll-worker", issue_title="Step 1", session_id=old_session_id, worktree=str(tmp_path))

    orch.session_status_events[old_session_id] = asyncio.Event()
    mock_oc.get_session_status = AsyncMock(return_value={"type": "idle"})
    orch.handle_agent_complete = AsyncMock()

    async def timeout_and_mutate(awaitable, timeout):
        agent.session_id = new_session_id
        awaitable.close()
        raise asyncio.TimeoutError

    with patch("hive.orchestrator.asyncio.wait_for", side_effect=timeout_and_mutate):
        await orch.monitor_agent(agent)

    mock_oc.get_session_status.assert_awaited_once_with(
        old_session_id,
        directory=str(tmp_path),
    )


@pytest.mark.asyncio
async def test_monitor_keeps_running_after_busy_heartbeat_refresh(temp_db, tmp_path):
    """Lease/heartbeat expiry with busy status should not drop the monitor."""
    mock_oc = AsyncMock(spec=HiveBackend)
    mock_oc.get_session_status = AsyncMock(return_value={"type": "busy"})
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    session_id = "busy-session-1"
    agent = _make_agent(temp_db, orch, session_id=session_id, worktree=str(tmp_path))
    orch.session_status_events[session_id] = asyncio.Event()

    file_result = {
        "status": "success",
        "summary": "done",
        "files_changed": [],
        "tests_added": [],
        "tests_run": True,
        "blockers": [],
        "artifacts": [],
    }

    async def timeout_and_stale(awaitable, timeout):
        orch._session_last_activity[session_id] = datetime.now() - timedelta(seconds=Config.LEASE_DURATION + 5)
        awaitable.close()
        raise asyncio.TimeoutError

    orch.handle_agent_complete = AsyncMock()
    with (
        patch("hive.orchestrator.asyncio.wait_for", side_effect=timeout_and_stale),
        patch("hive.orchestrator.read_result_file", side_effect=[None, None, file_result]),
    ):
        await orch.monitor_agent(agent)

    orch.handle_agent_complete.assert_called_once_with(agent, file_result=file_result)
    assert session_id not in orch.session_status_events
    events = temp_db.get_events(issue_id=agent.issue_id, event_type="heartbeat_refreshed")
    assert len(events) == 1


# =============================================================================
# BUG-2: Blocking time.sleep / subprocess calls in event loop
# =============================================================================


@pytest.mark.asyncio
async def test_bug2_event_loop_not_blocked_during_worktree_creation(tmp_path):
    """Verify that create_worktree_async doesn't block the event loop.

    We do this by running a concurrent task that increments a counter.
    If the event loop is blocked, the counter won't increment.
    """
    import subprocess

    # Create a git repo
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("# test\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=repo, check=True, capture_output=True)

    from hive.git import create_worktree_async, remove_worktree_async

    counter = 0
    done = False

    async def tick_counter():
        nonlocal counter
        while not done:
            counter += 1
            await asyncio.sleep(0.01)

    # Start counter task
    task = asyncio.create_task(tick_counter())

    # Create and remove worktree via async wrapper
    wt = await create_worktree_async(str(repo), "async-test")
    await remove_worktree_async(wt)

    done = True
    await task

    # If the event loop was blocked, counter would be 0 or very low
    assert counter > 0, "Counter didn't increment during async worktree creation — event loop was blocked by synchronous subprocess call"


# =============================================================================
# BUG-3: Orphaned agent record when worktree creation fails
# =============================================================================


@pytest.mark.asyncio
async def test_bug3_agent_marked_failed_on_worktree_error(temp_db, tmp_path):
    """Verify that when worktree creation fails, the agent is marked failed
    instead of being left as an orphan in 'idle' status.

    Before the fix, spawn_worker would create_agent() then return early
    on worktree error, leaving the agent record in 'idle' forever.
    """
    mock_oc = AsyncMock(spec=HiveBackend)
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    # Create an issue to work on
    issue_id = temp_db.create_issue("Test task", project="test")
    issue = temp_db.get_issue(issue_id)

    # Patch create_worktree_async to fail
    with patch("hive.orchestrator.create_worktree_async", new_callable=AsyncMock, side_effect=Exception("git ref contention")):
        await orch.spawn_worker(issue)

    # With ephemeral agents, the agent should be deleted immediately on failure
    cursor = temp_db.conn.execute("SELECT * FROM agents")
    agents = cursor.fetchall()
    assert len(agents) == 0, "No agents should remain after worktree creation failure (ephemeral agents)"

    # Issue should NOT be claimed (should still be open)
    issue = temp_db.get_issue(issue_id)
    assert issue["status"] == "open"
    assert issue["assignee"] is None


# =============================================================================
# DC-2: Double-handling race between SSE handler and monitor_agent
# =============================================================================


@pytest.mark.asyncio
async def test_dc2_handling_guard_prevents_concurrent_processing(temp_db, tmp_path):
    """Verify DB fence prevents duplicate completion handling.

    First call claims agent handling via DB CAS; second call is a no-op.
    """
    mock_oc = AsyncMock(spec=HiveBackend)
    mock_oc.get_messages = AsyncMock(return_value=[])
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    agent = _make_agent(temp_db, orch)

    # Call handle_agent_complete first
    await orch.handle_agent_complete(agent)

    # Count events from first call
    events_after_first = temp_db.get_events(issue_id=agent.issue_id)
    first_call_count = len(events_after_first)

    # Re-add agent to active_agents to simulate a stale in-memory entry.
    orch.active_agents[agent.agent_id] = agent

    # Second call should be blocked by DB CAS fence.
    await orch.handle_agent_complete(agent)

    # No additional events should have been logged
    events_after_second = temp_db.get_events(issue_id=agent.issue_id)
    assert len(events_after_second) == first_call_count, (
        "Second concurrent call to handle_agent_complete logged additional events despite DB handling fence"
    )


@pytest.mark.asyncio
async def test_dc2_handling_guard_cleanup_on_exception(temp_db, tmp_path):
    """Verify DB fence still blocks re-entry after exception."""
    mock_oc = AsyncMock(spec=HiveBackend)
    # Make get_messages raise to trigger exception path
    mock_oc.get_messages = AsyncMock(side_effect=Exception("API Error"))
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    agent = _make_agent(temp_db, orch)

    # First call exercises exception path and tears down agent.
    await orch.handle_agent_complete(agent)

    orch.active_agents[agent.agent_id] = agent
    events_after_first = len(temp_db.get_events(issue_id=agent.issue_id))
    await orch.handle_agent_complete(agent)
    events_after_second = len(temp_db.get_events(issue_id=agent.issue_id))
    assert events_after_second == events_after_first


@pytest.mark.asyncio
async def test_dc2_stalled_handler_uses_guard(temp_db, tmp_path):
    """Verify handle_stalled_agent respects DB handling fence."""
    mock_oc = AsyncMock(spec=HiveBackend)
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    agent = _make_agent(temp_db, orch)

    # Pretend another handler has already claimed this agent.
    temp_db.try_transition_agent_status(agent.agent_id, from_status="working", to_status="failed")

    # Call handle_stalled_agent — should be a no-op
    await orch.handle_stalled_agent(agent)

    # No 'stalled' event should have been logged
    stall_events = [e for e in temp_db.get_events(issue_id=agent.issue_id) if e["event_type"] == "stalled"]
    assert len(stall_events) == 0, "handle_stalled_agent ran despite DB handling fence"


# =============================================================================
# SA-2: Stuck 'running' merge entries after daemon crash
# =============================================================================


@pytest.mark.asyncio
async def test_sa2_initialize_resets_stuck_running_merges(temp_db, tmp_path):
    """Verify MergeProcessor.initialize() resets 'running' merges to 'queued'.

    If the daemon crashes mid-merge, the entry stays in 'running' forever.
    On next startup, initialize() should reset it so it gets retried.
    """
    from hive.merge import MergeProcessor

    mock_oc = AsyncMock(spec=HiveBackend)
    mock_oc.create_session = AsyncMock(return_value={"id": "refinery-session"})

    mp = MergeProcessor(temp_db, mock_oc, str(tmp_path), "test")

    # Create a merge queue entry stuck in 'running' (simulates crash mid-merge)
    issue_id = temp_db.create_issue("Stuck merge task", project="test")
    agent_id = temp_db.create_agent("stuck-agent")
    temp_db.conn.execute(
        "INSERT INTO merge_queue (issue_id, agent_id, project, worktree, branch_name, status) VALUES (?, ?, ?, ?, ?, 'running')",
        (issue_id, agent_id, "test", "/tmp/wt", "agent/stuck-agent"),
    )
    temp_db.conn.commit()

    # Verify it's stuck in 'running'
    cursor = temp_db.conn.execute("SELECT status FROM merge_queue WHERE issue_id = ?", (issue_id,))
    assert cursor.fetchone()["status"] == "running"

    # Initialize should reset it
    await mp.initialize()

    # Verify it's been reset to 'queued'
    cursor = temp_db.conn.execute("SELECT status FROM merge_queue WHERE issue_id = ?", (issue_id,))
    assert cursor.fetchone()["status"] == "queued", (
        "Stuck 'running' merge entry was not reset to 'queued' on startup — this merge will be permanently stuck after a daemon crash"
    )

    # System event should be logged
    events = temp_db.get_events(event_type="stuck_merges_reset")
    assert len(events) == 1


@pytest.mark.asyncio
async def test_sa2_initialize_ignores_queued_entries(temp_db, tmp_path):
    """Verify initialize() doesn't touch entries that are already 'queued'."""
    from hive.merge import MergeProcessor

    mock_oc = AsyncMock(spec=HiveBackend)
    mock_oc.create_session = AsyncMock(return_value={"id": "refinery-session"})

    mp = MergeProcessor(temp_db, mock_oc, str(tmp_path), "test")

    # Create a normal queued entry
    issue_id = temp_db.create_issue("Normal merge", project="test")
    agent_id = temp_db.create_agent("normal-agent")
    temp_db.conn.execute(
        "INSERT INTO merge_queue (issue_id, agent_id, project, worktree, branch_name) VALUES (?, ?, ?, ?, ?)",
        (issue_id, agent_id, "test", "/tmp/wt", "agent/normal-agent"),
    )
    temp_db.conn.commit()

    await mp.initialize()

    # Entry should still be queued
    cursor = temp_db.conn.execute("SELECT status FROM merge_queue WHERE issue_id = ?", (issue_id,))
    assert cursor.fetchone()["status"] == "queued"

    # No stuck_merges_reset event (nothing was stuck)
    events = temp_db.get_events(event_type="stuck_merges_reset")
    assert len(events) == 0


# =============================================================================
# NEW-1: Session leak in spawn_worker exception handler
# =============================================================================


@pytest.mark.asyncio
async def test_new1_spawn_worker_cleans_up_session_on_post_creation_failure(temp_db, tmp_path):
    """Verify that when spawn_worker fails AFTER session creation, the session
    is cleaned up (abort+delete), the agent is marked failed, and in-memory
    tracking is cleared.

    Before the fix, the except block only removed the worktree and marked the
    issue failed — it leaked the backend session, left the agent in
    active_agents, and didn't clean up reverse lookup maps.
    """
    mock_oc = AsyncMock(spec=HiveBackend)
    mock_oc.create_session = AsyncMock(return_value={"id": "leaked-session-999"})
    # Make send_message_async fail to trigger the except block AFTER session creation
    mock_oc.send_message_async = AsyncMock(side_effect=Exception("network timeout"))

    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    # Create an issue to work on
    issue_id = temp_db.create_issue("Spawn failure task", project="test")
    issue = temp_db.get_issue(issue_id)

    # Patch create_worktree_async to succeed (returns a fake path)
    fake_worktree = str(tmp_path / "fake-worktree")
    with patch("hive.orchestrator.create_worktree_async", new_callable=AsyncMock, return_value=fake_worktree):
        with patch("hive.orchestrator.remove_worktree_async", new_callable=AsyncMock):
            await orch.spawn_worker(issue)

    # Session should have been cleaned up (abort + delete)
    mock_oc.cleanup_session.assert_called_once_with("leaked-session-999", directory=fake_worktree)

    # active_agents should be empty (agent was unregistered)
    assert len(orch.active_agents) == 0, f"active_agents has {len(orch.active_agents)} entries — agent was not unregistered after spawn failure"

    # Reverse lookup maps should be empty
    assert len(orch._session_to_agent) == 0, "Session-to-agent map not cleaned up after spawn failure"
    assert len(orch._issue_to_agent) == 0, "Issue-to-agent map not cleaned up after spawn failure"

    # Agent DB record should be marked failed
    cursor = temp_db.conn.execute("SELECT status, session_id FROM agents")
    agents = cursor.fetchall()
    assert len(agents) == 1
    agent = dict(agents[0])
    assert agent["status"] == "failed", f"Agent status is '{agent['status']}', expected 'failed'"
    assert agent["session_id"] is None, "Agent session_id should be NULL after cleanup"


@pytest.mark.asyncio
async def test_new1_spawn_worker_no_session_cleanup_when_creation_fails(temp_db, tmp_path):
    """Verify that when session creation itself fails (before session_id is set),
    cleanup_session is NOT called (there's nothing to clean up).
    """
    mock_oc = AsyncMock(spec=HiveBackend)
    # Session creation itself fails
    mock_oc.create_session = AsyncMock(side_effect=Exception("Backend unreachable"))

    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    issue_id = temp_db.create_issue("Session creation failure", project="test")
    issue = temp_db.get_issue(issue_id)

    fake_worktree = str(tmp_path / "fake-worktree")
    with patch("hive.orchestrator.create_worktree_async", new_callable=AsyncMock, return_value=fake_worktree):
        with patch("hive.orchestrator.remove_worktree_async", new_callable=AsyncMock):
            await orch.spawn_worker(issue)

    # cleanup_session should NOT have been called (session_id was None)
    mock_oc.cleanup_session.assert_not_called()

    # Agent should still be marked failed
    cursor = temp_db.conn.execute("SELECT status FROM agents")
    agent = dict(cursor.fetchone())
    assert agent["status"] == "failed"


# =============================================================================
# NEW-3: Orphaned agent record on failed claim
# =============================================================================


@pytest.mark.asyncio
async def test_new3_agent_marked_failed_on_claim_failure(temp_db, tmp_path):
    """Verify that when the CAS claim fails (another worker claimed the issue
    first), the agent record is marked 'failed' instead of left in 'idle'.

    Before the fix, only the worktree was cleaned up and the agent record
    lingered in the DB indefinitely.
    """
    mock_oc = AsyncMock(spec=HiveBackend)
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    # Create an issue and pre-claim it (simulating another worker won the race)
    issue_id = temp_db.create_issue("Contested task", project="test")
    other_agent_id = temp_db.create_agent("winner-agent")
    temp_db.claim_issue(issue_id, other_agent_id)  # Winner claims first

    issue = temp_db.get_issue(issue_id)

    fake_worktree = str(tmp_path / "loser-worktree")
    with patch("hive.orchestrator.create_worktree_async", new_callable=AsyncMock, return_value=fake_worktree):
        with patch("hive.orchestrator.remove_worktree_async", new_callable=AsyncMock) as mock_remove:
            await orch.spawn_worker(issue)

    # Worktree should have been cleaned up
    mock_remove.assert_called_once_with(fake_worktree)

    # With ephemeral agents, the loser agent should be deleted immediately
    cursor = temp_db.conn.execute("SELECT * FROM agents WHERE id != ?", (other_agent_id,))
    loser_agents = cursor.fetchall()
    assert len(loser_agents) == 0, "Loser agent should be deleted immediately after failed claim (ephemeral agents)"

    # No session should have been created (we didn't get that far)
    mock_oc.create_session.assert_not_called()
