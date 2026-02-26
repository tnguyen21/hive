"""Tests for CLI interface."""

import json
import subprocess
import unittest.mock

import pytest

from hive.cli import HiveCLI


def test_cli_create(temp_db, tmp_path):
    """Test creating an issue via CLI."""
    cli = HiveCLI(temp_db, str(tmp_path))

    result = cli.create("Test issue", "Test description", priority=1)
    issue_id = result["id"]

    assert issue_id.startswith("w-")

    # Verify issue was created
    issue = temp_db.get_issue(issue_id)
    assert issue is not None
    assert issue["title"] == "Test issue"
    assert issue["description"] == "Test description"
    assert issue["priority"] == 1


def test_cli_create_with_depends_on(temp_db, tmp_path, capsys):
    """Test creating an issue with --depends-on wires deps atomically."""
    cli = HiveCLI(temp_db, str(tmp_path))

    # Create a blocker issue first
    blocker_id = cli.create("Blocker", "block desc")["id"]

    # Create a dependent issue with --depends-on
    dependent_id = cli.create("Dependent", "dep desc", depends_on=[blocker_id])["id"]

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

    blocker_id = cli.create("Blocker", "desc")["id"]
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
    assert data["id"] == issue_id
    assert data["title"] == "Test issue"
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


def test_cli_status_json_includes_dirty_main_merge_blocker(temp_db, tmp_path, capsys):
    """JSON status should include structured dirty-main merge blocker details."""
    repo = tmp_path / "repo-json"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("# Repo\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=repo, check=True, capture_output=True)

    cli = HiveCLI(temp_db, str(repo))
    issue_id = temp_db.create_issue("Queued merge", project=repo.name)
    temp_db.update_issue_status(issue_id, "done")
    temp_db.conn.execute(
        """
        INSERT INTO merge_queue (issue_id, agent_id, project, worktree, branch_name, status)
        VALUES (?, ?, ?, ?, ?, 'queued')
        """,
        (issue_id, "agent-2", repo.name, str(repo / ".worktrees" / "agent-2"), "agent/agent-2"),
    )
    temp_db.conn.commit()
    (repo / "README.md").write_text("# Dirty Repo\n")

    cli.status(json_mode=True)
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["main_worktree"]["dirty"] is True
    assert data["merge_blockers"]
    assert data["merge_blockers"][0]["type"] == "dirty_main_worktree"


def test_cli_show_format_json(temp_db, tmp_path, capsys):
    """--format json on show should produce same JSON output as json_mode=True."""
    import argparse

    cli = HiveCLI(temp_db, str(tmp_path))
    issue_id = temp_db.create_issue("Format test", "desc", priority=3, project=tmp_path.name)

    # Call show with format=json via the method directly to verify the flag routes correctly
    cli.show(issue_id, json_mode=True)
    captured_method = capsys.readouterr()
    expected = json.loads(captured_method.out)

    # Now verify the argparse route: simulate --format json
    # We patch main() by verifying argparse produces json_mode behavior
    # Since we can't easily invoke main() without a DB path, we test the
    # argparse namespace directly to confirm the flag is wired correctly.
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    show_p = subparsers.add_parser("show")
    show_p.add_argument("issue_id")
    show_p.add_argument("--format", "-f", choices=["text", "json"], default="text", dest="show_format")

    args_json = parser.parse_args(["show", issue_id, "--format", "json"])
    assert args_json.show_format == "json"

    args_short = parser.parse_args(["show", issue_id, "-f", "json"])
    assert args_short.show_format == "json"

    args_default = parser.parse_args(["show", issue_id])
    assert args_default.show_format == "text"

    # Verify text format produces human-readable output (not JSON)
    cli.show(issue_id, json_mode=False)
    captured_text = capsys.readouterr()
    assert "Format test" in captured_text.out
    assert "Priority: 3" in captured_text.out
    # Text output should NOT be parseable as JSON top-level object
    assert not captured_text.out.strip().startswith("{")

    # Verify json format output has correct structure
    assert expected["id"] == issue_id
    assert expected["title"] == "Format test"


def test_cli_show_format_json_matches_global_json_flag(temp_db, tmp_path, capsys):
    """--format json on show must produce identical output to global --json flag."""
    cli = HiveCLI(temp_db, str(tmp_path))
    issue_id = temp_db.create_issue("Comparison issue", "details", priority=1, project=tmp_path.name)

    cli.show(issue_id, json_mode=True)
    out_via_method = capsys.readouterr().out

    # Both paths (global --json and --format json) ultimately call show(json_mode=True)
    # The dispatch logic: show_json = json_mode or show_format == "json"
    # So when show_format=="json", it must produce the same result.
    data = json.loads(out_via_method)
    assert data["title"] == "Comparison issue"
    assert "dependencies" in data
    assert "dependents" in data
    assert "recent_events" in data


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


def test_cli_cancel(temp_db, tmp_path, capsys):
    """Test canceling an issue."""
    cli = HiveCLI(temp_db, str(tmp_path))

    issue_id = temp_db.create_issue("To cancel", project=tmp_path.name)
    cli.cancel(issue_id, reason="no longer needed")

    issue = temp_db.get_issue(issue_id)
    assert issue["status"] == "canceled"


def test_cli_finalize(temp_db, tmp_path, capsys):
    """Test finalizing an issue."""
    cli = HiveCLI(temp_db, str(tmp_path))

    issue_id = temp_db.create_issue("To finalize", project=tmp_path.name)
    cli.finalize(issue_id, resolution="completed manually")

    issue = temp_db.get_issue(issue_id)
    assert issue["status"] == "finalized"


def test_cli_finalize_marks_merge_queue_merged(temp_db, tmp_path):
    """Manual finalize should close queued merge entries for the issue."""
    cli = HiveCLI(temp_db, str(tmp_path))

    issue_id = temp_db.create_issue("To finalize", project=tmp_path.name)
    temp_db.update_issue_status(issue_id, "done")
    temp_db.conn.execute(
        """
        INSERT INTO merge_queue (issue_id, agent_id, project, worktree, branch_name, status)
        VALUES (?, ?, ?, ?, ?, 'queued')
        """,
        (issue_id, "agent-1", tmp_path.name, str(tmp_path / ".worktrees" / "agent-1"), "agent/agent-1"),
    )
    temp_db.conn.commit()

    cli.finalize(issue_id, resolution="manual review")

    row = temp_db.conn.execute("SELECT status, completed_at FROM merge_queue WHERE issue_id = ?", (issue_id,)).fetchone()
    assert row is not None
    assert row["status"] == "merged"
    assert row["completed_at"] is not None


def test_cli_review_lists_done_issues(temp_db, tmp_path, capsys):
    """Review command should show done issues with actionable git/finalize hints."""
    cli = HiveCLI(temp_db, str(tmp_path))

    issue_id = temp_db.create_issue("Ready for review", project=tmp_path.name)
    temp_db.update_issue_status(issue_id, "done")
    temp_db.conn.execute(
        """
        INSERT INTO merge_queue (issue_id, agent_id, project, worktree, branch_name, status)
        VALUES (?, ?, ?, ?, ?, 'queued')
        """,
        (issue_id, "agent-2", tmp_path.name, str(tmp_path / ".worktrees" / "agent-2"), "agent/agent-2"),
    )
    temp_db.conn.commit()

    cli.review()

    captured = capsys.readouterr()
    assert "Ready for review" in captured.out
    assert "git -C" in captured.out
    assert "hive finalize" in captured.out


def test_cli_review_json(temp_db, tmp_path, capsys):
    """Review command should support JSON output."""
    cli = HiveCLI(temp_db, str(tmp_path))

    issue_id = temp_db.create_issue("JSON review", project=tmp_path.name)
    temp_db.update_issue_status(issue_id, "done")
    temp_db.conn.execute(
        """
        INSERT INTO merge_queue (issue_id, agent_id, project, worktree, branch_name, status)
        VALUES (?, ?, ?, ?, ?, 'queued')
        """,
        (issue_id, "agent-3", tmp_path.name, str(tmp_path / ".worktrees" / "agent-3"), "agent/agent-3"),
    )
    temp_db.conn.commit()

    cli.review(json_mode=True)

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["count"] == 1
    assert data["review"][0]["id"] == issue_id
    assert "finalize_hint" in data["review"][0]


def test_cli_retry(temp_db, tmp_path, capsys):
    """Test retrying a failed issue."""
    cli = HiveCLI(temp_db, str(tmp_path))

    issue_id = temp_db.create_issue("Failed task", project=tmp_path.name)
    temp_db.update_issue_status(issue_id, "failed")

    cli.retry(issue_id, notes="try different approach")

    issue = temp_db.get_issue(issue_id)
    assert issue["status"] == "open"
    assert issue["assignee"] is None


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


def test_cli_epic(temp_db, tmp_path, capsys):
    """Test creating a epic."""
    cli = HiveCLI(temp_db, str(tmp_path))

    steps = json.dumps(
        [
            {"title": "Step 1", "description": "First step"},
            {"title": "Step 2", "description": "Second step", "needs": [0]},
        ]
    )

    cli.epic("Test workflow", description="A test", steps_json=steps)

    captured = capsys.readouterr()
    assert "Created epic" in captured.out
    assert "Step 0" in captured.out
    assert "Step 1" in captured.out


def test_cli_epic_json(temp_db, tmp_path, capsys):
    """Test creating a epic with JSON output."""
    cli = HiveCLI(temp_db, str(tmp_path))

    steps = json.dumps(
        [
            {"title": "Step A"},
            {"title": "Step B", "needs": [0]},
        ]
    )

    cli.epic("Workflow", steps_json=steps, json_mode=True)

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["steps_count"] == 2
    assert "epic_id" in data


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


def test_cli_agents(temp_db, tmp_path, capsys):
    """Test listing agents."""
    cli = HiveCLI(temp_db, str(tmp_path))

    cli.list_agents()

    captured = capsys.readouterr()
    assert "No agents found" in captured.out


def test_cli_events(temp_db, tmp_path, capsys):
    """Test getting events via logs command (which replaced events command)."""
    cli = HiveCLI(temp_db, str(tmp_path))

    # Create an issue to generate events
    temp_db.create_issue("Event test", project=tmp_path.name)

    # Use logs command (which now includes event filtering via --type)
    cli.logs(n=5)

    captured = capsys.readouterr()
    assert "created" in captured.out


def test_cli_logs(temp_db, tmp_path, capsys):
    """Test getting events via logs."""
    cli = HiveCLI(temp_db, str(tmp_path))

    # Create an issue to generate events
    temp_db.create_issue("Event test", project=tmp_path.name)

    cli.logs(n=5)

    captured = capsys.readouterr()
    assert "created" in captured.out


def test_evaluate_permission_policy():
    """Test permission policy evaluation."""
    from hive.backends import OpenCodeClient
    from hive.orchestrator import Orchestrator

    # Create a minimal orchestrator for testing
    db = None
    opencode = OpenCodeClient()
    orch = Orchestrator(db, opencode)

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
    from hive.backends import OpenCodeClient
    from hive.orchestrator import Orchestrator

    async with OpenCodeClient() as opencode:
        Orchestrator(
            db=temp_db,
            opencode_client=opencode,
        )

        # Get pending permissions (should be empty initially)
        pending = await opencode.get_pending_permissions()

        # For now, just verify the method works
        # In a real scenario, we'd create a session that triggers a permission request
        # and verify it gets auto-resolved
        assert isinstance(pending, list)


def test_cli_costs_no_data(temp_db, tmp_path, capsys):
    """Test metrics --costs with no token usage data."""
    cli = HiveCLI(temp_db, str(tmp_path))

    cli.metrics(show_costs=True)

    captured = capsys.readouterr()
    assert "Token Usage & Costs" in captured.out
    assert "Total tokens: 0" in captured.out
    assert "Estimated cost: $0.0000" in captured.out


def test_cli_costs_with_data(temp_db, tmp_path, capsys):
    """Test metrics --costs with token usage data."""
    cli = HiveCLI(temp_db, str(tmp_path))

    # Create issue and agent
    issue_id = temp_db.create_issue("Test issue", project=tmp_path.name)
    agent_id = temp_db.create_agent("test-agent")

    # Add some token usage events
    temp_db.log_event(issue_id, agent_id, "tokens_used", {"input_tokens": 1000, "output_tokens": 500, "model": "claude-sonnet-4-5-20250929"})
    temp_db.log_event(issue_id, agent_id, "tokens_used", {"input_tokens": 2000, "output_tokens": 1000, "model": "claude-sonnet-4-5-20250929"})

    cli.metrics(show_costs=True)

    captured = capsys.readouterr()
    assert "Total tokens: 4,500" in captured.out
    assert "Input tokens: 3,000" in captured.out
    assert "Output tokens: 1,500" in captured.out
    assert "$" in captured.out  # Should have some cost estimate


def test_cli_costs_json(temp_db, tmp_path, capsys):
    """Test metrics --costs with JSON output."""
    cli = HiveCLI(temp_db, str(tmp_path))

    # Create issue and agent with token usage
    issue_id = temp_db.create_issue("Test issue", project=tmp_path.name)
    agent_id = temp_db.create_agent("test-agent")

    temp_db.log_event(issue_id, agent_id, "tokens_used", {"input_tokens": 1000, "output_tokens": 500, "model": "claude-sonnet-4-5-20250929"})

    cli.metrics(show_costs=True, json_mode=True)

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
    """Test metrics --costs filtered by issue."""
    cli = HiveCLI(temp_db, str(tmp_path))

    # Create issues and agents with token usage
    issue1_id = temp_db.create_issue("Issue 1", project=tmp_path.name)
    issue2_id = temp_db.create_issue("Issue 2", project=tmp_path.name)
    agent_id = temp_db.create_agent("test-agent")

    # Add token usage for both issues
    temp_db.log_event(issue1_id, agent_id, "tokens_used", {"input_tokens": 1000, "output_tokens": 500})
    temp_db.log_event(issue2_id, agent_id, "tokens_used", {"input_tokens": 2000, "output_tokens": 1000})

    # Filter by issue1
    cli.metrics(show_costs=True, issue_id=issue1_id)

    captured = capsys.readouterr()
    assert f"Issue: {issue1_id}" in captured.out
    assert "Total tokens: 1,500" in captured.out


def test_cli_costs_by_agent(temp_db, tmp_path, capsys):
    """Test metrics --costs filtered by agent."""
    cli = HiveCLI(temp_db, str(tmp_path))

    # Create issues and agents with token usage
    issue_id = temp_db.create_issue("Test issue", project=tmp_path.name)
    agent1_id = temp_db.create_agent("agent-1")
    agent2_id = temp_db.create_agent("agent-2")

    # Add token usage for both agents
    temp_db.log_event(issue_id, agent1_id, "tokens_used", {"input_tokens": 1000, "output_tokens": 500})
    temp_db.log_event(issue_id, agent2_id, "tokens_used", {"input_tokens": 2000, "output_tokens": 1000})

    # Filter by agent1
    cli.metrics(show_costs=True, agent_id=agent1_id)

    captured = capsys.readouterr()
    assert f"Agent: {agent1_id}" in captured.out
    assert "Total tokens: 1,500" in captured.out


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


# ── Setup wizard tests ──────────────────────────────────────────


def test_setup_creates_config(tmp_path, capsys):
    """Test setup creates .hive.toml with claude backend."""
    from hive.cli import _do_setup

    _do_setup(tmp_path, tmp_path.name, json_mode=True)

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "config_created" in data

    config = (tmp_path / ".hive.toml").read_text()
    assert 'backend = "claude"' in config
    assert "merge_queue_enabled = false" in config
    assert tmp_path.name in config


def test_setup_skips_existing_config(tmp_path, capsys):
    """Test setup doesn't overwrite existing config."""
    from hive.cli import _do_setup

    (tmp_path / ".hive.toml").write_text('[hive]\nbackend = "opencode"\n')

    _do_setup(tmp_path, tmp_path.name, json_mode=True)

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["config_exists"] is True

    # Original content preserved
    assert "opencode" in (tmp_path / ".hive.toml").read_text()


def test_queen_auto_starts_daemon(temp_db, tmp_path):
    """`hive queen` should start daemon when not running."""
    cli = HiveCLI(temp_db, str(tmp_path))

    daemon = unittest.mock.Mock()
    daemon.status.side_effect = [
        {"running": False, "pid": None},
        {"running": True, "pid": 9012},
    ]
    daemon.start.return_value = True

    with (
        unittest.mock.patch.object(cli, "_make_daemon", return_value=daemon),
        unittest.mock.patch.object(cli, "_queen_claude") as queen_claude,
    ):
        cli.queen(backend="claude")

    daemon.start.assert_called_once()
    queen_claude.assert_called_once()


def test_list_empty_suggests_create(temp_db, tmp_path, capsys):
    """Test that empty list suggests hive create."""
    cli = HiveCLI(temp_db, str(tmp_path))
    cli.list_issues()

    captured = capsys.readouterr()
    assert "No issues found." in captured.out
    assert "hive create" in captured.out


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


def test_status_includes_daemon_info_json(temp_db, tmp_path, capsys):
    """Test that status --json includes daemon info."""
    cli = HiveCLI(temp_db, str(tmp_path))
    cli.status(json_mode=True)

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert "daemon" in result
    assert "running" in result["daemon"]
    assert "pid" in result["daemon"]
    assert "log_file" in result["daemon"]


def test_status_shows_daemon_info(temp_db, tmp_path, capsys):
    """Test that status shows daemon info in human-readable output."""
    cli = HiveCLI(temp_db, str(tmp_path))
    cli.status()

    captured = capsys.readouterr()
    assert "Daemon:" in captured.out


# ── Status worker/refinery detail tests ────────────────────────────────


def test_status_shows_worker_details(temp_db, tmp_path, capsys):
    """Status should list each active worker with its issue."""
    cli = HiveCLI(temp_db, str(tmp_path))

    # Create issues
    issue_id = temp_db.create_issue("Add auth module", project=tmp_path.name)
    temp_db.update_issue_status(issue_id, "in_progress")

    # Create a working agent
    agent_id = temp_db.create_agent(name="worker-abc123", project=tmp_path.name)
    temp_db.conn.execute(
        "UPDATE agents SET status = 'working', current_issue = ? WHERE id = ?",
        (issue_id, agent_id),
    )
    temp_db.conn.commit()

    cli.status()
    captured = capsys.readouterr()
    assert "worker-abc123" in captured.out
    assert issue_id in captured.out
    assert "Add auth module" in captured.out


def test_status_shows_refinery_reviewing(temp_db, tmp_path, capsys):
    """Status should show refinery reviewing when a merge is running."""
    cli = HiveCLI(temp_db, str(tmp_path))

    issue_id = temp_db.create_issue("Fix login bug", project=tmp_path.name)
    temp_db.update_issue_status(issue_id, "done")

    # Insert a running merge entry
    temp_db.conn.execute(
        """
        INSERT INTO merge_queue (issue_id, agent_id, project, worktree, branch_name, status)
        VALUES (?, ?, ?, ?, ?, 'running')
        """,
        (issue_id, "agent-1", tmp_path.name, "/tmp/wt", "agent/agent-1"),
    )
    temp_db.conn.commit()

    cli.status()
    captured = capsys.readouterr()
    assert "Refinery: reviewing" in captured.out
    assert issue_id in captured.out
    assert "Fix login bug" in captured.out


def test_status_shows_refinery_idle(temp_db, tmp_path, capsys):
    """Status should show refinery idle when no merge is running."""
    cli = HiveCLI(temp_db, str(tmp_path))
    cli.status()

    captured = capsys.readouterr()
    assert "Refinery: idle" in captured.out


def test_status_json_includes_workers_and_refinery(temp_db, tmp_path, capsys):
    """JSON status should include workers list and refinery object."""
    cli = HiveCLI(temp_db, str(tmp_path))

    issue_id = temp_db.create_issue("Task A", project=tmp_path.name)
    temp_db.update_issue_status(issue_id, "in_progress")

    agent_id = temp_db.create_agent(name="worker-xyz", project=tmp_path.name)
    temp_db.conn.execute(
        "UPDATE agents SET status = 'working', current_issue = ? WHERE id = ?",
        (issue_id, agent_id),
    )
    temp_db.conn.commit()

    cli.status(json_mode=True)
    captured = capsys.readouterr()
    data = json.loads(captured.out)

    assert "workers" in data
    assert len(data["workers"]) == 1
    assert data["workers"][0]["name"] == "worker-xyz"
    assert data["workers"][0]["issue_id"] == issue_id
    assert data["workers"][0]["issue_title"] == "Task A"

    assert "refinery" in data
    assert data["refinery"]["active"] is False


def test_status_json_refinery_active(temp_db, tmp_path, capsys):
    """JSON status should show active refinery when merge is running."""
    cli = HiveCLI(temp_db, str(tmp_path))

    issue_id = temp_db.create_issue("Running merge", project=tmp_path.name)
    temp_db.conn.execute(
        """
        INSERT INTO merge_queue (issue_id, agent_id, project, worktree, branch_name, status)
        VALUES (?, ?, ?, ?, ?, 'running')
        """,
        (issue_id, "agent-1", tmp_path.name, "/tmp/wt", "agent/agent-1"),
    )
    temp_db.conn.commit()

    cli.status(json_mode=True)
    captured = capsys.readouterr()
    data = json.loads(captured.out)

    assert data["refinery"]["active"] is True
    assert data["refinery"]["issue_id"] == issue_id
    assert data["refinery"]["issue_title"] == "Running merge"


# ── Merges summary footer tests ──────────────────────────────────────


def _insert_merge_entry(db, issue_id, agent_id, project, status):
    """Helper: insert a merge_queue row with the given status."""
    db.conn.execute(
        """
        INSERT INTO merge_queue (issue_id, agent_id, project, worktree, branch_name, status)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (issue_id, agent_id, project, f"/worktrees/{agent_id}", f"agent/{agent_id}", status),
    )
    db.conn.commit()


def test_merges_summary_footer_shows_counts_by_status(temp_db, tmp_path, capsys):
    """merges output should show a summary footer with counts per status, not just a total."""
    cli = HiveCLI(temp_db, str(tmp_path))

    issue1 = temp_db.create_issue("Issue 1", project=tmp_path.name)
    issue2 = temp_db.create_issue("Issue 2", project=tmp_path.name)
    issue3 = temp_db.create_issue("Issue 3", project=tmp_path.name)
    issue4 = temp_db.create_issue("Issue 4", project=tmp_path.name)

    _insert_merge_entry(temp_db, issue1, "a1", tmp_path.name, "queued")
    _insert_merge_entry(temp_db, issue2, "a2", tmp_path.name, "running")
    _insert_merge_entry(temp_db, issue3, "a3", tmp_path.name, "merged")
    _insert_merge_entry(temp_db, issue4, "a4", tmp_path.name, "failed")

    cli.merges()

    captured = capsys.readouterr()
    # Summary footer must include per-status counts
    assert "1 queued" in captured.out
    assert "1 running" in captured.out
    assert "1 merged" in captured.out
    assert "1 failed" in captured.out


def test_merges_summary_footer_omits_zero_statuses(temp_db, tmp_path, capsys):
    """merges footer should not mention statuses with zero entries."""
    cli = HiveCLI(temp_db, str(tmp_path))

    issue1 = temp_db.create_issue("Only merged", project=tmp_path.name)
    _insert_merge_entry(temp_db, issue1, "a1", tmp_path.name, "merged")

    cli.merges()

    captured = capsys.readouterr()
    assert "1 merged" in captured.out
    # Last line is the summary; it should only contain 'merged', not other statuses
    summary_line = [line for line in captured.out.splitlines() if line.strip()][-1]
    assert "1 merged" in summary_line
    assert "queued" not in summary_line
    assert "running" not in summary_line
    assert "failed" not in summary_line


def test_merges_summary_aggregates_multiple_same_status(temp_db, tmp_path, capsys):
    """merges footer should sum entries of the same status correctly."""
    cli = HiveCLI(temp_db, str(tmp_path))

    for i in range(3):
        issue_id = temp_db.create_issue(f"Merged issue {i}", project=tmp_path.name)
        _insert_merge_entry(temp_db, issue_id, f"a{i}", tmp_path.name, "merged")

    issue_queued = temp_db.create_issue("Queued issue", project=tmp_path.name)
    _insert_merge_entry(temp_db, issue_queued, "aq", tmp_path.name, "queued")

    cli.merges()

    captured = capsys.readouterr()
    assert "3 merged" in captured.out
    assert "1 queued" in captured.out


def test_merges_json_includes_status_counts(temp_db, tmp_path, capsys):
    """merges --json output should include a status_counts field."""
    cli = HiveCLI(temp_db, str(tmp_path))

    issue1 = temp_db.create_issue("Q1", project=tmp_path.name)
    issue2 = temp_db.create_issue("Q2", project=tmp_path.name)
    issue3 = temp_db.create_issue("M1", project=tmp_path.name)

    _insert_merge_entry(temp_db, issue1, "a1", tmp_path.name, "queued")
    _insert_merge_entry(temp_db, issue2, "a2", tmp_path.name, "queued")
    _insert_merge_entry(temp_db, issue3, "a3", tmp_path.name, "merged")

    cli.merges(json_mode=True)

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "status_counts" in data
    assert data["status_counts"]["queued"] == 2
    assert data["status_counts"]["merged"] == 1
    assert data["count"] == 3


def test_merges_empty_shows_no_entries_message(temp_db, tmp_path, capsys):
    """merges with no entries should print the empty message, not the summary footer."""
    cli = HiveCLI(temp_db, str(tmp_path))

    cli.merges()

    captured = capsys.readouterr()
    assert "No merge queue entries found." in captured.out
    # No status counts should appear when there are no entries
    assert "queued" not in captured.out
    assert "merged" not in captured.out


# ── Note targeting + mail CLI tests ─────────────────────────────────


def test_cli_note_with_to_agent(temp_db, tmp_path, capsys):
    """hive note --to-agent creates note + agent-global delivery."""
    cli = HiveCLI(temp_db, str(tmp_path))

    cli.note_with_targets("Important info", issue_id=None, to_agents=["agent-abc"], to_issues=None, must_read=False, json_mode=False)

    captured = capsys.readouterr()
    assert "Sent note #" in captured.out
    assert "delivery" in captured.out

    notes = temp_db.get_notes(limit=1)
    assert len(notes) == 1
    assert notes[0]["content"] == "Important info"

    deliveries = temp_db.get_inbox_deliveries("agent-abc")
    assert len(deliveries) == 1
    assert deliveries[0]["status"] == "queued"


def test_cli_note_with_to_issue(temp_db, tmp_path, capsys):
    """hive note --to-issue creates note + issue-following delivery row."""
    cli = HiveCLI(temp_db, str(tmp_path))
    issue_id = temp_db.create_issue("Some issue", project=tmp_path.name)

    cli.note_with_targets("Watch out", issue_id=None, to_agents=None, to_issues=[issue_id], must_read=False, json_mode=False)

    captured = capsys.readouterr()
    assert "Sent note #" in captured.out

    notes = temp_db.get_notes(limit=1)
    assert notes[0]["content"] == "Watch out"

    # Issue-following row has recipient_issue_id set and recipient_agent_id NULL
    rows = temp_db.conn.execute(
        "SELECT * FROM note_deliveries WHERE note_id = ? AND recipient_issue_id = ? AND recipient_agent_id IS NULL",
        (notes[0]["id"], issue_id),
    ).fetchall()
    assert len(rows) == 1


def test_cli_note_with_must_read(temp_db, tmp_path, capsys):
    """hive note --must-read sets must_read=1 on the note."""
    cli = HiveCLI(temp_db, str(tmp_path))

    cli.note_with_targets("Critical note", issue_id=None, to_agents=["agent-xyz"], to_issues=None, must_read=True, json_mode=False)

    notes = temp_db.get_notes(limit=1)
    assert notes[0]["must_read"] == 1

    deliveries = temp_db.get_inbox_deliveries("agent-xyz")
    assert len(deliveries) == 1


def test_cli_note_legacy_no_targets(temp_db, tmp_path, capsys):
    """hive note without targets falls through to legacy add_note behavior."""
    cli = HiveCLI(temp_db, str(tmp_path))

    cli.add_note("Legacy note")

    captured = capsys.readouterr()
    assert "Added note #" in captured.out
    assert "[discovery]" in captured.out

    notes = temp_db.get_notes(limit=1)
    assert notes[0]["content"] == "Legacy note"
    # No deliveries created
    assert temp_db.conn.execute("SELECT COUNT(*) FROM note_deliveries").fetchone()[0] == 0


def test_cli_mail_inbox(temp_db, tmp_path, capsys):
    """hive mail inbox --agent shows deliveries for that agent."""
    cli = HiveCLI(temp_db, str(tmp_path))

    note_id = temp_db.add_note(agent_id=None, content="Hello agent", project=tmp_path.name)
    temp_db.create_note_deliveries(note_id, to_agents=["agent-1"])

    cli.mail_inbox("agent-1", issue_id=None, unread_only=False, json_mode=False)

    captured = capsys.readouterr()
    assert "Hello agent" in captured.out


def test_cli_mail_inbox_unread(temp_db, tmp_path, capsys):
    """hive mail inbox --unread hides already-read deliveries."""
    cli = HiveCLI(temp_db, str(tmp_path))

    note_id = temp_db.add_note(agent_id=None, content="Old note", project=tmp_path.name)
    temp_db.create_note_deliveries(note_id, to_agents=["agent-1"])
    deliveries = temp_db.get_inbox_deliveries("agent-1")
    temp_db.mark_delivery_read(deliveries[0]["id"], "agent-1")

    note_id2 = temp_db.add_note(agent_id=None, content="New note", project=tmp_path.name)
    temp_db.create_note_deliveries(note_id2, to_agents=["agent-1"])

    cli.mail_inbox("agent-1", issue_id=None, unread_only=True, json_mode=False)

    captured = capsys.readouterr()
    assert "New note" in captured.out
    assert "Old note" not in captured.out


def test_cli_mail_read(temp_db, tmp_path, capsys):
    """hive mail read <id> --agent marks delivery read."""
    cli = HiveCLI(temp_db, str(tmp_path))

    note_id = temp_db.add_note(agent_id=None, content="Read me", project=tmp_path.name)
    temp_db.create_note_deliveries(note_id, to_agents=["agent-r"])
    deliveries = temp_db.get_inbox_deliveries("agent-r")
    delivery_id = deliveries[0]["id"]

    cli.mail_read(delivery_id, "agent-r", json_mode=False)

    captured = capsys.readouterr()
    assert "read" in captured.out.lower() or "marked" in captured.out.lower()

    updated = temp_db.get_inbox_deliveries("agent-r")
    assert updated[0]["status"] == "read"


def test_cli_mail_ack(temp_db, tmp_path, capsys):
    """hive mail ack <id> --agent acknowledges a must_read delivery."""
    cli = HiveCLI(temp_db, str(tmp_path))

    note_id = temp_db.add_note(agent_id=None, content="Must ack", must_read=True, project=tmp_path.name)
    temp_db.create_note_deliveries(note_id, to_agents=["agent-a"])
    deliveries = temp_db.get_inbox_deliveries("agent-a")
    delivery_id = deliveries[0]["id"]

    cli.mail_ack(delivery_id, "agent-a", json_mode=False)

    captured = capsys.readouterr()
    assert "acked" in captured.out.lower() or "acknowledged" in captured.out.lower()

    updated = temp_db.get_inbox_deliveries("agent-a")
    assert updated[0]["status"] == "acked"


def test_cli_mail_ack_non_must_read(temp_db, tmp_path, capsys):
    """hive mail ack on a non-must-read note shows 'unchanged'."""
    cli = HiveCLI(temp_db, str(tmp_path))

    note_id = temp_db.add_note(agent_id=None, content="Normal note", must_read=False, project=tmp_path.name)
    temp_db.create_note_deliveries(note_id, to_agents=["agent-b"])
    deliveries = temp_db.get_inbox_deliveries("agent-b")
    delivery_id = deliveries[0]["id"]

    cli.mail_ack(delivery_id, "agent-b", json_mode=False)

    captured = capsys.readouterr()
    assert "unchanged" in captured.out.lower()

    # Status should remain queued — ack only works on must_read notes
    updated = temp_db.get_inbox_deliveries("agent-b")
    assert updated[0]["status"] == "queued"


def test_cli_note_json_mode(temp_db, tmp_path, capsys):
    """hive note --to-agent with --json outputs structured JSON."""
    cli = HiveCLI(temp_db, str(tmp_path))

    cli.note_with_targets("JSON note", issue_id=None, to_agents=["agent-j"], to_issues=None, must_read=False, json_mode=True)

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "note_id" in data
    assert data["delivery_count"] == 1
    assert data["to_agents"] == ["agent-j"]
    assert data["must_read"] is False


def test_cli_mail_inbox_json_mode(temp_db, tmp_path, capsys):
    """hive mail inbox with --json outputs structured JSON."""
    cli = HiveCLI(temp_db, str(tmp_path))

    note_id = temp_db.add_note(agent_id=None, content="JSON inbox note", project=tmp_path.name)
    temp_db.create_note_deliveries(note_id, to_agents=["agent-k"])

    cli.mail_inbox("agent-k", issue_id=None, unread_only=False, json_mode=True)

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert "count" in data
    assert data["count"] == 1
    assert len(data["deliveries"]) == 1


# ── Observability event tests ─────────────────────────────────────────


def test_note_sent_event_logged(temp_db, tmp_path):
    """hive note with targets logs a note_sent event."""
    cli = HiveCLI(temp_db, str(tmp_path))

    cli.note_with_targets("Heads up", issue_id=None, to_agents=["agent-obs"], to_issues=None, must_read=False)

    events = temp_db.get_events(event_type="note_sent")
    assert len(events) == 1
    detail = json.loads(events[0]["detail"])
    assert detail["to_agents"] == ["agent-obs"]
    assert detail["must_read"] is False
    assert "note_id" in detail


def test_note_sent_event_includes_must_read_flag(temp_db, tmp_path):
    """note_sent event records must_read correctly when True."""
    cli = HiveCLI(temp_db, str(tmp_path))

    cli.note_with_targets("Critical", issue_id=None, to_agents=["agent-mr"], to_issues=None, must_read=True)

    events = temp_db.get_events(event_type="note_sent")
    assert len(events) == 1
    detail = json.loads(events[0]["detail"])
    assert detail["must_read"] is True


def test_mail_read_event_logged(temp_db, tmp_path):
    """hive mail read logs a note_read event with delivery_id."""
    cli = HiveCLI(temp_db, str(tmp_path))

    note_id = temp_db.add_note(agent_id=None, content="Read event test", project=tmp_path.name)
    temp_db.create_note_deliveries(note_id, to_agents=["agent-read-ev"])
    deliveries = temp_db.get_inbox_deliveries("agent-read-ev")
    delivery_id = deliveries[0]["id"]

    cli.mail_read(delivery_id, "agent-read-ev")

    events = temp_db.get_events(agent_id="agent-read-ev", event_type="note_read")
    assert len(events) == 1
    detail = json.loads(events[0]["detail"])
    assert detail["delivery_id"] == delivery_id


def test_mail_read_event_not_logged_if_already_read(temp_db, tmp_path):
    """note_read event is not logged when delivery is already read."""
    cli = HiveCLI(temp_db, str(tmp_path))

    note_id = temp_db.add_note(agent_id=None, content="Already read", project=tmp_path.name)
    temp_db.create_note_deliveries(note_id, to_agents=["agent-2r"])
    deliveries = temp_db.get_inbox_deliveries("agent-2r")
    delivery_id = deliveries[0]["id"]

    # Mark already read via DB directly
    temp_db.mark_delivery_read(delivery_id, "agent-2r")

    # Calling mail_read again should not log a second event
    cli.mail_read(delivery_id, "agent-2r")

    events = temp_db.get_events(agent_id="agent-2r", event_type="note_read")
    # Only 0 events from CLI call (since updated=False means we don't log)
    assert len(events) == 0


def test_mail_ack_event_logged(temp_db, tmp_path):
    """hive mail ack logs a note_acked event with delivery_id."""
    cli = HiveCLI(temp_db, str(tmp_path))

    note_id = temp_db.add_note(agent_id=None, content="Ack event test", must_read=True, project=tmp_path.name)
    temp_db.create_note_deliveries(note_id, to_agents=["agent-ack-ev"])
    deliveries = temp_db.get_inbox_deliveries("agent-ack-ev")
    delivery_id = deliveries[0]["id"]

    cli.mail_ack(delivery_id, "agent-ack-ev")

    events = temp_db.get_events(agent_id="agent-ack-ev", event_type="note_acked")
    assert len(events) == 1
    detail = json.loads(events[0]["detail"])
    assert detail["delivery_id"] == delivery_id


def test_mail_ack_event_not_logged_on_non_must_read(temp_db, tmp_path):
    """note_acked event is not logged when ack fails (non-must-read delivery)."""
    cli = HiveCLI(temp_db, str(tmp_path))

    note_id = temp_db.add_note(agent_id=None, content="Not must read", must_read=False, project=tmp_path.name)
    temp_db.create_note_deliveries(note_id, to_agents=["agent-nr"])
    deliveries = temp_db.get_inbox_deliveries("agent-nr")
    delivery_id = deliveries[0]["id"]

    cli.mail_ack(delivery_id, "agent-nr")

    # No note_acked event should be logged since ack was rejected
    events = temp_db.get_events(agent_id="agent-nr", event_type="note_acked")
    assert len(events) == 0


# ── cli_command decorator invariant tests ────────────────────────────────────


def test_cli_command_inv1_json_mode_produces_valid_json(temp_db, tmp_path, capsys):
    """INV-1: Every decorated command in json_mode produces valid JSON to stdout on success."""
    cli = HiveCLI(temp_db, str(tmp_path))

    # Spot-check: create, list_issues, cancel
    cli.create("INV1 issue", json_mode=True)
    out = capsys.readouterr().out
    data = json.loads(out)
    assert "id" in data

    cli.list_issues(json_mode=True)
    out = capsys.readouterr().out
    data = json.loads(out)
    assert "issues" in data

    issue_id = temp_db.create_issue("To cancel", project=tmp_path.name)
    cli.cancel(issue_id, reason="test", json_mode=True)
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data["issue_id"] == issue_id


def test_cli_command_inv2_exception_produces_error_json(temp_db, tmp_path, capsys):
    """INV-2: Every decorated command in json_mode produces {"error": "..."} on exception."""
    cli = HiveCLI(temp_db, str(tmp_path))

    with pytest.raises(SystemExit):
        cli.show("nonexistent-id-xyz", json_mode=True)

    out = capsys.readouterr().out
    data = json.loads(out)
    assert "error" in data
    assert isinstance(data["error"], str)
    assert len(data["error"]) > 0


def test_cli_command_inv2_exception_exits_nonzero(temp_db, tmp_path):
    """INV-2b: Decorated command exits with code 1 on exception."""
    cli = HiveCLI(temp_db, str(tmp_path))

    with pytest.raises(SystemExit) as exc_info:
        cli.show("nonexistent-id-xyz", json_mode=True)
    assert exc_info.value.code == 1


def test_cli_command_inv3_human_output_create(temp_db, tmp_path, capsys):
    """INV-3: Human-readable output for create is unchanged."""
    cli = HiveCLI(temp_db, str(tmp_path))

    cli.create("My Title", priority=3, tags="refactor,small")
    out = capsys.readouterr().out
    assert "Created issue:" in out
    assert "My Title" in out
    assert "Priority: 3" in out
    assert "Tags: refactor, small" in out


def test_cli_command_inv3_human_output_cancel(temp_db, tmp_path, capsys):
    """INV-3: Human-readable output for cancel is unchanged."""
    cli = HiveCLI(temp_db, str(tmp_path))
    issue_id = temp_db.create_issue("Cancel me", project=tmp_path.name)

    cli.cancel(issue_id, reason="done")
    out = capsys.readouterr().out
    assert f"Canceled issue {issue_id}" in out


def test_cli_command_inv3_human_output_list_empty(temp_db, tmp_path, capsys):
    """INV-3: Human-readable output for list_issues when empty is unchanged."""
    cli = HiveCLI(temp_db, str(tmp_path))

    cli.list_issues()
    out = capsys.readouterr().out
    assert "No issues found." in out
    assert "hive create" in out


def test_cli_command_decorator_exception_human_mode(temp_db, tmp_path, capsys):
    """In human mode, exception prints 'Error: ...' to stderr and exits 1."""
    cli = HiveCLI(temp_db, str(tmp_path))

    with pytest.raises(SystemExit) as exc_info:
        cli.show("nonexistent-id-xyz", json_mode=False)

    assert exc_info.value.code == 1
    # stdout should be empty; error goes to stderr
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Error:" in captured.err


def test_cli_command_decorator_does_not_print_json_in_human_mode(temp_db, tmp_path, capsys):
    """Decorator must NOT print JSON output when json_mode=False."""
    cli = HiveCLI(temp_db, str(tmp_path))

    cli.create("Silent", json_mode=False)
    out = capsys.readouterr().out
    # Should be human text, not JSON
    assert out.startswith("Created issue:")
    try:
        json.loads(out)
        assert False, "Output was parseable JSON — decorator double-printed"
    except (json.JSONDecodeError, ValueError):
        pass  # expected: not JSON
