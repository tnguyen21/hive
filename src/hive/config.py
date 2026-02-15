"""Layered configuration for Hive orchestrator.

Resolution order (later wins):
  1. Built-in defaults
  2. ~/.hive/config.toml   (global user prefs)
  3. .hive.toml             (per-project overrides)
  4. Environment variables   (HIVE_* — backwards compat)
"""

import os
import tomllib
from pathlib import Path


# ── Mapping from TOML [hive] keys → (env var, type, default) ────────────

_FIELDS: dict[str, tuple[str, type, object]] = {
    "max_agents": ("HIVE_MAX_AGENTS", int, 10),
    "poll_interval": ("HIVE_POLL_INTERVAL", int, 5),
    "lease_duration": ("HIVE_LEASE_DURATION", int, 900),
    "lease_extension": ("HIVE_LEASE_EXTENSION", int, 600),
    "permission_poll_interval": ("HIVE_PERMISSION_POLL_INTERVAL", float, 0.5),
    "permission_safety_net_interval": ("HIVE_PERMISSION_SAFETY_NET_INTERVAL", float, 2.0),
    "opencode_url": ("OPENCODE_URL", str, "http://127.0.0.1:4096"),
    "opencode_password": ("OPENCODE_SERVER_PASSWORD", str, None),
    "db_path": ("HIVE_DB_PATH", str, None),  # default set below
    "refinery_token_threshold": ("HIVE_REFINERY_TOKEN_THRESHOLD", int, 100_000),
    "max_retries": ("HIVE_MAX_RETRIES", int, 2),
    "max_agent_switches": ("HIVE_MAX_AGENT_SWITCHES", int, 2),
    "merge_poll_interval": ("HIVE_MERGE_POLL_INTERVAL", int, 10),
    "test_command": ("HIVE_TEST_COMMAND", str, None),
    "merge_queue_enabled": ("HIVE_MERGE_QUEUE_ENABLED", bool, True),
    "default_model": ("HIVE_DEFAULT_MODEL", str, "claude-opus-4-6"),
    "worker_model": ("HIVE_WORKER_MODEL", str, "claude-sonnet-4-20250514"),
    "refinery_model": ("HIVE_REFINERY_MODEL", str, "claude-sonnet-4-20250514"),
    # Cost guardrails
    "max_tokens_per_issue": ("HIVE_MAX_TOKENS_PER_ISSUE", int, 200_000),
    "max_tokens_per_run": ("HIVE_MAX_TOKENS_PER_RUN", int, 2_000_000),
    "anomaly_window_minutes": ("HIVE_ANOMALY_WINDOW_MINUTES", int, 10),
    "anomaly_failure_threshold": ("HIVE_ANOMALY_FAILURE_THRESHOLD", int, 3),
    # Backend selection
    "backend": ("HIVE_BACKEND", str, "opencode"),  # "opencode" or "claude-ws"
    # Claude WS backend settings
    "claude_ws_host": ("HIVE_CLAUDE_WS_HOST", str, "127.0.0.1"),
    "claude_ws_port": ("HIVE_CLAUDE_WS_PORT", int, 8765),
    "claude_ws_max_concurrent": ("HIVE_CLAUDE_WS_MAX_CONCURRENT", int, 3),
}

# Directory that holds global state (DB, pids, logs)
HIVE_DIR = Path.home() / ".hive"
_DEFAULT_DB_PATH = str(HIVE_DIR / "hive.db")


class _Config:
    """Singleton configuration object.

    All attributes use UPPER_CASE names so existing ``Config.MAX_AGENTS``
    style access keeps working.
    """

    def __init__(self):
        self._loaded = False
        # Set defaults immediately so import-time access works
        self._apply_defaults()
        self._apply_env()

    # ── internal helpers ─────────────────────────────────────────────

    def _apply_defaults(self):
        for key, (_env, _typ, default) in _FIELDS.items():
            val = default
            if key == "db_path" and val is None:
                val = _DEFAULT_DB_PATH
            setattr(self, key.upper(), val)
        self.HIVE_DIR = HIVE_DIR

    def _apply_toml(self, path: Path):
        """Overlay values from a TOML file's [hive] section."""
        if not path.is_file():
            return
        with open(path, "rb") as f:
            data = tomllib.load(f)
        section = data.get("hive", {})
        for key, (_env, typ, _default) in _FIELDS.items():
            if key in section:
                setattr(self, key.upper(), _coerce(section[key], typ))

    def _apply_env(self):
        """Overlay values from environment variables."""
        for key, (env, typ, _default) in _FIELDS.items():
            raw = os.environ.get(env)
            if raw is not None:
                setattr(self, key.upper(), _coerce(raw, typ))

    # ── public API ───────────────────────────────────────────────────

    def load(self, project_root: Path | None = None):
        """(Re-)load config from TOML files + env vars.

        Called once from ``cli.main()`` after project detection.
        """
        self._apply_defaults()
        self._apply_toml(HIVE_DIR / "config.toml")
        if project_root:
            self._apply_toml(project_root / ".hive.toml")
        self._apply_env()
        self._loaded = True


def _coerce(value: object, typ: type) -> object:
    """Coerce *value* to *typ*, handling bools and None."""
    if value is None:
        return None
    if typ is bool:
        if isinstance(value, bool):
            return value
        return str(value).lower() in ("true", "1", "yes")
    return typ(value)


# ── Module-level singleton ───────────────────────────────────────────────

Config = _Config()

# Shared permission configurations (not TOML-configurable)
WORKER_PERMISSIONS = [
    {"permission": "*", "pattern": "*", "action": "allow"},
    {"permission": "question", "pattern": "*", "action": "deny"},
    {"permission": "plan_enter", "pattern": "*", "action": "deny"},
    {"permission": "external_directory", "pattern": "*", "action": "deny"},
]
