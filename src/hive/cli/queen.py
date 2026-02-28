"""Queen Bee mixin for HiveCLI."""

import os
import subprocess
import sys
from pathlib import Path

from ..config import Config


class QueenMixin:
    """Mixin providing Queen Bee TUI methods for HiveCLI."""

    # Sentinel markers for the Queen identity block in CLAUDE.md
    _QUEEN_SENTINEL_START = "<!-- HIVE-QUEEN-SESSION-START -->"
    _QUEEN_SENTINEL_END = "<!-- HIVE-QUEEN-SESSION-END -->"

    def _resolve_mcp_configs(self, configs: list[str] | None) -> list[str]:
        """Resolve bare MCP config names against ~/.claude."""
        resolved: list[str] = []
        for config in configs or []:
            path = Path(config).expanduser()
            if not path.is_absolute() and path.parent == Path("."):
                path = Path.home() / ".claude" / config
            resolved.append(str(path))
        return resolved

    def queen(self, *, backend: str | None = None, skip_permissions: bool = False, mcp_configs: list[str] | None = None):
        """Launch Queen Bee TUI using the configured backend."""
        # Propagate to daemon and workers via env var (before daemon.start())
        if skip_permissions:
            os.environ["HIVE_CLAUDE_SKIP_PERMISSIONS"] = "1"
        resolved_mcp_configs = self._resolve_mcp_configs(mcp_configs)
        if resolved_mcp_configs:
            os.environ["HIVE_CLAUDE_MCP_CONFIGS"] = os.pathsep.join(resolved_mcp_configs)

        daemon = self._make_daemon()
        daemon_status = daemon.status()
        if not daemon_status["running"]:
            print("Starting daemon... ", end="", flush=True)
            daemon.start()
            daemon_status = daemon.status()
            if daemon_status["running"]:
                print(f"done (PID {daemon_status['pid']})")
            else:
                print("failed")
                self._error("Failed to start daemon. Check `hive daemon logs`.")

        effective = backend or Config.BACKEND
        if effective == "codex":
            self._queen_codex()
        else:
            self._queen_claude(skip_permissions=skip_permissions, mcp_configs=resolved_mcp_configs)

    def _queen_write_identity_files(self) -> tuple[Path, Path]:
        """Write Queen identity files for compaction persistence.

        Returns (claude_md_path, instructions_path) for cleanup.
        """
        from ..prompts import _load_template

        queen_prompt = _load_template("queen")

        # Write full instructions to .hive/ so Queen can re-read after compaction
        hive_dir = self.project_path / ".hive"
        hive_dir.mkdir(exist_ok=True)
        instructions_path = hive_dir / "queen-instructions.md"
        instructions_path.write_text(queen_prompt)

        # Write condensed identity anchor to .claude/CLAUDE.md (re-read every turn)
        claude_dir = self.project_path / ".claude"
        claude_dir.mkdir(exist_ok=True)
        claude_md = claude_dir / "CLAUDE.md"

        queen_block = (
            f"\n{self._QUEEN_SENTINEL_START}\n"
            "# HIVE QUEEN BEE — ACTIVE SESSION\n"
            "You are the Queen Bee coordinator. You do NOT write code — you plan, decompose, and monitor.\n"
            "Full instructions: `.hive/queen-instructions.md` — re-read if your context feels incomplete.\n"
            "Operational state: `.hive/queen-state.md` — re-read to recall what you were working on.\n"
            "Always use `hive --json` for CLI commands. The daemon runs in background.\n"
            f"{self._QUEEN_SENTINEL_END}\n"
        )

        existing = claude_md.read_text() if claude_md.exists() else ""
        if self._QUEEN_SENTINEL_START not in existing:
            claude_md.write_text(existing + queen_block)

        return claude_md, instructions_path

    def _queen_cleanup_identity_files(self, claude_md: Path, instructions_path: Path):
        """Remove Queen identity files written for the session."""
        # Strip queen block from CLAUDE.md
        if claude_md.exists():
            content = claude_md.read_text()
            start = content.find(self._QUEEN_SENTINEL_START)
            if start != -1:
                end = content.find(self._QUEEN_SENTINEL_END)
                if end != -1:
                    end += len(self._QUEEN_SENTINEL_END)
                    # Consume trailing newline if present
                    if end < len(content) and content[end] == "\n":
                        end += 1
                    cleaned = (content[:start] + content[end:]).rstrip("\n")
                    if cleaned.strip():
                        claude_md.write_text(cleaned + "\n")
                    else:
                        claude_md.unlink()

        # Remove instructions file
        instructions_path.unlink(missing_ok=True)

        # Remove state file (ephemeral per-session)
        state_file = self.project_path / ".hive" / "queen-state.md"
        state_file.unlink(missing_ok=True)

    def _queen_claude(self, *, skip_permissions: bool = False, mcp_configs: list[str] | None = None):
        """Launch Queen Bee as an interactive Claude CLI session."""
        # Claude CLI refuses to launch inside another Claude Code session.
        # Since the queen is a top-level interactive session (not nested), clear the guard.
        os.environ.pop("CLAUDECODE", None)

        # Write identity files for compaction persistence
        claude_md, instructions_path = self._queen_write_identity_files()

        # Short system prompt — full instructions are in the file
        short_prompt = "You are the Hive Queen Bee coordinator. Read .hive/queen-instructions.md for your full instructions now."

        claude_cmd = os.environ.get("CLAUDE_CMD", "claude")
        cmd = [
            claude_cmd,
            "--model",
            Config.DEFAULT_MODEL,
            "--append-system-prompt",
            short_prompt,
        ]
        for config in mcp_configs or []:
            cmd.extend(["--mcp-config", config])
        if skip_permissions:
            cmd.append("--dangerously-skip-permissions")
        else:
            cmd.extend(
                [
                    "--allowedTools",
                    "Bash(hive:*) Bash(git:*) Bash(ls:*) Bash(find:*) Bash(rg:*) Read Edit Write",
                ]
            )

        print("Launching Queen Bee TUI (Claude CLI)...\n")

        # Use subprocess.run (not os.execvp) so we can clean up identity files on exit
        try:
            result = subprocess.run(cmd)
            sys.exit(result.returncode)
        except KeyboardInterrupt:
            pass
        finally:
            self._queen_cleanup_identity_files(claude_md, instructions_path)

    def _queen_codex(self):
        """Launch Queen Bee as an interactive Codex CLI session."""
        # Write identity files for compaction persistence (instructions + state files).
        claude_md, instructions_path = self._queen_write_identity_files()

        short_prompt = "Read .hive/queen-instructions.md for your full instructions now."

        # Persistent instructions that survive long chats + compaction.
        # Keep this short and point at the durable instruction/state files.
        developer_instructions = (
            "You are the Hive Queen Bee coordinator. You do NOT write code; you plan, decompose, and monitor.\\n"
            "Full instructions: .hive/queen-instructions.md (read now; re-read after compaction).\\n"
            "Operational state: .hive/queen-state.md (re-read after compaction; update after significant actions).\\n"
            "Before creating issues/epics, output a human-readable plan for user review and wait for explicit approval.\\n"
            "Always use hive --json for Hive CLI commands."
        )

        # Codex compaction prompt (used when the client compacts long threads).
        # Ensure summaries keep the queen's durable context pointers.
        compact_prompt = (
            "Summarize the conversation for continuity.\\n"
            "Preserve: user goals, key decisions, current plan/issues, and next steps.\\n"
            "Always include a reminder to read .hive/queen-instructions.md and .hive/queen-state.md after compaction."
        )

        codex_cmd = os.environ.get("CODEX_CMD", "codex")
        sandbox = os.environ.get("HIVE_CODEX_QUEEN_SANDBOX") or getattr(Config, "CODEX_SANDBOX", "workspace-write")
        approval = os.environ.get("HIVE_CODEX_QUEEN_APPROVAL_POLICY") or getattr(Config, "CODEX_APPROVAL_POLICY", "never")
        cmd = [
            codex_cmd,
            "--sandbox",
            sandbox,
            "--ask-for-approval",
            approval,
            "-c",
            f'developer_instructions="{developer_instructions}"',
            "-c",
            f'compact_prompt="{compact_prompt}"',
            "--cd",
            str(self.project_path),
            short_prompt,
        ]

        print("Launching Queen Bee TUI (Codex CLI)...\n")

        try:
            result = subprocess.run(cmd)
            sys.exit(result.returncode)
        except FileNotFoundError:
            self._error("Codex CLI not found. Install `codex` and ensure it's on PATH, or set CODEX_CMD to the codex executable path.")
        except KeyboardInterrupt:
            pass
        finally:
            self._queen_cleanup_identity_files(claude_md, instructions_path)
