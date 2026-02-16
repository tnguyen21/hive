"""WebSocket backend using Claude Code CLI with --sdk-url.

Replaces OpenCode as middleware: instead of Hive -> HTTP -> OpenCode -> Anthropic API,
this runs Hive as a WebSocket server that Claude CLI processes connect to, using
Claude Code subscription credits instead of API billing.

Implements the same interfaces as OpenCodeClient + SSEClient so the orchestrator
and merge processor work unchanged.
"""

import asyncio
import inspect
import json
import logging
import os
import shutil
import signal
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import aiohttp
import aiohttp.web

from .config import Config
from .utils import generate_id

logger = logging.getLogger(__name__)


@dataclass
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


class ClaudeWSBackend:
    """WebSocket backend using Claude Code CLI with --sdk-url.

    Acts as a WebSocket server that Claude CLI processes connect to.
    Implements OpenCodeClient + SSEClient interfaces for drop-in replacement.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8765):
        self.host = host
        self.port = port
        self.app = aiohttp.web.Application()
        self.app.router.add_get("/agent/{session_id}", self._ws_handler)

        # Per-session state
        self.sessions: Dict[str, SessionState] = {}

        # SSE-compatible event handlers
        self._handlers: Dict[str, Callable] = {}

        # Concurrency limiter
        self._spawn_semaphore = asyncio.Semaphore(Config.CLAUDE_WS_MAX_CONCURRENT)

        # Server lifecycle
        self.running = False
        self.server_ready = asyncio.Event()
        self._runner: Optional[aiohttp.web.AppRunner] = None

    # ── OpenCodeClient-compatible methods ─────────────────────────────

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
            proc = await asyncio.create_subprocess_exec(
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
                "-p",
                "",
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
        model: Optional[Dict[str, str]] = None,
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
        await self._ws_send(
            session_id,
            {
                "type": "user",
                "message": {"role": "user", "content": text},
                "parent_tool_use_id": None,
                "session_id": session.cli_session_id or "",
            },
        )
        session.status = "busy"

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
            return {"type": "idle"}

        # Check if process has died
        if session.process and session.process.returncode is not None:
            return {"type": "error"}

        return {"type": session.status}

    async def get_messages(
        self,
        session_id: str,
        directory: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Return collected messages, translated to OpenCode format."""
        session = self.sessions.get(session_id)
        if not session:
            return []
        messages = session.messages
        if limit:
            messages = messages[-limit:]
        return [self._translate_message(m) for m in messages]

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
            return False
        if session.process and session.process.returncode is None:
            # Send SIGTERM to the process group (child is a session leader
            # via start_new_session=True) so grandchildren are also killed.
            try:
                os.killpg(session.process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.wait_for(session.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                try:
                    os.killpg(session.process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    session.process.kill()
        if session.ws and not session.ws.closed:
            await session.ws.close()
        return True

    async def cleanup_session(self, session_id: str, directory: Optional[str] = None):
        """Abort + delete. Best-effort."""
        try:
            await self.abort_session(session_id, directory)
        except Exception:
            pass
        try:
            await self.delete_session(session_id, directory)
        except Exception:
            pass

    async def get_pending_permissions(self, directory: Optional[str] = None) -> List[Dict[str, Any]]:
        """No-op with bypassPermissions mode."""
        return []

    async def reply_permission(
        self,
        request_id: str,
        reply: str,
        message: Optional[str] = None,
        directory: Optional[str] = None,
    ):
        """No-op with bypassPermissions mode."""
        pass

    # ── SSEClient-compatible methods ──────────────────────────────────

    def on(self, event_type: str, handler: Callable):
        """Register handler for specific event type."""
        self._handlers[event_type] = handler

    def on_all(self, handler: Callable):
        """Register catch-all handler for all events."""
        self._handlers["*"] = handler

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

        msg_type = msg.get("type")

        if msg_type == "system" and msg.get("subtype") == "init":
            session.cli_session_id = msg.get("session_id")
            session.connected.set()
            logger.info(f"Session {session_id} initialized (cli_session={session.cli_session_id})")

        elif msg_type == "assistant":
            session.messages.append(msg)
            # Emit activity event for lease renewal
            await self._emit("session.status", {"sessionID": session_id, "status": {"type": "busy"}})

        elif msg_type == "result":
            session.status = "idle"
            session.result = msg
            session.messages.append(msg)
            session.total_usage = msg.get("usage", {})
            # Emit SSE-compatible idle event
            await self._emit("session.status", {"sessionID": session_id, "status": {"type": "idle"}})

        elif msg_type == "control_request":
            # Shouldn't happen with bypassPermissions, but handle gracefully
            subtype = msg.get("request", {}).get("subtype")
            if subtype == "can_use_tool":
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

        elif msg_type in ("keep_alive", "user", "control_response"):
            pass  # Expected protocol messages, no action needed

        else:
            logger.debug(f"Unhandled message type '{msg_type}' from session {session_id}")

    def _translate_message(self, msg: dict) -> dict:
        """Translate WS protocol message to OpenCode message format."""
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

    async def _emit(self, event_type: str, properties: dict):
        """Emit an SSE-compatible event to registered handlers."""
        handler = self._handlers.get(event_type)
        if handler:
            if inspect.iscoroutinefunction(handler):
                await handler(properties)
            else:
                handler(properties)

        # Call catch-all handler if registered
        all_handler = self._handlers.get("*")
        if all_handler:
            if inspect.iscoroutinefunction(all_handler):
                await all_handler(event_type, properties)
            else:
                all_handler(event_type, properties)

    async def _ws_send(self, session_id: str, msg: dict):
        """Send a message to the CLI process via WebSocket."""
        session = self.sessions.get(session_id)
        if session and session.ws and not session.ws.closed:
            await session.ws.send_str(json.dumps(msg) + "\n")

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

    # ── Context manager (matches OpenCodeClient interface) ────────────

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.stop()
        # Kill all child processes immediately (SIGKILL to process groups).
        # No graceful abort/WS-close — we're shutting down the whole daemon.
        for session_id, session in list(self.sessions.items()):
            if session.process and session.process.returncode is None:
                try:
                    os.killpg(session.process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    try:
                        session.process.kill()
                    except ProcessLookupError:
                        pass
        self.sessions.clear()
        if self._runner:
            try:
                await asyncio.wait_for(self._runner.cleanup(), timeout=3)
            except (asyncio.TimeoutError, Exception):
                pass
