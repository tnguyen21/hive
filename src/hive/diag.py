"""Hive diagnostic report — one-stop debug bundle.

Collects system info, config, daemon status, doctor checks, DB stats,
recent events, daemon log tail, and backend reachability into a single
report that users can paste when filing bug reports.
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
            result = subprocess.run([uv, "--version"], capture_output=True, text=True, timeout=5)
            info["uv_version"] = result.stdout.strip()
        except Exception:
            info["uv_version"] = "error"
    else:
        info["uv_version"] = "not found"

    # claude CLI version
    claude = shutil.which("claude")
    if claude:
        try:
            result = subprocess.run([claude, "--version"], capture_output=True, text=True, timeout=5)
            info["claude_cli_version"] = result.stdout.strip()
        except Exception:
            info["claude_cli_version"] = "error"
    else:
        info["claude_cli_version"] = "not found"

    return info


def _gather_config(project_path: Path | None) -> list[dict]:
    return Config.get_resolved_config(project_root=project_path)


def _gather_daemon(project_name: str, project_path: str) -> dict:
    from .daemon import HiveDaemon

    daemon = HiveDaemon(project_name, project_path)
    return daemon.status()


def _gather_doctor(db: Database) -> list[dict]:
    from .doctor import run_all_checks

    results = run_all_checks(db)
    return [
        {
            "id": r.id,
            "status": r.status,
            "description": r.description,
        }
        for r in results
    ]


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


def _gather_daemon_log_tail(project_name: str, lines: int = 30) -> list[str]:
    log_file = HIVE_DIR / "logs" / f"orchestrator-{project_name}.log"
    if not log_file.exists():
        return [f"(no log file: {log_file})"]
    try:
        all_lines = log_file.read_text().splitlines()
        return all_lines[-lines:]
    except Exception as exc:
        return [f"(error reading log: {exc})"]


def _gather_backend_reachability() -> dict:
    result: dict[str, Any] = {}
    backend = Config.BACKEND

    result["configured_backend"] = backend

    # opencode HTTP check
    if backend == "opencode":
        import urllib.request

        url = Config.OPENCODE_URL
        result["opencode_url"] = url
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                result["opencode_reachable"] = True
                result["opencode_status"] = resp.status
        except Exception as exc:
            result["opencode_reachable"] = False
            result["opencode_error"] = str(exc)

    # claude backend
    elif backend == "claude":
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


def gather_report(db: Database, project_path: str, project_name: str) -> dict:
    """Collect all diagnostic sections into a single dict."""
    pp = Path(project_path)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "system": _section(_gather_system),
        "config": _section(_gather_config, pp),
        "daemon": _section(_gather_daemon, project_name, project_path),
        "doctor": _section(_gather_doctor, db),
        "db_stats": _section(_gather_db_stats, db),
        "recent_events": _section(_gather_recent_events, db),
        "daemon_log_tail": _section(_gather_daemon_log_tail, project_name),
        "backend_reachability": _section(_gather_backend_reachability),
    }


# ---------------------------------------------------------------------------
# Human-readable formatter
# ---------------------------------------------------------------------------


def format_report_text(report: dict) -> str:
    """Format a diagnostic report dict as human-readable text."""
    lines: list[str] = []
    w = 70

    lines.append("=" * w)
    lines.append("HIVE DIAGNOSTIC REPORT")
    lines.append(f"Generated: {report.get('generated_at', 'unknown')}")
    lines.append("=" * w)

    # --- System ---
    lines.append("")
    lines.append("--- System ---")
    sys_info = report.get("system", {})
    if isinstance(sys_info, dict) and "error" not in sys_info:
        lines.append(f"  Hive version:  {sys_info.get('hive_version', '?')}")
        lines.append(f"  Python:        {sys_info.get('python_version', '?')}")
        lines.append(f"  Platform:      {sys_info.get('platform', '?')}")
        lines.append(f"  uv:            {sys_info.get('uv_version', '?')}")
        lines.append(f"  Claude CLI:    {sys_info.get('claude_cli_version', '?')}")
        lines.append(f"  ~/.hive exists: {sys_info.get('hive_dir_exists', '?')}")
    else:
        lines.append(f"  {sys_info}")

    # --- Config ---
    lines.append("")
    lines.append("--- Config ---")
    cfg = report.get("config", [])
    if isinstance(cfg, list):
        for entry in cfg:
            field = entry.get("field", "?")
            value = entry.get("value", "?")
            source = entry.get("source", "?")
            lines.append(f"  {field} = {value} ({source})")
    else:
        lines.append(f"  {cfg}")

    # --- Daemon ---
    lines.append("")
    lines.append("--- Daemon ---")
    daemon = report.get("daemon", {})
    if isinstance(daemon, dict) and "error" not in daemon:
        running = daemon.get("running", False)
        lines.append(f"  Running: {running}")
        if daemon.get("pid"):
            lines.append(f"  PID:     {daemon['pid']}")
        if daemon.get("log_file"):
            lines.append(f"  Log:     {daemon['log_file']}")
    else:
        lines.append(f"  {daemon}")

    # --- Doctor Checks ---
    lines.append("")
    lines.append("--- Doctor Checks ---")
    doctor = report.get("doctor", [])
    if isinstance(doctor, list):
        for check in doctor:
            cid = check.get("id", "?")
            status = check.get("status", "?").upper()
            desc = check.get("description", "?")
            lines.append(f"  {cid:<8} {status:<6} {desc}")
    else:
        lines.append(f"  {doctor}")

    # --- DB Stats ---
    lines.append("")
    lines.append("--- DB Stats ---")
    db_stats = report.get("db_stats", {})
    if isinstance(db_stats, dict) and "error" not in db_stats:
        size = db_stats.get("file_size_bytes")
        if size is not None:
            if size > 1_048_576:
                lines.append(f"  File size:      {size / 1_048_576:.1f} MB")
            else:
                lines.append(f"  File size:      {size / 1024:.1f} KB")
        lines.append(f"  SQLite version: {db_stats.get('sqlite_version', '?')}")
        lines.append(f"  Journal mode:   {db_stats.get('journal_mode', '?')}")
        rc = db_stats.get("row_counts", {})
        if rc:
            lines.append("  Row counts:")
            for table, count in rc.items():
                lines.append(f"    {table}: {count}")
        breakdown = db_stats.get("issue_status_breakdown", {})
        if breakdown:
            lines.append("  Issue status breakdown:")
            for status, count in breakdown.items():
                lines.append(f"    {status}: {count}")
    else:
        lines.append(f"  {db_stats}")

    # --- Recent Events (last 20 in text mode) ---
    lines.append("")
    lines.append("--- Recent Events (last 20) ---")
    events = report.get("recent_events", [])
    if isinstance(events, list):
        for event in events[-20:]:
            ts = event.get("created_at", "?")
            etype = event.get("event_type", "?")
            issue = event.get("issue_id", "-")
            lines.append(f"  {ts}  {etype:<24s}  issue={issue}")
    else:
        lines.append(f"  {events}")

    # --- Daemon Log Tail ---
    lines.append("")
    lines.append("--- Daemon Log (last 30 lines) ---")
    log_lines = report.get("daemon_log_tail", [])
    if isinstance(log_lines, list):
        for line in log_lines:
            lines.append(f"  {line}")
    else:
        lines.append(f"  {log_lines}")

    # --- Backend Reachability ---
    lines.append("")
    lines.append("--- Backend Reachability ---")
    br = report.get("backend_reachability", {})
    if isinstance(br, dict) and "error" not in br:
        lines.append(f"  Backend: {br.get('configured_backend', '?')}")
        for k, v in br.items():
            if k != "configured_backend":
                lines.append(f"  {k}: {v}")
    else:
        lines.append(f"  {br}")

    lines.append("")
    lines.append("=" * w)
    lines.append("END OF REPORT")
    lines.append("=" * w)

    return "\n".join(lines)
