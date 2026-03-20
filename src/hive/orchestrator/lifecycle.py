"""Lifecycle and spawn/monitor mixin for the Hive orchestrator."""

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..config import WORKER_PERMISSIONS
from ..prompts import build_retry_context, build_system_prompt, build_worker_prompt, get_prompt_version
from ..status import BackendSessionStatusType, IssueStatus, parse_backend_session_status_type
from ..utils import AgentIdentity, CompletionResult, generate_id
from .completion import _exc_detail
from .deps import deps

logger = logging.getLogger(__name__)


class StalledTransition(StrEnum):
    """Transition outcomes for stalled-agent handling."""

    FAIL_STALLED_IN_PROGRESS = "fail_stalled_in_progress"
    FAIL_STALLED_TERMINAL = "fail_stalled_terminal"


class StalledSessionCheckResult(StrEnum):
    """Outcome for lease-expiry session verification."""

    CONTINUE_MONITORING = "continue_monitoring"
    STOP_MONITORING = "stop_monitoring"


class MonitorSignal(StrEnum):
    """Normalized monitor loop outcomes."""

    FILE_RESULT = "file_result"
    IDLE_HINT = "idle_hint"
    CANCELED = "canceled"
    CONTINUE_MONITORING = "continue_monitoring"
    STOP_MONITORING = "stop_monitoring"


class AgentLivenessState(StrEnum):
    """Observed liveness state for one agent/session probe."""

    FILE_RESULT = "file_result"
    SESSION_IDLE = "session_idle"
    SESSION_BUSY = "session_busy"
    SESSION_UNAVAILABLE = "session_unavailable"


@dataclass
class MonitorStep:
    """A single monitor loop outcome."""

    signal: MonitorSignal
    detection_via: str | None = None
    file_result: dict[str, Any] | None = None


@dataclass
class AgentLivenessProbe:
    """Result of reading completion truth plus backend session state."""

    state: AgentLivenessState
    file_result: dict[str, Any] | None = None
    session_status: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class SpawnContext:
    """Resolved spawn inputs for a single worker launch."""

    issue: dict[str, Any]
    issue_id: str
    issue_project: str
    agent_name: str
    model: str
    project_path: Path


@dataclass
class SpawnResources:
    """Resources allocated while spawning a worker."""

    agent_id: str
    worktree: str | None = None
    session_id: str | None = None


class LifecycleMixin:
    """Mixin providing worker spawn, monitor, and stall handling."""

    async def spawn_worker(self, issue: dict[str, str]):
        """Spawn a new worker agent to handle an issue."""
        issue_id = issue["id"]
        self._spawning_issues.add(issue_id)
        try:
            await self._spawn_worker_inner(issue)
        finally:
            self._spawning_issues.discard(issue_id)

    async def _spawn_worker_inner(self, issue: dict[str, str]):
        """Inner spawn logic, wrapped by spawn_worker's TOCTOU guard."""
        ctx = self._prepare_spawn(issue)
        resources = await self._create_spawn_resources(ctx)
        if resources is None:
            return

        if not await self._claim_spawn_issue(ctx, resources):
            return

        await self._activate_spawn(ctx, resources)

    def _prepare_spawn(self, issue: dict[str, Any]) -> SpawnContext:
        """Resolve the immutable inputs for a worker spawn."""
        issue_id = issue["id"]
        issue_project = issue["project"]
        agent_name = generate_id("worker")
        model = issue.get("model") or deps.Config.WORKER_MODEL or deps.Config.DEFAULT_MODEL

        # Resolve project path from the DB — raises ValueError for unknown projects.
        project_path = self._resolve_project_path(issue_project)

        # Ensure the merge pool has a processor for this project (lazy registration).
        self.merge_pool.get(issue_project, str(project_path))

        return SpawnContext(
            issue=issue,
            issue_id=issue_id,
            issue_project=issue_project,
            agent_name=agent_name,
            model=model,
            project_path=project_path,
        )

    async def _create_spawn_resources(self, ctx: SpawnContext) -> SpawnResources | None:
        """Create the DB agent row and worktree needed before issue claim."""
        agent_id = self.db.create_agent(
            name=ctx.agent_name,
            model=ctx.model,
            metadata={"issue_id": ctx.issue_id},
            project=ctx.issue_project,
        )
        resources = SpawnResources(agent_id=agent_id)

        try:
            resources.worktree = await deps.create_worktree_async(str(ctx.project_path), ctx.agent_name)
        except Exception as e:
            self.db.log_event(
                ctx.issue_id,
                resources.agent_id,
                "worktree_error",
                {"error": _exc_detail(e)},
            )
            await self._cleanup_agent(agent_id=resources.agent_id, delete_agent_row=True)
            return None

        return resources

    async def _claim_spawn_issue(self, ctx: SpawnContext, resources: SpawnResources) -> bool:
        """Attempt to claim the issue for this freshly-created agent."""
        claimed = self.db.claim_issue(ctx.issue_id, resources.agent_id)
        if claimed:
            return True

        await self._cleanup_agent(
            agent_id=resources.agent_id,
            worktree=resources.worktree,
            remove_worktree=True,
            delete_agent_row=True,
        )
        return False

    async def _activate_spawn(self, ctx: SpawnContext, resources: SpawnResources) -> AgentIdentity | None:
        """Create the backend session, register the agent, and dispatch work."""
        assert resources.worktree is not None

        try:
            backend = self._backend_for_project(ctx.issue_project)
            session = await backend.create_session(
                directory=resources.worktree,
                title=f"{ctx.agent_name}: {ctx.issue['title']}",
                permissions=WORKER_PERMISSIONS,
            )
            resources.session_id = session["id"]
            self.backend_pool.track_session(resources.session_id, backend)

            with self.db.transaction() as conn:
                conn.execute(
                    """
                    UPDATE agents
                    SET session_id = ?,
                        worktree = ?,
                        last_heartbeat_at = datetime('now'),
                        last_progress_at = datetime('now')
                    WHERE id = ?
                    """,
                    (resources.session_id, resources.worktree, resources.agent_id),
                )

            agent = AgentIdentity(
                agent_id=resources.agent_id,
                name=ctx.agent_name,
                issue_id=ctx.issue_id,
                worktree=resources.worktree,
                session_id=resources.session_id,
                project=ctx.issue_project,
            )
            self._register_active_agent(agent)

            await self._dispatch_worker_to_issue(
                agent=agent,
                issue=ctx.issue,
                model=ctx.model,
                started_event_type="worker_started",
                started_event_detail={
                    "session_id": resources.session_id,
                    "worktree": resources.worktree,
                    "routing_method": "new_agent",
                    "prompt_version": get_prompt_version("worker"),
                    "model": ctx.model,
                },
            )
            return agent

        except Exception as e:
            self.db.log_event(
                ctx.issue_id,
                resources.agent_id,
                "spawn_error",
                {"error": _exc_detail(e)},
            )
            await self._cleanup_agent(
                agent_id=resources.agent_id,
                worktree=resources.worktree,
                session_id=resources.session_id,
                cleanup_session=bool(resources.session_id),
                unregister_agent=True,
                mark_failed=True,
                remove_worktree=True,
                delete_agent_row=False,
            )
            self.db.try_transition_issue_status(
                ctx.issue_id,
                from_status=IssueStatus.IN_PROGRESS,
                to_status=IssueStatus.ESCALATED,
                expected_assignee=resources.agent_id,
            )
            self.db.log_event(ctx.issue_id, resources.agent_id, "escalated", {"reason": "Spawn failure"})
            return None

    def _gather_notes_for_worker(self, issue_id: str, project: str) -> list[dict[str, Any]] | None:
        """Gather project-wide notes for a worker prompt. Returns None if none found (so caller skips the section)."""
        seen_ids: set = set()
        notes: list[dict[str, Any]] = []

        # Get recent project-wide notes
        for note in self.db.get_notes(project=project, limit=10):
            if note["id"] not in seen_ids:
                seen_ids.add(note["id"])
                notes.append(note)

        return notes if notes else None

    def _is_issue_canceled(self, issue_id: str) -> bool:
        """Check if an issue has been canceled in the database."""
        with suppress(Exception):
            issue = self.db.get_issue(issue_id)
            return issue is not None and issue.get("status") == IssueStatus.CANCELED
        return False

    async def _dispatch_worker_to_issue(
        self,
        *,
        agent: AgentIdentity,
        issue: dict[str, Any],
        model: str,
        started_event_type: str,
        started_event_detail: dict[str, Any],
    ):
        """Shared prompt + dispatch flow for worker spawning."""
        issue_id = issue["id"]
        issue_project = issue["project"]

        worker_notes = self._gather_notes_for_worker(issue_id, issue_project)
        if worker_notes:
            self.db.log_event(issue_id, agent.agent_id, "notes_injected", {"count": len(worker_notes)})

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

        backend = self._backend_for_session(agent.session_id)
        await backend.send_message_async(
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
        """Monitor an agent until completion.

        Uses dual detection:
        1. SSE events: `session.status` → `idle` sets an asyncio.Event immediately
        2. Polling fallback: polls `get_session_status` periodically to catch idle
           transitions missed by SSE (reconnect gaps, etc.)

        Reads the result file for structured completion data after idle is detected.
        Cancels immediately if the issue is canceled while the agent is working.
        """
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
            check_interval = min(30, deps.Config.LEASE_DURATION // 4)
            completion_detected_via = "unknown"
            file_result: dict[str, Any] | None = None
            idle_hint_seen = False
            logger.info(
                f"Starting monitor for session {my_session_id} "
                f"(agent={agent.agent_id}, issue={agent.issue_id}, check_interval={check_interval}s)"
            )
            while True:
                step = self._read_monitor_completion_truth(agent)
                if step is not None:
                    file_result = step.file_result
                    if completion_detected_via == "unknown":
                        completion_detected_via = step.detection_via or "file"
                    break

                # If the session has gone idle (via SSE or polling) but the
                # worker didn't write a result file, treat that as completion
                # and let handle_agent_complete record the failure instead of
                # waiting out the full lease duration.
                if idle_hint_seen:
                    if completion_detected_via == "unknown":
                        completion_detected_via = "idle_hint_no_file"
                    break

                step = await self._wait_for_monitor_signal(
                    agent,
                    session_id=my_session_id,
                    event=event,
                    check_interval=check_interval,
                    lease_duration=deps.Config.LEASE_DURATION,
                )
                if step.signal == MonitorSignal.CANCELED:
                    return
                if step.signal == MonitorSignal.STOP_MONITORING:
                    return
                if step.signal == MonitorSignal.FILE_RESULT:
                    file_result = step.file_result
                    completion_detected_via = step.detection_via or completion_detected_via
                    break
                if step.signal == MonitorSignal.IDLE_HINT:
                    completion_detected_via = step.detection_via or completion_detected_via
                    idle_hint_seen = True

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
            # Tear down the agent so it doesn't leak in active_agents.
            # remove_worktree=True: no other path will clean it up since
            # the agent is about to be unregistered and marked failed.
            await self._cleanup_agent(agent, cleanup_session=True, unregister_agent=True, mark_failed=True, remove_worktree=True)
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

    def _read_monitor_completion_truth(self, agent: AgentIdentity) -> MonitorStep | None:
        """Return structured completion truth when the result file is present."""
        file_result = deps.read_result_file(agent.worktree)
        if file_result is None:
            return None
        return MonitorStep(
            signal=MonitorSignal.FILE_RESULT,
            detection_via="file",
            file_result=file_result,
        )

    def _refresh_agent_heartbeat(self, session_id: str, agent: AgentIdentity) -> None:
        """Refresh heartbeat state when a session is observed busy.

        Updates _session_last_activity, touches the DB heartbeat, and logs the
        heartbeat_refreshed event. Called from both _handle_monitor_timeout and
        _handle_stalled_with_session_check.
        """
        self._session_last_activity[session_id] = datetime.now()
        self.db.try_touch_agent_heartbeat(agent.agent_id)
        self.db.log_event(
            agent.issue_id,
            agent.agent_id,
            "heartbeat_refreshed",
            {"session_status": BackendSessionStatusType.BUSY.value},
        )

    async def _probe_agent_liveness(self, agent: AgentIdentity, *, session_id: str | None = None) -> AgentLivenessProbe:
        """Read result-file truth first, then one backend session-status snapshot."""
        completion_truth = self._read_monitor_completion_truth(agent)
        if completion_truth is not None:
            return AgentLivenessProbe(
                state=AgentLivenessState.FILE_RESULT,
                file_result=completion_truth.file_result,
            )

        probe_session_id = session_id or agent.session_id
        try:
            backend = self._backend_for_session(probe_session_id)
            status = await backend.get_session_status(probe_session_id, directory=agent.worktree)
        except Exception as e:
            return AgentLivenessProbe(
                state=AgentLivenessState.SESSION_UNAVAILABLE,
                error=str(e),
            )

        raw_status_type = status.get("type") if isinstance(status, dict) else None
        status_type = parse_backend_session_status_type(raw_status_type)
        if status_type == BackendSessionStatusType.IDLE:
            return AgentLivenessProbe(
                state=AgentLivenessState.SESSION_IDLE,
                session_status=BackendSessionStatusType.IDLE.value,
            )
        if status_type == BackendSessionStatusType.BUSY:
            return AgentLivenessProbe(
                state=AgentLivenessState.SESSION_BUSY,
                session_status=BackendSessionStatusType.BUSY.value,
            )
        return AgentLivenessProbe(
            state=AgentLivenessState.SESSION_UNAVAILABLE,
            session_status=raw_status_type,
        )

    async def _wait_for_monitor_signal(
        self,
        agent: AgentIdentity,
        *,
        session_id: str,
        event: asyncio.Event,
        check_interval: int,
        lease_duration: int,
    ) -> MonitorStep:
        """Wait for the next monitor signal or timeout-driven fallback."""
        try:
            await asyncio.wait_for(event.wait(), timeout=check_interval)
        except asyncio.TimeoutError:
            return await self._handle_monitor_timeout(
                agent,
                session_id=session_id,
                event=event,
                lease_duration=lease_duration,
            )

        if self._is_issue_canceled(agent.issue_id):
            # Issue was canceled while agent was working.
            # cancel_agent_for_issue already handled cleanup + set the event.
            logger.info(f"Monitor exiting due to cancellation for session {session_id} (agent={agent.agent_id}, issue={agent.issue_id})")
            return MonitorStep(signal=MonitorSignal.CANCELED)

        event.clear()
        return MonitorStep(
            signal=MonitorSignal.IDLE_HINT,
            detection_via="event_hint",
        )

    async def _handle_monitor_timeout(
        self,
        agent: AgentIdentity,
        *,
        session_id: str,
        event: asyncio.Event,
        lease_duration: int,
    ) -> MonitorStep:
        """Handle one monitor timeout tick."""
        logger.debug(
            f"Monitor timeout waiting for idle event for session {session_id}; "
            f"polling fallback (event_set={event.is_set()}, current_agent_session={agent.session_id})"
        )

        if self._is_issue_canceled(agent.issue_id):
            await self.cancel_agent_for_issue(agent.issue_id)
            return MonitorStep(signal=MonitorSignal.CANCELED)

        probe = await self._probe_agent_liveness(agent, session_id=session_id)
        if probe.state == AgentLivenessState.FILE_RESULT:
            return MonitorStep(
                signal=MonitorSignal.FILE_RESULT,
                detection_via="poll_file",
                file_result=probe.file_result,
            )

        if probe.state == AgentLivenessState.SESSION_IDLE:
            logger.info(
                f"Session poll detected idle for session {session_id} "
                f"(agent={agent.agent_id}, issue={agent.issue_id}, status={probe.session_status})"
            )
            return MonitorStep(
                signal=MonitorSignal.IDLE_HINT,
                detection_via="poll_hint",
            )

        if probe.state == AgentLivenessState.SESSION_BUSY:
            logger.debug(
                f"Session poll observed active status for session {session_id} "
                f"(agent={agent.agent_id}, issue={agent.issue_id}, status={probe.session_status})"
            )
        elif probe.error:
            logger.warning(f"Session poll failed for session {session_id} (agent={agent.agent_id}, issue={agent.issue_id}): {probe.error}")
        else:
            logger.warning(
                f"Session poll observed non-runnable status for session {session_id} "
                f"(agent={agent.agent_id}, issue={agent.issue_id}, status={probe.session_status})"
            )

        last_activity = self._session_last_activity.get(session_id, datetime.now())
        elapsed = (datetime.now() - last_activity).total_seconds()
        if elapsed <= lease_duration:
            return MonitorStep(signal=MonitorSignal.CONTINUE_MONITORING)

        if probe.state == AgentLivenessState.SESSION_BUSY:
            self._refresh_agent_heartbeat(session_id, agent)
            return MonitorStep(signal=MonitorSignal.CONTINUE_MONITORING)

        # Heartbeat appears stale. Re-check file/session once.
        check_result = await self._handle_stalled_with_session_check(
            agent,
            session_id_override=session_id,
        )
        if check_result == StalledSessionCheckResult.CONTINUE_MONITORING:
            # Status is still busy; keep monitor alive.
            self._session_last_activity[session_id] = datetime.now()
            return MonitorStep(signal=MonitorSignal.CONTINUE_MONITORING)

        return MonitorStep(signal=MonitorSignal.STOP_MONITORING)

    async def cancel_agent_for_issue(self, issue_id: str):
        """Abort the backend session and clean up when an issue is canceled while an agent is working."""
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

        self.db.log_event(
            issue_id,
            agent.agent_id,
            "agent_canceled",
            {"reason": "issue canceled by user, session aborted"},
        )

        await self._cleanup_agent(agent, cleanup_session=True, unregister_agent=True, mark_failed=True, remove_worktree=True)

    async def handle_stalled_agent(self, agent: AgentIdentity):
        """Handle a stalled agent (lease expired). Routes through the retry escalation chain so repeatedly stalling issues eventually get escalated."""
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
            if current_issue and current_issue.get("status") == IssueStatus.IN_PROGRESS:
                stalled_transition = StalledTransition.FAIL_STALLED_IN_PROGRESS
                stall_result = CompletionResult(
                    success=False,
                    reason="Agent stalled (lease expired, no activity)",
                    summary="Worker became unresponsive",
                )
                await self._handle_agent_failure(agent, stall_result)
        finally:
            logger.debug(f"Stall transition for {agent.name}: {stalled_transition.value}")
            await self._cleanup_agent(agent, cleanup_session=True, unregister_agent=True, mark_failed=True, remove_worktree=True)

    async def check_stalled_agents(self):
        """Check stalled agents owned by THIS daemon. Only checks self.active_agents (in-memory) to avoid interfering with stale DB rows from a previous daemon run."""
        if not self.active_agents:
            return

        # Check each active agent against heartbeat freshness in DB.
        stalled = []
        for agent_id, agent in list(self.active_agents.items()):
            with suppress(Exception):
                row = self.db.conn.execute(
                    """
                    SELECT last_heartbeat_at
                    FROM agents
                    WHERE id = ? AND status = 'working'
                      AND (
                        last_heartbeat_at IS NULL
                        OR last_heartbeat_at < datetime('now', ?)
                      )
                    """,
                    (agent_id, f"-{deps.Config.LEASE_DURATION} seconds"),
                ).fetchone()
                if row:
                    stalled.append(agent)

        # For stalled agents, check backend session status before handling
        for agent in stalled:
            await self._handle_stalled_with_session_check(agent)

    async def _handle_stalled_with_session_check(
        self,
        agent: AgentIdentity,
        *,
        session_id_override: str | None = None,
    ) -> StalledSessionCheckResult:
        """Handle stalled agent with backend session status verification.

        Heartbeat-expiry policy:
        - if result file parses, treat as completion immediately;
        - else check session status once:
          - idle -> completion path
          - busy -> refresh heartbeat and continue monitoring
          - error/not_found -> stalled path
        """
        probe = await self._probe_agent_liveness(agent, session_id=session_id_override)
        if probe.state == AgentLivenessState.FILE_RESULT:
            self.db.log_event(
                agent.issue_id,
                agent.agent_id,
                "missed_completion",
                {"source": "heartbeat_expiry", "reason": "result_file_present"},
            )
            await self.handle_agent_complete(agent, file_result=probe.file_result)
            return StalledSessionCheckResult.STOP_MONITORING

        if probe.state == AgentLivenessState.SESSION_IDLE:
            # Idle is only a hint; completion still routes through standard handler.
            self.db.log_event(
                agent.issue_id,
                agent.agent_id,
                "missed_completion",
                {"source": "heartbeat_expiry", "session_status": BackendSessionStatusType.IDLE.value},
            )
            await self.handle_agent_complete(agent)
            return StalledSessionCheckResult.STOP_MONITORING

        if probe.state == AgentLivenessState.SESSION_BUSY:
            activity_session_id = session_id_override or agent.session_id
            self._refresh_agent_heartbeat(activity_session_id, agent)
            return StalledSessionCheckResult.CONTINUE_MONITORING

        if probe.error:
            # Backend API failure falls through to stalled path.
            self.db.log_event(
                agent.issue_id,
                agent.agent_id,
                "session_check_failed",
                {"error": probe.error, "fallback": "handle_stalled_agent"},
            )

        # Default fallback behavior
        await self.handle_stalled_agent(agent)
        return StalledSessionCheckResult.STOP_MONITORING
