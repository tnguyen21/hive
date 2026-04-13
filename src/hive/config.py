"""Layered configuration for Hive orchestrator."""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


HIVE_DIR = Path.home() / ".hive"
_DEFAULT_DB_PATH = str(HIVE_DIR / "hive.db")


@dataclass(frozen=True)
class FieldSpec:
    """Declarative metadata for one config field."""

    env_var: str
    typ: type
    default: Any


def _attr_name(key: str) -> str:
    """Return the uppercase attribute name for a config key."""
    return key.upper()


_FIELDS: dict[str, FieldSpec] = {
    "max_agents": FieldSpec("HIVE_MAX_AGENTS", int, 3),
    "poll_interval": FieldSpec("HIVE_POLL_INTERVAL", int, 5),
    "lease_duration": FieldSpec("HIVE_LEASE_DURATION", int, 900),
    "lease_extension": FieldSpec("HIVE_LEASE_EXTENSION", int, 600),
    "db_path": FieldSpec("HIVE_DB_PATH", str, _DEFAULT_DB_PATH),
    "refinery_token_threshold": FieldSpec("HIVE_REFINERY_TOKEN_THRESHOLD", int, 100_000),
    "max_retries": FieldSpec("HIVE_MAX_RETRIES", int, 2),
    "max_agent_switches": FieldSpec("HIVE_MAX_AGENT_SWITCHES", int, 2),
    "merge_poll_interval": FieldSpec("HIVE_MERGE_POLL_INTERVAL", int, 10),
    "test_command": FieldSpec("HIVE_TEST_COMMAND", str, None),
    "merge_queue_enabled": FieldSpec("HIVE_MERGE_QUEUE_ENABLED", bool, True),
    "default_model": FieldSpec("HIVE_DEFAULT_MODEL", str, "claude-opus-4-6"),
    "worker_model": FieldSpec("HIVE_WORKER_MODEL", str, "claude-sonnet-4-6"),
    "refinery_model": FieldSpec("HIVE_REFINERY_MODEL", str, "claude-opus-4-6"),
    # Cost guardrails
    "max_tokens_per_issue": FieldSpec("HIVE_MAX_TOKENS_PER_ISSUE", int, 200_000),
    "anomaly_window_minutes": FieldSpec("HIVE_ANOMALY_WINDOW_MINUTES", int, 10),
    "anomaly_failure_threshold": FieldSpec("HIVE_ANOMALY_FAILURE_THRESHOLD", int, 3),
    # Backend selection
    "backend": FieldSpec("HIVE_BACKEND", str, "claude"),  # "claude" | "codex"
    "queen_backend": FieldSpec("HIVE_QUEEN_BACKEND", str, None),
    "worker_backend": FieldSpec("HIVE_WORKER_BACKEND", str, None),
    "refinery_backend": FieldSpec("HIVE_REFINERY_BACKEND", str, None),
    # Claude WS backend settings
    "claude_ws_host": FieldSpec("HIVE_CLAUDE_WS_HOST", str, "127.0.0.1"),
    "claude_ws_port": FieldSpec("HIVE_CLAUDE_WS_PORT", int, 8765),
    "claude_skip_permissions": FieldSpec("HIVE_CLAUDE_SKIP_PERMISSIONS", bool, False),
    # Codex App Server backend settings
    "codex_cmd": FieldSpec("HIVE_CODEX_CMD", str, "codex app-server --listen stdio://"),
    "codex_approval_policy": FieldSpec("HIVE_CODEX_APPROVAL_POLICY", str, "never"),
    "codex_sandbox": FieldSpec("HIVE_CODEX_SANDBOX", str, "workspace-write"),
    "codex_personality": FieldSpec("HIVE_CODEX_PERSONALITY", str, "pragmatic"),
    "codex_heartbeat_interval": FieldSpec("HIVE_CODEX_HEARTBEAT_INTERVAL", int, 60),
}
_FIELD_ATTRS = {_attr_name(key) for key in _FIELDS}


def _read_hive_section(path: Path | None) -> dict:
    """Read the ``[hive]`` section from *path* if it exists."""
    if path is None or not path.is_file():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f).get("hive", {})


class _Config:
    """Configuration object with uppercase fields for backward compatibility."""

    def __init__(self):
        # Set defaults immediately so import-time access works
        self._apply_defaults()
        self._apply_env()

    def _apply_defaults(self):
        for key, spec in _FIELDS.items():
            setattr(self, _attr_name(key), spec.default)
        self.HIVE_DIR = HIVE_DIR

    def _apply_toml(self, path: Path):
        """Overlay values from a TOML file's [hive] section."""
        section = _read_hive_section(path)
        for key, spec in _FIELDS.items():
            if key in section:
                setattr(self, _attr_name(key), _coerce(section[key], spec.typ))

    def _apply_env(self):
        """Overlay values from environment variables."""
        for key, spec in _FIELDS.items():
            raw = os.environ.get(spec.env_var)
            if raw is not None:
                setattr(self, _attr_name(key), _coerce(raw, spec.typ))

    def load(self, project_root: Path | None = None):
        """(Re-)load config from TOML files plus env vars."""
        self._apply_defaults()
        self._apply_toml(HIVE_DIR / "config.toml")
        if project_root:
            self._apply_toml(project_root / ".hive.toml")
        self._apply_env()

    def get_resolved_config(self, project_root: Path | None = None) -> list[dict]:
        """Return resolved config with per-field source attribution.

        Re-walks the 4 layers (defaults → global TOML → project TOML → env)
        to determine which layer set each field.
        """
        global_toml = HIVE_DIR / "config.toml"
        project_toml = (project_root / ".hive.toml") if project_root else None

        global_section = _read_hive_section(global_toml)
        project_section = _read_hive_section(project_toml)

        res = []
        for key, spec in _FIELDS.items():
            attr = _attr_name(key)
            value = getattr(self, attr, spec.default)

            # Walk layers in order to find effective source
            source = "default"
            if key in global_section:
                source = "global_toml"
            if key in project_section:
                source = "project_toml"
            if os.environ.get(spec.env_var) is not None:
                source = "env"

            res.append(
                {
                    "field": attr,
                    "value": value,
                    "source": source,
                    "env_var": spec.env_var,
                }
            )

        return res


def _coerce(value: object, typ: type) -> object:
    """Coerce *value* to *typ*, handling bools and None."""
    if value is None:
        return None
    if typ is bool:
        if isinstance(value, bool):
            return value
        return str(value).lower() in ("true", "1", "yes")
    return typ(value)


# ── ConfigRegistry — per-project config cache ────────────────────────────


class ConfigRegistry:
    """Per-project config cache with a global CLI fallback."""

    def __init__(self):
        self._configs: dict[str, _Config] = {}  # project_name → _Config
        self._global: _Config | None = None  # fallback for CLI (single-project context)

    @staticmethod
    def _load_config(project_root: Path | None = None) -> _Config:
        cfg = _Config()
        cfg.load(project_root=project_root)
        return cfg

    def _clear_shadowed_field_attrs(self) -> None:
        """Drop config-field attrs set directly on the registry instance.

        Some tests patch or assign `Config.MAX_AGENTS`-style attributes on the
        registry object itself. If those survive into the next load, they shadow
        delegated reads from `self.current`. Clearing them here makes each fresh
        load behave like production.
        """
        for attr in _FIELD_ATTRS:
            self.__dict__.pop(attr, None)
        self.__dict__.pop("HIVE_DIR", None)

    def get(self, project_name: str, project_root: Path | None = None) -> _Config:
        """Get or lazy-load config for a named project."""
        if project_name not in self._configs:
            self._configs[project_name] = self._load_config(project_root=project_root)
        return self._configs[project_name]

    def load_global(self, project_root: Path | None = None) -> _Config:
        """Load config for the current CLI context (backward compat)."""
        self._clear_shadowed_field_attrs()
        self._global = self._load_config(project_root=project_root)
        return self._global

    @property
    def current(self) -> _Config:
        """Access the global/CLI config. Raises RuntimeError if load_global() not called."""
        if self._global is None:
            raise RuntimeError("ConfigRegistry.load_global() not called — no global config loaded")
        return self._global

    def __getattr__(self, name: str):
        # Delegate attribute access to the global config for backward compat.
        # __getattr__ is only called when normal attribute lookup fails.
        try:
            return getattr(self.current, name)
        except RuntimeError:
            raise RuntimeError(f"ConfigRegistry: cannot access '{name}' before load_global() is called")


Config = ConfigRegistry()

# Shared permission configurations (not TOML-configurable)
WORKER_PERMISSIONS = [
    {"permission": "*", "pattern": "*", "action": "allow"},
    {"permission": "question", "pattern": "*", "action": "deny"},
    {"permission": "plan_enter", "pattern": "*", "action": "deny"},
    {"permission": "external_directory", "pattern": "*", "action": "deny"},
]
