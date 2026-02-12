"""Tests for CLI interface."""

import json

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


def test_cli_close_issue(temp_db, tmp_path):
    """Test closing an issue."""
    cli = HiveCLI(temp_db, str(tmp_path))

    issue_id = temp_db.create_issue("Test issue", project=tmp_path.name)

    cli.close(issue_id)

    # Verify issue was closed
    issue = temp_db.get_issue(issue_id)
    assert issue["status"] == "canceled"


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
