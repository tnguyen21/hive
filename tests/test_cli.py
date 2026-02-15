"""Tests for CLI interface."""

import json
import unittest.mock

import pytest

from hive.cli import HiveCLI


def test_cli_create(temp_db, tmp_path):
    """Test creating an issue via CLI."""
    cli = HiveCLI(temp_db, str(tmp_path))

    issue_id = cli.create("Test issue", "Test description", priority=1)

    assert issue_id.startswith("w-")

    # Verify issue was created
    issue = temp_db.get_issue(issue_id)
    assert issue is not None
    assert issue["title"] == "Test issue"
    assert issue["description"] == "Test description"
    assert issue["priority"] == 1


def test_cli_create_json(temp_db, tmp_path, capsys):
    """Test creating an issue with JSON output."""
    cli = HiveCLI(temp_db, str(tmp_path))

    cli.create("JSON test", "desc", priority=1, json_mode=True)

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["issue_id"].startswith("w-")
    assert data["title"] == "JSON test"
    assert data["status"] == "open"


def test_cli_create_with_depends_on(temp_db, tmp_path, capsys):
    """Test creating an issue with --depends-on wires deps atomically."""
    cli = HiveCLI(temp_db, str(tmp_path))

    # Create a blocker issue first
    blocker_id = cli.create("Blocker", "block desc")

    # Create a dependent issue with --depends-on
    dependent_id = cli.create("Dependent", "dep desc", depends_on=[blocker_id])

    # Verify dependency was created
    issue = temp_db.get_issue(dependent_id)
    assert issue["status"] == "open"

    # The dependent should NOT be claimable (blocker is still open)
    agent_id = temp_db.create_agent("test-agent")
    claimed = temp_db.claim_issue(dependent_id, agent_id)
    assert not claimed

    # Resolve blocker, then claim should work
    temp_db.update_issue_status(blocker_id, "finalized")
    claimed = temp_db.claim_issue(dependent_id, agent_id)
    assert claimed


def test_cli_create_with_depends_on_json(temp_db, tmp_path, capsys):
    """Test --depends-on shows in JSON output."""
    cli = HiveCLI(temp_db, str(tmp_path))

    blocker_id = cli.create("Blocker", "desc")
    capsys.readouterr()  # Clear output from first create

    cli.create("Dependent", "desc", depends_on=[blocker_id], json_mode=True)

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert blocker_id in data["depends_on"]


def test_cli_list_issues(temp_db, tmp_path, capsys):
    """Test listing issues via CLI."""
    cli = HiveCLI(temp_db, str(tmp_path))

    # Create some issues
    temp_db.create_issue("Issue 1", priority=1, project=tmp_path.name)
    temp_db.create_issue("Issue 2", priority=2, project=tmp_path.name)
    temp_db.create_issue("Issue 3", priority=3, project=tmp_path.name)

    cli.list_issues()

    captured = capsys.readouterr()
    assert "Issue 1" in captured.out
    assert "Issue 2" in captured.out
    assert "Issue 3" in captured.out
    assert "Total: 3 issues" in captured.out


def test_cli_list_issues_json(temp_db, tmp_path, capsys):
    """Test listing issues with JSON output."""
    cli = HiveCLI(temp_db, str(tmp_path))

    temp_db.create_issue("Issue A", priority=1, project=tmp_path.name)
    temp_db.create_issue("Issue B", priority=2, project=tmp_path.name)

    cli.list_issues(json_mode=True)

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["count"] == 2
    assert len(data["issues"]) == 2


def test_cli_list_issues_by_status(temp_db, tmp_path, capsys):
    """Test listing issues filtered by status."""
    cli = HiveCLI(temp_db, str(tmp_path))

    # Create issues with different statuses
    temp_db.create_issue("Open issue", project=tmp_path.name)
    issue2 = temp_db.create_issue("Done issue", project=tmp_path.name)
    temp_db.update_issue_status(issue2, "done")

    cli.list_issues(status="open")

    captured = capsys.readouterr()
    assert "Open issue" in captured.out
    assert "Done issue" not in captured.out


def test_cli_show_ready(temp_db, tmp_path, capsys):
    """Test showing ready queue."""
    cli = HiveCLI(temp_db, str(tmp_path))

    # Create ready issues
    temp_db.create_issue("Ready 1", priority=1, project=tmp_path.name)
    temp_db.create_issue("Ready 2", priority=2, project=tmp_path.name)

    cli.show_ready()

    captured = capsys.readouterr()
    assert "Ready 1" in captured.out
    assert "Ready 2" in captured.out
    assert "Total: 2 ready issues" in captured.out


def test_cli_show_ready_json(temp_db, tmp_path, capsys):
    """Test showing ready queue with JSON output."""
    cli = HiveCLI(temp_db, str(tmp_path))

    temp_db.create_issue("Ready 1", priority=1, project=tmp_path.name)

    cli.show_ready(json_mode=True)

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["count"] == 1
    assert data["ready_issues"][0]["title"] == "Ready 1"


def test_cli_show_issue(temp_db, tmp_path, capsys):
    """Test showing issue details."""
    cli = HiveCLI(temp_db, str(tmp_path))

    issue_id = temp_db.create_issue("Test issue", "Detailed description", priority=1, project=tmp_path.name)

    cli.show(issue_id)

    captured = capsys.readouterr()
    assert issue_id in captured.out
    assert "Test issue" in captured.out
    assert "Detailed description" in captured.out
    assert "Priority: 1" in captured.out


def test_cli_show_issue_json(temp_db, tmp_path, capsys):
    """Test showing issue details with JSON output."""
    cli = HiveCLI(temp_db, str(tmp_path))

    issue_id = temp_db.create_issue("Test issue", "desc", priority=1, project=tmp_path.name)

    cli.show(issue_id, json_mode=True)

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["issue"]["id"] == issue_id
    assert data["issue"]["title"] == "Test issue"
    assert "dependencies" in data
    assert "dependents" in data


def test_cli_status(temp_db, tmp_path, capsys):
    """Test showing status."""
    cli = HiveCLI(temp_db, str(tmp_path))

    # Create some issues
    temp_db.create_issue("Open 1", project=tmp_path.name)
    temp_db.create_issue("Open 2", project=tmp_path.name)
    issue3 = temp_db.create_issue("Done 1", project=tmp_path.name)
    temp_db.update_issue_status(issue3, "done")

    cli.status()

    captured = capsys.readouterr()
    assert "Hive Status" in captured.out
    assert "open: 2" in captured.out
    assert "done: 1" in captured.out
    assert "Ready queue:" in captured.out


def test_cli_status_json(temp_db, tmp_path, capsys):
    """Test showing status with JSON output."""
    cli = HiveCLI(temp_db, str(tmp_path))

    temp_db.create_issue("Open 1", project=tmp_path.name)

    cli.status(json_mode=True)

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "issues" in data
    assert "total_issues" in data
    assert data["project"] == tmp_path.name


def test_cli_show_issue_with_dependencies(temp_db, tmp_path, capsys):
    """Test showing issue with dependencies."""
    cli = HiveCLI(temp_db, str(tmp_path))

    # Create issues with dependencies
    issue1 = temp_db.create_issue("Dependency", project=tmp_path.name)
    issue2 = temp_db.create_issue("Main task", project=tmp_path.name)

    temp_db.add_dependency(issue2, issue1)

    cli.show(issue2)

    captured = capsys.readouterr()
    assert "Depends on:" in captured.out
    assert issue1 in captured.out
    assert "Dependency" in captured.out


# ── New subcommand tests ────────────────────────────────────────────


def test_cli_update(temp_db, tmp_path, capsys):
    """Test updating an issue."""
    cli = HiveCLI(temp_db, str(tmp_path))

    issue_id = temp_db.create_issue("Original title", project=tmp_path.name)
    cli.update(issue_id, title="Updated title")

    issue = temp_db.get_issue(issue_id)
    assert issue["title"] == "Updated title"


def test_cli_update_json(temp_db, tmp_path, capsys):
    """Test updating an issue with JSON output."""
    cli = HiveCLI(temp_db, str(tmp_path))

    issue_id = temp_db.create_issue("Original", project=tmp_path.name)
    cli.update(issue_id, title="New title", json_mode=True)

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["issue_id"] == issue_id


def test_cli_cancel(temp_db, tmp_path, capsys):
    """Test canceling an issue."""
    cli = HiveCLI(temp_db, str(tmp_path))

    issue_id = temp_db.create_issue("To cancel", project=tmp_path.name)
    cli.cancel(issue_id, reason="no longer needed")

    issue = temp_db.get_issue(issue_id)
    assert issue["status"] == "canceled"


def test_cli_cancel_json(temp_db, tmp_path, capsys):
    """Test canceling an issue with JSON output."""
    cli = HiveCLI(temp_db, str(tmp_path))

    issue_id = temp_db.create_issue("To cancel", project=tmp_path.name)
    cli.cancel(issue_id, reason="test", json_mode=True)

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["status"] == "canceled"


def test_cli_finalize(temp_db, tmp_path, capsys):
    """Test finalizing an issue."""
    cli = HiveCLI(temp_db, str(tmp_path))

    issue_id = temp_db.create_issue("To finalize", project=tmp_path.name)
    cli.finalize(issue_id, resolution="completed manually")

    issue = temp_db.get_issue(issue_id)
    assert issue["status"] == "finalized"


def test_cli_finalize_json(temp_db, tmp_path, capsys):
    """Test finalizing with JSON output."""
    cli = HiveCLI(temp_db, str(tmp_path))

    issue_id = temp_db.create_issue("To finalize", project=tmp_path.name)
    cli.finalize(issue_id, json_mode=True)

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["status"] == "finalized"


def test_cli_retry(temp_db, tmp_path, capsys):
    """Test retrying a failed issue."""
    cli = HiveCLI(temp_db, str(tmp_path))

    issue_id = temp_db.create_issue("Failed task", project=tmp_path.name)
    temp_db.update_issue_status(issue_id, "failed")

    cli.retry(issue_id, notes="try different approach")

    issue = temp_db.get_issue(issue_id)
    assert issue["status"] == "open"
    assert issue["assignee"] is None


def test_cli_retry_json(temp_db, tmp_path, capsys):
    """Test retrying with JSON output."""
    cli = HiveCLI(temp_db, str(tmp_path))

    issue_id = temp_db.create_issue("Failed task", project=tmp_path.name)
    temp_db.update_issue_status(issue_id, "failed")

    cli.retry(issue_id, json_mode=True)

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["status"] == "open"


def test_cli_retry_logs_manual_retry_event(temp_db, tmp_path):
    """Test that manual retry logs 'manual_retry' event type, not 'retry'."""
    cli = HiveCLI(temp_db, str(tmp_path))

    # Create and fail an issue
    issue_id = temp_db.create_issue("Failed task", project=tmp_path.name)
    temp_db.update_issue_status(issue_id, "failed")

    # Retry the issue manually
    cli.retry(issue_id, notes="manual retry test")

    # Verify the event type is 'manual_retry' and 'retry' count is 0
    manual_retry_count = temp_db.count_events_by_type(issue_id, "manual_retry")
    retry_count = temp_db.count_events_by_type(issue_id, "retry")

    assert manual_retry_count == 1, "Should have exactly 1 manual_retry event"
    assert retry_count == 0, "Should have 0 retry events (only manual_retry)"


def test_cli_escalate(temp_db, tmp_path, capsys):
    """Test escalating an issue."""
    cli = HiveCLI(temp_db, str(tmp_path))

    issue_id = temp_db.create_issue("Needs help", project=tmp_path.name)
    cli.escalate(issue_id, reason="blocked on API access")

    issue = temp_db.get_issue(issue_id)
    assert issue["status"] == "escalated"


def test_cli_escalate_json(temp_db, tmp_path, capsys):
    """Test escalating with JSON output."""
    cli = HiveCLI(temp_db, str(tmp_path))

    issue_id = temp_db.create_issue("Needs help", project=tmp_path.name)
    cli.escalate(issue_id, reason="blocked", json_mode=True)

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["status"] == "escalated"


def test_cli_molecule(temp_db, tmp_path, capsys):
    """Test creating a molecule."""
    cli = HiveCLI(temp_db, str(tmp_path))

    steps = json.dumps(
        [
            {"title": "Step 1", "description": "First step"},
            {"title": "Step 2", "description": "Second step", "needs": [0]},
        ]
    )

    cli.molecule("Test workflow", description="A test", steps_json=steps)

    captured = capsys.readouterr()
    assert "Created molecule" in captured.out
    assert "Step 0" in captured.out
    assert "Step 1" in captured.out


def test_cli_molecule_json(temp_db, tmp_path, capsys):
    """Test creating a molecule with JSON output."""
    cli = HiveCLI(temp_db, str(tmp_path))

    steps = json.dumps(
        [
            {"title": "Step A"},
            {"title": "Step B", "needs": [0]},
        ]
    )

    cli.molecule("Workflow", steps_json=steps, json_mode=True)

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["steps_count"] == 2
    assert "molecule_id" in data


def test_cli_dep_add_remove(temp_db, tmp_path, capsys):
    """Test adding and removing dependencies."""
    cli = HiveCLI(temp_db, str(tmp_path))

    issue1 = temp_db.create_issue("Blocker", project=tmp_path.name)
    issue2 = temp_db.create_issue("Blocked", project=tmp_path.name)

    cli.dep_add(issue2, issue1)

    captured = capsys.readouterr()
    assert "dependency" in captured.out.lower()

    # Verify dependency exists
    cursor = temp_db.conn.execute("SELECT * FROM dependencies WHERE issue_id = ? AND depends_on = ?", (issue2, issue1))
    assert cursor.fetchone() is not None

    cli.dep_remove(issue2, issue1)

    # Verify dependency removed
    cursor = temp_db.conn.execute("SELECT * FROM dependencies WHERE issue_id = ? AND depends_on = ?", (issue2, issue1))
    assert cursor.fetchone() is None


def test_cli_dep_add_json(temp_db, tmp_path, capsys):
    """Test adding dependencies with JSON output."""
    cli = HiveCLI(temp_db, str(tmp_path))

    issue1 = temp_db.create_issue("A", project=tmp_path.name)
    issue2 = temp_db.create_issue("B", project=tmp_path.name)

    cli.dep_add(issue2, issue1, json_mode=True)

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["issue_id"] == issue2
    assert data["depends_on"] == issue1


def test_cli_agents(temp_db, tmp_path, capsys):
    """Test listing agents."""
    cli = HiveCLI(temp_db, str(tmp_path))

    cli.list_agents()

    captured = capsys.readouterr()
    assert "No agents found" in captured.out


def test_cli_agents_json(temp_db, tmp_path, capsys):
    """Test listing agents with JSON output."""
    cli = HiveCLI(temp_db, str(tmp_path))

    cli.list_agents(json_mode=True)

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["count"] == 0
    assert data["agents"] == []


def test_cli_events(temp_db, tmp_path, capsys):
    """Test getting events."""
    cli = HiveCLI(temp_db, str(tmp_path))

    # Create an issue to generate events
    temp_db.create_issue("Event test", project=tmp_path.name)

    cli.get_events(limit=5)

    captured = capsys.readouterr()
    assert "created" in captured.out


def test_cli_events_json(temp_db, tmp_path, capsys):
    """Test getting events with JSON output."""
    cli = HiveCLI(temp_db, str(tmp_path))

    temp_db.create_issue("Event test", project=tmp_path.name)

    cli.get_events(limit=5, json_mode=True)

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "count" in data
    assert "events" in data
    assert data["count"] >= 1


def test_cli_logs(temp_db, tmp_path, capsys):
    """Test getting events via logs."""
    cli = HiveCLI(temp_db, str(tmp_path))

    # Create an issue to generate events
    temp_db.create_issue("Event test", project=tmp_path.name)

    cli.logs(n=5)

    captured = capsys.readouterr()
    assert "created" in captured.out


def test_cli_logs_json(temp_db, tmp_path, capsys):
    """Test getting events via logs with JSON output."""
    cli = HiveCLI(temp_db, str(tmp_path))

    temp_db.create_issue("Event test", project=tmp_path.name)

    cli.logs(n=5, json_mode=True)

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert isinstance(data, list)
    assert len(data) >= 1


def test_evaluate_permission_policy():
    """Test permission policy evaluation."""
    from hive.opencode import OpenCodeClient
    from hive.orchestrator import Orchestrator

    # Create a minimal orchestrator for testing
    db = None
    opencode = OpenCodeClient()
    orch = Orchestrator(db, opencode, "/tmp", "test")

    # Test deny rules
    assert orch.evaluate_permission_policy({"permission": "question", "patterns": []}) == "reject"
    assert orch.evaluate_permission_policy({"permission": "plan_enter", "patterns": []}) == "reject"
    assert orch.evaluate_permission_policy({"permission": "external_directory", "patterns": []}) == "reject"

    # Test allow rules
    assert orch.evaluate_permission_policy({"permission": "read", "patterns": []}) == "once"
    assert orch.evaluate_permission_policy({"permission": "edit", "patterns": []}) == "once"
    assert orch.evaluate_permission_policy({"permission": "write", "patterns": []}) == "once"
    assert orch.evaluate_permission_policy({"permission": "bash", "patterns": []}) == "once"

    # Test unknown permission
    assert orch.evaluate_permission_policy({"permission": "unknown", "patterns": []}) is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_permission_unblocker_auto_resolve(temp_db, tmp_path):
    """Test that permission unblocker auto-resolves permissions (requires OpenCode server)."""
    from hive.opencode import OpenCodeClient
    from hive.orchestrator import Orchestrator

    async with OpenCodeClient() as opencode:
        Orchestrator(
            db=temp_db,
            opencode_client=opencode,
            project_path=str(tmp_path),
            project_name="test",
        )

        # Get pending permissions (should be empty initially)
        pending = await opencode.get_pending_permissions()

        # For now, just verify the method works
        # In a real scenario, we'd create a session that triggers a permission request
        # and verify it gets auto-resolved
        assert isinstance(pending, list)


def test_cli_costs_no_data(temp_db, tmp_path, capsys):
    """Test costs command with no token usage data."""
    cli = HiveCLI(temp_db, str(tmp_path))

    cli.costs()

    captured = capsys.readouterr()
    assert "Token Usage & Costs" in captured.out
    assert "Total tokens: 0" in captured.out
    assert "Estimated cost: $0.0000" in captured.out


def test_cli_costs_with_data(temp_db, tmp_path, capsys):
    """Test costs command with token usage data."""
    cli = HiveCLI(temp_db, str(tmp_path))

    # Create issue and agent
    issue_id = temp_db.create_issue("Test issue", project=tmp_path.name)
    agent_id = temp_db.create_agent("test-agent")

    # Add some token usage events
    temp_db.log_event(issue_id, agent_id, "tokens_used", {"input_tokens": 1000, "output_tokens": 500, "model": "claude-sonnet-4-5-20250929"})
    temp_db.log_event(issue_id, agent_id, "tokens_used", {"input_tokens": 2000, "output_tokens": 1000, "model": "claude-sonnet-4-5-20250929"})

    cli.costs()

    captured = capsys.readouterr()
    assert "Total tokens: 4,500" in captured.out
    assert "Input tokens: 3,000" in captured.out
    assert "Output tokens: 1,500" in captured.out
    assert "$" in captured.out  # Should have some cost estimate


def test_cli_costs_json(temp_db, tmp_path, capsys):
    """Test costs command with JSON output."""
    cli = HiveCLI(temp_db, str(tmp_path))

    # Create issue and agent with token usage
    issue_id = temp_db.create_issue("Test issue", project=tmp_path.name)
    agent_id = temp_db.create_agent("test-agent")

    temp_db.log_event(issue_id, agent_id, "tokens_used", {"input_tokens": 1000, "output_tokens": 500, "model": "claude-sonnet-4-5-20250929"})

    cli.costs(json_mode=True)

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["total_tokens"] == 1500
    assert data["total_input_tokens"] == 1000
    assert data["total_output_tokens"] == 500
    assert "estimated_cost_usd" in data
    assert "issue_breakdown" in data
    assert "agent_breakdown" in data
    assert "model_breakdown" in data


def test_cli_costs_by_issue(temp_db, tmp_path, capsys):
    """Test costs command filtered by issue."""
    cli = HiveCLI(temp_db, str(tmp_path))

    # Create issues and agents with token usage
    issue1_id = temp_db.create_issue("Issue 1", project=tmp_path.name)
    issue2_id = temp_db.create_issue("Issue 2", project=tmp_path.name)
    agent_id = temp_db.create_agent("test-agent")

    # Add token usage for both issues
    temp_db.log_event(issue1_id, agent_id, "tokens_used", {"input_tokens": 1000, "output_tokens": 500})
    temp_db.log_event(issue2_id, agent_id, "tokens_used", {"input_tokens": 2000, "output_tokens": 1000})

    # Filter by issue1
    cli.costs(issue_id=issue1_id)

    captured = capsys.readouterr()
    assert f"Issue: {issue1_id}" in captured.out
    assert "Total tokens: 1,500" in captured.out


def test_cli_costs_by_agent(temp_db, tmp_path, capsys):
    """Test costs command filtered by agent."""
    cli = HiveCLI(temp_db, str(tmp_path))

    # Create issues and agents with token usage
    issue_id = temp_db.create_issue("Test issue", project=tmp_path.name)
    agent1_id = temp_db.create_agent("agent-1")
    agent2_id = temp_db.create_agent("agent-2")

    # Add token usage for both agents
    temp_db.log_event(issue_id, agent1_id, "tokens_used", {"input_tokens": 1000, "output_tokens": 500})
    temp_db.log_event(issue_id, agent2_id, "tokens_used", {"input_tokens": 2000, "output_tokens": 1000})

    # Filter by agent1
    cli.costs(agent_id=agent1_id)

    captured = capsys.readouterr()
    assert f"Agent: {agent1_id}" in captured.out
    assert "Total tokens: 1,500" in captured.out


def test_cli_watch_no_issue(temp_db, tmp_path, capsys):
    """Test watch command with non-existent issue."""
    cli = HiveCLI(temp_db, str(tmp_path))

    with pytest.raises(SystemExit):
        cli.watch("nonexistent-issue")

    captured = capsys.readouterr()
    assert "Issue nonexistent-issue not found" in captured.err


def test_cli_watch_unassigned_issue(temp_db, tmp_path, capsys):
    """Test watch command with unassigned issue."""
    cli = HiveCLI(temp_db, str(tmp_path))

    # Create an issue without assignee
    issue_id = temp_db.create_issue("Test issue", project=tmp_path.name)

    with pytest.raises(SystemExit):
        cli.watch(issue_id)

    captured = capsys.readouterr()
    assert "is not assigned to any agent" in captured.err


def test_cli_watch_no_session(temp_db, tmp_path, capsys):
    """Test watch command with agent that has no active session."""
    cli = HiveCLI(temp_db, str(tmp_path))

    # Create an issue and agent
    issue_id = temp_db.create_issue("Test issue", project=tmp_path.name)
    agent_id = temp_db.create_agent("test-agent", "idle")

    # Assign issue to agent
    temp_db.claim_issue(issue_id, agent_id)

    with pytest.raises(SystemExit):
        cli.watch(issue_id)

    captured = capsys.readouterr()
    assert "has no active session" in captured.err


# ── Notes CLI tests ─────────────────────────────────────────────


def test_cli_add_note(temp_db, tmp_path, capsys):
    """Test adding a note via CLI."""
    cli = HiveCLI(temp_db, str(tmp_path))

    cli.add_note("Test discovery note")

    captured = capsys.readouterr()
    assert "Added note #" in captured.out
    assert "[discovery]" in captured.out

    # Verify note was stored
    notes = temp_db.get_notes(limit=1)
    assert len(notes) == 1
    assert notes[0]["content"] == "Test discovery note"
    assert notes[0]["category"] == "discovery"


def test_cli_add_note_with_issue_and_category(temp_db, tmp_path, capsys):
    """Test adding a note with issue and category."""
    cli = HiveCLI(temp_db, str(tmp_path))

    issue_id = temp_db.create_issue("Test issue", project=tmp_path.name)
    cli.add_note("Watch out for X", issue_id=issue_id, category="gotcha")

    captured = capsys.readouterr()
    assert "[gotcha]" in captured.out

    notes = temp_db.get_notes(issue_id=issue_id)
    assert len(notes) == 1
    assert notes[0]["category"] == "gotcha"
    assert notes[0]["issue_id"] == issue_id


def test_cli_add_note_json(temp_db, tmp_path, capsys):
    """Test adding a note with JSON output."""
    cli = HiveCLI(temp_db, str(tmp_path))

    cli.add_note("JSON note test", category="pattern", json_mode=True)

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "note_id" in data
    assert data["content"] == "JSON note test"
    assert data["category"] == "pattern"


def test_cli_list_notes(temp_db, tmp_path, capsys):
    """Test listing notes via CLI."""
    cli = HiveCLI(temp_db, str(tmp_path))

    temp_db.add_note(content="Note 1", category="discovery")
    temp_db.add_note(content="Note 2", category="gotcha")
    temp_db.add_note(content="Note 3", category="pattern")

    cli.list_notes()

    captured = capsys.readouterr()
    assert "Note 1" in captured.out
    assert "Note 2" in captured.out
    assert "Note 3" in captured.out
    assert "Total: 3 notes" in captured.out


def test_cli_list_notes_empty(temp_db, tmp_path, capsys):
    """Test listing notes when none exist."""
    cli = HiveCLI(temp_db, str(tmp_path))

    cli.list_notes()

    captured = capsys.readouterr()
    assert "No notes found" in captured.out


def test_cli_list_notes_filter_category(temp_db, tmp_path, capsys):
    """Test listing notes filtered by category."""
    cli = HiveCLI(temp_db, str(tmp_path))

    temp_db.add_note(content="Discovery note", category="discovery")
    temp_db.add_note(content="Gotcha note", category="gotcha")

    cli.list_notes(category="gotcha")

    captured = capsys.readouterr()
    assert "Gotcha note" in captured.out
    assert "Discovery note" not in captured.out
    assert "Total: 1 notes" in captured.out


def test_cli_list_notes_json(temp_db, tmp_path, capsys):
    """Test listing notes with JSON output."""
    cli = HiveCLI(temp_db, str(tmp_path))

    temp_db.add_note(content="JSON list note", category="discovery")

    cli.list_notes(json_mode=True)

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["count"] == 1
    assert data["notes"][0]["content"] == "JSON list note"


@unittest.mock.patch("hive.cli.asyncio.run")
def test_cli_watch_valid_issue(mock_asyncio_run, temp_db, tmp_path):
    """Test watch command with valid issue assignment."""
    cli = HiveCLI(temp_db, str(tmp_path))

    # Create an issue and agent with session
    issue_id = temp_db.create_issue("Test issue", project=tmp_path.name)
    agent_id = temp_db.create_agent("test-agent", "working")

    # Set agent session_id and worktree
    temp_db.conn.execute("UPDATE agents SET session_id = ?, worktree = ? WHERE id = ?", ("test-session", "/test/worktree", agent_id))
    temp_db.conn.commit()

    # Assign issue to agent
    temp_db.claim_issue(issue_id, agent_id)

    # Call watch
    cli.watch(issue_id)

    # Verify asyncio.run was called
    mock_asyncio_run.assert_called_once()

    # Get the arguments passed to asyncio.run (should be a coroutine)
    call_args = mock_asyncio_run.call_args[0][0]
    assert hasattr(call_args, "__await__")  # It's a coroutine


# ── Setup wizard tests ──────────────────────────────────────────


def test_setup_creates_config(tmp_path, capsys):
    """Test setup wizard creates .hive.toml with claude backend."""
    from hive.cli import _do_setup

    _do_setup(tmp_path, tmp_path.name, json_mode=True)

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["claude_cli"] is not None  # bool
    assert "config_created" in data

    config = (tmp_path / ".hive.toml").read_text()
    assert 'backend = "claude"' in config
    assert tmp_path.name in config


def test_setup_skips_existing_config(tmp_path, capsys):
    """Test setup wizard doesn't overwrite existing config."""
    from hive.cli import _do_setup

    (tmp_path / ".hive.toml").write_text('[hive]\nbackend = "opencode"\n')

    _do_setup(tmp_path, tmp_path.name, json_mode=True)

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["config_exists"] is True
    assert "config_created" not in data

    # Original content preserved
    assert "opencode" in (tmp_path / ".hive.toml").read_text()


def test_setup_interactive_with_test_command(tmp_path, capsys, monkeypatch):
    """Test interactive setup reads test command from input."""
    from hive.cli import _do_setup

    # Make it look like a git repo
    (tmp_path / ".git").mkdir()

    monkeypatch.setattr("builtins.input", lambda prompt: "pytest tests/")

    _do_setup(tmp_path, tmp_path.name)

    config = (tmp_path / ".hive.toml").read_text()
    assert 'test_command = "pytest tests/"' in config
    assert 'backend = "claude"' in config

    captured = capsys.readouterr()
    assert "Next steps:" in captured.out
    assert "hive create" in captured.out


def test_setup_interactive_no_test_command(tmp_path, capsys, monkeypatch):
    """Test interactive setup with blank test command."""
    from hive.cli import _do_setup

    monkeypatch.setattr("builtins.input", lambda prompt: "")

    _do_setup(tmp_path, tmp_path.name)

    config = (tmp_path / ".hive.toml").read_text()
    assert "test_command" not in config
    assert 'backend = "claude"' in config


# ── Two-tier help tests ─────────────────────────────────────────


def test_help_shows_essential_commands(capsys):
    """Test that hive -h shows only essential commands."""

    with pytest.raises(SystemExit) as exc_info:
        from hive.cli import main

        import sys

        sys.argv = ["hive", "-h"]
        main()

    assert exc_info.value.code == 0
    captured = capsys.readouterr()

    # Essential commands should be visible
    for cmd in ("setup", "create", "list", "show", "status", "start", "stop", "queen", "doctor"):
        assert cmd in captured.out

    # Hidden commands should NOT appear in the main listing
    # (they're only in the epilog)
    lines = captured.out.split("\n")
    main_section = []
    for line in lines:
        if "advanced commands:" in line:
            break
        main_section.append(line)
    main_text = "\n".join(main_section)

    for cmd in ("molecule", "agents", "merges", "costs", "metrics"):
        # These should only appear in the epilog, not in the main command listing
        assert cmd not in main_text or cmd in ("status",)  # status is essential


def test_hidden_commands_in_epilog(capsys):
    """Test that hidden commands are listed in the epilog."""
    with pytest.raises(SystemExit):
        import sys

        sys.argv = ["hive", "-h"]
        from hive.cli import main

        main()

    captured = capsys.readouterr()
    assert "advanced commands:" in captured.out
    assert "hive <command> -h" in captured.out


# ── Smart no-args tests ────────────────────────────────────────


def test_smart_noargs_no_issues_no_config(temp_db, tmp_path, capsys):
    """Test bare hive with no issues and no .hive.toml shows getting-started guide."""
    from hive.cli import _smart_noargs

    cli = HiveCLI(temp_db, str(tmp_path))
    _smart_noargs(cli, tmp_path, tmp_path.name)

    captured = capsys.readouterr()
    assert "Get started:" in captured.out
    assert "hive setup" in captured.out
    assert "hive create" in captured.out


def test_smart_noargs_no_issues_with_config(temp_db, tmp_path, capsys):
    """Test bare hive with config but no issues shows abbreviated guide."""
    from hive.cli import _smart_noargs

    (tmp_path / ".hive.toml").write_text("[hive]\n")
    cli = HiveCLI(temp_db, str(tmp_path))
    _smart_noargs(cli, tmp_path, tmp_path.name)

    captured = capsys.readouterr()
    assert "No issues yet" in captured.out
    assert "hive create" in captured.out
    assert "hive setup" not in captured.out


def test_smart_noargs_issues_no_daemon(temp_db, tmp_path, capsys):
    """Test bare hive with issues but no daemon shows status + start hint."""
    from hive.cli import _smart_noargs

    temp_db.create_issue("Test issue", project=tmp_path.name)
    cli = HiveCLI(temp_db, str(tmp_path))
    _smart_noargs(cli, tmp_path, tmp_path.name)

    captured = capsys.readouterr()
    assert "Hive Status" in captured.out
    assert "hive start" in captured.out


def test_smart_noargs_json_new_project(temp_db, tmp_path, capsys):
    """Test bare hive with --json and no issues."""
    from hive.cli import _smart_noargs

    cli = HiveCLI(temp_db, str(tmp_path))
    _smart_noargs(cli, tmp_path, tmp_path.name, json_mode=True)

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["state"] == "new_project"
    assert data["total_issues"] == 0


# ── Smart empty state tests ────────────────────────────────────


def test_list_empty_suggests_create(temp_db, tmp_path, capsys):
    """Test that empty list suggests hive create."""
    cli = HiveCLI(temp_db, str(tmp_path))
    cli.list_issues()

    captured = capsys.readouterr()
    assert "No issues found." in captured.out
    assert "hive create" in captured.out


def test_ready_empty_suggests_start(temp_db, tmp_path, capsys):
    """Test that empty ready queue mentions daemon when not running."""
    cli = HiveCLI(temp_db, str(tmp_path))
    cli.show_ready()

    captured = capsys.readouterr()
    assert "No ready issues." in captured.out
    assert "hive start" in captured.out


def test_status_no_issues_suggests_create(temp_db, tmp_path, capsys):
    """Test that status with 0 issues suggests hive create."""
    cli = HiveCLI(temp_db, str(tmp_path))
    cli.status()

    captured = capsys.readouterr()
    assert "No issues yet" in captured.out
    assert "hive create" in captured.out


def test_status_with_issues_no_hint(temp_db, tmp_path, capsys):
    """Test that status with issues does NOT show create hint."""
    cli = HiveCLI(temp_db, str(tmp_path))
    temp_db.create_issue("Existing issue", project=tmp_path.name)
    cli.status()

    captured = capsys.readouterr()
    assert "No issues yet" not in captured.out
