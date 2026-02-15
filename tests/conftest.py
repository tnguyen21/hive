"""pytest fixtures for Hive tests."""

import os
import tempfile
from typing import AsyncGenerator
from unittest.mock import patch

import pytest
import pytest_asyncio

from hive.config import Config
from hive.db import Database
from tests.fake_opencode import FakeOpenCodeServer


def _opencode_reachable() -> bool:
    """Check if OpenCode server is reachable with a quick HTTP request."""
    import urllib.request

    try:
        req = urllib.request.Request(f"{Config.OPENCODE_URL}/session", method="GET")
        urllib.request.urlopen(req, timeout=2)
        return True
    except Exception:
        return False


_server_up = _opencode_reachable()


INTEGRATION_TIMEOUT = 60  # seconds per integration test


def pytest_collection_modifyitems(config, items):
    """Auto-skip integration tests when OpenCode server is not running, and add timeouts."""
    skip = pytest.mark.skip(reason="OpenCode server not reachable")
    timeout = pytest.mark.timeout(INTEGRATION_TIMEOUT)
    for item in items:
        if "integration" in item.keywords:
            if not _server_up:
                item.add_marker(skip)
            else:
                item.add_marker(timeout)


@pytest.fixture
def temp_db():
    """Provide a temporary test database."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    db = Database(db_path)
    db.connect()

    yield db

    db.close()
    os.unlink(db_path)


@pytest.fixture
def db_with_issues(temp_db):
    """Provide a database pre-populated with test issues."""
    # Create some test issues
    issue1 = temp_db.create_issue("Task 1", "First task", priority=1, project="test")
    issue2 = temp_db.create_issue("Task 2", "Second task", priority=2, project="test")
    issue3 = temp_db.create_issue("Task 3", "Blocked task", priority=1, project="test")

    # Add dependency: issue3 depends on issue1
    temp_db.add_dependency(issue3, issue1)

    return temp_db, {"issue1": issue1, "issue2": issue2, "issue3": issue3}


@pytest_asyncio.fixture
async def fake_server() -> AsyncGenerator[FakeOpenCodeServer, None]:
    """Provide a fake OpenCode server for integration tests."""
    server = FakeOpenCodeServer()
    host, port = await server.start_server()
    url = f"http://{host}:{port}"
    server.set_url(url)

    # Patch Config.OPENCODE_URL to point at fake server
    with patch.object(Config, "OPENCODE_URL", url):
        yield server


@pytest.fixture
def patched_config(fake_server):
    """Provide Config patched to use fake server."""
    with patch.object(Config, "OPENCODE_URL", fake_server.url):
        yield Config


@pytest.fixture
def temp_git_repo(tmp_path):
    """Create a temporary git repository for testing."""
    import subprocess

    repo_path = tmp_path / "test_repo"
    repo_path.mkdir()

    # Initialize git repo
    subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_path, check=True, capture_output=True)

    # Create initial commit
    readme_path = repo_path / "README.md"
    readme_path.write_text("# Test Repository\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_path, check=True, capture_output=True)

    return repo_path


@pytest_asyncio.fixture
async def integration_orchestrator(fake_server, temp_db, temp_git_repo):
    """Create a fully wired Orchestrator for integration testing."""
    from hive.orchestrator import Orchestrator

    # Patch Config to use fake server and set other testing values
    with patch.object(Config, "OPENCODE_URL", fake_server.url), patch.object(Config, "MAX_AGENTS", 1), patch.object(Config, "POLL_INTERVAL", 1):
        orchestrator = Orchestrator(project_path=str(temp_git_repo), project_name="test-project", db=temp_db)

        # Initialize the orchestrator
        await orchestrator.initialize()

        yield orchestrator

        # Cleanup
        try:
            await orchestrator.shutdown()
        except Exception:
            pass


def write_hive_result(
    worktree_path: str,
    status: str = "success",
    summary: str = "Test completed",
    files_changed: list[str] = None,
    tests_added: list[str] = None,
    tests_run: bool = True,
    test_command: str = "pytest",
    blockers: list[str] = None,
    artifacts: list[dict] = None,
):
    """Helper function to write a valid .hive-result.jsonl file."""
    import json
    from pathlib import Path

    result = {
        "status": status,
        "summary": summary,
        "files_changed": files_changed or [],
        "tests_added": tests_added or [],
        "tests_run": tests_run,
        "test_command": test_command,
        "blockers": blockers or [],
        "artifacts": artifacts or [],
    }

    result_file = Path(worktree_path) / ".hive-result.jsonl"
    with open(result_file, "w") as f:
        f.write(json.dumps(result) + "\n")
