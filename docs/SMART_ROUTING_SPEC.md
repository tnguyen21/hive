# Smart Routing Spec (Haiku vs Sonnet)

_Proposed design (not implemented)._

**Status**: Draft  
**Date**: 2026-02-17  
**Owner**: Hive

---

## 1. Problem Statement

Hive currently picks worker model via:

1. `issue.model` override
2. `HIVE_WORKER_MODEL`
3. `HIVE_DEFAULT_MODEL`

This is static. It does not automatically route new issues to the model that has historically performed best for similar work (for example by issue `type` + `tags`), and "agent switch" does not explicitly switch to a different model.

We want data-driven routing so incoming requests can be delegated more efficiently (for example, route `bug+python+small` to Haiku if it has >=95% success there with enough samples).

---

## 2. Goals

- Add automatic model selection for worker dispatch based on historical performance by issue class.
- Support routing classes from issue metadata:
  - `type` (bug, feature, task, step, etc.)
  - `tags` (bugfix, python, small, etc.)
- Include merge quality signals in scoring:
  - clean merge path
  - test pass/fail at merge gate
  - rebase conflict rate
- Support model switching on retry/agent-switch paths.
- Preserve manual override (`issue.model`) as highest priority.
- Make decisions observable in events and CLI metrics.

## 3. Non-Goals (MVP)

- No online learning service or external model registry.
- No probabilistic/ML model training; SQL + deterministic policy only.
- No changes to worker prompt protocol.
- No cross-project global routing by default.

---

## 4. Current State Snapshot

### Existing data available

- Model chosen during dispatch is logged in `worker_started.detail.model`.
- Outcome and quality events already exist:
  - `completed`, `incomplete`, `escalated`
  - `tests_passed`, `test_failure`, `rebase_conflict`, `merged`, `merge_rejected`
- Issue classification already exists:
  - `issues.type`
  - `issues.tags` (JSON array)

### Existing gaps

- `agent_switch` currently requeues work but does not choose an alternate model.
- Some analytics derive model from `issues.model`, which can be wrong for per-run attribution when retries/switches use different models.
- No routing policy or configuration exists for automatic selection.

---

## 5. Proposed Design

## 5.1 Routing Modes

- `manual`:
  - If `issue.model` is set, use it directly.
- `smart`:
  - If no `issue.model`, select from candidate models using historical performance for the issue class.
- `fallback`:
  - If insufficient/uncertain data, use `HIVE_WORKER_MODEL` then `HIVE_DEFAULT_MODEL`.

## 5.2 Route Class

Route class key (MVP):

- `project`
- `issue_type`
- `tag_set` (sorted tags, or subset features for scoring)

Example:

- `project=acme`, `issue_type=bug`, `tags=[bugfix,python,small]`

## 5.3 Candidate Models

Config-driven candidate list (example):

- `claude-haiku-*`
- `claude-sonnet-*`

Router evaluates each candidate and picks the highest-scoring model that meets safety gates.

## 5.4 Scoring and Gates (MVP)

For each candidate model in the matching class/window:

- `run_success_rate` = completed runs / total runs
- `merge_clean_rate` = tests_passed / (tests_passed + test_failure + rebase_conflict)
- `escalation_rate` = escalated runs / total runs
- `sample_size` = total runs

Recommended MVP decision rule:

1. Filter candidates with `sample_size >= min_samples`.
2. Filter candidates with `run_success_rate >= success_threshold` (example 95%).
3. Among remaining, choose highest `merge_clean_rate`, then highest `run_success_rate`, then highest `sample_size`.
4. If none remain, fallback to default model.

Optional tie-breaker:

- Lower median duration.

## 5.5 Retry and Agent Switch Behavior

When failure path enters `agent_switch` tier:

- Recompute routing with a penalty against previously failed model(s) on that issue.
- Prefer a different model if available and above minimum confidence.
- Log explicit switch reason and `model_from -> model_to`.

---

## 6. Data and Query Changes

## 6.1 Attribution Source of Truth

Per-run model attribution must come from `worker_started.detail.model` (not only `issues.model`).

Fallback order for attribution:

1. `json_extract(worker_started.detail, '$.model')`
2. `agents.model` at run start (if available)
3. `issues.model`
4. `unknown`

## 6.2 New DB API (proposed)

Add a DB method for routing stats (name illustrative):

- `get_routing_performance(project, issue_type, tags, lookback_days, models)`

Returns per-model:

- `runs`
- `run_success_rate`
- `merge_clean_rate`
- `escalation_rate`
- `avg_duration_s`
- optional confidence fields (for future)

Implementation can be SQL over existing `events` + `issues` without schema migration in MVP.

## 6.3 Event Logging Additions

Log routing decisions in `worker_started.detail`:

- `routing_mode`: `manual|smart|fallback`
- `routing_reason`: short string
- `routing_features`: type/tags used
- `routing_sample_size`
- `routing_metrics`: summarized metrics used at decision time

On switch:

- include `previous_model`, `new_model`, `switch_policy_reason`.

---

## 7. Config Additions (proposed)

- `HIVE_SMART_ROUTING_ENABLED` (bool, default `false`)
- `HIVE_ROUTING_CANDIDATE_MODELS` (CSV/list, default empty -> no smart routing)
- `HIVE_ROUTING_MIN_SAMPLES` (int, default `20`)
- `HIVE_ROUTING_SUCCESS_THRESHOLD` (float percent, default `95.0`)
- `HIVE_ROUTING_LOOKBACK_DAYS` (int, default `90`)
- `HIVE_ROUTING_EXPLORATION_RATE` (float 0-1, default `0.0` in MVP)
- `HIVE_ROUTING_PREFER_DIFFERENT_MODEL_ON_SWITCH` (bool, default `true`)

---

## 8. Orchestrator Integration Points

Apply routing selection at:

1. `spawn_worker` model resolution
2. `agent_switch` path before issue is requeued or immediately before next claim

Manual `issue.model` always bypasses smart routing.

---

## 9. CLI / Observability (proposed)

MVP can ship with existing `hive metrics` plus improved run attribution.

Optional additions:

- `hive metrics --route --type bug --tags bugfix,python`
- `hive route explain <issue_id>`
  - prints candidate models, sample sizes, rates, and selected model

---

## 10. Rollout Plan

## Phase 0: Instrumentation hardening

- Ensure per-run model attribution is correct in metrics.
- Add routing decision fields to events even when smart routing disabled.

## Phase 1: Shadow mode

- Compute smart decision but do not enforce.
- Log `would_route_to` and compare against actual outcomes.

## Phase 2: Enforced smart routing

- Enable with config flag.
- Keep kill switch (`HIVE_SMART_ROUTING_ENABLED=false`).

## Phase 3: Retry switch optimization

- Enable model-change preference on `agent_switch`.
- Track improvement in merge/test outcomes.

---

## 11. Test Plan

Unit tests:

- Routing stats query correctness by type/tags/model.
- Correct attribution from `worker_started.detail.model`.
- Threshold/min-sample gating behavior.
- Fallback behavior when data missing.
- Agent switch selects alternate model when policy allows.

Integration tests:

- End-to-end dispatch logs routing metadata.
- Retry path changes model when switch tier is reached.
- Metrics reflect changed model split accurately.

Regression tests:

- Manual per-issue `model` override still wins.
- Existing queue/claim/escalation semantics unchanged.

---

## 12. Risks and Mitigations

- Sparse data for many tag combinations:
  - Mitigate with fallback hierarchy (exact class -> type only -> global).
- Tag quality variance:
  - Encourage strict tagging discipline in issue creation.
- Route thrashing from small sample noise:
  - Use minimum sample gates and optional lookback windows.
- Attribution drift:
  - Standardize on run-time event attribution for model identity.

---

## 13. Open Questions

1. Should tags remain from a fixed allowlist, or be opened for arbitrary team taxonomy?
2. Should route classes require exact tag-set match, or weighted partial match?
3. Should we include cost per successful merge as a routing objective in MVP?
4. Should exploration be enabled by default (for drift detection), and at what rate?
5. Should smart routing be project-local only, or optionally use global cross-project history?

---

## 14. Initial Effort Estimate

- MVP (phases 0-2): 1-2 engineering days.
- Retry switch optimization + optional explainability CLI: +1-2 days.
