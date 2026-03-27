"""Tests for ConfigRegistry (per-project config cache).

Invariants tested:
  INV-1: Two projects with different .hive.toml settings return different config values.
  INV-2: Config.MAX_AGENTS (via __getattr__) still works after refactor (backward compat).
  INV-3: ConfigRegistry.get() is lazy — config is only loaded on first access.

Failure modes:
  - Access Config.current before load_global() → RuntimeError.
  - Access Config attribute before load_global() → RuntimeError.
"""

import pytest

from hive.config import ConfigRegistry, _coerce


# ── Helpers ──────────────────────────────────────────────────────────────


def _write_hive_toml(path, max_agents: int):
    toml_file = path / ".hive.toml"
    toml_file.write_text(f"[hive]\nmax_agents = {max_agents}\n")
    return path


# ── INV-1: per-project isolation ─────────────────────────────────────────


def test_two_projects_different_configs(tmp_path):
    """Two projects with different max_agents return independent configs."""
    proj_a = tmp_path / "project_a"
    proj_b = tmp_path / "project_b"
    proj_a.mkdir()
    proj_b.mkdir()
    _write_hive_toml(proj_a, max_agents=3)
    _write_hive_toml(proj_b, max_agents=7)

    registry = ConfigRegistry()
    cfg_a = registry.get("project_a", project_root=proj_a)
    cfg_b = registry.get("project_b", project_root=proj_b)

    assert cfg_a.MAX_AGENTS == 3
    assert cfg_b.MAX_AGENTS == 7
    # They must be distinct objects
    assert cfg_a is not cfg_b


# ── INV-2: backward-compat __getattr__ delegation ────────────────────────


def test_getattr_delegation_after_load_global(tmp_path):
    """Config.MAX_AGENTS works via __getattr__ after load_global()."""
    _write_hive_toml(tmp_path, max_agents=42)

    registry = ConfigRegistry()
    registry.load_global(project_root=tmp_path)

    # __getattr__ delegation
    assert registry.MAX_AGENTS == 42
    # Direct .current access
    assert registry.current.MAX_AGENTS == 42


# ── INV-3: lazy loading ───────────────────────────────────────────────────


def test_registry_get_is_lazy(tmp_path):
    """get() only loads a project once; second call returns cached instance."""
    _write_hive_toml(tmp_path, max_agents=5)

    registry = ConfigRegistry()
    cfg1 = registry.get("myproject", project_root=tmp_path)
    cfg2 = registry.get("myproject", project_root=tmp_path)

    assert cfg1 is cfg2  # same object — no reload on second call


# ── Failure modes ─────────────────────────────────────────────────────────


def test_current_before_load_global_raises():
    """Accessing .current before load_global() raises RuntimeError."""
    registry = ConfigRegistry()
    with pytest.raises(RuntimeError, match="load_global"):
        _ = registry.current


def test_getattr_before_load_global_raises():
    """Accessing attributes via __getattr__ before load_global() raises RuntimeError."""
    registry = ConfigRegistry()
    with pytest.raises(RuntimeError):
        _ = registry.MAX_AGENTS


# ── Cross-project independence ────────────────────────────────────────────


def test_get_does_not_affect_global(tmp_path):
    """registry.get() for a named project does not set ._global."""
    proj = tmp_path / "p"
    proj.mkdir()
    _write_hive_toml(proj, max_agents=9)

    registry = ConfigRegistry()
    registry.get("p", project_root=proj)

    # ._global must still be None — get() is not load_global()
    assert registry._global is None
    with pytest.raises(RuntimeError):
        _ = registry.current


def test_load_global_twice_replaces_global(tmp_path):
    """Calling load_global() again replaces the existing global config."""
    proj_a = tmp_path / "a"
    proj_b = tmp_path / "b"
    proj_a.mkdir()
    proj_b.mkdir()
    _write_hive_toml(proj_a, max_agents=2)
    _write_hive_toml(proj_b, max_agents=8)

    registry = ConfigRegistry()
    registry.load_global(project_root=proj_a)
    assert registry.MAX_AGENTS == 2

    registry.load_global(project_root=proj_b)
    assert registry.MAX_AGENTS == 8


# ── _coerce edge cases ──────────────────────────────────────────────────


class TestCoerce:
    def test_none_returns_none(self):
        assert _coerce(None, int) is None
        assert _coerce(None, bool) is None
        assert _coerce(None, str) is None

    def test_bool_true_strings(self):
        for val in ("true", "True", "TRUE", "1", "yes", "Yes", "YES"):
            assert _coerce(val, bool) is True

    def test_bool_false_strings(self):
        for val in ("false", "False", "FALSE", "0", "no", "No", "NO", "anything"):
            assert _coerce(val, bool) is False

    def test_bool_isinstance_passthrough(self):
        assert _coerce(True, bool) is True
        assert _coerce(False, bool) is False

    def test_int_from_string(self):
        assert _coerce("42", int) == 42

    def test_str_passthrough(self):
        assert _coerce("hello", str) == "hello"

    def test_int_passthrough(self):
        assert _coerce(7, int) == 7
