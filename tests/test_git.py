"""Tests for git worktree management."""

import subprocess
from pathlib import Path

import pytest

from hive.git import (
    GitWorktreeError,
    create_worktree,
    delete_branch,
    get_commit_hash,
    get_current_branch,
    remove_worktree,
)


@pytest.fixture
def git_repo(tmp_path):
    """Create a temporary git repository for testing."""
    repo_path = tmp_path / "test_repo"
    repo_path.mkdir()

    # Initialize git repo
    subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    # Create initial commit
    (repo_path / "README.md").write_text("# Test Repo\n")
    subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )

    # Create main branch (in case default is master)
    subprocess.run(
        ["git", "branch", "-M", "main"], cwd=repo_path, check=True, capture_output=True
    )

    return repo_path


def test_create_worktree(git_repo):
    """Test creating a git worktree."""
    worktree_path = create_worktree(str(git_repo), "test-agent")

    assert Path(worktree_path).exists()
    assert Path(worktree_path).is_dir()
    assert (Path(worktree_path) / "README.md").exists()

    # Check branch name
    branch = get_current_branch(worktree_path)
    assert branch == "agent/test-agent"

    # Clean up
    remove_worktree(worktree_path)


def test_create_worktree_not_git_repo(tmp_path):
    """Test creating worktree in non-git directory fails."""
    non_git_dir = tmp_path / "not_git"
    non_git_dir.mkdir()

    with pytest.raises(GitWorktreeError, match="Not a git repository"):
        create_worktree(str(non_git_dir), "test-agent")


def test_remove_worktree(git_repo):
    """Test removing a git worktree."""
    worktree_path = create_worktree(str(git_repo), "test-agent")
    assert Path(worktree_path).exists()

    remove_worktree(worktree_path)

    # Worktree directory should be removed
    assert not Path(worktree_path).exists()


def test_remove_nonexistent_worktree(tmp_path):
    """Test removing a nonexistent worktree doesn't error."""
    # Should not raise
    remove_worktree(str(tmp_path / "nonexistent"))


def test_get_current_branch(git_repo):
    """Test getting current branch name."""
    worktree_path = create_worktree(str(git_repo), "test-agent")

    branch = get_current_branch(worktree_path)
    assert branch == "agent/test-agent"

    # Clean up
    remove_worktree(worktree_path)


def test_get_commit_hash(git_repo):
    """Test getting commit hash."""
    commit_hash = get_commit_hash(str(git_repo))

    assert commit_hash is not None
    assert len(commit_hash) == 40  # Full SHA-1 hash
    assert all(c in "0123456789abcdef" for c in commit_hash)


def test_delete_branch(git_repo):
    """Test deleting a branch."""
    # Create a worktree first
    worktree_path = create_worktree(str(git_repo), "test-agent")
    branch_name = "agent/test-agent"

    # Remove worktree
    remove_worktree(worktree_path)

    # Delete the branch
    delete_branch(str(git_repo), branch_name, force=True)

    # Verify branch is gone
    result = subprocess.run(
        ["git", "branch", "--list", branch_name],
        cwd=str(git_repo),
        capture_output=True,
        text=True,
    )
    assert branch_name not in result.stdout
