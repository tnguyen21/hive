"""Regression tests for race conditions fixed in the race-condition audit.

Each test in this file validates a specific bug that was found and fixed.
The test is designed to FAIL against the old code and PASS against the fix.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from hive.utils import AgentIdentity
from hive.backends import OpenCodeClient
from hive.orchestrator import Orchestrator
from hive.backends import SSEClient


# --- Helpers ---


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


def _make_agent(temp_db, orch, name="test-agent", issue_title="Test task", session_id="session-123", worktree="/tmp/wt"):
    """Create an agent identity and register it in the orchestrator."""
    issue_id = temp_db.create_issue(issue_title)
    agent_id = temp_db.create_agent(name)

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
#         epic cycling mutates agent.session_id
# =============================================================================


@pytest.mark.asyncio
async def test_bug1_monitor_agent_preserves_new_session_event_after_cycling(temp_db, tmp_path):
    """Verify monitor_agent's finally block cleans up its OWN session event,
    not the new session's event created by cycle_agent_to_next_step.

    Before the fix, the finally block used agent.session_id which got mutated
    by cycle_agent_to_next_step, causing it to delete the new session's event.

    We mock handle_agent_complete to simulate what epic cycling does:
    mutate agent.session_id and create a new Event in session_status_events.
    This isolates the BUG-1 fix (snapshotting my_session_id) from side effects
    of the full cycling flow (spawned tasks, DB interactions, etc.).
    """
    mock_oc = AsyncMock(spec=OpenCodeClient)
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

    # Mock handle_agent_complete to simulate epic cycling:
    # it mutates agent.session_id and creates a new event for the new session.
    # This is exactly what cycle_agent_to_next_step does (among other things).
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
    mock_oc = AsyncMock(spec=OpenCodeClient)
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    old_session_id = "old-session-333"
    new_session_id = "new-session-444"
    agent = _make_agent(temp_db, orch, name="poll-worker", issue_title="Step 1", session_id=old_session_id, worktree=str(tmp_path))

    orch.session_status_events[old_session_id] = asyncio.Event()
    orch._poll_session_idle = AsyncMock(return_value=True)
    orch.handle_agent_complete = AsyncMock()

    async def timeout_and_mutate(awaitable, timeout):
        agent.session_id = new_session_id
        awaitable.close()
        raise asyncio.TimeoutError

    with patch("hive.orchestrator.asyncio.wait_for", side_effect=timeout_and_mutate):
        await orch.monitor_agent(agent)

    orch._poll_session_idle.assert_awaited_once_with(
        old_session_id,
        str(tmp_path),
        agent_id=agent.agent_id,
        issue_id=agent.issue_id,
    )


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
    mock_oc = AsyncMock(spec=OpenCodeClient)
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
    """Verify that _handling_agents guard prevents two handlers from
    processing the same agent simultaneously.

    Simulates: handle_agent_complete is running (yields at await),
    and handle_stalled_agent is called for the same agent. The second
    call should be a no-op.
    """
    mock_oc = AsyncMock(spec=OpenCodeClient)
    mock_oc.get_messages = AsyncMock(return_value=[])
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    agent = _make_agent(temp_db, orch)

    # Call handle_agent_complete first
    await orch.handle_agent_complete(agent)

    # Count events from first call
    events_after_first = temp_db.get_events(issue_id=agent.issue_id)
    first_call_count = len(events_after_first)

    # Re-add agent to active_agents (simulating it wasn't removed yet
    # because the first handler yielded at an await point)
    orch.active_agents[agent.agent_id] = agent

    # Manually add to _handling_agents to simulate concurrent execution
    orch._handling_agents.add(agent.agent_id)

    # Second call should be blocked by the guard
    await orch.handle_agent_complete(agent)

    # No additional events should have been logged
    events_after_second = temp_db.get_events(issue_id=agent.issue_id)
    assert len(events_after_second) == first_call_count, (
        "Second concurrent call to handle_agent_complete logged additional events — _handling_agents guard failed to prevent double processing"
    )

    orch._handling_agents.discard(agent.agent_id)


@pytest.mark.asyncio
async def test_dc2_handling_guard_cleanup_on_exception(temp_db, tmp_path):
    """Verify that _handling_agents is cleaned up even when processing raises."""
    mock_oc = AsyncMock(spec=OpenCodeClient)
    # Make get_messages raise to trigger exception path
    mock_oc.get_messages = AsyncMock(side_effect=Exception("API Error"))
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    agent = _make_agent(temp_db, orch)

    await orch.handle_agent_complete(agent)

    # Guard should be cleaned up after exception
    assert agent.agent_id not in orch._handling_agents, (
        "_handling_agents not cleaned up after exception — future calls for this agent will be permanently blocked"
    )


@pytest.mark.asyncio
async def test_dc2_stalled_handler_uses_guard(temp_db, tmp_path):
    """Verify handle_stalled_agent also respects the _handling_agents guard."""
    mock_oc = AsyncMock(spec=OpenCodeClient)
    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    agent = _make_agent(temp_db, orch)

    # Pretend agent is already being handled
    orch._handling_agents.add(agent.agent_id)

    # Call handle_stalled_agent — should be a no-op
    await orch.handle_stalled_agent(agent)

    # No 'stalled' event should have been logged
    stall_events = [e for e in temp_db.get_events(issue_id=agent.issue_id) if e["event_type"] == "stalled"]
    assert len(stall_events) == 0, "handle_stalled_agent ran despite _handling_agents guard"

    orch._handling_agents.discard(agent.agent_id)


# =============================================================================
# DC-4: SSE stop() defeated by connect() resetting self.running
# =============================================================================


@pytest.mark.asyncio
async def test_dc4_stop_not_defeated_by_connect():
    """Verify that calling stop() actually stops the SSE client,
    and connect() does NOT reset self.running = True.

    Before the fix, connect() always set self.running = True, so if
    stop() was called between reconnect attempts, the next connect()
    would undo the stop.
    """
    client = SSEClient(base_url="http://localhost:9999")  # Non-existent

    # Simulate: connect_with_reconnect sets running=True, then connect fails
    client.running = True
    client.stop()
    assert client.running is False

    # Now simulate what happens if connect() is called after stop()
    # Before the fix, this would reset running to True
    try:
        await asyncio.wait_for(client.connect(), timeout=0.5)
    except Exception:
        pass  # Connection will fail, that's expected

    # Critical: running should still be False after connect() returns
    assert client.running is False, (
        "SSE client running flag was reset to True by connect()! This means stop() is unreliable — the client will keep reconnecting."
    )


@pytest.mark.asyncio
async def test_dc4_connect_with_reconnect_sets_running():
    """Verify connect_with_reconnect sets running=True at the start."""
    client = SSEClient(base_url="http://localhost:9999")
    assert client.running is False

    # connect_with_reconnect should set running=True
    # It will fail to connect and retry, but we stop it quickly
    task = asyncio.create_task(client.connect_with_reconnect(max_retries=1, retry_delay=0))

    await asyncio.sleep(0.1)
    # running should have been set to True by connect_with_reconnect
    was_running = client.running
    client.stop()

    try:
        await asyncio.wait_for(task, timeout=2.0)
    except Exception:
        pass

    assert was_running is True, "connect_with_reconnect didn't set running=True at start"


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

    mock_oc = AsyncMock(spec=OpenCodeClient)
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

    mock_oc = AsyncMock(spec=OpenCodeClient)
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
    issue failed — it leaked the OpenCode session, left the agent in
    active_agents, and didn't clean up reverse lookup maps.
    """
    mock_oc = AsyncMock(spec=OpenCodeClient)
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
    mock_oc = AsyncMock(spec=OpenCodeClient)
    # Session creation itself fails
    mock_oc.create_session = AsyncMock(side_effect=Exception("OpenCode unreachable"))

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
# NEW-2: Session not deleted in cycle_agent_to_next_step
# =============================================================================


@pytest.mark.asyncio
async def test_new2_cycle_agent_deletes_old_session(temp_db, tmp_path):
    """Verify that cycle_agent_to_next_step uses cleanup_session (abort+delete)
    for the old session, not just abort_session.

    Before the fix, only abort_session was called, leaving the old session
    object lingering on the OpenCode server.
    """
    mock_oc = AsyncMock(spec=OpenCodeClient)
    mock_oc.create_session = AsyncMock(return_value={"id": "new-session-456"})
    mock_oc.send_message_async = AsyncMock()

    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    old_session_id = "old-session-123"
    agent = _make_agent(temp_db, orch, session_id=old_session_id, worktree=str(tmp_path))

    # Mark the agent's issue as in_progress
    temp_db.conn.execute("UPDATE issues SET status = 'in_progress', assignee = ? WHERE id = ?", (agent.agent_id, agent.issue_id))
    temp_db.conn.execute("UPDATE agents SET status = 'working', current_issue = ? WHERE id = ?", (agent.issue_id, agent.agent_id))
    temp_db.conn.commit()

    # Create a next step issue
    next_step_id = temp_db.create_issue("Step 2", project="test")
    next_step = temp_db.get_issue(next_step_id)

    await orch.cycle_agent_to_next_step(agent, next_step)

    # cleanup_session should have been called on the OLD session (abort + delete)
    mock_oc.cleanup_session.assert_called_once_with(old_session_id, directory=str(tmp_path))

    # abort_session should NOT have been called separately (cleanup_session does both)
    mock_oc.abort_session.assert_not_called()


@pytest.mark.asyncio
async def test_new2_cycle_agent_cleans_up_new_session_on_failure(temp_db, tmp_path):
    """Verify that when cycle_agent_to_next_step fails AFTER creating the new
    session, the new session is cleaned up.

    Before the fix, the except block only unregistered the agent but didn't
    clean up the newly created OpenCode session.
    """
    mock_oc = AsyncMock(spec=OpenCodeClient)
    mock_oc.create_session = AsyncMock(return_value={"id": "new-session-leaked"})
    # Make send_message_async fail AFTER session creation
    mock_oc.send_message_async = AsyncMock(side_effect=Exception("send failed"))

    orch = _make_orchestrator(temp_db, tmp_path, mock_oc)

    agent = _make_agent(temp_db, orch, session_id="old-session-123", worktree=str(tmp_path))

    # Set up agent DB state
    temp_db.conn.execute("UPDATE issues SET status = 'in_progress', assignee = ? WHERE id = ?", (agent.agent_id, agent.issue_id))
    temp_db.conn.execute("UPDATE agents SET status = 'working', current_issue = ? WHERE id = ?", (agent.issue_id, agent.agent_id))
    temp_db.conn.commit()

    next_step_id = temp_db.create_issue("Step 2", project="test")
    next_step = temp_db.get_issue(next_step_id)

    await orch.cycle_agent_to_next_step(agent, next_step)

    # cleanup_session should have been called TWICE:
    # 1. On the old session (the normal abort+delete at the top)
    # 2. On the new session (error cleanup in except block)
    calls = mock_oc.cleanup_session.call_args_list
    assert len(calls) == 2, f"Expected 2 cleanup_session calls, got {len(calls)}"

    # First call: old session cleanup
    assert calls[0].args[0] == "old-session-123"
    # Second call: new session cleanup (the fix)
    assert calls[1].args[0] == "new-session-leaked"

    # Agent should be unregistered from active_agents
    assert agent.agent_id not in orch.active_agents


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
    mock_oc = AsyncMock(spec=OpenCodeClient)
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
