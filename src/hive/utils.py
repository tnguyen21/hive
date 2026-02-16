"""Shared utilities: ID generation, project detection, data models."""

import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional


# --- ID generation ---


def generate_id(prefix: str = "w", length: int = 12) -> str:
    """Generate a hash-based ID with a prefix.

    Returns ID string in format "{prefix}-{hash[:length]}" (e.g., "w-a3f8b1c4d2e5").
    If prefix is empty, returns just the hash suffix.
    """
    unique_part = uuid.uuid4().hex[:length]
    if prefix:
        return f"{prefix}-{unique_part}"
    return unique_part


# --- Data models ---


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


# --- Project detection ---


def _parse_repo_name(remote_url: str) -> str | None:
    """Extract repository name from a git remote URL."""
    url = remote_url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    for sep in (":", "/"):
        if sep in url:
            name = url.rsplit(sep, 1)[-1]
            if name:
                return name
    return None


def _git_remote_name(project_root: Path) -> str | None:
    """Get the repo name from git remote origin."""
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return _parse_repo_name(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None


def detect_project(cwd: Path | None = None) -> tuple[Path, str]:
    """Detect the current project root and name.

    Walks up from *cwd* looking for a ``.git/`` directory. Project name is
    resolved from ``.hive.toml``, git remote origin, or directory name.

    Returns ``(project_path, project_name)``.
    Raises SystemExit if no ``.git/`` directory is found.
    """
    import sys

    start = Path(cwd) if cwd else Path.cwd()
    current = start.resolve()

    while True:
        if (current / ".git").exists():
            break
        parent = current.parent
        if parent == current:
            print(f"fatal: not a git repository (searched up from {start})", file=sys.stderr)
            sys.exit(128)
        current = parent

    project_root = current

    # 1. Try .hive.toml
    hive_toml = project_root / ".hive.toml"
    if hive_toml.exists():
        import tomllib

        with open(hive_toml, "rb") as f:
            data = tomllib.load(f)
        name = (data.get("project") or {}).get("name")
        if name:
            return project_root, name

    # 2. Try git remote origin
    remote_name = _git_remote_name(project_root)
    if remote_name:
        return project_root, remote_name

    # 3. Fallback to directory name
    return project_root, project_root.name
