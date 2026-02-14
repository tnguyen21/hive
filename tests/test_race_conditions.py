"""Regression tests for race conditions fixed in the race-condition audit.

Each test in this file validates a specific bug that was found and fixed.
The test is designed to FAIL against the old code and PASS against the fix.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from hive.config import Config
from hive.db import Database
from hive.models import AgentIdentity, CompletionResult
from hive.opencode import OpenCodeClient
from hive.orchestrator import Orchestrator
from hive.sse import SSEClient


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
#         molecule cycling mutates agent.session_id
# =============================================================================


@pytest.mark.asyncio
async def test_bug1_monitor_agent_preserves_new_session_event_after_cycling(temp_db, tmp_path):
    """Verify monitor_agent's finally block cleans up its OWN session event,
    not the new session's event created by cycle_agent_to_next_step.

    Before the fix, the finally block used agent.session_id which got mutated
    by cycle_agent_to_next_step, causing it to delete the new session's event.

    We mock handle_agent_complete to simulate what molecule cycling does:
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

    # Mock handle_agent_complete to simulate molecule cycling:
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
    assert old_session_id not in orch.session_status_events, (
        "Old session's event should have been cleaned up by monitor_agent's finally block"
    )


# =============================================================================
# BUG-2: Blocking time.sleep / subprocess calls in event loop
# =============================================================================


@pytest.mark.asyncio
async def test_bug2_create_worktree_async_exists():
    """Verify async wrappers exist and are importable."""
    from hive.git import (
        create_worktree_async,
        remove_worktree_async,
        rebase_onto_main_async,
        abort_rebase_async,
        merge_to_main_async,
        run_command_in_worktree_async,
        delete_branch_async,
    )

    # All should be coroutine functions
    import inspect

    assert inspect.iscoroutinefunction(create_worktree_async)
    assert inspect.iscoroutinefunction(remove_worktree_async)
    assert inspect.iscoroutinefunction(rebase_onto_main_async)
    assert inspect.iscoroutinefunction(abort_rebase_async)
    assert inspect.iscoroutinefunction(merge_to_main_async)
    assert inspect.iscoroutinefunction(run_command_in_worktree_async)
    assert inspect.iscoroutinefunction(delete_branch_async)


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
    assert counter > 0, (
        "Counter didn't increment during async worktree creation — "
        "event loop was blocked by synchronous subprocess call"
    )


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

    # Find the agent that was created (there should be exactly one)
    cursor = temp_db.conn.execute("SELECT * FROM agents")
    agents = cursor.fetchall()
    assert len(agents) == 1

    agent = dict(agents[0])
    assert agent["status"] == "failed", (
        f"Agent status is '{agent['status']}' but should be 'failed'. "
        "Orphan agent left in DB after worktree creation failure."
    )

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
        "Second concurrent call to handle_agent_complete logged additional events — "
        "_handling_agents guard failed to prevent double processing"
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
        "_handling_agents not cleaned up after exception — "
        "future calls for this agent will be permanently blocked"
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
        "SSE client running flag was reset to True by connect()! "
        "This means stop() is unreliable — the client will keep reconnecting."
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
        "Stuck 'running' merge entry was not reset to 'queued' on startup — "
        "this merge will be permanently stuck after a daemon crash"
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
