"""Main orchestrator for Hive multi-agent system."""

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from .config import Config
from .db import Database
from .git import create_worktree, get_commit_hash, remove_worktree
from .ids import generate_id
from .models import AgentIdentity, CompletionResult, WorkPlan
from .opencode import OpenCodeClient
from .prompts import (
    assess_completion,
    build_mayor_prompt,
    build_mayor_state_summary,
    build_system_prompt,
    build_worker_prompt,
    parse_work_plan,
)
from .sse import SSEClient


class Orchestrator:
    """Main orchestration engine for Hive."""

    def __init__(
        self,
        db: Database,
        opencode_client: OpenCodeClient,
        project_path: str,
        project_name: str = "default",
    ):
        """
        Initialize orchestrator.

        Args:
            db: Database instance
            opencode_client: OpenCode HTTP client
            project_path: Path to the project repository
            project_name: Name of the project
        """
        self.db = db
        self.opencode = opencode_client
        self.project_path = Path(project_path).resolve()
        self.project_name = project_name

        # Track active agents
        self.active_agents: Dict[str, AgentIdentity] = {}

        # Mayor session
        self.mayor_session_id: Optional[str] = None

        # SSE client for event monitoring
        self.sse_client = SSEClient(
            base_url=Config.OPENCODE_URL,
            password=Config.OPENCODE_PASSWORD,
            global_events=True,
        )

        # Event handlers
        self.session_status_events: Dict[str, asyncio.Event] = {}

        # Running flag
        self.running = False

    def _setup_sse_handlers(self):
        """Set up SSE event handlers."""

        async def handle_session_status(properties):
            session_id = properties.get("sessionID")
            status = properties.get("status", {})

            # If session becomes idle, signal completion
            if status.get("type") == "idle" and session_id in self.session_status_events:
                self.session_status_events[session_id].set()

        self.sse_client.on("session.status", handle_session_status)

    async def start(self):
        """Start the orchestrator."""
        self.running = True
        self._setup_sse_handlers()

        # Create Mayor session
        await self.create_mayor_session()

        # Start SSE event consumer in background
        sse_task = asyncio.create_task(self.sse_client.connect_with_reconnect())

        try:
            # Run main loop
            await self.main_loop()
        finally:
            self.running = False
            self.sse_client.stop()
            await sse_task

    async def main_loop(self):
        """Main orchestration loop."""
        while self.running:
            try:
                # Check if we can spawn more agents
                if len(self.active_agents) < Config.MAX_AGENTS:
                    # Get ready work
                    ready = self.db.get_ready_queue(limit=1)

                    if ready:
                        issue = ready[0]
                        # Try to claim and spawn worker
                        await self.spawn_worker(issue)
                    else:
                        # No ready work, wait before polling again
                        await asyncio.sleep(Config.POLL_INTERVAL)
                else:
                    # At capacity, wait
                    await asyncio.sleep(Config.POLL_INTERVAL)

                # Check for stalled agents
                await self.check_stalled_agents()

            except Exception as e:
                print(f"Error in main loop: {e}")
                await asyncio.sleep(Config.POLL_INTERVAL)

    async def create_mayor_session(self) -> str:
        """
        Create the Mayor's persistent OpenCode session.

        Returns:
            Session ID
        """
        session = await self.opencode.create_session(
            directory=str(self.project_path),
            title="mayor",
            permissions=[
                # Mayor can read files to understand the codebase
                {"permission": "read", "pattern": "*", "action": "allow"},
                # Mayor can run non-destructive commands
                {"permission": "bash", "pattern": "git *", "action": "allow"},
                {"permission": "bash", "pattern": "ls *", "action": "allow"},
                {"permission": "bash", "pattern": "find *", "action": "allow"},
                # Mayor should NOT edit files or run tests
                {"permission": "edit", "pattern": "*", "action": "deny"},
                {"permission": "write", "pattern": "*", "action": "deny"},
                # No interactive questions
                {"permission": "question", "pattern": "*", "action": "deny"},
                {"permission": "plan_enter", "pattern": "*", "action": "deny"},
            ],
        )

        self.mayor_session_id = session["id"]
        self.db.log_event(None, None, "mayor_session_created", {"session_id": self.mayor_session_id})
        return self.mayor_session_id

    async def send_to_mayor(self, message: str) -> Optional[WorkPlan]:
        """
        Send a message to the Mayor session and wait for response.

        Args:
            message: Message to send to Mayor

        Returns:
            Parsed WorkPlan, or None if no work plan in response
        """
        if not self.mayor_session_id:
            raise RuntimeError("Mayor session not created")

        # Build Mayor's context with current state
        active_workers = list(self.active_agents.values())
        active_workers_info = [
            {
                "name": agent.name,
                "current_issue_title": self.db.get_issue(agent.issue_id).get("title", "unknown"),
            }
            for agent in active_workers
        ]

        open_issues = self.db.get_ready_queue(limit=50)
        recent_completions = self.db.conn.execute(
            """
            SELECT * FROM issues
            WHERE status = 'done'
            ORDER BY updated_at DESC
            LIMIT 5
            """
        ).fetchall()
        recent_completions = [dict(row) for row in recent_completions]

        escalations = []  # TODO: track escalated issues

        # Build Mayor prompt with current state
        mayor_prompt = build_mayor_prompt(
            project=self.project_name,
            active_workers=active_workers_info,
            open_issues=open_issues,
            recent_completions=recent_completions,
            escalations=escalations,
        )

        # Create event for waiting on completion
        self.session_status_events[self.mayor_session_id] = asyncio.Event()

        # Send message asynchronously
        await self.opencode.send_message_async(
            self.mayor_session_id,
            parts=[{"type": "text", "text": f"{mayor_prompt}\n\n{message}"}],
            directory=str(self.project_path),
        )

        # Wait for Mayor to finish
        try:
            await asyncio.wait_for(
                self.session_status_events[self.mayor_session_id].wait(), timeout=300
            )
        except asyncio.TimeoutError:
            self.db.log_event(None, None, "mayor_timeout", {"message": message})
            return None
        finally:
            if self.mayor_session_id in self.session_status_events:
                del self.session_status_events[self.mayor_session_id]

        # Get messages and parse work plan
        messages = await self.opencode.get_messages(
            self.mayor_session_id, directory=str(self.project_path)
        )

        work_plan = parse_work_plan(messages)
        return work_plan

    async def handle_user_request(self, user_input: str):
        """
        Handle a user request by routing to Mayor.

        Args:
            user_input: User's natural language request
        """
        message = f"""New request from user:

{user_input}

Analyze this request and create a work plan. Output a :::WORK_PLAN::: block."""

        work_plan = await self.send_to_mayor(message)

        if work_plan and work_plan.issues:
            await self.create_issues_from_plan(work_plan)
            self.db.log_event(
                None,
                None,
                "work_plan_created",
                {"user_input": user_input, "issues_count": len(work_plan.issues)},
            )

    async def create_issues_from_plan(self, work_plan: WorkPlan):
        """
        Create issues and dependencies from Mayor's work plan.

        Args:
            work_plan: WorkPlan with issues list
        """
        # Map of plan IDs to database IDs
        id_map = {}

        # Create all issues first
        for issue_spec in work_plan.issues:
            issue_id = self.db.create_issue(
                title=issue_spec.get("title", "Untitled"),
                description=issue_spec.get("description", ""),
                priority=issue_spec.get("priority", 2),
                issue_type=issue_spec.get("type", "task"),
                project=issue_spec.get("project", self.project_name),
            )
            plan_id = issue_spec.get("id", issue_id)
            id_map[plan_id] = issue_id

        # Wire up dependencies
        for issue_spec in work_plan.issues:
            plan_id = issue_spec.get("id")
            if plan_id and plan_id in id_map:
                issue_id = id_map[plan_id]
                needs = issue_spec.get("needs", [])
                for dep_plan_id in needs:
                    if dep_plan_id in id_map:
                        dep_issue_id = id_map[dep_plan_id]
                        self.db.add_dependency(issue_id, dep_issue_id, dep_type="blocks")

    async def maybe_cycle_mayor(self):
        """Cycle Mayor session if context is getting full."""
        if not self.mayor_session_id:
            return

        # Get messages and check token count
        messages = await self.opencode.get_messages(
            self.mayor_session_id, directory=str(self.project_path)
        )

        total_tokens = sum(
            msg.get("info", {}).get("tokens", {}).get("input", 0)
            + msg.get("info", {}).get("tokens", {}).get("output", 0)
            for msg in messages
        )

        if total_tokens > Config.MAYOR_TOKEN_THRESHOLD:
            # Build state summary from DB
            active_workers = list(self.active_agents.values())
            active_workers_info = [
                {
                    "name": agent.name,
                    "current_issue_title": self.db.get_issue(agent.issue_id).get("title", "unknown"),
                }
                for agent in active_workers
            ]

            open_issues = self.db.get_ready_queue(limit=50)
            recent_completions = self.db.conn.execute(
                """
                SELECT * FROM issues
                WHERE status = 'done'
                ORDER BY updated_at DESC
                LIMIT 5
                """
            ).fetchall()
            recent_completions = [dict(row) for row in recent_completions]

            state_summary = build_mayor_state_summary(
                active_workers=active_workers_info,
                open_issues=open_issues,
                recent_completions=recent_completions,
            )

            # Delete old session
            old_session = self.mayor_session_id
            await self.opencode.delete_session(old_session, directory=str(self.project_path))

            # Create new session
            await self.create_mayor_session()

            # Prime with state summary
            await self.opencode.send_message_async(
                self.mayor_session_id,
                parts=[
                    {
                        "type": "text",
                        "text": f"""You are the Mayor resuming after a context cycle. Current system state:

{state_summary}

Ready for the next request.""",
                    }
                ],
                directory=str(self.project_path),
            )

            self.db.log_event(None, None, "mayor_cycled", {"old_session": old_session})

    async def spawn_worker(self, issue: Dict[str, str]):
        """
        Spawn a worker to handle an issue.

        Args:
            issue: Issue dict from database
        """
        issue_id = issue["id"]
        agent_name = f"worker-{generate_id('')[2:]}"  # Strip "w-" prefix

        # Create agent identity in database
        agent_id = self.db.create_agent(
            name=agent_name,
            model=Config.DEFAULT_MODEL,
            metadata={"issue_id": issue_id},
        )

        # Create git worktree
        try:
            worktree_path = create_worktree(str(self.project_path), agent_name)
        except Exception as e:
            self.db.log_event(
                issue_id,
                agent_id,
                "worktree_error",
                {"error": str(e)},
            )
            return

        # Atomic claim
        claimed = self.db.claim_issue(issue_id, agent_id)
        if not claimed:
            # Someone else claimed it first, clean up
            remove_worktree(worktree_path)
            return

        # Create OpenCode session
        try:
            session = await self.opencode.create_session(
                directory=worktree_path,
                title=f"{agent_name}: {issue['title']}",
                permissions=[
                    {"permission": "*", "pattern": "*", "action": "allow"},
                    {"permission": "question", "pattern": "*", "action": "deny"},
                    {"permission": "plan_enter", "pattern": "*", "action": "deny"},
                    {"permission": "external_directory", "pattern": "*", "action": "deny"},
                ],
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
                """.format(
                    Config.LEASE_DURATION
                ),
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
                project=self.project_name,
            )
            self.active_agents[agent_id] = agent

            # Build and send prompt
            branch_name = f"agent/{agent_name}"
            prompt = build_worker_prompt(
                agent_name=agent_name,
                issue=issue,
                worktree_path=worktree_path,
                branch_name=branch_name,
                project=self.project_name,
            )

            system_prompt = build_system_prompt(
                project=self.project_name,
                agent_name=agent_name,
                worktree_path=worktree_path,
            )

            # Create event for waiting on completion
            self.session_status_events[session_id] = asyncio.Event()

            # Send prompt asynchronously
            await self.opencode.send_message_async(
                session_id,
                parts=[{"type": "text", "text": prompt}],
                directory=worktree_path,
            )

            self.db.log_event(
                issue_id,
                agent_id,
                "worker_started",
                {"session_id": session_id, "worktree": worktree_path},
            )

            # Start monitoring task
            asyncio.create_task(self.monitor_agent(agent))

        except Exception as e:
            self.db.log_event(
                issue_id,
                agent_id,
                "spawn_error",
                {"error": str(e)},
            )
            # Clean up
            remove_worktree(worktree_path)
            self.db.update_issue_status(issue_id, "failed")

    async def monitor_agent(self, agent: AgentIdentity):
        """
        Monitor an agent until completion.

        Args:
            agent: Agent identity
        """
        try:
            # Wait for session to become idle
            event = self.session_status_events.get(agent.session_id)
            if event:
                # Wait with timeout
                try:
                    await asyncio.wait_for(event.wait(), timeout=Config.LEASE_DURATION)
                except asyncio.TimeoutError:
                    # Lease expired without completion
                    await self.handle_stalled_agent(agent)
                    return

            # Agent finished, assess completion
            await self.handle_agent_complete(agent)

        except Exception as e:
            self.db.log_event(
                agent.issue_id,
                agent.agent_id,
                "monitor_error",
                {"error": str(e)},
            )
        finally:
            # Clean up
            if agent.session_id in self.session_status_events:
                del self.session_status_events[agent.session_id]

    async def handle_agent_complete(self, agent: AgentIdentity):
        """
        Handle agent completion.

        Args:
            agent: Agent identity
        """
        # Get messages from session
        try:
            messages = await self.opencode.get_messages(
                agent.session_id, directory=agent.worktree
            )

            # Assess completion
            result = assess_completion(messages)

            if result.success:
                # Mark issue as done
                self.db.update_issue_status(agent.issue_id, "done")

                # Get commit hash if available
                commit_hash = result.git_commit or get_commit_hash(agent.worktree)

                # Enqueue to merge queue
                self.db.conn.execute(
                    """
                    INSERT INTO merge_queue (issue_id, agent_id, project, worktree, branch_name)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        agent.issue_id,
                        agent.agent_id,
                        self.project_name,
                        agent.worktree,
                        f"agent/{agent.name}",
                    ),
                )
                self.db.conn.commit()

                self.db.log_event(
                    agent.issue_id,
                    agent.agent_id,
                    "completed",
                    {
                        "summary": result.summary,
                        "commit": commit_hash,
                        "artifacts": result.artifacts,
                    },
                )
            else:
                # Mark as failed
                self.db.update_issue_status(agent.issue_id, "failed")

                self.db.log_event(
                    agent.issue_id,
                    agent.agent_id,
                    "incomplete",
                    {"reason": result.reason, "summary": result.summary},
                )

        except Exception as e:
            self.db.log_event(
                agent.issue_id,
                agent.agent_id,
                "completion_error",
                {"error": str(e)},
            )
        finally:
            # Remove from active agents
            if agent.agent_id in self.active_agents:
                del self.active_agents[agent.agent_id]

    async def handle_stalled_agent(self, agent: AgentIdentity):
        """
        Handle a stalled agent (lease expired).

        Args:
            agent: Agent identity
        """
        self.db.log_event(
            agent.issue_id,
            agent.agent_id,
            "stalled",
            {"lease_expired": True},
        )

        # Abort the session
        await self.opencode.abort_session(agent.session_id, directory=agent.worktree)

        # Unassign issue so it can be retried
        self.db.conn.execute(
            """
            UPDATE issues
            SET assignee = NULL,
                status = 'open'
            WHERE id = ?
            """,
            (agent.issue_id,),
        )
        self.db.conn.commit()

        # Remove from active agents
        if agent.agent_id in self.active_agents:
            del self.active_agents[agent.agent_id]

    async def check_stalled_agents(self):
        """Check for stalled agents and handle them."""
        now = datetime.now()

        # Query agents with expired leases
        cursor = self.db.conn.execute(
            """
            SELECT id, session_id, worktree, current_issue, name
            FROM agents
            WHERE status = 'working'
              AND lease_expires_at < datetime('now')
            """
        )

        for row in cursor.fetchall():
            agent_dict = dict(row)
            agent = AgentIdentity(
                agent_id=agent_dict["id"],
                name=agent_dict["name"],
                issue_id=agent_dict["current_issue"],
                worktree=agent_dict["worktree"],
                session_id=agent_dict["session_id"],
                project=self.project_name,
            )

            await self.handle_stalled_agent(agent)


async def main():
    """Main entry point for orchestrator."""
    db = Database(Config.DB_PATH)
    db.connect()

    async with OpenCodeClient(Config.OPENCODE_URL, Config.OPENCODE_PASSWORD) as opencode:
        # Get project path from command line or env
        import sys

        project_path = sys.argv[1] if len(sys.argv) > 1 else "."
        project_name = Path(project_path).name

        orchestrator = Orchestrator(
            db=db, opencode_client=opencode, project_path=project_path, project_name=project_name
        )

        try:
            await orchestrator.start()
        except KeyboardInterrupt:
            print("\nShutting down orchestrator...")
        finally:
            db.close()


if __name__ == "__main__":
    asyncio.run(main())
