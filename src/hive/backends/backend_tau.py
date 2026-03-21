"""Tau backend (tau `serve` over stdio).

This backend spawns one `tau serve --cwd <worktree>` process per session and
speaks JSON-RPC 2.0 over stdin/stdout.

Internal contract details this file relies on:

- Transport/lifecycle:
  - `initialize` request then `initialized` notification handshake is required.
  - One process == one session. No session IDs in the protocol.
  - Prompts are sent with `session/send`; completion is inferred from
    `session.status` notifications emitted by tau.
- Event mapping into Hive's backend abstraction:
  - `session.status` `busy`  -> `session.status` BUSY
  - `session.status` `idle`  -> `session.status` IDLE
  - Heartbeat re-emits busy while a session is active so Hive leases do not expire.
- Prompting/system behavior:
  - Hive system prompt is injected via the `system` parameter on `session/send`.
- Token accounting:
  - tau includes `usage` in the `session.status` idle notification.
  - We synthesize a minimal assistant message at turn completion so Hive can log
    token usage and keep merge/completion fences stable.
- Result semantics:
  - Hive treats `.hive-result.jsonl` as source-of-truth for worker success.
    `session.status` idle means the turn ended, not that the issue succeeded.
"""

import asyncio
from contextlib import suppress
import json
import logging
import os
import shlex
from dataclasses import dataclass, field
from typing import Any

from ..config import Config
from ..status import BackendSessionStatusType, SESSION_STATUS_EVENT, session_status_payload
from .base import HiveBackend, _first_text, _terminate_process_group

logger = logging.getLogger(__name__)

# How often to re-emit BUSY so Hive leases don't expire during long turns.
HEARTBEAT_INTERVAL = 60


@dataclass(slots=True)
class TauSessionState:
    session_id: str
    directory: str | None = None
    title: str | None = None
    status: BackendSessionStatusType = BackendSessionStatusType.IDLE
    model: str | None = None
    system_prompt_set: bool = False

    proc: asyncio.subprocess.Process | None = None
    stdout_task: asyncio.Task | None = None
    stderr_task: asyncio.Task | None = None
    stderr_tail: list[str] = field(default_factory=list)

    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    next_id: int = 1
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    server_ready: asyncio.Event = field(default_factory=asyncio.Event)

    messages: list[dict[str, Any]] = field(default_factory=list)
    heartbeat_task: asyncio.Task | None = None


class TauBackend(HiveBackend):
    """Hive backend that drives tau via `tau serve` (stdio transport).

    Unlike the Codex backend (one long-lived process, multiple threads), this
    backend spawns one tau process per session. Each process manages a single
    agent loop bound to a specific working directory.
    """

    def __init__(self, cmd: list[str] | None = None):
        super().__init__()
        cmd_str = os.environ.get("HIVE_TAU_CMD", "tau serve")
        self._base_cmd = cmd if cmd is not None else shlex.split(cmd_str)
        self.sessions: dict[str, TauSessionState] = {}
        self.running = False

    # ── Session management ────────────────────────────────────────────

    async def list_sessions(self) -> list[dict[str, Any]]:
        return [{"id": sid, "title": s.title, "directory": s.directory} for sid, s in self.sessions.items()]

    async def create_session(
        self,
        directory: str | None = None,
        title: str | None = None,
        permissions: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        session_id = f"tau-{id(object()):x}"

        state = TauSessionState(
            session_id=session_id,
            directory=directory,
            title=title,
        )

        # Spawn tau process for this session
        await self._start_session_process(state)
        self.sessions[session_id] = state

        return {"id": session_id, "title": title, "directory": directory}

    async def send_message_async(
        self,
        session_id: str,
        parts: list[dict[str, Any]],
        agent: str = "build",
        model: str | None = None,
        system: str | None = None,
        directory: str | None = None,
    ):
        state = self.sessions.get(session_id)
        if not state:
            raise ValueError(f"Session {session_id} not found")

        await state.server_ready.wait()

        text = _first_text(parts)
        model_id = model or state.model or getattr(Config, "WORKER_MODEL", None) or getattr(Config, "DEFAULT_MODEL", None)

        params: dict[str, Any] = {"prompt": text}
        if model_id:
            params["model"] = model_id

        # Inject system prompt once per session.
        if system and not state.system_prompt_set:
            params["system"] = system
            state.system_prompt_set = True

        # Emit busy immediately so the orchestrator renews leases.
        state.status = BackendSessionStatusType.BUSY
        await self._emit(SESSION_STATUS_EVENT, session_status_payload(session_id, state.status))
        self._start_heartbeat(session_id)

        # Fire-and-forget: session/send returns immediately, tau runs in background.
        await self._request(state, "session/send", params)

    async def abort_session(self, session_id: str, directory: str | None = None) -> bool:
        state = self.sessions.get(session_id)
        if not state:
            return False
        try:
            await self._request(state, "session/abort", {})
            return True
        except Exception:
            return False

    async def delete_session(self, session_id: str, directory: str | None = None) -> bool:
        state = self.sessions.pop(session_id, None)
        if not state:
            return True
        await self._stop_session_process(state)
        return True

    async def get_session_status(self, session_id: str, directory: str | None = None) -> dict[str, Any]:
        state = self.sessions.get(session_id)
        if not state:
            return {"type": BackendSessionStatusType.NOT_FOUND}

        # If the process died, it's an error.
        if state.proc and state.proc.returncode is not None:
            return {"type": BackendSessionStatusType.ERROR}

        return {"type": state.status}

    async def get_messages(self, session_id: str, directory: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        state = self.sessions.get(session_id)
        if not state:
            return []
        if limit:
            return state.messages[-limit:]
        return state.messages

    # ── Event streaming ───────────────────────────────────────────────

    async def connect_with_reconnect(self, max_retries: int = -1, retry_delay: int = 5):
        self.running = True
        # No central process to manage — processes are per-session.
        while self.running:
            await asyncio.sleep(1)

    def stop(self):
        self.running = False

    # ── Context manager ───────────────────────────────────────────────

    async def __aenter__(self) -> "TauBackend":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()
        for state in list(self.sessions.values()):
            await self._stop_session_process(state)
        self.sessions.clear()

    # ── Internal: per-session process management ──────────────────────

    async def _start_session_process(self, state: TauSessionState):
        cmd = list(self._base_cmd)

        # Ensure 'serve' subcommand is present.
        if "serve" not in cmd:
            cmd.append("serve")

        if state.directory:
            cmd.extend(["--cwd", state.directory])

        logger.info(f"Starting tau session {state.session_id}: {cmd!r}")
        try:
            state.proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                env=os.environ.copy(),
            )
        except FileNotFoundError as e:
            raise FileNotFoundError(
                "Failed to start tau (coding-agent binary not found).\nInstall tau, or set HIVE_TAU_CMD to the full command."
            ) from e

        assert state.proc.stdin and state.proc.stdout and state.proc.stderr

        state.stdout_task = asyncio.create_task(self._stdout_reader(state))
        state.stderr_task = asyncio.create_task(self._stderr_reader(state))

        # Handshake
        try:
            await self._request(state, "initialize", {}, timeout=15)
            await self._notify(state, "initialized", None)
        except Exception as e:
            returncode = state.proc.returncode if state.proc else None
            tail = "\n".join(state.stderr_tail[-20:])
            raise RuntimeError(
                f"tau failed to initialize.\nCommand: {cmd!r}\nReturn code: {returncode}\n" + (f"Stderr (tail):\n{tail}\n" if tail else "")
            ) from e

        state.server_ready.set()

    async def _stop_session_process(self, state: TauSessionState):
        if state.heartbeat_task:
            state.heartbeat_task.cancel()
            state.heartbeat_task = None

        # Fail pending requests
        for _id, fut in list(state.pending.items()):
            if not fut.done():
                fut.set_exception(RuntimeError("tau session shutting down"))
        state.pending.clear()

        if state.stdout_task:
            state.stdout_task.cancel()
        if state.stderr_task:
            state.stderr_task.cancel()

        proc = state.proc
        state.proc = None
        if not proc or proc.returncode is not None:
            return

        # Try graceful shutdown first
        try:
            await self._request(state, "shutdown", {}, timeout=2)
        except Exception:
            pass

        await _terminate_process_group(proc, timeout=3)

    # ── Internal: stdio transport ─────────────────────────────────────

    async def _stdout_reader(self, state: TauSessionState):
        assert state.proc and state.proc.stdout
        while True:
            line = await state.proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line.decode("utf-8"))
            except Exception:
                continue
            await self._route_incoming(state, msg)

    async def _stderr_reader(self, state: TauSessionState):
        assert state.proc and state.proc.stderr
        while True:
            line = await state.proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            state.stderr_tail.append(text)
            if len(state.stderr_tail) > 200:
                state.stderr_tail = state.stderr_tail[-200:]
            logger.debug(f"[tau {state.session_id}] {text}")

    async def _route_incoming(self, state: TauSessionState, msg: dict[str, Any]):
        # Response to a request we sent
        if "id" in msg and ("result" in msg or "error" in msg):
            fut = state.pending.pop(str(msg["id"]), None)
            if fut and not fut.done():
                fut.set_result(msg)
            return

        # Notification from tau
        method = msg.get("method")
        params = msg.get("params", {})
        if method:
            await self._handle_notification(state, method, params)

    async def _handle_notification(self, state: TauSessionState, method: str, params: dict[str, Any]):
        if method == "session.status":
            status_data = params.get("status", {})
            status_type = status_data.get("type", "idle")

            if status_type == "busy":
                state.status = BackendSessionStatusType.BUSY
                self._start_heartbeat(state.session_id)
            elif status_type == "error":
                state.status = BackendSessionStatusType.ERROR
                self._stop_heartbeat(state.session_id)
            else:
                # idle
                state.status = BackendSessionStatusType.IDLE
                self._stop_heartbeat(state.session_id)

                # Synthesize a minimal message with token metadata.
                usage = params.get("usage") or {}
                state.messages.append(
                    {
                        "role": "assistant",
                        "content": [],
                        "metadata": {
                            "input_tokens": int(usage.get("input_tokens", 0)),
                            "output_tokens": int(usage.get("output_tokens", 0)),
                            "model": state.model,
                        },
                    }
                )

            await self._emit(
                SESSION_STATUS_EVENT,
                session_status_payload(state.session_id, state.status),
            )
            return

    async def _write_line(self, state: TauSessionState, obj: dict[str, Any]):
        if not state.proc or not state.proc.stdin:
            raise RuntimeError(f"tau session {state.session_id} is not running")
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
        async with state.write_lock:
            state.proc.stdin.write(data)
            await state.proc.stdin.drain()

    async def _request(self, state: TauSessionState, method: str, params: dict[str, Any] | None, *, timeout: int = 30) -> dict[str, Any]:
        req_id = str(state.next_id)
        state.next_id += 1

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        state.pending[req_id] = fut

        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        await self._write_line(state, payload)

        resp = await asyncio.wait_for(fut, timeout=timeout)
        if "error" in resp and resp["error"]:
            raise RuntimeError(resp["error"])
        return resp.get("result") or {}

    async def _notify(self, state: TauSessionState, method: str, params: dict[str, Any] | None):
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._write_line(state, payload)

    # ── Heartbeat ─────────────────────────────────────────────────────

    def _start_heartbeat(self, session_id: str):
        state = self.sessions.get(session_id)
        if not state:
            return
        if state.heartbeat_task and not state.heartbeat_task.done():
            return

        async def _beat():
            while self.running and state.status == BackendSessionStatusType.BUSY:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if not self.running or state.status != BackendSessionStatusType.BUSY:
                    break
                with suppress(Exception):
                    await self._emit(
                        SESSION_STATUS_EVENT,
                        session_status_payload(session_id, BackendSessionStatusType.BUSY),
                    )

        state.heartbeat_task = asyncio.create_task(_beat())

    def _stop_heartbeat(self, session_id: str):
        state = self.sessions.get(session_id)
        if not state:
            return
        if state.heartbeat_task:
            state.heartbeat_task.cancel()
            state.heartbeat_task = None
