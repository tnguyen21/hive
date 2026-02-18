"""Tests for refinery prompt dual modes."""

import pytest

from hive.prompts import build_refinery_prompt


BASE_KWARGS = {
    "issue_title": "Add audit logging",
    "issue_id": "w-123",
    "branch_name": "agent/worker-9",
    "worktree_path": "/tmp/wt",
}


def test_refinery_prompt_modes_are_distinct():
    """INV-1: mode selection changes prompt instructions deterministically."""
    review_prompt = build_refinery_prompt(**BASE_KWARGS, mode="review")
    integration_prompt = build_refinery_prompt(**BASE_KWARGS, mode="integration")

    assert "Review Mode" in review_prompt
    assert "Integration Mode" in integration_prompt
    assert review_prompt != integration_prompt


def test_review_mode_contains_decision_contract_and_checklist():
    """INV-2: review mode includes explicit approval/rejection/escalation contract."""
    prompt = build_refinery_prompt(**BASE_KWARGS, mode="review")

    assert "DECISION CONTRACT" in prompt
    assert "approved" in prompt
    assert "rejected" in prompt
    assert "escalated" in prompt
    assert "REVIEW CHECKLIST" in prompt
    assert "Evidence-driven" in prompt


def test_integration_mode_preserves_completion_contract_and_steps():
    """INV-3: integration mode preserves completion signal contract."""
    prompt = build_refinery_prompt(**BASE_KWARGS, mode="integration")

    assert ".hive-result.jsonl" in prompt
    assert "merged" in prompt
    assert "needs_human" in prompt
    assert "git rebase main" in prompt


def test_unknown_mode_rejected():
    """Unknown mode input should fail fast."""
    with pytest.raises(ValueError):
        build_refinery_prompt(**BASE_KWARGS, mode="unknown")


def test_review_mode_does_not_use_old_fallback_wording():
    """Review mode should not emit old fallback-only wording."""
    prompt = build_refinery_prompt(**BASE_KWARGS, mode="review")

    assert "Mechanical rebase FAILED" not in prompt
    assert "Rebase succeeded but TESTS FAILED" not in prompt
