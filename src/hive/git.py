"""Git worktree management for agent sandboxes."""

import asyncio
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional


class GitWorktreeError(Exception):
    """Exception raised for git worktree operations."""

    pass


def _run_git(*args: str, cwd: str, check: bool = True) -> str:
    """Run a git command and return stripped stdout. Raises GitWorktreeError on non-zero exit if check=True."""
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise GitWorktreeError(result.stderr.strip())
    return result.stdout.strip()


def create_worktree(project_path: str, agent_name: str, base_branch: str = "main") -> str:
    """
    Create a git worktree for an agent.

    Args:
        project_path: Path to the main git repository
        agent_name: Name of the agent (used for branch and directory name)
        base_branch: Base branch to branch from (default: main)

    Returns:
        Path to the created worktree directory

    Raises:
        GitWorktreeError: If worktree creation fails
    """
    project_path = Path(project_path).resolve()

    if not (project_path / ".git").exists():
        raise GitWorktreeError(f"Not a git repository: {project_path}")

    # Worktree directory: <project>/.worktrees/<agent_name>
    worktree_dir = project_path / ".worktrees" / agent_name
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)

    # Branch name: agent/<agent_name>
    branch_name = f"agent/{agent_name}"

    # Retry with backoff — concurrent worktree creation can transiently fail
    # with "invalid reference: main" when git ref resolution hits contention.
    # TODO: Investigate a proper fix for git ref contention instead of this
    # sleep-and-retry hack. It's unclear whether longer sleeps actually help
    # or if the root cause is something else entirely (e.g. packed-refs
    # rewriting, loose ref gc, or worktree metadata races).
    max_retries = 4
    for attempt in range(max_retries):
        try:
            _run_git("worktree", "add", "-b", branch_name, str(worktree_dir), base_branch, cwd=str(project_path))
            return str(worktree_dir)

        except GitWorktreeError as e:
            is_transient = "invalid reference" in str(e) or "index.lock" in str(e)
            if is_transient and attempt < max_retries - 1:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise GitWorktreeError(f"Failed to create worktree: {e}") from e


def remove_worktree(worktree_path: str) -> bool:
    """Remove a git worktree."""
    abs_path = os.path.abspath(worktree_path)
    if not os.path.exists(abs_path):
        return False
    try:
        _run_git("worktree", "remove", "--force", abs_path, cwd=os.path.dirname(abs_path))
        return True
    except GitWorktreeError:
        # Fallback: force remove if worktree is dirty
        try:
            shutil.rmtree(abs_path, ignore_errors=True)
            _run_git("worktree", "prune", cwd=os.path.dirname(abs_path), check=False)
            return True
        except Exception:
            return False


def delete_branch(project_path: str, branch_name: str, force: bool = False):
    """
    Delete a git branch.

    Args:
        project_path: Path to the git repository
        branch_name: Name of the branch to delete
        force: Force deletion even if not fully merged

    Raises:
        GitWorktreeError: If branch deletion fails
    """
    _run_git("branch", "-D" if force else "-d", branch_name, cwd=str(project_path))


def rebase_onto_main(worktree_path: str, main_branch: str = "main") -> bool:
    """
    Rebase the worktree branch onto the latest main branch.

    Args:
        worktree_path: Path to the worktree
        main_branch: Branch to rebase onto (default: main)

    Returns:
        True if rebase succeeded cleanly, False if conflicts occurred.

    Raises:
        GitWorktreeError: On unexpected git failures (not conflicts)
    """
    _run_git("fetch", "origin", main_branch, cwd=str(worktree_path), check=False)  # failure ok if no remote

    result = subprocess.run(
        ["git", "rebase", main_branch],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return True
    # Conflict detection: rebase exits non-zero on conflicts
    if "CONFLICT" in result.stdout or "conflict" in result.stderr.lower() or "could not apply" in result.stderr.lower():
        return False
    # Also treat merge failures as conflicts (rebase couldn't apply)
    if result.returncode in (1, 128):
        return False
    raise GitWorktreeError(f"Rebase failed unexpectedly: {result.stderr}")


def abort_rebase(worktree_path: str):
    """
    Abort an in-progress rebase.

    Args:
        worktree_path: Path to the worktree

    Raises:
        GitWorktreeError: If abort fails
    """
    try:
        _run_git("rebase", "--abort", cwd=str(worktree_path))
    except GitWorktreeError as e:
        # If no rebase in progress, that's fine
        if "no rebase in progress" in str(e).lower():
            return
        raise GitWorktreeError(f"Failed to abort rebase: {e}") from e


def merge_to_main(project_path: str, branch_name: str, main_branch: str = "main"):
    """
    Fast-forward merge a branch into main from the main project repo.

    This runs in the MAIN repo (not a worktree). The branch should already
    be rebased onto main so ff-only succeeds.

    Args:
        project_path: Path to the main git repository
        branch_name: Branch to merge (e.g. "agent/worker-abc123")
        main_branch: Target branch (default: main)

    Raises:
        GitWorktreeError: If merge fails
    """
    project_path = Path(project_path).resolve()

    try:
        # Ensure we're on main
        _run_git("checkout", main_branch, cwd=str(project_path))
        # Fast-forward only merge
        _run_git("merge", "--ff-only", branch_name, cwd=str(project_path))
    except GitWorktreeError as e:
        raise GitWorktreeError(f"Failed to merge {branch_name} to {main_branch}: {e}") from e


def get_worktree_dirty_status(project_path: str) -> tuple[bool, str]:
    """
    Check whether a repository worktree has local changes.

    Args:
        project_path: Path to the git repository worktree

    Returns:
        Tuple of (is_dirty, porcelain_output)

    Raises:
        GitWorktreeError: If git status cannot be read
    """
    project_path = Path(project_path).resolve()
    output = _run_git("status", "--porcelain", "--untracked-files=no", cwd=str(project_path))
    return (bool(output), output)


def run_command_in_worktree(worktree_path: str, cmd: str, timeout: int = 300) -> tuple:
    """
    Run an arbitrary shell command in a worktree.

    Useful for running test commands, linters, etc.

    Args:
        worktree_path: Path to the worktree
        cmd: Shell command to run
        timeout: Timeout in seconds (default: 300)

    Returns:
        Tuple of (success: bool, output: str) where output is combined stdout+stderr
    """
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = result.stdout + result.stderr
        return (result.returncode == 0, output)
    except subprocess.TimeoutExpired:
        return (False, f"Command timed out after {timeout}s: {cmd}")
    except Exception as e:
        return (False, f"Command failed: {e}")


def get_commit_hash(worktree_path: str) -> Optional[str]:
    """
    Get the current commit hash in a worktree.

    Args:
        worktree_path: Path to the worktree

    Returns:
        Commit hash, or None if not in a git repo

    Raises:
        GitWorktreeError: If getting commit hash fails
    """
    result = _run_git("rev-parse", "HEAD", cwd=str(worktree_path), check=False)
    return result or None


def has_diff_from_main(worktree_path: str, main_branch: str = "main") -> bool:
    """
    Check if the worktree branch has any commits relative to main branch.

    Runs 'git log main..HEAD --oneline' and returns True if output is non-empty.
    This detects if the worker actually committed any changes.

    Args:
        worktree_path: Path to the worktree
        main_branch: Main branch to compare against (default: main)

    Returns:
        True if there are commits ahead of main, False otherwise

    Raises:
        GitWorktreeError: If git command fails
    """
    output = _run_git("log", f"{main_branch}..HEAD", "--oneline", cwd=str(worktree_path))
    return bool(output)


# --- Async wrappers ---
# These run blocking git operations in a thread executor to avoid
# blocking the asyncio event loop. Use these from async code instead
# of calling the sync versions directly.


async def create_worktree_async(project_path: str, agent_name: str, base_branch: str = "main") -> str:
    """Async wrapper for create_worktree. Runs in a thread executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, create_worktree, project_path, agent_name, base_branch)


async def remove_worktree_async(worktree_path: str) -> bool:
    """Async wrapper for remove_worktree. Runs in a thread executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, remove_worktree, worktree_path)


async def rebase_onto_main_async(worktree_path: str, main_branch: str = "main") -> bool:
    """Async wrapper for rebase_onto_main. Runs in a thread executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, rebase_onto_main, worktree_path, main_branch)


async def abort_rebase_async(worktree_path: str):
    """Async wrapper for abort_rebase. Runs in a thread executor."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, abort_rebase, worktree_path)


async def merge_to_main_async(project_path: str, branch_name: str, main_branch: str = "main"):
    """Async wrapper for merge_to_main. Runs in a thread executor."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, merge_to_main, project_path, branch_name, main_branch)


async def get_worktree_dirty_status_async(project_path: str) -> tuple[bool, str]:
    """Async wrapper for get_worktree_dirty_status. Runs in a thread executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_worktree_dirty_status, project_path)


async def run_command_in_worktree_async(worktree_path: str, cmd: str, timeout: int = 300) -> tuple:
    """Async wrapper for run_command_in_worktree. Runs in a thread executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, run_command_in_worktree, worktree_path, cmd, timeout)


async def delete_branch_async(project_path: str, branch_name: str, force: bool = False):
    """Async wrapper for delete_branch. Runs in a thread executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, delete_branch, project_path, branch_name, force)


async def has_diff_from_main_async(worktree_path: str, main_branch: str = "main") -> bool:
    """Async wrapper for has_diff_from_main. Runs in a thread executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, has_diff_from_main, worktree_path, main_branch)
