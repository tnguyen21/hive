"""Prompt templates for Hive agents."""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from .models import CompletionResult

# Regex for parsing structured completion signal
COMPLETION_RE = re.compile(r":::COMPLETION\s*\n(.*?):::", re.DOTALL)


def build_worker_prompt(
    agent_name: str,
    issue: Dict[str, Any],
    worktree_path: str,
    branch_name: str,
    project: str,
    step_number: Optional[int] = None,
    total_steps: Optional[int] = None,
    molecule_title: Optional[str] = None,
    completed_steps: Optional[List[str]] = None,
) -> str:
    """
    Build the worker prompt for an issue.

    Args:
        agent_name: Name of the agent
        issue: Issue dict with title and description
        worktree_path: Path to the git worktree
        branch_name: Git branch name
        project: Project name
        step_number: Step number (for molecules)
        total_steps: Total steps (for molecules)
        molecule_title: Molecule title (for molecules)
        completed_steps: List of completed step summaries (for molecules)

    Returns:
        Formatted worker prompt string
    """
    # Build context section
    context_parts = [
        f"- You are working in a git worktree at: {worktree_path}",
        f"- Branch: {branch_name}",
    ]

    if step_number and total_steps and molecule_title:
        context_parts.append(
            f'- This is step {step_number} of {total_steps} in the workflow "{molecule_title}"'
        )

    context = "\n".join(context_parts)

    # Build completed steps section (for molecules)
    completed_section = ""
    if completed_steps:
        completed_section = "\n\n### Previous Steps (already completed)\n" + "\n".join(
            f"{i + 1}. {step}" for i, step in enumerate(completed_steps)
        )

    prompt = f"""You are agent '{agent_name}', working on project '{project}'.

## YOUR TASK

**{issue["title"]}**

{issue.get("description", "")}

## CONTEXT

{context}{completed_section}

## BEHAVIORAL CONTRACT

### The Propulsion Principle
You are a piston in a machine. The system's throughput depends on pistons firing.
When you have work, EXECUTE. No confirmation seeking, no clarifying questions,
no waiting for approval. Read the task, understand it, implement it, verify it,
commit it, signal completion. That is the entire cycle.

### No Approval Fallacy
There is NO approval step. There is NO confirmation. There is NO human reviewing
your work before you finish. Here is the failure mode you must avoid:

> You complete the work. You write a nice summary. Then you WAIT for approval
> that will never come. The orchestrator is async. Nobody is watching your
> terminal. You will sit there FOREVER.

THIS IS THE FAILURE MODE. When your implementation is complete and tests pass,
you commit and signal completion IMMEDIATELY. Do NOT:
- Output a summary and wait for "looks good"
- Ask "should I commit this?"
- Pause after finishing to see if there's feedback
- Wait for a human to press enter

### The Idle Worker Heresy
An idle worker is a system failure. The instant your implementation is done and
committed, output the COMPLETION signal. Do not review your work a third time.
Do not write a long retrospective. Do not sit idle. Complete, commit, signal. Go.

### Directory Discipline
**Stay in your worktree: {worktree_path}**
- ALL file edits must be within this directory
- NEVER cd to parent directories to edit files there
- If your worktree lacks dependencies, install them here
- Verify with `pwd` if uncertain

### Escalate and Move On
If you are blocked for more than 2-3 attempts at the same problem, STOP.
The system is async — no human is going to unblock you interactively.
1. Describe the blocker clearly and specifically
2. Include what you tried and what failed
3. Signal completion with status "blocked"
4. Do NOT spin. Do NOT wait for human input. Escalate and stop.

### Capability Ledger
Your work is recorded in a permanent capability ledger. Every completion builds
your track record. Every failure is recorded too. Execute with care — but execute.
Do not over-engineer. Do not gold-plate. Implement what was asked, verify it works,
commit, and stop. Quality comes from disciplined execution, not from endless polish.

## INSTRUCTIONS

1. Implement the task described above
2. Run tests/linting relevant to your changes
3. Make atomic, well-described git commits as you work
4. When finished, ensure ALL changes are committed and git status is clean
5. Do NOT push — the orchestrator handles that
6. Do NOT create pull requests — the orchestrator handles that

## COMPLETION SIGNAL

When you are finished, output a completion signal as the LAST thing in your response:

:::COMPLETION
status: success | blocked | failed
summary: <one-line summary of what was done>
files_changed: <number of files modified>
tests_run: <yes/no>
blockers: <description if blocked, otherwise "none">
artifacts:
  - type: git_commit
    value: <sha>
  - type: test_result
    value: pass | fail | skipped
:::

## CONSTRAINTS

- Stay within your worktree directory ({worktree_path})
- Do not modify files outside the project
- Do not access external services unless the task requires it
- If you encounter an issue outside your scope, note it in your final message
"""

    return prompt


def build_system_prompt(
    project: str, agent_name: str, worktree_path: Optional[str] = None
) -> str:
    """
    Build the system prompt for an agent session.

    Args:
        project: Project name
        agent_name: Agent name
        worktree_path: Path to worktree (if available, checks for CLAUDE.md)

    Returns:
        System prompt string
    """
    parts = [
        f"You are agent '{agent_name}' working autonomously on the '{project}' project.",
        "You are a piston in a machine. When you have work, EXECUTE. No confirmation, "
        "no questions, no waiting. Read, implement, verify, commit, signal. "
        "You execute tasks to completion without human interaction. "
        "Nobody is watching your terminal — do not wait for approval that will never come.",
        "When you finish, ensure all changes are committed with clean git status.",
    ]

    # Inject project-specific CLAUDE.md if it exists
    if worktree_path:
        claude_md = Path(worktree_path) / "CLAUDE.md"
        if claude_md.exists():
            parts.append(f"\n## Project Instructions\n\n{claude_md.read_text()}")

    return "\n\n".join(parts)


def assess_completion(messages: List[Dict[str, Any]]) -> CompletionResult:
    """
    Assess completion based on structured signal or heuristics.

    Args:
        messages: List of message dicts from OpenCode session

    Returns:
        CompletionResult with success status, reason, and artifacts
    """
    if not messages:
        return CompletionResult(
            success=False,
            reason="No messages in session",
            summary="",
        )

    # Get the last message
    last = messages[-1]
    parts = last.get("parts", [])

    # Extract all text from the last message
    text_parts = [p.get("text", "") for p in parts if p.get("type") == "text"]
    text = " ".join(text_parts)

    # Try to parse structured completion signal
    match = COMPLETION_RE.search(text)
    if match:
        try:
            payload = yaml.safe_load(match.group(1))
            status = payload.get("status", "unknown")
            artifacts_list = payload.get("artifacts", [])

            # Convert artifacts list to dict
            artifacts = {}
            if isinstance(artifacts_list, list):
                for artifact in artifacts_list:
                    if isinstance(artifact, dict):
                        art_type = artifact.get("type")
                        art_value = artifact.get("value")
                        if art_type:
                            artifacts[art_type] = art_value

            return CompletionResult(
                success=(status == "success"),
                reason=payload.get("blockers", "none") if status != "success" else "",
                summary=payload.get("summary", ""),
                artifacts=artifacts,
            )
        except (yaml.YAMLError, KeyError, TypeError):
            # Malformed completion signal, fall through to heuristics
            pass

    # Fallback: heuristic assessment
    text_lower = text.lower()

    # Check for blocker signals
    blocker_signals = [
        "blocked by",
        "cannot proceed",
        "need help",
        "unable to",
        "escalating",
        "stuck on",
        "waiting for",
        "requires human",
    ]
    if any(signal in text_lower for signal in blocker_signals):
        return CompletionResult(
            success=False,
            reason="Blocker detected in message",
            summary=text[:200],  # First 200 chars as summary
        )

    # Check for tool errors in the last message
    tool_errors = [
        p
        for p in parts
        if p.get("type") == "tool" and p.get("state", {}).get("status") == "error"
    ]
    if tool_errors:
        error_details = "; ".join(
            p.get("state", {}).get("output", "Unknown error")[:100] for p in tool_errors
        )
        return CompletionResult(
            success=False,
            reason=f"Tool errors: {error_details}",
            summary="Task incomplete due to tool errors",
        )

    # Check for success indicators
    success_signals = [
        "committed",
        "changes committed",
        "implementation complete",
        "task complete",
        "all tests passing",
        "successfully implemented",
    ]
    if any(signal in text_lower for signal in success_signals):
        return CompletionResult(
            success=True,
            reason="",
            summary=text[:200],
        )

    # Default: assume success if no blocker detected
    # (optimistic interpretation — agent finished without explicit signal)
    return CompletionResult(
        success=True,
        reason="",
        summary="Task appears complete (no explicit completion signal)",
    )
