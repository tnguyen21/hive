"""Git worktree management for agent sandboxes."""

import os
import shutil
import subprocess
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

    # Branch name: agent/<agent_name>
    branch_name = f"agent/{agent_name}"

    try:
        # Create worktree with a new branch
        subprocess.run(
            [
                "git",
                "worktree",
                "add",
                "-b",
                branch_name,
                str(worktree_dir),
                base_branch,
            ],
            cwd=str(project_path),
            check=True,
            capture_output=True,
            text=True,
        )

        return str(worktree_dir)

    except subprocess.CalledProcessError as e:
        raise GitWorktreeError(f"Failed to create worktree: {e.stderr}") from e


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
        subprocess.run(
            ["git", "checkout", main_branch],
            cwd=str(project_path),
            check=True,
            capture_output=True,
            text=True,
        )

        # Fast-forward only merge
        subprocess.run(
            ["git", "merge", "--ff-only", branch_name],
            cwd=str(project_path),
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        raise GitWorktreeError(f"Failed to merge {branch_name} to {main_branch}: {e.stderr}") from e


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
