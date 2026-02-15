"""Performance tests for orchestrator."""

from unittest.mock import MagicMock, AsyncMock
import pytest
from hive.orchestrator import Orchestrator
from hive.models import AgentIdentity
from hive.db import Database

@pytest.mark.asyncio
async def test_check_stalled_agents_query_count(tmp_path):
    """Test that check_stalled_agents makes queries proportional to agent count (before fix)."""
    # Create mocks
    mock_db = MagicMock(spec=Database)
    mock_conn = MagicMock()
    mock_db.conn = mock_conn
    mock_opencode = AsyncMock()

    # Mock execute to return a cursor with one stalled agent
    mock_cursor = MagicMock()
    # Mock fetchall returns one row with stalled agent id "agent-0"
    mock_cursor.fetchall.return_value = [("agent-0",)]

    mock_conn.execute.return_value = mock_cursor

    # Create orchestrator with mocks
    orch = Orchestrator(
        db=mock_db,
        opencode_client=mock_opencode,
        project_path=str(tmp_path),
        project_name="test-project",
    )

    # Mock _handle_stalled_with_session_check
    orch._handle_stalled_with_session_check = AsyncMock()

    # Add 5 active agents to internal state
    agent_count = 5
    for i in range(agent_count):
        agent_id = f"agent-{i}"
        agent = AgentIdentity(
            agent_id=agent_id,
            name=f"agent-{i}",
            issue_id=f"issue-{i}",
            worktree="/tmp",
            session_id=f"session-{i}"
        )
        orch.active_agents[agent_id] = agent

    # Run check_stalled_agents
    await orch.check_stalled_agents()

    # In the optimized implementation (1 query), expected calls = 1.
    assert mock_conn.execute.call_count == 1

    # Verify _handle_stalled_with_session_check called for agent-0
    orch._handle_stalled_with_session_check.assert_called_once()
    assert orch._handle_stalled_with_session_check.call_args[0][0].agent_id == "agent-0"
