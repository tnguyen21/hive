"""Abstract base class for Hive backends.

A backend provides two capabilities to the orchestrator:
1. Session management (create, send messages, abort, delete, get status/messages)
2. Event streaming (register handlers, connect, stop)

Both the Claude and Codex backends combine these into a single class.
"""

import inspect
from types import TracebackType
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Self


class HiveBackend(ABC):
    """Interface that all Hive backends must implement."""

    def __init__(self):
        self._handlers: Dict[str, Callable] = {}

    # ── Session management ────────────────────────────────────────────

    @abstractmethod
    async def list_sessions(self) -> List[Dict[str, Any]]:
        """List active sessions. Used for health checks and reconciliation."""

    @abstractmethod
    async def create_session(
        self,
        directory: Optional[str] = None,
        title: Optional[str] = None,
        permissions: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        """Create a new session. Returns dict with at least {"id": ...}."""

    @abstractmethod
    async def send_message_async(
        self,
        session_id: str,
        parts: List[Dict[str, Any]],
        agent: str = "build",
        model: Optional[str] = None,
        system: Optional[str] = None,
        directory: Optional[str] = None,
    ):
        """Send a message to a session (fire-and-forget)."""

    @abstractmethod
    async def abort_session(self, session_id: str, directory: Optional[str] = None) -> bool:
        """Abort a running session. Returns True if successful."""

    @abstractmethod
    async def delete_session(self, session_id: str, directory: Optional[str] = None) -> bool:
        """Delete a session. Returns True if successful."""

    @abstractmethod
    async def cleanup_session(self, session_id: str, directory: Optional[str] = None):
        """Abort + delete a session. Best-effort, exceptions swallowed."""

    @abstractmethod
    async def get_session_status(self, session_id: str, directory: Optional[str] = None) -> Dict[str, Any]:
        """Get session status. Returns dict with {"type": "idle"|"busy"|"error"|"not_found"}."""

    @abstractmethod
    async def get_messages(self, session_id: str, directory: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get messages from a session."""

    @abstractmethod
    async def get_pending_permissions(self, directory: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get pending permission requests."""

    @abstractmethod
    async def reply_permission(self, request_id: str, reply: str, message: Optional[str] = None, directory: Optional[str] = None):
        """Reply to a permission request."""

    # ── Event streaming ───────────────────────────────────────────────

    def on(self, event_type: str, handler: Callable):
        """Register handler for a specific event type (e.g. "session.status")."""
        self._handlers[event_type] = handler

    def on_all(self, handler: Callable):
        """Register catch-all handler for all events."""
        self._handlers["*"] = handler

    async def _emit(self, event_type: str, properties: dict):
        """Emit an event to registered handlers."""
        handler = self._handlers.get(event_type)
        if handler:
            if inspect.iscoroutinefunction(handler):
                await handler(properties)
            else:
                handler(properties)

        all_handler = self._handlers.get("*")
        if all_handler:
            if inspect.iscoroutinefunction(all_handler):
                await all_handler(event_type, properties)
            else:
                all_handler(event_type, properties)

    @abstractmethod
    async def connect_with_reconnect(self, max_retries: int = -1, retry_delay: int = 5):
        """Start consuming events (blocks until stopped)."""

    @abstractmethod
    def stop(self):
        """Stop consuming events."""

    # ── Context manager ───────────────────────────────────────────────

    @abstractmethod
    async def __aenter__(self) -> Self: ...

    @abstractmethod
    async def __aexit__(self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: TracebackType | None) -> None: ...
