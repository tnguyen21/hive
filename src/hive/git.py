"""Git worktree management for agent sandboxes."""

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


def remove_worktree(worktree_path: str, force: bool = False):
    """
    Remove a git worktree.

    Args:
        worktree_path: Path to the worktree to remove
        force: Force removal even if there are uncommitted changes

    Raises:
        GitWorktreeError: If worktree removal fails
    """
    worktree_path = Path(worktree_path).resolve()

    if not worktree_path.exists():
        # Already removed, nothing to do
        return

    try:
        # Get the main git directory by finding the .git file in worktree
        # (worktrees have a .git file, not directory)
        git_file = worktree_path / ".git"
        if git_file.is_file():
            # Read the gitdir path from .git file
            with open(git_file) as f:
                gitdir_line = f.read().strip()
                if gitdir_line.startswith("gitdir: "):
                    # Extract main repo path from gitdir
                    # gitdir: /path/to/main/.git/worktrees/<name>
                    gitdir = Path(gitdir_line[8:])
                    main_repo = gitdir.parent.parent

                    cmd = ["git", "worktree", "remove", str(worktree_path)]
                    if force:
                        cmd.append("--force")

                    subprocess.run(
                        cmd,
                        cwd=str(main_repo),
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    return

        # If we couldn't find main repo, try to remove directly
        subprocess.run(
            ["git", "worktree", "remove", str(worktree_path)] + (["--force"] if force else []),
            check=True,
            capture_output=True,
            text=True,
        )

    except subprocess.CalledProcessError as e:
        raise GitWorktreeError(f"Failed to remove worktree: {e.stderr}") from e


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


def get_current_branch(worktree_path: str) -> str:
    """
    Get the current branch name in a worktree.

    Args:
        worktree_path: Path to the worktree

    Returns:
        Branch name

    Raises:
        GitWorktreeError: If getting branch fails
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(worktree_path),
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        raise GitWorktreeError(f"Failed to get current branch: {e.stderr}") from e


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
