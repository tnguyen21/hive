"""pytest fixtures for Hive tests."""

import os
import tempfile

import pytest

from hive.config import Config
from hive.db import Database


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
