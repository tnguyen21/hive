"""Integration tests for the Hive orchestrator with fake OpenCode server."""

import asyncio
import pytest

from hive.opencode import OpenCodeClient


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fake_server_basic_functionality(fake_server):
    """Test that the fake OpenCode server works correctly.

    This validates the plumbing: start fake server, create a session via OpenCodeClient,
    inject an idle event, verify the SSE stream receives it.
    """
    # Create client pointing at fake server
    async with OpenCodeClient(base_url=fake_server.url) as client:
        # Create a session
        session = await client.create_session(title="Test Session")
        session_id = session["id"]

        # Verify session was created
        assert session_id.startswith("fake-")
        assert session_id in fake_server.get_created_sessions()

        # Get session status
        status = await client.get_session_status(session_id)
        assert status["id"] == session_id
        assert status["type"] == "running"

        # Set up SSE connection task
        events_received = []

        async def collect_events():
            """Connect to SSE and collect events."""
            from aiohttp import ClientSession, ClientTimeout

            timeout = ClientTimeout(total=5)
            async with ClientSession(timeout=timeout) as session:
                url = f"{fake_server.url}/session/{session_id}/events"
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    async for line in resp.content:
                        line = line.decode("utf-8").strip()
                        if line.startswith("data: "):
                            import json

                            data = line[6:]  # Strip "data: " prefix
                            try:
                                event = json.loads(data)
                                events_received.append(event)
                                # Stop when we get idle event
                                if event.get("type") == "session.status" and event.get("status") == "idle":
                                    break
                            except json.JSONDecodeError:
                                continue

        # Start collecting events
        event_task = asyncio.create_task(collect_events())

        # Give SSE connection time to establish
        await asyncio.sleep(0.1)

        # Inject an idle event
        fake_server.inject_idle(session_id)

        # Wait for event collection to complete
        try:
            await asyncio.wait_for(event_task, timeout=2.0)
        except asyncio.TimeoutError:
            pytest.fail("SSE event was not received within timeout")

        # Verify we received the idle event
        assert len(events_received) == 1
        assert events_received[0]["type"] == "session.status"
        assert events_received[0]["status"] == "idle"

        # Verify session status not changed by inject_idle (it only affects SSE events)
        status = await client.get_session_status(session_id)
        assert status["type"] == "running"

        # Test message endpoint
        await client.send_message_async(session_id, [{"type": "text", "text": "Hello world"}])

        # Test messages endpoint returns empty list
        messages = await client.get_messages(session_id)
        assert messages == []

        # Test abort session
        result = await client.abort_session(session_id)
        assert result is True

        # Verify session status changed to idle after abort
        status = await client.get_session_status(session_id)
        assert status["type"] == "idle"

        # Test delete session
        result = await client.delete_session(session_id)
        assert result is True
