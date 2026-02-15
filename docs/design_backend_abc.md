# Design: Unified Backend ABC

## Problem

Hive has two backends (OpenCode, Claude-WS) with no shared interface. The OpenCode path requires two separate objects (`OpenCodeClient` + `SSEClient`) wired into the orchestrator as separate constructor args. The Claude-WS path crams both roles into a single monolith that duck-types both interfaces. Adding a third backend means copying ~200 lines of boilerplate and hoping the duck typing holds.

Concrete symptoms:

1. **Orchestrator takes two args** (`opencode_client`, `sse_client`) — for Claude-WS, both point to the same object. The naming lies.
2. **No formal contract.** `merge.py` type-hints `opencode: OpenCodeClient` but actually receives a `ClaudeWSBackend`. No static analysis catches drift.
3. **Event dispatch is copy-pasted.** The `on()`/`on_all()`/`_emit()` pattern is reimplemented identically in `SSEClient` and `ClaudeWSBackend`.
4. **`directory` noise.** Every call in the orchestrator passes `directory=agent.worktree` — but the backend already knows the directory from `create_session`. It's pure plumbing that obscures intent.
5. **`make_model_config()` leaks wire format.** Callers build `{"providerID": "anthropic", "modelID": "..."}` dicts — that's OpenCode's HTTP wire format. Claude-WS ignores it and uses a plain string. The orchestrator shouldn't know about either.
6. **Daemon factory has two code paths** with duplicated orchestrator setup, start, and shutdown logic.

---

## Design

### One ABC, two implementations

```
src/hive/
├── backend.py          # Backend ABC + EventEmitterMixin
├── backend_opencode.py # OpenCodeBackend(Backend) — wraps existing HTTP + SSE
├── backend_claude_ws.py# ClaudeWSBackend(Backend) — wraps existing WS server
├── opencode.py         # (kept as-is, internal to OpenCodeBackend)
└── sse.py              # (kept as-is, internal to OpenCodeBackend)
```

### The ABC

```python
# src/hive/backend.py

from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional
import asyncio
import inspect


class EventEmitterMixin:
    """Shared event dispatch — no reason for each backend to reimplement."""

    def __init__(self):
        self._handlers: Dict[str, Callable] = {}

    def on(self, event_type: str, handler: Callable):
        self._handlers[event_type] = handler

    def on_all(self, handler: Callable):
        self._handlers["*"] = handler

    async def _emit(self, event_type: str, properties: dict):
        handler = self._handlers.get(event_type)
        if handler:
            if inspect.iscoroutinefunction(handler):
                await handler(properties)
            else:
                handler(properties)

        all_handler = self._handlers.get("*")
        if all_handler:
            if inspect.iscoroutinefunction(all_handler):
                await all_handler(event_type, properties)
            else:
                all_handler(event_type, properties)


class Backend(EventEmitterMixin, ABC):
    """Unified interface for session management + event streaming."""

    # ── Lifecycle ────────────────────────────────────────────────

    @abstractmethod
    async def start(self):
        """Start the backend (connect, launch server, etc).
        Called once. Must return only when the backend is ready to accept
        create_session() calls. Long-running work (SSE loop, WS accept loop)
        should be spawned as background tasks internally."""
        ...

    @abstractmethod
    async def stop(self):
        """Graceful shutdown. Kill sessions, close connections, clean up."""
        ...

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.stop()

    # ── Session management ───────────────────────────────────────

    @abstractmethod
    async def create_session(
        self,
        *,
        directory: str,
        title: str,
        permissions: Optional[list] = None,
        model: Optional[str] = None,
    ) -> dict:
        """Create a new worker session. Returns {"id": ..., ...}.
        The backend stores directory internally — callers don't pass it again."""
        ...

    @abstractmethod
    async def send_message(
        self,
        session_id: str,
        text: str,
        *,
        model: Optional[str] = None,
        system: Optional[str] = None,
    ) -> None:
        """Send a user message. Fire-and-forget.
        - `text`: plain string (not parts list)
        - `model`: plain model ID string like "claude-sonnet-4-5-20250929"
        - `system`: optional system prompt override
        Backend handles wire format internally."""
        ...

    @abstractmethod
    async def get_status(self, session_id: str) -> dict:
        """Return {"type": "idle"|"busy"|"error"}."""
        ...

    @abstractmethod
    async def get_messages(self, session_id: str, *, limit: Optional[int] = None) -> list:
        """Return message list for the session."""
        ...

    @abstractmethod
    async def abort(self, session_id: str) -> bool:
        """Abort a running session. Returns True if successful."""
        ...

    @abstractmethod
    async def delete(self, session_id: str) -> bool:
        """Delete/destroy a session. Returns True if successful."""
        ...

    async def cleanup(self, session_id: str):
        """Abort + delete. Best-effort, exceptions swallowed."""
        try:
            await self.abort(session_id)
        except Exception:
            pass
        try:
            await self.delete(session_id)
        except Exception:
            pass

    @abstractmethod
    async def list_sessions(self) -> list:
        """Return list of active sessions for health checks."""
        ...

    # ── Permissions (default no-op, OpenCode overrides) ──────────

    async def get_pending_permissions(self) -> list:
        return []

    async def reply_permission(self, request_id: str, reply: str, message: Optional[str] = None):
        pass
```

### What changes for callers

**Before (orchestrator.py):**
```python
class Orchestrator:
    def __init__(self, db, opencode_client, project_path, project_name, sse_client=None):
        self.opencode = opencode_client
        self.sse_client = sse_client or SSEClient(...)

        # Event registration
        self.sse_client.on("session.status", handle_session_status)

        # Creating sessions
        session = await self.opencode.create_session(
            directory=worktree_path,
            title=f"{agent_name}: {issue['title']}",
            permissions=WORKER_PERMISSIONS,
        )

        # Sending messages
        await self.opencode.send_message_async(
            session_id,
            parts=[{"type": "text", "text": prompt}],
            model=make_model_config(model),
            system=system_prompt,
            directory=worktree_path,
        )

        # Polling status
        status = await self.opencode.get_session_status(agent.session_id, directory=agent.worktree)

        # Cleanup
        await self.opencode.cleanup_session(agent.session_id, directory=agent.worktree)
```

**After:**
```python
class Orchestrator:
    def __init__(self, db, backend: Backend, project_path, project_name):
        self.backend = backend

        # Event registration — same object
        self.backend.on("session.status", handle_session_status)

        # Creating sessions
        session = await self.backend.create_session(
            directory=worktree_path,
            title=f"{agent_name}: {issue['title']}",
            permissions=WORKER_PERMISSIONS,
        )

        # Sending messages — plain text + plain model string
        await self.backend.send_message(
            session_id,
            prompt,
            model=model,
            system=system_prompt,
        )

        # Polling status — no directory arg
        status = await self.backend.get_status(agent.session_id)

        # Cleanup — no directory arg
        await self.backend.cleanup(agent.session_id)
```

**Daemon factory (before):**
```python
if Config.BACKEND == "claude":
    backend = ClaudeWSBackend(host=..., port=...)
    async with backend:
        orchestrator = Orchestrator(db=db, opencode_client=backend, ..., sse_client=backend)
        ...
else:
    async with OpenCodeClient(url, password) as opencode:
        orchestrator = Orchestrator(db=db, opencode_client=opencode, ...)
        ...
```

**Daemon factory (after):**
```python
backend = create_backend()  # returns Backend
async with backend:
    await backend.start()
    orchestrator = Orchestrator(db=db, backend=backend, ...)
    ...
```

### Where `directory` goes

Currently every `get_status()`, `cleanup()`, `get_messages()` call passes `directory=agent.worktree`. This is because OpenCode's HTTP API uses an `X-OpenCode-Directory` header to scope requests. But the backend already knows the directory from `create_session()`.

The fix: `Backend` implementations store `session_id → directory` internally and inject the header (or cwd, or whatever) themselves. Callers just pass `session_id`.

```python
# Inside OpenCodeBackend
class OpenCodeBackend(Backend):
    def __init__(self, base_url, password):
        super().__init__()
        self._client = OpenCodeClient(base_url, password)
        self._sse = SSEClient(base_url, password, global_events=True)
        self._session_dirs: dict[str, str] = {}  # session_id -> directory

    async def create_session(self, *, directory, title, permissions=None, model=None):
        result = await self._client.create_session(directory=directory, title=title, permissions=permissions)
        self._session_dirs[result["id"]] = directory
        return result

    async def get_status(self, session_id):
        directory = self._session_dirs.get(session_id)
        return await self._client.get_session_status(session_id, directory=directory)
```

### Where `make_model_config` goes

Into `OpenCodeBackend.send_message()`. The caller passes a plain string like `"claude-sonnet-4-5-20250929"`, and the OpenCode backend wraps it in `{"providerID": "anthropic", "modelID": "..."}` for the HTTP API. Claude-WS already uses plain strings. The orchestrator never touches wire format.

### Where `parts` wrapping goes

Into `Backend.send_message()`. The caller passes a plain `text` string. The OpenCode backend wraps it as `[{"type": "text", "text": text}]`. This matches how every callsite already constructs parts — always a single text part.

---

## Implementation Plan

### Phase 1: Add the ABC (no behavior change)

1. Create `src/hive/backend.py` with `EventEmitterMixin` and `Backend` ABC as shown above.
2. No callers change yet. Existing code continues to work.

### Phase 2: Wrap OpenCode

3. Create `src/hive/backend_opencode.py` with `OpenCodeBackend(Backend)`.
   - Composes existing `OpenCodeClient` + `SSEClient` internally.
   - Translates simplified interface → existing method calls.
   - Stores `session_id → directory` map.
   - `start()` opens the aiohttp session and spawns the SSE reconnect loop.
   - `stop()` stops SSE and closes the HTTP session.

### Phase 3: Wrap Claude-WS

4. Create `src/hive/backend_claude_ws.py` with `ClaudeWSBackend(Backend)`.
   - Mostly a rename/refactor of existing `claude_ws.py`.
   - Remove duplicated event dispatch code (inherits from `EventEmitterMixin`).
   - `start()` starts the WS server and sets `server_ready`.
   - `stop()` shuts down server + kills processes.

### Phase 4: Update consumers

5. Update `Orchestrator.__init__` to take `backend: Backend` instead of `opencode_client` + `sse_client`.
6. Mechanical find-replace across `orchestrator.py`:
   - `self.opencode.create_session(...)` → `self.backend.create_session(...)`
   - `self.opencode.send_message_async(sid, parts=[...], model=make_model_config(m), directory=d)` → `self.backend.send_message(sid, text, model=m)`
   - `self.opencode.get_session_status(sid, directory=d)` → `self.backend.get_status(sid)`
   - `self.opencode.cleanup_session(sid, directory=d)` → `self.backend.cleanup(sid)`
   - `self.sse_client.on(...)` → `self.backend.on(...)`
   - `self.sse_client.stop()` → `self.backend.stop()`
   - Remove `self.sse_client` entirely.
7. Same for `merge.py`:
   - `self.opencode` → `self.backend`
   - Drop `directory=self.project_path` from every call.
8. Update `daemon.py` to use a `create_backend()` factory, single code path.
9. Update `cli.py` watch command — either use a lightweight read-only SSE backend or keep `SSEClient` as a standalone utility for CLI-only use (it's a consumer, not a backend).

### Phase 5: Cleanup

10. Delete old imports: `from .opencode import make_model_config` from orchestrator/merge.
11. Remove the `sse_client` parameter from `Orchestrator`.
12. Update type hints in `merge.py` from `OpenCodeClient` to `Backend`.
13. Run tests, lint, format.

---

## What a third backend would look like

To add e.g. a direct Anthropic API backend:

```python
class AnthropicBackend(Backend):
    async def start(self):
        self._client = anthropic.AsyncAnthropic()

    async def create_session(self, *, directory, title, permissions=None, model=None):
        session_id = generate_id("api")
        self._sessions[session_id] = {"directory": directory, "model": model, "messages": []}
        return {"id": session_id, "title": title, "directory": directory}

    async def send_message(self, session_id, text, *, model=None, system=None):
        session = self._sessions[session_id]
        # Fire-and-forget: spawn task that calls Anthropic API
        asyncio.create_task(self._run_completion(session_id, text, system))

    # ... etc
```

No changes to orchestrator, merge processor, or daemon. Just register it in the factory.

---

## Non-goals

- **Changing SSE event semantics.** The `session.status` event shape stays the same. Backends emit what the orchestrator already expects.
- **Multi-directory session scoping.** If we ever need one backend instance to serve multiple directories with different scoping, that's a separate design. Today each session has one directory — we just stop making the caller repeat it.
- **Async iterators instead of callbacks.** The `on()`/`on_all()` callback pattern works fine for the event volumes we have. No need to over-engineer into `async for event in backend.events()`.
