"""Tests for prompt templates and completion assessment."""

from hive.prompts import (
    assess_completion,
    build_refinery_prompt,
    build_system_prompt,
    build_worker_prompt,
    parse_merge_result,
    read_notes_file,
    remove_notes_file,
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


def test_build_worker_prompt_with_completed_steps():
    """Test building a worker prompt for a molecule step with completed steps."""
    issue = {"title": "Step 1", "description": "First step"}

    prompt = build_worker_prompt(
        agent_name="test-agent",
        issue=issue,
        worktree_path="/tmp/worktree",
        branch_name="agent/test-agent",
        project="test-project",
        completed_steps=["Step 0: Setup complete"],
    )

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


# --- Notes functionality tests ---


def test_read_notes_file_nonexistent(tmp_path):
    """Test reading notes file that doesn't exist."""
    notes = read_notes_file(str(tmp_path))
    assert notes == []


def test_read_notes_file_empty(tmp_path):
    """Test reading empty notes file."""
    notes_file = tmp_path / ".hive-notes.jsonl"
    notes_file.write_text("")

    notes = read_notes_file(str(tmp_path))
    assert notes == []


def test_read_notes_file_valid_single_note(tmp_path):
    """Test reading notes file with a single valid note."""
    notes_file = tmp_path / ".hive-notes.jsonl"
    notes_file.write_text('{"category": "discovery", "content": "Test discovery", "issue_id": "w-123"}')

    notes = read_notes_file(str(tmp_path))
    assert len(notes) == 1
    assert notes[0]["category"] == "discovery"
    assert notes[0]["content"] == "Test discovery"
    assert notes[0]["issue_id"] == "w-123"


def test_read_notes_file_valid_multiple_notes(tmp_path):
    """Test reading notes file with multiple valid notes."""
    notes_file = tmp_path / ".hive-notes.jsonl"
    content = """{"category": "discovery", "content": "First discovery", "issue_id": "w-123"}
{"category": "gotcha", "content": "Second gotcha", "issue_id": "w-456"}
{"category": "dependency", "content": "Third dependency", "issue_id": "w-789"}"""
    notes_file.write_text(content)

    notes = read_notes_file(str(tmp_path))
    assert len(notes) == 3
    assert notes[0]["category"] == "discovery"
    assert notes[1]["category"] == "gotcha"
    assert notes[2]["category"] == "dependency"


def test_read_notes_file_malformed_json(tmp_path):
    """Test reading notes file with malformed JSON returns empty list."""
    notes_file = tmp_path / ".hive-notes.jsonl"
    notes_file.write_text('{"invalid": json}')

    notes = read_notes_file(str(tmp_path))
    assert notes == []


def test_read_notes_file_mixed_valid_invalid(tmp_path):
    """Test reading notes file with mix of valid and invalid lines."""
    notes_file = tmp_path / ".hive-notes.jsonl"
    content = """{"category": "discovery", "content": "Valid note", "issue_id": "w-123"}
{"invalid": json}
{"category": "gotcha", "content": "Another valid note", "issue_id": "w-456"}"""
    notes_file.write_text(content)

    notes = read_notes_file(str(tmp_path))
    # Should return empty list if any JSON parsing fails
    assert notes == []


def test_remove_notes_file_exists(tmp_path):
    """Test removing notes file that exists."""
    notes_file = tmp_path / ".hive-notes.jsonl"
    notes_file.write_text('{"category": "discovery", "content": "Test"}')

    result = remove_notes_file(str(tmp_path))
    assert result is True
    assert not notes_file.exists()


def test_remove_notes_file_doesnt_exist(tmp_path):
    """Test removing notes file that doesn't exist."""
    result = remove_notes_file(str(tmp_path))
    assert result is False


def test_build_worker_prompt_with_notes():
    """Test building worker prompt with notes parameter."""
    issue = {"title": "Test Issue", "description": "Test description"}
    notes = [
        {"category": "discovery", "content": "Test suite needs Python 3.12+", "issue_id": "w-123"},
        {"category": "gotcha", "content": "Connection can be None", "issue_id": "w-456"},
    ]

    prompt = build_worker_prompt(
        agent_name="test-agent",
        issue=issue,
        worktree_path="/tmp/worktree",
        branch_name="agent/test-agent",
        project="test-project",
        notes=notes,
    )

    assert "Project Notes (from other workers)" in prompt
    assert "[discovery] Test suite needs Python 3.12+ (from w-123)" in prompt
    assert "[gotcha] Connection can be None (from w-456)" in prompt


def test_build_worker_prompt_without_notes():
    """Test building worker prompt without notes parameter."""
    issue = {"title": "Test Issue", "description": "Test description"}

    prompt = build_worker_prompt(
        agent_name="test-agent",
        issue=issue,
        worktree_path="/tmp/worktree",
        branch_name="agent/test-agent",
        project="test-project",
        notes=None,
    )

    assert "Project Notes (from other workers)" not in prompt


def test_build_worker_prompt_empty_notes():
    """Test building worker prompt with empty notes list."""
    issue = {"title": "Test Issue", "description": "Test description"}

    prompt = build_worker_prompt(
        agent_name="test-agent",
        issue=issue,
        worktree_path="/tmp/worktree",
        branch_name="agent/test-agent",
        project="test-project",
        notes=[],
    )

    assert "Project Notes (from other workers)" not in prompt


def test_worker_prompt_contains_knowledge_sharing():
    """Test that worker prompt template contains KNOWLEDGE SHARING section."""
    issue = {"title": "Test Issue", "description": "Test description"}

    prompt = build_worker_prompt(
        agent_name="test-agent",
        issue=issue,
        worktree_path="/tmp/worktree",
        branch_name="agent/test-agent",
        project="test-project",
    )

    assert "KNOWLEDGE SHARING" in prompt
    assert ".hive-notes.jsonl" in prompt
    assert "discovery" in prompt
    assert "gotcha" in prompt
    assert "dependency" in prompt
    assert "pattern" in prompt
