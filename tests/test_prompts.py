"""Tests for prompt templates and completion assessment."""

from hive.prompts import (
    assess_completion,
    build_refinery_prompt,
    build_system_prompt,
    build_worker_prompt,
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


def test_assess_completion_file_result_success():
    """Test assessing completion with file-based result (success)."""
    messages = []  # Messages are now ignored
    file_result = {
        "status": "success",
        "summary": "Implemented authentication middleware",
        "files_changed": ["auth/middleware.py", "tests/test_auth.py"],
        "tests_run": True,
        "blockers": [],
        "artifacts": [{"type": "git_commit", "value": "abc123def456"}, {"type": "test_result", "value": "pass"}],
    }

    result = assess_completion(messages, file_result=file_result)

    assert result.success is True
    assert result.summary == "Implemented authentication middleware"
    assert result.artifacts["git_commit"] == "abc123def456"
    assert result.artifacts["test_result"] == "pass"


def test_assess_completion_file_result_blocked():
    """Test assessing completion with file-based result (blocked)."""
    messages = []  # Messages are now ignored
    file_result = {
        "status": "blocked",
        "summary": "Cannot proceed without database schema",
        "files_changed": [],
        "tests_run": False,
        "blockers": ["Missing database schema definition"],
        "artifacts": [],
    }

    result = assess_completion(messages, file_result=file_result)

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
    assert ".hive-result.jsonl" in prompt


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


def test_assess_completion_no_file_result():
    """Test behavior when no file result is provided - should fail."""
    messages = [{"parts": [{"type": "text", "text": "I've completed the task successfully."}]}]

    result = assess_completion(messages)

    assert result.success is False
    assert "Worker did not write completion signal" in result.reason


def test_assess_completion_file_result_failure():
    """Test assessing completion with file-based result (failure)."""
    messages = []  # Messages are now ignored
    file_result = {
        "status": "failure",
        "summary": "Tests failed after implementation",
        "files_changed": ["src/feature.py"],
        "tests_run": True,
        "blockers": ["Unit tests are failing", "Integration test timeout"],
        "artifacts": [],
    }

    result = assess_completion(messages, file_result=file_result)

    assert result.success is False
    assert "Unit tests are failing; Integration test timeout" in result.reason


def test_assess_completion_file_result_unknown_status():
    """Test handling unknown status in file result."""
    messages = []  # Messages are now ignored
    file_result = {
        "status": "unknown",
        "summary": "Something went wrong",
        "files_changed": [],
        "tests_run": False,
        "blockers": [],
        "artifacts": [],
    }

    result = assess_completion(messages, file_result=file_result)

    assert result.success is False
    assert "Worker reported status: unknown" in result.reason


def test_assess_completion_empty_messages_no_file():
    """Test handling empty messages with no file result."""
    result = assess_completion([])

    assert result.success is False
    assert "Worker did not write completion signal" in result.reason


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
