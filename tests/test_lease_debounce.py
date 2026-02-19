import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta
from hive.orchestrator import Orchestrator
from hive.utils import AgentIdentity
from hive.config import Config

@pytest.fixture
def mock_db():
    db = MagicMock()
    db.conn = MagicMock()
    return db

@pytest.fixture
def mock_opencode():
    return MagicMock()

def test_lease_renewal_debounce(mock_db, mock_opencode):
    """Test that lease renewal is debounced."""
    orch = Orchestrator(mock_db, mock_opencode, "/tmp")

    # Mock Config.LEASE_EXTENSION
    with patch("hive.orchestrator.Config") as mock_config:
        # Default lease extension is 600s
        mock_config.LEASE_EXTENSION = 600
        # Expected interval is 600 / 10 = 60s
        interval = mock_config.LEASE_EXTENSION / 10

        # Setup agent and session
        session_id = "sess-1"
        agent_id = "agent-1"
        agent = AgentIdentity(agent_id, "test", "issue-1", "/tmp", session_id)
        orch.active_agents[agent_id] = agent
        orch._session_to_agent[session_id] = agent_id

        # 1. First call - should hit DB
        orch._renew_lease_for_session(session_id)
        assert mock_db.conn.execute.call_count == 1

        # Capture last activity
        last_activity_1 = orch._session_last_activity[session_id]

        # 2. Immediate second call - should be debounced (no DB hit)
        # We patch datetime.now so that we can control time and ensure it's > last_activity_1 but < interval
        with patch("hive.orchestrator.datetime") as mock_datetime:
             mock_datetime.now.return_value = last_activity_1 + timedelta(seconds=1)
             orch._renew_lease_for_session(session_id)

        # Expect debounce (call count remains 1)
        assert mock_db.conn.execute.call_count == 1, "Should be debounced"

        # Verify last_activity updated even if debounced
        # Since we mocked datetime, it should be the mocked time
        assert orch._session_last_activity[session_id] == last_activity_1 + timedelta(seconds=1)

        # 3. Call after interval - should hit DB again
        # We need to ensure that the internal state tracking last DB update (if present) is old enough.
        # Since implementation details like _session_last_db_renewal are hidden,
        # we can only test this by waiting (mocking time).

        with patch("hive.orchestrator.datetime") as mock_datetime:
             mock_datetime.now.return_value = last_activity_1 + timedelta(seconds=interval + 5)
             orch._renew_lease_for_session(session_id)

        assert mock_db.conn.execute.call_count == 2, "Should hit DB after interval"
