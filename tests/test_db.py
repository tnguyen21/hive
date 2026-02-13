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
        "labels",
        "merge_queue",
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
    entry = db.get_merge_queue_entry(queue_id)
    assert entry["status"] == "running"
    assert entry["completed_at"] is None

    # Update to merged with timestamp
    db.update_merge_queue_status(queue_id, "merged", completed_at="2026-02-12 12:00:00")
    entry = db.get_merge_queue_entry(queue_id)
    assert entry["status"] == "merged"
    assert entry["completed_at"] == "2026-02-12 12:00:00"


def test_get_merge_queue_entry(db_with_merge_queue):
    """Test retrieving a single merge queue entry."""
    db, _, _ = db_with_merge_queue

    entry = db.get_merge_queue_entry(1)
    assert entry is not None
    assert entry["branch_name"] == "agent/worker-1"


def test_get_merge_queue_entry_missing(temp_db):
    """Test retrieving non-existent merge queue entry returns None."""
    entry = temp_db.get_merge_queue_entry(9999)
    assert entry is None


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
