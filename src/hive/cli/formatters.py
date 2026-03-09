"""Compatibility wrappers around Rich renderers."""

from .rich_views import (
    render_add_note as _fmt_add_note,
    render_create as _fmt_create,
    render_debug as _fmt_debug,
    render_issue_list as _fmt_list_issues,
    render_issue_show as _fmt_show,
    render_list_agents as _fmt_list_agents,
    render_logs as _fmt_logs,
    render_merges as _fmt_merges,
    render_message as _fmt_message,
    render_metrics as _fmt_metrics,
    render_review as _fmt_review,
    render_start as _fmt_start,
    render_status as _fmt_status,
    render_stop as _fmt_stop,
)

__all__ = [
    "_fmt_add_note",
    "_fmt_create",
    "_fmt_debug",
    "_fmt_list_agents",
    "_fmt_list_issues",
    "_fmt_logs",
    "_fmt_merges",
    "_fmt_message",
    "_fmt_metrics",
    "_fmt_review",
    "_fmt_show",
    "_fmt_start",
    "_fmt_status",
    "_fmt_stop",
]
