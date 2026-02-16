"""Tests for agent_runs view and metrics functionality."""

import json

import pytest


def test_agent_runs_single_completed(temp_db):
    """Test agent_runs view with a single completed run."""
    # Create issue
    issue_id = temp_db.create_issue("Test task", "Description", priority=1, project="test", model="claude-sonnet-4-5-20250929")

    # Create agent
    agent_id = temp_db.create_agent("worker-001", model="claude-sonnet-4-5-20250929")

    # Log worker_started event
    temp_db.log_event(issue_id, agent_id, "worker_started", {"session_id": "sess-123"})

    # Log completed event
    temp_db.log_event(issue_id, agent_id, "completed", {"status": "success"})

    # Query agent_runs view
    cursor = temp_db.conn.execute("SELECT * FROM agent_runs WHERE agent_id = ?", (agent_id,))
    rows = [dict(row) for row in cursor.fetchall()]

    assert len(rows) == 1
    run = rows[0]

    assert run["agent_id"] == agent_id
    assert run["issue_id"] == issue_id
    assert run["model"] == "claude-sonnet-4-5-20250929"
    assert run["outcome"] == "done"
    assert run["duration_s"] is not None
    assert run["duration_s"] >= 0  # Can be 0 due to rounding
    assert run["retry_count"] == 0
    assert run["notes_produced"] == 0
    assert run["notes_injected"] == 0


def test_agent_runs_with_retries(temp_db):
    """Test agent_runs view counts retries correctly."""
    issue_id = temp_db.create_issue("Test task", project="test")
    agent_id = temp_db.create_agent("worker-001")

    temp_db.log_event(issue_id, agent_id, "worker_started")
    temp_db.log_event(issue_id, agent_id, "retry", {"reason": "timeout"})
    temp_db.log_event(issue_id, agent_id, "retry", {"reason": "incomplete"})
    temp_db.log_event(issue_id, agent_id, "completed")

    cursor = temp_db.conn.execute("SELECT retry_count FROM agent_runs WHERE agent_id = ?", (agent_id,))
    row = cursor.fetchone()
    assert row["retry_count"] == 2


def test_agent_runs_with_notes(temp_db):
    """Test agent_runs view counts notes correctly."""
    issue_id = temp_db.create_issue("Test task", project="test")
    agent_id = temp_db.create_agent("worker-001")

    temp_db.log_event(issue_id, agent_id, "worker_started")
    temp_db.log_event(issue_id, agent_id, "notes_injected", {"count": 3})
    temp_db.log_event(issue_id, agent_id, "notes_harvested", {"count": 2})
    temp_db.log_event(issue_id, agent_id, "completed")

    cursor = temp_db.conn.execute("SELECT notes_injected, notes_produced FROM agent_runs WHERE agent_id = ?", (agent_id,))
    row = cursor.fetchone()
    assert row["notes_injected"] == 1  # count of events, not sum of counts
    assert row["notes_produced"] == 1


def test_agent_runs_failed_outcome(temp_db):
    """Test agent_runs view with failed outcome."""
    issue_id = temp_db.create_issue("Test task", project="test")
    agent_id = temp_db.create_agent("worker-001")

    temp_db.log_event(issue_id, agent_id, "worker_started")
    temp_db.log_event(issue_id, agent_id, "status_failed", {"reason": "error"})

    cursor = temp_db.conn.execute("SELECT outcome FROM agent_runs WHERE agent_id = ?", (agent_id,))
    row = cursor.fetchone()
    assert row["outcome"] == "failed"


def test_agent_runs_escalated_outcome(temp_db):
    """Test agent_runs view with escalated outcome."""
    issue_id = temp_db.create_issue("Test task", project="test")
    agent_id = temp_db.create_agent("worker-001")

    temp_db.log_event(issue_id, agent_id, "worker_started")
    temp_db.log_event(issue_id, None, "escalated", {"reason": "blocked"})

    cursor = temp_db.conn.execute("SELECT outcome FROM agent_runs WHERE agent_id = ?", (agent_id,))
    row = cursor.fetchone()
    assert row["outcome"] == "escalated"


def test_agent_runs_no_completion_event(temp_db):
    """Test agent_runs view when worker_started has no completion event."""
    issue_id = temp_db.create_issue("Test task", project="test")
    agent_id = temp_db.create_agent("worker-001")

    temp_db.log_event(issue_id, agent_id, "worker_started")

    cursor = temp_db.conn.execute("SELECT outcome, duration_s, ended_at FROM agent_runs WHERE agent_id = ?", (agent_id,))
    row = cursor.fetchone()
    assert row["outcome"] == "unknown"
    assert row["duration_s"] is None
    assert row["ended_at"] is None


def test_get_metrics_basic(temp_db):
    """Test get_metrics returns correct aggregations."""
    # Create multiple issues with different models
    issue1 = temp_db.create_issue("Task 1", project="test", model="sonnet")
    issue2 = temp_db.create_issue("Task 2", project="test", model="sonnet")
    issue3 = temp_db.create_issue("Task 3", project="test", model="opus")

    agent1 = temp_db.create_agent("worker-001", model="sonnet")
    agent2 = temp_db.create_agent("worker-002", model="sonnet")
    agent3 = temp_db.create_agent("worker-003", model="opus")

    # Sonnet: 2 runs, 1 success, 1 failure
    temp_db.log_event(issue1, agent1, "worker_started")
    temp_db.log_event(issue1, agent1, "completed")

    temp_db.log_event(issue2, agent2, "worker_started")
    temp_db.log_event(issue2, agent2, "status_failed")

    # Opus: 1 run, 1 success
    temp_db.log_event(issue3, agent3, "worker_started")
    temp_db.log_event(issue3, agent3, "completed")

    results = temp_db.get_metrics()

    assert len(results) == 2

    # Find sonnet and opus results
    sonnet_result = next(r for r in results if r["model"] == "sonnet")
    opus_result = next(r for r in results if r["model"] == "opus")

    assert sonnet_result["runs"] == 2
    assert sonnet_result["success_count"] == 1
    assert sonnet_result["failed_count"] == 1
    assert sonnet_result["success_rate"] == 50.0

    assert opus_result["runs"] == 1
    assert opus_result["success_count"] == 1
    assert opus_result["failed_count"] == 0
    assert opus_result["success_rate"] == 100.0


def test_get_metrics_with_retries(temp_db):
    """Test get_metrics calculates average retries correctly."""
    issue1 = temp_db.create_issue("Task 1", project="test", model="sonnet")
    issue2 = temp_db.create_issue("Task 2", project="test", model="sonnet")

    agent1 = temp_db.create_agent("worker-001", model="sonnet")
    agent2 = temp_db.create_agent("worker-002", model="sonnet")

    # First run: 2 retries
    temp_db.log_event(issue1, agent1, "worker_started")
    temp_db.log_event(issue1, agent1, "retry")
    temp_db.log_event(issue1, agent1, "retry")
    temp_db.log_event(issue1, agent1, "completed")

    # Second run: 0 retries
    temp_db.log_event(issue2, agent2, "worker_started")
    temp_db.log_event(issue2, agent2, "completed")

    results = temp_db.get_metrics()
    assert len(results) == 1

    sonnet_result = results[0]
    assert sonnet_result["avg_retries"] == 1.0  # (2 + 0) / 2


def test_get_metrics_filter_by_model(temp_db):
    """Test get_metrics filters by model correctly."""
    issue1 = temp_db.create_issue("Task 1", project="test", model="sonnet")
    issue2 = temp_db.create_issue("Task 2", project="test", model="opus")

    agent1 = temp_db.create_agent("worker-001", model="sonnet")
    agent2 = temp_db.create_agent("worker-002", model="opus")

    temp_db.log_event(issue1, agent1, "worker_started")
    temp_db.log_event(issue1, agent1, "completed")

    temp_db.log_event(issue2, agent2, "worker_started")
    temp_db.log_event(issue2, agent2, "completed")

    results = temp_db.get_metrics(model="sonnet")

    assert len(results) == 1
    assert results[0]["model"] == "sonnet"
    assert results[0]["runs"] == 1


def test_get_metrics_filter_by_tag(temp_db):
    """Test get_metrics filters by tag correctly."""
    issue1 = temp_db.create_issue("Task 1", project="test", model="sonnet", tags=["bugfix", "python"])
    issue2 = temp_db.create_issue("Task 2", project="test", model="sonnet", tags=["feature"])

    agent1 = temp_db.create_agent("worker-001", model="sonnet")
    agent2 = temp_db.create_agent("worker-002", model="sonnet")

    temp_db.log_event(issue1, agent1, "worker_started")
    temp_db.log_event(issue1, agent1, "completed")

    temp_db.log_event(issue2, agent2, "worker_started")
    temp_db.log_event(issue2, agent2, "completed")

    results = temp_db.get_metrics(tag="bugfix")

    assert len(results) == 1
    assert results[0]["runs"] == 1


def test_get_metrics_filter_by_issue_type(temp_db):
    """Test get_metrics filters by issue type correctly."""
    issue1 = temp_db.create_issue("Task 1", project="test", model="sonnet", issue_type="bug")
    issue2 = temp_db.create_issue("Task 2", project="test", model="sonnet", issue_type="feature")

    agent1 = temp_db.create_agent("worker-001", model="sonnet")
    agent2 = temp_db.create_agent("worker-002", model="sonnet")

    temp_db.log_event(issue1, agent1, "worker_started")
    temp_db.log_event(issue1, agent1, "completed")

    temp_db.log_event(issue2, agent2, "worker_started")
    temp_db.log_event(issue2, agent2, "completed")

    results = temp_db.get_metrics(issue_type="bug")

    assert len(results) == 1
    assert results[0]["runs"] == 1


def test_get_metrics_merge_health(temp_db):
    """Test get_metrics calculates merge health correctly."""
    issue1 = temp_db.create_issue("Task 1", project="test", model="sonnet")
    issue2 = temp_db.create_issue("Task 2", project="test", model="sonnet")
    issue3 = temp_db.create_issue("Task 3", project="test", model="sonnet")

    agent1 = temp_db.create_agent("worker-001", model="sonnet")
    agent2 = temp_db.create_agent("worker-002", model="sonnet")
    agent3 = temp_db.create_agent("worker-003", model="sonnet")

    # Run 1: tests passed
    temp_db.log_event(issue1, agent1, "worker_started")
    temp_db.log_event(issue1, agent1, "completed")
    temp_db.log_event(issue1, agent1, "tests_passed")

    # Run 2: test failure
    temp_db.log_event(issue2, agent2, "worker_started")
    temp_db.log_event(issue2, agent2, "completed")
    temp_db.log_event(issue2, agent2, "test_failure")

    # Run 3: tests passed
    temp_db.log_event(issue3, agent3, "worker_started")
    temp_db.log_event(issue3, agent3, "completed")
    temp_db.log_event(issue3, agent3, "tests_passed")

    results = temp_db.get_metrics()
    assert len(results) == 1

    sonnet_result = results[0]
    # 2 tests_passed out of 3 total merge events = 66.7%
    assert sonnet_result["merge_health"] == 66.7


def test_get_metrics_no_merge_events(temp_db):
    """Test get_metrics when no merge events exist."""
    issue1 = temp_db.create_issue("Task 1", project="test", model="sonnet")
    agent1 = temp_db.create_agent("worker-001", model="sonnet")

    temp_db.log_event(issue1, agent1, "worker_started")
    temp_db.log_event(issue1, agent1, "completed")

    results = temp_db.get_metrics()
    assert len(results) == 1
    assert results[0]["merge_health"] is None


def test_get_metrics_duration_calculation(temp_db):
    """Test get_metrics calculates average duration correctly."""
    issue1 = temp_db.create_issue("Task 1", project="test", model="sonnet")
    issue2 = temp_db.create_issue("Task 2", project="test", model="sonnet")

    agent1 = temp_db.create_agent("worker-001", model="sonnet")
    agent2 = temp_db.create_agent("worker-002", model="sonnet")

    # First run: 10 seconds (using explicit timestamp injection)
    temp_db.conn.execute(
        "INSERT INTO events (issue_id, agent_id, event_type, created_at) VALUES (?, ?, 'worker_started', datetime('now', '-10 seconds'))",
        (issue1, agent1),
    )
    temp_db.conn.execute(
        "INSERT INTO events (issue_id, agent_id, event_type, created_at) VALUES (?, ?, 'completed', datetime('now'))", (issue1, agent1)
    )

    # Second run: 10 seconds (using explicit timestamp injection)
    temp_db.conn.execute(
        "INSERT INTO events (issue_id, agent_id, event_type, created_at) VALUES (?, ?, 'worker_started', datetime('now', '-10 seconds'))",
        (issue2, agent2),
    )
    temp_db.conn.execute(
        "INSERT INTO events (issue_id, agent_id, event_type, created_at) VALUES (?, ?, 'completed', datetime('now'))", (issue2, agent2)
    )
    temp_db.conn.commit()

    results = temp_db.get_metrics()
    assert len(results) == 1

    sonnet_result = results[0]
    # Average should be approximately 10.0 seconds
    assert sonnet_result["avg_duration_s"] is not None
    assert sonnet_result["avg_duration_s"] == pytest.approx(10.0, abs=1.0)


def test_agent_runs_view_with_tags(temp_db):
    """Test agent_runs view preserves tags from issues table."""
    issue_id = temp_db.create_issue("Test task", project="test", model="sonnet", tags=["bugfix", "python", "small"])
    agent_id = temp_db.create_agent("worker-001", model="sonnet")

    temp_db.log_event(issue_id, agent_id, "worker_started")
    temp_db.log_event(issue_id, agent_id, "completed")

    cursor = temp_db.conn.execute("SELECT tags FROM agent_runs WHERE agent_id = ?", (agent_id,))
    row = cursor.fetchone()

    # Tags are stored as JSON
    tags = json.loads(row["tags"])
    assert tags == ["bugfix", "python", "small"]


def test_agent_runs_multiple_agents_same_issue(temp_db):
    """Test agent_runs view when multiple agents work on same issue (retry scenario)."""
    issue_id = temp_db.create_issue("Test task", project="test", model="sonnet")
    agent1 = temp_db.create_agent("worker-001", model="sonnet")
    agent2 = temp_db.create_agent("worker-002", model="sonnet")

    # First agent fails
    temp_db.log_event(issue_id, agent1, "worker_started")
    temp_db.log_event(issue_id, agent1, "status_failed")

    # Second agent succeeds
    temp_db.log_event(issue_id, agent2, "worker_started")
    temp_db.log_event(issue_id, agent2, "completed")

    cursor = temp_db.conn.execute("SELECT agent_id, outcome FROM agent_runs WHERE issue_id = ? ORDER BY started_at", (issue_id,))
    rows = [dict(row) for row in cursor.fetchall()]

    assert len(rows) == 2
    assert rows[0]["agent_id"] == agent1
    assert rows[0]["outcome"] == "failed"
    assert rows[1]["agent_id"] == agent2
    assert rows[1]["outcome"] == "done"
