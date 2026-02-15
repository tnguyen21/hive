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
        self.event_queues: Dict[str, asyncio.Queue] = {}
        self.created_session_ids: List[str] = []
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
        self.app.router.add_get("/session/{session_id}/events", self.sse_events)

        # Permissions (stub)
        self.app.router.add_post("/session/{session_id}/permission/{permission_id}/reply", self.reply_permission)

    async def create_session(self, request: web.Request) -> web.Response:
        """POST /session - Create a new session."""
        session_id = f"fake-{str(uuid.uuid4())[:8]}"

        # Parse request body if present
        try:
            body = await request.json()
        except Exception:
            body = {}

        session_data = {
            "id": session_id,
            "status": "running",
            "title": body.get("title", f"Session {session_id}"),
            "directory": request.headers.get("X-OpenCode-Directory"),
        }

        self.sessions[session_id] = session_data
        self.event_queues[session_id] = asyncio.Queue()
        self.created_session_ids.append(session_id)

        return web.json_response(session_data)

    async def list_sessions(self, request: web.Request) -> web.Response:
        """GET /session - List all sessions."""
        session_list = list(self.sessions.values())
        return web.json_response({"sessions": session_list})

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

        session = self.sessions[session_id]
        return web.json_response(session)

    async def delete_session(self, request: web.Request) -> web.Response:
        """DELETE /session/{session_id} - Delete session."""
        session_id = request.match_info["session_id"]

        if session_id in self.sessions:
            del self.sessions[session_id]
        if session_id in self.event_queues:
            del self.event_queues[session_id]

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

        # Parse the message (don't need to do anything with it for testing)
        try:
            await request.json()
        except Exception:
            pass

        return web.Response(status=200)

    async def get_messages(self, request: web.Request) -> web.Response:
        """GET /session/{session_id}/message - Get messages in session."""
        session_id = request.match_info["session_id"]

        if session_id not in self.sessions:
            return web.Response(status=404)

        # Return empty list as specified in requirements
        return web.json_response([])

    async def sse_events(self, request: web.Request) -> web.StreamResponse:
        """GET /session/{session_id}/events - SSE event stream."""
        session_id = request.match_info["session_id"]

        if session_id not in self.sessions:
            return web.Response(status=404)

        response = web.StreamResponse()
        response.headers["Content-Type"] = "text/event-stream"
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Connection"] = "keep-alive"

        await response.prepare(request)

        queue = self.event_queues.get(session_id)
        if not queue:
            return response

        try:
            while True:
                # Get next event from queue
                event = await queue.get()

                # Format as SSE: "data: {json}\n\n"
                event_data = json.dumps(event)
                await response.write(f"data: {event_data}\n\n".encode())

                # If this is an idle event, stop streaming
                if event.get("type") == "session.status" and event.get("status") == "idle":
                    break

        except asyncio.CancelledError:
            pass
        except Exception:
            pass

        return response

    async def reply_permission(self, request: web.Request) -> web.Response:
        """POST /session/{session_id}/permission/{permission_id}/reply - Reply to permission."""
        # Stub implementation - just return 200
        return web.Response(status=200)

    # Test helper methods

    def inject_event(self, session_id: str, event: Dict[str, Any]):
        """Push an event dict into the session SSE queue."""
        if session_id in self.event_queues:
            self.event_queues[session_id].put_nowait(event)

    def inject_idle(self, session_id: str):
        """Convenience: inject session.status idle event."""
        self.inject_event(session_id, {"type": "session.status", "status": "idle"})

    def get_created_sessions(self) -> List[str]:
        """Return list of session IDs that were created."""
        return self.created_session_ids.copy()

    async def start_server(self, host: str = "localhost", port: int = 0) -> tuple[str, int]:
        """Start the server and return the actual host and port.

        Returns:
            Tuple of (host, port) where the server is running
        """
        runner = web.AppRunner(self.app)
        await runner.setup()

        site = web.TCPSite(runner, host, port)
        await site.start()

        # Get the actual port if port=0 was passed
        actual_port = site._server.sockets[0].getsockname()[1]

        return host, actual_port

    @property
    def url(self) -> Optional[str]:
        """Get the base URL of the server (set by fixture)."""
        return getattr(self, "_url", None)

    def set_url(self, url: str):
        """Set the base URL (used by fixture)."""
        self._url = url
