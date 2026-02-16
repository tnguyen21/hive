"""Tests for ClaudeWSBackend."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
import pytest_asyncio

from hive.backends import ClaudeWSBackend, SessionState


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def backend():
    """Create a ClaudeWSBackend with server started."""
    b = ClaudeWSBackend(host="127.0.0.1", port=0)  # port=0 won't bind; tests mock the server
    # Pre-set server_ready so create_session doesn't block waiting for server
    b.server_ready.set()
    yield b
    # Cleanup
    for sid in list(b.sessions):
        b.sessions.pop(sid, None)


@pytest_asyncio.fixture
async def running_backend():
    """Create a ClaudeWSBackend with WS server actually running."""
    b = ClaudeWSBackend(host="127.0.0.1", port=0)

    # Start the server on an ephemeral port
    runner = aiohttp.web.AppRunner(b.app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()

    # Extract actual port
    sock = site._server.sockets[0]
    actual_port = sock.getsockname()[1]
    b.port = actual_port
    b.host = "127.0.0.1"
    b.server_ready.set()
    b.running = True

    yield b

    b.running = False
    for sid in list(b.sessions):
        try:
            await b.delete_session(sid)
        except Exception:
            pass
    await runner.cleanup()


# ── SessionState tests ────────────────────────────────────────────────


def test_session_state_defaults():
    """SessionState has sane defaults."""
    s = SessionState()
    assert s.status == "idle"
    assert s.messages == []
    assert s.result is None
    assert s.initialized is False
    assert not s.connected.is_set()


# ── Message routing tests ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_route_system_init(backend):
    """system/init message sets cli_session_id and signals connected."""
    session_id = "test-session"
    backend.sessions[session_id] = SessionState()

    msg = {"type": "system", "subtype": "init", "session_id": "cli-abc123"}
    await backend._route_message(session_id, msg)

    session = backend.sessions[session_id]
    assert session.cli_session_id == "cli-abc123"
    assert session.connected.is_set()


@pytest.mark.asyncio
async def test_route_assistant_message(backend):
    """assistant messages are collected."""
    session_id = "test-session"
    backend.sessions[session_id] = SessionState()
    backend.sessions[session_id].connected.set()

    msg = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "Hello"}],
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "model": "claude-sonnet-4-20250514",
        },
    }
    await backend._route_message(session_id, msg)

    assert len(backend.sessions[session_id].messages) == 1
    assert backend.sessions[session_id].messages[0]["type"] == "assistant"


@pytest.mark.asyncio
async def test_route_result_message(backend):
    """result message sets status to idle and emits event."""
    session_id = "test-session"
    backend.sessions[session_id] = SessionState(status="busy")

    events_received = []

    async def handler(props):
        events_received.append(props)

    backend.on("session.status", handler)

    msg = {
        "type": "result",
        "usage": {"input_tokens": 500, "output_tokens": 200},
    }
    await backend._route_message(session_id, msg)

    session = backend.sessions[session_id]
    assert session.status == "idle"
    assert session.result == msg
    assert len(session.messages) == 1

    # Check SSE event was emitted
    assert len(events_received) == 1
    assert events_received[0]["sessionID"] == session_id
    assert events_received[0]["status"]["type"] == "idle"


@pytest.mark.asyncio
async def test_route_control_request_can_use_tool(backend):
    """control_request/can_use_tool gets auto-approved."""
    session_id = "test-session"
    ws_mock = AsyncMock()
    ws_mock.closed = False
    backend.sessions[session_id] = SessionState(ws=ws_mock)

    msg = {
        "type": "control_request",
        "request_id": "req-123",
        "request": {
            "subtype": "can_use_tool",
            "input": {"tool": "bash", "command": "ls"},
        },
    }
    await backend._route_message(session_id, msg)

    # Should have sent a control_response
    ws_mock.send_str.assert_called_once()
    sent = json.loads(ws_mock.send_str.call_args[0][0].strip())
    assert sent["type"] == "control_response"
    assert sent["response"]["response"]["behavior"] == "allow"


@pytest.mark.asyncio
async def test_route_keepalive_ignored(backend):
    """keep_alive messages are silently ignored."""
    session_id = "test-session"
    backend.sessions[session_id] = SessionState()

    msg = {"type": "keep_alive"}
    await backend._route_message(session_id, msg)

    # No crash, no messages stored
    assert len(backend.sessions[session_id].messages) == 0


@pytest.mark.asyncio
async def test_route_unknown_session(backend):
    """Messages for unknown sessions are ignored."""
    await backend._route_message("nonexistent", {"type": "system", "subtype": "init"})
    # No crash


# ── Message translation tests ────────────────────────────────────────


def test_translate_assistant_message(backend):
    """assistant message translates to OpenCode format."""
    msg = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "Hello"}],
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "model": "claude-sonnet-4-20250514",
        },
    }
    translated = backend._translate_message(msg)

    assert translated["role"] == "assistant"
    assert translated["content"] == [{"type": "text", "text": "Hello"}]
    assert translated["metadata"]["input_tokens"] == 100
    assert translated["metadata"]["output_tokens"] == 50
    assert translated["metadata"]["model"] == "claude-sonnet-4-20250514"


def test_translate_result_message(backend):
    """result message translates to OpenCode format."""
    msg = {
        "type": "result",
        "usage": {"input_tokens": 500, "output_tokens": 200},
    }
    translated = backend._translate_message(msg)

    assert translated["role"] == "result"
    assert translated["metadata"]["input_tokens"] == 500
    assert translated["metadata"]["output_tokens"] == 200


def test_translate_unknown_message(backend):
    """Unknown message types pass through unchanged."""
    msg = {"type": "unknown", "data": "foo"}
    translated = backend._translate_message(msg)
    assert translated == msg


# ── SSE-compatible event handler tests ────────────────────────────────


@pytest.mark.asyncio
async def test_on_handler(backend):
    """Registered handlers are called for matching events."""
    received = []

    async def handler(props):
        received.append(props)

    backend.on("session.status", handler)
    await backend._emit("session.status", {"test": True})

    assert len(received) == 1
    assert received[0]["test"] is True


@pytest.mark.asyncio
async def test_on_all_handler(backend):
    """Catch-all handler receives all events."""
    received = []

    async def handler(event_type, props):
        received.append((event_type, props))

    backend.on_all(handler)
    await backend._emit("session.status", {"test": True})
    await backend._emit("session.error", {"err": "oops"})

    assert len(received) == 2
    assert received[0][0] == "session.status"
    assert received[1][0] == "session.error"


@pytest.mark.asyncio
async def test_sync_handler(backend):
    """Sync handlers also work."""
    received = []

    def handler(props):
        received.append(props)

    backend.on("session.status", handler)
    await backend._emit("session.status", {"sync": True})

    assert len(received) == 1


# ── Session lifecycle tests ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_session_status_unknown(backend):
    """Unknown session returns idle."""
    status = await backend.get_session_status("nonexistent")
    assert status["type"] == "idle"


@pytest.mark.asyncio
async def test_get_session_status_dead_process(backend):
    """Dead process returns error status."""
    session_id = "test-session"
    proc = MagicMock()
    proc.returncode = 1  # process has exited
    backend.sessions[session_id] = SessionState(process=proc)

    status = await backend.get_session_status(session_id)
    assert status["type"] == "error"


@pytest.mark.asyncio
async def test_get_session_status_busy(backend):
    """Busy session returns busy status."""
    session_id = "test-session"
    proc = MagicMock()
    proc.returncode = None  # process still running
    backend.sessions[session_id] = SessionState(process=proc, status="busy")

    status = await backend.get_session_status(session_id)
    assert status["type"] == "busy"


@pytest.mark.asyncio
async def test_get_messages_empty(backend):
    """Empty session returns empty messages."""
    msgs = await backend.get_messages("nonexistent")
    assert msgs == []


@pytest.mark.asyncio
async def test_get_messages_with_limit(backend):
    """Message limit is respected."""
    session_id = "test-session"
    backend.sessions[session_id] = SessionState()
    backend.sessions[session_id].messages = [
        {"type": "assistant", "message": {"role": "assistant", "content": [], "usage": {}}},
        {"type": "assistant", "message": {"role": "assistant", "content": [], "usage": {}}},
        {"type": "result", "usage": {}},
    ]

    msgs = await backend.get_messages(session_id, limit=1)
    assert len(msgs) == 1


@pytest.mark.asyncio
async def test_delete_session_unknown(backend):
    """Deleting unknown session returns False."""
    result = await backend.delete_session("nonexistent")
    assert result is False


@pytest.mark.asyncio
async def test_abort_session_no_ws(backend):
    """Aborting session without WS returns False."""
    session_id = "test-session"
    backend.sessions[session_id] = SessionState()

    result = await backend.abort_session(session_id)
    assert result is False


@pytest.mark.asyncio
async def test_cleanup_session_swallows_errors(backend):
    """cleanup_session never raises."""
    # Should not raise even for nonexistent session
    await backend.cleanup_session("nonexistent")


@pytest.mark.asyncio
async def test_list_sessions_filters_dead(backend):
    """list_sessions only returns sessions with live processes."""
    live_proc = MagicMock()
    live_proc.returncode = None  # alive

    dead_proc = MagicMock()
    dead_proc.returncode = 1  # dead

    backend.sessions["live"] = SessionState(process=live_proc, title="Live")
    backend.sessions["dead"] = SessionState(process=dead_proc, title="Dead")
    backend.sessions["no-proc"] = SessionState(title="No Process")

    sessions = await backend.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["id"] == "live"


@pytest.mark.asyncio
async def test_get_pending_permissions_noop(backend):
    """get_pending_permissions returns empty list."""
    result = await backend.get_pending_permissions()
    assert result == []


@pytest.mark.asyncio
async def test_reply_permission_noop(backend):
    """reply_permission is a no-op."""
    await backend.reply_permission("req-1", "once")  # Should not raise


# ── send_message_async tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_message_async_unknown_session(backend):
    """send_message_async raises for unknown session."""
    with pytest.raises(ValueError, match="not found"):
        await backend.send_message_async("nonexistent", [{"type": "text", "text": "hi"}])


@pytest.mark.asyncio
async def test_send_message_async_sends_user_message(backend):
    """send_message_async sends correct user message format."""
    session_id = "test-session"
    ws_mock = AsyncMock()
    ws_mock.closed = False
    session = SessionState(
        ws=ws_mock,
        cli_session_id="cli-123",
        initialized=True,
    )
    session.connected.set()  # Already initialized
    backend.sessions[session_id] = session

    await backend.send_message_async(
        session_id,
        parts=[{"type": "text", "text": "Fix the bug"}],
    )

    ws_mock.send_str.assert_called_once()
    sent = json.loads(ws_mock.send_str.call_args[0][0].strip())
    assert sent["type"] == "user"
    assert sent["message"]["content"] == "Fix the bug"
    assert sent["session_id"] == "cli-123"

    assert backend.sessions[session_id].status == "busy"


@pytest.mark.asyncio
async def test_send_message_with_system_prompt_initializes(backend):
    """First message with system prompt sends initialize first."""
    session_id = "test-session"
    ws_mock = AsyncMock()
    ws_mock.closed = False
    session = SessionState(
        ws=ws_mock,
        cli_session_id="cli-123",
        initialized=False,
    )
    backend.sessions[session_id] = session

    # Simulate CLI sending system/init shortly after user message is sent
    async def simulate_init():
        await asyncio.sleep(0.05)
        session.connected.set()

    asyncio.create_task(simulate_init())

    with patch("hive.backends.backend_claude.asyncio.sleep", new_callable=AsyncMock):
        await backend.send_message_async(
            session_id,
            parts=[{"type": "text", "text": "Do stuff"}],
            system="You are a worker.",
        )

    # Should have sent 2 messages: initialize + user
    assert ws_mock.send_str.call_count == 2

    init_msg = json.loads(ws_mock.send_str.call_args_list[0][0][0].strip())
    assert init_msg["type"] == "control_request"
    assert init_msg["request"]["subtype"] == "initialize"
    assert init_msg["request"]["appendSystemPrompt"] == "You are a worker."

    user_msg = json.loads(ws_mock.send_str.call_args_list[1][0][0].strip())
    assert user_msg["type"] == "user"

    assert backend.sessions[session_id].initialized is True


# ── WebSocket handler integration test ────────────────────────────────


@pytest.mark.asyncio
async def test_ws_handler_connects_and_routes(running_backend):
    """CLI connects via WS, sends init, backend routes it."""
    backend = running_backend
    session_id = "ws-test-123"
    backend.sessions[session_id] = SessionState()

    # Connect as a WS client (simulating Claude CLI)
    url = f"http://127.0.0.1:{backend.port}/agent/{session_id}"
    async with aiohttp.ClientSession() as http_session:
        async with http_session.ws_connect(url) as ws:
            # Send system/init
            await ws.send_str(json.dumps({"type": "system", "subtype": "init", "session_id": "cli-sess-1"}) + "\n")

            # Wait for backend to process
            await asyncio.sleep(0.1)

            assert backend.sessions[session_id].cli_session_id == "cli-sess-1"
            assert backend.sessions[session_id].connected.is_set()

            # Send an assistant message
            await ws.send_str(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Working on it"}],
                            "usage": {"input_tokens": 10, "output_tokens": 5},
                        },
                    }
                )
                + "\n"
            )

            await asyncio.sleep(0.1)
            assert len(backend.sessions[session_id].messages) == 1

            # Send result
            await ws.send_str(json.dumps({"type": "result", "usage": {"input_tokens": 100, "output_tokens": 50}}) + "\n")

            await asyncio.sleep(0.1)
            assert backend.sessions[session_id].status == "idle"
            assert len(backend.sessions[session_id].messages) == 2


@pytest.mark.asyncio
async def test_ws_handler_unknown_session_closes(running_backend):
    """WS connection for unknown session is closed."""
    backend = running_backend

    url = f"http://127.0.0.1:{backend.port}/agent/unknown-session"
    async with aiohttp.ClientSession() as http_session:
        async with http_session.ws_connect(url) as ws:
            # Server should close the connection
            msg = await asyncio.wait_for(ws.receive(), timeout=2)
            assert msg.type == aiohttp.WSMsgType.CLOSE


# ── Context manager test ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_context_manager():
    """Backend works as async context manager."""
    async with ClaudeWSBackend() as b:
        assert b is not None
