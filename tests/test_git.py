"""Tests for git worktree management."""

import subprocess
from pathlib import Path

import pytest

from hive.git import (
    GitWorktreeError,
    abort_rebase,
    create_worktree,
    delete_branch,
    get_commit_hash,
    get_current_branch,
    merge_to_main,
    rebase_onto_main,
    remove_worktree,
    run_command_in_worktree,
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
    subprocess.run(["git", "branch", "-M", "main"], cwd=repo_path, check=True, capture_output=True)

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


# --- Tests for merge/rebase operations ---


@pytest.fixture
def git_repo_with_worktree(git_repo):
    """Create a git repo with a worktree that has diverged from main."""
    worktree_path = create_worktree(str(git_repo), "merge-test")

    # Make a commit on the worktree branch
    (Path(worktree_path) / "feature.py").write_text("# new feature\n")
    subprocess.run(["git", "add", "."], cwd=worktree_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Add feature"],
        cwd=worktree_path,
        check=True,
        capture_output=True,
    )

    return git_repo, worktree_path


def test_rebase_onto_main_clean(git_repo_with_worktree):
    """Test clean rebase when no conflicts exist."""
    git_repo, worktree_path = git_repo_with_worktree

    result = rebase_onto_main(worktree_path)
    assert result is True

    # Clean up
    remove_worktree(worktree_path)


def test_rebase_onto_main_with_conflict(git_repo_with_worktree):
    """Test rebase returns False when conflicts occur."""
    git_repo, worktree_path = git_repo_with_worktree

    # Make a conflicting commit on main
    (git_repo / "feature.py").write_text("# conflicting content on main\n")
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Conflicting change on main"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )

    result = rebase_onto_main(worktree_path)
    assert result is False

    # Abort the rebase so we can clean up
    abort_rebase(worktree_path)
    remove_worktree(worktree_path)


def test_abort_rebase(git_repo_with_worktree):
    """Test aborting a rebase in progress."""
    git_repo, worktree_path = git_repo_with_worktree

    # Create a conflict
    (git_repo / "feature.py").write_text("# conflict\n")
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Conflict"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )

    rebase_onto_main(worktree_path)  # Will fail with conflict
    abort_rebase(worktree_path)  # Should succeed

    # Verify we're back to a clean state
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == ""

    remove_worktree(worktree_path)


def test_abort_rebase_no_rebase_in_progress(git_repo):
    """Test aborting when no rebase is in progress doesn't error."""
    worktree_path = create_worktree(str(git_repo), "no-rebase")
    abort_rebase(worktree_path)  # Should not raise
    remove_worktree(worktree_path)


def test_merge_to_main(git_repo_with_worktree):
    """Test fast-forward merge to main."""
    git_repo, worktree_path = git_repo_with_worktree
    branch_name = "agent/merge-test"

    # Rebase first (should be clean since main hasn't moved)
    assert rebase_onto_main(worktree_path) is True

    # Now merge to main from the main repo
    merge_to_main(str(git_repo), branch_name)

    # Verify main now has the feature file
    assert (git_repo / "feature.py").exists()

    # Verify we're on main
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=git_repo,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "main"

    remove_worktree(worktree_path)


def test_merge_to_main_not_ff(git_repo_with_worktree):
    """Test merge fails when not fast-forwardable."""
    git_repo, worktree_path = git_repo_with_worktree
    branch_name = "agent/merge-test"

    # Make a commit on main so ff-only will fail
    (git_repo / "other.py").write_text("# other\n")
    subprocess.run(["git", "add", "."], cwd=git_repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Diverge main"],
        cwd=git_repo,
        check=True,
        capture_output=True,
    )

    with pytest.raises(GitWorktreeError, match="Failed to merge"):
        merge_to_main(str(git_repo), branch_name)

    remove_worktree(worktree_path)


def test_run_command_in_worktree_success(git_repo):
    """Test running a command that succeeds."""
    success, output = run_command_in_worktree(str(git_repo), "echo hello")
    assert success is True
    assert "hello" in output


def test_run_command_in_worktree_failure(git_repo):
    """Test running a command that fails."""
    success, output = run_command_in_worktree(str(git_repo), "false")
    assert success is False


def test_run_command_in_worktree_timeout(git_repo):
    """Test running a command that times out."""
    success, output = run_command_in_worktree(str(git_repo), "sleep 10", timeout=1)
    assert success is False
    assert "timed out" in output.lower()
