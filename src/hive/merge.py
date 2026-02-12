"""Merge queue processor for Hive orchestrator.

Processes the done→finalized pipeline:
  1. Mechanical fast-path: rebase, test, ff-merge (no LLM)
  2. Refinery LLM: conflict resolution, test failure diagnosis
"""

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from .config import Config
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
from .prompts import build_refinery_prompt, parse_merge_result


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
        self.running = False

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
                self._finalize_issue(entry)
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
            self.db.log_event(
                issue_id, agent_id, "rebase_conflict", {"branch": branch_name}
            )
            abort_rebase(worktree)
            return (False, None)

        self.db.log_event(issue_id, agent_id, "rebase_success", {"branch": branch_name})

        # Step 2: Run tests (if configured)
        test_output = None
        if Config.TEST_COMMAND:
            test_ok, test_output = run_command_in_worktree(
                worktree, Config.TEST_COMMAND
            )
            if not test_ok:
                self.db.log_event(
                    issue_id,
                    agent_id,
                    "test_failure",
                    {"command": Config.TEST_COMMAND, "output": test_output[:2000]},
                )
                return (False, test_output)

            self.db.log_event(
                issue_id, agent_id, "tests_passed", {"command": Config.TEST_COMMAND}
            )

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

    async def _send_to_refinery(
        self, entry: Dict[str, Any], test_output: Optional[str] = None
    ):
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

        try:
            session_id = await self._ensure_refinery_session()

            # Build the refinery prompt
            prompt = build_refinery_prompt(
                issue_title=entry.get("issue_title", "Unknown"),
                issue_id=issue_id,
                branch_name=entry["branch_name"],
                worktree_path=entry["worktree"],
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

            # Wait for refinery to finish (poll session status)
            result = await self._wait_for_refinery(session_id)

            # Process result
            if result["status"] == "merged":
                self._finalize_issue(entry)
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

        except Exception as e:
            self.db.update_merge_queue_status(queue_id, "failed")
            self.db.log_event(
                issue_id,
                agent_id,
                "refinery_error",
                {"error": str(e)},
            )

    async def _wait_for_refinery(
        self, session_id: str, timeout: int = None
    ) -> Dict[str, Any]:
        """
        Wait for the refinery session to become idle, then parse the result.

        Args:
            session_id: OpenCode session ID
            timeout: Timeout in seconds (defaults to LEASE_DURATION)

        Returns:
            Parsed merge result dict
        """
        if timeout is None:
            timeout = Config.LEASE_DURATION

        poll_interval = 5
        elapsed = 0

        while elapsed < timeout:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            try:
                status = await self.opencode.get_session_status(
                    session_id, directory=self.project_path
                )
                if status and status.get("type") == "idle":
                    # Session finished — get messages and parse result
                    messages = await self.opencode.get_messages(
                        session_id, directory=self.project_path
                    )
                    return parse_merge_result(messages)
            except Exception:
                continue

        # Timeout
        return {
            "status": "needs_human",
            "summary": f"Refinery timed out after {timeout}s",
            "tests_passed": False,
            "conflicts_resolved": 0,
        }

    def _finalize_issue(self, entry: Dict[str, Any]):
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

        # Tear down worktree and agent
        self._teardown_after_finalize(entry)

    def _teardown_after_finalize(self, entry: Dict[str, Any]):
        """
        Clean up worktree and agent state after finalization.

        Args:
            entry: Merge queue entry dict
        """
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
        agent_id = entry.get("agent_id")
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

    async def _ensure_refinery_session(self) -> str:
        """
        Ensure a refinery session exists. Create one if needed.

        Returns:
            OpenCode session ID for the refinery
        """
        # Check if existing session is still alive
        if self.refinery_session_id:
            try:
                status = await self.opencode.get_session_status(
                    self.refinery_session_id, directory=self.project_path
                )
                if status is not None:
                    return self.refinery_session_id
            except Exception:
                pass
            self.refinery_session_id = None

        # Create new refinery session
        session = await self.opencode.create_session(
            directory=self.project_path,
            title="refinery",
            permissions=[
                {"permission": "*", "pattern": "*", "action": "allow"},
                {"permission": "question", "pattern": "*", "action": "deny"},
                {"permission": "plan_enter", "pattern": "*", "action": "deny"},
                {"permission": "external_directory", "pattern": "*", "action": "deny"},
            ],
        )
        self.refinery_session_id = session["id"]
        return self.refinery_session_id
