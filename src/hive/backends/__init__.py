"""Backend implementations for Hive.

Backends available:
- Claude: Direct WebSocket to Claude CLI processes (--sdk-url)
- Codex: Local `codex app-server` over stdio (JSON-RPC)
- Tau: Local `tau serve` over stdio (JSON-RPC, one process per session)
"""

from .backend_claude import ClaudeWSBackend, SessionState
from .backend_codex import CodexAppServerBackend
from .backend_tau import TauBackend
from .base import HiveBackend
from .pool import BackendPool

__all__ = [
    "BackendPool",
    "ClaudeWSBackend",
    "CodexAppServerBackend",
    "HiveBackend",
    "SessionState",
    "TauBackend",
]
