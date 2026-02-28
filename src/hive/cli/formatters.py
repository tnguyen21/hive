"""Formatter functions for CLI output."""

import json

from ..config import Config


def _fmt_message(result):
    """Generic: print result['message']."""
    return result.get("message", "")


def _fmt_create(result):
    lines = [f"Created issue: {result['id']}"]
    lines.append(f"  Title: {result['title']}")
    lines.append(f"  Priority: {result['priority']}")
    if result.get("tags"):
        lines.append(f"  Tags: {', '.join(result['tags'])}")
    if result.get("depends_on"):
        lines.append(f"  Depends on: {', '.join(result['depends_on'])}")
    return "\n".join(lines)


def _fmt_list_issues(result):
    issues = result.get("issues", [])
    if not issues:
        return "No issues found.\n  Create one with: hive create 'title' 'description'"
    lines = [f"\n{'ID':<12} {'Status':<12} {'Pri':<4} {'Type':<10} {'Title':<40}"]
    lines.append("-" * 80)
    for issue in issues:
        itype = issue.get("type", "")[:10]
        lines.append(f"{issue['id']:<12} {issue['status']:<12} {issue['priority']:<4} {itype:<10} {issue['title'][:40]}")
    lines.append(f"\nTotal: {len(issues)} issues")
    return "\n".join(lines)


def _fmt_show(result):
    lines = [f"\nIssue: {result['id']}"]
    lines.append(f"Title: {result['title']}")
    lines.append(f"Status: {result['status']}")
    lines.append(f"Priority: {result['priority']}")
    lines.append(f"Type: {result['type']}")
    lines.append(f"Assignee: {result['assignee'] or 'None'}")
    if result.get("tags"):
        lines.append(f"Tags: {', '.join(result['tags'])}")
    if result.get("model"):
        lines.append(f"Model: {result['model']}")
    lines.append(f"Created: {result['created_at']}")
    if result.get("description"):
        lines.append(f"\nDescription:\n{result['description']}")
    deps = result.get("dependencies", [])
    if deps:
        lines.append("\nDepends on:")
        for dep in deps:
            lines.append(f"  - {dep['id']}: {dep['title']} ({dep['status']})")
    events = result.get("recent_events", [])
    if events:
        lines.append(f"\nEvents ({len(events)}):")
        for event in events[:10]:
            lines.append(f"  [{event['created_at']}] {event['event_type']}")
            if event["detail"]:
                detail = json.loads(event["detail"]) if isinstance(event["detail"], str) else event["detail"]
                for key, value in detail.items():
                    lines.append(f"    {key}: {value}")
    return "\n".join(lines)


def _fmt_review(result):
    rows = result.get("review", [])
    if not rows:
        return "No done issues pending review."
    # Single-issue review: detailed view
    if result.get("detail"):
        item = rows[0]
        merge_state = item.get("merge_status") or "-"
        assignee = item.get("assignee") or "-"
        lines = [f"\nReview: {item['id']}"]
        lines.append(f"  Title:    {item['title']}")
        lines.append(f"  Status:   {item.get('status', '-')}")
        lines.append(f"  Assignee: {assignee}")
        lines.append(f"  Merge:    {merge_state}")
        lines.append(f"  Updated:  {item.get('updated_at', '-')}")
        if item.get("description"):
            lines.append(f"\n  Description:\n    {item['description']}")
        lines.append("\n  Commands:")
        if item.get("diff_hint"):
            lines.append(f"    Diff:     {item['diff_hint']}")
        if item.get("worktree_hint"):
            lines.append(f"    Worktree: {item['worktree_hint']}")
        if item.get("merge_hint"):
            lines.append(f"    Merge:    {item['merge_hint']}")
        lines.append(f"    Finalize: {item['finalize_hint']}")
        return "\n".join(lines)
    # Multi-issue listing
    lines = [f"\n{'Issue':<14} {'Merge':<10} {'Assignee':<16} {'Title':<40}"]
    lines.append("-" * 88)
    for item in rows:
        merge_state = item.get("merge_status") or "-"
        assignee = item.get("assignee") or "-"
        lines.append(f"{item['id']:<14} {merge_state:<10} {assignee:<16} {item['title'][:40]}")
    lines.append(f"\nTotal: {len(rows)} issue(s) pending finalization")
    lines.append("\nPer-issue review commands:")
    for item in rows:
        lines.append(f"\n{item['id']}:")
        if item.get("diff_hint"):
            lines.append(f"  Diff:     {item['diff_hint']}")
        if item.get("worktree_hint"):
            lines.append(f"  Worktree: {item['worktree_hint']}")
        if item.get("merge_hint"):
            lines.append(f"  Merge:    {item['merge_hint']}")
        lines.append(f"  Finalize: {item['finalize_hint']}")
    return "\n".join(lines)


def _fmt_add_note(result):
    return f"Added note #{result['note_id']} [{result.get('category', 'discovery')}]"


def _fmt_note_with_targets(result):
    return f"Sent note #{result['note_id']} with {result['delivery_count']} delivery(ies)"


def _fmt_status(result):
    lines = ["\n=== Hive Status ==="]
    lines.append(f"\nProject: {result.get('project', '')}")
    lines.append("\nIssues:")
    for s in ["open", "in_progress", "done", "finalized", "escalated", "blocked", "canceled"]:
        count = result.get("issues", {}).get(s, 0)
        if count > 0:
            lines.append(f"  {s}: {count}")
    lines.append(f"\nActive workers: {result.get('active_agents', 0)}/{Config.MAX_AGENTS}")
    for w in result.get("workers", []):
        title = (w.get("issue_title") or "")[:40]
        lines.append(f"  {w.get('name', ''):<24} {w.get('issue_id', ''):<16} {title}")

    refinery = result.get("refinery", {})
    if refinery.get("active"):
        rtitle = (refinery.get("issue_title") or "")[:40]
        lines.append(f"\nRefinery: reviewing {refinery.get('issue_id', '')} ({rtitle})")
    else:
        lines.append("\nRefinery: idle")

    lines.append(f"Ready queue: {result.get('ready_queue', 0)} issues")
    mq = result.get("merge_queue", {})
    if isinstance(mq, dict):
        parts = []
        for k in ["queued", "running", "merged", "failed"]:
            v = mq.get(k, 0)
            if v > 0:
                parts.append(f"{v} {k}")
        lines.append(f"Merge queue: {', '.join(parts) if parts else 'empty'}")
    else:
        lines.append(f"Merge queue: {mq} pending")

    attention = result.get("attention_issues", [])
    if attention:
        lines.append(f"\nNeeds attention ({len(attention)}):")
        for ai in attention[:10]:
            lines.append(f"  {ai['id']:<16} [{ai['status']}] {ai['title'][:40]}")

    blockers = result.get("merge_blockers", [])
    if blockers:
        lines.append("\nMerge blockers:")
        for blocker in blockers:
            lines.append(f"  - {blocker.get('message', blocker.get('type', 'unknown blocker'))}")
            changes = blocker.get("changes") or []
            for line in changes[:5]:
                lines.append(f"    {line}")

    daemon_info = result.get("daemon", {})
    if daemon_info.get("running"):
        lines.append(f"\nDaemon: running (PID {daemon_info.get('pid')})")
        if daemon_info.get("log_file"):
            lines.append(f"  Log: {daemon_info.get('log_file')}")
    else:
        lines.append("\nDaemon: not running")

    total = result.get("total_issues", 0)
    if total == 0:
        lines.append("\n  No issues yet. Create one with: hive create 'title' 'description'")
    return "\n".join(lines)


def _fmt_list_agents(result):
    # Single-agent detail mode
    if "agents" not in result:
        lines = [f"\nAgent: {result.get('id', '')}"]
        lines.append(f"Name: {result.get('name', '')}")
        lines.append(f"Status: {result.get('status', '')}")
        if result.get("current_issue"):
            lines.append(f"Current issue: {result['current_issue']}")
        events = result.get("recent_events", [])
        if events:
            lines.append(f"\nRecent events ({len(events)}):")
            for event in events[:5]:
                lines.append(f"  [{event['created_at']}] {event['event_type']}")
        return "\n".join(lines)
    # List mode
    agents = result.get("agents", [])
    if not agents:
        return "No agents found."
    lines = [f"\n{'ID':<16} {'Name':<16} {'Status':<10} {'Current Issue':<30}"]
    lines.append("-" * 72)
    for agent in agents:
        issue_title = agent.get("current_issue_title", agent.get("current_issue", "")) or "-"
        lines.append(f"{agent['id']:<16} {agent['name']:<16} {agent['status']:<10} {str(issue_title)[:30]}")
    return "\n".join(lines)


def _fmt_mail_inbox(result):
    deliveries = result.get("deliveries", [])
    if not deliveries:
        return "No deliveries found."
    header = f"{'ID':<6} {'Note':<6} {'Status':<10} {'Must Read':<10} {'From':<16} Content"
    lines = [header, "-" * len(header)]
    for d in deliveries:
        content_preview = (d.get("content") or "")[:40]
        must_read_label = "yes" if d.get("must_read") else "no"
        from_agent = d.get("from_agent_id") or "-"
        lines.append(f"{d['id']:<6} {d['note_id']:<6} {d['status']:<10} {must_read_label:<10} {from_agent:<16} {content_preview}")
    return "\n".join(lines)


def _fmt_mail_read(result):
    if result.get("updated"):
        return f"Delivery #{result['delivery_id']} marked read."
    return f"Delivery #{result['delivery_id']} unchanged (already read or not found)."


def _fmt_mail_ack(result):
    if result.get("updated"):
        return f"Delivery #{result['delivery_id']} acked."
    return f"Delivery #{result['delivery_id']} unchanged (not must_read, already acked, or not found)."


def _fmt_merges(result):
    entries = result.get("merges", [])
    status_counts = result.get("status_counts", {})
    if not entries:
        return "No merge queue entries found."
    lines = [f"\n{'ID':<6} {'Status':<10} {'Issue':<14} {'Title':<30} {'Branch':<25} {'Enqueued'}"]
    lines.append("-" * 100)
    for e in entries:
        title = (e.get("issue_title") or "")[:30]
        branch = (e.get("branch_name") or "")[:25]
        lines.append(f"{e['id']:<6} {e['status']:<10} {e['issue_id']:<14} {title:<30} {branch:<25} {e.get('enqueued_at', '')}")
    summary_parts = [f"{count} {s}" for s, count in status_counts.items() if count > 0]
    lines.append(f"\n{', '.join(summary_parts)}")
    return "\n".join(lines)


def _fmt_debug(result):
    from ..diag import format_report_text

    return format_report_text(result)


def _fmt_metrics(result):
    view = result.get("view")
    if view == "group_by":
        results = result.get("results", [])
        if not results:
            return "No performance data yet."
        group_label = result.get("group_label", "Group")
        group_key = result.get("group_key", "group")
        lines = [f"{'Model':<35} {group_label:<15} {'Issues':>6} {'OK':>4} {'Esc':>4} {'Retries':>7} {'Avg Min':>8}"]
        lines.append("-" * 85)
        for r in results:
            model_name = (r.get("model") or "unknown")[:34]
            group_val = str(r.get(group_key, ""))[:14]
            lines.append(
                f"{model_name:<35} {group_val:<15} {r.get('issue_count', 0):>6} {r.get('successes', 0):>4} {r.get('escalations', 0):>4} {r.get('total_retries', 0):>7} {r.get('avg_duration_minutes', 0):>8}"
            )
        return "\n".join(lines)
    elif view == "costs":
        lines = ["\n=== Token Usage & Costs ==="]
        if result.get("issue_id"):
            lines.append(f"Issue: {result['issue_id']}")
        elif result.get("agent_id"):
            lines.append(f"Agent: {result['agent_id']}")
        else:
            lines.append("Project-wide")
        lines.append(f"\nTotal tokens: {result['total_tokens']:,}")
        lines.append(f"  Input tokens: {result['total_input_tokens']:,}")
        lines.append(f"  Output tokens: {result['total_output_tokens']:,}")
        lines.append(f"Estimated cost: ${result['estimated_cost_usd']:.4f}")
        if not result.get("issue_id") and not result.get("agent_id"):
            issue_breakdown = result.get("issue_breakdown", {})
            if issue_breakdown:
                lines.append("\n=== Top Issues by Token Usage ===")
                sorted_issues = sorted(issue_breakdown.items(), key=lambda x: x[1]["input_tokens"] + x[1]["output_tokens"], reverse=True)
                for issue, tokens in sorted_issues[:10]:
                    total = tokens["input_tokens"] + tokens["output_tokens"]
                    lines.append(f"{issue}: {total:,} tokens")
            agent_breakdown = result.get("agent_breakdown", {})
            if agent_breakdown:
                lines.append("\n=== Top Agents by Token Usage ===")
                sorted_agents = sorted(agent_breakdown.items(), key=lambda x: x[1]["input_tokens"] + x[1]["output_tokens"], reverse=True)
                for agent, tokens in sorted_agents[:10]:
                    total = tokens["input_tokens"] + tokens["output_tokens"]
                    lines.append(f"{agent}: {total:,} tokens")
            model_breakdown = result.get("model_breakdown", {})
            if model_breakdown:
                lines.append("\n=== Usage by Model ===")
                for model_name, tokens in model_breakdown.items():
                    total = tokens["input_tokens"] + tokens["output_tokens"]
                    lines.append(f"{model_name}: {total:,} tokens")
        return "\n".join(lines)
    else:  # default view
        results = result.get("metrics", [])
        summary = result.get("summary", {})
        if not results:
            return "No metrics data yet."
        for r in results:
            r["avg_duration_m"] = round(r["avg_duration_s"] / 60, 1) if r["avg_duration_s"] else 0
        lines = [f"{'Model':<35} {'Runs':>5} {'Success%':>9} {'Avg Duration':>12} {'Avg Retries':>12} {'Merge Health':>12}"]
        lines.append("-" * 95)
        for r in results:
            model_name = (r.get("model") or "unknown")[:34]
            success_pct = f"{r.get('success_rate', 0):.1f}%"
            avg_dur = f"{r.get('avg_duration_m', 0):.1f}m"
            avg_ret = f"{r.get('avg_retries', 0):.1f}"
            merge_health = f"{r.get('merge_health', 0):.1f}%" if r.get("merge_health") is not None else "N/A"
            lines.append(f"{model_name:<35} {r.get('runs', 0):>5} {success_pct:>9} {avg_dur:>12} {avg_ret:>12} {merge_health:>12}")
        lines.append("")
        lines.append(
            f"Escalation rate: {summary.get('escalation_rate', 0)}% | Mean time to resolution: {summary.get('mean_time_to_resolution_minutes', 0)}m"
        )
        return "\n".join(lines)


def _fmt_logs(result):
    events = result.get("events", [])
    lines = []
    for event in events:
        ts = event["created_at"]
        etype = event["event_type"]
        issue = event["issue_id"] or "-"
        agent = event["agent_id"] or "-"
        line = f"{ts}  {etype:<24s}  issue={issue:<10s}  agent={agent:<10s}"
        if event.get("detail"):
            try:
                detail = json.loads(event["detail"]) if isinstance(event["detail"], str) else event["detail"]
                parts = [f"{k}={v}" for k, v in detail.items()]
                line += "  " + " ".join(parts)
            except (json.JSONDecodeError, TypeError):
                line += f"  {event['detail']}"
        lines.append(line)
    return "\n".join(lines)


def _fmt_start(result):
    status = result.get("status")
    if status == "already_running":
        return f"Hive daemon already running (PID {result['pid']})"
    elif status == "started":
        return f"Hive daemon started (PID {result['pid']})\n  Log: {result.get('log_file')}"
    return None


def _fmt_stop(result):
    status = result.get("status")
    if status == "not_running":
        return "Hive daemon is not running."
    elif status == "stopped":
        return f"Hive daemon stopped (was PID {result['pid']})"
    return None
