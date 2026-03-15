"""Shared domain status enums and status groupings."""

from enum import StrEnum


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


class BackendSessionState(StrEnum):
    IDLE = "idle"
    BUSY = "busy"


class BackendSessionStatusType(StrEnum):
    IDLE = "idle"
    BUSY = "busy"
    ERROR = "error"
    NOT_FOUND = "not_found"
