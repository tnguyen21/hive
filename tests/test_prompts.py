"""Tests for prompt templates and completion assessment."""

import pytest

from hive.prompts import assess_completion, build_system_prompt, build_worker_prompt


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
    # GT behavioral principles
    assert "Propulsion Principle" in prompt
    assert "Idle Worker Heresy" in prompt
    assert "Escalate and Move On" in prompt
    assert "Capability Ledger" in prompt


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
    prompt = build_system_prompt(
        project="test-project", agent_name="test-agent", worktree_path=None
    )

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
    assert result.reason == "Missing database schema definition"
    assert "database schema" in result.summary


def test_assess_completion_structured_failed():
    """Test assessing completion with structured failed signal."""
    messages = [
        {
            "parts": [
                {
                    "type": "text",
                    "text": """Task failed.

:::COMPLETION
status: failed
summary: Tests failing after implementation
files_changed: 5
tests_run: yes
blockers: Unit tests failing - authentication logic incorrect
:::""",
                }
            ]
        }
    ]

    result = assess_completion(messages)

    assert result.success is False
    assert "authentication logic incorrect" in result.reason


def test_assess_completion_heuristic_blocker():
    """Test heuristic blocker detection."""
    messages = [
        {
            "parts": [
                {
                    "type": "text",
                    "text": "I am blocked by missing API credentials and cannot proceed with the implementation.",
                }
            ]
        }
    ]

    result = assess_completion(messages)

    assert result.success is False
    assert "Blocker detected" in result.reason


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
