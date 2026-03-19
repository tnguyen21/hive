"""Hive diagnostic report — one-stop debug bundle.

Collects system info, config, daemon status, DB stats, recent events,
daemon log tail, and backend reachability into a single report that
users can paste when filing bug reports.
"""

import platform
import shutil
import socket
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .config import HIVE_DIR, Config
from .db import Database


# ---------------------------------------------------------------------------
# Section wrapper — catches exceptions so one broken section doesn't
# prevent the rest of the report from appearing.
# ---------------------------------------------------------------------------


def _section(fn, *args, **kwargs) -> Any:
    """Call *fn* and return its result; on error return an error dict."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# Individual section gatherers
# ---------------------------------------------------------------------------


def _gather_system() -> dict:
    info: dict[str, Any] = {
        "hive_version": __version__,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "hive_dir_exists": HIVE_DIR.is_dir(),
    }

    # uv version
    uv = shutil.which("uv")
    if uv:
        try:
            res = subprocess.run([uv, "--version"], capture_output=True, text=True, timeout=5)
            info["uv_version"] = res.stdout.strip()
        except Exception:
            info["uv_version"] = "error"
    else:
        info["uv_version"] = "not found"

    # claude CLI version
    claude = shutil.which("claude")
    if claude:
        try:
            res = subprocess.run([claude, "--version"], capture_output=True, text=True, timeout=5)
            info["claude_cli_version"] = res.stdout.strip()
        except Exception:
            info["claude_cli_version"] = "error"
    else:
        info["claude_cli_version"] = "not found"

    return info


def _gather_config(project_path: Path | None) -> list[dict]:
    return Config.get_resolved_config(project_root=project_path)


def _gather_daemon() -> dict:
    from .daemon import HiveDaemon

    daemon = HiveDaemon()
    return daemon.status()


def _gather_db_stats(db: Database) -> dict:
    stats: dict[str, Any] = {}

    # File size
    db_path = Path(db.db_path)
    if db_path.exists():
        stats["file_size_bytes"] = db_path.stat().st_size
    else:
        stats["file_size_bytes"] = None

    # SQLite version + journal mode
    stats["sqlite_version"] = sqlite3.sqlite_version
    cursor = db.conn.execute("PRAGMA journal_mode")
    stats["journal_mode"] = cursor.fetchone()[0]

    # Row counts per table
    tables = ["issues", "agents", "events", "notes", "merge_queue", "dependencies"]
    row_counts = {}
    for table in tables:
        try:
            cursor = db.conn.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
            row_counts[table] = cursor.fetchone()[0]
        except Exception:
            row_counts[table] = "error"
    stats["row_counts"] = row_counts

    # Issue status breakdown
    try:
        cursor = db.conn.execute("SELECT status, COUNT(*) FROM issues GROUP BY status")
        stats["issue_status_breakdown"] = {row[0]: row[1] for row in cursor.fetchall()}
    except Exception:
        stats["issue_status_breakdown"] = {}

    return stats


def _gather_recent_events(db: Database, limit: int = 50) -> list[dict]:
    return db.get_recent_events(n=limit)


def _gather_daemon_log_tail(lines: int = 50) -> list[str]:
    log_file = HIVE_DIR / "logs" / "hive.log"
    if not log_file.exists():
        return [f"(no log file: {log_file})"]
    try:
        # Read only the tail of the file to avoid OOM on large logs.
        # Seek back from EOF by a generous byte budget (4KB per line).
        chunk_size = lines * 4096
        file_size = log_file.stat().st_size
        with open(log_file, "rb") as f:
            f.seek(max(0, file_size - chunk_size))
            tail_bytes = f.read()
        all_lines = tail_bytes.decode(errors="replace").splitlines()
        return all_lines[-lines:]
    except Exception as exc:
        return [f"(error reading log: {exc})"]


def _gather_backend_reachability() -> dict:
    result: dict[str, Any] = {}
    backend = Config.BACKEND

    result["configured_backend"] = backend

    if backend == "claude":
        claude = shutil.which("claude")
        result["claude_cli_found"] = claude is not None
        host = Config.CLAUDE_WS_HOST
        port = Config.CLAUDE_WS_PORT
        result["claude_ws_endpoint"] = f"{host}:{port}"
        try:
            sock = socket.create_connection((host, port), timeout=2)
            sock.close()
            result["claude_ws_reachable"] = True
        except Exception as exc:
            result["claude_ws_reachable"] = False
            result["claude_ws_error"] = str(exc)

    # codex backend
    elif backend == "codex":
        codex_cmd = Config.CODEX_CMD.split()[0] if Config.CODEX_CMD else "codex"
        result["codex_found"] = shutil.which(codex_cmd) is not None

    return result


# ---------------------------------------------------------------------------
# Main report assembly
# ---------------------------------------------------------------------------


def gather_report(db: Database, project_path: str) -> dict:
    """Collect all diagnostic sections into a single dict."""
    pp = Path(project_path)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "system": _section(_gather_system),
        "config": _section(_gather_config, pp),
        "daemon": _section(_gather_daemon),
        "db_stats": _section(_gather_db_stats, db),
        "recent_events": _section(_gather_recent_events, db),
        "daemon_log_tail": _section(_gather_daemon_log_tail),
        "backend_reachability": _section(_gather_backend_reachability),
    }


# ---------------------------------------------------------------------------
# Human-readable formatter
# ---------------------------------------------------------------------------


def _fmt_system(data) -> list[str]:
    if isinstance(data, dict) and "error" not in data:
        return [
            f"  Hive version:  {data.get('hive_version', '?')}",
            f"  Python:        {data.get('python_version', '?')}",
            f"  Platform:      {data.get('platform', '?')}",
            f"  uv:            {data.get('uv_version', '?')}",
            f"  Claude CLI:    {data.get('claude_cli_version', '?')}",
            f"  ~/.hive exists: {data.get('hive_dir_exists', '?')}",
        ]
    return [f"  {data}"]


def _fmt_config_section(data) -> list[str]:
    if isinstance(data, list):
        return [f"  {e.get('field', '?')} = {e.get('value', '?')} ({e.get('source', '?')})" for e in data]
    return [f"  {data}"]


def _fmt_daemon(data) -> list[str]:
    if isinstance(data, dict) and "error" not in data:
        lines = [f"  Running: {data.get('running', False)}"]
        if data.get("pid"):
            lines.append(f"  PID:     {data['pid']}")
        if data.get("log_file"):
            lines.append(f"  Log:     {data['log_file']}")
        return lines
    return [f"  {data}"]


def _fmt_db_stats(data) -> list[str]:
    if not (isinstance(data, dict) and "error" not in data):
        return [f"  {data}"]
    lines: list[str] = []
    size = data.get("file_size_bytes")
    if size is not None:
        if size > 1_048_576:
            lines.append(f"  File size:      {size / 1_048_576:.1f} MB")
        else:
            lines.append(f"  File size:      {size / 1024:.1f} KB")
    lines.append(f"  SQLite version: {data.get('sqlite_version', '?')}")
    lines.append(f"  Journal mode:   {data.get('journal_mode', '?')}")
    rc = data.get("row_counts", {})
    if rc:
        lines.append("  Row counts:")
        for table, count in rc.items():
            lines.append(f"    {table}: {count}")
    breakdown = data.get("issue_status_breakdown", {})
    if breakdown:
        lines.append("  Issue status breakdown:")
        for status, count in breakdown.items():
            lines.append(f"    {status}: {count}")
    return lines


def _fmt_recent_events(data) -> list[str]:
    if isinstance(data, list):
        return [f"  {e.get('created_at', '?')}  {e.get('event_type', '?'):<24s}  issue={e.get('issue_id', '-')}" for e in data[-20:]]
    return [f"  {data}"]


def _fmt_log_tail(data) -> list[str]:
    if isinstance(data, list):
        return [f"  {line}" for line in data]
    return [f"  {data}"]


def _fmt_backend(data) -> list[str]:
    if isinstance(data, dict) and "error" not in data:
        lines = [f"  Backend: {data.get('configured_backend', '?')}"]
        for k, v in data.items():
            if k != "configured_backend":
                lines.append(f"  {k}: {v}")
        return lines
    return [f"  {data}"]


_REPORT_SECTIONS = [
    ("System", "system", _fmt_system),
    ("Config", "config", _fmt_config_section),
    ("Daemon", "daemon", _fmt_daemon),
    ("DB Stats", "db_stats", _fmt_db_stats),
    ("Recent Events (last 20)", "recent_events", _fmt_recent_events),
    ("Daemon Log (last 50 lines)", "daemon_log_tail", _fmt_log_tail),
    ("Backend Reachability", "backend_reachability", _fmt_backend),
]


def format_report_text(report: dict) -> str:
    """Format a diagnostic report dict as human-readable text."""
    w = 70
    lines: list[str] = [
        "=" * w,
        "HIVE DIAGNOSTIC REPORT",
        f"Generated: {report.get('generated_at', 'unknown')}",
        "=" * w,
    ]

    for title, key, formatter in _REPORT_SECTIONS:
        lines.append("")
        lines.append(f"--- {title} ---")
        lines.extend(formatter(report.get(key, {})))

    lines.append("")
    lines.append("=" * w)
    lines.append("END OF REPORT")
    lines.append("=" * w)

    return "\n".join(lines)
