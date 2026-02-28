"""Tests for prompt templates and completion assessment."""

from hive.prompts import (
    assess_completion,
    build_refinery_prompt,
    build_retry_context,
    build_worker_prompt,
    get_prompt_version,
    read_notes_file,
    remove_notes_file,
    render_inbox_section,
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


# --- render_inbox_section tests ---


def _make_delivery(
    delivery_id="d-1",
    note_id="n-1",
    content="Note content.",
    must_read=False,
    status="delivered",
    from_agent_id="agent-5",
    scope="agent",
    recipient_issue_id=None,
):
    return {
        "delivery_id": delivery_id,
        "note_id": note_id,
        "content": content,
        "must_read": must_read,
        "status": status,
        "from_agent_id": from_agent_id,
        "scope": scope,
        "recipient_issue_id": recipient_issue_id,
    }


def test_render_inbox_section_empty():
    """Empty deliveries list returns empty string."""
    assert render_inbox_section([]) == ""


def test_render_inbox_section_single_agent_scope():
    """Single delivery with scope=agent formats header, tags, from, and content correctly."""
    deliveries = [_make_delivery(delivery_id="d-1", note_id="n-1", content="Use the new parser.", from_agent_id="agent-5", scope="agent")]
    result = render_inbox_section(deliveries)

    assert "### Notes Inbox Update (1 pending)" in result
    assert "[delivery:d-1][note:n-1]" in result
    assert "[scope:agent]" in result
    assert "from agent=agent-5" in result
    assert "Use the new parser." in result
    assert "[must_read]" not in result
    assert "Required actions" not in result


def test_render_inbox_section_single_issue_scope():
    """Single delivery with scope=issue includes recipient_issue_id in scope tag."""
    deliveries = [
        _make_delivery(
            delivery_id="d-10",
            note_id="n-5",
            content="Fix the migration.",
            from_agent_id="agent-2",
            scope="issue",
            recipient_issue_id="w-abc123",
        )
    ]
    result = render_inbox_section(deliveries)

    assert "[scope:issue issue=w-abc123]" in result
    assert "from agent=agent-2" in result


def test_render_inbox_section_must_read():
    """Delivery with must_read=True shows [must_read] tag and Required actions block."""
    deliveries = [
        _make_delivery(
            delivery_id="d-20", note_id="n-10", content="Critical: do not use deprecated API.", must_read=True, from_agent_id="agent-3"
        )
    ]
    result = render_inbox_section(deliveries)

    assert "[must_read]" in result
    assert "Required actions:" in result
    assert "hive mail ack <delivery_id>" in result
    assert "Proceed with implementation" in result


def test_render_inbox_section_from_system():
    """from_agent_id=None renders 'from system' instead of 'from agent=...'."""
    deliveries = [_make_delivery(delivery_id="d-30", note_id="n-15", content="System announcement.", from_agent_id=None)]
    result = render_inbox_section(deliveries)

    assert "from system" in result
    assert "from agent=" not in result


def test_render_inbox_section_has_more():
    """has_more=True appends 'More notes pending' line to the output."""
    deliveries = [_make_delivery(delivery_id="d-40", note_id="n-20", content="One note.")]
    result = render_inbox_section(deliveries, has_more=True)

    assert "More notes pending" in result


def test_render_inbox_section_multiple_mixed():
    """Multiple deliveries: ordering preserved, must_read only on correct items, Required actions block present."""
    deliveries = [
        _make_delivery(delivery_id="d-1", note_id="n-1", content="First note.", must_read=False, from_agent_id="agent-1", scope="agent"),
        _make_delivery(
            delivery_id="d-2",
            note_id="n-2",
            content="Second note, required.",
            must_read=True,
            from_agent_id="agent-2",
            scope="issue",
            recipient_issue_id="w-xyz",
        ),
    ]
    result = render_inbox_section(deliveries)

    assert "### Notes Inbox Update (2 pending)" in result
    assert result.find("d-1") < result.find("d-2"), "First delivery should appear before second"
    assert "First note." in result
    assert "Second note, required." in result
    assert "[must_read]" in result
    assert "Required actions:" in result


def test_build_worker_prompt_with_inbox_section():
    """inbox_section parameter is appended after notes_section in the generated prompt."""
    issue = {"title": "Test Issue", "description": "Test description"}
    inbox = "### Notes Inbox Update (1 pending)\n- [delivery:d-1][note:n-1][scope:agent] from agent=agent-5\n  Note content."

    prompt = build_worker_prompt(
        agent_name="test-agent",
        issue=issue,
        worktree_path="/tmp/worktree",
        branch_name="agent/test-agent",
        project="test-project",
        inbox_section=inbox,
    )

    assert "### Notes Inbox Update" in prompt
    assert "Note content." in prompt


def test_build_worker_prompt_with_inbox_section_and_notes():
    """inbox_section appears after legacy notes_section when both are provided."""
    issue = {"title": "Test Issue", "description": "Test description"}
    notes = [{"category": "gotcha", "content": "A legacy note.", "issue_id": "w-99"}]
    inbox = "### Notes Inbox Update (1 pending)\n- [delivery:d-5][note:n-5][scope:agent] from agent=agent-1\n  Inbox note."

    prompt = build_worker_prompt(
        agent_name="test-agent",
        issue=issue,
        worktree_path="/tmp/worktree",
        branch_name="agent/test-agent",
        project="test-project",
        notes=notes,
        inbox_section=inbox,
    )

    assert "Project Notes (from other workers)" in prompt
    assert "A legacy note." in prompt
    assert "### Notes Inbox Update" in prompt
    assert "Inbox note." in prompt
    # inbox should appear after the legacy notes section
    assert prompt.find("A legacy note.") < prompt.find("Inbox note.")
