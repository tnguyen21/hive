"""Main orchestrator for Hive multi-agent system."""

import asyncio
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import Config, WORKER_PERMISSIONS
from .db import Database
from .git import create_worktree_async, get_commit_hash, has_diff_from_main_async, remove_worktree_async
from .merge import MergeProcessor
from .utils import generate_id, AgentIdentity, CompletionResult
from .backends import OpenCodeClient, make_model_config
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
)
from .backends import SSEClient

logger = logging.getLogger(__name__)


class CompletionTransition(str, Enum):
    """Transition outcomes for completion handling."""

    SKIP_TERMINAL_ISSUE = "skip_terminal_issue"
    FAIL_BUDGET = "fail_budget"
    FAIL_ASSESSMENT = "fail_assessment"
    FAIL_VALIDATION_NO_DIFF = "fail_validation_no_diff"
    SUCCESS_DONE = "success_done"
    SUCCESS_CYCLE_NEXT_STEP = "success_cycle_next_step"
    ERROR_COMPLETION_HANDLER = "error_completion_handler"


class PostAction(str, Enum):
    """Post-transition action for completion handler."""

    TEARDOWN = "teardown"
    CONTINUE_AGENT = "continue_agent"


class EscalationDecision(str, Enum):
    """Escalation routing decision after a failure."""

    RETRY = "retry"
    AGENT_SWITCH = "agent_switch"
    ESCALATE = "escalate"
    ANOMALY_ESCALATE = "anomaly_escalate"


@dataclass
class CompletionDecision:
    """Decision payload from completion transition analysis."""

    transition: CompletionTransition
    result: Optional[CompletionResult] = None
    terminal_status: Optional[str] = None
    budget_tokens: Optional[int] = None
    validation_original_summary: Optional[str] = None
    next_step: Optional[Dict[str, Any]] = None


class Orchestrator:
    """Main orchestration engine for Hive."""

    def __init__(
        self,
        db: Database,
        opencode_client: OpenCodeClient,
        project_path: str,
        project_name: str = "default",
        sse_client: Optional[SSEClient] = None,
    ):
        """
        Initialize orchestrator.

        Args:
            db: Database instance
            opencode_client: OpenCode HTTP client (or ClaudeWSBackend)
            project_path: Path to the project repository
            project_name: Name of the project
            sse_client: Optional SSE client override (e.g. ClaudeWSBackend serves both roles)
        """
        self.db = db
        self.opencode = opencode_client
        self.project_path = Path(project_path).resolve()
        self.project_name = project_name

        # Merge processor
        self.merge_processor = MergeProcessor(
            db=db,
            opencode=opencode_client,
            project_path=project_path,
            project_name=project_name,
        )

        # Track active agents
        self.active_agents: Dict[str, AgentIdentity] = {}

        # Reverse lookup maps for O(1) session_id and issue_id lookups
        self._session_to_agent: dict[str, str] = {}  # session_id -> agent_id
        self._issue_to_agent: dict[str, str] = {}  # issue_id -> agent_id

        # SSE client for event monitoring
        self.sse_client = sse_client or SSEClient(
            base_url=Config.OPENCODE_URL,
            password=Config.OPENCODE_PASSWORD,
            global_events=True,
        )

        # Event handlers
        self.session_status_events: Dict[str, asyncio.Event] = {}

        # Track last SSE activity per session for lease renewal
        self._session_last_activity: Dict[str, datetime] = {}

        # Running flag
        self.running = False

        # Guard against double-handling: tracks agent_ids currently being
        # processed by handle_agent_complete or handle_stalled_agent. Prevents
        # interleaving across await points from causing duplicate processing.
        self._handling_agents: set[str] = set()

        # Degraded mode state
        self._opencode_healthy = True
        self._degraded_since: Optional[datetime] = None
        self._backoff_delay = 5  # Initial backoff delay in seconds

        # Cost guardrails
        self._budget_paused = False

    def _setup_sse_handlers(self):
        """Set up SSE event handlers."""

        async def handle_session_status(properties):
            session_id = properties.get("sessionID")
            status = properties.get("status", {})

            # Any session activity = renew the lease for the associated agent
            self._renew_lease_for_session(session_id)

            # If session becomes idle, signal completion
            if status.get("type") == "idle" and session_id in self.session_status_events:
                self.session_status_events[session_id].set()

        self.sse_client.on("session.status", handle_session_status)

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

        self.sse_client.on("session.error", handle_session_error)

        # Register permission event handler
        self.sse_client.on("permission.request", self._handle_permission_event)

    async def _handle_permission_event(self, event_data: dict):
        """Handle permission request from SSE event — resolve immediately."""
        try:
            perm_id = event_data.get("id")
            if not perm_id:
                # If SSE event doesn't include full permission data,
                # fetch pending permissions and resolve
                pending = await self.opencode.get_pending_permissions()
                for perm in pending:
                    decision = self.evaluate_permission_policy(perm)
                    if decision:
                        await self.opencode.reply_permission(perm["id"], reply=decision)
                        self._log_permission_resolved(perm, decision)
                return

            decision = self.evaluate_permission_policy(event_data)
            if decision:
                await self.opencode.reply_permission(perm_id, reply=decision)
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

    def _renew_lease_for_session(self, session_id: str):
        """Renew the lease for the agent associated with a session.

        Called on any SSE activity from the session, proving the worker
        is still alive and making progress. Uses LEASE_EXTENSION (not
        LEASE_DURATION) so renewals grant a shorter window than the
        initial lease.
        """
        now = datetime.now()
        self._session_last_activity[session_id] = now

        # Find agent for this session and extend its DB lease
        agent_id = self._session_to_agent.get(session_id)
        if agent_id and agent_id in self.active_agents:
            agent = self.active_agents[agent_id]
            try:
                self.db.conn.execute(
                    """
                    UPDATE agents
                    SET lease_expires_at = datetime('now', '+{} seconds'),
                        last_progress_at = datetime('now')
                    WHERE id = ?
                    """.format(Config.LEASE_EXTENSION),
                    (agent.agent_id,),
                )
                self.db.conn.commit()
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
            sessions = await self.opencode.list_sessions()
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
                        await self.opencode.cleanup_session(session_id, directory=worktree)
                        live_session_ids.discard(session_id)
                    else:
                        # Ghost agent — session already gone, just log
                        logger.info(f"Agent {agent_id} is a ghost (session {session_id} no longer exists)")
                else:
                    # OpenCode unreachable — best-effort abort/delete
                    await self.opencode.cleanup_session(session_id, directory=worktree)

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
                    self.db.conn.execute(
                        """
                        UPDATE issues
                        SET assignee = NULL, status = 'open'
                        WHERE id = ? AND status = 'in_progress'
                        """,
                        (issue_id,),
                    )
                    self.db.log_event(
                        issue_id,
                        agent_id,
                        "reconciled",
                        {"reason": "stale agent from previous daemon run"},
                    )
                else:
                    self.db.conn.execute(
                        """
                        UPDATE issues
                        SET assignee = NULL, status = 'failed'
                        WHERE id = ? AND status = 'in_progress'
                        """,
                        (issue_id,),
                    )
                    self.db.log_event(
                        issue_id,
                        agent_id,
                        "reconciled",
                        {"reason": "stale agent, retry budget exhausted — marking failed"},
                    )

            # Clean up worktree (in executor to avoid blocking event loop)
            if worktree:
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
                    await self.opencode.cleanup_session(session_id)

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
        self._setup_sse_handlers()

        # Start SSE/WS server in background FIRST — other init steps may need
        # to create sessions (e.g. eager refinery), which requires the server.
        sse_task = asyncio.create_task(self.sse_client.connect_with_reconnect())

        # If the backend has a server_ready gate, wait for it before proceeding
        if hasattr(self.sse_client, "server_ready"):
            await self.sse_client.server_ready.wait()

        await self._reconcile_stale_agents()

        # Initialize merge processor (eager refinery session creation)
        await self.merge_processor.initialize()

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
            self.sse_client.stop()
            # Cancel background tasks so we don't block on their long sleeps
            for task in (sse_task, permission_task, merge_task):
                task.cancel()
            await asyncio.gather(sse_task, permission_task, merge_task, return_exceptions=True)

    async def main_loop(self):
        """Main orchestration loop."""
        while self.running:
            try:
                # Check if OpenCode is healthy before scheduling work
                if not self._opencode_healthy:
                    # In degraded mode - check health with exponential backoff
                    healthy = await self._check_opencode_health()

                    if healthy:
                        # OpenCode recovered
                        degraded_duration = (datetime.now() - self._degraded_since).total_seconds() if self._degraded_since else 0
                        self.db.log_system_event(
                            "opencode_recovered", {"degraded_duration_seconds": degraded_duration, "backoff_delay": self._backoff_delay}
                        )
                        self._opencode_healthy = True
                        self._degraded_since = None
                        self._backoff_delay = 5  # Reset backoff
                        logger.info(f"Backend recovered after {degraded_duration:.1f}s degraded mode")
                    else:
                        # Still unhealthy - wait with exponential backoff
                        await asyncio.sleep(self._backoff_delay)
                        self._backoff_delay = min(60, self._backoff_delay * 2)  # Cap at 60 seconds
                        continue  # Skip scheduling and stall checks

                # Per-run budget cap — stop spawning if total token spend exceeds limit
                if Config.MAX_TOKENS_PER_RUN:
                    run_tokens = self.db.get_run_token_total()
                    if run_tokens > Config.MAX_TOKENS_PER_RUN:
                        if not self._budget_paused:
                            logger.warning(f"Run budget exceeded ({run_tokens} tokens). Pausing new spawns.")
                            self._budget_paused = True
                            self.db.log_system_event("budget_paused", {"total_tokens": run_tokens})
                        await asyncio.sleep(Config.POLL_INTERVAL)
                        # Still check for stalled agents even when budget-paused
                        if self._opencode_healthy:
                            await self.check_stalled_agents()
                        continue

                # Normal operation - check if we can spawn more agents
                if len(self.active_agents) < Config.MAX_AGENTS:
                    # Get ready work
                    ready = self.db.get_ready_queue(project=self.project_name, limit=1)

                    if ready:
                        issue = ready[0]
                        try:
                            # Try to claim and spawn worker
                            await self.spawn_worker(issue)
                        except Exception as e:
                            # Check if the error suggests OpenCode is unhealthy
                            if self._is_opencode_error(e):
                                await self._enter_degraded_mode(str(e))
                    else:
                        # No ready work, wait before polling again
                        await asyncio.sleep(Config.POLL_INTERVAL)
                else:
                    # At capacity, wait
                    await asyncio.sleep(Config.POLL_INTERVAL)

                # Check for stalled agents (only when healthy)
                if self._opencode_healthy:
                    await self.check_stalled_agents()

            except Exception as e:
                # Check if this is an OpenCode connectivity issue
                if self._is_opencode_error(e):
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
        agent_name = generate_id("worker")
        model = issue.get("model") or Config.WORKER_MODEL or Config.DEFAULT_MODEL

        # Create agent identity in database
        agent_id = self.db.create_agent(
            name=agent_name,
            model=model,
            metadata={"issue_id": issue_id},
            project=self.project_name,
        )

        # Create git worktree (in executor to avoid blocking event loop)
        try:
            worktree_path = await create_worktree_async(str(self.project_path), agent_name)
        except Exception as e:
            self.db.log_event(
                issue_id,
                agent_id,
                "worktree_error",
                {"error": str(e)},
            )
            # Delete the orphaned agent
            self.db.conn.execute("PRAGMA foreign_keys = OFF")
            self.db.conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
            self.db.conn.execute("PRAGMA foreign_keys = ON")
            self.db.conn.commit()
            return

        # Atomic claim
        claimed = self.db.claim_issue(issue_id, agent_id)
        if not claimed:
            # Someone else claimed it first, clean up worktree and delete agent
            await remove_worktree_async(worktree_path)
            self.db.conn.execute("PRAGMA foreign_keys = OFF")
            self.db.conn.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
            self.db.conn.execute("PRAGMA foreign_keys = ON")
            self.db.conn.commit()
            return

        # Create OpenCode session
        session_id = None  # Track for cleanup on failure
        try:
            session = await self.opencode.create_session(
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
                    lease_expires_at = datetime('now', '+{} seconds'),
                    last_progress_at = datetime('now')
                WHERE id = ?
                """.format(Config.LEASE_DURATION),
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
            )
            self.active_agents[agent_id] = agent

            # Populate reverse lookup maps
            self._session_to_agent[agent.session_id] = agent_id
            self._issue_to_agent[agent.issue_id] = agent_id

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
                {"error": str(e)},
            )
            # Clean up the OpenCode session if it was created (best-effort —
            # don't let cleanup failure prevent DB/worktree cleanup below)
            if session_id:
                try:
                    await self.opencode.cleanup_session(session_id, directory=worktree_path)
                except Exception:
                    pass
            # Clean up in-memory tracking (if agent was registered)
            if agent_id in self.active_agents:
                self._unregister_agent(agent_id)
            # Mark agent as failed in DB
            self._mark_agent_failed(agent_id)
            # Clean up worktree and mark issue failed
            await remove_worktree_async(worktree_path)
            self.db.update_issue_status(issue_id, "failed")

    def _gather_notes_for_worker(self, issue_id: str) -> Optional[List[Dict[str, Any]]]:
        """Gather relevant notes to inject into a worker's prompt.

        Combines molecule-specific notes (if the issue is a step) with
        recent project-wide notes, deduplicating by note ID.

        Returns None if no notes are found (so build_worker_prompt skips the section).
        """
        seen_ids: set = set()
        notes: List[Dict[str, Any]] = []

        # Get molecule-scoped notes if this is a step
        issue = self.db.get_issue(issue_id)
        if issue and issue.get("parent_id"):
            for note in self.db.get_notes_for_molecule(issue["parent_id"]):
                if note["id"] not in seen_ids:
                    seen_ids.add(note["id"])
                    notes.append(note)

        # Get recent project-wide notes
        for note in self.db.get_recent_project_notes(project=self.project_name, limit=10):
            if note["id"] not in seen_ids:
                seen_ids.add(note["id"])
                notes.append(note)

        return notes if notes else None

    def _is_issue_canceled(self, issue_id: str) -> bool:
        """Check if an issue has been canceled in the database."""
        try:
            issue = self.db.get_issue(issue_id)
            return issue is not None and issue.get("status") == "canceled"
        except Exception:
            return False

    async def _poll_session_idle(self, agent: AgentIdentity) -> bool:
        """Poll opencode to check if a session has gone idle.

        Fallback for when SSE events are missed (e.g., reconnect gap).
        Returns True if the session is idle, False otherwise.
        """
        try:
            status = await self.opencode.get_session_status(agent.session_id, directory=agent.worktree)
            return status is not None and status.get("type") == "idle"
        except Exception:
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
        # Snapshot the session_id we're monitoring. The agent object may be
        # mutated by cycle_agent_to_next_step during molecule processing,
        # and we must clean up OUR session's event, not the new one.
        my_session_id = agent.session_id
        try:
            event = self.session_status_events.get(my_session_id)
            if not event:
                return

            # Record initial activity
            self._session_last_activity[my_session_id] = datetime.now()

            # Poll loop: check for completion or inactivity
            check_interval = min(30, Config.LEASE_DURATION // 4)
            while True:
                try:
                    await asyncio.wait_for(event.wait(), timeout=check_interval)
                    # Event was set — could be idle (done) or canceled
                    # Check if canceled before assessing completion
                    if self._is_issue_canceled(agent.issue_id):
                        # Issue was canceled while agent was working.
                        # cancel_agent_for_issue already handled cleanup + set the event.
                        return
                    break
                except asyncio.TimeoutError:
                    # Check if the issue was canceled
                    if self._is_issue_canceled(agent.issue_id):
                        await self.cancel_agent_for_issue(agent.issue_id)
                        return

                    # Polling fallback: directly check if the session went idle.
                    # This catches cases where the SSE event was missed.
                    if await self._poll_session_idle(agent):
                        break

                    # Check if there's been recent activity
                    last_activity = self._session_last_activity.get(my_session_id, datetime.now())
                    elapsed = (datetime.now() - last_activity).total_seconds()

                    if elapsed > Config.LEASE_DURATION:
                        # No activity for full lease duration — truly stalled
                        await self.handle_stalled_agent(agent)
                        return
                    # Otherwise, keep waiting — worker is active

            # AFTER idle detected, read result file for structured data
            file_result = read_result_file(agent.worktree)

            # Agent finished, assess completion
            await self.handle_agent_complete(agent, file_result=file_result)

        except Exception as e:
            self.db.log_event(
                agent.issue_id,
                agent.agent_id,
                "monitor_error",
                {"error": str(e)},
            )
        finally:
            # Clean up using the snapshotted session_id, not agent.session_id
            # which may have been mutated by cycle_agent_to_next_step.
            if my_session_id in self.session_status_events:
                del self.session_status_events[my_session_id]
            self._session_last_activity.pop(my_session_id, None)

    async def _cleanup_session(self, agent: AgentIdentity):
        """Abort and delete an agent's opencode session.

        Called after agent completion or failure to ensure the session
        does not linger and consume tokens.
        """
        await self.opencode.cleanup_session(agent.session_id, directory=agent.worktree)

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

    def _release_issue(self, issue_id: str):
        """Release an issue back to the open queue."""
        self.db.update_issue_status(issue_id, "open")
        self.db.conn.execute("UPDATE issues SET assignee = NULL WHERE id = ?", (issue_id,))
        self.db.conn.commit()

    async def _teardown_agent(self, agent: AgentIdentity, *, remove_worktree: bool = False):
        """Best-effort cleanup for session, in-memory registration, and worktree."""
        try:
            await self._cleanup_session(agent)
        except Exception:
            pass

        if agent.agent_id in self.active_agents:
            self._unregister_agent(agent.agent_id)

        if remove_worktree and agent.worktree:
            try:
                await remove_worktree_async(agent.worktree)
            except Exception:
                pass

    @contextmanager
    def _agent_handling_scope(self, agent: AgentIdentity, *, handler_name: str):
        """Guard against double-handling and ensure membership cleanup."""
        if agent.agent_id not in self.active_agents:
            logger.debug(f"Skipping {handler_name} for {agent.name} — already removed from active agents")
            yield False
            return

        if agent.agent_id in self._handling_agents:
            logger.debug(f"Skipping {handler_name} for {agent.name} — already being handled")
            yield False
            return

        self._handling_agents.add(agent.agent_id)
        try:
            yield True
        finally:
            self._handling_agents.discard(agent.agent_id)

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
        """Shared prompt + dispatch flow for spawn and molecule cycling."""
        issue_id = issue["id"]

        worker_notes = self._gather_notes_for_worker(issue_id)
        if worker_notes:
            self.db.log_event(issue_id, agent.agent_id, "notes_injected", {"count": len(worker_notes)})

        retry_context = build_retry_context(self.db, issue_id)
        branch_name = f"agent/{agent.name}"
        prompt = build_worker_prompt(
            agent_name=agent.name,
            issue=issue,
            worktree_path=agent.worktree,
            branch_name=branch_name,
            project=self.project_name,
            notes=worker_notes,
            completed_steps=completed_steps,
            retry_context=retry_context,
        )

        system_prompt = build_system_prompt(
            project=self.project_name,
            agent_name=agent.name,
            worktree_path=agent.worktree,
        )

        self.session_status_events[agent.session_id] = asyncio.Event()

        await self.opencode.send_message_async(
            agent.session_id,
            parts=[{"type": "text", "text": prompt}],
            model=make_model_config(model),
            system=system_prompt,
            directory=agent.worktree,
        )

        self.db.log_event(issue_id, agent.agent_id, started_event_type, started_event_detail)
        asyncio.create_task(self.monitor_agent(agent))

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

        messages = await self.opencode.get_messages(agent.session_id, directory=agent.worktree)
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

        issue = self.db.get_issue(agent.issue_id)
        if issue and issue.get("parent_id"):
            next_step = self.db.get_next_ready_step(issue["parent_id"])
            if next_step:
                return CompletionDecision(
                    transition=CompletionTransition.SUCCESS_CYCLE_NEXT_STEP,
                    result=result,
                    next_step=next_step,
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
    # - SUCCESS_CYCLE_NEXT_STEP  -> SUCCESS_DONE effects + cycle agent + skip teardown
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
        with self._agent_handling_scope(agent, handler_name="completion handling") as should_handle:
            if not should_handle:
                return

            post_action = PostAction.TEARDOWN
            decision: Optional[CompletionDecision] = None

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
                                project=self.project_name,
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
                        self.db.log_event(
                            agent.issue_id,
                            agent.agent_id,
                            "agent_complete_skipped",
                            {"reason": f"issue already {status}, cleaning up session"},
                        )

                    case CompletionTransition.FAIL_BUDGET:
                        self.db.log_event(
                            agent.issue_id,
                            agent.agent_id,
                            "budget_exceeded",
                            {"issue_tokens": decision.budget_tokens, "limit": Config.MAX_TOKENS_PER_ISSUE},
                        )
                        if decision.result is not None:
                            await self._handle_agent_failure(agent, decision.result)

                    case CompletionTransition.FAIL_VALIDATION_NO_DIFF:
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
                        if decision.result is not None:
                            await self._handle_agent_failure(agent, decision.result)

                    case CompletionTransition.SUCCESS_DONE | CompletionTransition.SUCCESS_CYCLE_NEXT_STEP:
                        if decision.result is None:
                            raise RuntimeError("Missing completion result for success transition")

                        # Mark issue as done.
                        self.db.update_issue_status(agent.issue_id, "done")

                        # Get commit hash if available.
                        commit_hash = decision.result.git_commit or get_commit_hash(agent.worktree)

                        # Extract test_command from worker's file_result.
                        test_command = file_result.get("test_command") if file_result else None

                        # Enqueue to merge queue.
                        self.db.conn.execute(
                            """
                            INSERT INTO merge_queue (issue_id, agent_id, project, worktree, branch_name, test_command)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                agent.issue_id,
                                agent.agent_id,
                                self.project_name,
                                agent.worktree,
                                f"agent/{agent.name}",
                                test_command,
                            ),
                        )
                        self.db.conn.commit()

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

                        if decision.transition == CompletionTransition.SUCCESS_CYCLE_NEXT_STEP:
                            if decision.next_step is None:
                                raise RuntimeError("Missing next step for cycle transition")
                            await self.cycle_agent_to_next_step(agent, decision.next_step)
                            if agent.agent_id in self.active_agents:
                                post_action = PostAction.CONTINUE_AGENT

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
                if post_action == PostAction.TEARDOWN:
                    await self._teardown_agent(agent)

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
            self.db.update_issue_status(issue_id, "escalated")
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
            self._release_issue(issue_id)
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
            self._release_issue(issue_id)
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
        self.db.update_issue_status(issue_id, "escalated")
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

    async def cycle_agent_to_next_step(self, agent: AgentIdentity, next_step: Dict[str, Any]):
        """
        Cycle an agent to the next step in a molecule.

        Args:
            agent: Current agent identity
            next_step: Next step issue dict
        """
        # Abort and delete current session (not just abort — delete prevents leaked sessions)
        await self.opencode.cleanup_session(agent.session_id, directory=agent.worktree)

        # Claim the next step
        claimed = self.db.claim_issue(next_step["id"], agent.agent_id)
        if not claimed:
            # Someone else claimed it, release agent
            if agent.agent_id in self.active_agents:
                self._unregister_agent(agent.agent_id)
            return

        # Resolve model for the next step: next_step.model > Config.WORKER_MODEL > Config.DEFAULT_MODEL
        model = next_step.get("model") or Config.WORKER_MODEL or Config.DEFAULT_MODEL

        # Create new session (same worktree)
        new_session_id = None  # Track for cleanup on failure
        try:
            session = await self.opencode.create_session(
                directory=agent.worktree,
                title=f"{agent.name}: {next_step['title']}",
                permissions=WORKER_PERMISSIONS,
            )
            new_session_id = session["id"]

            # Update agent
            self.db.conn.execute(
                """
                UPDATE agents
                SET session_id = ?,
                    current_issue = ?,
                    lease_expires_at = datetime('now', '+{} seconds'),
                    last_progress_at = datetime('now')
                WHERE id = ?
                """.format(Config.LEASE_DURATION),
                (new_session_id, next_step["id"], agent.agent_id),
            )
            self.db.conn.commit()

            # Update agent identity and reverse lookup maps
            old_session_id = agent.session_id
            old_issue_id = agent.issue_id

            # Remove old mappings
            self._session_to_agent.pop(old_session_id, None)
            self._issue_to_agent.pop(old_issue_id, None)

            # Update agent identity
            agent.session_id = new_session_id
            agent.issue_id = next_step["id"]

            # Add new mappings
            self._session_to_agent[agent.session_id] = agent.agent_id
            self._issue_to_agent[agent.issue_id] = agent.agent_id

            # Gather completed steps for context
            completed_steps = None
            if next_step.get("parent_id"):
                completed_issues = self.db.get_completed_molecule_steps(next_step["parent_id"])
                completed_steps = [f"{s['title']}: {(s.get('description') or '')[:100]}" for s in completed_issues]

            await self._dispatch_worker_to_issue(
                agent=agent,
                issue=next_step,
                model=model,
                completed_steps=completed_steps,
                started_event_type="session_cycled",
                started_event_detail={
                    "new_session_id": new_session_id,
                    "step_title": next_step["title"],
                    "prompt_version": get_prompt_version("worker"),
                },
            )

        except Exception as e:
            self.db.log_event(
                next_step["id"],
                agent.agent_id,
                "session_cycle_error",
                {"error": str(e)},
            )
            # Clean up the new session if it was created (best-effort —
            # don't let cleanup failure prevent _unregister_agent below)
            if new_session_id:
                try:
                    await self.opencode.cleanup_session(new_session_id, directory=agent.worktree)
                except Exception:
                    pass
            # Release agent
            if agent.agent_id in self.active_agents:
                self._unregister_agent(agent.agent_id)

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

        with self._agent_handling_scope(agent, handler_name="stall handling") as should_handle:
            if not should_handle:
                return

            stalled_transition = "FAIL_STALLED_TERMINAL"
            try:
                self.db.log_event(
                    agent.issue_id,
                    agent.agent_id,
                    "stalled",
                    {"lease_expired": True},
                )

                # Mark agent as failed so it's not picked up again
                self._mark_agent_failed(agent.agent_id)

                # Route through escalation chain (retry → agent_switch → escalate)
                # instead of unconditionally resetting to open, which caused an
                # infinite spawn loop for issues whose workers always stall.
                current_issue = self.db.get_issue(agent.issue_id)
                if current_issue and current_issue.get("status") == "in_progress":
                    stalled_transition = "FAIL_STALLED_IN_PROGRESS"
                    stall_result = CompletionResult(
                        success=False,
                        reason="Agent stalled (lease expired, no activity)",
                        summary="Worker became unresponsive",
                    )
                    await self._handle_agent_failure(agent, stall_result)
            finally:
                logger.debug(f"Stall transition for {agent.name}: {stalled_transition}")
                await self._teardown_agent(agent, remove_worktree=True)

    async def _check_opencode_health(self) -> bool:
        """
        Check if OpenCode is healthy by listing sessions via the client.

        Reuses the existing OpenCodeClient (and its connection pool / auth)
        instead of creating a throwaway aiohttp.ClientSession per call.

        Returns:
            True if OpenCode is healthy, False otherwise
        """
        try:
            await self.opencode.list_sessions()
            return True
        except Exception:
            return False

    def _is_opencode_error(self, exception: Exception) -> bool:
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
        if self._opencode_healthy:  # Only log the first time we enter degraded mode
            self._opencode_healthy = False
            self._degraded_since = datetime.now()
            self._backoff_delay = 5  # Reset backoff

            self.db.log_system_event("opencode_degraded", {"reason": error_reason, "timestamp": self._degraded_since.isoformat()})
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

        # Check each active agent against the DB lease
        stalled = []
        for agent_id, agent in list(self.active_agents.items()):
            try:
                cursor = self.db.conn.execute(
                    """
                    SELECT lease_expires_at
                    FROM agents
                    WHERE id = ? AND status = 'working'
                      AND lease_expires_at < datetime('now')
                    """,
                    (agent_id,),
                )
                row = cursor.fetchone()
                if row:
                    stalled.append(agent)
            except Exception:
                pass

        # For stalled agents, check OpenCode session status before handling
        for agent in stalled:
            await self._handle_stalled_with_session_check(agent)

    async def _handle_stalled_with_session_check(self, agent: AgentIdentity):
        """Handle stalled agent with OpenCode session status verification.

        Checks if the session is actually idle (completion missed due to SSE failure)
        or busy (false positive, extend lease) before falling back to handle_stalled_agent.
        """
        try:
            # Query OpenCode for actual session status
            status = await self.opencode.get_session_status(agent.session_id, directory=agent.worktree)

            if status["type"] == "idle":
                # Session finished but we missed the SSE completion event
                self.db.log_event(agent.issue_id, agent.agent_id, "missed_completion", {"session_status": "idle", "reason": "sse_missed"})
                await self.handle_agent_complete(agent)
                return

            elif status["type"] == "busy":
                # Session still active - check if we've already extended the lease
                cursor = self.db.conn.execute(
                    """
                    SELECT 1 FROM events 
                    WHERE agent_id = ? AND event_type = 'lease_extended'
                    AND created_at > datetime('now', '-{} seconds')
                    """.format(Config.LEASE_DURATION),
                    (agent.agent_id,),
                )

                if cursor.fetchone():
                    # Already extended lease in current period - treat as truly stalled
                    await self.handle_stalled_agent(agent)
                else:
                    # First extension - extend the lease
                    self.db.conn.execute(
                        "UPDATE agents SET lease_expires_at = datetime('now', '+{} seconds') WHERE id = ?".format(Config.LEASE_DURATION),
                        (agent.agent_id,),
                    )
                    self.db.conn.commit()

                    self.db.log_event(
                        agent.issue_id,
                        agent.agent_id,
                        "lease_extended",
                        {"session_status": "busy", "new_expires_at": "now+{}s".format(Config.LEASE_DURATION)},
                    )
                return

        except Exception as e:
            # OpenCode API failure - fall back to original behavior
            self.db.log_event(agent.issue_id, agent.agent_id, "session_check_failed", {"error": str(e), "fallback": "handle_stalled_agent"})

        # Default fallback behavior
        await self.handle_stalled_agent(agent)

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
                    await self.merge_processor.process_queue_once()

                # Health check every 6 iterations (~60s at 10s poll interval)
                health_check_counter += 1
                if health_check_counter >= 6:
                    health_check_counter = 0
                    await self.merge_processor.health_check()

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
                pending = await self.opencode.get_pending_permissions()

                for perm in pending:
                    decision = self.evaluate_permission_policy(perm)
                    if decision:
                        # Auto-resolve based on policy
                        await self.opencode.reply_permission(perm["id"], reply=decision)

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
