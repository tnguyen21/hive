"""Tests for multi-worker pool."""


def test_get_active_agents(temp_db):
    """Test getting all active agents."""
    # Create agents
    agent1 = temp_db.create_agent("agent-1")
    agent2 = temp_db.create_agent("agent-2")
    agent3 = temp_db.create_agent("agent-3")

    # Set some to working
    temp_db.conn.execute("UPDATE agents SET status = 'working' WHERE id = ?", (agent1,))
    temp_db.conn.execute("UPDATE agents SET status = 'working' WHERE id = ?", (agent3,))
    temp_db.conn.commit()

    active = temp_db.get_active_agents()

    assert len(active) == 2
    active_ids = [a["id"] for a in active]
    assert agent1 in active_ids
    assert agent3 in active_ids
    assert agent2 not in active_ids
