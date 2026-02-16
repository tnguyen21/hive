"""Tests for hive.diag diagnostic report module."""

import json

import pytest

from hive.config import Config
from hive.diag import format_report_text, gather_report


@pytest.fixture
def report(temp_db, tmp_path):
    """Gather a diagnostic report using the temp DB and a scratch project dir."""
    return gather_report(temp_db, str(tmp_path), "test-project")


def test_gather_report_returns_all_sections(report):
    expected_keys = {
        "generated_at",
        "system",
        "config",
        "daemon",
        "doctor",
        "db_stats",
        "recent_events",
        "daemon_log_tail",
        "backend_reachability",
    }
    assert expected_keys.issubset(report.keys())


def test_system_info_has_versions(report):
    sys_info = report["system"]
    assert "python_version" in sys_info
    assert "hive_version" in sys_info
    assert sys_info["hive_version"] == "0.1.0"
    assert "platform" in sys_info


def test_config_sanitizes_password(temp_db, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "supersecret")
    Config.load(project_root=tmp_path)
    report = gather_report(temp_db, str(tmp_path), "test-project")
    cfg = report["config"]
    pw_entry = next(e for e in cfg if e["field"] == "OPENCODE_PASSWORD")
    assert pw_entry["value"] == "***"
    assert pw_entry["source"] == "env"


def test_db_stats_row_counts(temp_db, tmp_path):
    temp_db.create_issue("Test issue", "desc", project="test-project")
    report = gather_report(temp_db, str(tmp_path), "test-project")
    db_stats = report["db_stats"]
    assert "row_counts" in db_stats
    assert db_stats["row_counts"]["issues"] >= 1


def test_doctor_checks_included(report):
    doctor = report["doctor"]
    assert isinstance(doctor, list)
    assert len(doctor) == 7
    for check in doctor:
        assert "id" in check
        assert "status" in check
        assert "description" in check


def test_recent_events_captured(temp_db, tmp_path):
    temp_db.create_issue("Event test", "desc", project="test-project")
    report = gather_report(temp_db, str(tmp_path), "test-project")
    events = report["recent_events"]
    assert isinstance(events, list)
    assert any(e["event_type"] == "created" for e in events)


def test_format_report_text_readable(report):
    text = format_report_text(report)
    assert "HIVE DIAGNOSTIC REPORT" in text
    assert "--- System ---" in text
    assert "--- Config ---" in text
    assert "--- Doctor Checks ---" in text
    assert "--- DB Stats ---" in text
    assert "END OF REPORT" in text


def test_json_serializable(report):
    # Should not raise
    serialized = json.dumps(report, default=str)
    assert isinstance(serialized, str)
    parsed = json.loads(serialized)
    assert "system" in parsed
