"""Daemon management for Hive orchestrator.

This module provides daemon functionality for running the orchestrator
as a background service with PID file management, signal handling, and logging.

Uses subprocess to spawn a detached child process running the orchestrator
in "foreground" mode with stdout/stderr redirected to a log file. The parent
process (the CLI) survives and can report status back to the user.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from .config import Config


class HiveDaemon:
    """Manages the Hive orchestrator as a background daemon."""

    def __init__(self, project_name: str, project_path: str):
        """
        Initialize daemon manager.

        Args:
            project_name: Name of the project
            project_path: Path to the project directory
        """
        self.project_name = project_name
        self.project_path = Path(project_path).resolve()

        # PID file location: ~/.hive/pids/<project>.pid
        self.pid_dir = Path.home() / ".hive" / "pids"
        self.pid_file = self.pid_dir / f"{project_name}.pid"

        # Log directory: ~/.hive/logs/
        self.log_dir = Path.home() / ".hive" / "logs"
        self.log_file = self.log_dir / f"orchestrator-{project_name}.log"

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

    def start(self, db_path: str = "hive.db") -> bool:
        """
        Start the daemon if not already running.

        Spawns a detached subprocess that runs the orchestrator in
        "foreground" mode with stdout/stderr redirected to the log file.
        The parent process returns immediately so the CLI can report status.

        Args:
            db_path: Path to the SQLite database file.

        Returns:
            True if started successfully, False if already running.
        """
        self._ensure_dirs()

        # Check if already running
        existing_pid = self._read_pid()
        if existing_pid and self._is_running(existing_pid):
            return False

        # Clean up stale PID file
        if existing_pid:
            self._remove_pid()

        # Spawn a detached subprocess running `hive start --foreground`
        # Strip CLAUDECODE so the daemon (and its workers) don't think they're nested
        spawn_env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        log_fd = open(self.log_file, "a")  # noqa: SIM115
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "hive.cli",
                "--db",
                str(db_path),
                "--project",
                str(self.project_path),
                "start",
                "--foreground",
            ],
            stdout=log_fd,
            stderr=log_fd,
            stdin=subprocess.DEVNULL,
            cwd=str(self.project_path),
            start_new_session=True,  # detach from parent's session
            env=spawn_env,
        )
        log_fd.close()

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
        Stop the running daemon.

        Returns:
            True if stopped successfully, False if not running
        """
        pid = self._read_pid()

        if not pid:
            return False

        if not self._is_running(pid):
            self._remove_pid()
            return False

        # Send SIGTERM
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return False

        # Wait for process to exit
        for _ in range(30):  # Wait up to 3 seconds
            if not self._is_running(pid):
                self._remove_pid()
                return True
            time.sleep(0.1)

        # Force kill if still running
        try:
            os.kill(pid, signal.SIGKILL)
            time.sleep(0.5)
        except OSError:
            pass

        self._remove_pid()
        return True

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
            # Fallback: read file directly
            try:
                content = self.log_file.read_text()
                log_lines = content.split("\n")
                print("\n".join(log_lines[-lines:]))
            except Exception as e2:
                print(f"Failed to read logs: {e2}")


def run_daemon_foreground(db, project_path: str, project_name: str):
    """
    Run the orchestrator in the foreground (for debugging or as daemon child).

    Args:
        db: Database instance (must be connected)
        project_path: Path to project
        project_name: Project name
    """
    import asyncio

    from .orchestrator import Orchestrator

    async def main():
        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        def _signal_handler():
            print("\nShutting down...", flush=True)
            stop_event.set()
            # Restore default handlers so a second Ctrl+C kills the
            # process immediately at the OS level — no event loop needed.
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            signal.signal(signal.SIGTERM, signal.SIG_DFL)

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)

        if Config.BACKEND == "claude":
            from .backends import ClaudeWSBackend

            backend = ClaudeWSBackend(
                host=Config.CLAUDE_WS_HOST,
                port=Config.CLAUDE_WS_PORT,
            )
            async with backend:
                orchestrator = Orchestrator(
                    db=db,
                    opencode_client=backend,
                    project_path=project_path,
                    project_name=project_name,
                    sse_client=backend,
                )
                # Run orchestrator until stop signal
                main_task = asyncio.create_task(orchestrator.start())
                await stop_event.wait()
                orchestrator.running = False
                main_task.cancel()
                try:
                    await asyncio.wait_for(main_task, timeout=5)
                except (asyncio.CancelledError, asyncio.TimeoutError):
                    pass
        else:
            from .backends import OpenCodeClient

            async with OpenCodeClient(Config.OPENCODE_URL, Config.OPENCODE_PASSWORD) as opencode:
                orchestrator = Orchestrator(
                    db=db,
                    opencode_client=opencode,
                    project_path=project_path,
                    project_name=project_name,
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
        print("\nShutting down...")
