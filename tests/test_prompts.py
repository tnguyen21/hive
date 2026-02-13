"""Tests for prompt templates and completion assessment."""

import pytest

from hive.prompts import (
    assess_completion,
    build_refinery_prompt,
    build_system_prompt,
    build_worker_prompt,
    parse_merge_result,
)


def test_build_worker_prompt_basic():
    """Test building a basic worker prompt."""
    issue = {"title": "Test Issue", "description": "Test description"}

    prompt = build_worker_prompt(
        agent_name="test-agent",
        issue=issue,
        worktree_path="/tmp/worktree",
        branch_name="agent/test-agent",
        project="test-project",
    )

    assert "test-agent" in prompt
    assert "Test Issue" in prompt
    assert "Test description" in prompt
    assert "/tmp/worktree" in prompt
    assert "agent/test-agent" in prompt
    assert "BEHAVIORAL CONTRACT" in prompt
    assert "No Approval Fallacy" in prompt
    assert "Directory Discipline" in prompt
    assert "COMPLETION SIGNAL" in prompt
    # Behavioral principles
    assert "Propulsion Principle" in prompt
    assert "Escalate and Move On" in prompt


def test_build_worker_prompt_with_molecule():
    """Test building a worker prompt for a molecule step."""
    issue = {"title": "Step 1", "description": "First step"}

    prompt = build_worker_prompt(
        agent_name="test-agent",
        issue=issue,
        worktree_path="/tmp/worktree",
        branch_name="agent/test-agent",
        project="test-project",
        step_number=1,
        total_steps=3,
        molecule_title="Multi-step workflow",
        completed_steps=["Step 0: Setup complete"],
    )

    assert "step 1 of 3" in prompt
    assert "Multi-step workflow" in prompt
    assert "Previous Steps (already completed)" in prompt
    assert "Step 0: Setup complete" in prompt


def test_build_system_prompt():
    """Test building system prompt."""
    prompt = build_system_prompt(project="test-project", agent_name="test-agent", worktree_path=None)

    assert "test-agent" in prompt
    assert "test-project" in prompt
    assert "autonomously" in prompt
    assert "without human interaction" in prompt
    assert "piston" in prompt
    assert "approval" in prompt.lower()


def test_assess_completion_structured_success():
    """Test assessing completion with structured success signal."""
    messages = [
        {
            "parts": [
                {
                    "type": "text",
                    "text": """I've completed the task.

:::COMPLETION
status: success
summary: Implemented authentication middleware
files_changed: 3
tests_run: yes
blockers: none
artifacts:
  - type: git_commit
    value: abc123def456
  - type: test_result
    value: pass
:::""",
                }
            ]
        }
    ]

    result = assess_completion(messages)

    assert result.success is True
    assert result.summary == "Implemented authentication middleware"
    assert result.git_commit == "abc123def456"
    assert result.artifacts["test_result"] == "pass"


def test_assess_completion_structured_blocked():
    """Test assessing completion with structured blocked signal."""
    messages = [
        {
            "parts": [
                {
                    "type": "text",
                    "text": """I'm blocked.

:::COMPLETION
status: blocked
summary: Cannot proceed without database schema
files_changed: 0
tests_run: no
blockers: Missing database schema definition
:::""",
                }
            ]
        }
    ]

    result = assess_completion(messages)

    assert result.success is False
    assert "Missing database schema definition" in result.reason


# --- Refinery prompt and merge result tests ---


def test_build_refinery_prompt_conflict():
    """Test refinery prompt for rebase conflict."""
    prompt = build_refinery_prompt(
        issue_title="Add auth middleware",
        issue_id="w-abc123",
        branch_name="agent/worker-1",
        worktree_path="/tmp/wt1",
        agent_name="worker-1",
        rebase_succeeded=False,
    )

    assert "Refinery" in prompt
    assert "w-abc123" in prompt
    assert "Add auth middleware" in prompt
    assert "agent/worker-1" in prompt
    assert "conflicts detected" in prompt.lower()
    assert "MERGE_RESULT" in prompt


def test_build_refinery_prompt_test_failure():
    """Test refinery prompt for test failure."""
    prompt = build_refinery_prompt(
        issue_title="Fix login bug",
        issue_id="w-def456",
        branch_name="agent/worker-2",
        worktree_path="/tmp/wt2",
        rebase_succeeded=True,
        test_output="FAILED test_login.py::test_auth - AssertionError",
        test_command="pytest tests/",
    )

    assert "TESTS FAILED" in prompt
    assert "pytest tests/" in prompt
    assert "FAILED test_login" in prompt


def test_parse_merge_result_structured_success():
    """Test parsing a structured merge result signal."""
    messages = [
        {
            "parts": [
                {
                    "type": "text",
                    "text": """Resolved the conflict and ran tests.

:::MERGE_RESULT
issue_id: w-abc123
status: merged
summary: Resolved 2 import conflicts, all tests pass
tests_passed: true
conflicts_resolved: 2
:::""",
                }
            ]
        }
    ]

    result = parse_merge_result(messages)
    assert result["status"] == "merged"
    assert result["tests_passed"] is True
    assert result["conflicts_resolved"] == 2
    assert "import conflicts" in result["summary"]


def test_parse_merge_result_structured_rejected():
    """Test parsing a rejected merge result."""
    messages = [
        {
            "parts": [
                {
                    "type": "text",
                    "text": """:::MERGE_RESULT
issue_id: w-abc123
status: rejected
summary: Fundamental incompatibility with new API design
tests_passed: false
conflicts_resolved: 0
:::""",
                }
            ]
        }
    ]

    result = parse_merge_result(messages)
    assert result["status"] == "rejected"
    assert result["tests_passed"] is False


def test_parse_merge_result_heuristic_success():
    """Test heuristic fallback for merge success."""
    messages = [
        {
            "parts": [
                {
                    "type": "text",
                    "text": "Successfully merged the branch after resolving conflicts. All tests pass.",
                }
            ]
        }
    ]

    result = parse_merge_result(messages)
    assert result["status"] == "merged"
    assert result["tests_passed"] is True


def test_parse_merge_result_heuristic_rejected():
    """Test heuristic fallback for merge rejection."""
    messages = [
        {
            "parts": [
                {
                    "type": "text",
                    "text": "Rejecting this branch — the changes are incompatible with the new architecture.",
                }
            ]
        }
    ]

    result = parse_merge_result(messages)
    assert result["status"] == "rejected"


def test_parse_merge_result_empty():
    """Test parse_merge_result with empty messages."""
    result = parse_merge_result([])
    assert result["status"] == "needs_human"


def test_parse_merge_result_no_signal():
    """Test parse_merge_result with no recognizable signal."""
    messages = [
        {
            "parts": [
                {
                    "type": "text",
                    "text": "I looked at the code but I'm not sure what to do.",
                }
            ]
        }
    ]

    result = parse_merge_result(messages)
    assert result["status"] == "needs_human"


def test_assess_completion_heuristic_tool_error():
    """Test heuristic tool error detection."""
    messages = [
        {
            "parts": [
                {"type": "text", "text": "Running tests..."},
                {
                    "type": "tool",
                    "tool": "bash",
                    "state": {"status": "error", "output": "npm test failed"},
                },
            ]
        }
    ]

    result = assess_completion(messages)

    assert result.success is False
    assert "Tool errors" in result.reason


def test_assess_completion_heuristic_success():
    """Test heuristic success detection."""
    messages = [
        {
            "parts": [
                {
                    "type": "text",
                    "text": "I've successfully implemented the feature and all tests are passing. Changes committed.",
                }
            ]
        }
    ]

    result = assess_completion(messages)

    assert result.success is True


def test_assess_completion_optimistic_default():
    """Test optimistic default when no clear signals."""
    messages = [
        {
            "parts": [
                {"type": "text", "text": "Work complete."},
            ]
        }
    ]

    result = assess_completion(messages)

    # Should assume success by default
    assert result.success is True


def test_assess_completion_empty_messages():
    """Test handling empty messages."""
    result = assess_completion([])

    assert result.success is False
    assert "No messages" in result.reason


def test_assess_completion_malformed_yaml():
    """Test handling malformed YAML in completion signal."""
    messages = [
        {
            "parts": [
                {
                    "type": "text",
                    "text": """:::COMPLETION
status: success
  invalid: yaml: structure::
summary: This will fail to parse
:::

But I did complete the task.""",
                }
            ]
        }
    ]

    result = assess_completion(messages)

    # Should fall back to heuristics
    assert result.success is True  # "complete the task" triggers success


def test_assess_completion_multiple_text_parts():
    """Test handling multiple text parts in a message."""
    messages = [
        {
            "parts": [
                {"type": "text", "text": "First part. "},
                {"type": "text", "text": "I am blocked by missing dependencies."},
                {"type": "tool", "tool": "bash", "state": {"status": "completed"}},
            ]
        }
    ]

    result = assess_completion(messages)

    assert result.success is False
    assert "Blocker detected" in result.reason
