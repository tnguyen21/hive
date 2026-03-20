"""Merge queue processor for Hive orchestrator.

Processes the done→finalized pipeline:
  1. Refinery LLM: rebase, test, integration review
  2. Orchestrator ff-merge + finalize on refinery approval
"""

import asyncio
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Config, WORKER_PERMISSIONS
from .db import Database
from .git import (
    GitWorktreeError,
    delete_branch_async,
    get_worktree_dirty_status_async,
    merge_to_main_async,
    remove_worktree_async,
)
from .backends import HiveBackend
from .backends.pool import BackendPool
from .prompts import (
    build_refinery_prompt,
    build_refinery_system_prompt,
    read_notes_file,
    read_result_file,
    remove_notes_file,
    remove_result_file,
)

import logging

logger = logging.getLogger(__name__)


class RefinerySessionDied(Exception):
    """Raised when the refinery session is detected as dead (error/not_found) during polling."""

    pass


class MergeProcessor:
    """Processes the merge queue: rebase, test, merge, finalize."""

    def __init__(self, db: Database, backend: HiveBackend, project_path: str, project_name: str):
        self.db = db
        self.backend = backend
        self.project_path = str(Path(project_path).resolve())
        self.project_name = project_name
        self.refinery_session_id: str | None = None
        self._refinery_system_prompt: str | None = None
        self._refinery_message_count: int = 0
        self._refinery_token_estimate: int = 0
        self._main_dirty_blocked: bool = False
        self._main_dirty_snapshot: str | None = None

    async def shutdown(self):
        """Clean up the refinery session on shutdown."""
        if self.refinery_session_id:
            await self.backend.cleanup_session(self.refinery_session_id, directory=self.project_path)
            self.refinery_session_id = None

    async def _force_reset_refinery_session(self, reason: str):
        """Force reset the refinery session after a failure."""
        if not self.refinery_session_id:
            return

        session_id = self.refinery_session_id
        self.refinery_session_id = None  # Clear immediately to prevent reuse

        # Reset counters
        self._refinery_message_count = 0
        self._refinery_token_estimate = 0

        # Best-effort abort and delete
        await self.backend.cleanup_session(session_id, directory=self.project_path)

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
        with suppress(Exception):
            cursor = self.db.conn.execute("SELECT COUNT(*) FROM merge_queue WHERE status = 'running'")
            stuck_count = cursor.fetchone()[0]
            if stuck_count > 0:
                with self.db.transaction() as conn:
                    conn.execute("UPDATE merge_queue SET status = 'queued' WHERE status = 'running'")
                    self.db.log_system_event("stuck_merges_reset", {"count": stuck_count}, commit=False)

        with suppress(Exception):
            # Pre-create refinery session so it's warm when first merge arrives
            await self._ensure_refinery_session()

    async def health_check(self) -> bool:
        """Check if the refinery session is alive; recreate if needed. Returns False if recreation fails."""
        if not self.refinery_session_id:
            # No session exists, try to create one
            try:
                await self._ensure_refinery_session()
                return True
            except Exception:
                return False

        try:
            # Check if existing session is still alive
            status = await self.backend.get_session_status(self.refinery_session_id, directory=self.project_path)
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
        entries = self.db.list_merge_entries(self.project_name, status="queued", limit=1, ascending=True)
        if not entries:
            return

        # The final ff-merge runs in the project root worktree. If that worktree
        # is dirty, the merge will fail after a full refinery cycle. Pause the
        # queue until clean instead of wasting a refinery call.
        try:
            main_dirty, dirty_output = await get_worktree_dirty_status_async(self.project_path)
        except GitWorktreeError as e:
            self.db.log_system_event(
                "merge_preflight_error",
                {"path": self.project_path, "error": str(e)},
            )
            return

        if main_dirty:
            snapshot = "\n".join(dirty_output.splitlines()[:20])
            if (not self._main_dirty_blocked) or (snapshot != self._main_dirty_snapshot):
                self.db.log_system_event(
                    "merge_paused_dirty_main",
                    {"path": self.project_path, "project": self.project_name, "changes": snapshot},
                )
            self._main_dirty_blocked = True
            self._main_dirty_snapshot = snapshot
            return

        if self._main_dirty_blocked:
            self.db.log_system_event(
                "merge_resumed_main_clean",
                {"path": self.project_path, "project": self.project_name},
            )
            self._main_dirty_blocked = False
            self._main_dirty_snapshot = None

        entry = entries[0]
        queue_id = entry["id"]

        # Mark as running (CAS) — only one processor should claim a queued entry.
        claimed = self.db.try_transition_merge_queue_status(queue_id, from_status="queued", to_status="running")
        if not claimed:
            return
        self.db.log_event(
            entry["issue_id"],
            entry.get("agent_id"),
            "merge_started",
            {"queue_id": queue_id, "branch": entry["branch_name"]},
        )

        try:
            # Single path: refinery review/integration
            await self._send_to_refinery(entry)

        except Exception as e:
            # Unexpected error — mark queue entry failed, leave issue as-is
            self.db.try_transition_merge_queue_status(queue_id, from_status="running", to_status="failed")
            self.db.log_event(
                entry["issue_id"],
                entry.get("agent_id"),
                "merge_error",
                {"error": str(e), "queue_id": queue_id},
            )
            await self._cleanup_merge_resources(entry)

    async def _send_to_refinery(self, entry: dict[str, Any]):
        """Hand a merge to the Refinery LLM. Retries once on RefinerySessionDied; a second death escalates to needs_human."""
        queue_id = entry["id"]
        issue_id = entry["issue_id"]
        agent_id = entry.get("agent_id")

        self.db.log_event(
            issue_id,
            agent_id,
            "refinery_review_started",
            {
                "queue_id": queue_id,
                "branch": entry["branch_name"],
            },
        )

        worktree_path = entry["worktree"]

        try:
            res = await self._send_to_refinery_inner(entry, worktree_path)
        except RefinerySessionDied as e:
            # First death — log, reset session, and retry once
            self.db.log_event(
                issue_id,
                agent_id,
                "refinery_session_died",
                {"error": str(e), "queue_id": queue_id, "retry": True},
            )
            await self._force_reset_refinery_session(f"Session died: {e}")

            try:
                res = await self._send_to_refinery_inner(entry, worktree_path)
            except RefinerySessionDied as e2:
                # Second death — give up
                self.db.log_event(
                    issue_id,
                    agent_id,
                    "refinery_session_died",
                    {"error": str(e2), "queue_id": queue_id, "retry": False},
                )
                await self._force_reset_refinery_session(f"Session died twice: {e2}")
                res = {
                    "status": "needs_human",
                    "summary": f"Refinery session died twice: {e2}",
                    "tests_passed": False,
                    "conflicts_resolved": 0,
                }
            except Exception as e2:
                self.db.try_transition_merge_queue_status(queue_id, from_status="running", to_status="failed")
                self.db.log_event(issue_id, agent_id, "refinery_error", {"error": str(e2)})
                await self._force_reset_refinery_session(f"Exception in retry: {e2}")
                await self._cleanup_merge_resources(entry)
                return
        except Exception as e:
            self.db.try_transition_merge_queue_status(queue_id, from_status="running", to_status="failed")
            self.db.log_event(issue_id, agent_id, "refinery_error", {"error": str(e)})
            await self._force_reset_refinery_session(f"Exception in _send_to_refinery: {e}")
            await self._cleanup_merge_resources(entry)
            return

        # Process result
        needs_cleanup = False
        match res.get("status"):
            case "merged":
                branch_name = entry["branch_name"]
                try:
                    await merge_to_main_async(self.project_path, branch_name)
                except GitWorktreeError as e:
                    self.db.try_transition_merge_queue_status(queue_id, from_status="running", to_status="failed")
                    self.db.log_event(
                        issue_id,
                        agent_id,
                        "merge_failed",
                        {"error": str(e), "branch": branch_name, "after_refinery": True},
                    )
                    needs_cleanup = True

                if not needs_cleanup:
                    await self._finalize_issue(entry)
                    self.db.log_event(
                        issue_id,
                        agent_id,
                        "refinery_review_passed",
                        {"conflicts_resolved": res.get("conflicts_resolved", 0)},
                    )

            case "rejected":
                self.db.try_transition_merge_queue_status(queue_id, from_status="running", to_status="failed")
                self.db.try_transition_issue_status(issue_id, from_status="done", to_status="open")
                self.db.log_event(issue_id, agent_id, "refinery_review_rejected", {"summary": res.get("summary", "")})

                rejection_reason = res.get("summary", "Unknown reason")
                note_content = f"[Refinery rejection] {rejection_reason}\nBranch: {entry['branch_name']}"
                self.db.add_note(
                    issue_id=issue_id,
                    agent_id=agent_id,
                    category="rejection",
                    content=note_content,
                    project=self.project_name,
                )
                needs_cleanup = True

            case _:  # needs_human or unknown
                self.db.try_transition_merge_queue_status(queue_id, from_status="running", to_status="failed")
                self.db.try_transition_issue_status(issue_id, from_status="done", to_status="escalated")
                self.db.log_event(issue_id, agent_id, "refinery_review_escalated", {"summary": res.get("summary", "")})
                needs_cleanup = True

        # Harvest notes from the worktree (refinery may have written .hive-notes.jsonl)
        try:
            with suppress(Exception):
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
        finally:
            remove_notes_file(worktree_path)

        # Clean up orphaned resources on non-success paths
        if needs_cleanup:
            await self._cleanup_merge_resources(entry)

        # Check if refinery session should be cycled due to token usage
        await self._maybe_cycle_refinery_session()

    async def _send_to_refinery_inner(self, entry: dict[str, Any], worktree_path: str) -> dict[str, Any]:
        """Send a merge to the refinery and wait for a parsed result. Raises RefinerySessionDied if session dies."""
        session_id = await self._ensure_refinery_session()

        # Record message count before sending (fence against stale results)
        pre_send_count = self._refinery_message_count

        # Remove any stale result file before sending (belt-and-suspenders)
        remove_result_file(worktree_path)

        # Build the refinery prompt
        # Prefer worker test_command over global Config.TEST_COMMAND
        test_cmd = entry.get("test_command") or Config.TEST_COMMAND

        # Gather fresh project notes for this merge
        notes = self.db.get_notes(project=self.project_name, limit=10)

        prompt = build_refinery_prompt(
            issue_title=entry.get("issue_title", "Unknown"),
            issue_id=entry["issue_id"],
            branch_name=entry["branch_name"],
            worktree_path=worktree_path,
            agent_name=entry.get("agent_name"),
            test_command=test_cmd,
            notes=notes if notes else None,
        )

        # Send to refinery (system prompt only takes effect on first message of a new session)
        await self.backend.send_message_async(
            session_id,
            parts=[{"type": "text", "text": prompt}],
            model=Config.REFINERY_MODEL,
            system=self._refinery_system_prompt,
            directory=self.project_path,
        )

        # Brief delay to check if message was picked up
        await asyncio.sleep(0.5)
        status = await self.backend.get_session_status(session_id, directory=self.project_path)
        if status and status.get("type") == "idle":
            raise RuntimeError("Refinery session did not pick up the message")

        # Wait for refinery to finish (poll session status)
        res = await self._wait_for_refinery(session_id, worktree_path=worktree_path, min_message_count=pre_send_count)

        # Increment counters after successful refinery processing
        self._refinery_message_count += 2  # one for the prompt sent, one for the response
        self._refinery_token_estimate += len(prompt) // 4  # rough estimate for input

        return res

    async def _wait_for_refinery(
        self, session_id: str, worktree_path: str, timeout: int | None = None, min_message_count: int = 0
    ) -> dict[str, Any]:
        """Wait for the refinery session to become idle, then read the result from the worktree file."""
        timeout_seconds = Config.LEASE_DURATION if timeout is None else timeout

        poll_interval = 5
        consecutive_errors = 0

        try:
            async with asyncio.timeout(timeout_seconds):
                while True:
                    await asyncio.sleep(poll_interval)

                    try:
                        status = await self.backend.get_session_status(session_id, directory=self.project_path)

                        # Detect dead session (backend returned error or not_found)
                        if status and status.get("type") in ("error", "not_found"):
                            raise RefinerySessionDied(f"Refinery session returned {status.get('type')}")

                        if status and status.get("type") == "idle":
                            # Session finished — verify new messages were produced (fence against stale results)
                            msgs = await self.backend.get_messages(session_id, directory=self.project_path)

                            if len(msgs) <= min_message_count:
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

                    except RefinerySessionDied:
                        raise  # Propagate dead-session signal to caller for retry
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
        except TimeoutError:
            return {
                "status": "needs_human",
                "summary": f"Refinery timed out after {timeout_seconds}s",
                "tests_passed": False,
                "conflicts_resolved": 0,
            }

    async def _finalize_issue(self, entry: dict[str, Any]):
        """Mark an issue as finalized and clean up merge resources."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Update merge queue
        self.db.try_transition_merge_queue_status(
            entry["id"],
            from_status="running",
            to_status="merged",
            completed_at=now,
        )

        # Finalize the issue
        self.db.try_transition_issue_status(entry["issue_id"], from_status="done", to_status="finalized")
        self.db.log_event(
            entry["issue_id"],
            entry.get("agent_id"),
            "finalized",
            {"merged_at": now},
        )

        # Tear down worktree, session, and agent
        await self._teardown_after_finalize(entry)

    async def _cleanup_merge_resources(self, entry: dict[str, Any]):
        """Best-effort cleanup of worktree, branch, session, and agent row after finalization or failure."""

        # Clean up the backend session if one exists for the agent
        agent_id = entry.get("agent_id")
        if agent_id:
            agent = self.db.get_agent(agent_id)
            session_id = agent.get("session_id") if agent else None
            if session_id:
                with suppress(Exception):
                    await self.backend.cleanup_session(session_id, directory=entry.get("worktree"))

        # Remove worktree (in executor to avoid blocking event loop)
        if entry.get("worktree"):
            with suppress(GitWorktreeError, FileNotFoundError, OSError):
                await remove_worktree_async(entry["worktree"])

        # Delete branch (in executor to avoid blocking event loop)
        if entry.get("branch_name"):
            with suppress(GitWorktreeError, FileNotFoundError, OSError):
                await delete_branch_async(self.project_path, entry["branch_name"], force=True)

        # Delete ephemeral agent (events/notes/merge_queue retain agent_id as correlation key)
        if agent_id:
            with self.db.transaction() as conn:
                conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))

    async def _teardown_after_finalize(self, entry: dict[str, Any]):
        """Clean up worktree, session, and agent state after finalization."""
        await self._cleanup_merge_resources(entry)

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
            await self.backend.cleanup_session(self.refinery_session_id, directory=self.project_path)

            # Reset session ID and counters - next merge will create a fresh session
            self.refinery_session_id = None
            self._refinery_message_count = 0
            self._refinery_token_estimate = 0

    async def _ensure_refinery_session(self) -> str:
        """Ensure a refinery session exists, creating one if needed. Returns the session ID."""
        # Check if existing session is still alive
        if self.refinery_session_id:
            with suppress(Exception):
                status = await self.backend.get_session_status(self.refinery_session_id, directory=self.project_path)
                if status is not None:
                    return self.refinery_session_id
            self.refinery_session_id = None

        # Create new refinery session
        session = await self.backend.create_session(
            directory=self.project_path,
            title="refinery",
            permissions=WORKER_PERMISSIONS,
        )
        session_id: str = session["id"]
        self.refinery_session_id = session_id

        # Build system prompt with project conventions (CLAUDE.md)
        self._refinery_system_prompt = build_refinery_system_prompt(self.project_path)

        # Reset counters for new session
        self._refinery_message_count = 0
        self._refinery_token_estimate = 0

        return session_id


class MergeProcessorPool:
    """Pool of MergeProcessor instances keyed by project name.

    Each project gets its own MergeProcessor so merges in different projects
    are independent and do not block each other.
    """

    def __init__(self, db: Database, backend: HiveBackend | None = None, backend_pool: BackendPool | None = None):
        self._processors: dict[str, MergeProcessor] = {}
        self.db = db
        self._backend_pool = backend_pool
        self._fallback_backend = backend

    def _resolve_backend(self, project_name: str, project_path: str) -> HiveBackend:
        """Resolve the backend for a project."""
        if self._backend_pool is not None:
            return self._backend_pool.for_project(project_name, Path(project_path))
        if self._fallback_backend is not None:
            return self._fallback_backend
        raise ValueError("MergeProcessorPool has no backend configured")

    def get(self, project_name: str, project_path: str) -> MergeProcessor:
        """Return the MergeProcessor for the given project, creating it lazily."""
        if project_name not in self._processors:
            backend = self._resolve_backend(project_name, project_path)
            self._processors[project_name] = MergeProcessor(
                db=self.db,
                backend=backend,
                project_path=project_path,
                project_name=project_name,
            )
        return self._processors[project_name]

    async def process_all(self):
        """Process the merge queue once for every known project."""
        for processor in list(self._processors.values()):
            await processor.process_queue_once()

    async def health_check_all(self):
        """Run health checks on every known processor."""
        for processor in list(self._processors.values()):
            await processor.health_check()
