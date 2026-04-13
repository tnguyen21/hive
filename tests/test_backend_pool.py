"""Tests for BackendPool.for_role() — per-role backend resolution.

Invariants tested:
  INV-1: for_role() with no role-specific backend set returns same as for_project().
  INV-2: for_role() with a role-specific backend set returns that backend.
  INV-3: for_role() with an unregistered role backend falls back to project backend.
  INV-4: Different roles can resolve to different backends independently.
"""

import pytest

from hive.backends.pool import BackendPool
from hive.config import Config


class _StubBackend:
    """Minimal stub implementing enough of HiveBackend for pool tests."""

    def __init__(self, name: str):
        self.name = name

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass


@pytest.fixture(autouse=True)
def _clear_config_cache():
    """Clear the global Config registry cache before and after each test."""
    Config._configs.clear()
    yield
    Config._configs.clear()


@pytest.fixture()
def two_backend_pool():
    """Pool with 'claude' and 'codex' stub backends, default='claude'."""
    pool = BackendPool(default="claude")
    pool.register("claude", _StubBackend("claude"))
    pool.register("codex", _StubBackend("codex"))
    return pool


def _write_hive_toml(path, **fields):
    """Write a .hive.toml with the given [hive] fields."""
    lines = ["[hive]"]
    for k, v in fields.items():
        lines.append(f'{k} = "{v}"')
    (path / ".hive.toml").write_text("\n".join(lines) + "\n")


# ── INV-1: no role-specific backend → same as for_project ──────────────


def test_for_role_falls_back_to_project_backend(two_backend_pool, tmp_path):
    """When no role-specific backend is set, for_role returns the project backend."""
    proj = tmp_path / "proj"
    proj.mkdir()
    _write_hive_toml(proj, backend="claude")

    result = two_backend_pool.for_role("worker", "proj", proj)
    expected = two_backend_pool.for_project("proj", proj)
    assert result is expected


# ── INV-2: role-specific backend is used ────────────────────────────────


def test_for_role_returns_role_specific_backend(two_backend_pool, tmp_path):
    """When worker_backend is set, for_role('worker') returns that backend."""
    proj = tmp_path / "proj"
    proj.mkdir()
    _write_hive_toml(proj, backend="claude", worker_backend="codex")

    result = two_backend_pool.for_role("worker", "proj", proj)
    assert result.name == "codex"

    # Project-level backend should still be claude
    project_result = two_backend_pool.for_project("proj", proj)
    assert project_result.name == "claude"


# ── INV-3: unregistered role backend falls back ────────────────────────


def test_for_role_unregistered_role_backend_falls_back(two_backend_pool, tmp_path):
    """When role backend points to an unregistered name, falls back to project backend."""
    proj = tmp_path / "proj"
    proj.mkdir()
    _write_hive_toml(proj, backend="claude", worker_backend="nonexistent")

    result = two_backend_pool.for_role("worker", "proj", proj)
    assert result.name == "claude"


# ── INV-4: different roles resolve independently ───────────────────────


def test_for_role_queen_and_worker_different(two_backend_pool, tmp_path):
    """Queen and worker can use different backends in the same project."""
    proj = tmp_path / "proj"
    proj.mkdir()
    _write_hive_toml(proj, backend="claude", queen_backend="claude", worker_backend="codex")

    queen = two_backend_pool.for_role("queen", "proj", proj)
    worker = two_backend_pool.for_role("worker", "proj", proj)

    assert queen.name == "claude"
    assert worker.name == "codex"
    assert queen is not worker
