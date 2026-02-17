import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta
from hive.orchestrator import Orchestrator
from hive.utils import AgentIdentity

class MockDatabase:
    def __init__(self):
        self.conn = MagicMock()

@pytest.fixture
def mock_db():
    return MockDatabase()

@pytest.fixture
def mock_opencode():
    return MagicMock()

@pytest.fixture
def orchestrator(mock_db, mock_opencode):
    # Patch Config to ensure consistent LEASE_EXTENSION if needed,
    # but the code uses Config.LEASE_EXTENSION directly.
    # We assume default config is fine (600s), and debounce is 60s.
    return Orchestrator(
        db=mock_db,
        opencode_client=mock_opencode,
        project_path="/tmp/test",
        project_name="test_project"
    )

def test_renew_lease_debounce(orchestrator):
    # Setup
    session_id = "session-123"
    agent_id = "agent-123"

    # Mock agent registration
    agent = AgentIdentity(
        agent_id=agent_id,
        name="worker-1",
        issue_id="issue-1",
        worktree="/tmp/worktree",
        session_id=session_id
    )
    orchestrator.active_agents[agent_id] = agent
    orchestrator._session_to_agent[session_id] = agent_id

    # Mock datetime.now()
    # We need to mock it where it's used: hive.orchestrator.datetime
    with patch("hive.orchestrator.datetime") as mock_datetime:
        start_time = datetime(2023, 1, 1, 12, 0, 0)
        mock_datetime.now.return_value = start_time

        # 1. First call: should update DB
        orchestrator._renew_lease_for_session(session_id)
        assert orchestrator.db.conn.execute.call_count == 1

        # Verify last_renewal was set
        assert orchestrator._session_last_lease_renewal[session_id] == start_time

        # 2. Second call immediately after: should NOT update DB
        orchestrator._renew_lease_for_session(session_id)
        assert orchestrator.db.conn.execute.call_count == 1 # Still 1

        # 3. Third call 30 seconds later: should NOT update DB
        mock_datetime.now.return_value = start_time + timedelta(seconds=30)
        orchestrator._renew_lease_for_session(session_id)
        assert orchestrator.db.conn.execute.call_count == 1 # Still 1

        # 4. Fourth call 61 seconds later: SHOULD update DB
        # The debounce is 60s. So > 60s should trigger update.
        new_time = start_time + timedelta(seconds=61)
        mock_datetime.now.return_value = new_time
        orchestrator._renew_lease_for_session(session_id)
        assert orchestrator.db.conn.execute.call_count == 2

        # Verify last_renewal was updated
        assert orchestrator._session_last_lease_renewal[session_id] == new_time

def test_monitor_agent_cleanup(orchestrator):
    # Verify cleanup removes the debounce entry

    session_id = "session-cleanup-123"
    agent_id = "agent-cleanup-123"

    agent = AgentIdentity(
        agent_id=agent_id,
        name="worker-cleanup",
        issue_id="issue-cleanup",
        worktree="/tmp/worktree",
        session_id=session_id
    )

    # Pre-populate state
    orchestrator.session_status_events[session_id] = MagicMock()
    orchestrator._session_last_activity[session_id] = datetime.now()
    orchestrator._session_last_lease_renewal[session_id] = datetime.now()

    # We need to run monitor_agent. It's async.
    # To test cleanup logic specifically, we can check if the finally block runs.
    # However, running the full async loop might be complex.
    # Since we just added one line to the finally block, verifying that
    # the finally block logic *would* remove it is enough if we trust Python's finally.

    # But let's try to run it and cancel it to trigger finally.
    import asyncio

    async def run_cleanup_test():
        # Create a task for monitor_agent
        # We need to mock wait_for to raise CancelledError or something to exit loop?
        # Or just Cancel the task.

        # We need to mock wait_for to just hang so we can cancel it.
        # But monitor_agent has a loop.

        # Let's mock `asyncio.wait_for` inside orchestrator to hang forever
        with patch("hive.orchestrator.asyncio.wait_for", side_effect=asyncio.CancelledError):
             try:
                 await orchestrator.monitor_agent(agent)
             except asyncio.CancelledError:
                 pass

        # Verify cleanup
        assert session_id not in orchestrator._session_last_lease_renewal
        assert session_id not in orchestrator._session_last_activity

    # Run the async test
    asyncio.run(run_cleanup_test())
