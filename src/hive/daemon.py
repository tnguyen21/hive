"""Daemon management for Hive orchestrator.

This module provides daemon functionality for running the orchestrator
as a background service with PID file management, signal handling, and logging.

Uses subprocess to spawn a detached child process running the orchestrator
in "foreground" mode with stdout/stderr redirected to a log file. The parent
process (the CLI) survives and can report status back to the user.
"""

import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from .config import Config

logger = logging.getLogger(__name__)


class HiveDaemon:
    """Manages the global Hive orchestrator daemon.

    A single daemon serves all registered projects. The daemon discovers
    projects from the DB and dispatches workers across all of them.
    """

    def __init__(self, db_path: str = ""):
        """
        Initialize daemon manager.

        Args:
            db_path: Path to the SQLite database file (for spawn command)
        """
        self.db_path = db_path

        # Global PID file: ~/.hive/pids/daemon.pid (not per-project)
        self.pid_dir = Path.home() / ".hive" / "pids"
        self.pid_file = self.pid_dir / "daemon.pid"

        # Log directory and files
        self.log_dir = Path.home() / ".hive" / "logs"
        # Primary log: rotating hive.log (managed by RotatingFileHandler)
        self.log_file = self.log_dir / "hive.log"
        # Crash log: small stderr sink, truncated per daemon start
        self._crash_log = self.log_dir / "daemon-crash.log"

    def _ensure_dirs(self):
        """Ensure PID and log directories exist."""
        self.pid_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def _read_pid(self) -> Optional[int]:
        """Read PID from PID file if it exists."""
        try:
            if self.pid_file.exists():
                return int(self.pid_file.read_text().strip())
        except (ValueError, IOError):
            pass
        return None

    def _write_pid(self, pid: int):
        """Write PID to PID file."""
        self.pid_file.write_text(str(pid))

    def _remove_pid(self):
        """Remove PID file."""
        try:
            if self.pid_file.exists():
                self.pid_file.unlink()
        except OSError:
            pass

    def _is_running(self, pid: int) -> bool:
        """Check if a process with the given PID is running."""
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

    def _find_all_daemon_pids(self) -> list[int]:
        """Find all running hive daemon PIDs by scanning the process table.

        Returns PIDs of processes matching `hive ... start --foreground`.
        This catches orphaned daemons that the PID file doesn't track.
        """
        try:
            res = subprocess.run(
                ["ps", "ax", "-o", "pid,command"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if res.returncode != 0:
                return []
        except Exception:
            return []

        my_pid = os.getpid()
        pids = []
        for line in res.stdout.splitlines():
            line = line.strip()
            if "hive" not in line or "start" not in line or "--foreground" not in line:
                continue
            # Extract PID from the first column
            match = re.match(r"(\d+)\s", line)
            if match:
                pid = int(match.group(1))
                if pid != my_pid:
                    pids.append(pid)
        return pids

    def _kill_orphaned_daemons(self) -> int:
        """Kill any orphaned daemon processes.

        Returns the number of processes killed.
        """
        pids = self._find_all_daemon_pids()
        killed = 0
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
                killed += 1
            except OSError:
                pass
        if killed:
            time.sleep(1.0)
            # Force kill any survivors
            for pid in pids:
                try:
                    os.kill(pid, 0)  # Check if still alive
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        return killed

    def start(self) -> bool:
        """
        Start the global daemon if not already running.

        Spawns a detached subprocess that runs the orchestrator in
        "foreground" mode with stdout/stderr redirected to the log file.
        The parent process returns immediately so the CLI can report status.

        Returns:
            True if started successfully, False if already running.
        """
        self._ensure_dirs()

        # Check if already running via PID file
        existing_pid = self._read_pid()
        if existing_pid and self._is_running(existing_pid):
            return False

        # Kill any orphaned daemon processes.
        # Multiple daemons can accumulate if the PID file gets overwritten
        # or stale, leading to duplicate orchestrator loops that independently
        # claim issues and spawn duplicate workers.
        self._kill_orphaned_daemons()

        # Clean up stale PID file
        if existing_pid:
            self._remove_pid()

        # Spawn a detached subprocess running `hive start --foreground`.
        # Prefer the installed `hive` entry point over `sys.executable -m hive.cli`
        # because sys.executable can resolve to a base Python (outside the
        # tool venv) when hive was installed via `uv tool install`.
        hive_bin = shutil.which("hive")
        if hive_bin:
            cmd = [hive_bin]
        else:
            cmd = [sys.executable, "-m", "hive.cli"]
        cmd += ["--db", str(self.db_path), "start", "--foreground"]

        # Strip CLAUDECODE so the daemon (and its workers) don't think they're nested
        spawn_env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

        # Structured logging goes to rotating hive.log via RotatingFileHandler
        # (configured in utils.configure_logging). Redirect stdout to /dev/null
        # and stderr to a small crash log truncated per start for forensics.
        crash_fd = open(self._crash_log, "w")  # noqa: SIM115
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=crash_fd,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # detach from parent's session
            env=spawn_env,
        )
        crash_fd.close()

        # Write child PID
        self._write_pid(proc.pid)

        # Give it a moment to start and verify it's alive.
        # Use proc.poll() instead of os.kill(pid, 0) — the latter
        # returns True on zombie processes, causing false positives
        # when the child exits immediately (e.g. argparse error).
        time.sleep(0.5)
        exit_code = proc.poll()
        if exit_code is not None:
            self._remove_pid()
            return False

        return True

    def stop(self) -> bool:
        """
        Stop the running daemon and any orphaned instances.

        Returns:
            True if stopped successfully, False if not running
        """
        pid = self._read_pid()
        stopped_any = False

        if pid and self._is_running(pid):
            # Send SIGTERM to the tracked PID
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass

            # Wait for process to exit
            for _ in range(30):  # Wait up to 3 seconds
                if not self._is_running(pid):
                    break
                time.sleep(0.1)

            # Force kill if still running
            if self._is_running(pid):
                try:
                    os.kill(pid, signal.SIGKILL)
                    time.sleep(0.5)
                except OSError:
                    pass
            stopped_any = True

        # Also kill any orphaned daemon processes not tracked by PID file
        orphans_killed = self._kill_orphaned_daemons()
        stopped_any = stopped_any or orphans_killed > 0

        self._remove_pid()
        return stopped_any

    def status(self) -> dict:
        """
        Get daemon status.

        Returns:
            Dict with status information
        """
        pid = self._read_pid()

        base = {"log_file": str(self.log_file)}

        if not pid:
            return {**base, "running": False, "pid": None, "message": "Daemon not running (no PID file)"}

        if not self._is_running(pid):
            self._remove_pid()
            return {**base, "running": False, "pid": pid, "message": f"Daemon not running (stale PID file for {pid})"}

        return {**base, "running": True, "pid": pid, "message": f"Daemon running (PID {pid})"}

    def logs(self, lines: int = 50, follow: bool = False):
        """
        Show daemon logs.

        Args:
            lines: Number of lines to show (default: 50)
            follow: If True, follow log output like tail -f
        """
        if not self.log_file.exists():
            print(f"No log file found: {self.log_file}")
            return

        try:
            cmd = ["tail"]
            if follow:
                cmd.append("-f")
            cmd.extend(["-n", str(lines), str(self.log_file)])

            subprocess.run(cmd)
        except KeyboardInterrupt:
            pass
        except Exception:
            # Fallback: read tail directly without slurping entire file
            try:
                chunk_size = lines * 4096
                file_size = self.log_file.stat().st_size
                with open(self.log_file, "rb") as f:
                    f.seek(max(0, file_size - chunk_size))
                    tail = f.read().decode(errors="replace").splitlines()
                print("\n".join(tail[-lines:]))
            except Exception as e2:
                print(f"Failed to read logs: {e2}")


def run_daemon_foreground(db):
    """Run the orchestrator in the foreground (called by daemon child process)."""
    import asyncio

    from .backends import BackendPool, ClaudeWSBackend, CodexAppServerBackend
    from .orchestrator import Orchestrator

    async def main():
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        def _signal_handler():
            logger.info("Received shutdown signal")
            stop_event.set()
            # Restore default handlers so a second Ctrl+C kills the
            # process immediately at the OS level — no event loop needed.
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            signal.signal(signal.SIGTERM, signal.SIG_DFL)

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)

        # Determine which backend types are needed by scanning registered
        # projects. Always include the global default so new projects work.
        needed_backends: set[str] = {Config.BACKEND}
        for project in db.list_projects():
            from .config import ConfigRegistry

            cfg = ConfigRegistry._load_config(project_root=Path(project["path"]))
            needed_backends.add(cfg.BACKEND)

        pool = BackendPool(default=Config.BACKEND)
        if "claude" in needed_backends:
            pool.register(
                "claude",
                ClaudeWSBackend(
                    host=Config.CLAUDE_WS_HOST,
                    port=Config.CLAUDE_WS_PORT,
                ),
            )
        if "codex" in needed_backends:
            pool.register("codex", CodexAppServerBackend())

        async with pool:
            orchestrator = Orchestrator(
                db=db,
                backend_pool=pool,
            )
            main_task = asyncio.create_task(orchestrator.start())
            await stop_event.wait()
            orchestrator.running = False
            main_task.cancel()
            try:
                await asyncio.wait_for(main_task, timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass  # Already handled by signal handler
    finally:
        logger.info("Daemon process exiting")
