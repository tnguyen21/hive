"""Backend pool for multi-backend support.

Manages multiple backend instances keyed by type name (e.g., "claude", "codex").
Tracks which backend owns each session for routing operations.
"""

from pathlib import Path
from types import TracebackType
from typing import Self

from .base import HiveBackend


class BackendPool:
    """Pool of backend instances keyed by type name.

    Resolves which backend a project should use based on per-project config,
    and tracks session-to-backend mapping for routing session operations.
    """

    def __init__(self, default: str = "claude"):
        self._backends: dict[str, HiveBackend] = {}
        self._session_backend: dict[str, str] = {}  # session_id -> backend_name
        self.default = default

    @classmethod
    def from_single(cls, backend: HiveBackend, name: str = "claude") -> "BackendPool":
        """Create a pool containing a single backend (backward compat)."""
        pool = cls(default=name)
        pool.register(name, backend)
        return pool

    def register(self, name: str, backend: HiveBackend):
        """Register a backend instance under the given name."""
        self._backends[name] = backend

    def get(self, name: str) -> HiveBackend:
        """Get a backend by type name. Raises ValueError if not registered."""
        backend = self._backends.get(name)
        if backend is None:
            raise ValueError(f"No backend registered with name '{name}'. Available: {list(self._backends.keys())}")
        return backend

    @property
    def default_backend(self) -> HiveBackend:
        """Return the default backend."""
        return self.get(self.default)

    def for_project(self, project_name: str, project_root: Path | None = None) -> HiveBackend:
        """Resolve the backend for a project from its per-project config. Falls back to default."""
        from ..config import Config

        cfg = Config.get(project_name, project_root)
        backend_name = cfg.BACKEND
        if backend_name not in self._backends:
            return self.default_backend
        return self.get(backend_name)

    def for_role(self, role: str, project_name: str, project_root: Path | None = None) -> HiveBackend:
        """Resolve backend for a role (queen/worker/refinery) in a project.

        Falls back: role-specific config -> project backend -> pool default.
        """
        from ..config import Config

        cfg = Config.get(project_name, project_root)
        role_backend = getattr(cfg, f"{role.upper()}_BACKEND", None)
        if role_backend and role_backend in self._backends:
            return self.get(role_backend)
        backend_name = cfg.BACKEND
        if backend_name in self._backends:
            return self.get(backend_name)
        return self.default_backend

    def for_session(self, session_id: str) -> HiveBackend:
        """Resolve the backend that owns a session. Falls back to default if session is not tracked."""
        name = self._session_backend.get(session_id)
        if name is None:
            return self.default_backend
        return self.get(name)

    def track_session(self, session_id: str, backend: HiveBackend):
        """Record which backend owns a session."""
        for name, b in self._backends.items():
            if b is backend:
                self._session_backend[session_id] = name
                return

    def untrack_session(self, session_id: str):
        """Remove session tracking."""
        self._session_backend.pop(session_id, None)

    def all_backends(self) -> list[HiveBackend]:
        """Return all registered backend instances."""
        return list(self._backends.values())

    # -- Context manager --

    async def __aenter__(self) -> Self:
        for backend in self._backends.values():
            await backend.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        for backend in self._backends.values():
            await backend.__aexit__(exc_type, exc_val, exc_tb)
