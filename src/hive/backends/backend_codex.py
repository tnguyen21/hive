"""Codex App Server backend (Codex CLI `app-server` over stdio).

This backend runs a local `codex app-server --listen stdio://` subprocess and
speaks its JSON-RPC-like line protocol over stdin/stdout.

Internal contract details this file relies on:

- Transport/lifecycle:
  - `initialize` request then `initialized` notification handshake is required.
  - Worker sessions are Codex threads started with `thread/start` (`ephemeral=True`).
  - Prompts are sent with `turn/start`; completion is inferred from notifications.
- Event mapping into Hive's backend abstraction:
  - `turn/started`   -> `session.status` busy
  - `turn/completed` -> `session.status` idle
  - Heartbeat re-emits busy while a turn is active so Hive leases do not expire.
- Prompting/system behavior:
  - Hive system prompt is injected once via
    `collaborationMode.settings.developer_instructions`.
- Token accounting compatibility:
  - Codex does not emit standard session messages; we synthesize a minimal assistant
    message at turn completion using `thread/tokenUsage/updated` totals so Hive can
    log token usage and keep merge/completion fences stable.
- Approval behavior:
  - For non-interactive workers we auto-accept app-server approval requests
    (`item/commandExecution/requestApproval`, `item/fileChange/requestApproval`) and
    answer `item/tool/requestUserInput` deterministically to avoid deadlocks.
- Result semantics:
  - Hive still treats `.hive-result.jsonl` as source-of-truth for worker success.
    `turn/completed` means the turn ended, not necessarily that the issue succeeded.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import Config
from .base import HiveBackend

logger = logging.getLogger(__name__)


@dataclass
class ThreadState:
    directory: Optional[str] = None
    title: Optional[str] = None
    status: str = "idle"  # "idle" | "busy"
    model: Optional[str] = None
    approval_policy: Optional[str] = None
    sandbox_mode: Optional[str] = None
    sandbox_policy_set: bool = False
    sandbox_writable_roots: Optional[List[str]] = None

    active_turn_id: Optional[str] = None
    developer_instructions_set: bool = False

    # Minimal "message" list to satisfy Hive's token accounting + merge fencing.
    messages: List[Dict[str, Any]] = field(default_factory=list)
    token_usage_by_turn: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    heartbeat_task: Optional[asyncio.Task] = None


class CodexAppServerBackend(HiveBackend):
    """Hive backend that drives Codex via `codex app-server` (stdio transport)."""

    def __init__(self, cmd: Optional[List[str]] = None):
        super().__init__()
        # Default command can be overridden via `HIVE_CODEX_CMD` / `.hive.toml`.
        #
        # Also support `CODEX_CMD` (used by `hive queen`) as a fallback for the
        # executable path so `uv tool install` setups can point at Codex without
        # requiring a separate Hive-specific env var.
        default_cmd_str = "codex app-server --listen stdio://"
        cmd_str = getattr(Config, "CODEX_CMD", None) or default_cmd_str

        if cmd is not None:
            self._cmd = cmd
        else:
            base = os.environ.get("CODEX_CMD")
            if base and cmd_str == default_cmd_str and os.environ.get("HIVE_CODEX_CMD") is None:
                self._cmd = shlex.split(base) + ["app-server", "--listen", "stdio://"]
            else:
                self._cmd = shlex.split(cmd_str)

        # Hive currently speaks the App Server protocol over stdin/stdout.
        # If the user configures a websocket listener, we'd need a WS client instead.
        listen_url = None
        if "--listen" in self._cmd:
            try:
                listen_url = self._cmd[self._cmd.index("--listen") + 1]
            except Exception:
                listen_url = None
        else:
            for tok in self._cmd:
                if tok.startswith("--listen="):
                    listen_url = tok.split("=", 1)[1]
                    break
        if listen_url and not str(listen_url).startswith("stdio://"):
            raise ValueError(
                f"Codex backend requires stdio transport (got --listen {listen_url!r}). "
                "Set HIVE_CODEX_CMD to 'codex app-server --listen stdio://' (or set CODEX_CMD to the codex executable)."
            )

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._stdout_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._stderr_tail: List[str] = []

        self._write_lock = asyncio.Lock()
        self._next_id = 1
        self._pending: Dict[str, asyncio.Future] = {}

        self.sessions: Dict[str, ThreadState] = {}

        self.running = False
        self.server_ready = asyncio.Event()

    # ── Session management ────────────────────────────────────────────

    @staticmethod
    def _compute_git_sandbox_writable_roots(worktree_dir: Optional[str]) -> List[str]:
        """Compute extra writable roots needed for git to work in a worktree sandbox.

        Codex's `workspace-write` sandbox allows writing to the turn `cwd`, but git
        worktrees store their real gitdir under the parent repo's `.git/`, e.g.:

          <repo>/.worktrees/<name>/.git  ->  gitdir: <repo>/.git/worktrees/<name>

        Git operations like `git add`/`git commit` need to write lock/index/object
        files under `<repo>/.git/...`, which is outside the worktree `cwd`. Without
        adding the parent repo `.git` as a writable root, these operations fail with
        sandbox permission errors and Hive workers can thrash/retry/escalate.
        """
        if not worktree_dir:
            return []

        wt = Path(worktree_dir)
        git_marker = wt / ".git"

        # Only apply to worktrees where `.git` is a file pointing elsewhere.
        try:
            if not git_marker.is_file():
                return []
            first_line = git_marker.read_text(encoding="utf-8", errors="replace").splitlines()[0].strip()
        except Exception:
            return []

        if not first_line.startswith("gitdir:"):
            return []

        gitdir_raw = first_line.split("gitdir:", 1)[1].strip()
        if not gitdir_raw:
            return []

        gitdir_path = Path(gitdir_raw)
        try:
            if not gitdir_path.is_absolute():
                gitdir_path = (wt / gitdir_path).resolve()
            else:
                gitdir_path = gitdir_path.resolve()
        except Exception:
            # Best-effort: keep the raw path.
            gitdir_path = (wt / gitdir_path) if not gitdir_path.is_absolute() else gitdir_path

        # Typical shape: <repo>/.git/worktrees/<name> -> common git dir is <repo>/.git
        if gitdir_path.parent.name == "worktrees":
            common_git_dir = gitdir_path.parent.parent
        else:
            common_git_dir = gitdir_path.parent

        roots: List[str] = []
        try:
            roots.append(str(common_git_dir.resolve()))
        except Exception:
            roots.append(str(common_git_dir))

        # Stable uniqueness while preserving order.
        deduped: List[str] = []
        seen: set[str] = set()
        for r in roots:
            if r and r not in seen:
                seen.add(r)
                deduped.append(r)
        return deduped

    async def list_sessions(self) -> List[Dict[str, Any]]:
        return [{"id": sid, "title": s.title, "directory": s.directory} for sid, s in self.sessions.items()]

    async def create_session(
        self,
        directory: Optional[str] = None,
        title: Optional[str] = None,
        permissions: Optional[List[Dict[str, str]]] = None,
    ) -> Dict[str, Any]:
        await self.server_ready.wait()

        approval_policy = getattr(Config, "CODEX_APPROVAL_POLICY", "never")
        sandbox_mode = getattr(Config, "CODEX_SANDBOX", "workspace-write")
        personality = getattr(Config, "CODEX_PERSONALITY", "pragmatic")

        params: Dict[str, Any] = {
            "cwd": directory,
            "ephemeral": True,
            "approvalPolicy": approval_policy,
            "sandbox": sandbox_mode,
            "personality": personality,
        }

        # Codex ignores unknown keys; keep payload minimal.
        result = await self._request("thread/start", params)
        thread = result.get("thread", {})
        thread_id = thread.get("id")
        if not thread_id:
            raise RuntimeError(f"Codex thread/start returned no thread.id: {result}")

        self.sessions[str(thread_id)] = ThreadState(
            directory=directory,
            title=title,
            status="idle",
            model=result.get("model"),
            approval_policy=approval_policy,
            sandbox_mode=sandbox_mode,
        )

        return {"id": str(thread_id), "title": title, "directory": directory}

    async def send_message_async(
        self,
        session_id: str,
        parts: List[Dict[str, Any]],
        agent: str = "build",
        model: Optional[str] = None,
        system: Optional[str] = None,
        directory: Optional[str] = None,
    ):
        await self.server_ready.wait()

        state = self.sessions.get(session_id)
        if not state:
            raise ValueError(f"Session {session_id} not found")

        text = ""
        for part in parts:
            if part.get("type") == "text":
                text = part.get("text", "")
                break

        model_id = model or state.model or Config.WORKER_MODEL or Config.DEFAULT_MODEL

        approval_policy = getattr(Config, "CODEX_APPROVAL_POLICY", state.approval_policy or "never")
        personality = getattr(Config, "CODEX_PERSONALITY", "pragmatic")
        sandbox_mode = getattr(Config, "CODEX_SANDBOX", state.sandbox_mode or "workspace-write")

        params: Dict[str, Any] = {
            "threadId": session_id,
            "input": [{"type": "text", "text": text}],
            "approvalPolicy": approval_policy,
            "cwd": directory or state.directory,
            "personality": personality,
            "model": model_id,
        }

        # Sandbox gotcha: git worktrees need write access to the parent repo `.git/`.
        # Without it, `git add`/`git commit` fail under `workspace-write` because
        # the worktree's real gitdir lives outside the worktree `cwd`.
        if not state.sandbox_policy_set and sandbox_mode == "workspace-write":
            if state.sandbox_writable_roots is None:
                state.sandbox_writable_roots = self._compute_git_sandbox_writable_roots(params.get("cwd"))
                if state.sandbox_writable_roots:
                    logger.info(f"Codex workspace-write sandbox: adding writableRoots for git: {state.sandbox_writable_roots}")
            if state.sandbox_writable_roots:
                params["sandboxPolicy"] = {"type": "workspaceWrite", "writableRoots": state.sandbox_writable_roots}
                state.sandbox_policy_set = True

        # Inject developer instructions once per thread so the turn runs with
        # Hive's system prompt (agent identity + project rules).
        if system and not state.developer_instructions_set:
            params["collaborationMode"] = {
                "mode": "default",
                "settings": {
                    "model": model_id,
                    "developer_instructions": system,
                    "reasoning_effort": None,
                },
            }
            state.developer_instructions_set = True

        # Emit a busy status immediately so the orchestrator renews leases even
        # if Codex takes time before sending turn/started.
        state.status = "busy"
        await self._emit("session.status", {"sessionID": session_id, "status": {"type": "busy"}})
        self._start_heartbeat(session_id)

        # Fire-and-forget semantics: this returns after Codex accepts the turn,
        # not after it completes. Completion is signaled via notifications.
        await self._request("turn/start", params)

    async def abort_session(self, session_id: str, directory: Optional[str] = None) -> bool:
        await self.server_ready.wait()
        state = self.sessions.get(session_id)
        if not state:
            return False
        if not state.active_turn_id:
            return True  # Nothing running
        try:
            await self._request("turn/interrupt", {"threadId": session_id, "turnId": state.active_turn_id})
            return True
        except Exception:
            return False

    async def delete_session(self, session_id: str, directory: Optional[str] = None) -> bool:
        # Threads are started ephemeral; best-effort local cleanup is enough.
        state = self.sessions.pop(session_id, None)
        if state and state.heartbeat_task:
            state.heartbeat_task.cancel()
        return True

    async def cleanup_session(self, session_id: str, directory: Optional[str] = None):
        try:
            await self.abort_session(session_id, directory=directory)
        except Exception:
            pass
        try:
            await self.delete_session(session_id, directory=directory)
        except Exception:
            pass

    async def get_session_status(self, session_id: str, directory: Optional[str] = None) -> Dict[str, Any]:
        state = self.sessions.get(session_id)
        if not state:
            return {"type": "not_found"}
        return {"type": "idle" if state.status != "busy" else "busy"}

    async def get_messages(self, session_id: str, directory: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        state = self.sessions.get(session_id)
        if not state:
            return []
        if limit:
            return state.messages[-limit:]
        return state.messages

    async def get_pending_permissions(self, directory: Optional[str] = None) -> List[Dict[str, Any]]:
        # Codex approvals are handled directly via JSON-RPC server->client requests.
        return []

    async def reply_permission(self, request_id: str, reply: str, message: Optional[str] = None, directory: Optional[str] = None):
        # No-op: we auto-handle Codex approval requests.
        return

    # ── Event streaming ───────────────────────────────────────────────

    async def connect_with_reconnect(self, max_retries: int = -1, retry_delay: int = 5):
        self.running = True

        await self._start_process()
        self.server_ready.set()

        # Block until stopped
        while self.running:
            await asyncio.sleep(1)

    def stop(self):
        self.running = False

    # ── Context manager ───────────────────────────────────────────────

    async def __aenter__(self) -> CodexAppServerBackend:
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()
        await self._stop_process()

    # ── Internal: process + JSON-RPC transport ────────────────────────

    async def _start_process(self):
        if self._proc and self._proc.returncode is None:
            return

        logger.info(f"Starting Codex app-server: {self._cmd!r}")
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                env=os.environ.copy(),
            )
        except FileNotFoundError as e:
            raise FileNotFoundError(
                "Failed to start Codex app-server (codex binary not found).\n"
                "Install Codex CLI, or set one of:\n"
                "- HIVE_CODEX_CMD='codex app-server --listen stdio://'\n"
                "- CODEX_CMD='/absolute/path/to/codex' (Hive will append 'app-server --listen stdio://')"
            ) from e
        except PermissionError as e:
            raise PermissionError(
                f"Failed to start Codex app-server due to permissions: {e}.\n"
                "If you're using an npm-installed Codex wrapper, ensure `node` is on PATH for the Hive daemon."
            ) from e
        assert self._proc.stdin and self._proc.stdout and self._proc.stderr

        self._stdout_task = asyncio.create_task(self._stdout_reader())
        self._stderr_task = asyncio.create_task(self._stderr_reader())

        # Handshake: initialize + initialized notification.
        try:
            await self._request(
                "initialize",
                {
                    "clientInfo": {"name": "hive", "version": "0.0"},
                    "capabilities": {"experimentalApi": True},
                },
                timeout=15,
            )
            await self._notify("initialized", None)
        except Exception as e:
            # Bubble up a more actionable error (common in `uv tool install` setups where
            # PATH differs between the interactive shell and the daemon process).
            returncode = self._proc.returncode if self._proc else None
            tail = "\n".join(self._stderr_tail[-20:])
            raise RuntimeError(
                "Codex app-server failed to initialize.\n"
                f"Command: {self._cmd!r}\n"
                f"Return code: {returncode}\n"
                + (f"Stderr (tail):\n{tail}\n" if tail else "")
                + "If you installed Codex via npm (wrapper script), ensure `node` is on PATH for the Hive daemon.\n"
                "You can also set CODEX_CMD to the absolute path of the native Codex binary."
            ) from e

    async def _stop_process(self):
        for sid, state in list(self.sessions.items()):
            if state.heartbeat_task:
                state.heartbeat_task.cancel()
        self.sessions.clear()

        # Fail all pending requests so callers don't hang.
        for _id, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(RuntimeError("Codex backend shutting down"))
        self._pending.clear()

        if self._stdout_task:
            self._stdout_task.cancel()
        if self._stderr_task:
            self._stderr_task.cancel()

        proc = self._proc
        self._proc = None
        if not proc or proc.returncode is not None:
            return

        # Kill the process group (best-effort).
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    async def _stdout_reader(self):
        assert self._proc and self._proc.stdout
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line.decode("utf-8"))
            except Exception:
                continue
            await self._route_incoming(msg)

    async def _stderr_reader(self):
        assert self._proc and self._proc.stderr
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                break
            # Keep this at debug to avoid noisy daemon logs in normal operation.
            text = line.decode("utf-8", errors="replace").rstrip()
            self._stderr_tail.append(text)
            if len(self._stderr_tail) > 200:
                self._stderr_tail = self._stderr_tail[-200:]
            logger.debug(f"[codex stderr] {text}")

    async def _route_incoming(self, msg: Dict[str, Any]):
        # Response
        if "id" in msg and ("result" in msg or "error" in msg):
            fut = self._pending.pop(str(msg["id"]), None)
            if fut and not fut.done():
                fut.set_result(msg)
            return

        # Server -> client request (expects a response)
        if "id" in msg and "method" in msg:
            await self._handle_server_request(msg)
            return

        # Notification
        method = msg.get("method")
        params = msg.get("params", {})
        if method:
            await self._handle_notification(method, params)

    async def _handle_server_request(self, msg: Dict[str, Any]):
        req_id = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}

        # Default: auto-accept for session to avoid stalls.
        if method == "item/commandExecution/requestApproval":
            await self._respond(req_id, {"decision": "acceptForSession"})
            return
        if method == "item/fileChange/requestApproval":
            await self._respond(req_id, {"decision": "acceptForSession"})
            return

        if method == "item/tool/requestUserInput":
            # Avoid stalling: pick first option if present, else empty string.
            answers: Dict[str, Dict[str, List[str]]] = {}
            for q in params.get("questions", []) or []:
                qid = q.get("id")
                if not qid:
                    continue
                options = q.get("options") or []
                if options:
                    answers[qid] = {"answers": [str(options[0].get("label", ""))]}
                else:
                    answers[qid] = {"answers": [""]}
            await self._respond(req_id, {"answers": answers})
            return

        # If we don't know how to handle a request, return an error response.
        await self._respond_error(req_id, code=-32601, message=f"Unsupported server request: {method}")

    async def _handle_notification(self, method: str, params: Dict[str, Any]):
        # Primary lifecycle mapping for Hive.
        if method == "turn/started":
            thread_id = params.get("threadId")
            if thread_id:
                state = self.sessions.get(str(thread_id))
                if state:
                    state.status = "busy"
                    state.active_turn_id = (params.get("turn") or {}).get("id")
                    await self._emit("session.status", {"sessionID": str(thread_id), "status": {"type": "busy"}})
                    self._start_heartbeat(str(thread_id))
            return

        if method == "turn/completed":
            thread_id = params.get("threadId")
            if thread_id:
                state = self.sessions.get(str(thread_id))
                if state:
                    turn = params.get("turn") or {}
                    turn_id = turn.get("id")
                    state.status = "idle"
                    state.active_turn_id = None
                    self._stop_heartbeat(str(thread_id))

                    usage = state.token_usage_by_turn.get(str(turn_id), {}) if turn_id else {}
                    last = usage.get("last") or {}
                    # Synthesize a minimal message with token metadata so Hive can log it.
                    state.messages.append(
                        {
                            "role": "assistant",
                            "content": [],
                            "metadata": {
                                "input_tokens": int(last.get("inputTokens", 0) or 0),
                                "output_tokens": int(last.get("outputTokens", 0) or 0),
                                "model": state.model,
                            },
                        }
                    )

                    await self._emit("session.status", {"sessionID": str(thread_id), "status": {"type": "idle"}})
            return

        if method == "thread/tokenUsage/updated":
            thread_id = params.get("threadId")
            turn_id = params.get("turnId")
            if thread_id and turn_id:
                state = self.sessions.get(str(thread_id))
                if state:
                    state.token_usage_by_turn[str(turn_id)] = params.get("tokenUsage") or {}
            return

        if method == "error":
            # Codex reports transient or terminal backend errors here. Hive doesn't
            # have a first-class mapping; keep in logs.
            logger.warning(f"Codex error notification: {params}")
            return

        # Ignore everything else (item deltas, plan deltas, etc).

    async def _write_line(self, obj: Dict[str, Any]):
        if not self._proc or not self._proc.stdin:
            raise RuntimeError("Codex app-server is not running")
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
        async with self._write_lock:
            self._proc.stdin.write(data)
            await self._proc.stdin.drain()

    async def _request(self, method: str, params: Optional[Dict[str, Any]], *, timeout: int = 30) -> Dict[str, Any]:
        req_id = str(self._next_id)
        self._next_id += 1

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[req_id] = fut

        payload: Dict[str, Any] = {"id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        await self._write_line(payload)

        resp = await asyncio.wait_for(fut, timeout=timeout)
        if "error" in resp:
            raise RuntimeError(resp["error"])
        return resp.get("result") or {}

    async def _notify(self, method: str, params: Optional[Dict[str, Any]]):
        payload: Dict[str, Any] = {"method": method}
        if params is not None:
            payload["params"] = params
        await self._write_line(payload)

    async def _respond(self, req_id: Any, result: Dict[str, Any]):
        await self._write_line({"id": req_id, "result": result})

    async def _respond_error(self, req_id: Any, *, code: int, message: str, data: Any = None):
        err: Dict[str, Any] = {"code": int(code), "message": str(message)}
        if data is not None:
            err["data"] = data
        await self._write_line({"id": req_id, "error": err})

    def _start_heartbeat(self, session_id: str):
        state = self.sessions.get(session_id)
        if not state:
            return
        if state.heartbeat_task and not state.heartbeat_task.done():
            return

        interval = int(getattr(Config, "CODEX_HEARTBEAT_INTERVAL", 60))

        async def _beat():
            while self.running and state.status == "busy":
                await asyncio.sleep(interval)
                if not self.running or state.status != "busy":
                    break
                try:
                    await self._emit("session.status", {"sessionID": session_id, "status": {"type": "busy"}})
                except Exception:
                    # Heartbeat is best-effort.
                    pass

        state.heartbeat_task = asyncio.create_task(_beat())

    def _stop_heartbeat(self, session_id: str):
        state = self.sessions.get(session_id)
        if not state:
            return
        if state.heartbeat_task:
            state.heartbeat_task.cancel()
            state.heartbeat_task = None
