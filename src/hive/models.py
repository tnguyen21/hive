"""Data models for Hive orchestrator."""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class CompletionResult:
    """Result of assessing a worker's completion."""

    success: bool
    reason: str
    summary: str
    artifacts: Dict[str, Any] = field(default_factory=dict)

    @property
    def git_commit(self) -> Optional[str]:
        """Get git commit hash from artifacts."""
        return self.artifacts.get("git_commit")


@dataclass
class AgentIdentity:
    """Agent identity and context."""

    agent_id: str
    name: str
    issue_id: str
    worktree: str
    session_id: str
