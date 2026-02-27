"""pytest fixtures for Hive tests."""

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import AsyncGenerator
from unittest.mock import patch

import pytest
import pytest_asyncio

from hive.config import Config
from hive.db import Database
from tests.fake_opencode import FakeOpenCodeServer


def pytest_collection_modifyitems(config, items):
    """Add timeouts to integration tests."""
    timeout = pytest.mark.timeout(INTEGRATION_TIMEOUT)
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(timeout)


INTEGRATION_TIMEOUT = 30  # seconds per integration test


@pytest.fixture(autouse=True)
def _auto_load_global_config():
    """Ensure Config.load_global() is called before every test.

    Mirrors production behaviour (cli.main() always calls load_global before
    running any command).  Tests that create their own ConfigRegistry() are
    unaffected because they use local instances rather than the module-level
    Config singleton.
    """
    Config.load_global()


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

    with patch.object(Config, "OPENCODE_URL", url):
        yield server

    await server.stop_server()


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

    # Create initial commit on main branch
    readme_path = repo_path / "README.md"
    readme_path.write_text("# Test Repository\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=repo_path, check=True, capture_output=True)

    return repo_path


@pytest_asyncio.fixture
async def integration_orchestrator(fake_server, temp_db, temp_git_repo):
    """Create a fully wired Orchestrator for integration testing.

    Config overrides for fast, deterministic tests:
    - POLL_INTERVAL=0.1: fast polling
    - LEASE_DURATION=4: short enough for stall tests (check_interval=1)
    - MERGE_QUEUE_ENABLED=False: no merge processing (tested separately)
    - ANOMALY_FAILURE_THRESHOLD=0: disable anomaly detection (0 is falsy)
    """
    from hive.backends import OpenCodeClient
    from hive.orchestrator import Orchestrator

    with (
        patch.object(Config, "OPENCODE_URL", fake_server.url),
        patch.object(Config, "MAX_AGENTS", 1),
        patch.object(Config, "POLL_INTERVAL", 0.1),
        patch.object(Config, "LEASE_DURATION", 4),
        patch.object(Config, "LEASE_EXTENSION", 2),
        patch.object(Config, "MERGE_QUEUE_ENABLED", False),
        patch.object(Config, "PERMISSION_SAFETY_NET_INTERVAL", 60),
        patch.object(Config, "ANOMALY_FAILURE_THRESHOLD", 0),
    ):
        # Register the project so orchestrator can resolve project paths
        temp_db.register_project("test-project", str(temp_git_repo))

        async with OpenCodeClient(base_url=fake_server.url) as opencode_client:
            orchestrator = Orchestrator(
                db=temp_db,
                opencode_client=opencode_client,
            )
            # Point SSE client at fake server
            orchestrator.sse_client.base_url = fake_server.url

            yield orchestrator

            orchestrator.running = False
            orchestrator.sse_client.stop()
            orchestrator.active_agents.clear()


# ── Integration test helpers ───────────────────────────────────────────


async def run_orchestrator_until(orchestrator, predicate, timeout=10.0):
    """Start orchestrator and run until predicate() returns True or timeout.

    Starts the full orchestrator stack (SSE, main_loop, permission loop,
    merge loop) and polls predicate every 50ms. Tears everything down
    in the finally block regardless of outcome.
    """
    orchestrator.running = True
    orchestrator._setup_sse_handlers()

    # Only initialize merge processors if merge queue is enabled —
    # initialize() eagerly creates a refinery session which would
    # confuse session-counting helpers in tests.
    if Config.MERGE_QUEUE_ENABLED:
        for proc in list(orchestrator.merge_pool._processors.values()):
            await proc.initialize()

    # Snapshot existing tasks so we only cancel tasks spawned during the run
    # (i.e., fire-and-forget monitor_agent tasks from spawn_worker).
    pre_existing_tasks = set(asyncio.all_tasks())

    sse_task = asyncio.create_task(orchestrator.sse_client.connect_with_reconnect(max_retries=3, retry_delay=0.1))
    permission_task = asyncio.create_task(orchestrator.permission_unblocker_loop())
    merge_task = asyncio.create_task(orchestrator.merge_processor_loop())
    main_task = asyncio.create_task(orchestrator.main_loop())
    managed_tasks = {sse_task, permission_task, merge_task, main_task}

    try:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.05)
        raise TimeoutError(f"Predicate not satisfied within {timeout}s")
    finally:
        orchestrator.running = False
        orchestrator.sse_client.stop()

        # Cancel managed tasks + any fire-and-forget tasks spawned during the run
        # (monitor_agent tasks created by spawn_worker).
        spawned_tasks = asyncio.all_tasks() - pre_existing_tasks
        all_to_cancel = managed_tasks | spawned_tasks
        for task in all_to_cancel:
            task.cancel()

        # Await all cancelled tasks to ensure clean teardown
        await asyncio.gather(*all_to_cancel, return_exceptions=True)

        orchestrator.active_agents.clear()
        orchestrator.session_status_events.clear()


async def await_session_created(fake_server, count=1, timeout=5.0):
    """Wait until fake_server has at least `count` created sessions.

    Returns the most recently created session_id.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if len(fake_server.created_session_ids) >= count:
            return fake_server.created_session_ids[count - 1]
        await asyncio.sleep(0.05)
    raise TimeoutError(f"Expected {count} session(s), got {len(fake_server.created_session_ids)}")


def complete_worker(fake_server, session_id, worktree, status="success", summary="Done", artifacts=None):
    """Simulate a worker completing: write result file + inject idle SSE event."""
    write_hive_result(
        worktree_path=worktree,
        status=status,
        summary=summary,
        artifacts=artifacts or [{"type": "git_commit", "value": "abc1234"}],
    )
    fake_server.inject_idle(session_id)


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
