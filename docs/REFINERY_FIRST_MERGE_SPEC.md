# Refinery-First Merge Queue Spec

_Status: proposed_

_Last updated: 2026-02-17_

## 1. Proposal in One Line

Replace the current two-tier merge processor (mechanical fast-path, refinery fallback) with a single refinery-first path where every queued merge is reviewed and integrated by the Refinery LLM before finalization.

## 2. Why This Change

Today, many branches are auto-merged without LLM review when they pass rebase/test gates mechanically. That optimizes throughput, but it means semantic integration review is inconsistent: only "hard" merges get refinery attention.

This proposal makes merge review policy uniform:
- every `done` issue entering `merge_queue` gets refinery review,
- refinery explicitly accepts/rejects/escalates,
- only accepted branches are ff-merged and finalized.

## 3. Current vs Proposed Behavior

### Current (implemented)

Flow in `src/hive/merge.py`:
1. `merge_queue` entry (`queued -> running`)
2. Tier 1 mechanical path (`_try_mechanical_merge`):
   - `git rebase main`
   - test gate (`test_command` and/or `HIVE_TEST_COMMAND`)
   - `git merge --ff-only` into main
3. If Tier 1 fails, Tier 2 refinery path (`_send_to_refinery`)
4. Finalization and teardown

Effect: branches that clear deterministic gates can be merged with no LLM review.

### Proposed (refinery-first)

Flow for every queued entry:
1. `merge_queue` entry (`queued -> running`)
2. Send directly to refinery review/integration
3. Refinery writes `.hive-result.jsonl` with:
   - `merged` -> orchestrator ff-merges + finalizes
   - `rejected` -> queue failed, issue reopened
   - `needs_human` -> queue failed, issue escalated
4. Teardown unchanged on successful finalization

Mechanical logic becomes optional preflight only (existence/sanity checks), not merge authority.

## 4. State Machine and Side-Effect Impact

### 4.1 Issue statuses

No new issue statuses are required.

Unchanged high-level transitions:
- `in_progress -> done` in orchestrator completion handler
- `done -> finalized` on merge success
- `done -> open` on merge rejection
- `done -> escalated` on ambiguous/hard failure

What changes is who decides `done -> {finalized|open|escalated}`:
- today: deterministic mechanical gate first, refinery second
- proposed: refinery always

### 4.2 Merge queue statuses

No schema change is required. Keep:
- `queued`, `running`, `merged`, `failed`

Interpretation shift:
- `running` effectively means "under refinery review" rather than "mechanical attempt in progress, maybe refinery later".

### 4.3 Transition side effects

Side effects that remain:
- finalization event logging
- branch/worktree/session teardown on merged
- rejection notes persisted
- refinery session lifecycle/cycling logic

Side effects removed or reduced:
- direct mechanical merge events (`rebase_success`, `test_failure` from Tier 1) as merge-decision signals
- deterministic merge path as a separate execution branch

New side effects recommended:
- standardized review events for every merge:
  - `refinery_review_started`
  - `refinery_review_passed`
  - `refinery_review_rejected`
  - `refinery_review_escalated`

This yields cleaner analytics because all merge decisions come from one path.

### 4.4 Transition matrix (current vs refinery-first)

| Scenario | Current transition owner | Proposed transition owner | Status outcome |
|---|---|---|---|
| Clean rebase + tests pass | Mechanical path | Refinery | `done -> finalized` |
| Rebase conflict | Refinery fallback | Refinery | `done -> finalized|open|escalated` |
| Tests fail after rebase | Refinery fallback | Refinery | `done -> finalized|open|escalated` |
| Ambiguous integration | Refinery fallback | Refinery | `done -> escalated` |
| Explicit rejection | Refinery fallback | Refinery | `done -> open` |

Behavioral delta:
- today, trivial merges can bypass LLM review entirely;
- proposed, every `done` item gets the same review gate before finalization.

## 5. Implementation Delta

### 5.1 `src/hive/merge.py`

Primary changes:
1. `process_queue_once()` dispatches all entries to refinery directly.
2. `_try_mechanical_merge()` is removed or reduced to non-authoritative preflight checks.
3. `_send_to_refinery()` becomes the single decision engine.

Recommended structure:
- `process_queue_once`
  - mark `running`
  - validate entry/worktree/branch preconditions
  - call `_send_to_refinery(entry, mode="full_review")`
- `_send_to_refinery`
  - prompt refinery for integration + review + test verification
  - parse result and apply state transition
- `_finalize_issue` unchanged

Complexity effect:
- lower branching complexity in merge processor,
- higher dependence on prompt contract and LLM determinism.

### 5.2 `src/hive/prompts.py` and `src/hive/prompts/refinery.md`

`build_refinery_prompt()` currently frames refinery mainly as fallback for conflict/test failures. It should support first-pass review for all merges.

Required prompt changes:
- remove assumption that an upstream mechanical attempt happened,
- define mandatory review checklist for every merge:
  - rebase cleanliness,
  - test execution,
  - diff/test coverage sanity,
  - explicit accept/reject rationale.

Recommended result schema extension (backward-compatible):
- `review_summary`
- `risk_level` (`low|medium|high`)
- `tests_run` list
- `warnings`

### 5.3 Orchestrator / DB / CLI

- `src/hive/orchestrator.py`: no state machine rewrite needed for `in_progress -> done`; merge enqueue remains unchanged.
- `src/hive/db.py`: no required migration if keeping existing queue statuses.
- `src/hive/cli.py`:
  - `hive review` remains useful for manual QA and audit,
  - `hive finalize` path remains fallback/manual override for merge-disabled/manual modes.

### 5.4 Config surface

Current flags are overloaded (`merge_queue_enabled` mixes auto-processing and policy semantics). Introduce explicit merge policy:

- `HIVE_MERGE_POLICY=mechanical_then_refinery|refinery_first|manual`

Mapping:
- `mechanical_then_refinery`: current behavior
- `refinery_first`: proposed behavior
- `manual`: no background processing; human-driven finalize

`HIVE_MERGE_QUEUE_ENABLED` can remain as deprecated alias for compatibility.

## 6. Simpler or More Complex?

Net effect is mixed.

Simpler:
- one merge decision path instead of two,
- easier observability and fewer edge-case branches,
- less duplicated logic around tests/rejections across mechanical vs refinery paths.

More complex:
- system correctness now depends more on prompt quality and refinery behavior,
- latency/cost variability increases,
- higher need for robust refinery health/retry/error instrumentation.

Practical conclusion:
- code complexity goes down,
- operational complexity (cost, latency, model drift) goes up.

## 7. Expected Merge Quality Impact

Most likely outcome is "quality up, throughput down" if implemented with strict guardrails.

Potential quality improvements:
- consistent semantic review for every merge, not just failing mechanical cases,
- better detection of risky diffs that pass tests but violate intent/conventions,
- richer rejection notes for rework.

Potential quality regressions:
- false rejects from model over-conservatism,
- inconsistent decisions across similar changes,
- occasional model mistakes on trivial merges that deterministic path handled reliably.

Expected net:
- integration correctness likely improves,
- deterministic reliability and lead time likely worsen,
- cost per merged issue definitely increases.

### 7.1 When quality gets better vs worse

More likely better when:
- test suite is reliable and refinery is required to run it,
- prompts enforce evidence-based decisions (commands run, failures observed),
- rejection reasons are concrete and tied to files/tests.

More likely worse when:
- tests are weak/flaky and refinery substitutes judgment for evidence,
- prompt allows style-based subjective rejection without objective failures,
- refinery model/version changes without acceptance-rate monitoring.

Non-negotiable guardrails for `refinery_first`:
- acceptance requires explicit proof of verification (command + outcome),
- rejection requires actionable reason and rework direction,
- timeout or missing result file must produce deterministic `needs_human` (never silent merge),
- policy kill switch must be operable at runtime.

## 8. Rollout Strategy (Strongly Recommended)

Do not flip globally in one step.

1. Shadow mode
- Keep current merge authority.
- Also run refinery review in parallel for sampled merges.
- Record "would_accept/would_reject" without enforcing.

2. Canary policy
- Enable `refinery_first` for a subset of projects/issue tags.
- Compare rejection/escalation and post-merge failure rates.

3. Full rollout with kill switch
- Make `refinery_first` default only after metrics stabilize.
- Keep immediate rollback to `mechanical_then_refinery`.

## 9. Metrics to Judge Success

Track before/after and by merge policy:
- merge lead time (`done -> finalized`)
- merge queue failure rate (`failed / total`)
- rejection reopen rate (`done -> open` after merge processing)
- escalation rate (`done -> escalated`)
- post-finalization incident proxy (hotfix/reopen within 24-48h)
- refinery token cost per finalized issue

Success should be defined as:
- lower post-merge defect signal,
- acceptable lead-time increase,
- rejection/escalation rates not exploding,
- predictable cost envelope.

## 10. Test Plan Changes

Update/add tests around:
- merge queue always dispatching to refinery in `refinery_first`
- no mechanical-authoritative merge in `refinery_first`
- result-state transitions (`merged/rejected/needs_human`)
- fallback behavior when refinery is unavailable
- policy switch behavior (`mechanical_then_refinery`, `refinery_first`, `manual`)

Likely touchpoints:
- `tests/test_merge.py`
- `tests/test_orchestrator.py`
- `tests/test_cli.py`

## 11. Risks and Mitigations

Risk: refinery outage blocks all merges.
Mitigation: policy fallback switch + health-based auto-downgrade to mechanical/manual.

Risk: queue latency spikes.
Mitigation: tighter SLAs, queue alerts, optional secondary refinery worker pool (future).

Risk: project-root `main` worktree has local tracked changes, causing ff-merge failures.
Mitigation: preflight dirty-worktree guard that pauses merge queue and emits explicit system events until clean.

Risk: model quality drift changes acceptance behavior.
Mitigation: pinned model version, prompt versioning, periodic acceptance-rate audits.

Risk: over-rejection creates churn.
Mitigation: enforce rejection rubric with concrete, actionable reasons and bounded retry policy.

## 12. Recommendation

Proceed, but as a policy-mode addition first (`refinery_first`), not a hard replacement on day one.

Reason:
- this is a strategic product decision (quality-first merge governance),
- it is technically straightforward to implement,
- it has real cost/latency and operational-risk tradeoffs that should be measured in production before defaulting globally.
