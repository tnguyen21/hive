"""Core orchestrator class for Hive multi-agent system."""

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Dict, List, Optional

from ..db import Database
from ..merge import MergeProcessorPool
from ..utils import AgentIdentity
from ..backends import HiveBackend

logger = logging.getLogger(__name__)


class OrchestratorCore:
    """Core orchestration engine for Hive."""

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
        - Phase 0: Fetch live sessions from the backend
        - Phase 1: Reconcile DB agents with status='working' (ghost + live)
        - Phase 2: Clean up orphan sessions (alive on server, no DB agent)
        - Phase 3: Purge idle/failed agents (leftovers from previous runs)
        """
        import hive.orchestrator as _mod

        Config = _mod.Config
        remove_worktree_async = _mod.remove_worktree_async

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
                    # Backend unreachable — best-effort abort/delete
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
            messages: List of session messages from the backend
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
        if hasattr(self.backend, "server_ready"):
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

        # Start merge queue processor in background
        merge_task = asyncio.create_task(self.merge_processor_loop())
        merge_task.add_done_callback(self._on_merge_task_done)

        try:
            # Run main loop
            await self.main_loop()
        finally:
            self.running = False
            # Abort all active backend sessions before shutting down
            await self._shutdown_all_sessions()
            self.backend.stop()
            # Cancel background tasks so we don't block on their long sleeps
            for task in (sse_task, merge_task):
                task.cancel()
            await asyncio.gather(sse_task, merge_task, return_exceptions=True)

    async def main_loop(self):
        """Main orchestration loop."""
        import hive.orchestrator as _mod

        Config = _mod.Config

        while self.running:
            try:
                if len(self.active_agents) + len(self._spawning_issues) < Config.MAX_AGENTS:
                    ready = self.db.get_ready_queue(project=None, limit=1)

                    if ready:
                        issue = ready[0]
                        if issue["id"] in self._spawning_issues:
                            await asyncio.sleep(1)
                            continue
                        try:
                            await self.spawn_worker(issue)
                        except Exception as e:
                            logger.error(f"Failed to spawn worker for {issue['id']}: {e}")
                    else:
                        await asyncio.sleep(Config.POLL_INTERVAL)
                else:
                    await asyncio.sleep(Config.POLL_INTERVAL)

                await self.check_stalled_agents()

            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                await asyncio.sleep(Config.POLL_INTERVAL)

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
        import hive.orchestrator as _mod

        Config = _mod.Config

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

    def evaluate_permission_policy(self, perm: Dict[str, Any]) -> Optional[str]:
        """
        Apply policy rules to decide allow/deny.

        Args:
            perm: Permission request dict from the backend

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

    async def _cleanup_session(self, agent: AgentIdentity):
        """Abort and delete an agent's backend session.

        Called after agent completion or failure to ensure the session
        does not linger and consume tokens.
        """
        logger.info(f"Cleaning up session {agent.session_id} (agent={agent.agent_id}, issue={agent.issue_id}, worktree={agent.worktree})")
        await self.backend.cleanup_session(agent.session_id, directory=agent.worktree)

    async def _teardown_agent(self, agent: AgentIdentity, *, remove_worktree: bool = False):
        """Best-effort cleanup for session, in-memory registration, worktree, and DB state.

        Always marks the agent row as terminal ('failed') so stale 'working'
        rows don't accumulate across retries.  The merge processor stores its
        own copy of worktree/branch in the merge_queue table, so clearing the
        agent row's references is safe.
        """
        import hive.orchestrator as _mod

        await self._best_effort_cleanup("cleanup_session", self._cleanup_session(agent))

        if agent.agent_id in self.active_agents:
            self._unregister_agent(agent.agent_id)

        # Mark agent as terminal in DB — prevents ghost 'working' rows from
        # accumulating when agents are retried / agent-switched.
        self._mark_agent_failed(agent.agent_id)

        if remove_worktree and agent.worktree:
            await self._best_effort_cleanup("remove_worktree", _mod.remove_worktree_async(agent.worktree))
