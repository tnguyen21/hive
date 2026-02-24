"""Tests for cost guardrails: per-issue budget, per-run budget, anomaly detection."""

import json
import os
import tempfile
from unittest.mock import AsyncMock

import pytest

from hive.config import Config
from hive.db import Database
from hive.utils import AgentIdentity, CompletionResult


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def temp_db():
    """Provide a temporary test database."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    db = Database(db_path)
    db.connect()

    yield db

    db.close()
    os.unlink(db_path)


# ── Config defaults ──────────────────────────────────────────────────────


def test_config_defaults():
    """Cost guardrail config fields have sensible defaults."""
    assert Config.MAX_TOKENS_PER_ISSUE == 200_000
    assert Config.ANOMALY_WINDOW_MINUTES == 10
    assert Config.ANOMALY_FAILURE_THRESHOLD == 3


def test_config_env_override(monkeypatch):
    """Cost guardrail config can be overridden via env vars."""
    monkeypatch.setenv("HIVE_MAX_TOKENS_PER_ISSUE", "500000")
    monkeypatch.setenv("HIVE_ANOMALY_WINDOW_MINUTES", "5")
    monkeypatch.setenv("HIVE_ANOMALY_FAILURE_THRESHOLD", "10")

    Config._apply_defaults()
    Config._apply_env()

    assert Config.MAX_TOKENS_PER_ISSUE == 500_000
    assert Config.ANOMALY_WINDOW_MINUTES == 5
    assert Config.ANOMALY_FAILURE_THRESHOLD == 10

    # Restore defaults
    Config._apply_defaults()


# ── DB helper methods ────────────────────────────────────────────────────


def test_get_issue_token_total_empty(temp_db):
    """Returns 0 when no token events exist for an issue."""
    issue_id = temp_db.create_issue("Test", project="test")
    assert temp_db.get_issue_token_total(issue_id) == 0


def test_get_issue_token_total_sums_correctly(temp_db):
    """Sums input + output tokens across multiple events."""
    issue_id = temp_db.create_issue("Test", project="test")
    agent_id = temp_db.create_agent("worker-1")

    # Log two token usage events
    temp_db.log_event(issue_id, agent_id, "tokens_used", {"input_tokens": 1000, "output_tokens": 500})
    temp_db.log_event(issue_id, agent_id, "tokens_used", {"input_tokens": 2000, "output_tokens": 1000})

    assert temp_db.get_issue_token_total(issue_id) == 4500  # (1000+500) + (2000+1000)


def test_get_issue_token_total_isolates_issues(temp_db):
    """Only counts tokens for the specified issue."""
    issue1 = temp_db.create_issue("Issue 1", project="test")
    issue2 = temp_db.create_issue("Issue 2", project="test")
    agent_id = temp_db.create_agent("worker-1")

    temp_db.log_event(issue1, agent_id, "tokens_used", {"input_tokens": 1000, "output_tokens": 500})
    temp_db.log_event(issue2, agent_id, "tokens_used", {"input_tokens": 9000, "output_tokens": 9000})

    assert temp_db.get_issue_token_total(issue1) == 1500
    assert temp_db.get_issue_token_total(issue2) == 18000


def test_count_events_since_minutes(temp_db):
    """Counts events within the last N minutes."""
    issue_id = temp_db.create_issue("Test", project="test")
    agent_id = temp_db.create_agent("worker-1")

    # Log events
    temp_db.log_event(issue_id, agent_id, "incomplete", {"reason": "failed"})
    temp_db.log_event(issue_id, agent_id, "incomplete", {"reason": "failed again"})
    temp_db.log_event(issue_id, agent_id, "incomplete", {"reason": "failed yet again"})

    # All events should be within the last minute
    assert temp_db.count_events_since_minutes(issue_id, "incomplete", 1) == 3

    # Zero window should return 0 (events are at time 'now', window is 0 minutes ago = 'now')
    # Events created_at == datetime('now') so with 0 minutes they should still match
    assert temp_db.count_events_since_minutes(issue_id, "incomplete", 0) == 3


def test_count_events_since_minutes_filters_event_type(temp_db):
    """Only counts the specified event type."""
    issue_id = temp_db.create_issue("Test", project="test")
    agent_id = temp_db.create_agent("worker-1")

    temp_db.log_event(issue_id, agent_id, "incomplete", {"reason": "failed"})
    temp_db.log_event(issue_id, agent_id, "retry", {"retry_count": 1})
    temp_db.log_event(issue_id, agent_id, "incomplete", {"reason": "failed again"})

    assert temp_db.count_events_since_minutes(issue_id, "incomplete", 1) == 2
    assert temp_db.count_events_since_minutes(issue_id, "retry", 1) == 1


# ── Per-issue token budget (orchestrator integration) ────────────────────


@pytest.mark.asyncio
async def test_per_issue_budget_triggers_failure(temp_db):
    """When issue token total exceeds budget, handle_agent_complete treats it as failure."""
    from hive.orchestrator import Orchestrator

    opencode = AsyncMock()
    opencode.get_messages = AsyncMock(return_value=[])
    opencode.cleanup_session = AsyncMock()

    orch = Orchestrator(db=temp_db, opencode_client=opencode, project_path="/tmp/test", project_name="test")

    # Create issue and agent
    issue_id = temp_db.create_issue("Budget test", project="test")
    agent_id = temp_db.create_agent("worker-1")
    temp_db.claim_issue(issue_id, agent_id)

    agent = AgentIdentity(agent_id=agent_id, name="worker-1", issue_id=issue_id, worktree="/tmp/wt", session_id="sess-1")
    orch.active_agents[agent_id] = agent
    orch._session_to_agent["sess-1"] = agent_id
    orch._issue_to_agent[issue_id] = agent_id

    # Log tokens exceeding the budget
    temp_db.log_event(issue_id, agent_id, "tokens_used", {"input_tokens": 150_000, "output_tokens": 100_000})

    # Set a low budget for testing
    original = Config.MAX_TOKENS_PER_ISSUE
    Config.MAX_TOKENS_PER_ISSUE = 200_000
    try:
        await orch.handle_agent_complete(agent, file_result=None)
    finally:
        Config.MAX_TOKENS_PER_ISSUE = original

    # Should have logged budget_exceeded event
    events = temp_db.get_events(issue_id=issue_id, event_type="budget_exceeded")
    assert len(events) == 1
    detail = json.loads(events[0]["detail"])
    assert detail["issue_tokens"] == 250_000

    # Issue should have been processed through failure path (incomplete event logged)
    incomplete_events = temp_db.get_events(issue_id=issue_id, event_type="incomplete")
    assert len(incomplete_events) >= 1


@pytest.mark.asyncio
async def test_per_issue_budget_allows_under_limit(temp_db):
    """When issue tokens are under budget, normal completion proceeds."""
    from hive.orchestrator import Orchestrator

    opencode = AsyncMock()
    opencode.get_messages = AsyncMock(
        return_value=[
            {"metadata": {"input_tokens": 100, "output_tokens": 50, "model": "test"}},
        ]
    )
    opencode.cleanup_session = AsyncMock()

    orch = Orchestrator(db=temp_db, opencode_client=opencode, project_path="/tmp/test", project_name="test")

    issue_id = temp_db.create_issue("Under budget test", project="test")
    agent_id = temp_db.create_agent("worker-1")
    temp_db.claim_issue(issue_id, agent_id)

    agent = AgentIdentity(agent_id=agent_id, name="worker-1", issue_id=issue_id, worktree="/tmp/wt", session_id="sess-1")
    orch.active_agents[agent_id] = agent
    orch._session_to_agent["sess-1"] = agent_id
    orch._issue_to_agent[issue_id] = agent_id

    # Small token usage - well under budget
    temp_db.log_event(issue_id, agent_id, "tokens_used", {"input_tokens": 100, "output_tokens": 50})

    original = Config.MAX_TOKENS_PER_ISSUE
    Config.MAX_TOKENS_PER_ISSUE = 200_000
    try:
        # Provide a failure result (to avoid needing the full merge queue path)
        await orch.handle_agent_complete(
            agent,
            file_result={"status": "failure", "summary": "Could not solve", "blockers": ["test"]},
        )
    finally:
        Config.MAX_TOKENS_PER_ISSUE = original

    # Should NOT have budget_exceeded event
    events = temp_db.get_events(issue_id=issue_id, event_type="budget_exceeded")
    assert len(events) == 0


# ── Anomaly detection ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_anomaly_detection_escalates(temp_db):
    """Rapid failures trigger anomaly detection and auto-escalation."""
    from hive.orchestrator import Orchestrator

    opencode = AsyncMock()
    orch = Orchestrator(db=temp_db, opencode_client=opencode, project_path="/tmp/test", project_name="test")

    issue_id = temp_db.create_issue("Anomaly test", project="test")
    agent_id = temp_db.create_agent("worker-1")
    temp_db.claim_issue(issue_id, agent_id)

    agent = AgentIdentity(agent_id=agent_id, name="worker-1", issue_id=issue_id, worktree="/tmp/wt", session_id="sess-1")

    # Pre-populate with failures to trigger anomaly (threshold=3, we add 2 then call fails)
    temp_db.log_event(issue_id, agent_id, "incomplete", {"reason": "fail 1"})
    temp_db.log_event(issue_id, agent_id, "incomplete", {"reason": "fail 2"})

    original_threshold = Config.ANOMALY_FAILURE_THRESHOLD
    original_window = Config.ANOMALY_WINDOW_MINUTES
    Config.ANOMALY_FAILURE_THRESHOLD = 3
    Config.ANOMALY_WINDOW_MINUTES = 10
    try:
        result = CompletionResult(success=False, reason="fail 3", summary="Third failure")
        await orch._handle_agent_failure(agent, result)
    finally:
        Config.ANOMALY_FAILURE_THRESHOLD = original_threshold
        Config.ANOMALY_WINDOW_MINUTES = original_window

    # Should have escalated
    issue = temp_db.get_issue(issue_id)
    assert issue["status"] == "escalated"

    escalated_events = temp_db.get_events(issue_id=issue_id, event_type="escalated")
    assert len(escalated_events) == 1
    detail = json.loads(escalated_events[0]["detail"])
    assert "Anomaly detection" in detail["reason"]


@pytest.mark.asyncio
async def test_anomaly_detection_allows_normal_retry(temp_db):
    """Below anomaly threshold, normal retry proceeds."""
    from hive.orchestrator import Orchestrator

    opencode = AsyncMock()
    orch = Orchestrator(db=temp_db, opencode_client=opencode, project_path="/tmp/test", project_name="test")

    issue_id = temp_db.create_issue("Normal retry test", project="test")
    agent_id = temp_db.create_agent("worker-1")
    temp_db.claim_issue(issue_id, agent_id)

    agent = AgentIdentity(agent_id=agent_id, name="worker-1", issue_id=issue_id, worktree="/tmp/wt", session_id="sess-1")

    original_threshold = Config.ANOMALY_FAILURE_THRESHOLD
    original_window = Config.ANOMALY_WINDOW_MINUTES
    Config.ANOMALY_FAILURE_THRESHOLD = 3
    Config.ANOMALY_WINDOW_MINUTES = 10
    try:
        result = CompletionResult(success=False, reason="first failure", summary="First attempt")
        await orch._handle_agent_failure(agent, result)
    finally:
        Config.ANOMALY_FAILURE_THRESHOLD = original_threshold
        Config.ANOMALY_WINDOW_MINUTES = original_window

    # Should have retried, not escalated
    issue = temp_db.get_issue(issue_id)
    assert issue["status"] == "open"

    retry_events = temp_db.get_events(issue_id=issue_id, event_type="retry")
    assert len(retry_events) == 1

    escalated_events = temp_db.get_events(issue_id=issue_id, event_type="escalated")
    assert len(escalated_events) == 0
