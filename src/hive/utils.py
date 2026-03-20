"""Shared utilities: ID generation, project detection, data models, logging."""

import logging
import logging.handlers
import os
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional


# --- Logging ---


def configure_logging() -> None:
    """Configure logging for the Hive application.

    Creates a root 'hive' logger with:
    - Console handler with formatted output
    - File handler with rotation (if not in CLI context)
    - Configurable log level via HIVE_LOG_LEVEL environment variable
    """
    log_level = os.environ.get("HIVE_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, log_level, logging.INFO)

    logger = logging.getLogger("hive")
    logger.setLevel(level)

    if logger.handlers:
        return

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(level)
    logger.addHandler(console_handler)

    # Skip file logging in CLI context (unless explicitly opted in)
    is_cli = os.environ.get("HIVE_ENABLE_FILE_LOGGING") != "1" and os.environ.get("HIVE_CLI_CONTEXT") == "1"
    if not is_cli:
        log_dir = Path.home() / ".hive" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "hive.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)
        logger.addHandler(file_handler)


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


@dataclass(frozen=True)
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


@dataclass(slots=True, frozen=True)
class AgentIdentity:
    """Agent identity and context."""

    agent_id: str
    name: str
    issue_id: str
    worktree: str
    session_id: str
    project: str = ""


# --- Project detection ---


def _parse_repo_name(remote_url: str) -> str | None:
    """Extract repository name (bare, no slashes) from a git remote URL.

    Works for:
    - SSH:   git@github.com:org/repo.git  → "repo"
    - HTTPS: https://github.com/org/repo.git → "repo"

    INV-1: Always returns a bare name with no slashes.
    """
    url = remote_url.strip().rstrip("/")
    url = url.removesuffix(".git")
    # Strip colon-delimited host prefix (SSH URLs: "git@host:path")
    if ":" in url:
        url = url.split(":", 1)[1]
    # Take the last non-empty path component
    parts = [p for p in url.split("/") if p]
    return parts[-1] if parts else None


def _normalize_project_name(name: str) -> str:
    """Normalize a project name to the bare repo form (no slashes).

    If *name* contains a slash (e.g. "org/repo"), the portion after the last
    slash is returned.  Plain names (e.g. "repo") are returned unchanged.
    """
    if "/" in name:
        return name.rsplit("/", 1)[-1]
    return name


def _git_remote_name(project_root: Path) -> str | None:
    """Get the repo name from git remote origin."""
    try:
        res = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0 and res.stdout.strip():
            return _parse_repo_name(res.stdout)
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
