"""Daemon management for Hive orchestrator.

This module provides proper daemon functionality for running the orchestrator
as a background service with PID file management, signal handling, and logging.
"""

import atexit
import os
import signal
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

    def _daemonize(self):
        """
        Daemonize the current process using the double-fork method.

        This detaches the process from the terminal and runs it in the background.
        """
        # First fork
        try:
            pid = os.fork()
            if pid > 0:
                # Parent exits
                sys.exit(0)
        except OSError as e:
            raise RuntimeError(f"First fork failed: {e}")

        # Decouple from parent environment
        os.chdir(str(self.project_path))
        os.setsid()
        os.umask(0)

        # Second fork
        try:
            pid = os.fork()
            if pid > 0:
                # Parent exits
                sys.exit(0)
        except OSError as e:
            raise RuntimeError(f"Second fork failed: {e}")

        # Now running as daemon
        # Redirect standard file descriptors to log file
        sys.stdout.flush()
        sys.stderr.flush()

        # Open log file for writing
        log_fd = os.open(str(self.log_file), os.O_WRONLY | os.O_CREAT | os.O_APPEND)

        # Duplicate to stdout and stderr
        os.dup2(log_fd, sys.stdout.fileno())
        os.dup2(log_fd, sys.stderr.fileno())
        os.close(log_fd)

        # Redirect stdin to /dev/null
        with open(os.devnull, "r") as devnull:
            os.dup2(devnull.fileno(), sys.stdin.fileno())

    def start(self) -> bool:
        """
        Start the daemon if not already running.

        Returns:
            True if started successfully, False if already running

        Raises:
            RuntimeError: If daemonization fails
        """
        self._ensure_dirs()

        # Check if already running
        existing_pid = self._read_pid()
        if existing_pid and self._is_running(existing_pid):
            print(f"Hive daemon already running (PID {existing_pid})")
            return False

        # Clean up stale PID file
        if existing_pid:
            self._remove_pid()

        print(f"Starting Hive daemon for project: {self.project_name}")
        print(f"Log file: {self.log_file}")

        # Daemonize
        self._daemonize()

        # Write PID file
        self._write_pid(os.getpid())

        # Register cleanup on exit
        atexit.register(self._remove_pid)

        # Set up signal handlers
        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGHUP, self._signal_handler)

        print(f"Daemon started (PID {os.getpid()})")
        return True

    def _signal_handler(self, signum, frame):
        """Handle signals for graceful shutdown."""
        if signum == signal.SIGHUP:
            # Reload configuration
            print("Received SIGHUP, reloading configuration...")
            # TODO: Reload config if needed
        else:
            # SIGTERM or SIGINT - graceful shutdown
            print(f"Received signal {signum}, shutting down...")
            self._remove_pid()
            sys.exit(0)

    def stop(self) -> bool:
        """
        Stop the running daemon.

        Returns:
            True if stopped successfully, False if not running
        """
        pid = self._read_pid()

        if not pid:
            print("Hive daemon not running (no PID file)")
            return False

        if not self._is_running(pid):
            print(f"Hive daemon not running (stale PID file for {pid})")
            self._remove_pid()
            return False

        print(f"Stopping Hive daemon (PID {pid})...")

        # Send SIGTERM
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as e:
            print(f"Failed to stop daemon: {e}")
            return False

        # Wait for process to exit
        for _ in range(30):  # Wait up to 3 seconds
            if not self._is_running(pid):
                print("Daemon stopped")
                self._remove_pid()
                return True
            time.sleep(0.1)

        # Force kill if still running
        print("Daemon not responding, force killing...")
        try:
            os.kill(pid, signal.SIGKILL)
            time.sleep(0.5)
        except OSError:
            pass

        self._remove_pid()
        return True

    def restart(self) -> bool:
        """
        Restart the daemon.

        Returns:
            True if restarted successfully
        """
        self.stop()
        time.sleep(0.5)
        return self.start()

    def status(self) -> dict:
        """
        Get daemon status.

        Returns:
            Dict with status information
        """
        pid = self._read_pid()

        if not pid:
            return {
                "running": False,
                "pid": None,
                "message": "Daemon not running (no PID file)",
            }

        if not self._is_running(pid):
            return {
                "running": False,
                "pid": pid,
                "message": f"Daemon not running (stale PID file for {pid})",
            }

        return {
            "running": True,
            "pid": pid,
            "message": f"Daemon running (PID {pid})",
            "log_file": str(self.log_file),
        }

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
            import subprocess

            cmd = ["tail"]
            if follow:
                cmd.append("-f")
            cmd.extend(["-n", str(lines), str(self.log_file)])

            subprocess.run(cmd)
        except KeyboardInterrupt:
            pass
        except Exception as e:
            # Fallback: read file directly
            try:
                content = self.log_file.read_text()
                log_lines = content.split("\n")
                print("\n".join(log_lines[-lines:]))
            except Exception as e2:
                print(f"Failed to read logs: {e2}")


def run_daemon_foreground(db, project_path: str, project_name: str):
    """
    Run the orchestrator in the foreground (for debugging).

    Args:
        db: Database instance
        project_path: Path to project
        project_name: Project name
    """
    import asyncio

    from .opencode import OpenCodeClient
    from .orchestrator import Orchestrator

    async def main():
        async with OpenCodeClient(
            Config.OPENCODE_URL, Config.OPENCODE_PASSWORD
        ) as opencode:
            orchestrator = Orchestrator(
                db=db,
                opencode_client=opencode,
                project_path=project_path,
                project_name=project_name,
            )

            await orchestrator.start()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down...")
