"""OpenCode HTTP client wrapper."""

import base64
import os
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import aiohttp


class OpenCodeClient:
    """HTTP client for OpenCode server API."""

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
        self.session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self.session:
            await self.session.close()

    async def create_session(
        self,
        directory: Optional[str] = None,
        title: Optional[str] = None,
        permissions: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        """
        Create a new OpenCode session.

        Args:
            directory: Project directory to scope the session to
            title: Session title (auto-generated if omitted)
            permissions: Permission rules for the session

        Returns:
            Session info dict with id, title, directory, etc.
        """
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

    async def send_message(
        self,
        session_id: str,
        parts: List[Dict[str, Any]],
        agent: str = "build",
        model: Optional[Dict[str, str]] = None,
        system: Optional[str] = None,
        directory: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Send a message to a session (synchronous - blocks until complete).

        Args:
            session_id: Session ID
            parts: Message parts (e.g., [{"type": "text", "text": "..."}])
            agent: Agent type (default: "build")
            model: Model config dict with providerID and modelID
            system: Additional system prompt
            directory: Directory context for this request

        Returns:
            Full message response with info and parts
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

        url = f"{self.base_url}/session/{session_id}/message"
        async with self.session.post(url, json=payload, headers=headers) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def send_message_async(
        self,
        session_id: str,
        parts: List[Dict[str, Any]],
        agent: str = "build",
        directory: Optional[str] = None,
    ):
        """
        Send a message asynchronously (fire-and-forget).

        Returns immediately with HTTP 204. Monitor progress via SSE.

        Args:
            session_id: Session ID
            parts: Message parts
            agent: Agent type (default: "build")
            directory: Directory context for this request
        """
        if not self.session:
            raise RuntimeError("Client not initialized. Use async with context manager.")

        headers = {
            **self._get_auth_header(),
            **self._get_directory_header(directory),
            "Content-Type": "application/json",
        }

        payload = {"parts": parts, "agent": agent}

        url = f"{self.base_url}/session/{session_id}/prompt_async"
        async with self.session.post(url, json=payload, headers=headers) as resp:
            resp.raise_for_status()

    async def abort_session(self, session_id: str, directory: Optional[str] = None) -> bool:
        """
        Abort a running session.

        Args:
            session_id: Session ID to abort
            directory: Directory context

        Returns:
            True if aborted successfully
        """
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
        """
        Delete a session.

        Args:
            session_id: Session ID to delete
            directory: Directory context

        Returns:
            True if deleted successfully
        """
        if not self.session:
            raise RuntimeError("Client not initialized. Use async with context manager.")

        headers = {
            **self._get_auth_header(),
            **self._get_directory_header(directory),
        }

        url = f"{self.base_url}/session/{session_id}"
        async with self.session.delete(url, headers=headers) as resp:
            return resp.status == 200

    async def get_session_status(
        self, session_id: str, directory: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get session status (idle/busy/retry).

        Args:
            session_id: Session ID
            directory: Directory context

        Returns:
            Status dict with type field
        """
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

    async def get_session(
        self, session_id: str, directory: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get session info.

        Args:
            session_id: Session ID
            directory: Directory context

        Returns:
            Session info dict
        """
        if not self.session:
            raise RuntimeError("Client not initialized. Use async with context manager.")

        headers = {
            **self._get_auth_header(),
            **self._get_directory_header(directory),
        }

        url = f"{self.base_url}/session/{session_id}"
        async with self.session.get(url, headers=headers) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_messages(
        self, session_id: str, directory: Optional[str] = None, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Get all messages in a session.

        Args:
            session_id: Session ID
            directory: Directory context
            limit: Maximum number of messages to return

        Returns:
            List of message dicts
        """
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

    async def list_sessions(
        self, directory: Optional[str] = None, limit: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        List all sessions.

        Args:
            directory: Filter by directory
            limit: Maximum number of sessions to return

        Returns:
            List of session info dicts
        """
        if not self.session:
            raise RuntimeError("Client not initialized. Use async with context manager.")

        headers = {
            **self._get_auth_header(),
            **self._get_directory_header(directory),
        }

        url = f"{self.base_url}/session"
        params = {}
        if limit:
            params["limit"] = limit
        if params:
            url += f"?{urlencode(params)}"

        async with self.session.get(url, headers=headers) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_pending_permissions(
        self, directory: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Get all pending permission requests.

        Args:
            directory: Filter by directory

        Returns:
            List of pending permission request dicts
        """
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
        """
        Reply to a permission request.

        Args:
            request_id: Permission request ID
            reply: "once", "always", or "reject"
            message: Optional message when rejecting
            directory: Directory context
        """
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
