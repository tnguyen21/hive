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


def _fmt_epic(result):
    lines = [result.get("message", f"Created epic {result.get('epic_id', '')}")]
    for step in result.get("steps", []):
        lines.append(f"  Step {step['index']}: {step['id']} - {step['title']}")
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
