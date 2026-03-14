"""In-memory HiveBackend for testing (no HTTP/WS server required)."""

import asyncio
import inspect
import uuid
from typing import Any, Callable, Dict, List, Optional

from hive.backends.base import HiveBackend


class FakeBackend(HiveBackend):
    """A fake backend that stores everything in-memory for unit/integration tests.

    All calls go directly to in-memory dicts — no HTTP server needed.
    """

    def __init__(self):
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.messages: Dict[str, List[Dict[str, Any]]] = {}
        self.pending_permissions: List[Dict[str, Any]] = []
        self.created_session_ids: List[str] = []
        self._handlers: Dict[str, List[Callable]] = {}
        self.running = False
        self.server_ready = asyncio.Event()

    # ── Session management ────────────────────────────────────────────

    async def list_sessions(self) -> List[Dict[str, Any]]:
        return list(self.sessions.values())

    async def create_session(
        self,
        directory: Optional[str] = None,
        title: Optional[str] = None,
        permissions: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        session_id = f"fake-{uuid.uuid4().hex[:8]}"
        session_data = {
            "id": session_id,
            "status": "idle",
            "title": title or f"Session {session_id}",
            "directory": directory,
        }
        self.sessions[session_id] = session_data
        self.messages[session_id] = []
        self.created_session_ids.append(session_id)
        return session_data

    async def send_message_async(
        self,
        session_id: str,
        parts: List[Dict[str, Any]],
        agent: str = "build",
        model: Optional[str] = None,
        system: Optional[str] = None,
        directory: Optional[str] = None,
    ):
        if session_id not in self.sessions:
            raise ValueError(f"Session {session_id} not found")
        self.messages.setdefault(session_id, []).append({"parts": parts, "agent": agent, "model": model, "system": system})
        self.sessions[session_id]["status"] = "busy"

    async def abort_session(self, session_id: str, directory: Optional[str] = None) -> bool:
        if session_id in self.sessions:
            self.sessions[session_id]["status"] = "idle"
            return True
        return False

    async def delete_session(self, session_id: str, directory: Optional[str] = None) -> bool:
        self.sessions.pop(session_id, None)
        self.messages.pop(session_id, None)
        return True

    async def cleanup_session(self, session_id: str, directory: Optional[str] = None):
        try:
            await self.abort_session(session_id, directory=directory)
        except Exception:
            pass
        try:
            await self.delete_session(session_id, directory=directory)
        except Exception:
            pass

    async def get_session_status(self, session_id: str, directory: Optional[str] = None) -> Dict[str, Any]:
        session = self.sessions.get(session_id)
        if not session:
            return {"type": "not_found"}
        return {"id": session_id, "type": session["status"]}

    async def get_messages(self, session_id: str, directory: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        msgs = self.messages.get(session_id, [])
        if limit:
            return msgs[-limit:]
        return msgs

    async def get_pending_permissions(self, directory: Optional[str] = None) -> List[Dict[str, Any]]:
        return self.pending_permissions

    async def reply_permission(self, request_id: str, reply: str, message: Optional[str] = None, directory: Optional[str] = None):
        self.pending_permissions = [p for p in self.pending_permissions if p.get("id") != request_id]

    # ── Event streaming ───────────────────────────────────────────────

    def on(self, event_type: str, handler: Callable):
        self._handlers.setdefault(event_type, []).append(handler)

    def on_all(self, handler: Callable):
        self._handlers.setdefault("*", []).append(handler)

    async def connect_with_reconnect(self, max_retries: int = -1, retry_delay: int = 5):
        self.running = True
        self.server_ready.set()
        while self.running:
            await asyncio.sleep(0.1)

    def stop(self):
        self.running = False

    # ── Context manager ───────────────────────────────────────────────

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.stop()

    # ── Test helper methods ───────────────────────────────────────────

    async def _dispatch(self, event_type: str, properties: dict):
        """Dispatch an event to registered handlers."""
        for handler in self._handlers.get(event_type, []):
            if inspect.iscoroutinefunction(handler):
                await handler(properties)
            else:
                handler(properties)
        for handler in self._handlers.get("*", []):
            if inspect.iscoroutinefunction(handler):
                await handler(event_type, properties)
            else:
                handler(event_type, properties)

    def _dispatch_sync_or_schedule(self, event_type: str, properties: dict):
        """Dispatch immediately in sync contexts or schedule on a running loop."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.get_event_loop().run_until_complete(self._dispatch(event_type, properties))
            return None
        return loop.create_task(self._dispatch(event_type, properties))

    def inject_idle(self, session_id: str):
        """Set session to idle and dispatch event."""
        if session_id in self.sessions:
            self.sessions[session_id]["status"] = "idle"
        return self._dispatch_sync_or_schedule(
            "session.status",
            {"sessionID": session_id, "status": {"type": "idle"}},
        )

    def inject_idle_async(self, session_id: str):
        """Async version of inject_idle for use in async tests."""
        if session_id in self.sessions:
            self.sessions[session_id]["status"] = "idle"
        return self._dispatch(
            "session.status",
            {"sessionID": session_id, "status": {"type": "idle"}},
        )

    def inject_error(self, session_id: str, error_message: str = "session error"):
        """Dispatch a session error event."""
        return self._dispatch_sync_or_schedule(
            "session.error",
            {"sessionID": session_id, "error": error_message},
        )

    def inject_permission(self, data: dict):
        """Add a pending permission and dispatch event."""
        self.pending_permissions.append(data)
        return self._dispatch_sync_or_schedule("permission.request", data)

    def set_session_status(self, session_id: str, status: str):
        """Set stored session status without dispatching events."""
        if session_id in self.sessions:
            self.sessions[session_id]["status"] = status

    def set_messages(self, session_id: str, messages: List[Dict[str, Any]]):
        """Set the messages list for a session."""
        self.messages[session_id] = messages

    def get_created_sessions(self) -> List[str]:
        """Return list of session IDs that were created."""
        return self.created_session_ids.copy()
