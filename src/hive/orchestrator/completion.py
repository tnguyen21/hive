"""Completion handling mixin for the Hive orchestrator."""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

from ..utils import AgentIdentity, CompletionResult

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


class CompletionTransition(str, Enum):
    """Transition outcomes for completion handling."""

    SKIP_TERMINAL_ISSUE = "skip_terminal_issue"
    FAIL_BUDGET = "fail_budget"
    FAIL_ASSESSMENT = "fail_assessment"
    FAIL_VALIDATION_NO_DIFF = "fail_validation_no_diff"
    SUCCESS_DONE = "success_done"
    ERROR_COMPLETION_HANDLER = "error_completion_handler"


class EscalationDecision(str, Enum):
    """Escalation routing decision after a failure."""

    RETRY = "retry"
    AGENT_SWITCH = "agent_switch"
    ESCALATE = "escalate"
    ANOMALY_ESCALATE = "anomaly_escalate"


@dataclass
class CompletionDecision:
    """Decision payload from completion transition analysis."""

    transition: CompletionTransition
    result: Optional[CompletionResult] = None
    terminal_status: Optional[str] = None
    budget_tokens: Optional[int] = None
    validation_original_summary: Optional[str] = None


class CompletionMixin:
    """Mixin providing agent completion and failure handling."""

    async def _decide_completion_transition(
        self,
        agent: AgentIdentity,
        file_result: Optional[Dict[str, Any]] = None,
    ) -> CompletionDecision:
        """Decision phase for completion handling.

        Determines the next completion transition and any payload required by
        transition side effects.
        """
        import hive.orchestrator as _mod

        Config = _mod.Config
        assess_completion = _mod.assess_completion
        has_diff_from_main_async = _mod.has_diff_from_main_async

        terminal_issue = self.db.get_issue(agent.issue_id)
        if terminal_issue and terminal_issue.get("status") in ("canceled", "finalized"):
            return CompletionDecision(
                transition=CompletionTransition.SKIP_TERMINAL_ISSUE,
                terminal_status=terminal_issue["status"],
            )

        messages = await self.backend.get_messages(agent.session_id, directory=agent.worktree)
        self._log_token_usage(agent, messages)

        if Config.MAX_TOKENS_PER_ISSUE:
            budget_tokens = self.db.get_issue_token_total(agent.issue_id)
            if budget_tokens > Config.MAX_TOKENS_PER_ISSUE:
                logger.warning(f"Issue {agent.issue_id} exceeded token budget ({budget_tokens} > {Config.MAX_TOKENS_PER_ISSUE})")
                return CompletionDecision(
                    transition=CompletionTransition.FAIL_BUDGET,
                    budget_tokens=budget_tokens,
                    result=CompletionResult(
                        success=False,
                        reason=f"Exceeded per-issue token budget ({budget_tokens} > {Config.MAX_TOKENS_PER_ISSUE})",
                        summary=f"Terminated: per-issue token budget exceeded ({budget_tokens} tokens)",
                    ),
                )

        result = assess_completion(file_result=file_result)
        if not result.success:
            return CompletionDecision(
                transition=CompletionTransition.FAIL_ASSESSMENT,
                result=result,
            )

        has_commits = await has_diff_from_main_async(agent.worktree)
        if not has_commits:
            return CompletionDecision(
                transition=CompletionTransition.FAIL_VALIDATION_NO_DIFF,
                validation_original_summary=result.summary,
                result=CompletionResult(
                    success=False,
                    reason="No commits relative to main despite claiming success",
                    summary=result.summary,
                ),
            )

        return CompletionDecision(
            transition=CompletionTransition.SUCCESS_DONE,
            result=result,
        )

    # Completion transition table:
    # - SKIP_TERMINAL_ISSUE      -> log agent_complete_skipped
    # - FAIL_BUDGET              -> log budget_exceeded + _handle_agent_failure
    # - FAIL_ASSESSMENT          -> _handle_agent_failure
    # - FAIL_VALIDATION_NO_DIFF  -> log validation_failed + _handle_agent_failure
    # - SUCCESS_DONE             -> update done + enqueue merge + log completed
    # - ERROR_COMPLETION_HANDLER -> log completion_error
    def _log_completion_skip(self, agent: AgentIdentity, reason: str):
        """Log a completion-path skip reason."""
        self.db.log_event(
            agent.issue_id,
            agent.agent_id,
            "agent_complete_skipped",
            {"reason": reason},
        )

    def _harvest_worker_notes(self, agent: AgentIdentity):
        """Best-effort note harvesting from the worker worktree."""
        import hive.orchestrator as _mod

        read_notes_file = _mod.read_notes_file
        remove_notes_file = _mod.remove_notes_file

        # Harvest notes before terminal checks so canceled/failed workers'
        # discoveries are still preserved.
        try:
            notes_data = read_notes_file(agent.worktree)
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
            remove_notes_file(agent.worktree)

    async def _handle_completion_failure(self, agent: AgentIdentity, decision: CompletionDecision):
        """Apply completion failure side effects and route through failure handling."""
        import hive.orchestrator as _mod

        if decision.transition == CompletionTransition.FAIL_BUDGET:
            self.db.log_event(
                agent.issue_id,
                agent.agent_id,
                "budget_exceeded",
                {"issue_tokens": decision.budget_tokens, "limit": _mod.Config.MAX_TOKENS_PER_ISSUE},
            )
        elif decision.transition == CompletionTransition.FAIL_VALIDATION_NO_DIFF:
            self.db.log_event(
                agent.issue_id,
                agent.agent_id,
                "validation_failed",
                {
                    "reason": "No commits relative to main despite claiming success",
                    "original_summary": decision.validation_original_summary,
                },
            )

        if decision.result is not None:
            await self._handle_agent_failure(agent, decision.result)

    async def _handle_completion_success(
        self,
        agent: AgentIdentity,
        decision: CompletionDecision,
        file_result: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Apply success side effects. Returns True when teardown should remove the worktree."""
        import hive.orchestrator as _mod

        get_commit_hash = _mod.get_commit_hash

        if decision.result is None:
            raise RuntimeError("Missing completion result for success transition")

        transitioned = self.db.try_transition_issue_status(
            agent.issue_id,
            from_status="in_progress",
            to_status="done",
            expected_assignee=agent.agent_id,
        )
        if not transitioned:
            current_issue = self.db.get_issue(agent.issue_id)
            current_status = current_issue.get("status") if current_issue else None
            if current_status != "done":
                self._log_completion_skip(
                    agent,
                    f"success result but issue is {current_status or 'missing'}, skipping merge enqueue",
                )
                return True

        commit_hash = decision.result.git_commit or get_commit_hash(agent.worktree)
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

    async def handle_agent_complete(
        self,
        agent: AgentIdentity,
        file_result: Optional[Dict[str, Any]] = None,
    ):
        """
        Handle agent completion.

        Args:
            agent: Agent identity
            file_result: Optional parsed result from .hive-result.jsonl file.
                If provided, used directly for completion assessment (skips
                message parsing heuristics).
        """
        import hive.orchestrator as _mod

        remove_result_file = _mod.remove_result_file

        if not self._try_claim_agent_for_handling(agent, handler_name="completion handling"):
            return

        decision: Optional[CompletionDecision] = None
        remove_worktree_on_teardown = False

        try:
            # Always clean up the result file if it exists
            remove_result_file(agent.worktree)
            self._harvest_worker_notes(agent)

            decision = await self._decide_completion_transition(agent, file_result=file_result)

            match decision.transition:
                case CompletionTransition.SKIP_TERMINAL_ISSUE:
                    status = decision.terminal_status or "unknown"
                    remove_worktree_on_teardown = True
                    self._log_completion_skip(agent, f"issue already {status}, cleaning up session")

                case CompletionTransition.FAIL_BUDGET:
                    remove_worktree_on_teardown = True
                    await self._handle_completion_failure(agent, decision)

                case CompletionTransition.FAIL_VALIDATION_NO_DIFF:
                    remove_worktree_on_teardown = True
                    await self._handle_completion_failure(agent, decision)

                case CompletionTransition.FAIL_ASSESSMENT:
                    remove_worktree_on_teardown = True
                    await self._handle_completion_failure(agent, decision)

                case CompletionTransition.SUCCESS_DONE:
                    remove_worktree_on_teardown = await self._handle_completion_success(
                        agent,
                        decision,
                        file_result=file_result,
                    )

                case _:
                    raise RuntimeError(f"Unhandled completion transition: {decision.transition}")

        except Exception as e:
            transition = decision.transition.value if decision else CompletionTransition.ERROR_COMPLETION_HANDLER.value
            self.db.log_event(
                agent.issue_id,
                agent.agent_id,
                "completion_error",
                {"error": str(e), "transition": transition},
            )
        finally:
            await self._teardown_agent(agent, remove_worktree=remove_worktree_on_teardown)

    def _choose_escalation(self, issue_id: str, *, include_anomaly: bool = True) -> EscalationDecision:
        """Decide retry/switch/escalate tier for an issue."""
        import hive.orchestrator as _mod

        Config = _mod.Config

        if include_anomaly and Config.ANOMALY_FAILURE_THRESHOLD and Config.ANOMALY_WINDOW_MINUTES:
            recent_failures = self.db.count_events_since_minutes_since_reset(issue_id, "incomplete", Config.ANOMALY_WINDOW_MINUTES)
            if recent_failures >= Config.ANOMALY_FAILURE_THRESHOLD:
                return EscalationDecision.ANOMALY_ESCALATE

        retry_count = self.db.count_events_by_type_since_reset(issue_id, "retry")
        if retry_count < Config.MAX_RETRIES:
            return EscalationDecision.RETRY

        agent_switch_count = self.db.count_events_by_type_since_reset(issue_id, "agent_switch")
        if agent_switch_count < Config.MAX_AGENT_SWITCHES:
            return EscalationDecision.AGENT_SWITCH

        return EscalationDecision.ESCALATE

    def _apply_failure_disposition(
        self,
        *,
        issue_id: str,
        agent: AgentIdentity,
        decision: EscalationDecision,
        reason: str,
        model: Optional[str],
    ) -> bool:
        """Apply retry/switch/escalate side effects for a failure decision."""
        import hive.orchestrator as _mod

        Config = _mod.Config

        if decision == EscalationDecision.ANOMALY_ESCALATE:
            recent_failures = self.db.count_events_since_minutes_since_reset(issue_id, "incomplete", Config.ANOMALY_WINDOW_MINUTES)
            logger.warning(f"Anomaly: {recent_failures} failures on {issue_id} in {Config.ANOMALY_WINDOW_MINUTES}m — auto-escalating")
            return self._try_escalate_issue(
                issue_id,
                agent.agent_id,
                to_status="escalated",
                event_type="escalated",
                detail={
                    "reason": "Anomaly detection: rapid repeated failures",
                    "recent_failures": recent_failures,
                    "window_minutes": Config.ANOMALY_WINDOW_MINUTES,
                    "final_failure_reason": reason,
                },
                skip_event_type="anomaly_escalate_skipped",
            )

        if decision == EscalationDecision.RETRY:
            retry_count = self.db.count_events_by_type_since_reset(issue_id, "retry")
            if not self._try_escalate_issue(
                issue_id,
                agent.agent_id,
                to_status="open",
                event_type="retry",
                detail={"retry_count": retry_count + 1, "reason": reason, "previous_agent": agent.name},
                skip_event_type="retry_skipped",
                skip_reason="issue not releasable",
            ):
                return False
            logger.info(f"Retrying issue {issue_id} (attempt {retry_count + 1}/{Config.MAX_RETRIES})")
            return True

        if decision == EscalationDecision.AGENT_SWITCH:
            agent_switch_count = self.db.count_events_by_type_since_reset(issue_id, "agent_switch")
            if not self._try_escalate_issue(
                issue_id,
                agent.agent_id,
                to_status="open",
                event_type="agent_switch",
                detail={"switch_count": agent_switch_count + 1, "reason": reason, "previous_agent": agent.name, "model": model},
                skip_event_type="agent_switch_skipped",
                skip_reason="issue not releasable",
            ):
                return False
            logger.info(f"Switching agent for issue {issue_id} (switch {agent_switch_count + 1}/{Config.MAX_AGENT_SWITCHES})")
            return True

        retry_count = self.db.count_events_by_type_since_reset(issue_id, "retry")
        agent_switch_count = self.db.count_events_by_type_since_reset(issue_id, "agent_switch")
        if self._try_escalate_issue(
            issue_id,
            agent.agent_id,
            to_status="escalated",
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
