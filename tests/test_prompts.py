"""Tests for prompt templates and completion assessment."""

from hive.prompts import (
    _load_template,
    assess_completion,
    build_refinery_prompt,
    build_retry_context,
    build_worker_prompt,
    get_prompt_version,
    read_notes_file,
    remove_notes_file,
)


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
    """Test refinery prompt includes first-pass review structure."""
    prompt = build_refinery_prompt(
        issue_title="Add auth middleware",
        issue_id="w-abc123",
        branch_name="agent/worker-1",
        worktree_path="/tmp/wt1",
        agent_name="worker-1",
    )

    # Keep structural checks: issue_id and branch should appear
    assert "w-abc123" in prompt
    assert "Add auth middleware" in prompt
    assert "agent/worker-1" in prompt
    assert "Merge Review Scope" in prompt
    assert ".hive-result.jsonl" in prompt


def test_build_refinery_prompt_test_failure():
    """Test refinery prompt includes preferred test command when provided."""
    prompt = build_refinery_prompt(
        issue_title="Fix login bug",
        issue_id="w-def456",
        branch_name="agent/worker-2",
        worktree_path="/tmp/wt2",
        test_command="pytest tests/",
    )

    # Keep structural checks: test command should appear
    assert "pytest tests/" in prompt
    assert "Perform full first-pass merge review and integration" in prompt


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


def test_prompt_templates_match_current_cli_and_note_model():
    """Prompt templates should not reference removed mail/events flows."""
    worker = _load_template("worker")
    system = _load_template("system")
    queen = _load_template("queen")
    refinery = _load_template("refinery")

    assert "hive mail" not in worker
    assert "Notes Inbox" not in worker
    assert "acknowledgment CLI" in worker

    assert "hive mail" not in system
    assert "inbox or\nacknowledgment CLI" in system

    assert "hive mail" not in queen
    assert "hive --json events" not in queen
    assert "--to-agent" not in queen
    assert "mailbox or acknowledgment flow" in queen

    assert "note_delivered" not in refinery
    assert "notes_injected" in refinery


# --- Prompt versioning tests ---


def test_get_prompt_version_returns_hex_string():
    """Test get_prompt_version returns a 12-character hex string."""
    import re

    version = get_prompt_version("worker")

    assert isinstance(version, str)
    assert len(version) == 12
    assert re.match(r"[0-9a-f]{12}", version), f"Expected hex string, got: {version}"


def test_get_prompt_version_deterministic():
    """Test get_prompt_version returns the same result for same template."""
    version1 = get_prompt_version("worker")
    version2 = get_prompt_version("worker")

    assert version1 == version2


def test_get_prompt_version_changes_with_content():
    """Test get_prompt_version returns different hashes for different content."""
    import unittest.mock

    # Mock _load_template to return different content
    with unittest.mock.patch("hive.prompts._load_template") as mock_load:
        mock_load.return_value = "content1"
        version1 = get_prompt_version("worker")

        mock_load.return_value = "content2"
        version2 = get_prompt_version("worker")

        assert version1 != version2


# --- Retry context tests ---


def test_build_retry_context_no_events(temp_db):
    """Test build_retry_context with no events returns None."""
    issue_id = temp_db.create_issue("Test issue", "Test description", project="test-project")

    result = build_retry_context(temp_db, issue_id)

    assert result is None


def test_build_retry_context_with_failures(temp_db):
    """Test build_retry_context formats incomplete events."""

    # Create issue and agent
    issue_id = temp_db.create_issue("Test issue", "Test description", project="test-project")
    agent_id = temp_db.create_agent("test-agent")

    # Add incomplete event
    incomplete_detail = {"reason": "tests failed", "summary": "Unit tests are failing in auth module", "model": "claude-sonnet-4"}
    temp_db.log_event(issue_id, agent_id, "incomplete", incomplete_detail)

    result = build_retry_context(temp_db, issue_id)

    assert result is not None
    assert "## Prior Attempts" in result
    assert "Previous attempts failed:" in result
    assert "**Attempt failed**: tests failed — Unit tests are failing in auth module" in result
    assert "Address these specific failure reasons" in result
    assert "Do not repeat the same mistakes" in result


def test_build_retry_context_with_rejection(temp_db):
    """Test build_retry_context formats merge_rejected events."""

    # Create issue and agent
    issue_id = temp_db.create_issue("Test issue", "Test description", project="test-project")
    agent_id = temp_db.create_agent("test-agent")

    # Add merge_rejected event
    rejection_detail = {"summary": "Code quality issues found during review"}
    temp_db.log_event(issue_id, agent_id, "merge_rejected", rejection_detail)

    result = build_retry_context(temp_db, issue_id)

    assert result is not None
    assert "## Prior Attempts" in result
    assert "**Merge rejected**: Code quality issues found during review" in result


def test_build_retry_context_with_stalled_events(temp_db):
    """Test build_retry_context formats stalled events."""

    # Create issue and agent
    issue_id = temp_db.create_issue("Test issue", "Test description", project="test-project")
    agent_id = temp_db.create_agent("test-agent")

    # Add stalled event
    stalled_detail = {"reason": "Agent became unresponsive", "summary": "No progress for 30 minutes"}
    temp_db.log_event(issue_id, agent_id, "stalled", stalled_detail)

    result = build_retry_context(temp_db, issue_id)

    assert result is not None
    assert "## Prior Attempts" in result
    assert "**Attempt stalled**: Agent became unresponsive — No progress for 30 minutes" in result


def test_build_retry_context_mixed_events(temp_db):
    """Test build_retry_context with multiple different failure types."""

    # Create issue and agent
    issue_id = temp_db.create_issue("Test issue", "Test description", project="test-project")
    agent_id = temp_db.create_agent("test-agent")

    # Add different types of events
    temp_db.log_event(issue_id, agent_id, "incomplete", {"reason": "compilation error", "summary": "Syntax error in main.py"})
    temp_db.log_event(issue_id, agent_id, "merge_rejected", {"summary": "Missing test coverage"})
    temp_db.log_event(issue_id, agent_id, "stalled", {"reason": "timeout", "summary": ""})

    result = build_retry_context(temp_db, issue_id)

    assert result is not None
    assert "## Prior Attempts" in result
    assert "**Attempt failed**: compilation error — Syntax error in main.py" in result
    assert "**Merge rejected**: Missing test coverage" in result
    assert "**Attempt stalled**: timeout" in result


def test_build_retry_context_malformed_detail(temp_db):
    """Test build_retry_context handles malformed detail gracefully."""
    # Create issue and agent
    issue_id = temp_db.create_issue("Test issue", "Test description", project="test-project")
    agent_id = temp_db.create_agent("test-agent")

    # Manually insert event with malformed JSON detail
    temp_db.conn.execute(
        "INSERT INTO events (issue_id, agent_id, event_type, detail) VALUES (?, ?, ?, ?)", (issue_id, agent_id, "incomplete", "invalid json")
    )
    temp_db.conn.commit()

    result = build_retry_context(temp_db, issue_id)

    assert result is not None
    assert "**Attempt failed**: Unknown reason" in result


def test_build_retry_context_returns_none_after_reset(temp_db):
    """After a retry_reset, build_retry_context returns None if all failures are before the reset."""
    issue_id = temp_db.create_issue("Test issue", "Test description", project="test-project")
    agent_id = temp_db.create_agent("test-agent")

    # Add failure events before reset
    temp_db.log_event(issue_id, agent_id, "incomplete", {"reason": "tests failed", "summary": "Unit tests failing"})
    temp_db.log_event(issue_id, agent_id, "stalled", {"reason": "timeout", "summary": "No progress"})

    # Verify context exists before reset
    result = build_retry_context(temp_db, issue_id)
    assert result is not None
    assert "tests failed" in result

    # Log reset
    temp_db.log_event(issue_id, None, "retry_reset", {"notes": "fixed"})

    # After reset, no failures post-watermark → None
    result = build_retry_context(temp_db, issue_id)
    assert result is None


def test_build_retry_context_includes_post_reset_failures(temp_db):
    """After a retry_reset, only post-reset failures appear in retry context."""
    issue_id = temp_db.create_issue("Test issue", "Test description", project="test-project")
    agent_id = temp_db.create_agent("test-agent")

    # Pre-reset failure
    temp_db.log_event(issue_id, agent_id, "incomplete", {"reason": "old failure", "summary": "Old problem"})

    # Reset
    temp_db.log_event(issue_id, None, "retry_reset", {"notes": "fixed"})

    # Post-reset failure
    temp_db.log_event(issue_id, agent_id, "incomplete", {"reason": "new failure", "summary": "New problem"})

    result = build_retry_context(temp_db, issue_id)
    assert result is not None
    assert "new failure" in result
    assert "old failure" not in result


def test_build_worker_prompt_with_retry_context():
    """Test that retry context appears in worker prompt."""
    issue = {"title": "Test Issue", "description": "Test description"}
    retry_context = """## Prior Attempts
This issue has been attempted before. Previous attempts failed:
- **Attempt failed**: tests failed — Unit tests failing
Address these specific failure reasons. Do not repeat the same mistakes."""

    prompt = build_worker_prompt(
        agent_name="test-agent",
        issue=issue,
        worktree_path="/tmp/worktree",
        branch_name="agent/test-agent",
        project="test-project",
        retry_context=retry_context,
    )

    assert "## Prior Attempts" in prompt
    assert "Previous attempts failed:" in prompt
    assert "**Attempt failed**: tests failed — Unit tests failing" in prompt
    assert "Address these specific failure reasons" in prompt

    # Verify it appears between notes_section and BEHAVIORAL CONTRACT
    context_index = prompt.find("## CONTEXT")
    behavioral_index = prompt.find("## BEHAVIORAL CONTRACT")
    retry_index = prompt.find("## Prior Attempts")

    assert context_index < retry_index < behavioral_index
