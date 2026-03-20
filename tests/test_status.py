"""Tests for hive.status — enums, parsing, and status groupings."""

from hive.status import (
    CLOSED_ISSUE_STATUSES,
    ISSUE_STATUS_ORDER,
    UNBLOCKING_ISSUE_STATUSES,
    BackendSessionStatusType,
    IssueStatus,
    parse_backend_session_status_type,
    session_status_payload,
)


# ── parse_backend_session_status_type ────────────────────────────────────


class TestParseBackendSessionStatusType:
    def test_enum_passthrough(self):
        assert parse_backend_session_status_type(BackendSessionStatusType.IDLE) is BackendSessionStatusType.IDLE

    def test_valid_string(self):
        assert parse_backend_session_status_type("busy") is BackendSessionStatusType.BUSY

    def test_all_valid_strings(self):
        for member in BackendSessionStatusType:
            assert parse_backend_session_status_type(member.value) is member

    def test_invalid_string(self):
        assert parse_backend_session_status_type("nonexistent") is None

    def test_non_string(self):
        assert parse_backend_session_status_type(42) is None

    def test_none(self):
        assert parse_backend_session_status_type(None) is None


# ── session_status_payload ───────────────────────────────────────────────


class TestSessionStatusPayload:
    def test_structure(self):
        result = session_status_payload("sess-1", BackendSessionStatusType.IDLE)
        assert result == {"sessionID": "sess-1", "status": {"type": "idle"}}

    def test_with_status_type(self):
        result = session_status_payload("sess-2", BackendSessionStatusType.ERROR)
        assert result == {"sessionID": "sess-2", "status": {"type": "error"}}

    def test_keys(self):
        result = session_status_payload("s", BackendSessionStatusType.BUSY)
        assert set(result.keys()) == {"sessionID", "status"}
        assert set(result["status"].keys()) == {"type"}


# ── Status grouping membership ───────────────────────────────────────────


class TestStatusGroupings:
    def test_unblocking_contains_done(self):
        assert IssueStatus.DONE in UNBLOCKING_ISSUE_STATUSES

    def test_unblocking_contains_finalized(self):
        assert IssueStatus.FINALIZED in UNBLOCKING_ISSUE_STATUSES

    def test_unblocking_contains_canceled(self):
        assert IssueStatus.CANCELED in UNBLOCKING_ISSUE_STATUSES

    def test_unblocking_excludes_open(self):
        assert IssueStatus.OPEN not in UNBLOCKING_ISSUE_STATUSES

    def test_unblocking_excludes_in_progress(self):
        assert IssueStatus.IN_PROGRESS not in UNBLOCKING_ISSUE_STATUSES

    def test_unblocking_excludes_escalated(self):
        assert IssueStatus.ESCALATED not in UNBLOCKING_ISSUE_STATUSES

    def test_closed_contains_done(self):
        assert IssueStatus.DONE in CLOSED_ISSUE_STATUSES

    def test_closed_contains_finalized(self):
        assert IssueStatus.FINALIZED in CLOSED_ISSUE_STATUSES

    def test_closed_contains_canceled(self):
        assert IssueStatus.CANCELED in CLOSED_ISSUE_STATUSES

    def test_closed_contains_escalated(self):
        assert IssueStatus.ESCALATED in CLOSED_ISSUE_STATUSES

    def test_closed_excludes_open(self):
        assert IssueStatus.OPEN not in CLOSED_ISSUE_STATUSES

    def test_closed_excludes_in_progress(self):
        assert IssueStatus.IN_PROGRESS not in CLOSED_ISSUE_STATUSES


# ── ISSUE_STATUS_ORDER completeness ──────────────────────────────────────


class TestIssueStatusOrder:
    def test_covers_all_statuses(self):
        assert set(ISSUE_STATUS_ORDER) == set(IssueStatus)

    def test_no_duplicates(self):
        assert len(ISSUE_STATUS_ORDER) == len(set(ISSUE_STATUS_ORDER))

    def test_length_matches_enum(self):
        assert len(ISSUE_STATUS_ORDER) == len(IssueStatus)
