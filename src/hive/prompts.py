"""Prompt templates for Hive agents."""

import json
from pathlib import Path
from string import Template
from typing import Any, Dict, List, Optional

from .models import CompletionResult

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


def build_worker_prompt(
    agent_name: str,
    issue: Dict[str, Any],
    worktree_path: str,
    branch_name: str,
    project: str,
    completed_steps: Optional[List[str]] = None,
    notes: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """
    Build the worker prompt for an issue.

    Args:
        agent_name: Name of the agent
        issue: Issue dict with title and description
        worktree_path: Path to the git worktree
        branch_name: Git branch name
        project: Project name
        completed_steps: List of completed step summaries (for molecules)
        notes: List of note dicts from other workers

    Returns:
        Formatted worker prompt string
    """
    # Build context section
    context_parts = [
        f"- You are working in a git worktree at: {worktree_path}",
        f"- Branch: {branch_name}",
    ]

    context = "\n".join(context_parts)

    # Build completed steps section (for molecules)
    completed_section = ""
    if completed_steps:
        completed_section = "\n\n### Previous Steps (already completed)\n" + "\n".join(
            f"{i + 1}. {step}" for i, step in enumerate(completed_steps)
        )

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

    template_str = _load_template("worker")
    return Template(template_str).safe_substitute(
        agent_name=agent_name,
        project=project,
        title=issue["title"],
        description=issue.get("description", ""),
        context=context,
        completed_section=completed_section,
        notes_section=notes_section,
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
        messages: List of message dicts from OpenCode session (unused, kept for compatibility)
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
    rebase_succeeded: bool = False,
    test_output: Optional[str] = None,
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
        rebase_succeeded: Whether mechanical rebase succeeded
        test_output: Output from test run (if tests failed)
        test_command: Test command that was run

    Returns:
        Formatted refinery prompt string
    """
    if not rebase_succeeded:
        problem = "Mechanical rebase FAILED — conflicts detected. Please resolve them."
    elif test_output:
        problem = f"Rebase succeeded but TESTS FAILED. Please diagnose.\n\nTest command: {test_command}\nTest output:\n{test_output[:3000]}"
    else:
        problem = "Unknown merge issue. Investigate and resolve."

    worker_line = f"- **Worker**: {agent_name}" if agent_name else ""
    test_step = f"Run tests: `{test_command}`" if test_command else "Verify the code compiles/looks correct"

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
