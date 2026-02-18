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

    branch_name = f"agent/{agent_name}"

    # Two-step worktree creation: detached first, then create branch.
    # Using --detach avoids writing to .git/refs/heads/agent/ during worktree
    # setup, which fails with EPERM on macOS when the daemon process inherits
    # com.apple.provenance (e.g. when started from Claude Code's sandbox).
    # The branch is created inside the worktree afterward, which works
    # because git checkout -b operates locally without the ref-lock contention.
    max_retries = 4
    for attempt in range(max_retries):
        try:
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(worktree_dir), base_branch],
                cwd=str(project_path),
                check=True,
                capture_output=True,
                text=True,
            )
            break
        except subprocess.CalledProcessError as e:
            is_transient = "invalid reference" in e.stderr or "index.lock" in e.stderr
            if is_transient and attempt < max_retries - 1:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise GitWorktreeError(f"Failed to create worktree: {e.stderr}") from e

    # Create the named branch inside the worktree (needed by merge path).
    try:
        subprocess.run(
            ["git", "checkout", "-b", branch_name],
            cwd=str(worktree_dir),
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        # Clean up the detached worktree on branch creation failure
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree_dir)], capture_output=True, cwd=str(project_path))
        raise GitWorktreeError(f"Failed to create branch in worktree: {e.stderr}") from e

    return str(worktree_dir)


def remove_worktree(worktree_path: str) -> bool:
    """Remove a git worktree."""
    abs_path = os.path.abspath(worktree_path)
    if not os.path.exists(abs_path):
        return False
    try:
        subprocess.run(
            ["git", "worktree", "remove", "--force", abs_path],
            capture_output=True,
            text=True,
            check=True,
            cwd=os.path.dirname(abs_path),  # any valid directory works
        )
        return True
    except subprocess.CalledProcessError:
        # Fallback: force remove if worktree is dirty
        try:
            shutil.rmtree(abs_path, ignore_errors=True)
            subprocess.run(["git", "worktree", "prune"], capture_output=True, cwd=os.path.dirname(abs_path))
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
    try:
        cmd = ["git", "branch", "-D" if force else "-d", branch_name]
        subprocess.run(
            cmd,
            cwd=str(project_path),
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        raise GitWorktreeError(f"Failed to delete branch: {e.stderr}") from e


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
    try:
        subprocess.run(
            ["git", "fetch", "origin", main_branch],
            cwd=str(worktree_path),
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError:
        pass  # fetch may fail if no remote, continue with local

    try:
        subprocess.run(
            ["git", "rebase", main_branch],
            cwd=str(worktree_path),
            check=True,
            capture_output=True,
            text=True,
        )
        return True
    except subprocess.CalledProcessError as e:
        # Conflict detection: rebase exits non-zero on conflicts
        if "CONFLICT" in e.stdout or "conflict" in e.stderr.lower() or "could not apply" in e.stderr.lower():
            return False
        # Also treat merge failures as conflicts (rebase couldn't apply)
        if e.returncode in (1, 128):
            return False
        raise GitWorktreeError(f"Rebase failed unexpectedly: {e.stderr}") from e


def abort_rebase(worktree_path: str):
    """
    Abort an in-progress rebase.

    Args:
        worktree_path: Path to the worktree

    Raises:
        GitWorktreeError: If abort fails
    """
    try:
        subprocess.run(
            ["git", "rebase", "--abort"],
            cwd=str(worktree_path),
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        # If no rebase in progress, that's fine
        if "no rebase in progress" in e.stderr.lower():
            return
        raise GitWorktreeError(f"Failed to abort rebase: {e.stderr}") from e


def merge_to_main(project_path: str, branch_name: str, main_branch: str = "main"):
    """
    Fast-forward merge a branch into main from the main project repo.

    Uses git update-ref instead of checkout+merge to avoid creating
    .git/index.lock, which fails with EPERM on macOS when the daemon
    process inherits com.apple.provenance from Claude Code's sandbox.

    Args:
        project_path: Path to the main git repository
        branch_name: Branch to merge (e.g. "agent/worker-abc123")
        main_branch: Target branch (default: main)

    Raises:
        GitWorktreeError: If merge fails
    """
    project_path = Path(project_path).resolve()

    try:
        # Verify fast-forward is possible: main must be an ancestor of branch
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", main_branch, branch_name],
            cwd=str(project_path),
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError:
        raise GitWorktreeError(f"Cannot fast-forward {main_branch} to {branch_name}: {main_branch} is not an ancestor of {branch_name}")

    try:
        # Update the ref directly (no checkout, no index.lock needed)
        subprocess.run(
            ["git", "update-ref", f"refs/heads/{main_branch}", branch_name],
            cwd=str(project_path),
            check=True,
            capture_output=True,
            text=True,
        )

        # Sync working tree to match the updated ref. update-ref only moves
        # the branch pointer; without this the working tree is stale.
        subprocess.run(
            ["git", "reset", "--hard", main_branch],
            cwd=str(project_path),
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        raise GitWorktreeError(f"Failed to merge {branch_name} to {main_branch}: {e.stderr}") from e


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
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=str(project_path),
            check=True,
            capture_output=True,
            text=True,
        )
        output = result.stdout.strip()
        return (bool(output), output)
    except subprocess.CalledProcessError as e:
        raise GitWorktreeError(f"Failed to check worktree status: {e.stderr}") from e


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
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(worktree_path),
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return None


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
    try:
        result = subprocess.run(
            ["git", "log", f"{main_branch}..HEAD", "--oneline"],
            cwd=str(worktree_path),
            check=True,
            capture_output=True,
            text=True,
        )
        return bool(result.stdout.strip())
    except subprocess.CalledProcessError as e:
        raise GitWorktreeError(f"Failed to check diff from main: {e.stderr}") from e


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
