"""Tests for database operations."""

import json
import sqlite3

import pytest


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
    db.try_transition_issue_status(issues["issue1"], to_status="done")

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
    temp_db.try_transition_issue_status(blocker_id, to_status="finalized")

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


def test_create_issue_with_depends_on(temp_db):
    """Test that create_issue wires deps atomically in the same transaction."""
    blocker_id = temp_db.create_issue("Blocker")
    issue_id = temp_db.create_issue("Blocked issue", depends_on=[blocker_id])
    agent_id = temp_db.create_agent("test-agent")

    # Issue should not appear in ready queue (blocker is open)
    ready = temp_db.get_ready_queue()
    ready_ids = [r["id"] for r in ready]
    assert issue_id not in ready_ids

    # Claim should fail
    assert not temp_db.claim_issue(issue_id, agent_id)

    # Resolve blocker → now it should be claimable
    temp_db.try_transition_issue_status(blocker_id, to_status="finalized")
    ready = temp_db.get_ready_queue()
    ready_ids = [r["id"] for r in ready]
    assert issue_id in ready_ids
    assert temp_db.claim_issue(issue_id, agent_id)


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


def test_unconditional_status_transition(temp_db):
    """Unconditional try_transition_issue_status (no from_status) updates regardless of current status."""
    issue_id = temp_db.create_issue("Test issue")

    result = temp_db.try_transition_issue_status(issue_id, to_status="done")

    assert result is True
    issue = temp_db.get_issue(issue_id)
    assert issue["status"] == "done"
    assert issue["closed_at"] is not None

    # Verify event was logged
    events = temp_db.get_events(issue_id=issue_id)
    event_types = [e["event_type"] for e in events]
    assert "status_done" in event_types


def test_try_transition_issue_status_cas(temp_db):
    """CAS transition succeeds when from_status matches; fails when it doesn't."""
    issue_id = temp_db.create_issue("CAS test")

    # Wrong from_status → no update
    result = temp_db.try_transition_issue_status(issue_id, from_status="done", to_status="canceled")
    assert result is False
    assert temp_db.get_issue(issue_id)["status"] == "open"

    # Correct from_status → succeeds
    result = temp_db.try_transition_issue_status(issue_id, from_status="open", to_status="done")
    assert result is True
    assert temp_db.get_issue(issue_id)["status"] == "done"


def test_try_transition_issue_status_clears_assignee_on_open(temp_db):
    """Transitioning to 'open' always clears the assignee (INV-2), unconditionally."""
    issue_id = temp_db.create_issue("Assignee clear test")
    # Set an assignee directly
    temp_db.conn.execute("UPDATE issues SET assignee = 'agent-x', status = 'in_progress' WHERE id = ?", (issue_id,))
    temp_db.conn.commit()

    temp_db.try_transition_issue_status(issue_id, to_status="open")

    issue = temp_db.get_issue(issue_id)
    assert issue["status"] == "open"
    assert issue["assignee"] is None


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
    db.try_transition_issue_status(id1, to_status="done")
    db.try_transition_issue_status(id2, to_status="done")
    db.try_transition_issue_status(id3, to_status="done")

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


def test_list_merge_entries_queued_filter(db_with_merge_queue):
    """list_merge_entries(status='queued') returns only queued rows with joined fields."""
    db, agent_id, issue_ids = db_with_merge_queue

    merges = db.list_merge_entries("test", status="queued")
    assert len(merges) == 2  # Only 'queued', not 'merged'

    # Should have joined fields
    titles = {m["issue_title"] for m in merges}
    assert "Feature A" in titles
    assert "Feature C" not in titles  # 'merged' status excluded


def test_list_merge_entries_ascending_order(db_with_merge_queue):
    """ascending=True returns oldest-first (FIFO for merge processing)."""
    db, _, issue_ids = db_with_merge_queue

    merges = db.list_merge_entries("test", status="queued", ascending=True)
    assert len(merges) == 2
    assert merges[0]["issue_id"] == issue_ids[0]
    assert merges[1]["issue_id"] == issue_ids[1]


def test_list_merge_entries_limit(db_with_merge_queue):
    """limit parameter restricts result count."""
    db, _, _ = db_with_merge_queue

    merges = db.list_merge_entries("test", status="queued", limit=1)
    assert len(merges) == 1


def test_list_merge_entries_empty(temp_db):
    """list_merge_entries returns empty list when queue is empty."""
    temp_db.create_issue("dummy", project="test")  # project must exist for i.project join
    merges = temp_db.list_merge_entries("test", status="queued")
    assert merges == []


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


def test_count_events(temp_db):
    """Test counting events by type for an issue."""
    issue_id = temp_db.create_issue("Test issue")
    agent_id = temp_db.create_agent("test-agent")

    # Initially no events
    assert temp_db.count_events(issue_id, "retry") == 0
    assert temp_db.count_events(issue_id, "agent_switch") == 0

    # Log some retry events
    temp_db.log_event(issue_id, agent_id, "retry", {"attempt": 1})
    temp_db.log_event(issue_id, agent_id, "retry", {"attempt": 2})
    temp_db.log_event(issue_id, agent_id, "agent_switch", {"switch": 1})

    # Count should be correct
    assert temp_db.count_events(issue_id, "retry") == 2
    assert temp_db.count_events(issue_id, "agent_switch") == 1
    assert temp_db.count_events(issue_id, "escalated") == 0


def test_count_events_nonexistent_issue(temp_db):
    """Test counting events for non-existent issue returns 0."""
    assert temp_db.count_events("nonexistent", "retry") == 0


def test_count_events_since_reset_no_reset(temp_db):
    """Without a retry_reset event, since_reset=True behaves same as no filter."""
    issue_id = temp_db.create_issue("Test issue")
    agent_id = temp_db.create_agent("test-agent")

    temp_db.log_event(issue_id, agent_id, "retry", {"attempt": 1})
    temp_db.log_event(issue_id, agent_id, "retry", {"attempt": 2})

    assert temp_db.count_events(issue_id, "retry", since_reset=True) == 2
    assert temp_db.count_events(issue_id, "retry") == 2


def test_count_events_since_reset_after_reset(temp_db):
    """After a retry_reset, since_reset=True only counts events after the reset."""
    issue_id = temp_db.create_issue("Test issue")
    agent_id = temp_db.create_agent("test-agent")

    # Log events before reset
    temp_db.log_event(issue_id, agent_id, "retry", {"attempt": 1})
    temp_db.log_event(issue_id, agent_id, "retry", {"attempt": 2})
    temp_db.log_event(issue_id, agent_id, "agent_switch", {"switch": 1})

    # Log reset event
    temp_db.log_event(issue_id, None, "retry_reset", {"notes": "fixed"})

    # Log events after reset
    temp_db.log_event(issue_id, agent_id, "retry", {"attempt": 3})

    # Since-reset should only count post-reset events
    assert temp_db.count_events(issue_id, "retry", since_reset=True) == 1
    assert temp_db.count_events(issue_id, "agent_switch", since_reset=True) == 0

    # Total count still includes all events
    assert temp_db.count_events(issue_id, "retry") == 3
    assert temp_db.count_events(issue_id, "agent_switch") == 1


def test_count_events_since_reset_returns_zero_immediately_after_reset(temp_db):
    """Immediately after a reset, since_reset=True count should be 0."""
    issue_id = temp_db.create_issue("Test issue")
    agent_id = temp_db.create_agent("test-agent")

    temp_db.log_event(issue_id, agent_id, "retry", {"attempt": 1})
    temp_db.log_event(issue_id, agent_id, "retry", {"attempt": 2})
    temp_db.log_event(issue_id, None, "retry_reset", {"notes": "reset"})

    assert temp_db.count_events(issue_id, "retry", since_reset=True) == 0


def test_log_system_event(temp_db):
    """Test logging system-level events."""
    temp_db.log_system_event("daemon_started", {"reason": "Connection timeout"})

    # Get all events
    events = temp_db.get_events()

    # Find the system event
    system_events = [e for e in events if e["event_type"] == "daemon_started"]
    assert len(system_events) == 1

    event = system_events[0]
    assert event["issue_id"] is None
    assert event["agent_id"] is None
    assert event["event_type"] == "daemon_started"

    detail = json.loads(event["detail"])
    assert detail == {"reason": "Connection timeout"}


# --- Capability scoring tests ---


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
    temp_db.add_note(issue_id=issue_id, agent_id=agent_id, content="Pattern note", category="pattern")

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


def test_get_recent_project_notes(temp_db):
    """Test get_notes returns mixed notes newest first (was get_recent_project_notes)."""
    issue1_id = temp_db.create_issue("Issue 1")
    issue2_id = temp_db.create_issue("Issue 2")
    agent_id = temp_db.create_agent("test-agent")

    # Create various notes
    note1_id = temp_db.add_note(issue_id=issue1_id, agent_id=agent_id, content="Issue-specific note 1")
    note2_id = temp_db.add_note(content="Project-wide note 1")  # No issue_id
    note3_id = temp_db.add_note(issue_id=issue2_id, agent_id=agent_id, content="Issue-specific note 2")
    note4_id = temp_db.add_note(content="Project-wide note 2")  # No issue_id

    notes = temp_db.get_notes()

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
    """Test get_notes respects limit parameter (was get_recent_project_notes)."""
    agent_id = temp_db.create_agent("test-agent")

    # Create more notes than the limit
    for i in range(15):
        temp_db.add_note(content=f"Note {i}", agent_id=agent_id)

    # Test custom limit
    notes = temp_db.get_notes(limit=5)
    assert len(notes) == 5

    # Test default limit (get_notes default is 20, not 10)
    default_notes = temp_db.get_notes(limit=10)
    assert len(default_notes) == 10

    # Should be newest first
    assert default_notes[0]["content"] == "Note 14"  # Last created (newest)
    assert default_notes[9]["content"] == "Note 5"  # 10th newest


def test_get_recent_project_notes_empty(temp_db):
    """Test get_notes returns empty list when no notes exist (was get_recent_project_notes)."""
    notes = temp_db.get_notes()
    assert notes == []


def test_create_event_with_nonexistent_agent_id():
    """Test creating an event with non-existent agent_id succeeds on fresh DB."""
    import tempfile
    import os
    from hive.db import Database

    # Create completely fresh DB to ensure we get updated schema
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    db = Database(db_path)
    db.connect()

    try:
        issue_id = db.create_issue("Test issue")
        nonexistent_agent_id = "agent-nonexistent-12345"

        # This should succeed (no FK constraint on agent_id in fresh schema)
        db.log_event(issue_id, nonexistent_agent_id, "test_event", {"detail": "test"})

        # Verify the event was created
        events = db.get_events(agent_id=nonexistent_agent_id)
        assert len(events) == 1
        assert events[0]["agent_id"] == nonexistent_agent_id
        assert events[0]["event_type"] == "test_event"
    finally:
        db.close()
        os.unlink(db_path)


def test_create_note_with_nonexistent_agent_id():
    """Test creating a note with non-existent agent_id succeeds on fresh DB."""
    import tempfile
    import os
    from hive.db import Database

    # Create completely fresh DB to ensure we get updated schema
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    db = Database(db_path)
    db.connect()

    try:
        issue_id = db.create_issue("Test issue")
        nonexistent_agent_id = "agent-nonexistent-67890"

        # This should succeed (no FK constraint on agent_id in fresh schema)
        note_id = db.add_note(issue_id=issue_id, agent_id=nonexistent_agent_id, content="Test note with phantom agent")

        assert isinstance(note_id, int)

        # Verify the note was created
        notes = db.get_notes(issue_id=issue_id)
        assert len(notes) == 1
        assert notes[0]["agent_id"] == nonexistent_agent_id
        assert notes[0]["content"] == "Test note with phantom agent"
    finally:
        db.close()
        os.unlink(db_path)


def test_notes_database_not_connected_error(temp_db):
    """Test that notes methods raise error when database not connected."""
    # Close the connection
    temp_db.close()

    # All methods should raise RuntimeError
    with pytest.raises(RuntimeError, match="Database not connected"):
        temp_db.add_note(content="Test note")

    with pytest.raises(RuntimeError, match="Database not connected"):
        temp_db.get_notes()


# --- Model performance tests ---


@pytest.fixture
def db_with_model_events(temp_db):
    """Create a DB with issues that have model set for performance testing."""
    db = temp_db

    # Issue 1: completed with sonnet, tagged python+bugfix
    id1 = db.create_issue(title="Fix login", project="test", issue_type="bug", model="claude-sonnet-4-5-20250929", tags=["python", "bugfix"])
    db.log_event(id1, None, "tokens_used", {"input_tokens": 1000, "output_tokens": 500, "model": "claude-sonnet-4-5-20250929"})
    db.conn.execute("UPDATE issues SET status = 'done' WHERE id = ?", (id1,))

    # Issue 2: escalated with opus, tagged javascript+feature
    id2 = db.create_issue(title="Add dashboard", project="test", issue_type="feature", model="claude-opus-4-6", tags=["javascript", "feature"])
    db.log_event(id2, None, "tokens_used", {"input_tokens": 2000, "output_tokens": 800, "model": "claude-opus-4-6"})
    db.conn.execute("UPDATE issues SET status = 'escalated' WHERE id = ?", (id2,))

    # Issue 3: completed with sonnet, tagged javascript
    id3 = db.create_issue(title="Fix button", project="test", issue_type="bug", model="claude-sonnet-4-5-20250929", tags=["javascript"])
    db.conn.execute("UPDATE issues SET status = 'done' WHERE id = ?", (id3,))

    # Issue 4: no tags
    id4 = db.create_issue(title="Misc task", project="test", issue_type="task", model="claude-opus-4-6")
    db.conn.execute("UPDATE issues SET status = 'done' WHERE id = ?", (id4,))

    db.conn.commit()
    return db


def test_model_performance_reads_model_from_issue(db_with_model_events):
    """Model should come from issues.model column."""
    results = db_with_model_events.get_model_performance(group_by="type")
    models = {r["model"] for r in results}
    assert "claude-sonnet-4-5-20250929" in models
    assert "claude-opus-4-6" in models
    assert "unknown" not in models


def test_model_performance_group_by_tag(db_with_model_events):
    """Default grouping should be by tag, with json_each explosion."""
    results = db_with_model_events.get_model_performance(group_by="tag")
    tags = {r["tag"] for r in results}
    assert "python" in tags
    assert "javascript" in tags
    assert "bugfix" in tags
    assert "feature" in tags
    assert "untagged" in tags


def test_model_performance_group_by_type(db_with_model_events):
    """group_by='type' should group by issue type."""
    results = db_with_model_events.get_model_performance(group_by="type")
    types = {r["type"] for r in results}
    assert "bug" in types
    assert "feature" in types


def test_model_performance_filter_by_model(db_with_model_events):
    """Filtering by model should only return matching rows."""
    results = db_with_model_events.get_model_performance(model="claude-opus-4-6", group_by="type")
    assert all(r["model"] == "claude-opus-4-6" for r in results)
    assert len(results) >= 1


def test_model_performance_filter_by_tag(db_with_model_events):
    """Filtering by tag should only return issues containing that tag."""
    results = db_with_model_events.get_model_performance(tag="python", group_by="tag")
    assert len(results) >= 1
    # The python-tagged issue was completed by sonnet
    sonnet_python = [r for r in results if r["tag"] == "python" and r["model"] == "claude-sonnet-4-5-20250929"]
    assert len(sonnet_python) == 1
    assert sonnet_python[0]["successes"] >= 1


def test_model_performance_empty(temp_db):
    """Should return empty list when no issues exist."""
    results = temp_db.get_model_performance()
    assert results == []


# --- Project column migration tests ---


def test_project_column_migration_fresh_db():
    """Test that fresh DB has project columns in notes and agents tables."""
    import tempfile
    import os
    from hive.db import Database

    # Create completely fresh DB
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    db = Database(db_path)
    db.connect()

    try:
        # Check that notes table has project column
        cursor = db.conn.execute("PRAGMA table_info(notes)")
        notes_columns = [row[1] for row in cursor.fetchall()]
        assert "project" in notes_columns

        # Check that agents table has project column
        cursor = db.conn.execute("PRAGMA table_info(agents)")
        agents_columns = [row[1] for row in cursor.fetchall()]
        assert "project" in agents_columns

        # Check that indexes exist
        cursor = db.conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_notes_project'")
        assert cursor.fetchone() is not None

        cursor = db.conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_agents_project'")
        assert cursor.fetchone() is not None
    finally:
        db.close()
        os.unlink(db_path)


def test_merge_queue_idempotency_migration_dedupes_existing_dupes(tmp_path):
    """Migration should dedupe active merge_queue entries before adding unique fence."""
    import sqlite3

    from hive.db import Database, SCHEMA

    db_path = str(tmp_path / "legacy-dupes.db")

    # Create a "legacy" DB by applying schema (no unique active-merge fence),
    # then inserting duplicate queued entries for the same issue.
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)

    issue_id = "w-dup-merge"
    conn.execute(
        "INSERT INTO issues (id, title, status, project) VALUES (?, ?, ?, ?)",
        (issue_id, "Duplicate merge issue", "done", "test"),
    )
    conn.execute(
        "INSERT INTO merge_queue (issue_id, project, worktree, branch_name, status) VALUES (?, ?, ?, ?, 'queued')",
        (issue_id, "test", "/tmp/wt1", "agent/worker-1"),
    )
    conn.execute(
        "INSERT INTO merge_queue (issue_id, project, worktree, branch_name, status) VALUES (?, ?, ?, ?, 'queued')",
        (issue_id, "test", "/tmp/wt2", "agent/worker-2"),
    )
    conn.commit()
    conn.close()

    db = Database(db_path)
    db.connect()
    try:
        # Unique active-merge index should exist after migration.
        cursor = db.conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='uidx_merge_queue_active_issue'")
        assert cursor.fetchone() is not None

        # Only one active (queued|running) entry should remain for the issue.
        rows = db.conn.execute("SELECT status FROM merge_queue WHERE issue_id = ? ORDER BY id", (issue_id,)).fetchall()
        statuses = [row["status"] for row in rows]
        assert sum(1 for s in statuses if s in ("queued", "running")) == 1
        assert "failed" in statuses
    finally:
        db.close()


def test_project_column_migration_idempotent():
    """Test that migration is idempotent (can be run multiple times)."""
    import tempfile
    import os
    from hive.db import Database

    # Create completely fresh DB
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    db = Database(db_path)
    db.connect()

    try:
        # Run migration once (already happened in connect())
        # Check columns exist
        cursor = db.conn.execute("PRAGMA table_info(notes)")
        notes_columns = [row[1] for row in cursor.fetchall()]
        assert "project" in notes_columns

        cursor = db.conn.execute("PRAGMA table_info(agents)")
        agents_columns = [row[1] for row in cursor.fetchall()]
        assert "project" in agents_columns

        # Run migration again (should be no-op)
        db._migrate_if_needed()

        # Columns should still exist (no error)
        cursor = db.conn.execute("PRAGMA table_info(notes)")
        notes_columns = [row[1] for row in cursor.fetchall()]
        assert "project" in notes_columns

        cursor = db.conn.execute("PRAGMA table_info(agents)")
        agents_columns = [row[1] for row in cursor.fetchall()]
        assert "project" in agents_columns
    finally:
        db.close()
        os.unlink(db_path)


def test_project_column_backfill():
    """Test that notes.project is backfilled from issues.project via FK."""
    import tempfile
    import os
    from hive.db import Database

    # Create completely fresh DB
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    db = Database(db_path)
    db.connect()

    try:
        # Create an issue with a project
        issue_id = db.create_issue(title="Test issue", project="test-project")
        agent_id = db.create_agent("test-agent")

        # Add a note for this issue
        note_id = db.add_note(issue_id=issue_id, agent_id=agent_id, content="Test note")

        # Manually clear the project column to simulate old data
        db.conn.execute("UPDATE notes SET project = NULL WHERE id = ?", (note_id,))
        db.conn.commit()

        # Verify project is NULL
        cursor = db.conn.execute("SELECT project FROM notes WHERE id = ?", (note_id,))
        assert cursor.fetchone()["project"] is None

        # Run migration (backfill should happen)
        db._migrate_if_needed()

        # Verify project was backfilled
        cursor = db.conn.execute("SELECT project FROM notes WHERE id = ?", (note_id,))
        backfilled_project = cursor.fetchone()["project"]
        assert backfilled_project == "test-project"
    finally:
        db.close()
        os.unlink(db_path)


def test_project_column_backfill_null_issue_id():
    """Test that notes with NULL issue_id keep NULL project (project-wide notes)."""
    import tempfile
    import os
    from hive.db import Database

    # Create completely fresh DB
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    db = Database(db_path)
    db.connect()

    try:
        # Add a project-wide note (no issue_id)
        note_id = db.add_note(content="Project-wide note")

        # Run migration (should not crash on NULL issue_id)
        db._migrate_if_needed()

        # Verify project remains NULL for project-wide notes
        cursor = db.conn.execute("SELECT project FROM notes WHERE id = ?", (note_id,))
        note_project = cursor.fetchone()["project"]
        assert note_project is None
    finally:
        db.close()
        os.unlink(db_path)


# --- Project filtering tests ---


@pytest.fixture
def db_with_projects(temp_db):
    """Create a DB with issues, agents, and notes for multiple projects."""
    db = temp_db

    # Create issues for project alpha
    alpha_issue1 = db.create_issue("Alpha Issue 1", project="alpha")
    alpha_issue2 = db.create_issue("Alpha Issue 2", project="alpha", priority=1)

    # Create issues for project beta
    beta_issue1 = db.create_issue("Beta Issue 1", project="beta")
    beta_issue2 = db.create_issue("Beta Issue 2", project="beta")

    # Create agent for alpha
    alpha_agent = db.create_agent("alpha-agent", project="alpha")

    # Create agent for beta
    beta_agent = db.create_agent("beta-agent", project="beta")

    # Create notes for alpha
    db.add_note(issue_id=alpha_issue1, content="Alpha note 1", project="alpha")
    db.add_note(issue_id=alpha_issue2, content="Alpha note 2", project="alpha")

    # Create notes for beta
    db.add_note(issue_id=beta_issue1, content="Beta note 1", project="beta")

    # Create NULL-project note (should match any project query)
    db.add_note(content="Global note", project=None)

    # Add merge queue entries
    db.try_transition_issue_status(alpha_issue1, to_status="done")
    db.try_transition_issue_status(beta_issue1, to_status="done")
    db.conn.execute(
        "INSERT INTO merge_queue (issue_id, agent_id, project, worktree, branch_name) VALUES (?, ?, ?, ?, ?)",
        (alpha_issue1, alpha_agent, "alpha", "/tmp/alpha-wt", "agent/alpha-1"),
    )
    db.conn.execute(
        "INSERT INTO merge_queue (issue_id, agent_id, project, worktree, branch_name) VALUES (?, ?, ?, ?, ?)",
        (beta_issue1, beta_agent, "beta", "/tmp/beta-wt", "agent/beta-1"),
    )
    db.conn.commit()

    # Add token usage events
    db.log_event(alpha_issue1, alpha_agent, "tokens_used", {"input_tokens": 1000, "output_tokens": 500, "model": "claude-sonnet-4-5-20250929"})
    db.log_event(beta_issue1, beta_agent, "tokens_used", {"input_tokens": 2000, "output_tokens": 1000, "model": "claude-sonnet-4-5-20250929"})

    # Update agents to working status
    db.conn.execute("UPDATE agents SET status = 'working' WHERE id IN (?, ?)", (alpha_agent, beta_agent))
    db.conn.commit()

    return db, {
        "alpha_issues": [alpha_issue1, alpha_issue2],
        "beta_issues": [beta_issue1, beta_issue2],
        "alpha_agent": alpha_agent,
        "beta_agent": beta_agent,
    }


def test_get_ready_queue_filter_by_project(db_with_projects):
    """Test get_ready_queue filters by project."""
    db, ids = db_with_projects

    # Get alpha issues
    alpha_ready = db.get_ready_queue(project="alpha")
    alpha_ids = [item["id"] for item in alpha_ready]

    # Should only have alpha issues
    assert ids["alpha_issues"][1] in alpha_ids  # alpha_issue2 (alpha_issue1 is done)
    assert len([i for i in alpha_ids if i in ids["beta_issues"]]) == 0

    # Get beta issues
    beta_ready = db.get_ready_queue(project="beta")
    beta_ids = [item["id"] for item in beta_ready]

    # Should only have beta issues
    assert ids["beta_issues"][1] in beta_ids  # beta_issue2 (beta_issue1 is done)
    assert len([i for i in beta_ids if i in ids["alpha_issues"]]) == 0


def test_list_merge_entries_filter_by_project(db_with_projects):
    """list_merge_entries filters entries by project."""
    db, ids = db_with_projects

    alpha_merges = db.list_merge_entries("alpha", status="queued")
    assert len(alpha_merges) == 1
    assert alpha_merges[0]["issue_id"] == ids["alpha_issues"][0]

    beta_merges = db.list_merge_entries("beta", status="queued")
    assert len(beta_merges) == 1
    assert beta_merges[0]["issue_id"] == ids["beta_issues"][0]


def test_get_active_agents_filter_by_project(db_with_projects):
    """Test get_active_agents filters by project."""
    db, ids = db_with_projects

    # Get alpha agents
    alpha_agents = db.get_active_agents(project="alpha")
    assert len(alpha_agents) == 1
    assert alpha_agents[0]["id"] == ids["alpha_agent"]

    # Get beta agents
    beta_agents = db.get_active_agents(project="beta")
    assert len(beta_agents) == 1
    assert beta_agents[0]["id"] == ids["beta_agent"]


def test_get_notes_filter_by_project(db_with_projects):
    """Test get_notes filters by project."""
    db, ids = db_with_projects

    # Get alpha notes
    alpha_notes = db.get_notes(project="alpha")
    alpha_contents = [note["content"] for note in alpha_notes]

    # Should have alpha notes + NULL-project note
    assert "Alpha note 1" in alpha_contents
    assert "Alpha note 2" in alpha_contents
    assert "Global note" in alpha_contents  # NULL-project notes match any query
    assert "Beta note 1" not in alpha_contents

    # Get beta notes
    beta_notes = db.get_notes(project="beta")
    beta_contents = [note["content"] for note in beta_notes]

    # Should have beta notes + NULL-project note
    assert "Beta note 1" in beta_contents
    assert "Global note" in beta_contents
    assert "Alpha note 1" not in beta_contents
    assert "Alpha note 2" not in beta_contents


def test_get_recent_project_notes_filter_by_project(db_with_projects):
    """Test get_notes filters by project (was get_recent_project_notes)."""
    db, ids = db_with_projects

    # Get alpha notes
    alpha_notes = db.get_notes(project="alpha")
    alpha_contents = [note["content"] for note in alpha_notes]

    # Should have alpha notes + NULL-project note
    assert "Alpha note 1" in alpha_contents
    assert "Alpha note 2" in alpha_contents
    assert "Global note" in alpha_contents
    assert "Beta note 1" not in alpha_contents


def test_get_token_usage_filter_by_project(db_with_projects):
    """Test get_token_usage filters by project."""
    db, ids = db_with_projects

    # Get alpha tokens
    alpha_usage = db.get_token_usage(project="alpha")
    assert alpha_usage["total_input_tokens"] == 1000
    assert alpha_usage["total_output_tokens"] == 500
    assert alpha_usage["total_tokens"] == 1500

    # Get beta tokens
    beta_usage = db.get_token_usage(project="beta")
    assert beta_usage["total_input_tokens"] == 2000
    assert beta_usage["total_output_tokens"] == 1000
    assert beta_usage["total_tokens"] == 3000


def test_get_metrics_filter_by_project(db_with_projects):
    """Test get_metrics filters by project."""
    db, ids = db_with_projects

    # Add agent runs data
    db.log_event(ids["alpha_issues"][0], ids["alpha_agent"], "worker_started", {})
    db.log_event(ids["alpha_issues"][0], ids["alpha_agent"], "completed", {})
    db.log_event(ids["beta_issues"][0], ids["beta_agent"], "worker_started", {})
    db.log_event(ids["beta_issues"][0], ids["beta_agent"], "completed", {})

    # Get alpha metrics
    alpha_metrics = db.get_metrics(project="alpha")
    assert len(alpha_metrics) >= 1

    # Get beta metrics
    beta_metrics = db.get_metrics(project="beta")
    assert len(beta_metrics) >= 1


def test_add_note_with_project(temp_db):
    """Test add_note stores project."""
    issue_id = temp_db.create_issue("Test issue", project="test-project")
    agent_id = temp_db.create_agent("test-agent")

    note_id = temp_db.add_note(issue_id=issue_id, agent_id=agent_id, content="Test note", project="test-project")

    # Verify project was stored
    cursor = temp_db.conn.execute("SELECT project FROM notes WHERE id = ?", (note_id,))
    note_project = cursor.fetchone()["project"]
    assert note_project == "test-project"


def test_create_agent_with_project(temp_db):
    """Test create_agent stores project."""
    agent_id = temp_db.create_agent("test-agent", project="test-project")

    # Verify project was stored
    agent = temp_db.get_agent(agent_id)
    assert agent["project"] == "test-project"


def test_null_project_notes_match_any_project_query(temp_db):
    """Test that NULL-project notes appear in all project-filtered queries."""
    # Create issues for different projects
    alpha_issue = temp_db.create_issue("Alpha issue", project="alpha")
    beta_issue = temp_db.create_issue("Beta issue", project="beta")

    # Create notes with NULL project
    temp_db.add_note(content="Global note 1", project=None)
    temp_db.add_note(content="Global note 2", project=None)

    # Create project-specific notes
    temp_db.add_note(issue_id=alpha_issue, content="Alpha note", project="alpha")
    temp_db.add_note(issue_id=beta_issue, content="Beta note", project="beta")

    # Query for alpha project - should include NULL-project notes
    alpha_notes = temp_db.get_notes(project="alpha")
    alpha_contents = [note["content"] for note in alpha_notes]
    assert "Global note 1" in alpha_contents
    assert "Global note 2" in alpha_contents
    assert "Alpha note" in alpha_contents
    assert "Beta note" not in alpha_contents

    # Query for beta project - should include NULL-project notes
    beta_notes = temp_db.get_notes(project="beta")
    beta_contents = [note["content"] for note in beta_notes]
    assert "Global note 1" in beta_contents
    assert "Global note 2" in beta_contents
    assert "Beta note" in beta_contents
    assert "Alpha note" not in beta_contents


def test_worker_started_event_includes_prompt_version(temp_db):
    """Test worker_started event includes prompt_version in event detail."""
    import re

    from hive.prompts import get_prompt_version

    # Create an issue
    issue_id = temp_db.create_issue("Test issue", "Test description", project="test-project")

    # Create an agent (required for foreign key constraint)
    agent_id = temp_db.create_agent("test-agent")

    # Directly test the event logging with prompt_version (simulating what happens in orchestrator)
    event_detail = {
        "session_id": "test-session-id",
        "worktree": "/test/worktree",
        "routing_method": "new_agent",
        "prompt_version": get_prompt_version("worker"),
    }

    temp_db.log_event(issue_id, agent_id, "worker_started", event_detail)

    # Get all events for this issue
    events = temp_db.get_events(issue_id)

    # Find the worker_started event
    worker_started_events = [e for e in events if e["event_type"] == "worker_started"]
    assert len(worker_started_events) == 1

    event = worker_started_events[0]

    # Parse the detail JSON if it's a string
    detail = event["detail"]
    if isinstance(detail, str):
        detail = json.loads(detail)

    assert "prompt_version" in detail

    # Verify it's a valid 12-character hex string
    prompt_version = detail["prompt_version"]
    assert isinstance(prompt_version, str)
    assert len(prompt_version) == 12
    assert re.match(r"[0-9a-f]{12}", prompt_version)


# --- Note deliveries tests ---


def test_add_note_must_read_default(temp_db):
    """add_note without must_read -> must_read=0."""
    note_id = temp_db.add_note(content="default note")
    cursor = temp_db.conn.execute("SELECT must_read FROM notes WHERE id = ?", (note_id,))
    assert cursor.fetchone()["must_read"] == 0


def test_add_note_must_read_true(temp_db):
    """add_note with must_read=True -> must_read=1."""
    note_id = temp_db.add_note(content="urgent note", must_read=True)
    cursor = temp_db.conn.execute("SELECT must_read FROM notes WHERE id = ?", (note_id,))
    assert cursor.fetchone()["must_read"] == 1


# ── Project registration tests ─────────────────────────────────────────────


def test_project_register_and_lookup(temp_db):
    """INV-3: get_project_path returns None for unknown projects; returns path after registration."""
    assert temp_db.get_project_path("no-such-project") is None

    temp_db.register_project("my-project", "/home/user/my-project")
    assert temp_db.get_project_path("my-project") == "/home/user/my-project"


def test_project_register_idempotent(temp_db):
    """INV-1: register_project is idempotent — re-registering with a new path updates it, no error."""
    temp_db.register_project("proj", "/original/path")
    temp_db.register_project("proj", "/updated/path")  # should not raise

    assert temp_db.get_project_path("proj") == "/updated/path"


def test_project_list_all(temp_db):
    """INV-2: list_projects returns all registered projects."""
    assert temp_db.list_projects() == []

    temp_db.register_project("alpha", "/src/alpha")
    temp_db.register_project("beta", "/src/beta")

    projects = temp_db.list_projects()
    names = {p["name"] for p in projects}
    assert names == {"alpha", "beta"}

    # Each entry has required fields
    for p in projects:
        assert "name" in p
        assert "path" in p
        assert "registered_at" in p


def test_project_re_register_path_update(temp_db):
    """Critical path: register A→B→list returns both; re-register A with new path; lookup reflects update."""
    temp_db.register_project("project-a", "/path/to/a")
    temp_db.register_project("project-b", "/path/to/b")

    projects = temp_db.list_projects()
    assert len(projects) == 2

    temp_db.register_project("project-a", "/new/path/to/a")
    assert temp_db.get_project_path("project-a") == "/new/path/to/a"
    # Still only 2 rows — no duplicates
    assert len(temp_db.list_projects()) == 2


def test_project_register_empty_name_raises(temp_db):
    """Failure mode: registering with empty name raises ValueError."""
    with pytest.raises(ValueError):
        temp_db.register_project("", "/some/path")


def test_project_register_empty_path_raises(temp_db):
    """Failure mode: registering with empty path raises ValueError."""
    with pytest.raises(ValueError):
        temp_db.register_project("valid-name", "")


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


# --- Write-lock contention tests ---


def test_connect_nonblocking_when_write_locked(tmp_path):
    """CLI connect() must not block when another connection holds a write lock."""
    import time

    from hive.db import Database

    db_path = str(tmp_path / "locked.db")

    # First connection: create schema, then hold a write lock.
    writer = Database(db_path)
    writer.connect()
    writer.conn.execute("BEGIN IMMEDIATE")  # holds RESERVED lock

    # Second connection: simulates a CLI invocation while daemon writes.
    reader = Database(db_path)
    t0 = time.monotonic()
    reader.connect()  # must not block
    elapsed = time.monotonic() - t0

    # Should complete nearly instantly (< 1s), not wait for the 5s busy_timeout.
    assert elapsed < 1.0, f"connect() blocked for {elapsed:.1f}s — should be instant"

    # Reads must still work.
    rows = reader.conn.execute("SELECT COUNT(*) FROM issues").fetchone()
    assert rows[0] == 0

    writer.conn.rollback()
    writer.close()
    reader.close()


def test_register_project_nonblocking_when_write_locked(tmp_path):
    """register_project with busy_timeout=0 must fail immediately, not hang."""
    import time

    from hive.db import Database

    db_path = str(tmp_path / "locked.db")

    writer = Database(db_path)
    writer.connect()
    writer.conn.execute("BEGIN IMMEDIATE")

    reader = Database(db_path)
    reader.connect()

    # Simulate the parser.py pattern: zero timeout around register_project.
    t0 = time.monotonic()
    try:
        reader.conn.execute("PRAGMA busy_timeout = 0")
        reader.register_project("test-proj", "/tmp/test-proj")
    except sqlite3.OperationalError:
        pass
    finally:
        reader.conn.execute("PRAGMA busy_timeout = 5000")
    elapsed = time.monotonic() - t0

    assert elapsed < 1.0, f"register_project blocked for {elapsed:.1f}s"

    writer.conn.rollback()
    writer.close()
    reader.close()


# ── Tests for centralized query methods ──────────────────────────────


def test_get_issue_status_counts(temp_db):
    temp_db.create_issue("A", project="p1")
    temp_db.create_issue("B", project="p1")
    temp_db.create_issue("C", project="p2")
    temp_db.try_transition_issue_status(temp_db.create_issue("D", project="p1"), to_status="done")

    counts = temp_db.get_issue_status_counts(project="p1")
    assert counts["open"] == 2
    assert counts["done"] == 1

    all_counts = temp_db.get_issue_status_counts()
    assert sum(all_counts.values()) == 4


def test_get_running_merge(temp_db):
    iid = temp_db.create_issue("merge me", project="proj")
    assert temp_db.get_running_merge(project="proj") is None

    temp_db.enqueue_merge(issue_id=iid, agent_id=None, project="proj", worktree="/w", branch_name="b")
    # Still queued, not running
    assert temp_db.get_running_merge(project="proj") is None

    temp_db.try_transition_merge_queue_status(1, from_status="queued", to_status="running")
    result = temp_db.get_running_merge(project="proj")
    assert result is not None
    assert result["issue_id"] == iid
    assert result["issue_title"] == "merge me"


def test_get_escalated_issues(temp_db):
    temp_db.create_issue("ok", project="proj")
    i2 = temp_db.create_issue("bad", project="proj")
    temp_db.try_transition_issue_status(i2, to_status="escalated")

    esc = temp_db.get_escalated_issues(project="proj")
    assert len(esc) == 1
    assert esc[0]["id"] == i2
    assert esc[0]["status"] == "escalated"

    # Different project returns empty
    assert temp_db.get_escalated_issues(project="other") == []


def test_list_merge_entries(temp_db):
    i1 = temp_db.create_issue("issue1", project="proj")
    i2 = temp_db.create_issue("issue2", project="proj")
    temp_db.enqueue_merge(issue_id=i1, agent_id=None, project="proj", worktree="/w1", branch_name="b1")
    temp_db.enqueue_merge(issue_id=i2, agent_id=None, project="proj", worktree="/w2", branch_name="b2")

    entries = temp_db.list_merge_entries(project="proj")
    assert len(entries) == 2
    assert {e["issue_title"] for e in entries} == {"issue1", "issue2"}

    # Filter by status
    temp_db.try_transition_merge_queue_status(1, from_status="queued", to_status="running")
    queued = temp_db.list_merge_entries(project="proj", status="queued")
    assert len(queued) == 1


def test_list_agents(temp_db):
    temp_db.create_agent("worker-1", project="proj")
    temp_db.create_agent("worker-2", project="proj")
    temp_db.create_agent("worker-3", project="other")

    agents = temp_db.list_agents(project="proj")
    assert len(agents) == 2

    # Filter by status (all idle by default)
    idle = temp_db.list_agents(project="proj", status="idle")
    assert len(idle) == 2
    working = temp_db.list_agents(project="proj", status="working")
    assert len(working) == 0


def test_list_issues_method(temp_db):
    temp_db.create_issue("A", priority=1, project="proj", issue_type="bug")
    temp_db.create_issue("B", priority=2, project="proj", issue_type="task")
    i3 = temp_db.create_issue("C", priority=3, project="proj", issue_type="task")
    temp_db.try_transition_issue_status(i3, to_status="done")

    # Basic listing
    all_issues = temp_db.list_issues(project="proj")
    assert len(all_issues) == 3

    # Filter by type
    bugs = temp_db.list_issues(project="proj", issue_type="bug")
    assert len(bugs) == 1

    # Exclude statuses
    active = temp_db.list_issues(project="proj", exclude_statuses=("done", "finalized", "canceled"))
    assert len(active) == 2

    # Sort reverse
    rev = temp_db.list_issues(project="proj", sort="priority", reverse=True)
    assert rev[0]["priority"] >= rev[-1]["priority"]


def test_get_review_queue(temp_db):
    i1 = temp_db.create_issue("done1", project="proj")
    i2 = temp_db.create_issue("open1", project="proj")
    temp_db.try_transition_issue_status(i1, to_status="done")

    # Without issue_id — only done issues
    rows = temp_db.get_review_queue(project="proj")
    assert len(rows) == 1
    assert rows[0]["id"] == i1

    # With issue_id — returns that specific issue regardless of status
    rows = temp_db.get_review_queue(project="proj", issue_id=i2)
    assert len(rows) == 1
    assert rows[0]["id"] == i2


def test_get_dependencies_and_dependents(temp_db):
    i1 = temp_db.create_issue("blocker", project="proj")
    i2 = temp_db.create_issue("blocked", project="proj")
    temp_db.add_dependency(i2, i1)

    deps = temp_db.get_dependencies(i2)
    assert len(deps) == 1
    assert deps[0]["id"] == i1

    dependents = temp_db.get_dependents(i1)
    assert len(dependents) == 1
    assert dependents[0]["id"] == i2

    # No deps
    assert temp_db.get_dependencies(i1) == []
    assert temp_db.get_dependents(i2) == []
