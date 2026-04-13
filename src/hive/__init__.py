"""Hive: Lightweight Multi-Agent Orchestrator"""

import subprocess
from pathlib import Path

from .utils import configure_logging

__version__ = "0.1.0"


def get_version() -> str:
    """Return version string with git short hash, e.g. '0.1.0+abc1234'."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent,
            timeout=5,
        )
        if result.returncode == 0:
            return f"{__version__}+{result.stdout.strip()}"
    except Exception:
        pass
    return __version__


# Configure logging when module is loaded
configure_logging()
