"""Abstract base class for Hive backends.

A backend provides two capabilities to the orchestrator:
1. Session management (create, send messages, abort, delete, get status/messages)
2. Event streaming (register handlers, connect, stop)

The OpenCode backend splits these across two classes (OpenCodeClient + SSEClient)
because OpenCode uses HTTP REST for session management and a separate SSE endpoint
for events. The Claude backend combines both into a single class because its
WebSocket connections carry both commands and events.

The orchestrator accepts both via its constructor:
    opencode_client: HiveBackend   — session management
    sse_client: HiveBackend        — event streaming (same object for Claude)
"""

from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional


class HiveBackend(ABC):
    """Interface that all Hive backends must implement."""

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
        model: Optional[Dict[str, str]] = None,
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
        """Get session status. Returns dict with {"type": "idle"|"busy"|"error"}."""

    @abstractmethod
    async def get_messages(
        self,
        session_id: str,
        directory: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Get messages from a session."""

    @abstractmethod
    async def get_pending_permissions(self, directory: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get pending permission requests."""

    @abstractmethod
    async def reply_permission(
        self,
        request_id: str,
        reply: str,
        message: Optional[str] = None,
        directory: Optional[str] = None,
    ):
        """Reply to a permission request."""

    # ── Event streaming ───────────────────────────────────────────────

    @abstractmethod
    def on(self, event_type: str, handler: Callable):
        """Register handler for a specific event type (e.g. "session.status")."""

    @abstractmethod
    def on_all(self, handler: Callable):
        """Register catch-all handler for all events."""

    @abstractmethod
    async def connect_with_reconnect(self, max_retries: int = -1, retry_delay: int = 5):
        """Start consuming events (blocks until stopped)."""

    @abstractmethod
    def stop(self):
        """Stop consuming events."""

    # ── Context manager ───────────────────────────────────────────────

    @abstractmethod
    async def __aenter__(self): ...

    @abstractmethod
    async def __aexit__(self, exc_type, exc_val, exc_tb): ...
