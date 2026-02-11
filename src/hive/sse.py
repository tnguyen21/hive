"""SSE (Server-Sent Events) client for OpenCode event stream."""

import asyncio
import json
from typing import Any, Callable, Dict, Optional

import aiohttp


class SSEClient:
    """
    SSE client for consuming OpenCode events.

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
        """
        Initialize SSE client.

        Args:
            base_url: Base URL of OpenCode server
            password: Server password
            global_events: If True, connect to /global/event; else /event
            directory: Directory to scope events to (only for /event endpoint)
        """
        self.base_url = base_url.rstrip("/")
        self.password = password
        self.global_events = global_events
        self.directory = directory
        self.handlers: Dict[str, Callable] = {}
        self.running = False
        self.session: Optional[aiohttp.ClientSession] = None

    def on(self, event_type: str, handler: Callable[[Dict[str, Any]], None]):
        """
        Register an event handler.

        Args:
            event_type: Event type to listen for (e.g., "session.status")
            handler: Async or sync callback function that receives event properties
        """
        self.handlers[event_type] = handler

    def on_all(self, handler: Callable[[str, Dict[str, Any]], None]):
        """
        Register a catch-all event handler.

        Args:
            handler: Async or sync callback function that receives (event_type, properties)
        """
        self.handlers["*"] = handler

    async def connect(self):
        """
        Connect to SSE stream and start consuming events.

        This method will run until stop() is called or the connection fails.
        """
        if self.global_events:
            url = f"{self.base_url}/global/event"
        else:
            url = f"{self.base_url}/event"
            if self.directory:
                url += f"?directory={self.directory}"

        headers = {}
        if self.password:
            import base64
            import os

            username = os.environ.get("OPENCODE_SERVER_USERNAME", "opencode")
            credentials = f"{username}:{self.password}"
            encoded = base64.b64encode(credentials.encode()).decode()
            headers["Authorization"] = f"Basic {encoded}"

        self.running = True
        self.session = aiohttp.ClientSession()

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
                            # Skip malformed events
                            continue
        finally:
            await self.session.close()
            self.session = None

    async def _dispatch_event(self, event: Dict[str, Any]):
        """
        Dispatch an event to registered handlers.

        Args:
            event: Event dict with "type" and "properties" fields
        """
        event_type = event.get("type")
        properties = event.get("properties", {})

        # Call specific handler if registered
        if event_type in self.handlers:
            handler = self.handlers[event_type]
            if asyncio.iscoroutinefunction(handler):
                await handler(properties)
            else:
                handler(properties)

        # Call catch-all handler if registered
        if "*" in self.handlers:
            handler = self.handlers["*"]
            if asyncio.iscoroutinefunction(handler):
                await handler(event_type, properties)
            else:
                handler(event_type, properties)

    def stop(self):
        """Stop consuming events and close connection."""
        self.running = False

    async def connect_with_reconnect(
        self, max_retries: int = -1, retry_delay: int = 5
    ):
        """
        Connect with automatic reconnection on failure.

        Args:
            max_retries: Maximum retry attempts (-1 for infinite)
            retry_delay: Seconds to wait between retries
        """
        retries = 0
        while self.running and (max_retries < 0 or retries < max_retries):
            try:
                await self.connect()
            except Exception as e:
                retries += 1
                if max_retries >= 0 and retries >= max_retries:
                    raise

                # Wait before reconnecting
                await asyncio.sleep(retry_delay)
