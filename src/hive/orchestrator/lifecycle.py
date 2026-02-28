"""Lifecycle and spawn/monitor mixin for the Hive orchestrator."""

import asyncio
import logging
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from ..config import WORKER_PERMISSIONS
from ..prompts import build_retry_context, build_system_prompt, build_worker_prompt, get_prompt_version, render_inbox_section
from ..utils import AgentIdentity, CompletionResult, generate_id
from .completion import _exc_detail

logger = logging.getLogger(__name__)


class StalledTransition(str, Enum):
    """Transition outcomes for stalled-agent handling."""

    FAIL_STALLED_IN_PROGRESS = "fail_stalled_in_progress"
    FAIL_STALLED_TERMINAL = "fail_stalled_terminal"


class StalledSessionCheckResult(str, Enum):
    """Outcome for lease-expiry session verification."""

    CONTINUE_MONITORING = "continue_monitoring"
    STOP_MONITORING = "stop_monitoring"


class LifecycleMixin:
    """Mixin providing worker spawn, monitor, and stall handling."""

    async def spawn_worker(self, issue: Dict[str, str]):
        """
        Spawn a worker to handle an issue.
        Always creates a new agent.

        Args:
            issue: Issue dict from database
        """
        issue_id = issue["id"]
        self._spawning_issues.add(issue_id)
        try:
            await self._spawn_worker_inner(issue)
        finally:
            self._spawning_issues.discard(issue_id)

    async def _spawn_worker_inner(self, issue: Dict[str, str]):
        """Inner spawn logic, wrapped by spawn_worker's TOCTOU guard."""
        import hive.orchestrator as _mod

        Config = _mod.Config
        create_worktree_async = _mod.create_worktree_async
        remove_worktree_async = _mod.remove_worktree_async

        issue_id = issue["id"]
        issue_project = issue["project"]
        agent_name = generate_id("worker")
        model = issue.get("model") or Config.WORKER_MODEL or Config.DEFAULT_MODEL

        # Resolve project path from the DB — raises ValueError for unknown projects
        project_path = self._resolve_project_path(issue_project)

        # Ensure the merge pool has a processor for this project (lazy registration)
        self.merge_pool.get(issue_project, str(project_path))

        # Create agent identity in database
        agent_id = self.db.create_agent(
            name=agent_name,
            model=model,
            metadata={"issue_id": issue_id},
            project=issue_project,
        )

        # Create git worktree (in executor to avoid blocking event loop)
        try:
            worktree_path = await create_worktree_async(str(project_path), agent_name)
        except Exception as e:
            self.db.log_event(
                issue_id,
                agent_id,
                "worktree_error",
                {"error": _exc_detail(e)},
            )
            self._delete_agent_row(agent_id)
            return

        # Atomic claim
        claimed = self.db.claim_issue(issue_id, agent_id)
        if not claimed:
            # Someone else claimed it first, clean up worktree and delete agent
            await remove_worktree_async(worktree_path)
            self._delete_agent_row(agent_id)
            return

        # Create backend session
        session_id = None  # Track for cleanup on failure
        try:
            session = await self.backend.create_session(
                directory=worktree_path,
                title=f"{agent_name}: {issue['title']}",
                permissions=WORKER_PERMISSIONS,
            )
            session_id = session["id"]

            # Update agent with session info
            self.db.conn.execute(
                """
                UPDATE agents
                SET session_id = ?,
                    worktree = ?,
                    last_heartbeat_at = datetime('now'),
                    last_progress_at = datetime('now')
                WHERE id = ?
                """,
                (session_id, worktree_path, agent_id),
            )
            self.db.conn.commit()

            # Create agent identity
            agent = AgentIdentity(
                agent_id=agent_id,
                name=agent_name,
                issue_id=issue_id,
                worktree=worktree_path,
                session_id=session_id,
                project=issue_project,
            )
            self._register_active_agent(agent)

            await self._dispatch_worker_to_issue(
                agent=agent,
                issue=issue,
                model=model,
                started_event_type="worker_started",
                started_event_detail={
                    "session_id": session_id,
                    "worktree": worktree_path,
                    "routing_method": "new_agent",
                    "prompt_version": get_prompt_version("worker"),
                    "model": model,
                },
            )

        except Exception as e:
            self.db.log_event(
                issue_id,
                agent_id,
                "spawn_error",
                {"error": _exc_detail(e)},
            )
            # Clean up the backend session if it was created (best-effort —
            # don't let cleanup failure prevent DB/worktree cleanup below)
            if session_id:
                await self._best_effort_cleanup(
                    "spawn_session_cleanup",
                    self.backend.cleanup_session(session_id, directory=worktree_path),
                )
            # Clean up in-memory tracking (if agent was registered)
            if agent_id in self.active_agents:
                self._unregister_agent(agent_id)
            # Mark agent as failed in DB
            self._mark_agent_failed(agent_id)
            # Clean up worktree and escalate issue
            await remove_worktree_async(worktree_path)
            self.db.try_transition_issue_status(
                issue_id,
                from_status="in_progress",
                to_status="escalated",
                expected_assignee=agent_id,
            )
            self.db.log_event(issue_id, agent_id, "escalated", {"reason": "Spawn failure"})

    def _gather_notes_for_worker(self, issue_id: str, project: str) -> Optional[List[Dict[str, Any]]]:
        """Gather relevant notes to inject into a worker's prompt.

        Returns recent project-wide notes, deduplicated by note ID.
        Returns None if no notes are found (so build_worker_prompt skips the section).
        """
        seen_ids: set = set()
        notes: List[Dict[str, Any]] = []

        # Get recent project-wide notes
        for note in self.db.get_notes(project=project, limit=10):
            if note["id"] not in seen_ids:
                seen_ids.add(note["id"])
                notes.append(note)

        return notes if notes else None

    def _prepare_inbox_for_worker(self, agent_id: str, issue_id: str, project: str) -> Optional[str]:
        """
        Materialize issue-following deliveries and build the inbox section for injection.

        Per spec section 6.2 and 7.1:
        1. Materializes issue-following targets for this (agent_id, issue_id).
        2. Queries injectable deliveries.
        3. Marks queued deliveries as delivered.
        4. Renders the canonical inbox section.
        5. Logs note_delivered event.

        Returns the rendered inbox section string, or None if no deliveries.
        """
        self.db.materialize_issue_deliveries(issue_id, agent_id, project)
        deliveries, has_more = self.db.get_injectable_deliveries(agent_id, issue_id, project)
        if not deliveries:
            return None
        for d in deliveries:
            if d.get("status") == "queued":
                self.db.mark_delivery_delivered(d["delivery_id"])
        inbox_section = render_inbox_section(deliveries, has_more)
        self.db.log_event(
            issue_id,
            agent_id,
            "note_delivered",
            {"count": len(deliveries), "delivery_ids": [d["delivery_id"] for d in deliveries]},
        )
        return inbox_section

    def _is_issue_canceled(self, issue_id: str) -> bool:
        """Check if an issue has been canceled in the database."""
        try:
            issue = self.db.get_issue(issue_id)
            return issue is not None and issue.get("status") == "canceled"
        except Exception:
            return False

    async def _poll_session_idle(
        self,
        session_id: str,
        worktree: str,
        *,
        agent_id: Optional[str] = None,
        issue_id: Optional[str] = None,
    ) -> bool:
        """Poll backend to check if a session has gone idle.

        Fallback for when SSE events are missed (e.g., reconnect gap).
        Returns True if the session is idle, False otherwise.
        """
        try:
            status = await self.backend.get_session_status(session_id, directory=worktree)
            status_type = status.get("type") if isinstance(status, dict) else None

            if status_type == "idle":
                logger.info(f"Session poll detected idle for session {session_id} (agent={agent_id}, issue={issue_id}, status={status})")
                return True

            if status_type in ("not_found", "error", None):
                logger.warning(
                    f"Session poll observed non-runnable status for session {session_id} (agent={agent_id}, issue={issue_id}, status={status})"
                )
            else:
                logger.debug(
                    f"Session poll observed active status for session {session_id} (agent={agent_id}, issue={issue_id}, status={status})"
                )
            return False
        except Exception as e:
            logger.warning(f"Session poll failed for session {session_id} (agent={agent_id}, issue={issue_id}): {e}")
            return False

    async def _dispatch_worker_to_issue(
        self,
        *,
        agent: AgentIdentity,
        issue: Dict[str, Any],
        model: str,
        started_event_type: str,
        started_event_detail: Dict[str, Any],
    ):
        """Shared prompt + dispatch flow for worker spawning."""
        issue_id = issue["id"]
        issue_project = issue["project"]

        worker_notes = self._gather_notes_for_worker(issue_id, issue_project)
        if worker_notes:
            self.db.log_event(issue_id, agent.agent_id, "notes_injected", {"count": len(worker_notes)})

        # NEW: delivery-based inbox injection
        inbox_section = self._prepare_inbox_for_worker(agent.agent_id, issue_id, issue_project)

        retry_context = build_retry_context(self.db, issue_id)
        branch_name = f"agent/{agent.name}"
        prompt = build_worker_prompt(
            agent_name=agent.name,
            issue=issue,
            worktree_path=agent.worktree,
            branch_name=branch_name,
            project=issue_project,
            notes=worker_notes,
            retry_context=retry_context,
            inbox_section=inbox_section,
        )

        system_prompt = build_system_prompt(
            project=issue_project,
            agent_name=agent.name,
            worktree_path=agent.worktree,
        )

        self.session_status_events[agent.session_id] = asyncio.Event()
        logger.debug(
            f"Created session status event for session {agent.session_id} "
            f"(agent={agent.agent_id}, issue={issue_id}, started_event={started_event_type})"
        )

        await self.backend.send_message_async(
            agent.session_id,
            parts=[{"type": "text", "text": prompt}],
            model=model,
            system=system_prompt,
            directory=agent.worktree,
        )
        logger.info(f"Dispatched worker prompt for session {agent.session_id} (agent={agent.agent_id}, issue={issue_id}, model={model})")

        self.db.log_event(issue_id, agent.agent_id, started_event_type, started_event_detail)
        monitor_task = asyncio.create_task(self.monitor_agent(agent))
        logger.debug(
            f"Started monitor task for session {agent.session_id} (agent={agent.agent_id}, issue={issue_id}, task_id={id(monitor_task)})"
        )

    async def monitor_agent(self, agent: AgentIdentity):
        """
        Monitor an agent until completion.

        Uses dual detection strategy:
        1. SSE events: `session.status` → `idle` sets an asyncio.Event immediately
        2. Polling fallback: every check_interval, polls `get_session_status` directly
           to catch idle transitions that were missed by SSE (reconnect gaps, etc.)

        After idle is detected, reads the result file for structured completion data.

        Also checks periodically if the issue was canceled, and if so,
        aborts the session immediately.

        Args:
            agent: Agent identity
        """
        import hive.orchestrator as _mod

        Config = _mod.Config
        read_result_file = _mod.read_result_file

        # Snapshot the session_id we're monitoring and always clean up that key.
        # This keeps monitor cleanup stable even if the agent object is mutated.
        my_session_id = agent.session_id
        try:
            event = self.session_status_events.get(my_session_id)
            if not event:
                logger.warning(
                    f"Monitor started without session event for session {my_session_id} (agent={agent.agent_id}, issue={agent.issue_id})"
                )
                return

            # Record initial activity
            self._session_last_activity[my_session_id] = datetime.now()

            # Poll loop: result file is the source of completion truth.
            # SSE/poll idle are only hints that completion may now be available.
            check_interval = min(30, Config.LEASE_DURATION // 4)
            completion_detected_via = "unknown"
            file_result: Optional[Dict[str, Any]] = None
            idle_hint_seen = False
            logger.info(
                f"Starting monitor for session {my_session_id} "
                f"(agent={agent.agent_id}, issue={agent.issue_id}, check_interval={check_interval}s)"
            )
            while True:
                # Completion truth: parsed result file.
                file_result = read_result_file(agent.worktree)
                if file_result is not None:
                    if completion_detected_via == "unknown":
                        completion_detected_via = "file"
                    break
                # If the session has gone idle (via SSE or polling) but the
                # worker didn't write a result file, treat that as completion
                # and let handle_agent_complete record the failure instead of
                # waiting out the full lease duration.
                if idle_hint_seen:
                    if completion_detected_via == "unknown":
                        completion_detected_via = "idle_hint_no_file"
                    break

                try:
                    await asyncio.wait_for(event.wait(), timeout=check_interval)
                    # Event was set — could be idle (hint) or canceled.
                    # Check if canceled before assessing completion
                    if self._is_issue_canceled(agent.issue_id):
                        # Issue was canceled while agent was working.
                        # cancel_agent_for_issue already handled cleanup + set the event.
                        logger.info(
                            f"Monitor exiting due to cancellation for session {my_session_id} (agent={agent.agent_id}, issue={agent.issue_id})"
                        )
                        return
                    completion_detected_via = "event_hint"
                    idle_hint_seen = True
                    event.clear()
                except asyncio.TimeoutError:
                    logger.debug(
                        f"Monitor timeout waiting for idle event for session {my_session_id}; "
                        f"polling fallback (event_set={event.is_set()}, current_agent_session={agent.session_id})"
                    )
                    # Check if the issue was canceled
                    if self._is_issue_canceled(agent.issue_id):
                        await self.cancel_agent_for_issue(agent.issue_id)
                        return

                    # Polling fallback: directly check if the session went idle.
                    # This catches cases where the SSE event was missed.
                    if await self._poll_session_idle(
                        my_session_id,
                        agent.worktree,
                        agent_id=agent.agent_id,
                        issue_id=agent.issue_id,
                    ):
                        completion_detected_via = "poll_hint"
                        idle_hint_seen = True

                    # Check if there's been recent activity
                    last_activity = self._session_last_activity.get(my_session_id, datetime.now())
                    elapsed = (datetime.now() - last_activity).total_seconds()

                    if elapsed > Config.LEASE_DURATION:
                        # Heartbeat appears stale. Re-check file/session once.
                        check_result = await self._handle_stalled_with_session_check(agent)
                        if check_result == StalledSessionCheckResult.CONTINUE_MONITORING:
                            # Status is still busy; keep monitor alive.
                            self._session_last_activity[my_session_id] = datetime.now()
                            continue
                        return

            logger.info(
                f"Completion result detected for session {my_session_id} "
                f"(agent={agent.agent_id}, issue={agent.issue_id}, detected_via={completion_detected_via})"
            )
            await self.handle_agent_complete(agent, file_result=file_result)

        except Exception as e:
            logger.exception(f"Monitor error for session {my_session_id} (agent={agent.agent_id}, issue={agent.issue_id}): {e}")
            self.db.log_event(
                agent.issue_id,
                agent.agent_id,
                "monitor_error",
                {"error": str(e)},
            )
            # Tear down the agent so it doesn't leak in active_agents
            await self._teardown_agent(agent)
        finally:
            # Clean up using the snapshotted session_id, not agent.session_id.
            logger.debug(
                f"Monitor cleanup for session {my_session_id} "
                f"(agent={agent.agent_id}, issue={agent.issue_id}, "
                f"event_present={my_session_id in self.session_status_events})"
            )
            if my_session_id in self.session_status_events:
                del self.session_status_events[my_session_id]
            self._session_last_activity.pop(my_session_id, None)

    async def cancel_agent_for_issue(self, issue_id: str):
        """Cancel the active agent working on an issue.

        Aborts the backend session, cleans up the agent and worktree.
        Called when an issue is canceled while an agent is working on it.

        Args:
            issue_id: The issue ID that was canceled
        """
        # Cancel transition table:
        # - CANCELLED_BY_USER -> wake monitor event, mark failed, log cancel, teardown+worktree cleanup

        # Find the agent working on this issue
        agent_id = self._issue_to_agent.get(issue_id)
        agent = self.active_agents.get(agent_id) if agent_id else None

        if not agent:
            return  # No active agent for this issue

        logger.info(f"Canceling agent {agent.name} (session {agent.session_id}) for issue {issue_id}")

        # Signal the monitor_agent loop to stop waiting
        event = self.session_status_events.get(agent.session_id)
        if event:
            event.set()

        self._mark_agent_failed(agent.agent_id)

        self.db.log_event(
            issue_id,
            agent.agent_id,
            "agent_canceled",
            {"reason": "issue canceled by user, session aborted"},
        )

        await self._teardown_agent(agent, remove_worktree=True)

    async def handle_stalled_agent(self, agent: AgentIdentity):
        """
        Handle a stalled agent (lease expired).

        Routes through the retry escalation chain so that repeatedly
        stalling issues eventually get escalated instead of looping
        forever.

        Args:
            agent: Agent identity
        """
        # Stalled transition table:
        # - FAIL_STALLED_IN_PROGRESS -> mark failed + escalate via _handle_agent_failure + teardown
        # - FAIL_STALLED_TERMINAL    -> mark failed + teardown (no escalation)
        if not self._try_claim_agent_for_handling(agent, handler_name="stall handling"):
            return

        stalled_transition = StalledTransition.FAIL_STALLED_TERMINAL
        try:
            self.db.log_event(
                agent.issue_id,
                agent.agent_id,
                "stalled",
                {"lease_expired": True},
            )

            # Route through escalation chain (retry → agent_switch → escalate)
            # instead of unconditionally resetting to open, which caused an
            # infinite spawn loop for issues whose workers always stall.
            current_issue = self.db.get_issue(agent.issue_id)
            if current_issue and current_issue.get("status") == "in_progress":
                stalled_transition = StalledTransition.FAIL_STALLED_IN_PROGRESS
                stall_result = CompletionResult(
                    success=False,
                    reason="Agent stalled (lease expired, no activity)",
                    summary="Worker became unresponsive",
                )
                await self._handle_agent_failure(agent, stall_result)
        finally:
            logger.debug(f"Stall transition for {agent.name}: {stalled_transition.value}")
            await self._teardown_agent(agent, remove_worktree=True)

    async def check_stalled_agents(self):
        """Check for stalled agents owned by THIS daemon and handle them.

        Only checks agents in self.active_agents (in-memory). This prevents
        a newly restarted daemon from interfering with stale DB rows left
        by a previous daemon instance.

        Now enhanced with session status inspection to avoid false positives
        from missed SSE events.
        """
        import hive.orchestrator as _mod

        Config = _mod.Config

        if not self.active_agents:
            return

        # Check each active agent against heartbeat freshness in DB.
        stalled = []
        for agent_id, agent in list(self.active_agents.items()):
            try:
                cursor = self.db.conn.execute(
                    """
                    SELECT last_heartbeat_at
                    FROM agents
                    WHERE id = ? AND status = 'working'
                      AND (
                        last_heartbeat_at IS NULL
                        OR last_heartbeat_at < datetime('now', ?)
                      )
                    """,
                    (agent_id, f"-{Config.LEASE_DURATION} seconds"),
                )
                row = cursor.fetchone()
                if row:
                    stalled.append(agent)
            except Exception:
                pass

        # For stalled agents, check backend session status before handling
        for agent in stalled:
            await self._handle_stalled_with_session_check(agent)

    async def _handle_stalled_with_session_check(self, agent: AgentIdentity) -> StalledSessionCheckResult:
        """Handle stalled agent with backend session status verification.

        Heartbeat-expiry policy:
        - if result file parses, treat as completion immediately;
        - else check session status once:
          - idle -> completion path
          - busy -> refresh heartbeat and continue monitoring
          - error/not_found -> stalled path
        """
        import hive.orchestrator as _mod

        read_result_file = _mod.read_result_file

        file_result = read_result_file(agent.worktree)
        if file_result is not None:
            self.db.log_event(
                agent.issue_id,
                agent.agent_id,
                "missed_completion",
                {"source": "heartbeat_expiry", "reason": "result_file_present"},
            )
            await self.handle_agent_complete(agent, file_result=file_result)
            return StalledSessionCheckResult.STOP_MONITORING

        try:
            # Query backend for actual session status
            status = await self.backend.get_session_status(agent.session_id, directory=agent.worktree)
            status_type = status.get("type") if isinstance(status, dict) else None

            if status_type == "idle":
                # Idle is only a hint; completion still routes through standard handler.
                self.db.log_event(
                    agent.issue_id,
                    agent.agent_id,
                    "missed_completion",
                    {"source": "heartbeat_expiry", "session_status": "idle"},
                )
                await self.handle_agent_complete(agent)
                return StalledSessionCheckResult.STOP_MONITORING

            if status_type == "busy":
                self._session_last_activity[agent.session_id] = datetime.now()
                self.db.try_touch_agent_heartbeat(agent.agent_id)
                self.db.log_event(
                    agent.issue_id,
                    agent.agent_id,
                    "heartbeat_refreshed",
                    {"session_status": "busy"},
                )
                return StalledSessionCheckResult.CONTINUE_MONITORING

            if status_type in ("error", "not_found"):
                await self.handle_stalled_agent(agent)
                return StalledSessionCheckResult.STOP_MONITORING

        except Exception as e:
            # Backend API failure falls through to stalled path.
            self.db.log_event(agent.issue_id, agent.agent_id, "session_check_failed", {"error": str(e), "fallback": "handle_stalled_agent"})

        # Default fallback behavior
        await self.handle_stalled_agent(agent)
        return StalledSessionCheckResult.STOP_MONITORING
