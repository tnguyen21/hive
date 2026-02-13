"""Tests for database operations."""

import json
import sqlite3
from pathlib import Path

import pytest

from hive.db import Database


def test_database_connection(temp_db):
    """Test database connection and schema creation."""
    assert temp_db.conn is not None
    assert isinstance(temp_db.conn, sqlite3.Connection)

    # Check that tables were created
    cursor = temp_db.conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    tables = [row[0] for row in cursor.fetchall()]

    expected_tables = [
        "agents",
        "dependencies",
        "events",
        "issues",
        "merge_queue",
        "notes",
    ]
    for table in expected_tables:
        assert table in tables


def test_create_issue(temp_db):
    """Test issue creation."""
    issue_id = temp_db.create_issue(
        title="Test Issue",
        description="This is a test",
        priority=1,
        issue_type="bug",
        project="test-project",
    )

    assert issue_id.startswith("w-")

    # Verify issue was created
    issue = temp_db.get_issue(issue_id)
    assert issue is not None
    assert issue["title"] == "Test Issue"
    assert issue["description"] == "This is a test"
    assert issue["priority"] == 1
    assert issue["type"] == "bug"
    assert issue["project"] == "test-project"
    assert issue["status"] == "open"
    assert issue["assignee"] is None

    # Verify event was logged
    events = temp_db.get_events(issue_id=issue_id)
    assert len(events) == 1
    assert events[0]["event_type"] == "created"


def test_create_issue_with_metadata(temp_db):
    """Test issue creation with metadata."""
    metadata = {"tags": ["urgent", "security"], "estimate": "2h"}
    issue_id = temp_db.create_issue(
        title="Test with metadata",
        description="Testing metadata",
        metadata=metadata,
    )

    issue = temp_db.get_issue(issue_id)
    stored_metadata = json.loads(issue["metadata"])
    assert stored_metadata == metadata


def test_get_ready_queue_empty(temp_db):
    """Test ready queue when no issues exist."""
    ready = temp_db.get_ready_queue()
    assert ready == []


def test_get_ready_queue_with_issues(db_with_issues):
    """Test ready queue returns unblocked, unassigned issues."""
    db, issues = db_with_issues

    ready = db.get_ready_queue()

    # issue1 and issue2 should be ready (no dependencies)
    # issue3 should NOT be ready (depends on issue1)
    assert len(ready) == 2
    ready_ids = [item["id"] for item in ready]
    assert issues["issue1"] in ready_ids
    assert issues["issue2"] in ready_ids
    assert issues["issue3"] not in ready_ids


def test_get_ready_queue_priority_ordering(temp_db):
    """Test that ready queue orders by priority."""
    # Create issues with different priorities
    low = temp_db.create_issue("Low priority", priority=4)
    high = temp_db.create_issue("High priority", priority=0)
    medium = temp_db.create_issue("Medium priority", priority=2)

    ready = temp_db.get_ready_queue()
    assert len(ready) == 3
    assert ready[0]["id"] == high  # Priority 0 first
    assert ready[1]["id"] == medium  # Priority 2 second
    assert ready[2]["id"] == low  # Priority 4 last


def test_get_ready_queue_excludes_assigned(temp_db):
    """Test that ready queue excludes assigned issues."""
    issue_id = temp_db.create_issue("Test issue")
    agent_id = temp_db.create_agent("test-agent")

    # Initially should be in ready queue
    ready = temp_db.get_ready_queue()
    assert len(ready) == 1

    # Claim the issue
    success = temp_db.claim_issue(issue_id, agent_id)
    assert success

    # Should no longer be in ready queue
    ready = temp_db.get_ready_queue()
    assert len(ready) == 0


def test_get_ready_queue_resolved_dependencies(db_with_issues):
    """Test that issues become ready when dependencies are resolved."""
    db, issues = db_with_issues

    # issue3 depends on issue1, so it's not initially ready
    ready = db.get_ready_queue()
    assert issues["issue3"] not in [item["id"] for item in ready]

    # Mark issue1 as done
    db.update_issue_status(issues["issue1"], "done")

    # Now issue3 should be ready
    ready = db.get_ready_queue()
    assert issues["issue3"] in [item["id"] for item in ready]


def test_claim_issue_success(temp_db):
    """Test successful atomic claim."""
    issue_id = temp_db.create_issue("Test issue")
    agent_id = temp_db.create_agent("test-agent")

    success = temp_db.claim_issue(issue_id, agent_id)
    assert success

    # Verify issue is now assigned and in_progress
    issue = temp_db.get_issue(issue_id)
    assert issue["assignee"] == agent_id
    assert issue["status"] == "in_progress"

    # Verify agent's current_issue is updated
    agent = temp_db.get_agent(agent_id)
    assert agent["current_issue"] == issue_id
    assert agent["status"] == "working"

    # Verify event was logged
    events = temp_db.get_events(issue_id=issue_id)
    event_types = [e["event_type"] for e in events]
    assert "claimed" in event_types


def test_claim_issue_already_claimed(temp_db):
    """Test that claiming an already-claimed issue fails."""
    issue_id = temp_db.create_issue("Test issue")
    agent1_id = temp_db.create_agent("agent-1")
    agent2_id = temp_db.create_agent("agent-2")

    # First claim should succeed
    success1 = temp_db.claim_issue(issue_id, agent1_id)
    assert success1

    # Second claim should fail (CAS failure)
    success2 = temp_db.claim_issue(issue_id, agent2_id)
    assert not success2

    # Verify issue is still assigned to agent1
    issue = temp_db.get_issue(issue_id)
    assert issue["assignee"] == agent1_id


def test_claim_issue_concurrent(temp_db):
    """Test atomic claim behavior with concurrent attempts."""
    issue_id = temp_db.create_issue("Test issue")
    agent1_id = temp_db.create_agent("agent-1")
    agent2_id = temp_db.create_agent("agent-2")
    agent3_id = temp_db.create_agent("agent-3")

    # Simulate concurrent claims (only one should succeed)
    results = [
        temp_db.claim_issue(issue_id, agent1_id),
        temp_db.claim_issue(issue_id, agent2_id),
        temp_db.claim_issue(issue_id, agent3_id),
    ]

    # Exactly one should succeed
    assert sum(results) == 1

    # Verify issue is assigned to exactly one agent
    issue = temp_db.get_issue(issue_id)
    assert issue["assignee"] in [agent1_id, agent2_id, agent3_id]


def test_claim_issue_blocked_by_dependency(temp_db):
    """Test that claiming an issue with unresolved blocking deps fails."""
    blocker_id = temp_db.create_issue("Blocker issue")
    blocked_id = temp_db.create_issue("Blocked issue")
    agent_id = temp_db.create_agent("test-agent")

    # Add dependency: blocked depends on blocker
    temp_db.add_dependency(blocked_id, blocker_id, "blocks")

    # Claim should fail — blocker is still open
    success = temp_db.claim_issue(blocked_id, agent_id)
    assert not success

    # Issue should remain open and unassigned
    issue = temp_db.get_issue(blocked_id)
    assert issue["status"] == "open"
    assert issue["assignee"] is None


def test_claim_issue_resolved_dependency(temp_db):
    """Test that claiming succeeds when blocking deps are resolved."""
    blocker_id = temp_db.create_issue("Blocker issue")
    blocked_id = temp_db.create_issue("Blocked issue")
    agent_id = temp_db.create_agent("test-agent")

    temp_db.add_dependency(blocked_id, blocker_id, "blocks")

    # Resolve the blocker
    temp_db.update_issue_status(blocker_id, "finalized")

    # Now claim should succeed
    success = temp_db.claim_issue(blocked_id, agent_id)
    assert success

    issue = temp_db.get_issue(blocked_id)
    assert issue["status"] == "in_progress"
    assert issue["assignee"] == agent_id


def test_claim_issue_dep_race_condition(temp_db):
    """Test the exact race condition: issue created, then dep added before claim."""
    # Simulate: issue created (appears in ready queue), then dep added
    issue_id = temp_db.create_issue("New issue")
    blocker_id = temp_db.create_issue("Blocker")
    agent_id = temp_db.create_agent("test-agent")

    # Dep added after creation but before claim (the race window)
    temp_db.add_dependency(issue_id, blocker_id, "blocks")

    # Claim should fail even though the issue was "ready" when created
    success = temp_db.claim_issue(issue_id, agent_id)
    assert not success

    issue = temp_db.get_issue(issue_id)
    assert issue["status"] == "open"
    assert issue["assignee"] is None


def test_log_event(temp_db):
    """Test event logging."""
    issue_id = temp_db.create_issue("Test issue")
    agent_id = temp_db.create_agent("test-agent")

    temp_db.log_event(issue_id, agent_id, "test_event", {"key": "value"})

    events = temp_db.get_events(issue_id=issue_id)
    # Should have 2 events: created + test_event
    assert len(events) >= 2

    test_event = [e for e in events if e["event_type"] == "test_event"][0]
    assert test_event["issue_id"] == issue_id
    assert test_event["agent_id"] == agent_id
    detail = json.loads(test_event["detail"])
    assert detail == {"key": "value"}


def test_create_agent(temp_db):
    """Test agent creation."""
    agent_id = temp_db.create_agent("test-agent", model="claude-sonnet-4-5")

    assert agent_id.startswith("agent-")

    agent = temp_db.get_agent(agent_id)
    assert agent is not None
    assert agent["name"] == "test-agent"
    assert agent["model"] == "claude-sonnet-4-5"
    assert agent["status"] == "idle"
    assert agent["current_issue"] is None


def test_add_dependency(temp_db):
    """Test adding dependencies between issues."""
    issue1 = temp_db.create_issue("Task 1")
    issue2 = temp_db.create_issue("Task 2")

    temp_db.add_dependency(issue2, issue1)

    # issue2 should not be in ready queue since it depends on issue1
    ready = temp_db.get_ready_queue()
    ready_ids = [item["id"] for item in ready]
    assert issue1 in ready_ids
    assert issue2 not in ready_ids


def test_update_issue_status(temp_db):
    """Test updating issue status."""
    issue_id = temp_db.create_issue("Test issue")

    temp_db.update_issue_status(issue_id, "done")

    issue = temp_db.get_issue(issue_id)
    assert issue["status"] == "done"
    assert issue["closed_at"] is not None

    # Verify event was logged
    events = temp_db.get_events(issue_id=issue_id)
    event_types = [e["event_type"] for e in events]
    assert "status_done" in event_types


def test_wal_mode_enabled(temp_db):
    """Test that WAL mode is enabled."""
    cursor = temp_db.conn.execute("PRAGMA journal_mode")
    mode = cursor.fetchone()[0]
    assert mode.lower() == "wal"


def test_foreign_keys_enabled(temp_db):
    """Test that foreign keys are enabled."""
    cursor = temp_db.conn.execute("PRAGMA foreign_keys")
    enabled = cursor.fetchone()[0]
    assert enabled == 1


# --- Merge queue method tests ---


@pytest.fixture
def db_with_merge_queue(temp_db):
    """Create a DB with issues and merge queue entries."""
    db = temp_db

    # Create agents first (needed for FK)
    agent_id = db.create_agent(name="worker-abc")

    # Create issues
    id1 = db.create_issue(title="Feature A", project="test")
    id2 = db.create_issue(title="Feature B", project="test")
    id3 = db.create_issue(title="Feature C", project="test")

    # Mark them done
    db.update_issue_status(id1, "done")
    db.update_issue_status(id2, "done")
    db.update_issue_status(id3, "done")

    # Enqueue to merge queue
    db.conn.execute(
        "INSERT INTO merge_queue (issue_id, agent_id, project, worktree, branch_name) VALUES (?, ?, ?, ?, ?)",
        (id1, agent_id, "test", "/tmp/wt1", "agent/worker-1"),
    )
    db.conn.execute(
        "INSERT INTO merge_queue (issue_id, agent_id, project, worktree, branch_name) VALUES (?, ?, ?, ?, ?)",
        (id2, agent_id, "test", "/tmp/wt2", "agent/worker-2"),
    )
    db.conn.execute(
        "INSERT INTO merge_queue (issue_id, agent_id, project, worktree, branch_name, status) VALUES (?, ?, ?, ?, ?, ?)",
        (id3, agent_id, "test", "/tmp/wt3", "agent/worker-3", "merged"),
    )
    db.conn.commit()

    return db, agent_id, [id1, id2, id3]


def test_get_queued_merges(db_with_merge_queue):
    """Test retrieving queued merge entries."""
    db, agent_id, issue_ids = db_with_merge_queue

    merges = db.get_queued_merges()
    assert len(merges) == 2  # Only 'queued', not 'merged'

    # Should have joined fields
    assert merges[0]["issue_title"] == "Feature A"
    assert merges[0]["branch_name"] == "agent/worker-1"
    assert merges[0]["agent_name"] == "worker-abc"

    # Should be ordered by enqueued_at
    assert merges[0]["issue_id"] == issue_ids[0]
    assert merges[1]["issue_id"] == issue_ids[1]


def test_get_queued_merges_with_limit(db_with_merge_queue):
    """Test limit parameter on get_queued_merges."""
    db, _, _ = db_with_merge_queue

    merges = db.get_queued_merges(limit=1)
    assert len(merges) == 1


def test_get_queued_merges_empty(temp_db):
    """Test get_queued_merges with empty queue."""
    merges = temp_db.get_queued_merges()
    assert merges == []


def test_update_merge_queue_status(db_with_merge_queue):
    """Test updating merge queue entry status."""
    db, _, _ = db_with_merge_queue

    # Get the first queued entry
    merges = db.get_queued_merges(limit=1)
    queue_id = merges[0]["id"]

    # Update to running
    db.update_merge_queue_status(queue_id, "running")
    cursor = db.conn.execute("SELECT * FROM merge_queue WHERE id = ?", (queue_id,))
    entry = cursor.fetchone()
    assert entry is not None
    assert entry["status"] == "running"
    assert entry["completed_at"] is None

    # Update to merged with timestamp
    db.update_merge_queue_status(queue_id, "merged", completed_at="2026-02-12 12:00:00")
    cursor = db.conn.execute("SELECT * FROM merge_queue WHERE id = ?", (queue_id,))
    entry = cursor.fetchone()
    assert entry is not None
    assert entry["status"] == "merged"
    assert entry["completed_at"] == "2026-02-12 12:00:00"


def test_get_merge_queue_stats(db_with_merge_queue):
    """Test merge queue statistics."""
    db, _, _ = db_with_merge_queue

    stats = db.get_merge_queue_stats()
    assert stats["queued"] == 2
    assert stats["merged"] == 1
    assert stats["running"] == 0
    assert stats["failed"] == 0


def test_get_merge_queue_stats_empty(temp_db):
    """Test merge queue stats with empty queue."""
    stats = temp_db.get_merge_queue_stats()
    assert stats == {"queued": 0, "running": 0, "merged": 0, "failed": 0}


def test_count_events_by_type(temp_db):
    """Test counting events by type for an issue."""
    issue_id = temp_db.create_issue("Test issue")
    agent_id = temp_db.create_agent("test-agent")

    # Initially no events
    assert temp_db.count_events_by_type(issue_id, "retry") == 0
    assert temp_db.count_events_by_type(issue_id, "agent_switch") == 0

    # Log some retry events
    temp_db.log_event(issue_id, agent_id, "retry", {"attempt": 1})
    temp_db.log_event(issue_id, agent_id, "retry", {"attempt": 2})
    temp_db.log_event(issue_id, agent_id, "agent_switch", {"switch": 1})

    # Count should be correct
    assert temp_db.count_events_by_type(issue_id, "retry") == 2
    assert temp_db.count_events_by_type(issue_id, "agent_switch") == 1
    assert temp_db.count_events_by_type(issue_id, "escalated") == 0


def test_count_events_by_type_nonexistent_issue(temp_db):
    """Test counting events for non-existent issue returns 0."""
    assert temp_db.count_events_by_type("nonexistent", "retry") == 0


def test_log_system_event(temp_db):
    """Test logging system-level events."""
    temp_db.log_system_event("opencode_degraded", {"reason": "Connection timeout"})

    # Get all events
    events = temp_db.get_events()

    # Find the system event
    system_events = [e for e in events if e["event_type"] == "opencode_degraded"]
    assert len(system_events) == 1

    event = system_events[0]
    assert event["issue_id"] is None
    assert event["agent_id"] is None
    assert event["event_type"] == "opencode_degraded"

    detail = json.loads(event["detail"])
    assert detail == {"reason": "Connection timeout"}


def test_log_system_event_no_detail(temp_db):
    """Test logging system-level events without detail."""
    temp_db.log_system_event("system_started")

    # Get all events
    events = temp_db.get_events()

    # Find the system event
    system_events = [e for e in events if e["event_type"] == "system_started"]
    assert len(system_events) == 1

    event = system_events[0]
    assert event["issue_id"] is None
    assert event["agent_id"] is None
    assert event["event_type"] == "system_started"
    assert event["detail"] is None


# --- Capability scoring tests ---


def test_get_idle_agents_empty(temp_db):
    """Test get_idle_agents with no idle agents."""
    idle = temp_db.get_idle_agents()
    assert idle == []


def test_get_idle_agents_with_agents(temp_db):
    """Test get_idle_agents returns only idle agents."""
    agent1 = temp_db.create_agent("agent-1")
    agent2 = temp_db.create_agent("agent-2")
    agent3 = temp_db.create_agent("agent-3")

    # Update agent2 to working status
    temp_db.conn.execute("UPDATE agents SET status = 'working' WHERE id = ?", (agent2,))
    temp_db.conn.commit()

    idle = temp_db.get_idle_agents()
    idle_ids = [agent["id"] for agent in idle]

    assert len(idle) == 2
    assert agent1 in idle_ids
    assert agent2 not in idle_ids  # Not idle
    assert agent3 in idle_ids


def test_get_agent_capability_scores_no_idle_agents(temp_db):
    """Test scoring with no idle agents returns empty dict."""
    issue = {"project": "test-project", "type": "bug", "title": "Fix login issue"}

    scores = temp_db.get_agent_capability_scores(issue)
    assert scores == {}


def test_get_agent_capability_scores_no_track_record(temp_db):
    """Test scoring with idle agents but no track record."""
    agent1 = temp_db.create_agent("agent-1")
    agent2 = temp_db.create_agent("agent-2")

    issue = {"project": "test-project", "type": "bug", "title": "Fix login issue"}

    scores = temp_db.get_agent_capability_scores(issue)
    assert scores == {agent1: 0.0, agent2: 0.0}


@pytest.fixture
def db_with_capability_data(temp_db):
    """Create a DB with agents, issues, and completion events for capability testing."""
    db = temp_db

    # Create agents
    agent1 = db.create_agent("agent-1")
    agent2 = db.create_agent("agent-2")
    agent3 = db.create_agent("agent-3")

    # Update agent3 to working status (so it won't be considered)
    db.conn.execute("UPDATE agents SET status = 'working' WHERE id = ?", (agent3,))
    db.conn.commit()

    # Create completed issues with different characteristics
    # Issues completed by agent1
    issue1_same_project = db.create_issue(title="Fix authentication bug in login", project="webapp", issue_type="bug")
    issue2_same_type = db.create_issue(title="Resolve database connection bug", project="api", issue_type="bug")
    issue3_keyword_overlap = db.create_issue(title="Update login page styling", project="mobile", issue_type="task")

    # Issues completed by agent2
    issue4_different = db.create_issue(title="Add new dashboard feature", project="cms", issue_type="feature")
    issue5_same_project_type = db.create_issue(title="Fix header styling bug", project="webapp", issue_type="bug")

    # Mark issues as done and create completion events
    issues_agent1 = [issue1_same_project, issue2_same_type, issue3_keyword_overlap]
    issues_agent2 = [issue4_different, issue5_same_project_type]

    for issue_id in issues_agent1:
        db.update_issue_status(issue_id, "done")
        db.log_event(issue_id, agent1, "completed", {"success": True})

    for issue_id in issues_agent2:
        db.update_issue_status(issue_id, "done")
        db.log_event(issue_id, agent2, "done", {"success": True})

    return db, {
        "agent1": agent1,
        "agent2": agent2,
        "agent3": agent3,  # Working, shouldn't be scored
        "issues": {"agent1": issues_agent1, "agent2": issues_agent2},
    }


def test_get_agent_capability_scores_same_project(db_with_capability_data):
    """Test scoring prioritizes same project completions."""
    db, data = db_with_capability_data

    # New issue matching project of agent1's completed work, no keyword overlap
    issue = {"project": "webapp", "type": "feature", "title": "Add user profile page"}

    scores = db.get_agent_capability_scores(issue)

    # agent1: 1 same-project (webapp) = 3 points + 0 same-type + 1 keyword overlap ("page") = 4 total
    # agent2: 1 same-project (webapp) = 3 points + 1 same-type (feature) = 2 points + 1 keyword overlap ("add") = 6 total
    assert scores[data["agent1"]] == 4.0
    assert scores[data["agent2"]] == 6.0

    # agent3 is working, shouldn't be in scores
    assert data["agent3"] not in scores


def test_get_agent_capability_scores_same_type(db_with_capability_data):
    """Test scoring considers same type completions."""
    db, data = db_with_capability_data

    # New issue matching type of agent1's completed work, with "Fix" keyword overlap
    issue = {"project": "newproject", "type": "bug", "title": "Fix payment processing error"}

    scores = db.get_agent_capability_scores(issue)

    # agent1: 0 same-project + 2 same-type (bug issues) = 4 points + 1 keyword overlap ("Fix" in issue1) = 5 total
    # agent2: 0 same-project + 1 same-type (bug issue5) = 2 points + 1 keyword overlap ("Fix" in issue5) = 3 total
    assert scores[data["agent1"]] == 5.0
    assert scores[data["agent2"]] == 3.0


def test_get_agent_capability_scores_keyword_overlap(db_with_capability_data):
    """Test scoring considers keyword overlap."""
    db, data = db_with_capability_data

    # Issue with keywords that overlap with agent1's completed work
    issue = {"project": "newproject", "type": "feature", "title": "Improve login flow and authentication"}

    scores = db.get_agent_capability_scores(issue)

    # agent1: 0 same-project + 0 same-type + 2 keyword overlap issues ("login" in issue1&issue3, "authentication" in issue1) = 2 points
    # agent2: 0 same-project + 1 same-type (feature in issue4) + 0 keyword overlap = 2 points
    assert scores[data["agent1"]] == 2.0
    assert scores[data["agent2"]] == 2.0


def test_get_agent_capability_scores_combined_scoring(db_with_capability_data):
    """Test combined scoring with multiple matching criteria."""
    db, data = db_with_capability_data

    # Issue that matches project + type + keywords for agent1
    issue = {"project": "webapp", "type": "bug", "title": "Fix login authentication bug"}

    scores = db.get_agent_capability_scores(issue)

    # agent1:
    # - Same project (webapp): issue1 = 1 match = 3 points
    # - Same type (bug): issue1, issue2 = 2 matches = 4 points
    # - Keyword overlap: issue1 ("Fix","authentication","bug","login"), issue2 ("Fix","bug"), issue3 ("login") = 3 matches = 3 points
    # Total: 3 + 4 + 3 = 10 points
    assert scores[data["agent1"]] == 10.0

    # agent2:
    # - Same project (webapp): issue5 = 1 match = 3 points
    # - Same type (bug): issue5 = 1 match = 2 points
    # - Keyword overlap: issue5 ("Fix","bug") = 1 match = 1 point
    # Total: 3 + 2 + 1 = 6 points
    assert scores[data["agent2"]] == 6.0


def test_get_token_usage_no_data(temp_db):
    """Test get_token_usage with no data."""
    usage = temp_db.get_token_usage()

    assert usage["total_tokens"] == 0
    assert usage["total_input_tokens"] == 0
    assert usage["total_output_tokens"] == 0
    assert usage["estimated_cost_usd"] == 0
    assert usage["issue_breakdown"] == {}
    assert usage["agent_breakdown"] == {}
    assert usage["model_breakdown"] == {}


def test_get_token_usage_with_data(temp_db):
    """Test get_token_usage with token usage events."""
    # Create issue and agent
    issue_id = temp_db.create_issue("Test issue")
    agent_id = temp_db.create_agent("test-agent")

    # Add token usage events
    temp_db.log_event(issue_id, agent_id, "tokens_used", {"input_tokens": 1000, "output_tokens": 500, "model": "claude-sonnet-4-5-20250929"})
    temp_db.log_event(issue_id, agent_id, "tokens_used", {"input_tokens": 2000, "output_tokens": 1500, "model": "claude-sonnet-4-5-20250929"})

    usage = temp_db.get_token_usage()

    assert usage["total_tokens"] == 5000
    assert usage["total_input_tokens"] == 3000
    assert usage["total_output_tokens"] == 2000
    assert usage["estimated_cost_usd"] > 0  # Should have some cost

    # Check breakdowns
    assert issue_id in usage["issue_breakdown"]
    assert usage["issue_breakdown"][issue_id]["input_tokens"] == 3000
    assert usage["issue_breakdown"][issue_id]["output_tokens"] == 2000

    assert agent_id in usage["agent_breakdown"]
    assert usage["agent_breakdown"][agent_id]["input_tokens"] == 3000
    assert usage["agent_breakdown"][agent_id]["output_tokens"] == 2000

    assert "claude-sonnet-4-5-20250929" in usage["model_breakdown"]
    model_usage = usage["model_breakdown"]["claude-sonnet-4-5-20250929"]
    assert model_usage["input_tokens"] == 3000
    assert model_usage["output_tokens"] == 2000


def test_get_token_usage_filtered_by_issue(temp_db):
    """Test get_token_usage filtered by issue ID."""
    # Create two issues and agent
    issue1_id = temp_db.create_issue("Issue 1")
    issue2_id = temp_db.create_issue("Issue 2")
    agent_id = temp_db.create_agent("test-agent")

    # Add token usage for both issues
    temp_db.log_event(issue1_id, agent_id, "tokens_used", {"input_tokens": 1000, "output_tokens": 500})
    temp_db.log_event(issue2_id, agent_id, "tokens_used", {"input_tokens": 2000, "output_tokens": 1500})

    # Get usage for issue1 only
    usage = temp_db.get_token_usage(issue_id=issue1_id)

    assert usage["total_tokens"] == 1500
    assert usage["total_input_tokens"] == 1000
    assert usage["total_output_tokens"] == 500

    # Should only have issue1 in breakdown
    assert len(usage["issue_breakdown"]) == 1
    assert issue1_id in usage["issue_breakdown"]
    assert issue2_id not in usage["issue_breakdown"]


def test_get_token_usage_filtered_by_agent(temp_db):
    """Test get_token_usage filtered by agent ID."""
    # Create issue and two agents
    issue_id = temp_db.create_issue("Test issue")
    agent1_id = temp_db.create_agent("agent-1")
    agent2_id = temp_db.create_agent("agent-2")

    # Add token usage for both agents
    temp_db.log_event(issue_id, agent1_id, "tokens_used", {"input_tokens": 1000, "output_tokens": 500})
    temp_db.log_event(issue_id, agent2_id, "tokens_used", {"input_tokens": 2000, "output_tokens": 1500})

    # Get usage for agent1 only
    usage = temp_db.get_token_usage(agent_id=agent1_id)

    assert usage["total_tokens"] == 1500
    assert usage["total_input_tokens"] == 1000
    assert usage["total_output_tokens"] == 500

    # Should only have agent1 in breakdown
    assert len(usage["agent_breakdown"]) == 1
    assert agent1_id in usage["agent_breakdown"]
    assert agent2_id not in usage["agent_breakdown"]


def test_get_token_usage_with_invalid_json(temp_db):
    """Test get_token_usage with malformed JSON in events."""
    issue_id = temp_db.create_issue("Test issue")
    agent_id = temp_db.create_agent("test-agent")

    # Add valid event
    temp_db.log_event(issue_id, agent_id, "tokens_used", {"input_tokens": 1000, "output_tokens": 500})

    # Add invalid JSON event directly to database
    temp_db.conn.execute(
        "INSERT INTO events (issue_id, agent_id, event_type, detail) VALUES (?, ?, ?, ?)", (issue_id, agent_id, "tokens_used", "invalid json")
    )
    temp_db.conn.commit()

    # Should still work, ignoring the invalid JSON
    usage = temp_db.get_token_usage()

    assert usage["total_tokens"] == 1500
    assert usage["total_input_tokens"] == 1000
    assert usage["total_output_tokens"] == 500


# --- Notes system tests ---


def test_add_note_returns_integer_id(temp_db):
    """Test add_note returns an integer ID."""
    issue_id = temp_db.create_issue("Test issue")
    agent_id = temp_db.create_agent("test-agent")

    note_id = temp_db.add_note(issue_id=issue_id, agent_id=agent_id, content="Test note", category="discovery")

    assert isinstance(note_id, int)
    assert note_id > 0


def test_add_note_with_defaults(temp_db):
    """Test add_note with default parameters."""
    note_id = temp_db.add_note(content="Project-wide note")

    assert isinstance(note_id, int)

    # Verify the note was saved with defaults
    notes = temp_db.get_notes(limit=1)
    assert len(notes) == 1
    note = notes[0]
    assert note["id"] == note_id
    assert note["issue_id"] is None
    assert note["agent_id"] is None
    assert note["category"] == "discovery"
    assert note["content"] == "Project-wide note"
    assert note["created_at"] is not None


def test_add_note_all_categories(temp_db):
    """Test add_note works with all expected categories."""
    categories = ["discovery", "gotcha", "dependency", "pattern", "context"]

    for category in categories:
        note_id = temp_db.add_note(content=f"Test {category} note", category=category)
        assert isinstance(note_id, int)

        # Verify category was saved correctly
        notes = temp_db.get_notes(category=category, limit=1)
        assert len(notes) == 1
        assert notes[0]["category"] == category


def test_get_notes_no_filters_newest_first(temp_db):
    """Test get_notes with no filters returns notes ordered by newest first."""
    issue_id = temp_db.create_issue("Test issue")
    agent_id = temp_db.create_agent("test-agent")

    # Create notes in order
    note1_id = temp_db.add_note(issue_id=issue_id, agent_id=agent_id, content="First note")
    note2_id = temp_db.add_note(issue_id=issue_id, agent_id=agent_id, content="Second note")
    note3_id = temp_db.add_note(issue_id=issue_id, agent_id=agent_id, content="Third note")

    notes = temp_db.get_notes()

    # Should be ordered newest first
    assert len(notes) == 3
    assert notes[0]["id"] == note3_id  # Newest
    assert notes[1]["id"] == note2_id
    assert notes[2]["id"] == note1_id  # Oldest
    assert notes[0]["content"] == "Third note"


def test_get_notes_filter_by_issue_id(temp_db):
    """Test get_notes filters correctly by issue_id."""
    issue1_id = temp_db.create_issue("Issue 1")
    issue2_id = temp_db.create_issue("Issue 2")
    agent_id = temp_db.create_agent("test-agent")

    # Create notes for different issues
    note1_id = temp_db.add_note(issue_id=issue1_id, agent_id=agent_id, content="Note for issue 1")
    note2_id = temp_db.add_note(issue_id=issue2_id, agent_id=agent_id, content="Note for issue 2")
    note3_id = temp_db.add_note(issue_id=issue1_id, agent_id=agent_id, content="Another note for issue 1")

    # Filter by issue1
    notes = temp_db.get_notes(issue_id=issue1_id)

    assert len(notes) == 2
    note_ids = [note["id"] for note in notes]
    assert note1_id in note_ids
    assert note3_id in note_ids
    assert note2_id not in note_ids

    # Verify content
    contents = [note["content"] for note in notes]
    assert "Note for issue 1" in contents
    assert "Another note for issue 1" in contents
    assert "Note for issue 2" not in contents


def test_get_notes_filter_by_category(temp_db):
    """Test get_notes filters correctly by category."""
    issue_id = temp_db.create_issue("Test issue")
    agent_id = temp_db.create_agent("test-agent")

    # Create notes with different categories
    discovery_id = temp_db.add_note(issue_id=issue_id, agent_id=agent_id, content="Discovery note", category="discovery")
    gotcha_id = temp_db.add_note(issue_id=issue_id, agent_id=agent_id, content="Gotcha note", category="gotcha")
    pattern_id = temp_db.add_note(issue_id=issue_id, agent_id=agent_id, content="Pattern note", category="pattern")

    # Filter by gotcha category
    notes = temp_db.get_notes(category="gotcha")

    assert len(notes) == 1
    assert notes[0]["id"] == gotcha_id
    assert notes[0]["category"] == "gotcha"
    assert notes[0]["content"] == "Gotcha note"

    # Filter by discovery category
    discovery_notes = temp_db.get_notes(category="discovery")
    assert len(discovery_notes) == 1
    assert discovery_notes[0]["id"] == discovery_id


def test_get_notes_limit_parameter(temp_db):
    """Test get_notes respects the limit parameter."""
    issue_id = temp_db.create_issue("Test issue")
    agent_id = temp_db.create_agent("test-agent")

    # Create 5 notes
    for i in range(5):
        temp_db.add_note(issue_id=issue_id, agent_id=agent_id, content=f"Note {i}")

    # Test limit
    notes = temp_db.get_notes(limit=3)
    assert len(notes) == 3

    # Test default limit (should be 20, but we only have 5 notes)
    all_notes = temp_db.get_notes()
    assert len(all_notes) == 5


def test_get_notes_for_molecule(temp_db):
    """Test get_notes_for_molecule returns notes from child issues."""
    # Create a parent molecule issue
    parent_id = temp_db.create_issue("Parent molecule", issue_type="molecule")

    # Create child issues
    child1_id = temp_db.create_issue("Child 1", parent_id=parent_id, issue_type="step")
    child2_id = temp_db.create_issue("Child 2", parent_id=parent_id, issue_type="step")
    child3_id = temp_db.create_issue("Child 3", parent_id=parent_id, issue_type="step")

    # Create an unrelated issue
    unrelated_id = temp_db.create_issue("Unrelated issue")

    agent_id = temp_db.create_agent("test-agent")

    # Create notes for child issues (older first to test ordering)
    note1_id = temp_db.add_note(issue_id=child1_id, agent_id=agent_id, content="Note from child 1")
    note2_id = temp_db.add_note(issue_id=child2_id, agent_id=agent_id, content="Note from child 2")
    note3_id = temp_db.add_note(issue_id=child3_id, agent_id=agent_id, content="Note from child 3")

    # Create note for unrelated issue (should not be included)
    unrelated_note_id = temp_db.add_note(issue_id=unrelated_id, agent_id=agent_id, content="Unrelated note")

    # Get notes for the molecule
    notes = temp_db.get_notes_for_molecule(parent_id)

    assert len(notes) == 3
    note_ids = [note["id"] for note in notes]
    assert note1_id in note_ids
    assert note2_id in note_ids
    assert note3_id in note_ids
    assert unrelated_note_id not in note_ids

    # Should be ordered by creation time (oldest first)
    assert notes[0]["id"] == note1_id  # First created
    assert notes[1]["id"] == note2_id
    assert notes[2]["id"] == note3_id  # Last created

    # Verify content
    contents = [note["content"] for note in notes]
    assert "Note from child 1" in contents
    assert "Note from child 2" in contents
    assert "Note from child 3" in contents


def test_get_notes_for_molecule_empty(temp_db):
    """Test get_notes_for_molecule returns empty list for molecule with no child notes."""
    parent_id = temp_db.create_issue("Parent molecule", issue_type="molecule")

    notes = temp_db.get_notes_for_molecule(parent_id)
    assert notes == []


def test_get_notes_for_molecule_nonexistent_parent(temp_db):
    """Test get_notes_for_molecule with non-existent parent returns empty list."""
    notes = temp_db.get_notes_for_molecule("nonexistent-parent-id")
    assert notes == []


def test_get_recent_project_notes(temp_db):
    """Test get_recent_project_notes returns mixed notes newest first."""
    issue1_id = temp_db.create_issue("Issue 1")
    issue2_id = temp_db.create_issue("Issue 2")
    agent_id = temp_db.create_agent("test-agent")

    # Create various notes
    note1_id = temp_db.add_note(issue_id=issue1_id, agent_id=agent_id, content="Issue-specific note 1")
    note2_id = temp_db.add_note(content="Project-wide note 1")  # No issue_id
    note3_id = temp_db.add_note(issue_id=issue2_id, agent_id=agent_id, content="Issue-specific note 2")
    note4_id = temp_db.add_note(content="Project-wide note 2")  # No issue_id

    notes = temp_db.get_recent_project_notes()

    assert len(notes) == 4

    # Should be ordered newest first
    assert notes[0]["id"] == note4_id  # Last created
    assert notes[1]["id"] == note3_id
    assert notes[2]["id"] == note2_id
    assert notes[3]["id"] == note1_id  # First created

    # Verify mix of project-wide and issue-specific notes
    issue_ids = [note["issue_id"] for note in notes]
    assert None in issue_ids  # Project-wide notes
    assert issue1_id in issue_ids  # Issue-specific notes
    assert issue2_id in issue_ids


def test_get_recent_project_notes_limit(temp_db):
    """Test get_recent_project_notes respects limit parameter."""
    agent_id = temp_db.create_agent("test-agent")

    # Create more notes than the limit
    for i in range(15):
        temp_db.add_note(content=f"Note {i}", agent_id=agent_id)

    # Test custom limit
    notes = temp_db.get_recent_project_notes(limit=5)
    assert len(notes) == 5

    # Test default limit
    default_notes = temp_db.get_recent_project_notes()
    assert len(default_notes) == 10  # Default limit is 10

    # Should be newest first
    assert default_notes[0]["content"] == "Note 14"  # Last created (newest)
    assert default_notes[9]["content"] == "Note 5"  # 10th newest


def test_get_recent_project_notes_empty(temp_db):
    """Test get_recent_project_notes returns empty list when no notes exist."""
    notes = temp_db.get_recent_project_notes()
    assert notes == []


def test_notes_database_not_connected_error(temp_db):
    """Test that notes methods raise error when database not connected."""
    # Close the connection
    temp_db.close()

    # All methods should raise RuntimeError
    with pytest.raises(RuntimeError, match="Database not connected"):
        temp_db.add_note(content="Test note")

    with pytest.raises(RuntimeError, match="Database not connected"):
        temp_db.get_notes()

    with pytest.raises(RuntimeError, match="Database not connected"):
        temp_db.get_notes_for_molecule("test-parent")

    with pytest.raises(RuntimeError, match="Database not connected"):
        temp_db.get_recent_project_notes()
