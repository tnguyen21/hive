"""Main orchestrator for Hive multi-agent system."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Dict, List, Optional

from .config import Config, WORKER_PERMISSIONS
from .db import Database
from .git import create_worktree_async, get_commit_hash, has_diff_from_main_async, remove_worktree_async
from .merge import MergeProcessorPool
from .utils import generate_id, AgentIdentity, CompletionResult
from .backends import HiveBackend
from .prompts import (
    assess_completion,
    build_retry_context,
    build_system_prompt,
    build_worker_prompt,
    get_prompt_version,
    read_notes_file,
    read_result_file,
    remove_notes_file,
    remove_result_file,
    render_inbox_section,
)

logger = logging.getLogger(__name__)


def _exc_detail(e: BaseException) -> str:
    """Return a non-empty, human-readable string describing an exception.

    str(e) is empty for exceptions like asyncio.TimeoutError that carry no
    message. In that case fall back to the exception type name, and if a
    message is present prefix it with the type name for extra clarity.
    """
    msg = str(e)
    name = type(e).__name__
    if not msg:
        return name
    return f"{name}: {msg}"


class CompletionTransition(str, Enum):
    """Transition outcomes for completion handling."""

    SKIP_TERMINAL_ISSUE = "skip_terminal_issue"
    FAIL_BUDGET = "fail_budget"
    FAIL_ASSESSMENT = "fail_assessment"
    FAIL_VALIDATION_NO_DIFF = "fail_validation_no_diff"
    SUCCESS_DONE = "success_done"
    ERROR_COMPLETION_HANDLER = "error_completion_handler"


class EscalationDecision(str, Enum):
    """Escalation routing decision after a failure."""

    RETRY = "retry"
    AGENT_SWITCH = "agent_switch"
    ESCALATE = "escalate"
    ANOMALY_ESCALATE = "anomaly_escalate"


class StalledTransition(str, Enum):
    """Transition outcomes for stalled-agent handling."""

    FAIL_STALLED_IN_PROGRESS = "fail_stalled_in_progress"
    FAIL_STALLED_TERMINAL = "fail_stalled_terminal"


class StalledSessionCheckResult(str, Enum):
    """Outcome for lease-expiry session verification."""

    CONTINUE_MONITORING = "continue_monitoring"
    STOP_MONITORING = "stop_monitoring"


@dataclass
class CompletionDecision:
    """Decision payload from completion transition analysis."""

    transition: CompletionTransition
    result: Optional[CompletionResult] = None
    terminal_status: Optional[str] = None
    budget_tokens: Optional[int] = None
    validation_original_summary: Optional[str] = None


class Orchestrator:
    """Main orchestration engine for Hive."""

    def __init__(
        self,
        db: Database,
        backend: HiveBackend,
    ):
        """
        Initialize orchestrator.

        Args:
            db: Database instance
            backend: Backend implementing session management and event streaming
        """
        self.db = db
        self.backend = backend

        # Merge processor pool (one processor per project, lazy-created)
        self.merge_pool = MergeProcessorPool(db=db, backend=backend)

        # Track active agents
        self.active_agents: Dict[str, AgentIdentity] = {}

        # Reverse lookup maps for O(1) session_id and issue_id lookups
        self._session_to_agent: dict[str, str] = {}  # session_id -> agent_id
        self._issue_to_agent: dict[str, str] = {}  # issue_id -> agent_id

        # Event handlers
        self.session_status_events: Dict[str, asyncio.Event] = {}

        # Track last SSE activity per session for lease renewal
        self._session_last_activity: Dict[str, datetime] = {}

        # Running flag
        self.running = False

        # Degraded mode state
        self._backend_healthy = True
        self._degraded_since: Optional[datetime] = None
        self._backoff_delay = 5  # Initial backoff delay in seconds

        # Guard against TOCTOU race in main_loop: tracks issue_ids currently
        # being spawned. Prevents duplicate spawn_worker calls when
        # create_worktree_async yields control back to the event loop.
        self._spawning_issues: set[str] = set()

    def _resolve_project_path(self, project_name: str) -> Path:
        """Resolve the filesystem path for a registered project.

        Args:
            project_name: Registered project name

        Returns:
            Resolved Path object for the project

        Raises:
            ValueError: If the project is not registered in the DB
        """
        path = self.db.get_project_path(project_name)
        if path is None:
            raise ValueError(f"Unknown project: {project_name}")
        return Path(path)

    def _setup_sse_handlers(self):
        """Set up SSE event handlers."""

        async def handle_session_status(properties):
            session_id = properties.get("sessionID")
            status = properties.get("status", {})
            status_type = status.get("type")

            if not session_id:
                logger.warning(f"session.status event missing sessionID: {properties}")
                return

            # Any session activity = refresh worker heartbeat for this session.
            self._record_heartbeat_for_session(session_id)

            # If session becomes idle, signal completion
            if status_type == "idle":
                event = self.session_status_events.get(session_id)
                logger.info(
                    f"Received idle session.status for session {session_id} "
                    f"(event_exists={bool(event)}, mapped_agent={self._session_to_agent.get(session_id)})"
                )
                if event:
                    event.set()
                else:
                    logger.warning(
                        f"Idle status received for session {session_id} but no monitor event exists "
                        f"(mapped_agent={self._session_to_agent.get(session_id)})"
                    )

        self.backend.on("session.status", handle_session_status)

        async def handle_session_error(properties):
            session_id = properties.get("sessionID")
            if not session_id:
                return
            agent_id = self._session_to_agent.get(session_id)
            if not agent_id or agent_id not in self.active_agents:
                return
            agent = self.active_agents[agent_id]
            logger.error(f"Session error for {agent.name}: {properties}")
            self.db.log_event(agent.issue_id, agent.agent_id, "session_error", {"session_id": session_id, "error": properties})
            await self.handle_stalled_agent(agent)

        self.backend.on("session.error", handle_session_error)

        # Register permission event handler
        self.backend.on("permission.request", self._handle_permission_event)

    async def _handle_permission_event(self, event_data: dict):
        """Handle permission request from SSE event — resolve immediately."""
        try:
            perm_id = event_data.get("id")
            if not perm_id:
                # If SSE event doesn't include full permission data,
                # fetch pending permissions and resolve
                pending = await self.backend.get_pending_permissions()
                for perm in pending:
                    decision = self.evaluate_permission_policy(perm)
                    if decision:
                        await self.backend.reply_permission(perm["id"], reply=decision)
                        self._log_permission_resolved(perm, decision)
                return

            decision = self.evaluate_permission_policy(event_data)
            if decision:
                await self.backend.reply_permission(perm_id, reply=decision)
                self._log_permission_resolved(event_data, decision)

            logger.debug(f"Handled permission event via SSE: {perm_id}, decision: {decision}")

        except Exception as e:
            logger.warning(f"Error handling permission event: {e}")

    def _log_permission_resolved(self, perm: Dict[str, Any], decision: str):
        """Log permission resolution event."""
        session_id = perm.get("sessionID")
        agent_id = self._session_to_agent.get(session_id) if session_id else None
        issue_id = None

        if agent_id and agent_id in self.active_agents:
            agent = self.active_agents[agent_id]
            issue_id = agent.issue_id

        if issue_id and agent_id:
            self.db.log_event(
                issue_id,
                agent_id,
                "permission_resolved",
                {
                    "permission": perm.get("permission"),
                    "patterns": perm.get("patterns"),
                    "decision": decision,
                },
            )

    def _record_heartbeat_for_session(self, session_id: str):
        """Record worker heartbeat for the agent associated with a session.

        Called on any SSE activity from the session, proving the worker
        is still alive and making progress.
        """
        now = datetime.now()
        self._session_last_activity[session_id] = now

        # Find agent for this session and touch heartbeat in DB.
        agent_id = self._session_to_agent.get(session_id)
        if agent_id and agent_id in self.active_agents:
            try:
                self.db.try_touch_agent_heartbeat(agent_id)
            except Exception:
                pass  # Non-critical, best-effort

    async def _reconcile_stale_agents(self):
        """Bidirectional reconciliation on startup.

        Four phases:
        - Phase 0: Fetch live sessions from OpenCode server
        - Phase 1: Reconcile DB agents with status='working' (ghost + live)
        - Phase 2: Clean up orphan sessions (alive on server, no DB agent)
        - Phase 3: Purge idle/failed agents (leftovers from previous runs)
        """
        # Phase 0 — Fetch live sessions
        live_session_ids: set | None = None
        try:
            sessions = await self.backend.list_sessions()
            live_session_ids = {s["id"] for s in sessions}
            logger.info(f"Fetched {len(live_session_ids)} live session(s) from backend")
        except Exception as e:
            logger.warning(f"Could not fetch live sessions from backend ({e}), falling back to DB-only reconciliation")

        # Phase 1 — Reconcile stale DB agents
        cursor = self.db.conn.execute(
            """
            SELECT id, current_issue, worktree, name, session_id
            FROM agents
            WHERE status = 'working'
            """
        )
        stale = cursor.fetchall()

        if stale:
            logger.info(f"Reconciling {len(stale)} stale agent(s) from previous run")

        for row in stale:
            agent_dict = dict(row)
            agent_id = agent_dict["id"]
            issue_id = agent_dict["current_issue"]
            worktree = agent_dict["worktree"]
            session_id = agent_dict["session_id"]

            if session_id:
                if live_session_ids is not None:
                    # Authoritative: we know which sessions are alive
                    if session_id in live_session_ids:
                        # Session still running — abort + delete it
                        await self.backend.cleanup_session(session_id, directory=worktree)
                        live_session_ids.discard(session_id)
                    else:
                        # Ghost agent — session already gone, just log
                        logger.info(f"Agent {agent_id} is a ghost (session {session_id} no longer exists)")
                else:
                    # OpenCode unreachable — best-effort abort/delete
                    await self.backend.cleanup_session(session_id, directory=worktree)

            # Mark agent failed
            self.db.conn.execute(
                "UPDATE agents SET status = 'failed', current_issue = NULL, session_id = NULL WHERE id = ?",
                (agent_id,),
            )

            # Release the issue if still in_progress — but only if it
            # hasn't exhausted its retry budget. Otherwise escalate to
            # prevent an infinite spawn loop across daemon restarts.
            if issue_id:
                retry_count = self.db.count_events_by_type(issue_id, "retry")
                agent_switch_count = self.db.count_events_by_type(issue_id, "agent_switch")

                if retry_count < Config.MAX_RETRIES or agent_switch_count < Config.MAX_AGENT_SWITCHES:
                    self.db.try_transition_issue_status(
                        issue_id,
                        from_status="in_progress",
                        to_status="open",
                        expected_assignee=agent_id,
                    )
                    self.db.log_event(
                        issue_id,
                        agent_id,
                        "reconciled",
                        {"reason": "stale agent from previous daemon run"},
                    )
                else:
                    self.db.try_transition_issue_status(
                        issue_id,
                        from_status="in_progress",
                        to_status="escalated",
                        expected_assignee=agent_id,
                    )
                    self.db.log_event(issue_id, agent_id, "escalated", {"reason": "Stale agent with exhausted retry budget"})
                    self.db.log_event(
                        issue_id,
                        agent_id,
                        "reconciled",
                        {"reason": "stale agent, retry budget exhausted — escalating"},
                    )

            # Clean up worktree — but NOT if the issue is done with a
            # pending merge queue entry. The merge processor still needs the
            # worktree to run refinery review.
            worktree_needed = False
            if worktree and issue_id:
                mq_row = self.db.conn.execute(
                    "SELECT id FROM merge_queue WHERE issue_id = ? AND status IN ('queued', 'running')",
                    (issue_id,),
                ).fetchone()
                if mq_row:
                    worktree_needed = True
                    logger.info(f"Preserving worktree {worktree} for pending merge of {issue_id}")

            if worktree and not worktree_needed:
                try:
                    await remove_worktree_async(worktree)
                except Exception:
                    pass

        if stale:
            self.db.conn.commit()
            logger.info(f"Reconciled {len(stale)} stale agent(s)")

        # Phase 2 — Clean up orphan sessions (alive on server, no DB agent)
        if live_session_ids is not None and live_session_ids:
            # Collect all session_ids known to the DB (any status)
            cursor = self.db.conn.execute("SELECT session_id FROM agents WHERE session_id IS NOT NULL")
            db_session_ids = {row["session_id"] for row in cursor.fetchall()}

            orphans = live_session_ids - db_session_ids
            if orphans:
                for session_id in orphans:
                    await self.backend.cleanup_session(session_id)

                self.db.log_system_event("orphan_sessions_cleaned", {"count": len(orphans)})
                logger.info(f"Cleaned up {len(orphans)} orphan session(s)")

        # Phase 3: Purge idle/failed agents (leftovers from previous runs)
        cursor = self.db.conn.execute("SELECT COUNT(*) FROM agents WHERE status IN ('idle', 'failed')")
        count = cursor.fetchone()[0]
        if count > 0:
            self.db.conn.execute("PRAGMA foreign_keys = OFF")
            self.db.conn.execute("DELETE FROM agents WHERE status IN ('idle', 'failed')")
            self.db.conn.execute("PRAGMA foreign_keys = ON")
            self.db.conn.commit()
            logger.info(f"Purged {count} idle/failed agent(s) from previous runs")

    def _rebuild_reverse_maps(self):
        """Rebuild reverse lookup maps from current active_agents.

        This is primarily for robustness and debugging. Under normal operation,
        the maps should be kept in sync through spawn_worker and _unregister_agent.
        """
        self._session_to_agent.clear()
        self._issue_to_agent.clear()

        for agent_id, agent in self.active_agents.items():
            self._session_to_agent[agent.session_id] = agent_id
            self._issue_to_agent[agent.issue_id] = agent_id

    def _register_active_agent(self, agent: AgentIdentity):
        """Register an agent in active maps for session/issue lookup."""
        self.active_agents[agent.agent_id] = agent
        self._session_to_agent[agent.session_id] = agent.agent_id
        self._issue_to_agent[agent.issue_id] = agent.agent_id

    def _unregister_agent(self, agent_id: str):
        """Remove an agent from active_agents and clean up reverse lookup maps.

        Args:
            agent_id: The agent ID to remove
        """
        agent = self.active_agents.get(agent_id)
        if agent:
            self._session_to_agent.pop(agent.session_id, None)
            self._issue_to_agent.pop(agent.issue_id, None)
            del self.active_agents[agent_id]

    async def _shutdown_all_sessions(self):
        """Mark agents as failed and release issues on shutdown.

        Process cleanup (killing children) is handled by the backend's
        __aexit__. This just updates DB state.
        """
        if not self.active_agents:
            return

        logger.info(f"Shutting down {len(self.active_agents)} active session(s)")

        for agent_id, agent in list(self.active_agents.items()):
            try:
                self.db.conn.execute(
                    """
                    UPDATE agents
                    SET status = 'failed', current_issue = NULL, session_id = NULL
                    WHERE id = ?
                    """,
                    (agent_id,),
                )
                self.db.conn.execute(
                    """
                    UPDATE issues
                    SET assignee = NULL, status = 'open'
                    WHERE id = ? AND status = 'in_progress'
                    """,
                    (agent.issue_id,),
                )
            except Exception:
                pass

        try:
            self.db.conn.commit()
        except Exception:
            pass

        self.active_agents.clear()
        self._session_to_agent.clear()
        self._issue_to_agent.clear()
        logger.info("All sessions shut down")

    def _log_token_usage(self, agent: AgentIdentity, messages: List[Dict[str, Any]]):
        """
        Extract token usage from messages and log as 'tokens_used' event.

        Args:
            agent: Agent identity
            messages: List of session messages from OpenCode
        """
        total_input_tokens = 0
        total_output_tokens = 0
        model = None

        for message in messages:
            metadata = message.get("metadata", {})
            if metadata:
                # Check if this message has token usage metadata
                input_tokens = metadata.get("input_tokens", 0)
                output_tokens = metadata.get("output_tokens", 0)
                msg_model = metadata.get("model")

                if input_tokens > 0 or output_tokens > 0:
                    total_input_tokens += input_tokens
                    total_output_tokens += output_tokens
                    if msg_model and not model:
                        model = msg_model

        # Only log if we found some token usage
        if total_input_tokens > 0 or total_output_tokens > 0:
            detail = {
                "input_tokens": total_input_tokens,
                "output_tokens": total_output_tokens,
            }
            if model:
                detail["model"] = model

            self.db.log_event(
                agent.issue_id,
                agent.agent_id,
                "tokens_used",
                detail,
            )

    async def cancel_agent_for_issue(self, issue_id: str):
        """Cancel the active agent working on an issue.

        Aborts the opencode session, cleans up the agent and worktree.
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

    async def start(self):
        """Start the orchestrator."""
        self.running = True
        self.db.log_system_event("daemon_started")
        self._setup_sse_handlers()

        # Start SSE/WS server in background FIRST — other init steps may need
        # to create sessions (e.g. eager refinery), which requires the server.
        sse_task = asyncio.create_task(self.backend.connect_with_reconnect())

        # If the backend has a server_ready gate, wait for it before proceeding.
        # IMPORTANT: if the event loop task dies before setting server_ready
        # (e.g. missing binary / auth failure), don't hang forever.
        if hasattr(self.sse_client, "server_ready"):
            server_ready = self.backend.server_ready
            while not server_ready.is_set():
                if sse_task.done():
                    exc = sse_task.exception()
                    raise exc or RuntimeError("Backend event loop exited before becoming ready")
                await asyncio.sleep(0.1)

        await self._reconcile_stale_agents()

        # Pre-populate merge pool from registered projects and initialize each processor.
        # This ensures process_all() is not a no-op even before the first issue is dispatched.
        for project in self.db.list_projects():
            processor = self.merge_pool.get(project["name"], project["path"])
            await processor.initialize()

        # Start permission unblocker in background
        permission_task = asyncio.create_task(self.permission_unblocker_loop())

        # Start merge queue processor in background
        merge_task = asyncio.create_task(self.merge_processor_loop())
        merge_task.add_done_callback(self._on_merge_task_done)

        try:
            # Run main loop
            await self.main_loop()
        finally:
            self.running = False
            # Abort all active opencode sessions before shutting down
            await self._shutdown_all_sessions()
            self.backend.stop()
            # Cancel background tasks so we don't block on their long sleeps
            for task in (sse_task, permission_task, merge_task):
                task.cancel()
            await asyncio.gather(sse_task, permission_task, merge_task, return_exceptions=True)

    async def main_loop(self):
        """Main orchestration loop."""
        while self.running:
            try:
                # Check if OpenCode is healthy before scheduling work
                if not self._backend_healthy:
                    # In degraded mode - check health with exponential backoff
                    healthy = await self._check_backend_health()

                    if healthy:
                        # OpenCode recovered
                        degraded_duration = (datetime.now() - self._degraded_since).total_seconds() if self._degraded_since else 0
                        self.db.log_system_event(
                            "opencode_recovered", {"degraded_duration_seconds": degraded_duration, "backoff_delay": self._backoff_delay}
                        )
                        self._backend_healthy = True
                        self._degraded_since = None
                        self._backoff_delay = 5  # Reset backoff
                        logger.info(f"Backend recovered after {degraded_duration:.1f}s degraded mode")
                    else:
                        # Still unhealthy - wait with exponential backoff
                        await asyncio.sleep(self._backoff_delay)
                        self._backoff_delay = min(60, self._backoff_delay * 2)  # Cap at 60 seconds
                        continue  # Skip scheduling and stall checks

                # Normal operation - check if we can spawn more agents
                # Count both registered agents and in-flight spawns to avoid
                # transient over-spawning during concurrent teardown/spawn.
                if len(self.active_agents) + len(self._spawning_issues) < Config.MAX_AGENTS:
                    # Get ready work across all registered projects
                    ready = self.db.get_ready_queue(project=None, limit=1)

                    if ready:
                        issue = ready[0]
                        if issue["id"] in self._spawning_issues:
                            await asyncio.sleep(1)  # Back off briefly while spawn completes
                            continue
                        try:
                            # Try to claim and spawn worker
                            await self.spawn_worker(issue)
                        except Exception as e:
                            # Check if the error suggests OpenCode is unhealthy
                            if self._is_backend_error(e):
                                await self._enter_degraded_mode(str(e))
                    else:
                        # No ready work, wait before polling again
                        await asyncio.sleep(Config.POLL_INTERVAL)
                else:
                    # At capacity, wait
                    await asyncio.sleep(Config.POLL_INTERVAL)

                # Check for stalled agents (only when healthy)
                if self._backend_healthy:
                    await self.check_stalled_agents()

            except Exception as e:
                # Check if this is an OpenCode connectivity issue
                if self._is_backend_error(e):
                    await self._enter_degraded_mode(str(e))
                else:
                    logger.error(f"Error in main loop: {e}")
                    await asyncio.sleep(Config.POLL_INTERVAL)

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

        # Create OpenCode session
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
            # Clean up the OpenCode session if it was created (best-effort —
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

        Combines epic-specific notes (if the issue is a step) with
        recent project-wide notes, deduplicating by note ID.

        Returns None if no notes are found (so build_worker_prompt skips the section).
        """
        seen_ids: set = set()
        notes: List[Dict[str, Any]] = []

        # Get epic-scoped notes if this is a step
        issue = self.db.get_issue(issue_id)
        if issue and issue.get("parent_id"):
            for note in self.db.get_notes_for_epic(issue["parent_id"]):
                if note["id"] not in seen_ids:
                    seen_ids.add(note["id"])
                    notes.append(note)

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
        """Poll opencode to check if a session has gone idle.

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

    async def _cleanup_session(self, agent: AgentIdentity):
        """Abort and delete an agent's opencode session.

        Called after agent completion or failure to ensure the session
        does not linger and consume tokens.
        """
        logger.info(f"Cleaning up session {agent.session_id} (agent={agent.agent_id}, issue={agent.issue_id}, worktree={agent.worktree})")
        await self.backend.cleanup_session(agent.session_id, directory=agent.worktree)

    def _mark_agent_failed(self, agent_id: str):
        """Mark an agent failed in DB and clear issue/session references."""
        self.db.conn.execute(
            """
            UPDATE agents
            SET status = 'failed',
                current_issue = NULL,
                session_id = NULL
            WHERE id = ?
            """,
            (agent_id,),
        )
        self.db.conn.commit()

    def _release_issue(self, issue_id: str, *, expected_assignee: str) -> bool:
        """Release an issue back to the open queue (CAS)."""
        return self.db.try_transition_issue_status(
            issue_id,
            from_status="in_progress",
            to_status="open",
            expected_assignee=expected_assignee,
        )

    def _try_claim_agent_for_handling(self, agent: AgentIdentity, *, handler_name: str) -> bool:
        """Claim agent handling ownership via DB CAS fence.

        The first handler that transitions `working -> failed` owns teardown and
        completion/failure routing for that agent. All concurrent handlers exit.
        """
        if agent.agent_id not in self.active_agents:
            logger.debug(f"Skipping {handler_name} for {agent.name} — already removed from active agents")
            return False

        claimed = self.db.try_transition_agent_status(
            agent.agent_id,
            from_status="working",
            to_status="failed",
        )
        if not claimed:
            logger.debug(f"Skipping {handler_name} for {agent.name} — already claimed by another handler")
            return False
        return True

    def _delete_agent_row(self, agent_id: str):
        """Delete agent row for early spawn-orphan cleanup paths."""
        self.db.conn.execute("PRAGMA foreign_keys = OFF")
        try:
            self.db.conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        finally:
            self.db.conn.execute("PRAGMA foreign_keys = ON")
        self.db.conn.commit()

    async def _best_effort_cleanup(self, label: str, op: Awaitable[Any]):
        """Run async cleanup operation and suppress failures with debug logging."""
        try:
            await op
        except Exception as e:
            logger.debug(f"Best-effort cleanup failed ({label}): {e}")

    async def _teardown_agent(self, agent: AgentIdentity, *, remove_worktree: bool = False):
        """Best-effort cleanup for session, in-memory registration, worktree, and DB state.

        Always marks the agent row as terminal ('failed') so stale 'working'
        rows don't accumulate across retries.  The merge processor stores its
        own copy of worktree/branch in the merge_queue table, so clearing the
        agent row's references is safe.
        """
        await self._best_effort_cleanup("cleanup_session", self._cleanup_session(agent))

        if agent.agent_id in self.active_agents:
            self._unregister_agent(agent.agent_id)

        # Mark agent as terminal in DB — prevents ghost 'working' rows from
        # accumulating when agents are retried / agent-switched.
        self._mark_agent_failed(agent.agent_id)

        if remove_worktree and agent.worktree:
            await self._best_effort_cleanup("remove_worktree", remove_worktree_async(agent.worktree))

    async def _dispatch_worker_to_issue(
        self,
        *,
        agent: AgentIdentity,
        issue: Dict[str, Any],
        model: str,
        started_event_type: str,
        started_event_detail: Dict[str, Any],
        completed_steps: Optional[List[str]] = None,
    ):
        """Shared prompt + dispatch flow for spawn and epic cycling."""
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
            completed_steps=completed_steps,
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

    async def _decide_completion_transition(
        self,
        agent: AgentIdentity,
        file_result: Optional[Dict[str, Any]] = None,
    ) -> CompletionDecision:
        """Decision phase for completion handling.

        Determines the next completion transition and any payload required by
        transition side effects.
        """
        terminal_issue = self.db.get_issue(agent.issue_id)
        if terminal_issue and terminal_issue.get("status") in ("canceled", "finalized"):
            return CompletionDecision(
                transition=CompletionTransition.SKIP_TERMINAL_ISSUE,
                terminal_status=terminal_issue["status"],
            )

        messages = await self.backend.get_messages(agent.session_id, directory=agent.worktree)
        self._log_token_usage(agent, messages)

        if Config.MAX_TOKENS_PER_ISSUE:
            budget_tokens = self.db.get_issue_token_total(agent.issue_id)
            if budget_tokens > Config.MAX_TOKENS_PER_ISSUE:
                logger.warning(f"Issue {agent.issue_id} exceeded token budget ({budget_tokens} > {Config.MAX_TOKENS_PER_ISSUE})")
                return CompletionDecision(
                    transition=CompletionTransition.FAIL_BUDGET,
                    budget_tokens=budget_tokens,
                    result=CompletionResult(
                        success=False,
                        reason=f"Exceeded per-issue token budget ({budget_tokens} > {Config.MAX_TOKENS_PER_ISSUE})",
                        summary=f"Terminated: per-issue token budget exceeded ({budget_tokens} tokens)",
                    ),
                )

        # Materialize issue-following targets so we catch all pending required notes
        self.db.materialize_issue_deliveries(agent.issue_id, agent.agent_id, agent.project)

        # Check for required unacked notes
        unacked = self.db.get_required_unacked_deliveries(agent.agent_id, agent.issue_id)
        if unacked:
            self.db.log_event(
                agent.issue_id,
                agent.agent_id,
                "completion_blocked_unacked_notes",
                {"count": len(unacked), "delivery_ids": [d["delivery_id"] for d in unacked]},
            )
            return CompletionDecision(
                transition=CompletionTransition.FAIL_ASSESSMENT,
                result=CompletionResult(
                    success=False,
                    reason=f"Cannot complete: {len(unacked)} required note(s) not acknowledged. Acknowledge via: hive mail ack <delivery_id>",
                    summary=f"Blocked by {len(unacked)} unacknowledged required note(s)",
                ),
            )

        result = assess_completion(messages, file_result=file_result)
        if not result.success:
            return CompletionDecision(
                transition=CompletionTransition.FAIL_ASSESSMENT,
                result=result,
            )

        has_commits = await has_diff_from_main_async(agent.worktree)
        if not has_commits:
            return CompletionDecision(
                transition=CompletionTransition.FAIL_VALIDATION_NO_DIFF,
                validation_original_summary=result.summary,
                result=CompletionResult(
                    success=False,
                    reason="No commits relative to main despite claiming success",
                    summary=result.summary,
                ),
            )

        return CompletionDecision(
            transition=CompletionTransition.SUCCESS_DONE,
            result=result,
        )

    # Completion transition table:
    # - SKIP_TERMINAL_ISSUE      -> log agent_complete_skipped
    # - FAIL_BUDGET              -> log budget_exceeded + _handle_agent_failure
    # - FAIL_ASSESSMENT          -> _handle_agent_failure
    # - FAIL_VALIDATION_NO_DIFF  -> log validation_failed + _handle_agent_failure
    # - SUCCESS_DONE             -> update done + enqueue merge + log completed
    # - ERROR_COMPLETION_HANDLER -> log completion_error
    async def handle_agent_complete(
        self,
        agent: AgentIdentity,
        file_result: Optional[Dict[str, Any]] = None,
    ):
        """
        Handle agent completion.

        Args:
            agent: Agent identity
            file_result: Optional parsed result from .hive-result.jsonl file.
                If provided, used directly for completion assessment (skips
                message parsing heuristics).
        """
        if not self._try_claim_agent_for_handling(agent, handler_name="completion handling"):
            return

        decision: Optional[CompletionDecision] = None
        remove_worktree_on_teardown = False

        try:
            # Always clean up the result file if it exists
            remove_result_file(agent.worktree)

            # Harvest notes (best-effort) — do this BEFORE the canceled check
            # so even canceled/failed workers' discoveries are saved.
            try:
                notes_data = read_notes_file(agent.worktree)
                if notes_data:
                    for note in notes_data:
                        self.db.add_note(
                            issue_id=agent.issue_id,
                            agent_id=agent.agent_id,
                            content=note.get("content", ""),
                            category=note.get("category", "discovery"),
                            project=agent.project,
                        )
                    self.db.log_event(agent.issue_id, agent.agent_id, "notes_harvested", {"count": len(notes_data)})
                    logger.info(f"Harvested {len(notes_data)} notes from {agent.name}")
            except Exception as e:
                logger.warning(f"Failed to harvest notes from {agent.name}: {e}")
            finally:
                remove_notes_file(agent.worktree)

            decision = await self._decide_completion_transition(agent, file_result=file_result)

            match decision.transition:
                case CompletionTransition.SKIP_TERMINAL_ISSUE:
                    status = decision.terminal_status or "unknown"
                    remove_worktree_on_teardown = True
                    self.db.log_event(
                        agent.issue_id,
                        agent.agent_id,
                        "agent_complete_skipped",
                        {"reason": f"issue already {status}, cleaning up session"},
                    )

                case CompletionTransition.FAIL_BUDGET:
                    remove_worktree_on_teardown = True
                    self.db.log_event(
                        agent.issue_id,
                        agent.agent_id,
                        "budget_exceeded",
                        {"issue_tokens": decision.budget_tokens, "limit": Config.MAX_TOKENS_PER_ISSUE},
                    )
                    if decision.result is not None:
                        await self._handle_agent_failure(agent, decision.result)

                case CompletionTransition.FAIL_VALIDATION_NO_DIFF:
                    remove_worktree_on_teardown = True
                    self.db.log_event(
                        agent.issue_id,
                        agent.agent_id,
                        "validation_failed",
                        {
                            "reason": "No commits relative to main despite claiming success",
                            "original_summary": decision.validation_original_summary,
                        },
                    )
                    if decision.result is not None:
                        await self._handle_agent_failure(agent, decision.result)

                case CompletionTransition.FAIL_ASSESSMENT:
                    remove_worktree_on_teardown = True
                    if decision.result is not None:
                        await self._handle_agent_failure(agent, decision.result)

                case CompletionTransition.SUCCESS_DONE:
                    if decision.result is None:
                        raise RuntimeError("Missing completion result for success transition")

                    transitioned = self.db.try_transition_issue_status(
                        agent.issue_id,
                        from_status="in_progress",
                        to_status="done",
                        expected_assignee=agent.agent_id,
                    )
                    if not transitioned:
                        current_issue = self.db.get_issue(agent.issue_id)
                        current_status = current_issue.get("status") if current_issue else None
                        if current_status != "done":
                            remove_worktree_on_teardown = True
                            self.db.log_event(
                                agent.issue_id,
                                agent.agent_id,
                                "agent_complete_skipped",
                                {"reason": f"success result but issue is {current_status or 'missing'}, skipping merge enqueue"},
                            )
                            return

                    # Get commit hash if available.
                    commit_hash = decision.result.git_commit or get_commit_hash(agent.worktree)

                    # Extract test_command from worker's file_result.
                    test_command = file_result.get("test_command") if file_result else None

                    self.db.enqueue_merge(
                        issue_id=agent.issue_id,
                        agent_id=agent.agent_id,
                        project=agent.project,
                        worktree=agent.worktree,
                        branch_name=f"agent/{agent.name}",
                        test_command=test_command,
                    )

                    # Get agent model from database.
                    agent_row = self.db.get_agent(agent.agent_id)
                    model = agent_row["model"] if agent_row else None

                    self.db.log_event(
                        agent.issue_id,
                        agent.agent_id,
                        "completed",
                        {
                            "summary": decision.result.summary,
                            "commit": commit_hash,
                            "artifacts": decision.result.artifacts,
                            "model": model,
                        },
                    )

                case _:
                    raise RuntimeError(f"Unhandled completion transition: {decision.transition}")

        except Exception as e:
            transition = decision.transition.value if decision else CompletionTransition.ERROR_COMPLETION_HANDLER.value
            self.db.log_event(
                agent.issue_id,
                agent.agent_id,
                "completion_error",
                {"error": str(e), "transition": transition},
            )
        finally:
            await self._teardown_agent(agent, remove_worktree=remove_worktree_on_teardown)

    def _choose_escalation(self, issue_id: str) -> EscalationDecision:
        """Decide escalation tier based on anomaly/retry/switch counts."""
        if Config.ANOMALY_FAILURE_THRESHOLD and Config.ANOMALY_WINDOW_MINUTES:
            recent_failures = self.db.count_events_since_minutes(issue_id, "incomplete", Config.ANOMALY_WINDOW_MINUTES)
            if recent_failures >= Config.ANOMALY_FAILURE_THRESHOLD:
                return EscalationDecision.ANOMALY_ESCALATE

        retry_count = self.db.count_events_by_type(issue_id, "retry")
        if retry_count < Config.MAX_RETRIES:
            return EscalationDecision.RETRY

        agent_switch_count = self.db.count_events_by_type(issue_id, "agent_switch")
        if agent_switch_count < Config.MAX_AGENT_SWITCHES:
            return EscalationDecision.AGENT_SWITCH

        return EscalationDecision.ESCALATE

    async def _handle_agent_failure(self, agent: AgentIdentity, result: CompletionResult):
        """State machine for failure routing: retry -> agent switch -> escalate."""
        issue_id = agent.issue_id

        # Log the failure first so anomaly checks see this occurrence.
        agent_row = self.db.get_agent(agent.agent_id)
        model = agent_row["model"] if agent_row else None
        self.db.log_event(
            issue_id,
            agent.agent_id,
            "incomplete",
            {"reason": result.reason, "summary": result.summary, "model": model},
        )

        decision = self._choose_escalation(issue_id)

        if decision == EscalationDecision.ANOMALY_ESCALATE:
            recent_failures = self.db.count_events_since_minutes(issue_id, "incomplete", Config.ANOMALY_WINDOW_MINUTES)
            logger.warning(f"Anomaly: {recent_failures} failures on {issue_id} in {Config.ANOMALY_WINDOW_MINUTES}m — auto-escalating")
            escalated = self.db.try_transition_issue_status(
                issue_id,
                from_status="in_progress",
                to_status="escalated",
                expected_assignee=agent.agent_id,
            )
            if not escalated:
                self.db.log_event(
                    issue_id,
                    agent.agent_id,
                    "anomaly_escalate_skipped",
                    {"reason": "issue not escalatable"},
                )
                return
            self.db.log_event(
                issue_id,
                agent.agent_id,
                "escalated",
                {
                    "reason": "Anomaly detection: rapid repeated failures",
                    "recent_failures": recent_failures,
                    "window_minutes": Config.ANOMALY_WINDOW_MINUTES,
                    "final_failure_reason": result.reason,
                },
            )
            return

        if decision == EscalationDecision.RETRY:
            retry_count = self.db.count_events_by_type(issue_id, "retry")
            released = self._release_issue(issue_id, expected_assignee=agent.agent_id)
            if not released:
                self.db.log_event(issue_id, agent.agent_id, "retry_skipped", {"reason": "issue not releasable"})
                return
            self.db.log_event(
                issue_id,
                agent.agent_id,
                "retry",
                {"retry_count": retry_count + 1, "reason": result.reason, "previous_agent": agent.name},
            )
            logger.info(f"Retrying issue {issue_id} (attempt {retry_count + 1}/{Config.MAX_RETRIES})")
            return

        if decision == EscalationDecision.AGENT_SWITCH:
            agent_switch_count = self.db.count_events_by_type(issue_id, "agent_switch")
            released = self._release_issue(issue_id, expected_assignee=agent.agent_id)
            if not released:
                self.db.log_event(issue_id, agent.agent_id, "agent_switch_skipped", {"reason": "issue not releasable"})
                return
            self.db.log_event(
                issue_id,
                agent.agent_id,
                "agent_switch",
                {"switch_count": agent_switch_count + 1, "reason": result.reason, "previous_agent": agent.name, "model": model},
            )
            logger.info(f"Switching agent for issue {issue_id} (switch {agent_switch_count + 1}/{Config.MAX_AGENT_SWITCHES})")
            return

        retry_count = self.db.count_events_by_type(issue_id, "retry")
        agent_switch_count = self.db.count_events_by_type(issue_id, "agent_switch")
        escalated = self.db.try_transition_issue_status(
            issue_id,
            from_status="in_progress",
            to_status="escalated",
            expected_assignee=agent.agent_id,
        )
        if not escalated:
            self.db.log_event(issue_id, agent.agent_id, "escalate_skipped", {"reason": "issue not escalatable"})
            return
        self.db.log_event(
            issue_id,
            agent.agent_id,
            "escalated",
            {
                "reason": "Exhausted all retry and agent switch attempts",
                "final_failure_reason": result.reason,
                "total_retries": retry_count,
                "total_agent_switches": agent_switch_count,
            },
        )
        logger.warning(f"Escalating issue {issue_id} to human intervention after {retry_count} retries and {agent_switch_count} agent switches")

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

    async def _check_backend_health(self) -> bool:
        """
        Check if OpenCode is healthy by listing sessions via the client.

        Reuses the existing OpenCodeClient (and its connection pool / auth)
        instead of creating a throwaway aiohttp.ClientSession per call.

        Returns:
            True if OpenCode is healthy, False otherwise
        """
        try:
            await self.backend.list_sessions()
            return True
        except Exception:
            return False

    def _is_backend_error(self, exception: Exception) -> bool:
        """
        Determine if an exception indicates OpenCode connectivity issues.

        Args:
            exception: Exception to examine

        Returns:
            True if this suggests OpenCode is unavailable
        """
        error_msg = str(exception).lower()

        # Common connectivity issues
        if any(
            phrase in error_msg
            for phrase in [
                "connection refused",
                "connection failed",
                "timeout",
                "server error",
                "network",
                "unreachable",
                "500",
                "502",
                "503",
                "504",
            ]
        ):
            return True

        # HTTP status code 5xx errors
        if hasattr(exception, "status") and hasattr(exception.status, "__ge__"):
            return exception.status >= 500

        return False

    async def _enter_degraded_mode(self, error_reason: str):
        """
        Enter degraded mode due to OpenCode unavailability.

        Args:
            error_reason: Description of the error that caused degraded mode
        """
        if self._backend_healthy:  # Only log the first time we enter degraded mode
            self._backend_healthy = False
            self._degraded_since = datetime.now()
            self._backoff_delay = 5  # Reset backoff

            self.db.log_system_event("backend_degraded", {"reason": error_reason, "timestamp": self._degraded_since.isoformat()})
            logger.warning(f"Entering degraded mode: {error_reason}")

    async def check_stalled_agents(self):
        """Check for stalled agents owned by THIS daemon and handle them.

        Only checks agents in self.active_agents (in-memory). This prevents
        a newly restarted daemon from interfering with stale DB rows left
        by a previous daemon instance.

        Now enhanced with session status inspection to avoid false positives
        from missed SSE events.
        """
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

        # For stalled agents, check OpenCode session status before handling
        for agent in stalled:
            await self._handle_stalled_with_session_check(agent)

    async def _handle_stalled_with_session_check(self, agent: AgentIdentity) -> StalledSessionCheckResult:
        """Handle stalled agent with OpenCode session status verification.

        Heartbeat-expiry policy:
        - if result file parses, treat as completion immediately;
        - else check session status once:
          - idle -> completion path
          - busy -> refresh heartbeat and continue monitoring
          - error/not_found -> stalled path
        """
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
            # Query OpenCode for actual session status
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
            # OpenCode API failure falls through to stalled path.
            self.db.log_event(agent.issue_id, agent.agent_id, "session_check_failed", {"error": str(e), "fallback": "handle_stalled_agent"})

        # Default fallback behavior
        await self.handle_stalled_agent(agent)
        return StalledSessionCheckResult.STOP_MONITORING

    def _on_merge_task_done(self, task: asyncio.Task):
        """Handle merge_processor_loop task completion/failure.

        If the task died unexpectedly and the orchestrator is still running,
        auto-restart the merge processor loop.
        """
        if task.cancelled():
            logger.info("Merge processor loop was cancelled")
            return

        exception = task.exception()
        if exception:
            logger.error(f"Merge processor loop died with exception: {exception}")
            if self.running:
                logger.info("Auto-restarting merge processor loop")
                new_task = asyncio.create_task(self.merge_processor_loop())
                new_task.add_done_callback(self._on_merge_task_done)

    async def merge_processor_loop(self):
        """
        Background loop to process the merge queue.

        Runs on MERGE_POLL_INTERVAL, processes one merge at a time.
        Includes periodic health checks for the refinery session.
        """
        health_check_counter = 0

        while self.running:
            try:
                if Config.MERGE_QUEUE_ENABLED:
                    await self.merge_pool.process_all()

                # Health check every 6 iterations (~60s at 10s poll interval)
                health_check_counter += 1
                if health_check_counter >= 6:
                    health_check_counter = 0
                    await self.merge_pool.health_check_all()

            except Exception as e:
                logger.error(f"Error in merge processor: {e}")
            await asyncio.sleep(Config.MERGE_POLL_INTERVAL)

    async def permission_unblocker_loop(self):
        """
        Safety-net loop to auto-resolve pending permission requests based on policy.

        Now runs at longer intervals since SSE events handle real-time permissions.
        This serves as a safety net for SSE reconnection gaps or edge cases.
        """
        while self.running:
            try:
                # Very slow if no active agents
                if len(self.active_agents) == 0:
                    await asyncio.sleep(30)
                    continue

                # Get pending permissions
                pending = await self.backend.get_pending_permissions()

                for perm in pending:
                    decision = self.evaluate_permission_policy(perm)
                    if decision:
                        # Auto-resolve based on policy
                        await self.backend.reply_permission(perm["id"], reply=decision)

                        self._log_permission_resolved(perm, decision)

                # Safety net only - SSE handles real-time permissions
                await asyncio.sleep(Config.PERMISSION_SAFETY_NET_INTERVAL)

            except Exception as e:
                logger.error(f"Error in permission unblocker: {e}")
                await asyncio.sleep(Config.PERMISSION_SAFETY_NET_INTERVAL)

    def evaluate_permission_policy(self, perm: Dict[str, Any]) -> Optional[str]:
        """
        Apply policy rules to decide allow/deny.

        Args:
            perm: Permission request dict from OpenCode

        Returns:
            "once", "always", or None if no rule matches
        """
        permission = perm.get("permission")

        # Session-level permissions handle most cases (set at session creation).
        # This catches runtime permission requests that slip through.

        # Workers should never ask questions or enter plan mode
        if permission in ("question", "plan_enter", "plan_exit"):
            return "reject"

        # Workers should never leave their worktree
        if permission == "external_directory":
            return "reject"

        # Allow standard tool usage within the session's directory scope
        if permission in ("read", "edit", "write", "bash"):
            return "once"

        # Unknown permission - let it block (human reviews)
        return None
