"""Tests for OpenCode backend - merged opencode.py + sse.py implementation."""

import pytest

from hive.backend_opencode import OpenCodeBackend, SSEWatcher, make_model_config


class TestOpenCodeBackend:
    """Test OpenCodeBackend class functionality."""

    @pytest.mark.asyncio
    async def test_auth_header(self):
        """Test auth header generation."""
        backend = OpenCodeBackend(password="secret123")
        auth = backend._get_auth_header()

        assert "Authorization" in auth
        assert auth["Authorization"].startswith("Basic ")

    @pytest.mark.asyncio
    async def test_auth_header_no_password(self):
        """Test auth header when no password is set."""
        backend = OpenCodeBackend(password=None)
        auth = backend._get_auth_header()

        assert auth == {}

    @pytest.mark.asyncio
    async def test_context_manager(self):
        """Test async context manager functionality."""
        async with OpenCodeBackend(enable_sse=False) as backend:
            assert backend.session is not None
            assert backend.running is True
            assert backend._sse_task is None  # No SSE task when disabled

        # Should be stopped after context exit
        assert backend.session is None
        assert backend.running is False
        assert backend._sse_task is None

    @pytest.mark.asyncio
    async def test_sse_event_handlers(self):
        """Test SSE event handler registration."""
        backend = OpenCodeBackend()

        events_received = []
        all_events_received = []

        def status_handler(properties):
            events_received.append(("status", properties))

        def all_handler(event_type, properties):
            all_events_received.append((event_type, properties))

        # Register handlers
        backend.on("session.status", status_handler)
        backend.on_all(all_handler)

        assert "session.status" in backend._handlers
        assert "*" in backend._handlers

        # Test event dispatch
        test_event = {"payload": {"type": "session.status", "properties": {"sessionID": "test-123", "status": {"type": "idle"}}}}

        await backend._dispatch_event(test_event)

        # Check both handlers were called
        assert len(events_received) == 1
        assert events_received[0][0] == "status"
        assert events_received[0][1]["sessionID"] == "test-123"

        assert len(all_events_received) == 1
        assert all_events_received[0][0] == "session.status"
        assert all_events_received[0][1]["sessionID"] == "test-123"

    def test_make_model_config(self):
        """Test model config creation utility."""
        config = make_model_config("claude-3-5-sonnet-20241022")
        assert config == {"providerID": "anthropic", "modelID": "claude-3-5-sonnet-20241022"}

        config = make_model_config("gpt-4", "openai")
        assert config == {"providerID": "openai", "modelID": "gpt-4"}


class TestOpenCodeBackendWithFakeServer:
    """Integration tests using FakeOpenCodeServer."""

    @pytest.mark.asyncio
    async def test_create_session_with_directory_mapping(self, fake_server):
        """Test session creation stores directory mapping."""
        async with OpenCodeBackend(base_url=fake_server.url, enable_sse=False) as backend:
            session = await backend.create_session(directory="/tmp/test", title="Test session")

            session_id = session["id"]

            # Verify session was created
            assert "id" in session
            assert session["title"] == "Test session"
            assert session["directory"] == "/tmp/test"

            # Verify directory mapping was stored
            assert backend._get_session_directory(session_id) == "/tmp/test"

            # Clean up
            await backend.delete_session(session_id)

            # Verify mapping was removed after deletion
            assert backend._get_session_directory(session_id) is None

    @pytest.mark.asyncio
    async def test_get_session_status_uses_stored_directory(self, fake_server):
        """Test that get_session_status uses stored directory mapping."""
        async with OpenCodeBackend(base_url=fake_server.url, enable_sse=False) as backend:
            # Create session with directory
            session = await backend.create_session(directory="/tmp/test")
            session_id = session["id"]

            # Get status without providing directory - should use stored mapping
            status = await backend.get_session_status(session_id)

            assert "type" in status
            assert status["type"] in ["idle", "busy", "retry"]

            # Clean up
            await backend.delete_session(session_id)

    @pytest.mark.asyncio
    async def test_send_message_wrapper_functionality(self, fake_server):
        """Test send_message() wraps text into parts and model into config format."""
        async with OpenCodeBackend(base_url=fake_server.url, enable_sse=False) as backend:
            session = await backend.create_session(directory="/tmp/test")
            session_id = session["id"]

            # Test the convenience method that wraps text and model
            await backend.send_message(
                session_id, text="Hello, world!", model="claude-3-5-sonnet-20241022", system="You are a helpful assistant"
            )

            # Verify the message was received by fake server
            messages = fake_server.messages.get(session_id, [])
            assert len(messages) == 1

            message = messages[0]
            assert message["parts"] == [{"type": "text", "text": "Hello, world!"}]
            assert message["model"] == {"providerID": "anthropic", "modelID": "claude-3-5-sonnet-20241022"}
            assert message["system"] == "You are a helpful assistant"

            # Clean up
            await backend.delete_session(session_id)

    @pytest.mark.asyncio
    async def test_abort_and_delete_session(self, fake_server):
        """Test abort and delete session functionality."""
        async with OpenCodeBackend(base_url=fake_server.url, enable_sse=False) as backend:
            session = await backend.create_session(directory="/tmp/test")
            session_id = session["id"]

            # Abort session
            result = await backend.abort_session(session_id)
            assert result is True

            # Delete session
            result = await backend.delete_session(session_id)
            assert result is True

            # Verify session was removed from fake server
            assert session_id not in fake_server.sessions

    @pytest.mark.asyncio
    async def test_cleanup_session_best_effort(self, fake_server):
        """Test cleanup_session() swallows exceptions."""
        async with OpenCodeBackend(base_url=fake_server.url, enable_sse=False) as backend:
            session = await backend.create_session()
            session_id = session["id"]

            # Should not raise exception even if operations fail
            await backend.cleanup_session(session_id)

            # Should not raise exception even for non-existent session
            await backend.cleanup_session("non-existent-session")

    @pytest.mark.asyncio
    async def test_list_sessions(self, fake_server):
        """Test listing sessions."""
        async with OpenCodeBackend(base_url=fake_server.url, enable_sse=False) as backend:
            # Initially no sessions
            sessions = await backend.list_sessions()
            initial_count = len(sessions)

            # Create a session
            session = await backend.create_session(title="Test List Session")
            session_id = session["id"]

            # Should now have one more session
            sessions = await backend.list_sessions()
            assert len(sessions) == initial_count + 1

            # Find our session
            our_session = next(s for s in sessions if s["id"] == session_id)
            assert our_session["title"] == "Test List Session"

            # Clean up
            await backend.delete_session(session_id)

    @pytest.mark.asyncio
    async def test_get_messages(self, fake_server):
        """Test getting messages from a session."""
        async with OpenCodeBackend(base_url=fake_server.url, enable_sse=False) as backend:
            session = await backend.create_session()
            session_id = session["id"]

            # Send a message
            await backend.send_message(session_id, "Test message")

            # Get messages
            messages = await backend.get_messages(session_id)

            # The fake server should have stored our message
            assert len(messages) >= 1

            # Clean up
            await backend.delete_session(session_id)

    @pytest.mark.asyncio
    async def test_runtime_error_without_start(self):
        """Test that methods raise RuntimeError when backend not started."""
        backend = OpenCodeBackend()

        with pytest.raises(RuntimeError, match="Backend not started"):
            await backend.list_sessions()

        with pytest.raises(RuntimeError, match="Backend not started"):
            await backend.create_session()

        with pytest.raises(RuntimeError, match="Backend not started"):
            await backend.send_message_async("test", [])

        with pytest.raises(RuntimeError, match="Backend not started"):
            await backend.get_session_status("test")


class TestSSEEventParsing:
    """Test SSE event parsing and dispatch."""

    @pytest.mark.asyncio
    async def test_sse_event_unwrapping(self):
        """Test SSE event unwrapping from OpenCode envelope format."""
        backend = OpenCodeBackend()

        events_received = []

        def handler(properties):
            events_received.append(properties)

        backend.on("session.status", handler)

        # Test wrapped event (typical OpenCode format)
        wrapped_event = {
            "directory": "/tmp/project",
            "payload": {"type": "session.status", "properties": {"sessionID": "test-123", "status": {"type": "busy"}}},
        }

        await backend._dispatch_event(wrapped_event)

        assert len(events_received) == 1
        assert events_received[0]["sessionID"] == "test-123"
        assert events_received[0]["status"]["type"] == "busy"

        # Test unwrapped event (fallback format)
        events_received.clear()
        unwrapped_event = {"type": "session.status", "properties": {"sessionID": "test-456", "status": {"type": "idle"}}}

        await backend._dispatch_event(unwrapped_event)

        assert len(events_received) == 1
        assert events_received[0]["sessionID"] == "test-456"
        assert events_received[0]["status"]["type"] == "idle"

    @pytest.mark.asyncio
    async def test_sync_and_async_handlers(self):
        """Test both sync and async event handlers work."""
        backend = OpenCodeBackend()

        sync_events = []
        async_events = []

        def sync_handler(properties):
            sync_events.append(properties)

        async def async_handler(properties):
            async_events.append(properties)

        backend.on("test.sync", sync_handler)
        backend.on("test.async", async_handler)

        # Dispatch events
        await backend._dispatch_event({"payload": {"type": "test.sync", "properties": {"data": "sync_data"}}})

        await backend._dispatch_event({"payload": {"type": "test.async", "properties": {"data": "async_data"}}})

        assert len(sync_events) == 1
        assert sync_events[0]["data"] == "sync_data"

        assert len(async_events) == 1
        assert async_events[0]["data"] == "async_data"


class TestSSEWatcher:
    """Test SSEWatcher utility class."""

    @pytest.mark.asyncio
    async def test_sse_watcher_event_dispatch(self):
        """Test SSEWatcher event dispatch."""
        watcher = SSEWatcher()

        events_received = []
        all_events_received = []

        def status_handler(properties):
            events_received.append(properties)

        def all_handler(event_type, properties):
            all_events_received.append((event_type, properties))

        watcher.on("session.status", status_handler)
        watcher.on_all(all_handler)

        # Dispatch event
        test_event = {"payload": {"type": "session.status", "properties": {"sessionID": "watch-123", "status": {"type": "idle"}}}}

        await watcher._dispatch_event(test_event)

        # Check handlers were called
        assert len(events_received) == 1
        assert events_received[0]["sessionID"] == "watch-123"

        assert len(all_events_received) == 1
        assert all_events_received[0][0] == "session.status"
        assert all_events_received[0][1]["sessionID"] == "watch-123"
