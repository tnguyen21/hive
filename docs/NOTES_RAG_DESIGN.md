# Notes RAG Design (Issue Bootstrap Retrieval)

_Status: proposed_

## 1. Summary

This document proposes vector retrieval over historical notes so newly created issues can inherit relevant project knowledge discovered in prior debugging/development loops.

Recommendation: worthwhile, but only if we can prove it reduces duplicate groundwork/rework without adding excessive token or latency cost.

## 2. Problem

In long-lived projects, useful implementation knowledge is often captured in notes but not in commit messages or permanent docs. As note volume grows, keyword search is not enough to reliably surface relevant context when a new issue starts.

Resulting failure mode:
1. workers repeat prior investigations,
2. workers re-introduce previously rejected approaches,
3. parallel workers duplicate foundational changes (schema/contracts/migrations).

## 3. Goals

1. Retrieve relevant historical notes at issue creation/assignment time.
2. Inject a compact "historical context" block into initial worker prompt.
3. Keep retrieval deterministic and project-scoped.
4. Measure whether retrieval improves outcomes before broad rollout.

## 4. Non-Goals

1. Replacing deterministic live mail routing/ack from `docs/NOTES_SPEC.md`.
2. Building full semantic search UI in v1.
3. Auto-applying retrieved advice as hard constraints.

## 5. High-Level Design

Two channels, different purpose:
1. **Live notes** (from `NOTES_SPEC`): in-progress coordination, required acknowledgments.
2. **Historical retrieval** (this doc): prior-run memory at issue bootstrap.

Historical retrieval is best treated as advisory context, not required-ack mail.

## 6. Data Model (Additive)

### 6.1 New table: `note_embeddings`

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | surrogate key |
| `note_id` | INTEGER NOT NULL | FK to `notes(id)` |
| `project` | TEXT NOT NULL | project scope filter |
| `embedding_model` | TEXT NOT NULL | versioned model id |
| `embedding_dim` | INTEGER NOT NULL | validation/compat |
| `vector_blob` | BLOB NOT NULL | packed float32 embedding |
| `content_hash` | TEXT NOT NULL | recompute trigger |
| `created_at` | TEXT NOT NULL | timestamp |

Indexes:
1. `idx_note_embeddings_note_id` on `note_id`
2. `idx_note_embeddings_project_model` on `(project, embedding_model)`
3. `uidx_note_embeddings_note_model` unique on `(note_id, embedding_model)`

### 6.2 Optional metadata extension

If useful for reranking without extra joins:
1. `notes.importance_score` (manual or heuristic),
2. `notes.resolution_outcome` (`confirmed|superseded|experimental`).

These are optional and should not block v1 retrieval.

## 7. Retrieval Pipeline

### 7.1 Ingestion

When a note is created:
1. normalize content,
2. compute `content_hash`,
3. enqueue embedding generation job,
4. upsert `note_embeddings` row.

Batch backfill command:
```bash
hive notes embed backfill --project <name> [--since <date>] [--limit <n>]
```

### 7.2 Query Construction (on issue start)

Build query text from:
1. issue title,
2. issue description,
3. parent epic title/summary (if present),
4. dependency issue titles (optional),
5. tags/file hints (if available).

### 7.3 Candidate Retrieval

1. vector similarity search top `K` (project-scoped),
2. optional lexical fallback if embedding unavailable,
3. drop very old/low-confidence candidates with thresholding.

### 7.4 Reranking

Rerank by weighted score:
1. similarity,
2. recency,
3. graph proximity (same epic/dep neighborhood),
4. sender reliability signal (optional).

### 7.5 Prompt Injection

Inject a separate section in the initial worker turn only:

```text
### Historical Notes Context (top 3)
- [note:n-221][score:0.83][2026-01-09]
  Prior migration failed when defaults were applied before backfill.
- [note:n-180][score:0.79][2025-12-28]
  Reuse shared parser in src/foo/parser.py to avoid duplicate edge handling.
```

Hard cap:
1. max notes: 3-5,
2. max tokens: fixed budget (for example 250-400 tokens).

## 8. Routing/Safety Rules

1. Never retrieve cross-project notes by default.
2. Exclude notes marked obsolete/superseded (if such metadata exists).
3. Retrieval is advisory; live required notes still control completion gates.
4. Keep retrieved context read-only (no auto-ack semantics).

## 9. Why This Can Be Valuable

Likely high-value if:
1. projects run long enough to accumulate non-trivial note history,
2. repeated issue patterns occur (schema, infra, flaky tests, env quirks),
3. prior discoveries are not consistently written into permanent docs.

Likely low-value if:
1. projects are short-lived,
2. notes are low quality/noisy,
3. retrieval quality cannot beat simple recency/keyword heuristics.

## 10. Benchmark Plan (Decide If It Works)

### 10.1 Hypotheses

1. Retrieval reduces duplicate-groundwork incidents.
2. Retrieval reduces time-to-first-correct-change for repeated issue patterns.
3. Retrieval does not materially degrade token efficiency or completion latency.

### 10.2 Offline Evaluation

Create a replay dataset from historical issues:
1. sample completed issues with known notes history,
2. build query from issue at creation time,
3. label relevant historical notes (human or rubric-assisted),
4. compare retrieval strategies.

Strategies to compare:
1. baseline A: no retrieval,
2. baseline B: keyword/recency retrieval,
3. candidate C: vector retrieval + rerank.

Metrics:
1. Recall@K,
2. MRR@K,
3. nDCG@K,
4. precision of "actionable" notes.

### 10.3 Online A/B Evaluation

Randomize issues into:
1. control: no historical context injection,
2. treatment: historical context injection.

Primary metrics:
1. duplicate-groundwork incident rate,
2. rework rate (reverted/superseded changes within N turns),
3. completion success without escalation.

Secondary metrics:
1. cycle time per issue,
2. token cost per issue,
3. completion latency.

Guardrails:
1. no >10% median token increase,
2. no statistically significant latency regression beyond threshold.

### 10.4 Instrumentation Needed

Events:
1. `note_embedding_created`,
2. `historical_retrieval_performed`,
3. `historical_context_injected`,
4. `historical_note_cited` (optional),
5. `duplicate_groundwork_detected`.

Store per-issue experiment arm and retrieved note IDs for replay/debug.

### 10.5 Ship Criteria

Promote beyond experiment only if:
1. primary metrics improve with confidence,
2. guardrails hold,
3. qualitative review shows fewer coordination/regression misses.

## 11. Implementation Plan (Phased)

### 11.1 Phase 0: Instrumentation First

1. Add duplicate-groundwork detection signal (manual audit tag or heuristic).
2. Add experiment arm plumbing in orchestrator.
3. Add events for retrieval/injection.

### 11.2 Phase 1: Offline Prototype

1. Add `note_embeddings` table + migration.
2. Implement embed backfill + incremental upsert.
3. Build offline eval script to compare retrieval strategies.
4. Choose initial model, `K`, and thresholds.

### 11.3 Phase 2: Online Experiment

1. Enable retrieval on issue start behind feature flag.
2. Inject compact historical context section in treatment arm.
3. Collect metrics for a fixed sample window.

### 11.4 Phase 3: Harden or Drop

1. If effective: harden reranker, add stale-note filtering, tune token budget.
2. If ineffective: remove injection path, keep embeddings optional or archive.

## 12. Files Likely to Change

| File | Work |
|---|---|
| `src/hive/db.py` | new `note_embeddings` schema + migration + CRUD |
| `src/hive/orchestrator.py` | issue-start retrieval + experiment arm gating + injection |
| `src/hive/cli.py` | backfill/maintenance commands (`hive notes embed ...`) |
| `src/hive/prompts.py` | formatter for historical context block |
| `tests/test_db.py` | migration + upsert/idempotency tests |
| `tests/test_orchestrator.py` | issue-start retrieval injection behavior |
| `tests/test_experiments.py` (or existing file) | arm assignment and telemetry |
| `docs/NOTES_SPEC.md` | optional cross-reference line to this doc |

## 13. Prompt Considerations

Prompt update should be minimal and explicit:
1. "Historical Notes Context is advisory memory; validate against current codebase."
2. "If historical advice conflicts with live required notes, follow live required notes."
3. "Do not treat historical notes as completion gates."

Avoid broad reminders; keep one deterministic injected block at issue start.

## 14. Open Questions

1. Embedding backend: local model vs API model (cost/privacy/latency tradeoff).
2. Vector search implementation in SQLite stack: extension vs app-side ANN index.
3. Should resolved/superseded notes be explicitly tracked before enabling retrieval?
4. Minimum dataset size needed for meaningful online experiment power?
