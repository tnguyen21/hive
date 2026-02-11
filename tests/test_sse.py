"""Tests for SSE client."""

import asyncio
import json

import pytest

from hive.sse import SSEClient


@pytest.mark.asyncio
async def test_sse_client_initialization():
    """Test SSE client initialization."""
    client = SSEClient(base_url="http://localhost:4096")
    assert client.base_url == "http://localhost:4096"
    assert client.global_events is True
    assert client.handlers == {}


@pytest.mark.asyncio
async def test_sse_register_handler():
    """Test registering event handlers."""
    client = SSEClient()

    handler_called = False

    async def test_handler(properties):
        nonlocal handler_called
        handler_called = True

    client.on("session.status", test_handler)

    assert "session.status" in client.handlers


@pytest.mark.asyncio
async def test_sse_register_catch_all():
    """Test registering catch-all handler."""
    client = SSEClient()

    async def catch_all(event_type, properties):
        pass

    client.on_all(catch_all)

    assert "*" in client.handlers


@pytest.mark.asyncio
async def test_sse_dispatch_event():
    """Test event dispatching to handlers."""
    client = SSEClient()

    received_events = []

    async def handler(properties):
        received_events.append(("specific", properties))

    async def catch_all(event_type, properties):
        received_events.append(("catchall", event_type, properties))

    client.on("session.status", handler)
    client.on_all(catch_all)

    # Dispatch a test event
    event = {
        "type": "session.status",
        "properties": {"sessionID": "test123", "status": {"type": "idle"}},
    }
    await client._dispatch_event(event)

    # Both handlers should have been called
    assert len(received_events) == 2
    assert received_events[0][0] == "specific"
    assert received_events[0][1]["sessionID"] == "test123"
    assert received_events[1][0] == "catchall"
    assert received_events[1][1] == "session.status"


@pytest.mark.asyncio
async def test_sse_dispatch_sync_handler():
    """Test that synchronous handlers work."""
    client = SSEClient()

    received = []

    def sync_handler(properties):
        received.append(properties)

    client.on("test.event", sync_handler)

    event = {"type": "test.event", "properties": {"data": "value"}}
    await client._dispatch_event(event)

    assert len(received) == 1
    assert received[0]["data"] == "value"


@pytest.mark.asyncio
async def test_sse_stop():
    """Test stopping the SSE client."""
    client = SSEClient()
    client.running = True

    client.stop()

    assert client.running is False


# Integration test (requires running OpenCode server)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_sse_connect_and_receive():
    """Test connecting to SSE stream and receiving events (requires OpenCode server)."""
    client = SSEClient(global_events=True)

    events_received = []

    async def handler(event_type, properties):
        events_received.append(event_type)
        # Stop after receiving a few events
        if len(events_received) >= 3:
            client.stop()

    client.on_all(handler)

    # Connect for a short time
    try:
        await asyncio.wait_for(client.connect(), timeout=5.0)
    except asyncio.TimeoutError:
        pass

    # We should have received at least the connection event
    # (unless the server isn't running, in which case this test will fail)
    assert len(events_received) > 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_sse_session_status_event(tmp_path):
    """Test receiving session.status events (requires OpenCode server)."""
    from hive.opencode import OpenCodeClient

    # Create a session that will generate events
    async with OpenCodeClient() as opencode:
        session = await opencode.create_session(directory=str(tmp_path))
        session_id = session["id"]

        # Set up SSE client to listen for status events
        sse_client = SSEClient(global_events=True)
        status_events = []

        async def status_handler(properties):
            if properties.get("sessionID") == session_id:
                status_events.append(properties["status"])
                sse_client.stop()

        sse_client.on("session.status", status_handler)

        # Send an async message to trigger status change
        await opencode.send_message_async(
            session_id,
            parts=[{"type": "text", "text": "echo 'test'"}],
            directory=str(tmp_path),
        )

        # Connect to SSE stream
        try:
            await asyncio.wait_for(sse_client.connect(), timeout=10.0)
        except asyncio.TimeoutError:
            pass

        # Clean up
        await opencode.delete_session(session_id, directory=str(tmp_path))

        # We should have received at least one status event
        assert len(status_events) > 0
