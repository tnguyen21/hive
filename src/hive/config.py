"""Configuration for Hive orchestrator."""

import os


class Config:
    """Global configuration for Hive orchestrator."""

    # Concurrency
    MAX_AGENTS = int(os.environ.get("HIVE_MAX_AGENTS", "3"))

    # Timing
    POLL_INTERVAL = int(os.environ.get("HIVE_POLL_INTERVAL", "5"))  # seconds
    LEASE_DURATION = int(os.environ.get("HIVE_LEASE_DURATION", "300"))  # 5 minutes
    LEASE_EXTENSION = int(os.environ.get("HIVE_LEASE_EXTENSION", "180"))  # 3 minutes
    PERMISSION_POLL_INTERVAL = float(
        os.environ.get("HIVE_PERMISSION_POLL_INTERVAL", "0.5")
    )  # 500ms

    # OpenCode
    OPENCODE_URL = os.environ.get("OPENCODE_URL", "http://127.0.0.1:4096")
    OPENCODE_PASSWORD = os.environ.get("OPENCODE_SERVER_PASSWORD")

    # Database
    DB_PATH = os.environ.get("HIVE_DB_PATH", "hive.db")

    # Context cycling thresholds (token counts)
    MAYOR_TOKEN_THRESHOLD = int(os.environ.get("HIVE_MAYOR_TOKEN_THRESHOLD", "120000"))
    REFINERY_TOKEN_THRESHOLD = int(
        os.environ.get("HIVE_REFINERY_TOKEN_THRESHOLD", "100000")
    )
    WORKER_TOKEN_THRESHOLD = int(os.environ.get("HIVE_WORKER_TOKEN_THRESHOLD", "150000"))

    # Escalation
    MAX_RETRIES = int(os.environ.get("HIVE_MAX_RETRIES", "2"))
    MAX_AGENT_SWITCHES = int(os.environ.get("HIVE_MAX_AGENT_SWITCHES", "2"))

    # Model
    DEFAULT_MODEL = os.environ.get("HIVE_DEFAULT_MODEL", "claude-sonnet-4-5-20250929")
