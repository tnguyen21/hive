"""Merge queue processor for Hive orchestrator.

Processes the done→finalized pipeline:
  1. Mechanical fast-path: rebase, test, ff-merge (no LLM)
  2. Refinery LLM: conflict resolution, test failure diagnosis
"""

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from .config import Config, WORKER_PERMISSIONS
from .db import Database
from .git import (
    GitWorktreeError,
    abort_rebase_async,
    delete_branch_async,
    merge_to_main_async,
    rebase_onto_main_async,
    remove_worktree_async,
    run_command_in_worktree_async,
)
from .backends import OpenCodeClient, make_model_config
from .prompts import build_refinery_prompt, read_notes_file, read_result_file, remove_notes_file, remove_result_file


class MergeProcessor:
    """Processes the merge queue: rebase, test, merge, finalize."""

    def __init__(
        self,
        db: Database,
        opencode: OpenCodeClient,
        project_path: str,
        project_name: str,
    ):
        self.db = db
        self.opencode = opencode
        self.project_path = str(Path(project_path).resolve())
        self.project_name = project_name
        self.refinery_session_id: Optional[str] = None
        self._refinery_message_count: int = 0
        self._refinery_token_estimate: int = 0

    async def shutdown(self):
        """Clean up the refinery session on shutdown."""
        if self.refinery_session_id:
            await self.opencode.cleanup_session(self.refinery_session_id, directory=self.project_path)
            self.refinery_session_id = None

    async def _force_reset_refinery_session(self, reason: str):
        """Force reset the refinery session after a failure.

        Args:
            reason: Description of why the session is being reset
        """
        if not self.refinery_session_id:
            return

        session_id = self.refinery_session_id
        self.refinery_session_id = None  # Clear immediately to prevent reuse

        # Reset counters
        self._refinery_message_count = 0
        self._refinery_token_estimate = 0

        # Best-effort abort and delete
        await self.opencode.cleanup_session(session_id, directory=self.project_path)

        # Log the reset event
        self.db.log_event(
            None,  # No specific issue
            None,  # No specific agent
            "refinery_session_reset",
            {"session_id": session_id, "reason": reason},
        )

    async def initialize(self):
        """Initialize the merge processor, including eager refinery session creation."""
        # Reset any merge entries stuck in 'running' from a previous crash.
        # Without this, a daemon crash mid-merge leaves the entry permanently stuck.
        try:
            cursor = self.db.conn.execute("SELECT COUNT(*) FROM merge_queue WHERE status = 'running'")
            stuck_count = cursor.fetchone()[0]
            if stuck_count > 0:
                self.db.conn.execute("UPDATE merge_queue SET status = 'queued' WHERE status = 'running'")
                self.db.conn.commit()
                self.db.log_system_event("stuck_merges_reset", {"count": stuck_count})
        except Exception:
            pass  # Non-fatal

        try:
            # Pre-create refinery session so it's warm when first merge arrives
            await self._ensure_refinery_session()
        except Exception:
            # Non-fatal if creation fails, will fall back to lazy creation
            pass

    async def health_check(self) -> bool:
        """Check if the refinery session is alive, recreate if needed.

        Returns:
            True if healthy (or successfully recreated), False if failed to recreate
        """
        if not self.refinery_session_id:
            # No session exists, try to create one
            try:
                await self._ensure_refinery_session()
                return True
            except Exception:
                return False

        try:
            # Check if existing session is still alive
            status = await self.opencode.get_session_status(self.refinery_session_id, directory=self.project_path)
            if status is not None:
                return True  # Session is alive

            # Session is dead, recreate
            self.refinery_session_id = None
            await self._ensure_refinery_session()
            return True

        except Exception:
            # Failed to check or recreate
            self.refinery_session_id = None
            return False

    async def process_queue_once(self):
        """Process the next item in the merge queue. One at a time, sequential."""
        entries = self.db.get_queued_merges(project=self.project_name, limit=1)
        if not entries:
            return

        entry = entries[0]
        queue_id = entry["id"]

        # Mark as running
        self.db.update_merge_queue_status(queue_id, "running")
        self.db.log_event(
            entry["issue_id"],
            entry.get("agent_id"),
            "merge_started",
            {"queue_id": queue_id, "branch": entry["branch_name"]},
        )

        try:
            # Tier 1: Mechanical merge
            success, test_output = await self._try_mechanical_merge(entry)

            if success:
                await self._finalize_issue(entry)
            else:
                # Tier 2: Send to Refinery LLM
                await self._send_to_refinery(entry, test_output)

        except Exception as e:
            # Unexpected error — mark queue entry failed, leave issue as-is
            self.db.update_merge_queue_status(queue_id, "failed")
            self.db.log_event(
                entry["issue_id"],
                entry.get("agent_id"),
                "merge_error",
                {"error": str(e), "queue_id": queue_id},
            )

    async def _try_mechanical_merge(self, entry: Dict[str, Any]) -> tuple:
        """
        Try mechanical rebase + test + merge. No LLM involved.

        Returns:
            (success: bool, test_output: str | None)
        """
        worktree = entry["worktree"]
        branch_name = entry["branch_name"]
        issue_id = entry["issue_id"]
        agent_id = entry.get("agent_id")

        # Step 1: Rebase onto main (in executor to avoid blocking event loop)
        rebase_ok = await rebase_onto_main_async(worktree)
        if not rebase_ok:
            self.db.log_event(issue_id, agent_id, "rebase_conflict", {"branch": branch_name})
            await abort_rebase_async(worktree)

            # Create structured rejection note for rebase conflict
            self.db.add_note(
                issue_id=issue_id,
                agent_id=agent_id,
                category="rejection",
                content=f"[Merge conflict] Rebase onto main failed.\nBranch: {branch_name}",
                project=self.project_name,
            )

            return (False, None)

        self.db.log_event(issue_id, agent_id, "rebase_success", {"branch": branch_name})

        # Step 2: Run tests (if configured, in executor to avoid blocking event loop)
        test_output = None
        worker_test_cmd = entry.get("test_command")
        global_test_cmd = Config.TEST_COMMAND

        # Helper to create rejection note on test failure
        def _log_test_rejection(cmd, output):
            truncated_output = output[:500] if output else ""
            self.db.add_note(
                issue_id=issue_id,
                agent_id=agent_id,
                category="rejection",
                content=f"[Test failure] Tests failed after rebase.\nCommand: {cmd}\n```\n{truncated_output}\n```",
                project=self.project_name,
            )

        # If both worker and global test commands exist: run worker first (fast), then global (comprehensive)
        if worker_test_cmd and global_test_cmd:
            # Run worker-specific tests first (timeout 120s)
            worker_ok, worker_output = await run_command_in_worktree_async(worktree, worker_test_cmd, timeout=120)
            if not worker_ok:
                self.db.log_event(
                    issue_id,
                    agent_id,
                    "test_failure",
                    {"command": worker_test_cmd, "type": "worker", "output": worker_output[:2000]},
                )
                _log_test_rejection(worker_test_cmd, worker_output)
                return (False, worker_output)

            self.db.log_event(issue_id, agent_id, "tests_passed", {"command": worker_test_cmd, "type": "worker"})

            # Run global tests (timeout 300s)
            global_ok, global_output = await run_command_in_worktree_async(worktree, global_test_cmd, timeout=300)
            if not global_ok:
                self.db.log_event(
                    issue_id,
                    agent_id,
                    "test_failure",
                    {"command": global_test_cmd, "type": "global", "output": global_output[:2000]},
                )
                _log_test_rejection(global_test_cmd, global_output)
                return (False, global_output)

            self.db.log_event(issue_id, agent_id, "tests_passed", {"command": global_test_cmd, "type": "global"})

        elif worker_test_cmd:
            # Only worker test command - run it (timeout 120s)
            test_ok, test_output = await run_command_in_worktree_async(worktree, worker_test_cmd, timeout=120)
            if not test_ok:
                self.db.log_event(
                    issue_id,
                    agent_id,
                    "test_failure",
                    {"command": worker_test_cmd, "type": "worker", "output": test_output[:2000]},
                )
                _log_test_rejection(worker_test_cmd, test_output)
                return (False, test_output)

            self.db.log_event(issue_id, agent_id, "tests_passed", {"command": worker_test_cmd, "type": "worker"})

        elif global_test_cmd:
            # Only global test command - run it (timeout 300s)
            test_ok, test_output = await run_command_in_worktree_async(worktree, global_test_cmd, timeout=300)
            if not test_ok:
                self.db.log_event(
                    issue_id,
                    agent_id,
                    "test_failure",
                    {"command": global_test_cmd, "type": "global", "output": test_output[:2000]},
                )
                _log_test_rejection(global_test_cmd, test_output)
                return (False, test_output)

            self.db.log_event(issue_id, agent_id, "tests_passed", {"command": global_test_cmd, "type": "global"})

        # If neither test command exists, skip tests

        # Step 3: Merge to main (ff-only, in executor to avoid blocking event loop)
        try:
            await merge_to_main_async(self.project_path, branch_name)
        except GitWorktreeError as e:
            self.db.log_event(
                issue_id,
                agent_id,
                "merge_failed",
                {"error": str(e), "branch": branch_name},
            )
            return (False, str(e))

        self.db.log_event(issue_id, agent_id, "merged", {"branch": branch_name})
        return (True, None)

    async def _send_to_refinery(self, entry: Dict[str, Any], test_output: Optional[str] = None):
        """
        Hand a merge to the Refinery LLM for processing.

        Args:
            entry: Merge queue entry dict
            test_output: Test output if tests failed (None if rebase conflict)
        """
        queue_id = entry["id"]
        issue_id = entry["issue_id"]
        agent_id = entry.get("agent_id")
        rebase_ok = test_output is not None  # If we have test_output, rebase succeeded

        self.db.log_event(
            issue_id,
            agent_id,
            "refinery_dispatched",
            {
                "queue_id": queue_id,
                "reason": "test_failure" if rebase_ok else "rebase_conflict",
            },
        )

        worktree_path = entry["worktree"]

        try:
            session_id = await self._ensure_refinery_session()

            # Record message count before sending (fence against stale results)
            pre_send_count = self._refinery_message_count

            # Remove any stale result file before sending (belt-and-suspenders)
            remove_result_file(worktree_path)

            # Build the refinery prompt
            # Prefer worker test_command over global Config.TEST_COMMAND
            test_cmd = entry.get("test_command") or Config.TEST_COMMAND
            prompt = build_refinery_prompt(
                issue_title=entry.get("issue_title", "Unknown"),
                issue_id=issue_id,
                branch_name=entry["branch_name"],
                worktree_path=worktree_path,
                agent_name=entry.get("agent_name"),
                rebase_succeeded=rebase_ok,
                test_output=test_output,
                test_command=test_cmd,
            )

            # Send to refinery
            await self.opencode.send_message_async(
                session_id,
                parts=[{"type": "text", "text": prompt}],
                model=make_model_config(Config.REFINERY_MODEL),
                directory=self.project_path,
            )

            # Brief delay to check if message was picked up
            await asyncio.sleep(0.5)
            status = await self.opencode.get_session_status(session_id, directory=self.project_path)
            if status and status.get("type") == "idle":
                # Message wasn't picked up, session still idle
                raise RuntimeError("Refinery session did not pick up the message")

            # Wait for refinery to finish (poll session status)
            result = await self._wait_for_refinery(session_id, worktree_path=worktree_path, min_message_count=pre_send_count)

            # Increment counters after successful refinery processing
            self._refinery_message_count += 2  # one for the prompt sent, one for the response
            # Estimate tokens from prompt length
            self._refinery_token_estimate += len(prompt) // 4  # rough estimate for input

            # Process result
            if result["status"] == "merged":
                # Merge the branch to main — the refinery fixed the code in the
                # worktree but the branch still needs to land on main.
                branch_name = entry["branch_name"]
                try:
                    await merge_to_main_async(self.project_path, branch_name)
                except GitWorktreeError as e:
                    self.db.update_merge_queue_status(queue_id, "failed")
                    self.db.log_event(
                        issue_id,
                        agent_id,
                        "merge_failed",
                        {"error": str(e), "branch": branch_name, "after_refinery": True},
                    )
                    return

                await self._finalize_issue(entry)
                self.db.log_event(
                    issue_id,
                    agent_id,
                    "refinery_merged",
                    {"conflicts_resolved": result.get("conflicts_resolved", 0)},
                )
            elif result["status"] == "rejected":
                self.db.update_merge_queue_status(queue_id, "failed")
                # Reset issue to open so it can be reworked
                self.db.update_issue_status(issue_id, "open")
                self.db.log_event(
                    issue_id,
                    agent_id,
                    "merge_rejected",
                    {"summary": result.get("summary", "")},
                )

                # Create structured rejection note from refinery
                rejection_reason = result.get("summary", "Unknown reason")
                truncated_test_output = test_output[:500] if test_output else "N/A"
                note_content = f"[Refinery rejection] {rejection_reason}\nBranch: {entry['branch_name']}"
                if test_output:
                    note_content += f"\nTest output (truncated):\n```\n{truncated_test_output}\n```"

                self.db.add_note(
                    issue_id=issue_id,
                    agent_id=agent_id,
                    category="rejection",
                    content=note_content,
                    project=self.project_name,
                )
            else:
                # needs_human or unknown
                self.db.update_merge_queue_status(queue_id, "failed")
                self.db.update_issue_status(issue_id, "escalated")
                self.db.log_event(
                    issue_id,
                    agent_id,
                    "merge_escalated",
                    {"summary": result.get("summary", "")},
                )

            # Harvest notes from the worktree (refinery may have written .hive-notes.jsonl)
            try:
                notes_data = read_notes_file(worktree_path)
                if notes_data:
                    for note in notes_data:
                        self.db.add_note(
                            issue_id=issue_id,
                            agent_id=agent_id,
                            content=note.get("content", ""),
                            category=note.get("category", "discovery"),
                            project=self.project_name,
                        )
                    self.db.log_event(issue_id, agent_id, "notes_harvested", {"count": len(notes_data), "source": "refinery"})
            except Exception:
                pass  # Best-effort
            finally:
                remove_notes_file(worktree_path)

            # Check if refinery session should be cycled due to token usage
            await self._maybe_cycle_refinery_session()

        except Exception as e:
            self.db.update_merge_queue_status(queue_id, "failed")
            self.db.log_event(
                issue_id,
                agent_id,
                "refinery_error",
                {"error": str(e)},
            )
            # Force reset refinery session so next merge gets a fresh session
            await self._force_reset_refinery_session(f"Exception in _send_to_refinery: {str(e)}")

    async def _wait_for_refinery(self, session_id: str, worktree_path: str, timeout: int = None, min_message_count: int = 0) -> Dict[str, Any]:
        """
        Wait for the refinery session to become idle, then read result from file.

        Args:
            session_id: OpenCode session ID
            worktree_path: Path to the worktree (where .hive-result.jsonl is written)
            timeout: Timeout in seconds (defaults to LEASE_DURATION)
            min_message_count: Minimum expected message count to avoid stale-result race

        Returns:
            Parsed merge result dict
        """
        if timeout is None:
            timeout = Config.LEASE_DURATION

        poll_interval = 5
        elapsed = 0
        consecutive_errors = 0

        while elapsed < timeout:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            try:
                status = await self.opencode.get_session_status(session_id, directory=self.project_path)
                if status and status.get("type") == "idle":
                    # Session finished — verify new messages were produced (fence against stale results)
                    messages = await self.opencode.get_messages(session_id, directory=self.project_path)

                    if len(messages) <= min_message_count:
                        # No new messages, the prompt wasn't processed - continue waiting
                        continue

                    # Read result from file
                    file_result = read_result_file(worktree_path)
                    remove_result_file(worktree_path)

                    if file_result:
                        return {
                            "status": file_result.get("status", "needs_human"),
                            "summary": file_result.get("summary", ""),
                            "tests_passed": file_result.get("tests_passed", False),
                            "conflicts_resolved": int(file_result.get("conflicts_resolved", 0)),
                        }

                    # No result file — refinery didn't write one
                    return {
                        "status": "needs_human",
                        "summary": "Refinery did not write result file (.hive-result.jsonl)",
                        "tests_passed": False,
                        "conflicts_resolved": 0,
                    }

                # Reset consecutive errors on successful status check
                consecutive_errors = 0

            except Exception as e:
                consecutive_errors += 1
                if consecutive_errors >= 5:
                    # Too many consecutive errors, bail early with needs_human
                    return {
                        "status": "needs_human",
                        "summary": f"Refinery failed after {consecutive_errors} consecutive errors: {str(e)}",
                        "tests_passed": False,
                        "conflicts_resolved": 0,
                    }
                # Otherwise continue polling

        # Timeout
        return {
            "status": "needs_human",
            "summary": f"Refinery timed out after {timeout}s",
            "tests_passed": False,
            "conflicts_resolved": 0,
        }

    async def _finalize_issue(self, entry: Dict[str, Any]):
        """
        Mark an issue as finalized and clean up.

        Args:
            entry: Merge queue entry dict
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Update merge queue
        self.db.update_merge_queue_status(entry["id"], "merged", completed_at=now)

        # Finalize the issue
        self.db.update_issue_status(entry["issue_id"], "finalized")
        self.db.log_event(
            entry["issue_id"],
            entry.get("agent_id"),
            "finalized",
            {"merged_at": now},
        )

        # Check if this issue has a parent (part of a epic)
        issue = self.db.get_issue(entry["issue_id"])
        if issue and issue.get("parent_id"):
            parent_id = issue["parent_id"]
            # Check if all children of the parent are now complete
            if self.db.check_epic_complete(parent_id):
                # Mark parent epic as finalized (all steps merged, nothing left to do)
                self.db.update_issue_status(parent_id, "finalized")
                self.db.log_event(
                    parent_id,
                    None,
                    "epic_complete",
                    {"completed_at": now},
                )

        # Tear down worktree, session, and agent
        await self._teardown_after_finalize(entry)

    async def _teardown_after_finalize(self, entry: Dict[str, Any]):
        """
        Clean up worktree, session, and agent state after finalization.

        Args:
            entry: Merge queue entry dict
        """
        # Clean up the opencode session if one exists for the agent
        agent_id = entry.get("agent_id")
        if agent_id:
            agent = self.db.get_agent(agent_id)
            session_id = agent.get("session_id") if agent else None
            if session_id:
                await self.opencode.cleanup_session(session_id, directory=entry.get("worktree"))

        # Remove worktree (in executor to avoid blocking event loop)
        if entry.get("worktree"):
            try:
                await remove_worktree_async(entry["worktree"])
            except GitWorktreeError:
                pass  # Best-effort cleanup

        # Delete branch (in executor to avoid blocking event loop)
        if entry.get("branch_name"):
            try:
                await delete_branch_async(self.project_path, entry["branch_name"], force=True)
            except (GitWorktreeError, FileNotFoundError, OSError):
                pass  # Best-effort cleanup

        # Delete ephemeral agent (events/notes/merge_queue retain agent_id as correlation key)
        if agent_id:
            self.db.conn.execute("PRAGMA foreign_keys = OFF")
            self.db.conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
            self.db.conn.execute("PRAGMA foreign_keys = ON")
            self.db.conn.commit()

    async def _maybe_cycle_refinery_session(self):
        """
        Check if the refinery session exceeds token threshold and cycle it if needed.

        If token usage exceeds Config.REFINERY_TOKEN_THRESHOLD, the session is
        aborted, deleted, and reset to None (next merge will create a fresh one).
        """
        if not self.refinery_session_id:
            return

        # Check local counters instead of fetching messages from API
        should_cycle = False
        if self._refinery_token_estimate > Config.REFINERY_TOKEN_THRESHOLD:  # 100,000
            should_cycle = True
        elif self._refinery_message_count > 20:
            should_cycle = True

        if should_cycle:
            # Log the cycling event
            self.db.log_event(
                None,  # No specific issue
                None,  # No specific agent
                "refinery_session_cycled",
                {
                    "session_id": self.refinery_session_id,
                    "token_count": self._refinery_token_estimate,
                    "message_count": self._refinery_message_count,
                    "threshold": Config.REFINERY_TOKEN_THRESHOLD,
                },
            )

            # Abort and delete the current session
            await self.opencode.cleanup_session(self.refinery_session_id, directory=self.project_path)

            # Reset session ID and counters - next merge will create a fresh session
            self.refinery_session_id = None
            self._refinery_message_count = 0
            self._refinery_token_estimate = 0

    async def _ensure_refinery_session(self) -> str:
        """
        Ensure a refinery session exists. Create one if needed.

        Returns:
            OpenCode session ID for the refinery
        """
        # Check if existing session is still alive
        if self.refinery_session_id:
            try:
                status = await self.opencode.get_session_status(self.refinery_session_id, directory=self.project_path)
                if status is not None:
                    return self.refinery_session_id
            except Exception:
                pass
            self.refinery_session_id = None

        # Create new refinery session
        session = await self.opencode.create_session(
            directory=self.project_path,
            title="refinery",
            permissions=WORKER_PERMISSIONS,
        )
        self.refinery_session_id = session["id"]

        # Reset counters for new session
        self._refinery_message_count = 0
        self._refinery_token_estimate = 0

        return self.refinery_session_id
