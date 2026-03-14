"""Git worktree management for agent sandboxes."""

import asyncio
import functools
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
    res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and res.returncode != 0:
        raise GitWorktreeError(res.stderr.strip())
    return res.stdout.strip()


def _async_wrapper(fn):
    """Turn a sync git function into an async one via run_in_executor."""

    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))

    return wrapper


def create_worktree(project_path: str, agent_name: str, base_branch: str = "main") -> str:
    """Create a git worktree for an agent.

    Returns path to the created worktree directory.
    """
    root = Path(project_path).resolve()

    if not (root / ".git").exists():
        raise GitWorktreeError(f"Not a git repository: {root}")

    worktree_dir = root / ".worktrees" / agent_name
    worktree_dir.parent.mkdir(parents=True, exist_ok=True)
    branch_name = f"agent/{agent_name}"

    # Retry with backoff — concurrent worktree creation can transiently fail
    # with "invalid reference: main" when git ref resolution hits contention.
    max_retries = 4
    last_error: Optional[GitWorktreeError] = None
    for attempt in range(max_retries):
        try:
            _run_git("worktree", "add", "-b", branch_name, str(worktree_dir), base_branch, cwd=str(root))
            return str(worktree_dir)
        except GitWorktreeError as e:
            last_error = e
            is_transient = "invalid reference" in str(e) or "index.lock" in str(e)
            if is_transient and attempt < max_retries - 1:
                time.sleep(1.0 * (attempt + 1))
                continue
            raise GitWorktreeError(f"Failed to create worktree: {e}") from e
    raise GitWorktreeError(f"Failed to create worktree after {max_retries} retries: {last_error}") from last_error


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
    """Delete a git branch."""
    _run_git("branch", "-D" if force else "-d", branch_name, cwd=str(project_path))


def rebase_onto_main(worktree_path: str, main_branch: str = "main") -> bool:
    """Rebase the worktree branch onto the latest main branch.

    Returns True if rebase succeeded cleanly, False if conflicts occurred.
    """
    _run_git("fetch", "origin", main_branch, cwd=str(worktree_path), check=False)

    res = subprocess.run(
        ["git", "rebase", main_branch],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
    )
    if res.returncode == 0:
        return True
    if "CONFLICT" in res.stdout or "conflict" in res.stderr.lower() or "could not apply" in res.stderr.lower():
        return False
    if res.returncode in (1, 128):
        return False
    raise GitWorktreeError(f"Rebase failed unexpectedly: {res.stderr}")


def abort_rebase(worktree_path: str):
    """Abort an in-progress rebase."""
    try:
        _run_git("rebase", "--abort", cwd=str(worktree_path))
    except GitWorktreeError as e:
        if "no rebase in progress" in str(e).lower():
            return
        raise GitWorktreeError(f"Failed to abort rebase: {e}") from e


def merge_to_main(project_path: str, branch_name: str, main_branch: str = "main"):
    """Fast-forward merge a branch into main from the main project repo."""
    project_path = str(Path(project_path).resolve())
    try:
        _run_git("checkout", main_branch, cwd=str(project_path))
        _run_git("merge", "--ff-only", branch_name, cwd=str(project_path))
    except GitWorktreeError as e:
        raise GitWorktreeError(f"Failed to merge {branch_name} to {main_branch}: {e}") from e


def get_worktree_dirty_status(project_path: str) -> tuple[bool, str]:
    """Check whether a repository worktree has local changes."""
    project_path = str(Path(project_path).resolve())
    output = _run_git("status", "--porcelain", "--untracked-files=no", cwd=str(project_path))
    return (bool(output), output)


def run_command_in_worktree(worktree_path: str, cmd: str, timeout: int = 300) -> tuple:
    """Run an arbitrary shell command in a worktree.

    Returns (success: bool, output: str) where output is combined stdout+stderr.
    """
    try:
        res = subprocess.run(
            cmd,
            shell=True,
            cwd=str(worktree_path),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = res.stdout + res.stderr
        return (res.returncode == 0, output)
    except subprocess.TimeoutExpired:
        return (False, f"Command timed out after {timeout}s: {cmd}")
    except Exception as e:
        return (False, f"Command failed: {e}")


def get_commit_hash(worktree_path: str) -> Optional[str]:
    """Get the current commit hash in a worktree."""
    res = _run_git("rev-parse", "HEAD", cwd=str(worktree_path), check=False)
    return res or None


def has_diff_from_main(worktree_path: str, main_branch: str = "main") -> bool:
    """Check if the worktree branch has any commits relative to main branch."""
    output = _run_git("log", f"{main_branch}..HEAD", "--oneline", cwd=str(worktree_path))
    return bool(output)


# --- Async versions (run_in_executor wrappers) ---

create_worktree_async = _async_wrapper(create_worktree)
remove_worktree_async = _async_wrapper(remove_worktree)
rebase_onto_main_async = _async_wrapper(rebase_onto_main)
abort_rebase_async = _async_wrapper(abort_rebase)
merge_to_main_async = _async_wrapper(merge_to_main)
get_worktree_dirty_status_async = _async_wrapper(get_worktree_dirty_status)
run_command_in_worktree_async = _async_wrapper(run_command_in_worktree)
delete_branch_async = _async_wrapper(delete_branch)
has_diff_from_main_async = _async_wrapper(has_diff_from_main)
