"""Tests for OpenCode HTTP client."""

import pytest

from hive.backends import OpenCodeClient


@pytest.mark.asyncio
async def test_opencode_client_initialization():
    """Test client initialization."""
    client = OpenCodeClient(base_url="http://localhost:4096")
    assert client.base_url == "http://localhost:4096"
    assert client.session is None


@pytest.mark.asyncio
async def test_opencode_client_context_manager():
    """Test async context manager."""
    async with OpenCodeClient() as client:
        assert client.session is not None

    # Session should be closed after exit
    assert client.session is None or client.session.closed


@pytest.mark.asyncio
async def test_auth_header():
    """Test auth header generation."""
    client = OpenCodeClient(password="secret123")
    auth = client._get_auth_header()

    assert "Authorization" in auth
    assert auth["Authorization"].startswith("Basic ")


@pytest.mark.asyncio
async def test_directory_header():
    """Test directory header generation."""
    client = OpenCodeClient()
    header = client._get_directory_header("/home/user/project")

    assert "X-OpenCode-Directory" in header
    assert header["X-OpenCode-Directory"] == "/home/user/project"


@pytest.mark.asyncio
async def test_directory_header_none():
    """Test directory header when not specified."""
    client = OpenCodeClient()
    header = client._get_directory_header(None)

    assert "X-OpenCode-Directory" not in header


# Integration tests (require running OpenCode server)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_create_session(tmp_path):
    """Test session creation (requires OpenCode server)."""
    async with OpenCodeClient() as client:
        session = await client.create_session(
            directory=str(tmp_path),
            title="Test session",
            permissions=[
                {"permission": "*", "pattern": "*", "action": "allow"},
                {"permission": "question", "pattern": "*", "action": "deny"},
            ],
        )

        assert "id" in session
        assert session["title"] == "Test session"
        assert "directory" in session

        # Clean up
        await client.delete_session(session["id"], directory=str(tmp_path))


@pytest.mark.asyncio
@pytest.mark.integration
async def test_send_message_async(tmp_path):
    """Test async message sending (requires OpenCode server)."""
    async with OpenCodeClient() as client:
        # Create session
        session = await client.create_session(directory=str(tmp_path))
        session_id = session["id"]

        # Send async message
        await client.send_message_async(
            session_id,
            parts=[{"type": "text", "text": "What is 2+2?"}],
            directory=str(tmp_path),
        )

        # Clean up
        await client.delete_session(session_id, directory=str(tmp_path))


@pytest.mark.asyncio
@pytest.mark.integration
async def test_get_session_status(tmp_path):
    """Test getting session status (requires OpenCode server)."""
    async with OpenCodeClient() as client:
        # Create session
        session = await client.create_session(directory=str(tmp_path))
        session_id = session["id"]

        # Get status
        status = await client.get_session_status(session_id, directory=str(tmp_path))

        assert "type" in status
        assert status["type"] in ["idle", "busy", "retry"]

        # Clean up
        await client.delete_session(session_id, directory=str(tmp_path))


@pytest.mark.asyncio
@pytest.mark.integration
async def test_abort_session(tmp_path):
    """Test aborting a session (requires OpenCode server)."""
    async with OpenCodeClient() as client:
        # Create session and send a message
        session = await client.create_session(directory=str(tmp_path))
        session_id = session["id"]

        # Try to abort (may or may not be running, but should not error)
        success = await client.abort_session(session_id, directory=str(tmp_path))
        assert isinstance(success, bool)

        # Clean up
        await client.delete_session(session_id, directory=str(tmp_path))


@pytest.mark.asyncio
@pytest.mark.integration
async def test_delete_session(tmp_path):
    """Test deleting a session (requires OpenCode server)."""
    async with OpenCodeClient() as client:
        # Create session
        session = await client.create_session(directory=str(tmp_path))
        session_id = session["id"]

        # Delete it
        success = await client.delete_session(session_id, directory=str(tmp_path))
        assert success

        # Session deletion successful (no verification needed)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_get_pending_permissions(tmp_path):
    """Test getting pending permissions (requires OpenCode server)."""
    async with OpenCodeClient() as client:
        permissions = await client.get_pending_permissions(directory=str(tmp_path))
        assert isinstance(permissions, list)
