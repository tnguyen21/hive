"""Completion handling mixin for the Hive orchestrator."""

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..status import IssueStatus
from ..utils import AgentIdentity, CompletionResult
from .deps import deps

logger = logging.getLogger(__name__)


def _exc_detail(e: BaseException) -> str:
    """Return a non-empty, human-readable string describing an exception.

    str(e) is empty for exceptions like asyncio.TimeoutError that carry no
    message. In that case fall back to the exception type name, and if a
    message is present prefix it with the type name for extra clarity.
    """
    msg = str(e)
    name = type(e).__name__
    if not msg:
        return name
    return f"{name}: {msg}"


class CompletionTransition(StrEnum):
    """Transition outcomes for completion handling."""

    SKIP = "skip"
    FAIL = "fail"
    SUCCESS = "success"


class EscalationDecision(StrEnum):
    """Escalation routing decision after a failure."""

    RETRY = "retry"
    AGENT_SWITCH = "agent_switch"
    ESCALATE = "escalate"
    ANOMALY_ESCALATE = "anomaly_escalate"


@dataclass
class CompletionDecision:
    """Decision payload from completion transition analysis."""

    transition: CompletionTransition
    result: CompletionResult | None = None
    skip_reason: str | None = None
    failure_event_type: str | None = None
    failure_event_detail: dict[str, Any] | None = None


class CompletionMixin:
    """Mixin providing agent completion and failure handling."""

    async def _decide_completion_transition(self, agent: AgentIdentity, file_result: dict[str, Any] | None = None) -> CompletionDecision:
        """Decision phase for completion handling.

        Determines the next completion transition and any payload required by
        transition side effects.
        """
        terminal_issue = self.db.get_issue(agent.issue_id)
        if terminal_issue and terminal_issue.get("status") in (IssueStatus.CANCELED, IssueStatus.FINALIZED):
            return CompletionDecision(
                transition=CompletionTransition.SKIP,
                skip_reason=f"issue already {terminal_issue['status']}, cleaning up session",
            )

        backend = self._backend_for_session(agent.session_id)
        msgs = await backend.get_messages(agent.session_id, directory=agent.worktree)
        self._log_token_usage(agent, msgs)

        if deps.Config.MAX_TOKENS_PER_ISSUE:
            budget_tokens = self.db.get_issue_token_total(agent.issue_id)
            if budget_tokens > deps.Config.MAX_TOKENS_PER_ISSUE:
                logger.warning(f"Issue {agent.issue_id} exceeded token budget ({budget_tokens} > {deps.Config.MAX_TOKENS_PER_ISSUE})")
                return CompletionDecision(
                    transition=CompletionTransition.FAIL,
                    result=CompletionResult(
                        success=False,
                        reason=f"Exceeded per-issue token budget ({budget_tokens} > {deps.Config.MAX_TOKENS_PER_ISSUE})",
                        summary=f"Terminated: per-issue token budget exceeded ({budget_tokens} tokens)",
                    ),
                    failure_event_type="budget_exceeded",
                    failure_event_detail={
                        "issue_tokens": budget_tokens,
                        "limit": deps.Config.MAX_TOKENS_PER_ISSUE,
                    },
                )

        result = deps.assess_completion(file_result=file_result)
        if not result.success:
            return CompletionDecision(
                transition=CompletionTransition.FAIL,
                result=result,
            )

        has_commits = await deps.has_diff_from_main_async(agent.worktree)
        if not has_commits:
            return CompletionDecision(
                transition=CompletionTransition.FAIL,
                result=CompletionResult(
                    success=False,
                    reason="No commits relative to main despite claiming success",
                    summary=result.summary,
                ),
                failure_event_type="validation_failed",
                failure_event_detail={
                    "reason": "No commits relative to main despite claiming success",
                    "original_summary": result.summary,
                },
            )

        return CompletionDecision(
            transition=CompletionTransition.SUCCESS,
            result=result,
        )

    # Completion transition table:
    # - SKIP     -> log agent_complete_skipped
    # - FAIL     -> optionally log a failure event + _handle_agent_failure
    # - SUCCESS  -> update done + enqueue merge + log completed

    def _harvest_worker_notes(self, agent: AgentIdentity):
        """Best-effort note harvesting from the worker worktree."""
        # Harvest notes before terminal checks so canceled/failed workers'
        # discoveries are still preserved.
        try:
            notes_data = deps.read_notes_file(agent.worktree)
            if notes_data:
                for note in notes_data:
                    self.db.add_note(
                        issue_id=agent.issue_id,
                        agent_id=agent.agent_id,
                        content=note.get("content", ""),
                        category=note.get("category", "discovery"),
                        project=agent.project,
                    )
                self.db.log_event(agent.issue_id, agent.agent_id, "notes_harvested", {"count": len(notes_data)})
                logger.info(f"Harvested {len(notes_data)} notes from {agent.name}")
        except Exception as e:
            logger.warning(f"Failed to harvest notes from {agent.name}: {e}")
        finally:
            deps.remove_notes_file(agent.worktree)

    async def _handle_completion_failure(self, agent: AgentIdentity, decision: CompletionDecision):
        """Apply completion failure side effects and route through failure handling."""
        if decision.failure_event_type:
            self.db.log_event(
                agent.issue_id,
                agent.agent_id,
                decision.failure_event_type,
                decision.failure_event_detail or {},
            )

        if decision.result is None:
            raise RuntimeError("Missing completion result for failure transition")
        await self._handle_agent_failure(agent, decision.result)

    async def _handle_completion_success(
        self,
        agent: AgentIdentity,
        decision: CompletionDecision,
        file_result: dict[str, Any] | None = None,
    ) -> bool:
        """Apply success side effects. Returns True when teardown should remove the worktree."""
        if decision.result is None:
            raise RuntimeError("Missing completion result for success transition")

        transitioned = self.db.try_transition_issue_status(
            agent.issue_id,
            from_status=IssueStatus.IN_PROGRESS,
            to_status=IssueStatus.DONE,
            expected_assignee=agent.agent_id,
        )
        if not transitioned:
            current_issue = self.db.get_issue(agent.issue_id)
            current_status = current_issue.get("status") if current_issue else None
            if current_status != IssueStatus.DONE:
                self.db.log_event(
                    agent.issue_id,
                    agent.agent_id,
                    "agent_complete_skipped",
                    {"reason": f"success result but issue is {current_status or 'missing'}, skipping merge enqueue"},
                )
                return True

        commit_hash = decision.result.git_commit or deps.get_commit_hash(agent.worktree)
        test_command = file_result.get("test_command") if file_result else None

        self.db.enqueue_merge(
            issue_id=agent.issue_id,
            agent_id=agent.agent_id,
            project=agent.project,
            worktree=agent.worktree,
            branch_name=f"agent/{agent.name}",
            test_command=test_command,
        )

        agent_row = self.db.get_agent(agent.agent_id)
        model = agent_row["model"] if agent_row else None

        self.db.log_event(
            agent.issue_id,
            agent.agent_id,
            "completed",
            {
                "summary": decision.result.summary,
                "commit": commit_hash,
                "artifacts": decision.result.artifacts,
                "model": model,
            },
        )
        return False

    async def handle_agent_complete(self, agent: AgentIdentity, file_result: dict[str, Any] | None = None):
        """Handle agent completion. If file_result is provided it is used directly, bypassing message parsing."""
        if not self._try_claim_agent_for_handling(agent, handler_name="completion handling"):
            return

        decision: CompletionDecision | None = None
        remove_worktree_on_teardown = False

        try:
            # Always clean up the result file if it exists
            deps.remove_result_file(agent.worktree)
            self._harvest_worker_notes(agent)

            decision = await self._decide_completion_transition(agent, file_result=file_result)

            match decision.transition:
                case CompletionTransition.SKIP:
                    remove_worktree_on_teardown = True
                    self.db.log_event(
                        agent.issue_id,
                        agent.agent_id,
                        "agent_complete_skipped",
                        {"reason": decision.skip_reason or "completion skipped"},
                    )

                case CompletionTransition.FAIL:
                    remove_worktree_on_teardown = True
                    await self._handle_completion_failure(agent, decision)

                case CompletionTransition.SUCCESS:
                    remove_worktree_on_teardown = await self._handle_completion_success(
                        agent,
                        decision,
                        file_result=file_result,
                    )

                case _:
                    raise RuntimeError(f"Unhandled completion transition: {decision.transition}")

        except Exception as e:
            transition = decision.transition.value if decision else "error_completion_handler"
            self.db.log_event(
                agent.issue_id,
                agent.agent_id,
                "completion_error",
                {"error": str(e), "transition": transition},
            )
            # If the merge was never enqueued, nothing else will clean up
            # the worktree — force removal on teardown.
            if not remove_worktree_on_teardown:
                if not self.db.has_pending_merge(agent.issue_id):
                    remove_worktree_on_teardown = True
        finally:
            await self._cleanup_agent(agent, remove_worktree=remove_worktree_on_teardown)

    def _choose_escalation(self, issue_id: str, *, include_anomaly: bool = True) -> EscalationDecision:
        """Decide retry/switch/escalate tier for an issue."""
        if include_anomaly and deps.Config.ANOMALY_FAILURE_THRESHOLD and deps.Config.ANOMALY_WINDOW_MINUTES:
            recent_failures = self.db.count_events_in_window_after_reset(issue_id, "incomplete", deps.Config.ANOMALY_WINDOW_MINUTES)
            if recent_failures >= deps.Config.ANOMALY_FAILURE_THRESHOLD:
                return EscalationDecision.ANOMALY_ESCALATE

        retry_count = self.db.count_events_by_type_since_reset(issue_id, "retry")
        if retry_count < deps.Config.MAX_RETRIES:
            return EscalationDecision.RETRY

        agent_switch_count = self.db.count_events_by_type_since_reset(issue_id, "agent_switch")
        if agent_switch_count < deps.Config.MAX_AGENT_SWITCHES:
            return EscalationDecision.AGENT_SWITCH

        return EscalationDecision.ESCALATE

    def _apply_failure_disposition(
        self,
        *,
        issue_id: str,
        agent: AgentIdentity,
        decision: EscalationDecision,
        reason: str,
        model: str | None,
    ) -> bool:
        """Apply retry/switch/escalate side effects for a failure decision."""
        if decision == EscalationDecision.ANOMALY_ESCALATE:
            recent_failures = self.db.count_events_in_window_after_reset(issue_id, "incomplete", deps.Config.ANOMALY_WINDOW_MINUTES)
            logger.warning(f"Anomaly: {recent_failures} failures on {issue_id} in {deps.Config.ANOMALY_WINDOW_MINUTES}m — auto-escalating")
            return self._try_escalate_issue(
                issue_id,
                agent.agent_id,
                to_status=IssueStatus.ESCALATED,
                event_type="escalated",
                detail={
                    "reason": "Anomaly detection: rapid repeated failures",
                    "recent_failures": recent_failures,
                    "window_minutes": deps.Config.ANOMALY_WINDOW_MINUTES,
                    "final_failure_reason": reason,
                },
                skip_event_type="anomaly_escalate_skipped",
            )

        if decision == EscalationDecision.RETRY:
            retry_count = self.db.count_events_by_type_since_reset(issue_id, "retry")
            if not self._try_escalate_issue(
                issue_id,
                agent.agent_id,
                to_status=IssueStatus.OPEN,
                event_type="retry",
                detail={"retry_count": retry_count + 1, "reason": reason, "previous_agent": agent.name},
                skip_event_type="retry_skipped",
                skip_reason="issue not releasable",
            ):
                return False
            logger.info(f"Retrying issue {issue_id} (attempt {retry_count + 1}/{deps.Config.MAX_RETRIES})")
            return True

        if decision == EscalationDecision.AGENT_SWITCH:
            agent_switch_count = self.db.count_events_by_type_since_reset(issue_id, "agent_switch")
            if not self._try_escalate_issue(
                issue_id,
                agent.agent_id,
                to_status=IssueStatus.OPEN,
                event_type="agent_switch",
                detail={"switch_count": agent_switch_count + 1, "reason": reason, "previous_agent": agent.name, "model": model},
                skip_event_type="agent_switch_skipped",
                skip_reason="issue not releasable",
            ):
                return False
            logger.info(f"Switching agent for issue {issue_id} (switch {agent_switch_count + 1}/{deps.Config.MAX_AGENT_SWITCHES})")
            return True

        retry_count = self.db.count_events_by_type_since_reset(issue_id, "retry")
        agent_switch_count = self.db.count_events_by_type_since_reset(issue_id, "agent_switch")
        if self._try_escalate_issue(
            issue_id,
            agent.agent_id,
            to_status=IssueStatus.ESCALATED,
            event_type="escalated",
            detail={
                "reason": "Exhausted all retry and agent switch attempts",
                "final_failure_reason": reason,
                "total_retries": retry_count,
                "total_agent_switches": agent_switch_count,
            },
            skip_event_type="escalate_skipped",
        ):
            logger.warning(
                f"Escalating issue {issue_id} to human intervention after {retry_count} retries and {agent_switch_count} agent switches"
            )
            return True
        return False

    async def _handle_agent_failure(self, agent: AgentIdentity, result: CompletionResult):
        """State machine for failure routing: retry -> agent switch -> escalate."""
        issue_id = agent.issue_id

        # Log the failure first so anomaly checks see this occurrence.
        agent_row = self.db.get_agent(agent.agent_id)
        model = agent_row["model"] if agent_row else None
        self.db.log_event(
            issue_id,
            agent.agent_id,
            "incomplete",
            {"reason": result.reason, "summary": result.summary, "model": model},
        )

        decision = self._choose_escalation(issue_id)
        self._apply_failure_disposition(
            issue_id=issue_id,
            agent=agent,
            decision=decision,
            reason=result.reason,
            model=model,
        )
