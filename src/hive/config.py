"""Layered configuration for Hive orchestrator."""

import os
import tomllib
from pathlib import Path


_SENSITIVE_FIELDS: set[str] = set()

_FIELDS: dict[str, tuple[str, type, object]] = {
    "max_agents": ("HIVE_MAX_AGENTS", int, 3),
    "poll_interval": ("HIVE_POLL_INTERVAL", int, 5),
    "lease_duration": ("HIVE_LEASE_DURATION", int, 900),
    "lease_extension": ("HIVE_LEASE_EXTENSION", int, 600),
    "db_path": ("HIVE_DB_PATH", str, None),  # default set below
    "refinery_token_threshold": ("HIVE_REFINERY_TOKEN_THRESHOLD", int, 100_000),
    "max_retries": ("HIVE_MAX_RETRIES", int, 2),
    "max_agent_switches": ("HIVE_MAX_AGENT_SWITCHES", int, 2),
    "merge_poll_interval": ("HIVE_MERGE_POLL_INTERVAL", int, 10),
    "test_command": ("HIVE_TEST_COMMAND", str, None),
    "merge_queue_enabled": ("HIVE_MERGE_QUEUE_ENABLED", bool, True),
    "default_model": ("HIVE_DEFAULT_MODEL", str, "claude-opus-4-6"),
    "worker_model": ("HIVE_WORKER_MODEL", str, "claude-sonnet-4-6"),
    "refinery_model": ("HIVE_REFINERY_MODEL", str, "claude-opus-4-6"),
    # Cost guardrails
    "max_tokens_per_issue": ("HIVE_MAX_TOKENS_PER_ISSUE", int, 200_000),
    "anomaly_window_minutes": ("HIVE_ANOMALY_WINDOW_MINUTES", int, 10),
    "anomaly_failure_threshold": ("HIVE_ANOMALY_FAILURE_THRESHOLD", int, 3),
    # Backend selection
    "backend": ("HIVE_BACKEND", str, "claude"),  # "claude" | "codex"
    # Claude WS backend settings
    "claude_ws_host": ("HIVE_CLAUDE_WS_HOST", str, "127.0.0.1"),
    "claude_ws_port": ("HIVE_CLAUDE_WS_PORT", int, 8765),
    "claude_skip_permissions": ("HIVE_CLAUDE_SKIP_PERMISSIONS", bool, False),
    # Codex App Server backend settings
    "codex_cmd": ("HIVE_CODEX_CMD", str, "codex app-server --listen stdio://"),
    "codex_approval_policy": ("HIVE_CODEX_APPROVAL_POLICY", str, "never"),
    "codex_sandbox": ("HIVE_CODEX_SANDBOX", str, "workspace-write"),
    "codex_personality": ("HIVE_CODEX_PERSONALITY", str, "pragmatic"),
    "codex_heartbeat_interval": ("HIVE_CODEX_HEARTBEAT_INTERVAL", int, 60),
}

HIVE_DIR = Path.home() / ".hive"
_DEFAULT_DB_PATH = str(HIVE_DIR / "hive.db")


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
        for key, (_env, _typ, default) in _FIELDS.items():
            val = default
            if key == "db_path" and val is None:
                val = _DEFAULT_DB_PATH
            setattr(self, key.upper(), val)
        self.HIVE_DIR = HIVE_DIR

    def _apply_toml(self, path: Path):
        """Overlay values from a TOML file's [hive] section."""
        section = _read_hive_section(path)
        for key, (_env, typ, _default) in _FIELDS.items():
            if key in section:
                setattr(self, key.upper(), _coerce(section[key], typ))

    def _apply_env(self):
        """Overlay values from environment variables."""
        for key, (env, typ, _default) in _FIELDS.items():
            raw = os.environ.get(env)
            if raw is not None:
                setattr(self, key.upper(), _coerce(raw, typ))

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
        to determine which layer set each field. Sensitive fields are redacted.
        """
        global_toml = HIVE_DIR / "config.toml"
        project_toml = (project_root / ".hive.toml") if project_root else None

        global_section = _read_hive_section(global_toml)
        project_section = _read_hive_section(project_toml)

        res = []
        for key, (env_var, typ, default) in _FIELDS.items():
            attr = key.upper()
            value = getattr(self, attr, default)

            # Walk layers in order to find effective source
            source = "default"
            if key in global_section:
                source = "global_toml"
            if key in project_section:
                source = "project_toml"
            if os.environ.get(env_var) is not None:
                source = "env"

            # Redact sensitive fields
            display_value = "***" if key in _SENSITIVE_FIELDS and value else value

            res.append(
                {
                    "field": attr,
                    "value": display_value,
                    "source": source,
                    "env_var": env_var,
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

    def get(self, project_name: str, project_root: Path | None = None) -> _Config:
        """Get or lazy-load config for a named project."""
        if project_name not in self._configs:
            self._configs[project_name] = self._load_config(project_root=project_root)
        return self._configs[project_name]

    def load_global(self, project_root: Path | None = None) -> _Config:
        """Load config for the current CLI context (backward compat)."""
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
