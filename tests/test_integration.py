"""Integration tests for the Hive orchestrator with fake backend.

These tests exercise the real orchestrator code against a FakeBackend
to validate the seams between components: event flow, session lifecycle,
completion handling, retry escalation, and epic cycling.

Run with: pytest -m integration -v
"""

import asyncio
from unittest.mock import patch

import pytest

from hive.config import Config
from tests.conftest import await_session_created, complete_worker, create_worker_commit, run_orchestrator_until, write_hive_result


# =============================================================================
# Fake backend plumbing validation
# =============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fake_backend_basic_functionality(fake_backend):
    """Validate the FakeBackend: session CRUD, messages, event dispatch."""
    # Create a session
    session = await fake_backend.create_session(title="Test Session")
    session_id = session["id"]
    assert session_id.startswith("fake-")
    assert session_id in fake_backend.get_created_sessions()

    # Initial status is idle
    status = await fake_backend.get_session_status(session_id)
    assert status["type"] == "idle"

    # Send message → status becomes busy, message is recorded
    await fake_backend.send_message_async(session_id, [{"type": "text", "text": "Hello"}])
    status = await fake_backend.get_session_status(session_id)
    assert status["type"] == "busy"

    messages = await fake_backend.get_messages(session_id)
    assert len(messages) == 1

    # inject_idle_async → status becomes idle
    await fake_backend.inject_idle_async(session_id)
    status = await fake_backend.get_session_status(session_id)
    assert status["type"] == "idle"

    # list_sessions returns flat list
    sessions = await fake_backend.list_sessions()
    assert isinstance(sessions, list)
    assert any(s["id"] == session_id for s in sessions)

    # Abort + delete
    await fake_backend.abort_session(session_id)
    await fake_backend.delete_session(session_id)

    # Permissions endpoint
    perms = await fake_backend.get_pending_permissions()
    assert perms == []


# =============================================================================
# Direct completion test (bypasses SSE, validates handle_agent_complete)
# =============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_happy_path_direct_completion(integration_orchestrator, fake_backend, temp_git_repo):
    """Issue → spawn_worker → write result → handle_agent_complete → merge queued.

    This test calls handle_agent_complete directly (bypasses SSE monitoring)
    to validate the completion logic in isolation.
    """
    orch = integration_orchestrator

    issue_id = orch.db.create_issue(title="Test feature", description="Implement X", priority=1, issue_type="task", project="test-project")

    ready_issues = orch.db.get_ready_queue()
    assert len(ready_issues) == 1

    await orch.spawn_worker(ready_issues[0])

    # Verify session created on fake server
    sessions = fake_backend.get_created_sessions()
    assert len(sessions) >= 1

    # Get agent
    issue = orch.db.get_issue(issue_id)
    assert issue["status"] == "in_progress"
    agent_id = issue["assignee"]
    agent = orch.active_agents[agent_id]

    # Write result and call handle_agent_complete directly
    commit_hash = create_worker_commit(agent.worktree)
    write_hive_result(
        worktree_path=agent.worktree,
        status="success",
        summary="Implemented X",
        artifacts=[{"type": "git_commit", "value": commit_hash}],
    )

    from hive.prompts import read_result_file

    file_result = read_result_file(agent.worktree)
    await orch.handle_agent_complete(agent, file_result=file_result)

    # Verify final state
    assert orch.db.get_issue(issue_id)["status"] == "done"
    assert agent_id not in orch.active_agents

    merges = orch.db.list_merge_entries("test-project", status="queued")
    assert any(m["issue_id"] == issue_id for m in merges)

    events = orch.db.get_events(issue_id=issue_id)
    event_types = [e["event_type"] for e in events]
    for expected in ["created", "claimed", "worker_started", "completed"]:
        assert expected in event_types, f"Missing event: {expected}"


# =============================================================================
# Full end-to-end via SSE
# =============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_happy_path_via_sse(integration_orchestrator, fake_backend, temp_git_repo):
    """Full integration: issue → main_loop spawns → SSE idle → completion → merge queued.

    This is the "one test that matters most" — it exercises the full flow
    through the real orchestrator code with SSE-driven completion detection.
    """
    orch = integration_orchestrator

    issue_id = orch.db.create_issue(title="Add widget", description="Build widget X", priority=1, issue_type="task", project="test-project")

    async def inject_completion_when_ready():
        # Wait for spawn_worker to create a session
        session_id = await await_session_created(fake_backend, count=1, timeout=5)

        # Find the agent for this session
        for _ in range(50):
            for agent in orch.active_agents.values():
                if agent.session_id == session_id:
                    # Give monitor_agent time to set up its asyncio.Event
                    await asyncio.sleep(0.2)
                    complete_worker(fake_backend, session_id, agent.worktree)
                    return
            await asyncio.sleep(0.05)
        raise RuntimeError("Agent not found for session")

    inject_task = asyncio.create_task(inject_completion_when_ready())

    def issue_is_done():
        issue = orch.db.get_issue(issue_id)
        return issue and issue["status"] == "done"

    await run_orchestrator_until(orch, issue_is_done, timeout=10)
    await inject_task

    # Verify merge queue entry
    merges = orch.db.list_merge_entries("test-project", status="queued")
    assert any(m["issue_id"] == issue_id for m in merges)

    # Verify full event trail
    events = orch.db.get_events(issue_id=issue_id)
    event_types = [e["event_type"] for e in events]
    for expected in ["created", "claimed", "worker_started", "completed"]:
        assert expected in event_types, f"Missing event: {expected}"


# =============================================================================
# Worker failure + retry
# =============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_worker_failure_and_retry(integration_orchestrator, fake_backend, temp_git_repo):
    """First attempt fails → retry → second attempt succeeds."""
    orch = integration_orchestrator

    issue_id = orch.db.create_issue(title="Fix bug", description="Fix the thing", priority=1, issue_type="task", project="test-project")

    async def inject_outcomes():
        # First attempt: failure
        sid1 = await await_session_created(fake_backend, count=1, timeout=5)
        for _ in range(50):
            for agent in orch.active_agents.values():
                if agent.session_id == sid1:
                    await asyncio.sleep(0.2)
                    complete_worker(fake_backend, sid1, agent.worktree, status="failed", summary="Tests broke")
                    break
            else:
                await asyncio.sleep(0.05)
                continue
            break

        # Wait for retry: new session created (the issue gets reset to open,
        # then main_loop picks it up and spawns a new worker)
        # The count includes the refinery session created by merge_processor.initialize(),
        # so we need to account for that. Actually, count is based on created_session_ids
        # which tracks ALL sessions. Let's wait for the right count.
        sid2 = await await_session_created(fake_backend, count=len(fake_backend.created_session_ids) + 1, timeout=10)
        for _ in range(50):
            for agent in orch.active_agents.values():
                if agent.session_id == sid2:
                    await asyncio.sleep(0.2)
                    complete_worker(fake_backend, sid2, agent.worktree, status="success", summary="Fixed it")
                    return
            await asyncio.sleep(0.05)
        raise RuntimeError("Agent not found for retry session")

    inject_task = asyncio.create_task(inject_outcomes())

    def issue_is_done():
        issue = orch.db.get_issue(issue_id)
        return issue and issue["status"] == "done"

    await run_orchestrator_until(orch, issue_is_done, timeout=15)
    await inject_task

    # Verify retry happened
    assert orch.db.count_events(issue_id, "retry") >= 1
    assert orch.db.count_events(issue_id, "incomplete") >= 1

    # Verify final completion
    events = orch.db.get_events(issue_id=issue_id)
    event_types = [e["event_type"] for e in events]
    assert "completed" in event_types


# =============================================================================
# Stall detection
# =============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stall_detection(integration_orchestrator, fake_backend, temp_git_repo):
    """Worker becomes non-runnable, stall handler fires, retry succeeds.

    With LEASE_DURATION=4 and check_interval=min(30, 4//4)=1, the monitor_agent
    loop checks every 1s. After the backend session disappears, the stalled
    path should trigger and the retry should succeed.
    """
    orch = integration_orchestrator

    issue_id = orch.db.create_issue(title="Stalling task", description="Will stall", priority=1, issue_type="task", project="test-project")

    async def handle_stall_then_succeed():
        # First attempt: let the session go non-runnable without emitting idle.
        sid1 = await await_session_created(fake_backend, count=1, timeout=5)
        await asyncio.sleep(Config.LEASE_DURATION + 1)
        await fake_backend.cleanup_session(sid1)

        current_count = len(fake_backend.created_session_ids)
        sid2 = await await_session_created(fake_backend, count=current_count + 1, timeout=15)

        # Complete the retry
        for _ in range(50):
            for agent in orch.active_agents.values():
                if agent.session_id == sid2:
                    await asyncio.sleep(0.2)
                    complete_worker(fake_backend, sid2, agent.worktree, summary="Fixed after stall")
                    return
            await asyncio.sleep(0.05)
        raise RuntimeError("Agent not found for retry session")

    inject_task = asyncio.create_task(handle_stall_then_succeed())

    def issue_is_done():
        issue = orch.db.get_issue(issue_id)
        return issue and issue["status"] == "done"

    await run_orchestrator_until(orch, issue_is_done, timeout=25)
    await inject_task

    # Verify stall event was logged
    stall_events = orch.db.get_events(issue_id=issue_id, event_type="stalled")
    assert len(stall_events) >= 1


# =============================================================================
# Budget exhaustion
# =============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_budget_exhaustion(integration_orchestrator, fake_backend, temp_git_repo):
    """Exceed per-issue token budget → failure handling triggered."""
    orch = integration_orchestrator

    with patch.object(Config, "MAX_TOKENS_PER_ISSUE", 100):
        issue_id = orch.db.create_issue(title="Expensive task", priority=1, issue_type="task", project="test-project")

        async def inject_with_high_tokens():
            sid = await await_session_created(fake_backend, count=1, timeout=5)

            for _ in range(50):
                for agent in orch.active_agents.values():
                    if agent.session_id == sid:
                        # Pre-populate messages with high token metadata so
                        # _log_token_usage extracts them and budget check fires
                        fake_backend.set_messages(
                            sid,
                            [
                                {"metadata": {"input_tokens": 80, "output_tokens": 50, "model": "test-model"}},
                            ],
                        )
                        await asyncio.sleep(0.2)
                        complete_worker(fake_backend, sid, agent.worktree, summary="Done but expensive")
                        return
                await asyncio.sleep(0.05)
            raise RuntimeError("Agent not found")

        inject_task = asyncio.create_task(inject_with_high_tokens())

        def budget_exceeded():
            events = orch.db.get_events(issue_id=issue_id, event_type="budget_exceeded")
            return len(events) > 0

        await run_orchestrator_until(orch, budget_exceeded, timeout=10)
        await inject_task

        # Verify budget_exceeded event
        events = orch.db.get_events(issue_id=issue_id, event_type="budget_exceeded")
        assert len(events) == 1


# =============================================================================
# Startup reconciliation
# =============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_startup_reconciliation(integration_orchestrator, fake_backend, temp_git_repo):
    """Pre-populate stale agents in DB → reconcile → verify cleanup."""
    orch = integration_orchestrator

    # Create a stale agent directly in DB (simulating previous daemon crash)
    issue_id = orch.db.create_issue(title="Stale task", priority=1, issue_type="task", project="test-project")
    agent_id = orch.db.create_agent("stale-worker")
    orch.db.claim_issue(issue_id, agent_id)

    # Create a matching session on the fake backend
    session_data = await fake_backend.create_session(title="stale session", directory=str(temp_git_repo))
    stale_session_id = session_data["id"]

    # Update agent with stale session
    orch.db.conn.execute(
        "UPDATE agents SET session_id = ?, worktree = ? WHERE id = ?",
        (stale_session_id, str(temp_git_repo), agent_id),
    )
    orch.db.conn.commit()

    # Run reconciliation
    await orch._reconcile_stale_agents()

    # Agent should be purged after being reconciled to failed
    agent = orch.db.get_agent(agent_id)
    assert agent is None

    # Issue should be released back to open
    issue = orch.db.get_issue(issue_id)
    assert issue["status"] == "open"
    assert issue["assignee"] is None

    # Session should be cleaned up on fake server (abort+delete)
    assert stale_session_id not in fake_backend.sessions


# =============================================================================
# Session cleanup on spawn failure (validates NEW-1 fix end-to-end)
# =============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_cleanup_on_spawn_failure(integration_orchestrator, fake_backend, temp_git_repo):
    """Session created then send_message fails → session deleted on fake server.

    This validates the NEW-1 fix (session leak in spawn_worker) against a
    real fake server, not just mock call counts.
    """
    orch = integration_orchestrator

    issue_id = orch.db.create_issue(title="Doomed task", priority=1, issue_type="task", project="test-project")
    issue = orch.db.get_issue(issue_id)

    # Patch send_message_async to fail after session creation
    original_send = orch.backend.send_message_async

    async def failing_send(*args, **kwargs):
        raise Exception("Simulated send failure")

    orch.backend.send_message_async = failing_send

    try:
        await orch.spawn_worker(issue)
    finally:
        orch.backend.send_message_async = original_send

    # A session was created on the fake server
    sessions = fake_backend.get_created_sessions()
    assert len(sessions) >= 1
    created_sid = sessions[-1]

    # But it should have been cleaned up (abort + delete removes it from fake_backend.sessions)
    assert created_sid not in fake_backend.sessions, f"Session {created_sid} was not cleaned up after spawn failure — session leak!"

    # Issue should be escalated
    issue = orch.db.get_issue(issue_id)
    assert issue["status"] == "escalated"

    # Spawn error event logged
    events = orch.db.get_events(issue_id=issue_id, event_type="spawn_error")
    assert len(events) == 1
