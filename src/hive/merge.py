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
    abort_rebase,
    delete_branch,
    merge_to_main,
    rebase_onto_main,
    remove_worktree,
    run_command_in_worktree,
)
from .opencode import OpenCodeClient, make_model_config
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
        entries = self.db.get_queued_merges(limit=1)
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

        # Step 1: Rebase onto main
        rebase_ok = rebase_onto_main(worktree)
        if not rebase_ok:
            self.db.log_event(issue_id, agent_id, "rebase_conflict", {"branch": branch_name})
            abort_rebase(worktree)
            return (False, None)

        self.db.log_event(issue_id, agent_id, "rebase_success", {"branch": branch_name})

        # Step 2: Run tests (if configured)
        test_output = None
        if Config.TEST_COMMAND:
            test_ok, test_output = run_command_in_worktree(worktree, Config.TEST_COMMAND)
            if not test_ok:
                self.db.log_event(
                    issue_id,
                    agent_id,
                    "test_failure",
                    {"command": Config.TEST_COMMAND, "output": test_output[:2000]},
                )
                return (False, test_output)

            self.db.log_event(issue_id, agent_id, "tests_passed", {"command": Config.TEST_COMMAND})

        # Step 3: Merge to main (ff-only)
        try:
            merge_to_main(self.project_path, branch_name)
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
            prompt = build_refinery_prompt(
                issue_title=entry.get("issue_title", "Unknown"),
                issue_id=issue_id,
                branch_name=entry["branch_name"],
                worktree_path=worktree_path,
                agent_name=entry.get("agent_name"),
                rebase_succeeded=rebase_ok,
                test_output=test_output,
                test_command=Config.TEST_COMMAND,
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

        # Check if this issue has a parent (part of a molecule)
        issue = self.db.get_issue(entry["issue_id"])
        if issue and issue.get("parent_id"):
            parent_id = issue["parent_id"]
            # Check if all children of the parent are now complete
            if self.db.check_molecule_complete(parent_id):
                # Mark parent molecule as done
                self.db.update_issue_status(parent_id, "done")
                self.db.log_event(
                    parent_id,
                    None,
                    "molecule_complete",
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

        # Remove worktree
        if entry.get("worktree"):
            try:
                remove_worktree(entry["worktree"])
            except GitWorktreeError:
                pass  # Best-effort cleanup

        # Delete branch
        if entry.get("branch_name"):
            try:
                delete_branch(self.project_path, entry["branch_name"], force=True)
            except (GitWorktreeError, FileNotFoundError, OSError):
                pass  # Best-effort cleanup

        # Mark agent idle
        if agent_id:
            self.db.conn.execute(
                """
                UPDATE agents
                SET status = 'idle',
                    session_id = NULL,
                    worktree = NULL,
                    current_issue = NULL,
                    updated_at = datetime('now')
                WHERE id = ?
                """,
                (agent_id,),
            )
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
