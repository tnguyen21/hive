"""OpenCode backend: HTTP REST API for session management + SSE for event streaming.

OpenCode is a standalone server (https://github.com/sst/opencode) that acts as middleware
between Hive and the Anthropic API. Hive sends HTTP requests to manage sessions and
messages, and consumes a Server-Sent Events (SSE) stream for real-time event
notifications (session status changes, permission requests, etc.).

Architecture:
    Hive ──HTTP──> OpenCode server ──API──> Anthropic
    Hive <──SSE──  OpenCode server  (event stream)

The orchestrator instantiates two objects from this module:
    - OpenCodeClient  — HTTP client for session CRUD and message sending
    - SSEClient       — event stream consumer for real-time notifications

Both are passed separately to the Orchestrator constructor because they have
independent lifecycles (SSE reconnects independently of HTTP requests).
"""

import asyncio
import base64
import inspect
import json
import os
from typing import Any, Callable, Dict, List, Optional

import aiohttp

from .base import HiveBackend


def make_model_config(model_id: str, provider_id: str = "anthropic") -> Dict[str, str]:
    """Build a model config dict from a model ID string."""
    return {"providerID": provider_id, "modelID": model_id}


class OpenCodeClient(HiveBackend):
    """HTTP client for OpenCode server API (session management half of the backend)."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:4096",
        password: Optional[str] = None,
    ):
        """
        Initialize OpenCode client.

        Args:
            base_url: Base URL of OpenCode server
            password: Server password (reads from OPENCODE_SERVER_PASSWORD if not provided)
        """
        self.base_url = base_url.rstrip("/")
        self.password = password or os.environ.get("OPENCODE_SERVER_PASSWORD")
        self.session: Optional[aiohttp.ClientSession] = None

    def _get_auth_header(self) -> Dict[str, str]:
        """Generate Authorization header for HTTP Basic Auth."""
        if not self.password:
            return {}

        username = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
        credentials = f"{username}:{self.password}"
        encoded = base64.b64encode(credentials.encode()).decode()
        return {"Authorization": f"Basic {encoded}"}

    def _get_directory_header(self, directory: Optional[str]) -> Dict[str, str]:
        """Generate X-OpenCode-Directory header if directory is specified."""
        if directory:
            return {"X-OpenCode-Directory": directory}
        return {}

    async def __aenter__(self):
        """Async context manager entry."""
        timeout = aiohttp.ClientTimeout(total=30)
        self.session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self.session:
            await self.session.close()

    async def list_sessions(self) -> List[Dict[str, Any]]:
        if not self.session:
            raise RuntimeError("Client not initialized. Use async with context manager.")

        headers = {**self._get_auth_header()}

        url = f"{self.base_url}/session"
        async with self.session.get(url, headers=headers) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def create_session(
        self,
        directory: Optional[str] = None,
        title: Optional[str] = None,
        permissions: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        if not self.session:
            raise RuntimeError("Client not initialized. Use async with context manager.")

        headers = {
            **self._get_auth_header(),
            **self._get_directory_header(directory),
            "Content-Type": "application/json",
        }

        payload = {}
        if title:
            payload["title"] = title
        if permissions:
            payload["permission"] = permissions

        url = f"{self.base_url}/session"
        async with self.session.post(url, json=payload, headers=headers) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def send_message_async(
        self,
        session_id: str,
        parts: List[Dict[str, Any]],
        agent: str = "build",
        model: Optional[Dict[str, str]] = None,
        system: Optional[str] = None,
        directory: Optional[str] = None,
    ):
        """
        Send a message asynchronously (fire-and-forget).

        Returns immediately with HTTP 204. Monitor progress via SSE.
        """
        if not self.session:
            raise RuntimeError("Client not initialized. Use async with context manager.")

        headers = {
            **self._get_auth_header(),
            **self._get_directory_header(directory),
            "Content-Type": "application/json",
        }

        payload = {"parts": parts, "agent": agent}
        if model:
            payload["model"] = model
        if system:
            payload["system"] = system

        url = f"{self.base_url}/session/{session_id}/prompt_async"
        async with self.session.post(url, json=payload, headers=headers) as resp:
            resp.raise_for_status()

    async def abort_session(self, session_id: str, directory: Optional[str] = None) -> bool:
        if not self.session:
            raise RuntimeError("Client not initialized. Use async with context manager.")

        headers = {
            **self._get_auth_header(),
            **self._get_directory_header(directory),
        }

        url = f"{self.base_url}/session/{session_id}/abort"
        async with self.session.post(url, headers=headers) as resp:
            return resp.status == 200

    async def delete_session(self, session_id: str, directory: Optional[str] = None) -> bool:
        if not self.session:
            raise RuntimeError("Client not initialized. Use async with context manager.")

        headers = {
            **self._get_auth_header(),
            **self._get_directory_header(directory),
        }

        url = f"{self.base_url}/session/{session_id}"
        async with self.session.delete(url, headers=headers) as resp:
            return resp.status == 200

    async def get_session_status(self, session_id: str, directory: Optional[str] = None) -> Dict[str, Any]:
        if not self.session:
            raise RuntimeError("Client not initialized. Use async with context manager.")

        headers = {
            **self._get_auth_header(),
            **self._get_directory_header(directory),
        }

        url = f"{self.base_url}/session/{session_id}/status"
        async with self.session.get(url, headers=headers) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_messages(
        self,
        session_id: str,
        directory: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        if not self.session:
            raise RuntimeError("Client not initialized. Use async with context manager.")

        headers = {
            **self._get_auth_header(),
            **self._get_directory_header(directory),
        }

        url = f"{self.base_url}/session/{session_id}/message"
        if limit:
            url += f"?limit={limit}"

        async with self.session.get(url, headers=headers) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_pending_permissions(self, directory: Optional[str] = None) -> List[Dict[str, Any]]:
        if not self.session:
            raise RuntimeError("Client not initialized. Use async with context manager.")

        headers = {
            **self._get_auth_header(),
            **self._get_directory_header(directory),
        }

        url = f"{self.base_url}/permission"
        async with self.session.get(url, headers=headers) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def reply_permission(
        self,
        request_id: str,
        reply: str,
        message: Optional[str] = None,
        directory: Optional[str] = None,
    ):
        if not self.session:
            raise RuntimeError("Client not initialized. Use async with context manager.")

        headers = {
            **self._get_auth_header(),
            **self._get_directory_header(directory),
            "Content-Type": "application/json",
        }

        payload = {"reply": reply}
        if message:
            payload["message"] = message

        url = f"{self.base_url}/permission/{request_id}/reply"
        async with self.session.post(url, json=payload, headers=headers) as resp:
            resp.raise_for_status()

    async def cleanup_session(self, session_id: str, directory: Optional[str] = None):
        """Abort and delete a session. Best-effort — exceptions are swallowed."""
        try:
            await self.abort_session(session_id, directory=directory)
        except Exception:
            pass
        try:
            await self.delete_session(session_id, directory=directory)
        except Exception:
            pass

    # ── Event streaming stubs (not used — SSEClient handles events) ───

    def on(self, event_type: str, handler: Callable):
        raise NotImplementedError("Use SSEClient for event streaming with the OpenCode backend")

    def on_all(self, handler: Callable):
        raise NotImplementedError("Use SSEClient for event streaming with the OpenCode backend")

    async def connect_with_reconnect(self, max_retries: int = -1, retry_delay: int = 5):
        raise NotImplementedError("Use SSEClient for event streaming with the OpenCode backend")

    def stop(self):
        raise NotImplementedError("Use SSEClient for event streaming with the OpenCode backend")


class SSEClient(HiveBackend):
    """SSE event stream consumer (event streaming half of the OpenCode backend).

    Connects to the OpenCode SSE endpoint and dispatches events to registered handlers.
    Can connect to either:
    - /global/event (all events from all directories)
    - /event?directory=... (events from specific directory)
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:4096",
        password: Optional[str] = None,
        global_events: bool = True,
        directory: Optional[str] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.password = password
        self.global_events = global_events
        self.directory = directory
        self.handlers: Dict[str, Callable] = {}
        self.running = False
        self.session: Optional[aiohttp.ClientSession] = None

    def on(self, event_type: str, handler: Callable[[Dict[str, Any]], None]):
        """Register an event handler for a specific event type (e.g. "session.status")."""
        self.handlers[event_type] = handler

    def on_all(self, handler: Callable[[str, Dict[str, Any]], None]):
        """Register a catch-all event handler that receives (event_type, properties)."""
        self.handlers["*"] = handler

    async def connect(self):
        """Connect to SSE stream and start consuming events.

        Runs until stop() is called or the connection fails.
        Note: self.running must be set by the caller (connect_with_reconnect
        or direct usage). We do NOT reset it here to avoid defeating stop().
        """
        if self.global_events:
            url = f"{self.base_url}/global/event"
        else:
            url = f"{self.base_url}/event"
            if self.directory:
                url += f"?directory={self.directory}"

        headers = {}
        if self.password:
            username = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
            credentials = f"{username}:{self.password}"
            encoded = base64.b64encode(credentials.encode()).decode()
            headers["Authorization"] = f"Basic {encoded}"

        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=30)
        self.session = aiohttp.ClientSession(timeout=timeout)

        try:
            async with self.session.get(url, headers=headers) as resp:
                resp.raise_for_status()

                async for line in resp.content:
                    if not self.running:
                        break

                    line = line.decode("utf-8").strip()

                    # SSE format: "data: {json}"
                    if line.startswith("data: "):
                        data = line[6:]  # Strip "data: " prefix
                        try:
                            event = json.loads(data)
                            await self._dispatch_event(event)
                        except json.JSONDecodeError:
                            continue
        finally:
            await self.session.close()
            self.session = None

    async def _dispatch_event(self, event: Dict[str, Any]):
        """Dispatch an event to registered handlers.

        OpenCode wraps events in a payload envelope:
            {"directory": "...", "payload": {"type": "...", "properties": {...}}}
        Unwrap if present, otherwise fall back to top-level fields.
        """
        payload = event.get("payload", event)
        event_type = payload.get("type")
        properties = payload.get("properties", {})

        if event_type in self.handlers:
            handler = self.handlers[event_type]
            if inspect.iscoroutinefunction(handler):
                await handler(properties)
            else:
                handler(properties)

        if "*" in self.handlers:
            handler = self.handlers["*"]
            if inspect.iscoroutinefunction(handler):
                await handler(event_type, properties)
            else:
                handler(event_type, properties)

    def stop(self):
        """Stop consuming events and close connection."""
        self.running = False

    async def connect_with_reconnect(self, max_retries: int = -1, retry_delay: int = 5):
        """Connect with automatic reconnection on failure."""
        self.running = True
        retries = 0
        while self.running and (max_retries < 0 or retries < max_retries):
            try:
                await self.connect()
            except Exception:
                retries += 1
                if max_retries >= 0 and retries >= max_retries:
                    raise

                await asyncio.sleep(retry_delay)

    # ── Session management stubs (not used — OpenCodeClient handles sessions) ─

    async def list_sessions(self) -> List[Dict[str, Any]]:
        raise NotImplementedError("Use OpenCodeClient for session management")

    async def create_session(self, **kwargs) -> Dict[str, Any]:
        raise NotImplementedError("Use OpenCodeClient for session management")

    async def send_message_async(self, session_id: str, parts: List[Dict[str, Any]], **kwargs):
        raise NotImplementedError("Use OpenCodeClient for session management")

    async def abort_session(self, session_id: str, **kwargs) -> bool:
        raise NotImplementedError("Use OpenCodeClient for session management")

    async def delete_session(self, session_id: str, **kwargs) -> bool:
        raise NotImplementedError("Use OpenCodeClient for session management")

    async def cleanup_session(self, session_id: str, **kwargs):
        raise NotImplementedError("Use OpenCodeClient for session management")

    async def get_session_status(self, session_id: str, **kwargs) -> Dict[str, Any]:
        raise NotImplementedError("Use OpenCodeClient for session management")

    async def get_messages(self, session_id: str, **kwargs) -> List[Dict[str, Any]]:
        raise NotImplementedError("Use OpenCodeClient for session management")

    async def get_pending_permissions(self, **kwargs) -> List[Dict[str, Any]]:
        raise NotImplementedError("Use OpenCodeClient for session management")

    async def reply_permission(self, request_id: str, reply: str, **kwargs):
        raise NotImplementedError("Use OpenCodeClient for session management")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.stop()
