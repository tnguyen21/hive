"""Fake OpenCode server for integration testing."""

import asyncio
import json
import uuid
from typing import Any, Dict, List, Optional

from aiohttp import web


class FakeOpenCodeServer:
    """A fake OpenCode server that speaks enough of the protocol for integration tests.

    This server doesn't run real code - it just accepts HTTP requests and can emit
    canned SSE events for testing orchestrator behavior.
    """

    def __init__(self):
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.messages: Dict[str, List[Dict[str, Any]]] = {}
        self.pending_permissions: List[Dict[str, Any]] = []
        self.created_session_ids: List[str] = []
        self.global_event_queue: asyncio.Queue = asyncio.Queue()
        self._runner: Optional[web.AppRunner] = None
        self.app = web.Application()
        self._setup_routes()

    def _setup_routes(self):
        """Set up all the HTTP endpoints."""
        # Session management
        self.app.router.add_post("/session", self.create_session)
        self.app.router.add_get("/session", self.list_sessions)
        self.app.router.add_get("/session/{session_id}/status", self.get_session_status)
        self.app.router.add_get("/session/{session_id}", self.get_session_info)
        self.app.router.add_delete("/session/{session_id}", self.delete_session)
        self.app.router.add_post("/session/{session_id}/abort", self.abort_session)

        # Messages and communication
        self.app.router.add_post("/session/{session_id}/prompt_async", self.send_message)
        self.app.router.add_get("/session/{session_id}/message", self.get_messages)

        # SSE events
        self.app.router.add_get("/global/event", self.global_sse_events)
        self.app.router.add_get("/session/{session_id}/events", self.per_session_sse_events)

        # Permissions
        self.app.router.add_get("/permission", self.get_permissions)
        self.app.router.add_post("/permission/{permission_id}/reply", self.reply_permission)

    # ── HTTP Handlers ──────────────────────────────────────────────────

    async def create_session(self, request: web.Request) -> web.Response:
        """POST /session - Create a new session."""
        session_id = f"fake-{str(uuid.uuid4())[:8]}"

        try:
            body = await request.json()
        except Exception:
            body = {}

        session_data = {
            "id": session_id,
            "status": "idle",
            "title": body.get("title", f"Session {session_id}"),
            "directory": request.headers.get("X-OpenCode-Directory"),
        }

        self.sessions[session_id] = session_data
        self.messages[session_id] = []
        self.created_session_ids.append(session_id)

        return web.json_response(session_data)

    async def list_sessions(self, request: web.Request) -> web.Response:
        """GET /session - List all sessions (flat list)."""
        return web.json_response(list(self.sessions.values()))

    async def get_session_status(self, request: web.Request) -> web.Response:
        """GET /session/{session_id}/status - Get session status."""
        session_id = request.match_info["session_id"]
        if session_id not in self.sessions:
            return web.Response(status=404)
        session = self.sessions[session_id]
        return web.json_response({"id": session_id, "type": session["status"]})

    async def get_session_info(self, request: web.Request) -> web.Response:
        """GET /session/{session_id} - Get session info."""
        session_id = request.match_info["session_id"]
        if session_id not in self.sessions:
            return web.Response(status=404)
        return web.json_response(self.sessions[session_id])

    async def delete_session(self, request: web.Request) -> web.Response:
        """DELETE /session/{session_id} - Delete session."""
        session_id = request.match_info["session_id"]
        self.sessions.pop(session_id, None)
        self.messages.pop(session_id, None)
        return web.Response(status=200)

    async def abort_session(self, request: web.Request) -> web.Response:
        """POST /session/{session_id}/abort - Abort session."""
        session_id = request.match_info["session_id"]
        if session_id in self.sessions:
            self.sessions[session_id]["status"] = "idle"
        return web.Response(status=200)

    async def send_message(self, request: web.Request) -> web.Response:
        """POST /session/{session_id}/prompt_async - Send message to session."""
        session_id = request.match_info["session_id"]
        if session_id not in self.sessions:
            return web.Response(status=404)

        try:
            body = await request.json()
            self.messages.setdefault(session_id, []).append(body)
        except Exception:
            pass

        # Auto-transition to busy (mirrors real OpenCode behavior)
        self.sessions[session_id]["status"] = "busy"

        return web.Response(status=200)

    async def get_messages(self, request: web.Request) -> web.Response:
        """GET /session/{session_id}/message - Get messages in session."""
        session_id = request.match_info["session_id"]
        if session_id not in self.sessions:
            return web.Response(status=404)
        return web.json_response(self.messages.get(session_id, []))

    async def global_sse_events(self, request: web.Request) -> web.StreamResponse:
        """GET /global/event - Global SSE event stream (all sessions)."""
        response = web.StreamResponse()
        response.headers["Content-Type"] = "text/event-stream"
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Connection"] = "keep-alive"
        await response.prepare(request)

        try:
            while True:
                event = await self.global_event_queue.get()
                event_data = json.dumps(event)
                await response.write(f"data: {event_data}\n\n".encode())
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        except Exception:
            pass

        return response

    async def per_session_sse_events(self, request: web.Request) -> web.StreamResponse:
        """GET /session/{session_id}/events - Per-session SSE event stream (legacy)."""
        session_id = request.match_info["session_id"]
        if session_id not in self.sessions:
            return web.Response(status=404)

        response = web.StreamResponse()
        response.headers["Content-Type"] = "text/event-stream"
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Connection"] = "keep-alive"
        await response.prepare(request)

        # Per-session events are not used by the orchestrator (it uses /global/event).
        # Keep this endpoint for backward compatibility with direct SSE tests.
        try:
            while True:
                await asyncio.sleep(60)
        except (asyncio.CancelledError, ConnectionResetError):
            pass

        return response

    async def get_permissions(self, request: web.Request) -> web.Response:
        """GET /permission - List pending permissions."""
        return web.json_response(self.pending_permissions)

    async def reply_permission(self, request: web.Request) -> web.Response:
        """POST /permission/{permission_id}/reply - Reply to a permission request."""
        permission_id = request.match_info["permission_id"]
        self.pending_permissions = [p for p in self.pending_permissions if p.get("id") != permission_id]
        return web.Response(status=200)

    # ── Test Helper Methods ────────────────────────────────────────────

    def _push_global_event(self, session_id: str, event_type: str, properties: dict):
        """Push an event to the global SSE queue in the correct envelope format.

        The real OpenCode global stream emits:
            {"directory": "...", "payload": {"type": "...", "properties": {...}}}
        """
        directory = self.sessions.get(session_id, {}).get("directory", "")
        event = {
            "directory": directory or "",
            "payload": {
                "type": event_type,
                "properties": properties,
            },
        }
        self.global_event_queue.put_nowait(event)

    def inject_event(self, session_id: str, event_type: str, properties: dict):
        """Push a properly-enveloped event to the global SSE queue."""
        self._push_global_event(session_id, event_type, properties)

    def inject_idle(self, session_id: str):
        """Set session to idle and push SSE event to global stream."""
        if session_id in self.sessions:
            self.sessions[session_id]["status"] = "idle"
        self._push_global_event(
            session_id,
            "session.status",
            {
                "sessionID": session_id,
                "status": {"type": "idle"},
            },
        )

    def inject_error(self, session_id: str, error_message: str = "session error"):
        """Push a session error SSE event."""
        self._push_global_event(
            session_id,
            "session.error",
            {
                "sessionID": session_id,
                "error": error_message,
            },
        )

    def inject_permission(self, session_id: str, permission: str, permission_id: str = None):
        """Add a pending permission and push SSE event."""
        pid = permission_id or f"perm-{uuid.uuid4().hex[:8]}"
        perm = {"id": pid, "permission": permission, "sessionID": session_id}
        self.pending_permissions.append(perm)
        self._push_global_event(session_id, "permission.request", perm)

    def set_session_status(self, session_id: str, status: str):
        """Set stored session status without pushing SSE events."""
        if session_id in self.sessions:
            self.sessions[session_id]["status"] = status

    def set_messages(self, session_id: str, messages: List[Dict[str, Any]]):
        """Set the messages list for a session (for testing token extraction)."""
        self.messages[session_id] = messages

    def get_created_sessions(self) -> List[str]:
        """Return list of session IDs that were created."""
        return self.created_session_ids.copy()

    async def start_server(self, host: str = "localhost", port: int = 0) -> tuple[str, int]:
        """Start the server and return the actual host and port."""
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()

        site = web.TCPSite(self._runner, host, port)
        await site.start()

        actual_port = site._server.sockets[0].getsockname()[1]
        return host, actual_port

    async def stop_server(self):
        """Stop the server and clean up."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    @property
    def url(self) -> Optional[str]:
        """Get the base URL of the server (set by fixture)."""
        return getattr(self, "_url", None)

    def set_url(self, url: str):
        """Set the base URL (used by fixture)."""
        self._url = url
