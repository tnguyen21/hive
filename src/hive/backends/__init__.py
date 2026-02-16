"""Backend implementations for Hive.

Two backends available:
- OpenCode (default): HTTP REST + SSE via an external OpenCode server
- Claude: Direct WebSocket to Claude CLI processes (--sdk-url)
"""

from .backend_claude import ClaudeWSBackend, SessionState
from .backend_opencode import OpenCodeClient, SSEClient, make_model_config
from .base import HiveBackend

__all__ = [
    "ClaudeWSBackend",
    "HiveBackend",
    "OpenCodeClient",
    "SSEClient",
    "SessionState",
    "make_model_config",
]
