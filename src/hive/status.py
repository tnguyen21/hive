"""Shared domain status enums and status groupings."""

from enum import StrEnum
from typing import Any


class IssueStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FINALIZED = "finalized"
    ESCALATED = "escalated"
    CANCELED = "canceled"


ISSUE_STATUS_ORDER = (
    IssueStatus.OPEN,
    IssueStatus.IN_PROGRESS,
    IssueStatus.DONE,
    IssueStatus.FINALIZED,
    IssueStatus.ESCALATED,
    IssueStatus.CANCELED,
)

UNBLOCKING_ISSUE_STATUSES = (
    IssueStatus.DONE,
    IssueStatus.FINALIZED,
    IssueStatus.CANCELED,
)

CLOSED_ISSUE_STATUSES = (
    IssueStatus.DONE,
    IssueStatus.FINALIZED,
    IssueStatus.CANCELED,
    IssueStatus.ESCALATED,
)


class BackendSessionStatusType(StrEnum):
    IDLE = "idle"
    BUSY = "busy"
    ERROR = "error"
    NOT_FOUND = "not_found"


SESSION_STATUS_EVENT = "session.status"


def parse_backend_session_status_type(value: Any) -> BackendSessionStatusType | None:
    """Best-effort coercion for backend status payload values."""
    if isinstance(value, BackendSessionStatusType):
        return value
    if isinstance(value, str):
        try:
            return BackendSessionStatusType(value)
        except ValueError:
            return None
    return None


def session_status_payload(
    session_id: str,
    status_type: BackendSessionStatusType,
) -> dict[str, object]:
    """Build the normalized session.status event payload."""
    return {"sessionID": session_id, "status": {"type": str(status_type)}}
