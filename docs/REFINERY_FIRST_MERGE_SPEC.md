# Refinery-Only Merge Queue Spec

_Status: accepted_

_Last updated: 2026-02-18_

## 1. Proposal in One Line

Delete the mechanical merge path entirely. The refinery is the only way code gets merged. If the refinery is down, merges stop.

## 2. Why This Change

The two-tier merge system (mechanical fast-path + refinery fallback) is the single largest source of complexity and bugs in the merge processor.

The mechanical path exists to optimize throughput for "easy" merges. In practice:

- it creates two entirely separate code paths that must both be correct,
- it introduces git operations (checkout, index.lock, rebase) that interact badly with macOS sandbox provenance,
- it makes merge behavior inconsistent — some branches get LLM review, some don't,
- when it fails, the fallback to refinery adds latency anyway.

The refinery already handles the hard cases correctly. Make it handle all cases. Delete the rest.

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

### Proposed (refinery-only)

Flow for every queued entry:

1. `merge_queue` entry (`queued -> running`)
2. Send to refinery for review/integration
3. Refinery writes `.hive-result.jsonl` with:
   - `merged` -> orchestrator ff-merges + finalizes
   - `rejected` -> queue failed, issue reopened
   - `needs_human` -> queue failed, issue escalated
4. Teardown unchanged on successful finalization

There is no mechanical path. There is no preflight. The refinery owns the entire merge decision, including rebase, test execution, and ff-merge. `_try_mechanical_merge()` is deleted.

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

`running` now means exactly one thing: under refinery review.

### 4.3 Transition side effects

Side effects that remain:

- finalization event logging
- branch/worktree/session teardown on merged
- rejection notes persisted
- refinery session lifecycle/cycling logic

Side effects deleted:

- all mechanical merge events (`rebase_success`, `test_failure`, `mechanical_merge_success`, etc.)
- the entire `_try_mechanical_merge` code path and its associated error handling

New side effects:

- standardized review events for every merge:
  - `refinery_review_started`
  - `refinery_review_passed`
  - `refinery_review_rejected`
  - `refinery_review_escalated`

All merge decisions come from one path. No exceptions.

### 4.4 Transition matrix

| Scenario              | Transition owner | Status outcome                       |
| --------------------- | ---------------- | ------------------------------------ |
| Clean merge           | Refinery         | `done -> finalized`                  |
| Rebase conflict       | Refinery         | `done -> finalized\|open\|escalated` |
| Tests fail            | Refinery         | `done -> open`                       |
| Ambiguous integration | Refinery         | `done -> escalated`                  |
| Explicit rejection    | Refinery         | `done -> open`                       |
| Refinery unavailable  | Nobody           | Queue stalls, operator alerted       |

Every merge goes through the same path. There is no bypass.

## 5. Implementation Delta

### 5.1 `src/hive/merge.py`

Changes:

1. Delete `_try_mechanical_merge()` entirely.
2. Delete all git checkout/rebase/index.lock operations from the merge processor.
3. `process_queue_once()` dispatches every entry to `_send_to_refinery()`.
4. `_send_to_refinery()` is the only merge path.

Resulting structure:

- `process_queue_once`
  - mark `running`
  - call `_send_to_refinery(entry)`
- `_send_to_refinery`
  - prompt refinery for rebase + review + test + ff-merge
  - parse `.hive-result.jsonl` and apply state transition
- `_finalize_issue` unchanged

The refinery session operates inside the worker's worktree. It has full shell access. It does the rebase, runs the tests, writes the result file. The orchestrator just reads the verdict.

### 5.2 `src/hive/prompts.py` and `src/hive/prompts/refinery.md`

`build_refinery_prompt()` currently frames refinery mainly as fallback for conflict/test failures. It should support first-pass review for all merges.

Required prompt changes:

- remove assumption that an upstream mechanical attempt happened,
- define mandatory review checklist for every merge:
  - rebase cleanliness,
  - test execution,
  - diff/test coverage sanity,
  - explicit accept/reject rationale.

Result file schema is unchanged: verdict (`merged`/`rejected`/`needs_human`) plus reason string. Audit detail lives in the refinery session logs, not in structured result fields.

### 5.3 Orchestrator / DB / CLI

- `src/hive/orchestrator.py`: no changes. Merge enqueue remains unchanged.
- `src/hive/db.py`: no migration needed.
- `src/hive/cli.py`:
  - `hive review` remains for manual QA/audit,
  - `hive finalize` remains as manual override when merge queue is disabled.

### 5.4 Config surface

`HIVE_MERGE_QUEUE_ENABLED` remains the only toggle:

- `true` (default): refinery processes the queue automatically
- `false`: no background processing; human-driven finalize via `hive finalize`

## 6. Complexity

This change is a strict simplification.

Deleted:

- `_try_mechanical_merge()` and all its git checkout/rebase/index.lock operations
- the two-tier dispatch logic and tier-selection heuristics
- mechanical merge event types and their handling
- the fallback path from mechanical to refinery
- policy configuration surface (`HIVE_MERGE_POLICY`)

What remains:

- one merge path: refinery
- one toggle: enabled or not

The tradeoff is real — refinery outage means no merges — but that's an acceptable failure mode. A stopped merge queue is visible and recoverable. Silent bad merges from a buggy mechanical path are not.

## 7. Quality Guardrails

The refinery prompt must enforce:

- acceptance requires explicit proof of verification (tests run, output captured),
- rejection requires actionable reason and rework direction,
- timeout or missing result file produces `needs_human` (never silent merge, never silent drop).

The refinery is not a style police. It verifies:

1. rebase is clean,
2. tests pass,
3. diff matches the issue intent,
4. no obvious integration conflicts with recent main changes.

If all four pass, it merges. Subjective "code quality" rejections are out of scope for the refinery — that's what human review (`hive review`) is for.

## 8. Rollout

One step. Delete the mechanical path, ship the refinery-only path.

No shadow mode. No canary. No policy toggle. The mechanical path is the source of the bugs we're fixing. Keeping it around "just in case" means keeping the code that causes the problems.

If the refinery is broken, `HIVE_MERGE_QUEUE_ENABLED=false` stops the queue and the operator uses `hive finalize` manually. That's the escape hatch.

## 9. Metrics to Judge Success

Track before/after:

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

Delete:

- all tests for `_try_mechanical_merge`
- all tests for tier-selection / fallback logic
- all tests for `HIVE_MERGE_POLICY` config

Add/update:

- every queue entry dispatches to refinery
- result-state transitions (`merged/rejected/needs_human`)
- refinery unavailable → queue stalls (no fallback, no silent failure)
- `HIVE_MERGE_QUEUE_ENABLED=false` → queue does not process

Touchpoints:

- `tests/test_merge.py`
- `tests/test_orchestrator.py`

## 11. Risks

Risk: refinery outage blocks all merges.
This is fine. A stalled queue is visible. The operator disables the queue or fixes the refinery. There is no auto-downgrade to a mechanical path that has its own bugs.

Risk: queue latency increases.
Yes. Every merge now involves an LLM call. This is the cost of correctness.

Risk: dirty main worktree causes ff-merge failures.
Mitigation: preflight dirty-worktree guard that pauses queue and emits events until clean. (This already exists.)

Risk: model drift changes acceptance behavior.
Mitigation: pinned model version, prompt versioning, acceptance-rate monitoring.

## 12. Decision

Delete the mechanical merge path. Ship refinery-only.

The mechanical path was an optimization that became the primary source of merge bugs. The two-tier system doubled the surface area for git operation failures (EPERM, index.lock, provenance, worktree races) while providing inconsistent review coverage. Removing it is both simpler and more correct.
