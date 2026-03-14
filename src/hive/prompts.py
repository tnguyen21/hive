"""Prompt templates and worktree JSONL helpers for Hive agents."""

import hashlib
import json
from pathlib import Path
from string import Template
from typing import Any, Dict, List, Optional

from .utils import CompletionResult

RESULT_FILE_NAME = ".hive-result.jsonl"
NOTES_FILE_NAME = ".hive-notes.jsonl"
PROJECT_CONTEXT_FILE = ".hive/project-context.md"
_template_cache: Dict[str, str] = {}
_PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_template(name: str) -> str:
    """Load and cache a prompt template."""
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


def _artifacts_from_list(artifacts_list: Any) -> Dict[str, Any]:
    """Convert a result-file artifacts list into a simple dict."""
    artifacts: Dict[str, Any] = {}
    if isinstance(artifacts_list, list):
        for artifact in artifacts_list:
            if isinstance(artifact, dict):
                art_type = artifact.get("type")
                art_value = artifact.get("value")
                if art_type:
                    artifacts[art_type] = art_value
    return artifacts


def _read_first_jsonl(path: Path) -> Optional[Dict[str, Any]]:
    """Read the first non-empty JSON line from *path*."""
    if not path.exists():
        return None
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line:
                return json.loads(line)
    except (json.JSONDecodeError, OSError):
        pass
    return None


def _remove_file(path: Path) -> bool:
    """Remove *path* if it exists."""
    try:
        if path.exists():
            path.unlink()
            return True
    except OSError:
        pass
    return False


def build_retry_context(db, issue_id: str) -> Optional[str]:
    """Build retry context from prior failure events for an issue."""
    cursor = db.conn.execute(
        "SELECT MAX(id) FROM events WHERE issue_id = ? AND event_type = 'retry_reset'",
        (issue_id,),
    )
    reset_id = cursor.fetchone()[0]

    incomplete_events = db.get_events(issue_id=issue_id, event_type="incomplete")
    merge_rejected_events = db.get_events(issue_id=issue_id, event_type="merge_rejected")
    stalled_events = db.get_events(issue_id=issue_id, event_type="stalled")

    if reset_id is not None:
        incomplete_events = [e for e in incomplete_events if e["id"] > reset_id]
        merge_rejected_events = [e for e in merge_rejected_events if e["id"] > reset_id]
        stalled_events = [e for e in stalled_events if e["id"] > reset_id]

    failures = []
    for event in incomplete_events:
        detail = _parse_event_detail(event)
        reason = detail.get("reason", "Unknown reason")
        summary = detail.get("summary", "")

        if summary:
            failures.append(f"**Attempt failed**: {reason} — {summary}")
        else:
            failures.append(f"**Attempt failed**: {reason}")

    for event in merge_rejected_events:
        detail = _parse_event_detail(event)
        summary = detail.get("summary", "Merge was rejected")
        failures.append(f"**Merge rejected**: {summary}")

    for event in stalled_events:
        detail = _parse_event_detail(event)
        reason = detail.get("reason", "Agent stalled")
        summary = detail.get("summary", "")

        if summary:
            failures.append(f"**Attempt stalled**: {reason} — {summary}")
        else:
            failures.append(f"**Attempt stalled**: {reason}")

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
    """Build the worker prompt for an issue."""
    context_parts = [
        f"- You are working in a git worktree at: {worktree_path}",
        f"- Branch: {branch_name}",
    ]

    ctx = "\n".join(context_parts)

    notes_section = ""
    if notes:
        note_lines = []
        for note in notes:
            category = note.get("category", "discovery")
            content = note.get("content", "")
            source = note.get("issue_id", "project")
            note_lines.append(f"- [{category}] {content} (from {source})")
        notes_section = "\n\n### Project Notes (from other workers)\n" + "\n".join(note_lines)

    retry_section = ""
    if retry_context:
        retry_section = f"\n\n{retry_context}"

    template_str = _load_template("worker")
    return Template(template_str).safe_substitute(
        agent_name=agent_name,
        project=project,
        title=issue["title"],
        description=issue.get("description", ""),
        context=ctx,
        notes_section=notes_section,
        retry_section=retry_section,
        worktree_path=worktree_path,
    )


def _read_project_context(project_path: str) -> Optional[str]:
    """Read .hive/project-context.md from a project root if it exists."""
    context_file = Path(project_path) / PROJECT_CONTEXT_FILE
    if context_file.exists():
        try:
            return context_file.read_text().strip()
        except OSError:
            pass
    return None


def build_system_prompt(project: str, agent_name: str, worktree_path: Optional[str] = None) -> str:
    """Build the system prompt for an agent session."""
    template_str = _load_template("system")
    base = Template(template_str).safe_substitute(
        agent_name=agent_name,
        project=project,
    )

    res = base.rstrip()

    if worktree_path:
        claude_md = Path(worktree_path) / "CLAUDE.md"
        if claude_md.exists():
            res += f"\n\n## Project Instructions\n\n{claude_md.read_text()}"

        project_context = _read_project_context(worktree_path)
        if project_context:
            res += f"\n\n{project_context}"

    return res


def assess_completion(file_result: Optional[Dict[str, Any]] = None) -> CompletionResult:
    """Assess completion from the parsed result file."""
    if file_result is not None:
        status = file_result.get("status", "unknown")
        summary = file_result.get("summary", "")
        blockers = file_result.get("blockers", [])
        artifacts_list = file_result.get("artifacts", [])

        artifacts = _artifacts_from_list(artifacts_list)

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

    return CompletionResult(
        success=False,
        reason="Worker did not write completion signal (.hive-result.jsonl)",
        summary="",
    )


def read_result_file(worktree_path: str) -> Optional[Dict[str, Any]]:
    """Read and parse ``.hive-result.jsonl`` from a worktree."""
    return _read_first_jsonl(Path(worktree_path) / RESULT_FILE_NAME)


def remove_result_file(worktree_path: str) -> bool:
    """Remove ``.hive-result.jsonl`` from a worktree if present."""
    return _remove_file(Path(worktree_path) / RESULT_FILE_NAME)


def read_notes_file(worktree_path: str) -> List[Dict[str, Any]]:
    """Read .hive-notes.jsonl from a worktree. Returns list of note dicts, or empty list."""
    try:
        notes = _read_first_jsonl(Path(worktree_path) / NOTES_FILE_NAME)
        if notes is None:
            return []
        notes_path = Path(worktree_path) / NOTES_FILE_NAME
        parsed_notes = [notes]
        for line in notes_path.read_text().splitlines()[1:]:
            line = line.strip()
            if line:
                parsed_notes.append(json.loads(line))
        return parsed_notes
    except (json.JSONDecodeError, OSError):
        return []


def remove_notes_file(worktree_path: str) -> bool:
    """Remove .hive-notes.jsonl from a worktree. Returns True if file existed."""
    return _remove_file(Path(worktree_path) / NOTES_FILE_NAME)


def build_refinery_system_prompt(project_path: str) -> str:
    """Build the system prompt for a refinery session.

    Injects the project's CLAUDE.md and project-context.md (if present) so
    the refinery knows project conventions when reviewing merges.
    """
    parts = ["You are the Refinery for this project. You integrate completed worker branches into main."]

    claude_md = Path(project_path) / "CLAUDE.md"
    if claude_md.exists():
        parts.append(f"\n## Project Instructions\n\n{claude_md.read_text()}")

    project_context = _read_project_context(project_path)
    if project_context:
        parts.append(f"\n{project_context}")

    return "\n".join(parts)


def build_refinery_prompt(
    issue_title: str,
    issue_id: str,
    branch_name: str,
    worktree_path: str,
    agent_name: Optional[str] = None,
    test_command: Optional[str] = None,
    notes: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Build the refinery prompt for merge processing."""
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

    notes_section = ""
    if notes:
        note_lines = []
        for note in notes:
            category = note.get("category", "discovery")
            content = note.get("content", "")
            source = note.get("issue_id", "project")
            note_lines.append(f"- [{category}] {content} (from {source})")
        notes_section = "\n\n## Project Notes\n" + "\n".join(note_lines)

    template_str = _load_template("refinery")
    prompt = Template(template_str).safe_substitute(
        issue_id=issue_id,
        issue_title=issue_title,
        branch_name=branch_name,
        worktree_path=worktree_path,
        worker_line=worker_line,
        problem=problem,
        test_step=test_step,
    )

    if notes_section:
        prompt += notes_section

    return prompt
