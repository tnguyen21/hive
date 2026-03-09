"""Prompt templates for Hive agents."""

import hashlib
import json
from pathlib import Path
from string import Template
from typing import Any, Dict, List, Optional

from .utils import CompletionResult

# Filename for file-based completion signal
RESULT_FILE_NAME = ".hive-result.jsonl"

# Filename for notes file
NOTES_FILE_NAME = ".hive-notes.jsonl"


# Template cache: name -> template string
_template_cache: Dict[str, str] = {}

# Directory containing .md template files
_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_template(name: str) -> str:
    """
    Load a prompt template from prompts/{name}.md.

    Templates are cached after first load.

    Args:
        name: Template name (without .md extension)

    Returns:
        Raw template string
    """
    if name not in _template_cache:
        template_path = _PROMPTS_DIR / f"{name}.md"
        _template_cache[name] = template_path.read_text()
    return _template_cache[name]


def get_prompt_version(template_name: str) -> str:
    """Return a short hash of the prompt template file content."""
    content = _load_template(template_name)
    return hashlib.sha256(content.encode()).hexdigest()[:12]


def _parse_event_detail(event: Dict[str, Any]) -> Dict[str, Any]:
    """Parse the detail field of an event into a dict.

    Returns the detail as-is if already a dict, attempts JSON parsing if a string,
    and falls back to an empty dict on failure or absence.
    """
    detail = event.get("detail", {})
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except json.JSONDecodeError:
            detail = {}
    return detail if isinstance(detail, dict) else {}


def build_retry_context(db, issue_id: str) -> Optional[str]:
    """
    Build retry context by querying previous failure events for an issue.

    Only includes events after the most recent retry_reset watermark (if any).

    Args:
        db: Database instance
        issue_id: Issue ID to query events for

    Returns:
        Formatted retry context as markdown string, or None if no failures found
    """
    # Find the most recent retry_reset watermark (use id for ordering, not timestamp)
    cursor = db.conn.execute(
        "SELECT MAX(id) FROM events WHERE issue_id = ? AND event_type = 'retry_reset'",
        (issue_id,),
    )
    reset_id = cursor.fetchone()[0]

    # Query for different types of failure events
    incomplete_events = db.get_events(issue_id=issue_id, event_type="incomplete")
    merge_rejected_events = db.get_events(issue_id=issue_id, event_type="merge_rejected")
    stalled_events = db.get_events(issue_id=issue_id, event_type="stalled")

    # Filter to only events after the reset watermark
    if reset_id is not None:
        incomplete_events = [e for e in incomplete_events if e["id"] > reset_id]
        merge_rejected_events = [e for e in merge_rejected_events if e["id"] > reset_id]
        stalled_events = [e for e in stalled_events if e["id"] > reset_id]

    # Collect failure descriptions
    failures = []

    # Process incomplete events
    for event in incomplete_events:
        detail = _parse_event_detail(event)
        reason = detail.get("reason", "Unknown reason")
        summary = detail.get("summary", "")

        if summary:
            failures.append(f"**Attempt failed**: {reason} — {summary}")
        else:
            failures.append(f"**Attempt failed**: {reason}")

    # Process merge_rejected events
    for event in merge_rejected_events:
        detail = _parse_event_detail(event)
        summary = detail.get("summary", "Merge was rejected")
        failures.append(f"**Merge rejected**: {summary}")

    # Process stalled events (similar structure to incomplete)
    for event in stalled_events:
        detail = _parse_event_detail(event)
        reason = detail.get("reason", "Agent stalled")
        summary = detail.get("summary", "")

        if summary:
            failures.append(f"**Attempt stalled**: {reason} — {summary}")
        else:
            failures.append(f"**Attempt stalled**: {reason}")

    # Return formatted context if any failures found
    if failures:
        failures_text = "\n".join(f"- {failure}" for failure in failures)
        return f"""## Prior Attempts
This issue has been attempted before. Previous attempts failed:
{failures_text}
Address these specific failure reasons. Do not repeat the same mistakes."""

    return None


def build_worker_prompt(
    agent_name: str,
    issue: Dict[str, Any],
    worktree_path: str,
    branch_name: str,
    project: str,
    notes: Optional[List[Dict[str, Any]]] = None,
    retry_context: Optional[str] = None,
) -> str:
    """
    Build the worker prompt for an issue.

    Args:
        agent_name: Name of the agent
        issue: Issue dict with title and description
        worktree_path: Path to the git worktree
        branch_name: Git branch name
        project: Project name
        notes: List of note dicts from other workers
        retry_context: Optional retry context from previous failures

    Returns:
        Formatted worker prompt string
    """
    # Build context section
    context_parts = [
        f"- You are working in a git worktree at: {worktree_path}",
        f"- Branch: {branch_name}",
    ]

    context = "\n".join(context_parts)

    # Build notes section (knowledge from other workers)
    notes_section = ""
    if notes:
        note_lines = []
        for note in notes:
            category = note.get("category", "discovery")
            content = note.get("content", "")
            source = note.get("issue_id", "project")
            note_lines.append(f"- [{category}] {content} (from {source})")
        notes_section = "\n\n### Project Notes (from other workers)\n" + "\n".join(note_lines)

    # Build retry section
    retry_section = ""
    if retry_context:
        retry_section = f"\n\n{retry_context}"

    template_str = _load_template("worker")
    return Template(template_str).safe_substitute(
        agent_name=agent_name,
        project=project,
        title=issue["title"],
        description=issue.get("description", ""),
        context=context,
        notes_section=notes_section,
        retry_section=retry_section,
        worktree_path=worktree_path,
    )


def build_system_prompt(project: str, agent_name: str, worktree_path: Optional[str] = None) -> str:
    """
    Build the system prompt for an agent session.

    Args:
        project: Project name
        agent_name: Agent name
        worktree_path: Path to worktree (if available, checks for CLAUDE.md)

    Returns:
        System prompt string
    """
    template_str = _load_template("system")
    base = Template(template_str).safe_substitute(
        agent_name=agent_name,
        project=project,
    )

    result = base.rstrip()

    # Inject project-specific CLAUDE.md if it exists
    if worktree_path:
        claude_md = Path(worktree_path) / "CLAUDE.md"
        if claude_md.exists():
            result += f"\n\n## Project Instructions\n\n{claude_md.read_text()}"

    return result


def assess_completion(
    messages: List[Dict[str, Any]],
    file_result: Optional[Dict[str, Any]] = None,
) -> CompletionResult:
    """
    Assess completion based on file-based result only.

    Args:
        messages: List of message dicts from backend session (unused, kept for compatibility)
        file_result: Optional parsed result from .hive-result.jsonl file.
            If provided, used directly to construct CompletionResult.

    Returns:
        CompletionResult with success status, reason, and artifacts
    """
    # If we have a file-based result, use it directly
    if file_result is not None:
        status = file_result.get("status", "unknown")
        summary = file_result.get("summary", "")
        blockers = file_result.get("blockers", [])
        artifacts_list = file_result.get("artifacts", [])

        # Convert artifacts list to dict
        artifacts = {}
        if isinstance(artifacts_list, list):
            for artifact in artifacts_list:
                if isinstance(artifact, dict):
                    art_type = artifact.get("type")
                    art_value = artifact.get("value")
                    if art_type:
                        artifacts[art_type] = art_value

        reason = ""
        if status != "success" and blockers:
            reason = "; ".join(blockers) if isinstance(blockers, list) else str(blockers)
        elif status != "success":
            reason = f"Worker reported status: {status}"

        return CompletionResult(
            success=(status == "success"),
            reason=reason,
            summary=summary,
            artifacts=artifacts,
        )

    # No file result = worker didn't write completion signal = failure
    return CompletionResult(
        success=False,
        reason="Worker did not write completion signal (.hive-result.jsonl)",
        summary="",
    )


def read_result_file(worktree_path: str) -> Optional[Dict[str, Any]]:
    """
    Read and parse a .hive-result.jsonl file from a worktree.

    Args:
        worktree_path: Path to the git worktree

    Returns:
        Parsed dict from the JSON line, or None if file doesn't exist or is invalid.
    """
    result_path = Path(worktree_path) / RESULT_FILE_NAME
    if not result_path.exists():
        return None

    try:
        text = result_path.read_text().strip()
        if not text:
            return None
        # Read the first non-empty line (JSONL format)
        first_line = text.split("\n")[0].strip()
        return json.loads(first_line)
    except (json.JSONDecodeError, OSError, IndexError):
        return None


def remove_result_file(worktree_path: str) -> bool:
    """
    Remove the .hive-result.jsonl file from a worktree.

    Args:
        worktree_path: Path to the git worktree

    Returns:
        True if file was removed, False if it didn't exist.
    """
    result_path = Path(worktree_path) / RESULT_FILE_NAME
    try:
        if result_path.exists():
            result_path.unlink()
            return True
    except OSError:
        pass
    return False


def read_notes_file(worktree_path: str) -> List[Dict[str, Any]]:
    """Read .hive-notes.jsonl from a worktree. Returns list of note dicts, or empty list."""
    notes_path = Path(worktree_path) / NOTES_FILE_NAME
    if not notes_path.exists():
        return []
    try:
        text = notes_path.read_text().strip()
        if not text:
            return []
        notes = []
        for line in text.split("\n"):
            line = line.strip()
            if line:
                notes.append(json.loads(line))
        return notes
    except (json.JSONDecodeError, OSError):
        return []


def remove_notes_file(worktree_path: str) -> bool:
    """Remove .hive-notes.jsonl from a worktree. Returns True if file existed."""
    notes_path = Path(worktree_path) / NOTES_FILE_NAME
    if notes_path.exists():
        notes_path.unlink()
        return True
    return False


def build_refinery_prompt(
    issue_title: str,
    issue_id: str,
    branch_name: str,
    worktree_path: str,
    agent_name: Optional[str] = None,
    test_command: Optional[str] = None,
) -> str:
    """
    Build the Refinery prompt for processing a merge.

    Args:
        issue_title: Title of the issue being merged
        issue_id: Issue ID
        branch_name: Git branch name
        worktree_path: Path to the worktree
        agent_name: Name of the worker agent (optional)
        test_command: Preferred test command from queue metadata

    Returns:
        Formatted refinery prompt string
    """
    if test_command:
        problem = (
            "Perform full first-pass merge review and integration. "
            f"Preferred test command: {test_command}. "
            "You may run additional tests as needed."
        )
    else:
        problem = "Perform full first-pass merge review and integration. Determine and run the appropriate tests."

    worker_line = f"- **Worker**: {agent_name}" if agent_name else ""
    test_step = (
        f"Run tests: `{test_command}` (plus any additional coverage needed)" if test_command else "Determine and run an appropriate test suite"
    )

    template_str = _load_template("refinery")
    return Template(template_str).safe_substitute(
        issue_id=issue_id,
        issue_title=issue_title,
        branch_name=branch_name,
        worktree_path=worktree_path,
        worker_line=worker_line,
        problem=problem,
        test_step=test_step,
    )
