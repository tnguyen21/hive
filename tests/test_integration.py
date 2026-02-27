"""Integration tests for the Hive orchestrator with fake OpenCode server.

These tests exercise the real orchestrator code against a fake OpenCode server
(FakeOpenCodeServer) to validate the seams between components: SSE event flow,
session lifecycle, completion handling, retry escalation, and epic cycling.

Run with: pytest -m integration -v
"""

import asyncio
import json
from unittest.mock import patch

import pytest

from hive.config import Config
from hive.backends import OpenCodeClient
from tests.conftest import await_session_created, complete_worker, run_orchestrator_until, write_hive_result


# =============================================================================
# Fake server plumbing validation
# =============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fake_server_basic_functionality(fake_server):
    """Validate the fake OpenCode server: session CRUD, messages, global SSE."""
    async with OpenCodeClient(base_url=fake_server.url) as client:
        # Create a session
        session = await client.create_session(title="Test Session")
        session_id = session["id"]
        assert session_id.startswith("fake-")
        assert session_id in fake_server.get_created_sessions()

        # Initial status is idle
        status = await client.get_session_status(session_id)
        assert status["type"] == "idle"

        # Send message → status becomes busy, message is recorded
        await client.send_message_async(session_id, [{"type": "text", "text": "Hello"}])
        status = await client.get_session_status(session_id)
        assert status["type"] == "busy"

        messages = await client.get_messages(session_id)
        assert len(messages) == 1

        # inject_idle → status becomes idle
        fake_server.inject_idle(session_id)
        status = await client.get_session_status(session_id)
        assert status["type"] == "idle"

        # Global SSE stream receives the event
        events_received = []

        async def collect_one_event():
            from aiohttp import ClientSession, ClientTimeout

            timeout = ClientTimeout(total=3)
            async with ClientSession(timeout=timeout) as http:
                async with http.get(f"{fake_server.url}/global/event") as resp:
                    async for line in resp.content:
                        decoded = line.decode("utf-8").strip()
                        if decoded.startswith("data: "):
                            events_received.append(json.loads(decoded[6:]))
                            return

        # Push another event and verify it arrives via global SSE
        task = asyncio.create_task(collect_one_event())
        await asyncio.sleep(0.1)  # Let SSE connection establish
        fake_server.inject_event(session_id, "session.status", {"sessionID": session_id, "status": {"type": "busy"}})

        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.TimeoutError:
            pytest.fail("SSE event not received via /global/event")

        assert len(events_received) == 1
        payload = events_received[0]["payload"]
        assert payload["type"] == "session.status"
        assert payload["properties"]["sessionID"] == session_id

        # list_sessions returns flat list
        sessions = await client.list_sessions()
        assert isinstance(sessions, list)
        assert any(s["id"] == session_id for s in sessions)

        # Abort + delete
        await client.abort_session(session_id)
        await client.delete_session(session_id)

        # Permissions endpoint
        perms = await client.get_pending_permissions()
        assert perms == []


# =============================================================================
# Direct completion test (bypasses SSE, validates handle_agent_complete)
# =============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_happy_path_direct_completion(integration_orchestrator, fake_server, temp_git_repo):
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
    sessions = fake_server.get_created_sessions()
    assert len(sessions) >= 1

    # Get agent
    issue = orch.db.get_issue(issue_id)
    assert issue["status"] == "in_progress"
    agent_id = issue["assignee"]
    agent = orch.active_agents[agent_id]

    # Write result and call handle_agent_complete directly
    write_hive_result(worktree_path=agent.worktree, status="success", summary="Implemented X")

    from hive.prompts import read_result_file

    file_result = read_result_file(agent.worktree)
    await orch.handle_agent_complete(agent, file_result=file_result)

    # Verify final state
    assert orch.db.get_issue(issue_id)["status"] == "done"
    assert agent_id not in orch.active_agents

    merges = orch.db.get_queued_merges()
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
async def test_happy_path_via_sse(integration_orchestrator, fake_server, temp_git_repo):
    """Full integration: issue → main_loop spawns → SSE idle → completion → merge queued.

    This is the "one test that matters most" — it exercises the full flow
    through the real orchestrator code with SSE-driven completion detection.
    """
    orch = integration_orchestrator

    issue_id = orch.db.create_issue(title="Add widget", description="Build widget X", priority=1, issue_type="task", project="test-project")

    async def inject_completion_when_ready():
        # Wait for spawn_worker to create a session
        session_id = await await_session_created(fake_server, count=1, timeout=5)

        # Find the agent for this session
        for _ in range(50):
            for agent in orch.active_agents.values():
                if agent.session_id == session_id:
                    # Give monitor_agent time to set up its asyncio.Event
                    await asyncio.sleep(0.2)
                    complete_worker(fake_server, session_id, agent.worktree)
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
    merges = orch.db.get_queued_merges()
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
async def test_worker_failure_and_retry(integration_orchestrator, fake_server, temp_git_repo):
    """First attempt fails → retry → second attempt succeeds."""
    orch = integration_orchestrator

    issue_id = orch.db.create_issue(title="Fix bug", description="Fix the thing", priority=1, issue_type="task", project="test-project")

    async def inject_outcomes():
        # First attempt: failure
        sid1 = await await_session_created(fake_server, count=1, timeout=5)
        for _ in range(50):
            for agent in orch.active_agents.values():
                if agent.session_id == sid1:
                    await asyncio.sleep(0.2)
                    complete_worker(fake_server, sid1, agent.worktree, status="failed", summary="Tests broke")
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
        sid2 = await await_session_created(fake_server, count=len(fake_server.created_session_ids) + 1, timeout=10)
        for _ in range(50):
            for agent in orch.active_agents.values():
                if agent.session_id == sid2:
                    await asyncio.sleep(0.2)
                    complete_worker(fake_server, sid2, agent.worktree, status="success", summary="Fixed it")
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
    assert orch.db.count_events_by_type(issue_id, "retry") >= 1
    assert orch.db.count_events_by_type(issue_id, "incomplete") >= 1

    # Verify final completion
    events = orch.db.get_events(issue_id=issue_id)
    event_types = [e["event_type"] for e in events]
    assert "completed" in event_types


# =============================================================================
# Epic flow (3 sequential steps, no session cycling)
# =============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_epic_three_step_flow(integration_orchestrator, fake_server, temp_git_repo):
    """Parent epic with 3 sequential steps → each step runs as its own worker."""
    orch = integration_orchestrator

    # Create epic with 3 sequential steps
    parent_id = orch.db.create_issue(title="Big feature", issue_type="epic", project="test-project")
    step1_id = orch.db.create_issue(title="Step 1: API", parent_id=parent_id, issue_type="step", project="test-project")
    step2_id = orch.db.create_issue(title="Step 2: Frontend", parent_id=parent_id, issue_type="step", project="test-project")
    step3_id = orch.db.create_issue(title="Step 3: Tests", parent_id=parent_id, issue_type="step", project="test-project")

    # Sequential: step2 depends on step1, step3 depends on step2
    orch.db.add_dependency(step2_id, step1_id)
    orch.db.add_dependency(step3_id, step2_id)

    async def inject_step_completions():
        # Step 1: wait for initial spawn
        sid1 = await await_session_created(fake_server, count=1, timeout=5)
        for _ in range(50):
            for agent in orch.active_agents.values():
                if agent.session_id == sid1:
                    await asyncio.sleep(0.2)
                    complete_worker(fake_server, sid1, agent.worktree, summary="Step 1 done")
                    break
            else:
                await asyncio.sleep(0.05)
                continue
            break

        # Step 2: new worker/session (no cycling)
        sid2 = await await_session_created(fake_server, count=2, timeout=5)
        for _ in range(50):
            for agent in orch.active_agents.values():
                if agent.session_id == sid2:
                    await asyncio.sleep(0.2)
                    complete_worker(fake_server, sid2, agent.worktree, summary="Step 2 done")
                    break
            else:
                await asyncio.sleep(0.05)
                continue
            break

        # Step 3: new worker/session
        sid3 = await await_session_created(fake_server, count=3, timeout=5)
        for _ in range(50):
            for agent in orch.active_agents.values():
                if agent.session_id == sid3:
                    await asyncio.sleep(0.2)
                    complete_worker(fake_server, sid3, agent.worktree, summary="Step 3 done")
                    return
            await asyncio.sleep(0.05)
        raise RuntimeError("Agent not found for step 3 session")

    inject_task = asyncio.create_task(inject_step_completions())

    def all_steps_done():
        s1 = orch.db.get_issue(step1_id)
        s2 = orch.db.get_issue(step2_id)
        s3 = orch.db.get_issue(step3_id)
        return s1 and s1["status"] == "done" and s2 and s2["status"] == "done" and s3 and s3["status"] == "done"

    await run_orchestrator_until(orch, all_steps_done, timeout=15)
    await inject_task

    # Each step should enqueue a merge (merge processing is disabled in the fixture).
    merges = orch.db.get_queued_merges()
    merge_issue_ids = {m["issue_id"] for m in merges}
    assert {step1_id, step2_id, step3_id}.issubset(merge_issue_ids)


# =============================================================================
# Stall detection
# =============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_stall_detection(integration_orchestrator, fake_server, temp_git_repo):
    """Worker stalls (no SSE activity), stall handler fires, retry succeeds.

    With LEASE_DURATION=4 and check_interval=min(30, 4//4)=1, the monitor_agent
    loop checks every 1s. After 4s of no activity it triggers handle_stalled_agent.
    """
    orch = integration_orchestrator

    issue_id = orch.db.create_issue(title="Stalling task", description="Will stall", priority=1, issue_type="task", project="test-project")

    async def handle_stall_then_succeed():
        # First attempt: don't inject anything — let it stall
        await await_session_created(fake_server, count=1, timeout=5)

        # Wait for stall detection to fire and main_loop to spawn a retry.
        # LEASE_DURATION=4s, so we need to wait a bit longer.
        # The stall handler marks the agent failed, releases the issue,
        # and main_loop picks it up for retry.
        current_count = len(fake_server.created_session_ids)
        sid2 = await await_session_created(fake_server, count=current_count + 1, timeout=15)

        # Complete the retry
        for _ in range(50):
            for agent in orch.active_agents.values():
                if agent.session_id == sid2:
                    await asyncio.sleep(0.2)
                    complete_worker(fake_server, sid2, agent.worktree, summary="Fixed after stall")
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
async def test_budget_exhaustion(integration_orchestrator, fake_server, temp_git_repo):
    """Exceed per-issue token budget → failure handling triggered."""
    orch = integration_orchestrator

    with patch.object(Config, "MAX_TOKENS_PER_ISSUE", 100):
        issue_id = orch.db.create_issue(title="Expensive task", priority=1, issue_type="task", project="test-project")

        async def inject_with_high_tokens():
            sid = await await_session_created(fake_server, count=1, timeout=5)

            for _ in range(50):
                for agent in orch.active_agents.values():
                    if agent.session_id == sid:
                        # Pre-populate messages with high token metadata so
                        # _log_token_usage extracts them and budget check fires
                        fake_server.set_messages(
                            sid,
                            [
                                {"metadata": {"input_tokens": 80, "output_tokens": 50, "model": "test-model"}},
                            ],
                        )
                        await asyncio.sleep(0.2)
                        complete_worker(fake_server, sid, agent.worktree, summary="Done but expensive")
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
async def test_startup_reconciliation(integration_orchestrator, fake_server, temp_git_repo):
    """Pre-populate stale agents in DB → reconcile → verify cleanup."""
    orch = integration_orchestrator

    # Create a stale agent directly in DB (simulating previous daemon crash)
    issue_id = orch.db.create_issue(title="Stale task", priority=1, issue_type="task", project="test-project")
    agent_id = orch.db.create_agent("stale-worker")
    orch.db.claim_issue(issue_id, agent_id)

    # Create a matching session on the fake server
    from aiohttp import ClientSession

    async with ClientSession() as http:
        resp = await http.post(
            f"{fake_server.url}/session",
            json={"title": "stale session"},
            headers={"X-OpenCode-Directory": str(temp_git_repo)},
        )
        session_data = await resp.json()
        stale_session_id = session_data["id"]

    # Update agent with stale session
    orch.db.conn.execute(
        "UPDATE agents SET session_id = ?, worktree = ? WHERE id = ?",
        (stale_session_id, str(temp_git_repo), agent_id),
    )
    orch.db.conn.commit()

    # Run reconciliation
    await orch._reconcile_stale_agents()

    # Agent should be marked failed
    agent = orch.db.get_agent(agent_id)
    assert agent["status"] == "failed"

    # Issue should be released back to open
    issue = orch.db.get_issue(issue_id)
    assert issue["status"] == "open"
    assert issue["assignee"] is None

    # Session should be cleaned up on fake server (abort+delete)
    assert stale_session_id not in fake_server.sessions


# =============================================================================
# Session cleanup on spawn failure (validates NEW-1 fix end-to-end)
# =============================================================================


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_cleanup_on_spawn_failure(integration_orchestrator, fake_server, temp_git_repo):
    """Session created then send_message fails → session deleted on fake server.

    This validates the NEW-1 fix (session leak in spawn_worker) against a
    real fake server, not just mock call counts.
    """
    orch = integration_orchestrator

    issue_id = orch.db.create_issue(title="Doomed task", priority=1, issue_type="task", project="test-project")
    issue = orch.db.get_issue(issue_id)

    # Patch send_message_async to fail after session creation
    original_send = orch.opencode.send_message_async

    async def failing_send(*args, **kwargs):
        raise Exception("Simulated send failure")

    orch.opencode.send_message_async = failing_send

    try:
        await orch.spawn_worker(issue)
    finally:
        orch.opencode.send_message_async = original_send

    # A session was created on the fake server
    sessions = fake_server.get_created_sessions()
    assert len(sessions) >= 1
    created_sid = sessions[-1]

    # But it should have been cleaned up (abort + delete removes it from fake_server.sessions)
    assert created_sid not in fake_server.sessions, f"Session {created_sid} was not cleaned up after spawn failure — session leak!"

    # Issue should be marked failed
    issue = orch.db.get_issue(issue_id)
    assert issue["status"] == "failed"

    # Spawn error event logged
    events = orch.db.get_events(issue_id=issue_id, event_type="spawn_error")
    assert len(events) == 1
