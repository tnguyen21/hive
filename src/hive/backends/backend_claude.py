"""Claude WebSocket backend: direct Claude CLI via --sdk-url.

    Hive ──WebSocket──> Claude CLI process (one per session)
                        └──> Anthropic (using Claude Code subscription credits)

Hive spawns `claude` CLI processes with
`--sdk-url ws://<host>:<port>/agent/<session_id>`. Each CLI process connects
*back* to Hive's built-in WebSocket server, creating a bidirectional channel.

- **Billing**: Uses Claude Code subscription credits, not API keys.
- **Process lifecycle**: One OS process per session. Cleanup means killing the
  process group (SIGTERM → SIGKILL).
- **WS handshake protocol**: After Hive spawns the CLI and it connects via WS,
  the CLI sends a `user` message first. Only after receiving this does Hive
  respond with `system/init` (containing the system prompt). This is the
  reverse of what you might expect — the CLI drives initialization.
  See docs/claude_ws_protocol_reversed.md for the full protocol.
- **Permissions**: The CLI runs with `bypassPermissions` so the permission
  API methods are no-ops.
- **Concurrency**: Controlled via MAX_AGENTS config (semaphore on process
  spawning) to avoid overwhelming the machine.
"""

import asyncio
from contextlib import suppress
import json
import logging
import os
import shutil
import signal
import uuid
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Dict, List, Optional

import aiohttp
import aiohttp.web

from ..config import Config
from ..utils import generate_id
from .base import HiveBackend

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SessionState:
    """Per-session state tracking for a Claude CLI process."""

    directory: Optional[str] = None
    title: Optional[str] = None
    model: Optional[str] = None
    process: Optional[asyncio.subprocess.Process] = None
    ws: Optional[aiohttp.web.WebSocketResponse] = None
    cli_session_id: Optional[str] = None
    status: str = "idle"  # "idle" | "busy" | "error"
    messages: list = field(default_factory=list)
    result: Optional[dict] = None
    total_usage: dict = field(default_factory=dict)
    ws_connected: asyncio.Event = field(default_factory=asyncio.Event)  # WS handshake done
    connected: asyncio.Event = field(default_factory=asyncio.Event)  # system/init received
    initialized: bool = False


class ClaudeWSBackend(HiveBackend):
    """WebSocket backend using Claude Code CLI with --sdk-url.

    Acts as a WebSocket server that Claude CLI processes connect to.
    Implements the full HiveBackend interface (session management + event streaming).
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8765):
        super().__init__()
        self.host = host
        self.port = port
        self.app = aiohttp.web.Application()
        self.app.router.add_get("/agent/{session_id}", self._ws_handler)

        # Per-session state
        self.sessions: Dict[str, SessionState] = {}

        # Concurrency limiter — MAX_AGENTS + 1 reserves a slot for the refinery
        # session so worker slots aren't reduced.
        concurrency = Config.MAX_AGENTS + 1
        self._spawn_semaphore = asyncio.Semaphore(concurrency)

        # Server lifecycle
        self.running = False
        self.server_ready = asyncio.Event()
        self._runner: Optional[aiohttp.web.AppRunner] = None

    # ── Session management ────────────────────────────────────────────

    async def list_sessions(self) -> List[Dict[str, Any]]:
        """Return list of active sessions (for health checks/reconciliation)."""
        return [
            {"id": sid, "title": s.title, "directory": s.directory}
            for sid, s in self.sessions.items()
            if s.process and s.process.returncode is None
        ]

    async def create_session(
        self,
        directory: Optional[str] = None,
        title: Optional[str] = None,
        permissions: Optional[List[Dict[str, str]]] = None,
        model: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Spawn a claude CLI process. Returns session info dict."""
        await self.server_ready.wait()

        session_id = generate_id("ws")
        resolved_model = model or Config.WORKER_MODEL or Config.DEFAULT_MODEL

        self.sessions[session_id] = SessionState(
            directory=directory,
            title=title,
            model=resolved_model,
        )

        claude_bin = shutil.which("claude")
        if not claude_bin:
            raise RuntimeError("claude CLI not found on PATH")

        async with self._spawn_semaphore:
            # Strip CLAUDECODE env var so workers don't think they're nested sessions
            spawn_env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

            cli_args = [
                claude_bin,
                "--sdk-url",
                f"ws://{self.host}:{self.port}/agent/{session_id}",
                "--print",
                "--output-format",
                "stream-json",
                "--input-format",
                "stream-json",
                "--verbose",
                "--model",
                resolved_model,
            ]
            mcp_configs = [c for c in os.environ.get("HIVE_CLAUDE_MCP_CONFIGS", "").split(os.pathsep) if c]
            for config in mcp_configs:
                cli_args.extend(["--mcp-config", config])
            if Config.CLAUDE_SKIP_PERMISSIONS:
                cli_args.append("--dangerously-skip-permissions")
            cli_args.extend(["-p", ""])

            proc = await asyncio.create_subprocess_exec(
                *cli_args,
                cwd=directory,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                env=spawn_env,
            )
            self.sessions[session_id].process = proc
            logger.info(f"Spawned claude CLI (pid={proc.pid}) for session {session_id}, model={resolved_model}")

        # Wait for WebSocket connection (not system/init — that requires a user message first)
        try:
            await asyncio.wait_for(self.sessions[session_id].ws_connected.wait(), timeout=30)
        except asyncio.TimeoutError:
            # Capture stderr for diagnostics
            stderr_output = ""
            if proc.stderr:
                try:
                    stderr_bytes = await asyncio.wait_for(proc.stderr.read(4096), timeout=2)
                    stderr_output = stderr_bytes.decode(errors="replace").strip()
                except (asyncio.TimeoutError, Exception):
                    pass
            logger.error(f"Timeout waiting for CLI WS connection for session {session_id}. stderr: {stderr_output or '(empty)'}")
            await self.delete_session(session_id)
            raise RuntimeError(f"Claude CLI failed to connect within 30s for session {session_id}. stderr: {stderr_output or '(empty)'}")

        return {"id": session_id, "title": title, "directory": directory}

    async def send_message_async(
        self,
        session_id: str,
        parts: List[Dict[str, Any]],
        agent: str = "build",
        model: Optional[str] = None,
        system: Optional[str] = None,
        directory: Optional[str] = None,
    ):
        """Send a user message on the WebSocket. Fire-and-forget."""
        session = self.sessions.get(session_id)
        if not session:
            raise ValueError(f"Session {session_id} not found")

        # On first message, optionally send initialize with system prompt
        if not session.initialized and system:
            await self._send_initialize(session_id, system)

        # Extract text from parts
        text = ""
        for part in parts:
            if part.get("type") == "text":
                text = part.get("text", "")
                break

        # Send user message — the CLI responds with system/init after the
        # first user message, so we send first, then wait for init.
        prev_status = session.status
        message_sent = await self._ws_send(
            session_id,
            {
                "type": "user",
                "message": {"role": "user", "content": text},
                "parent_tool_use_id": None,
                "session_id": session.cli_session_id or "",
            },
        )
        session.status = "busy"
        logger.info(
            f"Session {session_id} status transition {prev_status} -> busy "
            f"(message_sent={message_sent}, ws_connected={session.ws_connected.is_set()}, "
            f"connected={session.connected.is_set()})"
        )
        if not message_sent:
            logger.warning(
                f"User message may have been dropped for session {session_id} "
                f"(ws_connected={session.ws_connected.is_set()}, ws_present={session.ws is not None})"
            )

        # Wait for system/init if this is the first message
        if not session.connected.is_set():
            try:
                await asyncio.wait_for(session.connected.wait(), timeout=30)
            except asyncio.TimeoutError:
                logger.error(f"Timeout waiting for system/init from session {session_id}")
                raise RuntimeError(f"CLI did not send system/init within 30s for session {session_id}")

    async def get_session_status(self, session_id: str, directory: Optional[str] = None) -> Dict[str, Any]:
        """Return tracked status from WS messages."""
        session = self.sessions.get(session_id)
        if not session:
            logger.warning(f"Session status requested for unknown session {session_id}")
            return {"type": "not_found"}

        # Check if process has died
        if session.process and session.process.returncode is not None:
            logger.warning(f"Session status requested for dead process {session_id} (returncode={session.process.returncode})")
            return {"type": "error"}

        return {"type": session.status}

    async def get_messages(self, session_id: str, directory: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Return collected messages, translated to standard format."""
        session = self.sessions.get(session_id)
        if not session:
            return []
        msgs = session.messages
        if limit:
            msgs = msgs[-limit:]
        return [self._translate_message(m) for m in msgs]

    async def abort_session(self, session_id: str, directory: Optional[str] = None) -> bool:
        """Send interrupt control request."""
        session = self.sessions.get(session_id)
        if not session or not session.ws:
            return False
        await self._send_interrupt(session_id)
        return True

    async def delete_session(self, session_id: str, directory: Optional[str] = None) -> bool:
        """Kill the CLI process and clean up."""
        session = self.sessions.pop(session_id, None)
        if not session:
            logger.debug(f"delete_session called for unknown session {session_id}")
            return False
        logger.info(f"Deleting session {session_id} (directory={directory or session.directory})")
        if session.process and session.process.returncode is None:
            # Send SIGTERM to the process group (child is a session leader
            # via start_new_session=True) so grandchildren are also killed.
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(session.process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(session.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                with suppress(ProcessLookupError, PermissionError):
                    os.killpg(session.process.pid, signal.SIGKILL)
                if session.process.returncode is None:
                    with suppress(ProcessLookupError):
                        session.process.kill()
        if session.ws and not session.ws.closed:
            await session.ws.close()
        return True

    async def cleanup_session(self, session_id: str, directory: Optional[str] = None):
        """Abort + delete. Best-effort."""
        with suppress(Exception):
            await self.abort_session(session_id, directory)
        with suppress(Exception):
            await self.delete_session(session_id, directory)

    async def get_pending_permissions(self, directory: Optional[str] = None) -> List[Dict[str, Any]]:
        """No-op — CLI runs with bypassPermissions."""
        return []

    async def reply_permission(self, request_id: str, reply: str, message: Optional[str] = None, directory: Optional[str] = None):
        """No-op — CLI runs with bypassPermissions."""
        pass

    # ── Event streaming ───────────────────────────────────────────────

    async def connect_with_reconnect(self, max_retries: int = -1, retry_delay: int = 5):
        """Start the WebSocket server (runs until stopped)."""
        self.running = True
        self._runner = aiohttp.web.AppRunner(self.app)
        await self._runner.setup()
        site = aiohttp.web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        logger.info(f"Claude WS backend listening on ws://{self.host}:{self.port}")
        self.server_ready.set()

        # Block until stopped
        while self.running:
            await asyncio.sleep(1)

        await self._runner.cleanup()

    def stop(self):
        """Stop the WebSocket server."""
        self.running = False

    # ── Internal: WS handler & message router ─────────────────────────

    async def _ws_handler(self, request: aiohttp.web.Request) -> aiohttp.web.WebSocketResponse:
        """Handle incoming WebSocket connection from Claude CLI."""
        session_id = request.match_info["session_id"]
        ws = aiohttp.web.WebSocketResponse()
        await ws.prepare(request)

        session = self.sessions.get(session_id)
        if not session:
            logger.warning(f"WS connection for unknown session {session_id}")
            await ws.close()
            return ws

        session.ws = ws
        session.ws_connected.set()
        logger.info(f"CLI WebSocket connected for session {session_id}")

        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                for line in msg.data.strip().split("\n"):
                    if line:
                        try:
                            parsed = json.loads(line)
                            await self._route_message(session_id, parsed)
                        except json.JSONDecodeError:
                            logger.warning(f"Malformed JSON from session {session_id}: {line[:100]}")
                        except Exception as e:
                            logger.exception(f"Error routing WS message for session {session_id}: {e}")
            elif msg.type == aiohttp.WSMsgType.ERROR:
                logger.error(f"WS error for session {session_id}: {ws.exception()}")

        # Connection closed
        if session_id in self.sessions:
            self.sessions[session_id].ws = None
            logger.info(f"CLI disconnected for session {session_id}")

        return ws

    async def _route_message(self, session_id: str, msg: dict):
        """Route an incoming message from the CLI to appropriate handlers."""
        session = self.sessions.get(session_id)
        if not session:
            return

        match msg:
            case {"type": "system", "subtype": "init"}:
                session.cli_session_id = msg.get("session_id")
                session.connected.set()
                logger.info(f"Session {session_id} initialized (cli_session={session.cli_session_id})")

            case {"type": "assistant"}:
                prev_status = session.status
                session.status = "busy"
                session.messages.append(msg)
                if prev_status != "busy":
                    logger.info(f"Session {session_id} status transition {prev_status} -> busy (source=assistant)")
                # Emit activity event for lease renewal
                try:
                    await self._emit("session.status", {"sessionID": session_id, "status": {"type": "busy"}})
                except Exception as e:
                    logger.exception(f"Failed to emit busy session.status for session {session_id}: {e}")
                    raise

            case {"type": "result"}:
                prev_status = session.status
                session.status = "idle"
                session.result = msg
                session.messages.append(msg)
                session.total_usage = msg.get("usage", {})
                logger.info(
                    f"Session {session_id} status transition {prev_status} -> idle (result_usage_keys={list(session.total_usage.keys())})"
                )
                # Emit SSE-compatible idle event
                try:
                    await self._emit("session.status", {"sessionID": session_id, "status": {"type": "idle"}})
                except Exception as e:
                    logger.exception(f"Failed to emit idle session.status for session {session_id}: {e}")
                    raise

            case {"type": "control_request", "request": {"subtype": "can_use_tool"}}:
                # Shouldn't happen with bypassPermissions, but handle gracefully
                await self._ws_send(
                    session_id,
                    {
                        "type": "control_response",
                        "response": {
                            "subtype": "success",
                            "request_id": msg["request_id"],
                            "response": {
                                "behavior": "allow",
                                "updatedInput": msg["request"].get("input"),
                            },
                        },
                    },
                )

            case {"type": "control_request"} | {"type": "keep_alive"} | {"type": "user"} | {"type": "control_response"}:
                pass  # Expected protocol messages, no action needed

            case _:
                logger.debug(f"Unhandled message type '{msg.get('type')}' from session {session_id}")

    def _translate_message(self, msg: dict) -> dict:
        """Translate WS protocol message to standard message format."""
        if msg.get("type") == "assistant":
            usage = msg.get("message", {}).get("usage", {})
            return {
                "role": "assistant",
                "content": msg.get("message", {}).get("content", []),
                "metadata": {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "model": msg.get("message", {}).get("model"),
                },
            }
        elif msg.get("type") == "result":
            usage = msg.get("usage", {})
            return {
                "role": "result",
                "metadata": {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                },
            }
        return msg

    async def _ws_send(self, session_id: str, msg: dict) -> bool:
        """Send a message to the CLI process via WebSocket."""
        session = self.sessions.get(session_id)
        msg_type = msg.get("type")
        if not session:
            logger.warning(f"Dropping WS send for unknown session {session_id} (type={msg_type})")
            return False
        if not session.ws:
            logger.warning(f"Dropping WS send for session {session_id} without WS connection (type={msg_type})")
            return False
        if session.ws.closed:
            logger.warning(f"Dropping WS send for closed session WS {session_id} (type={msg_type})")
            return False
        await session.ws.send_str(json.dumps(msg) + "\n")
        return True

    async def _send_initialize(self, session_id: str, system_prompt: str):
        """Send initialize control request with system prompt."""
        request_id = str(uuid.uuid4())
        await self._ws_send(
            session_id,
            {
                "type": "control_request",
                "request_id": request_id,
                "request": {
                    "subtype": "initialize",
                    "appendSystemPrompt": system_prompt,
                },
            },
        )
        # Wait briefly for response (best-effort)
        await asyncio.sleep(0.5)
        self.sessions[session_id].initialized = True

    async def _send_interrupt(self, session_id: str):
        """Send interrupt control request."""
        request_id = str(uuid.uuid4())
        await self._ws_send(
            session_id,
            {
                "type": "control_request",
                "request_id": request_id,
                "request": {"subtype": "interrupt"},
            },
        )

    # ── Context manager ───────────────────────────────────────────────

    async def __aenter__(self) -> "ClaudeWSBackend":
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: TracebackType | None) -> None:
        self.stop()
        # Kill all child processes immediately (SIGKILL to process groups).
        # No graceful abort/WS-close — we're shutting down the whole daemon.
        for session_id, session in list(self.sessions.items()):
            if session.process and session.process.returncode is None:
                with suppress(ProcessLookupError, PermissionError):
                    os.killpg(session.process.pid, signal.SIGKILL)
                if session.process.returncode is None:
                    with suppress(ProcessLookupError):
                        session.process.kill()
        self.sessions.clear()
        if self._runner:
            with suppress(Exception):
                await asyncio.wait_for(self._runner.cleanup(), timeout=3)
