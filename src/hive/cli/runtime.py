"""Shared CLI bootstrapping helpers."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from ..config import Config
from ..db import Database
from ..utils import detect_project
from .core import HiveCLI


def do_setup(project_path: Path, project_name: str, *, json_mode: bool = False):
    """Write a default .hive.toml if one doesn't exist."""
    target = project_path / ".hive.toml"
    if target.exists():
        if json_mode:
            print(json.dumps({"config_exists": True, "path": str(target)}))
        else:
            print(f"{target} already exists.")
        return
    target.write_text(f'[project]\nname = "{project_name}"\n\n[hive]\nbackend = "claude"\nmerge_queue_enabled = false\n')
    if json_mode:
        print(json.dumps({"config_created": str(target)}))
    else:
        print(f"Created {target}")


def resolve_project(project: str | None) -> tuple[Path, str]:
    """Resolve the active project path and name."""
    if project:
        project_path = Path(project).resolve()
        project_name = project_path.name
    else:
        project_path, project_name = detect_project()
    return project_path, project_name


def initialize_cli(*, db_override: str | None, project: str | None) -> tuple[Database, HiveCLI, Path, str]:
    """Load config, connect the DB, and create a ``HiveCLI`` instance."""
    project_path, project_name = resolve_project(project)

    # Load layered config: defaults -> ~/.hive/config.toml -> .hive.toml -> env
    Config.load_global(project_root=project_path)
    Config.HIVE_DIR.mkdir(parents=True, exist_ok=True)

    db_path = db_override or Config.DB_PATH
    db = Database(db_path)
    db.connect()

    # Auto-register the current project so the daemon knows where it lives.
    # Best-effort with zero timeout - never block CLI commands.
    if project_name:
        try:
            db.conn.execute("PRAGMA busy_timeout = 0")
            db.register_project(project_name, str(project_path))
        except sqlite3.OperationalError:
            pass
        finally:
            db.conn.execute("PRAGMA busy_timeout = 5000")

    cli = HiveCLI(db, str(project_path))
    return db, cli, project_path, project_name


def do_analyze(project_path: Path, project_name: str, *, json_mode: bool = False):
    """Launch a Claude CLI session to analyze the project and generate .hive/project-context.md."""
    from ..prompts import _load_template

    hive_dir = project_path / ".hive"
    hive_dir.mkdir(exist_ok=True)

    context_path = hive_dir / "project-context.md"
    if context_path.exists():
        if json_mode:
            print(json.dumps({"context_exists": True, "path": str(context_path)}))
        else:
            print(f"{context_path} already exists. Delete it first to re-analyze.")
        return

    init_prompt = _load_template("init")

    claude_cmd = os.environ.get("CLAUDE_CMD", "claude")
    cmd = [
        claude_cmd,
        "--print",
        "--model",
        Config.DEFAULT_MODEL,
        "--dangerously-skip-permissions",
        "-p",
        init_prompt,
    ]

    if not json_mode:
        print(f"Analyzing {project_name}...")

    try:
        result = subprocess.run(cmd, cwd=str(project_path), capture_output=not sys.stdout.isatty())
    except FileNotFoundError:
        msg = "Claude CLI not found. Install `claude` and ensure it's on PATH, or set CLAUDE_CMD."
        if json_mode:
            print(json.dumps({"error": msg}))
        else:
            print(msg)
        return

    if result.returncode != 0:
        msg = f"Analysis failed (exit code {result.returncode})"
        if json_mode:
            print(json.dumps({"error": msg}))
        else:
            print(msg)
        return

    if context_path.exists():
        if json_mode:
            print(json.dumps({"context_created": str(context_path)}))
        else:
            print(f"Created {context_path}")
    else:
        msg = "Analysis completed but .hive/project-context.md was not written. Check Claude output."
        if json_mode:
            print(json.dumps({"error": msg}))
        else:
            print(msg)
