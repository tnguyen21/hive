"""Rich renderers for CLI output."""

from __future__ import annotations

import json

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..config import Config


def _kv_panel(title: str, rows: list[tuple[str, str]], *, border_style: str = "blue"):
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold cyan", no_wrap=True)
    table.add_column()
    for label, value in rows:
        table.add_row(label, value)
    return Panel.fit(table, title=title, border_style=border_style)


def _simple_table(*columns: tuple[str, dict]):
    table = Table(box=box.SIMPLE_HEAVY, header_style="bold")
    for name, kwargs in columns:
        table.add_column(name, **kwargs)
    return table


def render_message(result: dict):
    return Text(result.get("message", ""))


def render_create(result: dict):
    rows = [
        ("Issue", result["id"]),
        ("Title", result["title"]),
        ("Priority", str(result["priority"])),
    ]
    if result.get("tags"):
        rows.append(("Tags", ", ".join(result["tags"])))
    if result.get("depends_on"):
        rows.append(("Depends on", ", ".join(result["depends_on"])))
    return _kv_panel("Created", rows, border_style="green")


def render_issue_list(result: dict):
    issues = result.get("issues", [])
    if not issues:
        return Group(
            Text("No issues found."),
            Text("Create one with: hive create 'title' 'description'", style="dim"),
        )

    table = _simple_table(
        ("ID", {"style": "cyan", "no_wrap": True}),
        ("Status", {"style": "magenta"}),
        ("Pri", {"justify": "right"}),
        ("Type", {}),
        ("Title", {"overflow": "fold"}),
    )
    for issue in issues:
        table.add_row(
            issue["id"],
            issue["status"],
            str(issue["priority"]),
            str(issue.get("type", ""))[:10],
            str(issue["title"]),
        )
    return Group(table, Text(f"Total: {len(issues)} issues", style="dim"))


def render_issue_show(result: dict):
    rows = [
        ("Issue", result["id"]),
        ("Title", result["title"]),
        ("Status", result["status"]),
        ("Priority", str(result["priority"])),
        ("Type", result["type"]),
        ("Assignee", result["assignee"] or "None"),
        ("Created", str(result["created_at"])),
    ]
    if result.get("tags"):
        rows.append(("Tags", ", ".join(result["tags"])))
    if result.get("model"):
        rows.append(("Model", str(result["model"])))

    renderables = [_kv_panel("Issue", rows)]

    if result.get("description"):
        renderables.append(Panel(result["description"], title="Description", border_style="white"))

    dependencies = result.get("dependencies", [])
    if dependencies:
        dep_table = _simple_table(
            ("Depends on", {"style": "cyan", "no_wrap": True}),
            ("Status", {"style": "magenta"}),
            ("Title", {}),
        )
        for dep in dependencies:
            dep_table.add_row(dep["id"], dep["status"], dep["title"])
        renderables.append(dep_table)

    events = result.get("recent_events", [])
    if events:
        event_table = Table(box=box.SIMPLE)
        event_table.add_column("When", style="dim", no_wrap=True)
        event_table.add_column("Event", style="bold")
        event_table.add_column("Detail")
        for event in events[:10]:
            detail = ""
            if event.get("detail"):
                parsed = json.loads(event["detail"]) if isinstance(event["detail"], str) else event["detail"]
                detail = ", ".join(f"{key}={value}" for key, value in parsed.items())
            event_table.add_row(str(event["created_at"]), str(event["event_type"]), detail)
        renderables.append(event_table)

    return Group(*renderables)


def render_review(result: dict):
    rows = result.get("review", [])
    if not rows:
        return Text("No done issues pending review.")
    if result.get("detail"):
        item = rows[0]
        summary = [
            ("Review", item["id"]),
            ("Title", item["title"]),
            ("Status", item.get("status", "-")),
            ("Assignee", item.get("assignee") or "-"),
            ("Merge", item.get("merge_status") or "-"),
            ("Updated", item.get("updated_at", "-")),
        ]
        renderables = [_kv_panel("Review", summary, border_style="yellow")]
        if item.get("description"):
            renderables.append(Panel(item["description"], title="Description", border_style="white"))
        commands = []
        for label, key in [
            ("Diff", "diff_hint"),
            ("Worktree", "worktree_hint"),
            ("Merge", "merge_hint"),
            ("Finalize", "finalize_hint"),
        ]:
            if item.get(key):
                commands.append((label, item[key]))
        renderables.append(_kv_panel("Commands", commands, border_style="magenta"))
        return Group(*renderables)

    table = _simple_table(
        ("Issue", {"style": "cyan", "no_wrap": True}),
        ("Merge", {"style": "magenta"}),
        ("Assignee", {}),
        ("Title", {"overflow": "fold"}),
    )
    details = []
    for item in rows:
        table.add_row(
            item["id"],
            item.get("merge_status") or "-",
            item.get("assignee") or "-",
            item["title"],
        )
        commands = []
        for label, key in [
            ("Diff", "diff_hint"),
            ("Worktree", "worktree_hint"),
            ("Merge", "merge_hint"),
            ("Finalize", "finalize_hint"),
        ]:
            if item.get(key):
                commands.append((label, item[key]))
        details.append(_kv_panel(item["id"], commands, border_style="magenta"))
    return Group(table, Text(f"Total: {len(rows)} issue(s) pending finalization", style="dim"), *details)


def render_add_note(result: dict):
    return Text(f"Added note #{result['note_id']} [{result.get('category', 'discovery')}]")


def render_status(result: dict):
    renderables = [
        _kv_panel(
            "Hive Status",
            [
                ("Project", result.get("project", "")),
                ("Active workers", f"{result.get('active_agents', 0)}/{Config.MAX_AGENTS}"),
                ("Refinery", _render_refinery(result)),
                ("Ready queue", f"{result.get('ready_queue', 0)} issues"),
                ("Merge queue", _render_merge_queue(result.get("merge_queue", {}))),
                ("Daemon", _render_daemon(result.get("daemon", {}))),
            ],
            border_style="cyan",
        )
    ]

    issues = result.get("issues", {})
    if issues:
        issues_table = _simple_table(("Status", {"style": "magenta"}), ("Count", {"justify": "right"}))
        for status in ["open", "in_progress", "done", "finalized", "escalated", "blocked", "canceled"]:
            count = issues.get(status, 0)
            if count > 0:
                issues_table.add_row(status, str(count))
        renderables.append(Panel(issues_table, title="Issues", border_style="blue"))

    workers = result.get("workers", [])
    if workers:
        workers_table = _simple_table(("Name", {}), ("Issue", {"style": "cyan"}), ("Title", {"overflow": "fold"}))
        for worker in workers:
            workers_table.add_row(worker.get("name", ""), worker.get("issue_id", ""), (worker.get("issue_title") or "")[:40])
        renderables.append(Panel(workers_table, title="Workers", border_style="green"))

    attention = result.get("attention_issues", [])
    if attention:
        attention_table = _simple_table(("Issue", {"style": "cyan"}), ("Status", {"style": "magenta"}), ("Title", {"overflow": "fold"}))
        for item in attention[:10]:
            attention_table.add_row(item["id"], item["status"], item["title"])
        renderables.append(Panel(attention_table, title="Needs Attention", border_style="yellow"))

    blockers = result.get("merge_blockers", [])
    if blockers:
        blocker_lines = []
        for blocker in blockers:
            blocker_lines.append(blocker.get("message", blocker.get("type", "unknown blocker")))
            for change in (blocker.get("changes") or [])[:5]:
                blocker_lines.append(f"  {change}")
        renderables.append(Panel(Text("\n".join(blocker_lines)), title="Merge Blockers", border_style="red"))

    if result.get("total_issues", 0) == 0:
        renderables.append(Text("No issues yet. Create one with: hive create 'title' 'description'", style="dim"))

    return Group(*renderables)


def _render_refinery(result: dict) -> str:
    refinery = result.get("refinery", {})
    if refinery.get("active"):
        return f"reviewing {refinery.get('issue_id', '')} ({(refinery.get('issue_title') or '')[:40]})"
    return "idle"


def _render_merge_queue(mq) -> str:
    if isinstance(mq, dict):
        parts = [f"{mq.get(key, 0)} {key}" for key in ["queued", "running", "merged", "failed"] if mq.get(key, 0) > 0]
        return ", ".join(parts) if parts else "empty"
    return f"{mq} pending"


def _render_daemon(daemon_info: dict) -> str:
    if daemon_info.get("running"):
        return f"running (PID {daemon_info.get('pid')})"
    return "not running"


def render_list_agents(result: dict):
    if "agents" not in result:
        rows = [
            ("Agent", result.get("id", "")),
            ("Name", result.get("name", "")),
            ("Status", result.get("status", "")),
        ]
        if result.get("current_issue"):
            rows.append(("Current issue", result["current_issue"]))
        renderables = [_kv_panel("Agent", rows)]
        events = result.get("recent_events", [])
        if events:
            event_table = Table(box=box.SIMPLE)
            event_table.add_column("When", style="dim")
            event_table.add_column("Event")
            for event in events[:5]:
                event_table.add_row(str(event["created_at"]), str(event["event_type"]))
            renderables.append(event_table)
        return Group(*renderables)

    agents = result.get("agents", [])
    if not agents:
        return Text("No agents found.")
    table = _simple_table(
        ("ID", {"style": "cyan", "no_wrap": True}),
        ("Name", {}),
        ("Status", {"style": "magenta"}),
        ("Current Issue", {"overflow": "fold"}),
    )
    for agent in agents:
        issue_title = agent.get("current_issue_title", agent.get("current_issue", "")) or "-"
        table.add_row(agent["id"], agent["name"], agent["status"], str(issue_title))
    return table


def render_merges(result: dict):
    entries = result.get("merges", [])
    if not entries:
        return Text("No merge queue entries found.")
    table = _simple_table(
        ("ID", {"style": "cyan", "no_wrap": True}),
        ("Status", {"style": "magenta"}),
        ("Issue", {"style": "cyan"}),
        ("Title", {"overflow": "fold"}),
        ("Branch", {"overflow": "fold"}),
        ("Enqueued", {}),
    )
    for entry in entries:
        table.add_row(
            str(entry["id"]),
            entry["status"],
            entry["issue_id"],
            (entry.get("issue_title") or "")[:30],
            (entry.get("branch_name") or "")[:25],
            entry.get("enqueued_at", ""),
        )
    summary_parts = [f"{count} {status}" for status, count in result.get("status_counts", {}).items() if count > 0]
    return Group(table, Text(", ".join(summary_parts), style="dim"))


def render_debug(result: dict):
    from ..diag import format_report_text

    return Panel(Text(format_report_text(result)), title="Debug Report", border_style="blue")


def render_metrics(result: dict):
    view = result.get("view")
    if view == "group_by":
        results = result.get("results", [])
        if not results:
            return Text("No performance data yet.")
        table = _simple_table(
            ("Model", {"overflow": "fold"}),
            (result.get("group_label", "Group"), {}),
            ("Issues", {"justify": "right"}),
            ("OK", {"justify": "right"}),
            ("Esc", {"justify": "right"}),
            ("Retries", {"justify": "right"}),
            ("Avg Min", {"justify": "right"}),
        )
        group_key = result.get("group_key", "group")
        for row in results:
            table.add_row(
                (row.get("model") or "unknown")[:34],
                str(row.get(group_key, ""))[:14],
                str(row.get("issue_count", 0)),
                str(row.get("successes", 0)),
                str(row.get("escalations", 0)),
                str(row.get("total_retries", 0)),
                str(row.get("avg_duration_minutes", 0)),
            )
        return table

    if view == "costs":
        rows = [
            ("Scope", result.get("issue_id") or result.get("agent_id") or "Project-wide"),
            ("Total tokens", f"{result['total_tokens']:,}"),
            ("Input tokens", f"{result['total_input_tokens']:,}"),
            ("Output tokens", f"{result['total_output_tokens']:,}"),
            ("Estimated cost", f"${result['estimated_cost_usd']:.4f}"),
        ]
        renderables = [_kv_panel("Token Usage & Costs", rows, border_style="green")]
        for title, key in [
            ("Top Issues by Token Usage", "issue_breakdown"),
            ("Top Agents by Token Usage", "agent_breakdown"),
            ("Usage by Model", "model_breakdown"),
        ]:
            breakdown = result.get(key, {})
            if breakdown and not result.get("issue_id") and not result.get("agent_id"):
                table = _simple_table(("Item", {"overflow": "fold"}), ("Tokens", {"justify": "right"}))
                items = breakdown.items()
                if key != "model_breakdown":
                    items = sorted(items, key=lambda item: item[1]["input_tokens"] + item[1]["output_tokens"], reverse=True)[:10]
                for name, tokens in items:
                    total = tokens["input_tokens"] + tokens["output_tokens"]
                    table.add_row(str(name), f"{total:,}")
                renderables.append(Panel(table, title=title, border_style="blue"))
        return Group(*renderables)

    metrics = result.get("metrics", [])
    if not metrics:
        return Text("No metrics data yet.")
    table = _simple_table(
        ("Model", {"overflow": "fold"}),
        ("Runs", {"justify": "right"}),
        ("Success%", {"justify": "right"}),
        ("Avg Duration", {"justify": "right"}),
        ("Avg Retries", {"justify": "right"}),
        ("Merge Health", {"justify": "right"}),
    )
    for row in metrics:
        avg_duration = round(row["avg_duration_s"] / 60, 1) if row["avg_duration_s"] else 0
        merge_health = f"{row.get('merge_health', 0):.1f}%" if row.get("merge_health") is not None else "N/A"
        table.add_row(
            (row.get("model") or "unknown")[:34],
            str(row.get("runs", 0)),
            f"{row.get('success_rate', 0):.1f}%",
            f"{avg_duration:.1f}m",
            f"{row.get('avg_retries', 0):.1f}",
            merge_health,
        )
    summary = result.get("summary", {})
    return Group(
        table,
        Text(
            f"Escalation rate: {summary.get('escalation_rate', 0)}% | Mean time to resolution: {summary.get('mean_time_to_resolution_minutes', 0)}m",
            style="dim",
        ),
    )


def render_logs(result: dict):
    events = result.get("events", [])
    if not events:
        return Text("")
    table = Table(box=box.SIMPLE)
    table.add_column("When", style="dim")
    table.add_column("Event", style="bold")
    table.add_column("Issue", style="cyan")
    table.add_column("Agent", style="magenta")
    table.add_column("Detail", overflow="fold")
    for event in events:
        detail = ""
        if event.get("detail"):
            try:
                parsed = json.loads(event["detail"]) if isinstance(event["detail"], str) else event["detail"]
                detail = " ".join(f"{key}={value}" for key, value in parsed.items())
            except (json.JSONDecodeError, TypeError):
                detail = str(event["detail"])
        table.add_row(
            str(event["created_at"]),
            str(event["event_type"]),
            event["issue_id"] or "-",
            event["agent_id"] or "-",
            detail,
        )
    return table


def render_start(result: dict):
    if result.get("status") == "already_running":
        return Text(f"Hive daemon already running (PID {result['pid']})")
    if result.get("status") == "started":
        return _kv_panel(
            "Daemon Started",
            [("PID", str(result["pid"])), ("Log", str(result.get("log_file")))],
            border_style="green",
        )
    return None


def render_stop(result: dict):
    if result.get("status") == "not_running":
        return Text("Hive daemon is not running.")
    if result.get("status") == "stopped":
        return Text(f"Hive daemon stopped (was PID {result['pid']})")
    return None


def render_error(message: str):
    return Text(f"Error: {message}", style="bold red")


def print_error(console: Console, message: str) -> None:
    """Render an error message."""
    console.print(render_error(message))
