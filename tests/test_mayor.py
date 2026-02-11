"""Tests for Mayor functionality."""

import pytest

from hive.prompts import (
    build_mayor_prompt,
    build_mayor_state_summary,
    parse_work_plan,
)


def test_parse_work_plan_success():
    """Test parsing a valid work plan."""
    messages = [
        {
            "parts": [
                {
                    "type": "text",
                    "text": """I'll create a work plan for this request.

:::WORK_PLAN
issues:
  - id: "design"
    title: "Design auth system"
    description: "Create design doc for authentication"
    type: "task"
    priority: 1
    project: "test"

  - id: "implement"
    title: "Implement auth middleware"
    description: "Build JWT validation middleware"
    type: "task"
    priority: 2
    project: "test"
    needs: ["design"]

  - id: "tests"
    title: "Write tests"
    description: "Unit and integration tests"
    type: "task"
    priority: 2
    project: "test"
    needs: ["implement"]
:::

This plan breaks the work into three sequential tasks.""",
                }
            ]
        }
    ]

    work_plan = parse_work_plan(messages)

    assert work_plan is not None
    assert len(work_plan.issues) == 3

    # Check first issue
    assert work_plan.issues[0]["id"] == "design"
    assert work_plan.issues[0]["title"] == "Design auth system"
    assert work_plan.issues[0]["priority"] == 1

    # Check dependencies
    assert work_plan.issues[1]["needs"] == ["design"]
    assert work_plan.issues[2]["needs"] == ["implement"]


def test_parse_work_plan_no_plan():
    """Test parsing when no work plan is present."""
    messages = [
        {
            "parts": [
                {
                    "type": "text",
                    "text": "I need more information to create a work plan.",
                }
            ]
        }
    ]

    work_plan = parse_work_plan(messages)
    assert work_plan is None


def test_parse_work_plan_malformed_yaml():
    """Test parsing malformed YAML in work plan."""
    messages = [
        {
            "parts": [
                {
                    "type": "text",
                    "text": """:::WORK_PLAN
issues:
  - id: "task1
    title: Unclosed quote
    invalid: yaml: structure::
:::""",
                }
            ]
        }
    ]

    work_plan = parse_work_plan(messages)
    assert work_plan is None


def test_parse_work_plan_empty_issues():
    """Test parsing work plan with empty issues list."""
    messages = [
        {
            "parts": [
                {
                    "type": "text",
                    "text": """:::WORK_PLAN
issues: []
:::""",
                }
            ]
        }
    ]

    work_plan = parse_work_plan(messages)
    assert work_plan is not None
    assert len(work_plan.issues) == 0


def test_build_mayor_prompt():
    """Test building Mayor's prompt with state."""
    active_workers = [
        {"name": "worker-1", "current_issue_title": "Implement feature A"},
        {"name": "worker-2", "current_issue_title": "Fix bug B"},
    ]

    open_issues = [
        {"id": "w-123", "title": "Task 1", "priority": 1},
        {"id": "w-456", "title": "Task 2", "priority": 2},
    ]

    recent_completions = [
        {"id": "w-789", "title": "Completed task"},
    ]

    escalations = []

    prompt = build_mayor_prompt(
        project="test-project",
        active_workers=active_workers,
        open_issues=open_issues,
        recent_completions=recent_completions,
        escalations=escalations,
    )

    assert "Mayor" in prompt
    assert "strategic coordinator" in prompt
    assert "worker-1" in prompt
    assert "worker-2" in prompt
    assert "Implement feature A" in prompt
    assert "Task 1" in prompt
    assert "Task 2" in prompt
    assert "Completed task" in prompt
    assert ":::WORK_PLAN" in prompt
    assert "GUIDELINES" in prompt


def test_build_mayor_prompt_empty_state():
    """Test building Mayor's prompt with no active work."""
    prompt = build_mayor_prompt(
        project="test-project",
        active_workers=[],
        open_issues=[],
        recent_completions=[],
        escalations=[],
    )

    assert "Mayor" in prompt
    assert "None" in prompt  # Should show "None" for empty lists


def test_build_mayor_state_summary():
    """Test building state summary for context cycling."""
    active_workers = [
        {"name": "worker-1", "current_issue_title": "Task A"},
    ]

    open_issues = [
        {"id": "w-123", "title": "Open task 1"},
        {"id": "w-456", "title": "Open task 2"},
    ]

    recent_completions = [
        {"id": "w-789", "title": "Completed task"},
    ]

    summary = build_mayor_state_summary(
        active_workers=active_workers,
        open_issues=open_issues,
        recent_completions=recent_completions,
    )

    assert "Active workers: 1" in summary
    assert "worker-1" in summary
    assert "Task A" in summary
    assert "Open issues: 2" in summary
    assert "Open task 1" in summary
    assert "Recently completed" in summary
    assert "Completed task" in summary


def test_parse_work_plan_with_summary():
    """Test parsing work plan with summary field."""
    messages = [
        {
            "parts": [
                {
                    "type": "text",
                    "text": """:::WORK_PLAN
summary: "Breaking down auth system into 3 tasks"
issues:
  - id: "task1"
    title: "Task 1"
    description: "First task"
    type: "task"
    priority: 1
    project: "test"
:::""",
                }
            ]
        }
    ]

    work_plan = parse_work_plan(messages)
    assert work_plan is not None
    assert work_plan.summary == "Breaking down auth system into 3 tasks"


# Integration tests


@pytest.mark.asyncio
@pytest.mark.integration
async def test_create_mayor_session(temp_db, tmp_path):
    """Test creating Mayor session (requires OpenCode server)."""
    from hive.opencode import OpenCodeClient
    from hive.orchestrator import Orchestrator

    async with OpenCodeClient() as opencode:
        orch = Orchestrator(
            db=temp_db,
            opencode_client=opencode,
            project_path=str(tmp_path),
            project_name="test",
        )

        session_id = await orch.create_mayor_session()

        assert session_id is not None
        assert orch.mayor_session_id == session_id

        # Verify session was created
        session = await opencode.get_session(session_id, directory=str(tmp_path))
        assert session["title"] == "mayor"

        # Clean up
        await opencode.delete_session(session_id, directory=str(tmp_path))


@pytest.mark.asyncio
@pytest.mark.integration
async def test_handle_user_request(temp_db, tmp_path):
    """Test handling user request through Mayor (requires OpenCode server)."""
    from hive.opencode import OpenCodeClient
    from hive.orchestrator import Orchestrator

    async with OpenCodeClient() as opencode:
        orch = Orchestrator(
            db=temp_db,
            opencode_client=opencode,
            project_path=str(tmp_path),
            project_name="test",
        )

        await orch.create_mayor_session()

        # Submit a simple request
        user_input = "Create a simple README.md file"

        await orch.handle_user_request(user_input)

        # Check if issues were created
        issues = temp_db.conn.execute(
            "SELECT * FROM issues WHERE project = 'test'"
        ).fetchall()

        # Mayor should have created at least one issue
        # (Actual behavior depends on the LLM response)
        # For now, just verify the request was processed without error

        # Clean up
        await opencode.delete_session(orch.mayor_session_id, directory=str(tmp_path))


@pytest.mark.asyncio
@pytest.mark.integration
async def test_create_issues_from_plan(temp_db, tmp_path):
    """Test creating issues from a work plan."""
    from hive.models import WorkPlan
    from hive.opencode import OpenCodeClient
    from hive.orchestrator import Orchestrator

    async with OpenCodeClient() as opencode:
        orch = Orchestrator(
            db=temp_db,
            opencode_client=opencode,
            project_path=str(tmp_path),
            project_name="test",
        )

        # Create a work plan
        work_plan = WorkPlan(
            issues=[
                {
                    "id": "design",
                    "title": "Design system",
                    "description": "Create design doc",
                    "type": "task",
                    "priority": 1,
                    "project": "test",
                },
                {
                    "id": "implement",
                    "title": "Implement feature",
                    "description": "Build the feature",
                    "type": "task",
                    "priority": 2,
                    "project": "test",
                    "needs": ["design"],
                },
            ]
        )

        await orch.create_issues_from_plan(work_plan)

        # Verify issues were created
        issues = temp_db.conn.execute(
            "SELECT * FROM issues WHERE project = 'test' ORDER BY priority"
        ).fetchall()

        assert len(issues) == 2
        assert dict(issues[0])["title"] == "Design system"
        assert dict(issues[1])["title"] == "Implement feature"

        # Verify dependency was created
        deps = temp_db.conn.execute("SELECT * FROM dependencies").fetchall()
        assert len(deps) == 1
