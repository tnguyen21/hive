"""Tests for TauBackend."""

import asyncio
import sys
import textwrap
from pathlib import Path

import pytest

from hive.backends.backend_tau import TauBackend
from hive.status import BackendSessionStatusType


# ── Mock tau serve script ──────────────────────────────────────────────
# A tiny Python script that mimics the tau serve JSON-RPC protocol,
# used in place of the real `coding-agent serve` binary.

MOCK_TAU_SCRIPT = textwrap.dedent("""\
    import json, sys

    def write(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue

        method = msg.get("method")
        req_id = msg.get("id")

        # Notifications (no id)
        if req_id is None:
            continue

        if method == "initialize":
            write({"jsonrpc": "2.0", "id": req_id, "result": {"capabilities": {}}})
        elif method == "session/send":
            write({"jsonrpc": "2.0", "id": req_id, "result": {}})
            # Simulate: emit busy, then idle with usage
            write({"jsonrpc": "2.0", "method": "session.status", "params": {"status": {"type": "busy"}}})
            write({"jsonrpc": "2.0", "method": "session.status", "params": {
                "status": {"type": "idle"},
                "usage": {"input_tokens": 100, "output_tokens": 50},
            }})
        elif method == "session/status":
            write({"jsonrpc": "2.0", "id": req_id, "result": {"type": "idle"}})
        elif method == "session/messages":
            write({"jsonrpc": "2.0", "id": req_id, "result": []})
        elif method == "session/abort":
            write({"jsonrpc": "2.0", "id": req_id, "result": {"success": True}})
        elif method == "shutdown":
            write({"jsonrpc": "2.0", "id": req_id, "result": {}})
            break
        else:
            write({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "not found"}})
""")


@pytest.fixture
def mock_tau_cmd(tmp_path: Path) -> list[str]:
    """Write mock tau script and return the command to run it."""
    script = tmp_path / "mock_tau.py"
    script.write_text(MOCK_TAU_SCRIPT)
    # The "serve" subcommand check in _start_session_process will append "serve"
    # if missing; our mock doesn't need it but tolerates extra args.
    return [sys.executable, str(script)]


@pytest.mark.asyncio
async def test_create_session(mock_tau_cmd: list[str], tmp_path: Path):
    backend = TauBackend(cmd=mock_tau_cmd)
    async with backend:
        result = await backend.create_session(directory=str(tmp_path), title="test")
        assert "id" in result
        assert result["title"] == "test"
        assert len(backend.sessions) == 1


@pytest.mark.asyncio
async def test_list_sessions(mock_tau_cmd: list[str], tmp_path: Path):
    backend = TauBackend(cmd=mock_tau_cmd)
    async with backend:
        await backend.create_session(directory=str(tmp_path), title="test")
        sessions = await backend.list_sessions()
        assert len(sessions) == 1
        assert sessions[0]["title"] == "test"


@pytest.mark.asyncio
async def test_send_message_and_idle_event(mock_tau_cmd: list[str], tmp_path: Path):
    backend = TauBackend(cmd=mock_tau_cmd)
    events: list[dict] = []

    backend.on("session.status", lambda props: events.append(props))

    async with backend:
        result = await backend.create_session(directory=str(tmp_path))
        session_id = result["id"]

        await backend.send_message_async(
            session_id,
            [{"type": "text", "text": "hello"}],
            system="You are a test agent.",
        )

        # Wait for the mock to emit status notifications
        for _ in range(50):
            await asyncio.sleep(0.05)
            state = backend.sessions.get(session_id)
            if state and state.status == BackendSessionStatusType.IDLE:
                break

        # Verify we got idle
        state = backend.sessions[session_id]
        assert state.status == BackendSessionStatusType.IDLE

        # Verify token usage was captured in messages
        assert len(state.messages) >= 1
        last_msg = state.messages[-1]
        assert last_msg["metadata"]["input_tokens"] == 100
        assert last_msg["metadata"]["output_tokens"] == 50


@pytest.mark.asyncio
async def test_get_session_status(mock_tau_cmd: list[str], tmp_path: Path):
    backend = TauBackend(cmd=mock_tau_cmd)
    async with backend:
        result = await backend.create_session(directory=str(tmp_path))
        session_id = result["id"]
        status = await backend.get_session_status(session_id)
        assert status["type"] == BackendSessionStatusType.IDLE


@pytest.mark.asyncio
async def test_get_session_status_not_found(mock_tau_cmd: list[str]):
    backend = TauBackend(cmd=mock_tau_cmd)
    async with backend:
        status = await backend.get_session_status("nonexistent")
        assert status["type"] == BackendSessionStatusType.NOT_FOUND


@pytest.mark.asyncio
async def test_abort_session(mock_tau_cmd: list[str], tmp_path: Path):
    backend = TauBackend(cmd=mock_tau_cmd)
    async with backend:
        result = await backend.create_session(directory=str(tmp_path))
        session_id = result["id"]
        success = await backend.abort_session(session_id)
        assert success is True


@pytest.mark.asyncio
async def test_delete_session(mock_tau_cmd: list[str], tmp_path: Path):
    backend = TauBackend(cmd=mock_tau_cmd)
    async with backend:
        result = await backend.create_session(directory=str(tmp_path))
        session_id = result["id"]
        assert len(backend.sessions) == 1

        success = await backend.delete_session(session_id)
        assert success is True
        assert len(backend.sessions) == 0


@pytest.mark.asyncio
async def test_cleanup_on_exit(mock_tau_cmd: list[str], tmp_path: Path):
    backend = TauBackend(cmd=mock_tau_cmd)
    async with backend:
        await backend.create_session(directory=str(tmp_path))
        await backend.create_session(directory=str(tmp_path))
        assert len(backend.sessions) == 2
    # After __aexit__, sessions should be cleaned up
    assert len(backend.sessions) == 0
