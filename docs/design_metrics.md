# Design: Meta-Metrics for Self-Improvement

## Problem

Hive orchestrates agents but has no feedback loop. We don't know:
- Which models are cost-effective for which issue types
- Where workers fail most often (and why)
- Whether notes actually help downstream workers
- How much each orchestration run costs in tokens/dollars
- Whether our prompts are getting better or worse over time

We're flying blind. The data to answer these questions flows through the system already — we just don't capture or analyze it.

## What to collect

### Per-agent-run metrics (primary source)

Every time an agent completes (success, failure, escalation), record:

| Field | Source | Purpose |
|-------|--------|---------|
| `agent_id` | agent record | join key |
| `issue_id` | agent record | join key |
| `issue_type` | issue record | segmentation |
| `model` | issue/agent record | model comparison |
| `outcome` | final status (done/failed/escalated/canceled) | success rate |
| `duration_s` | created_at → completed_at | throughput |
| `retry_count` | count of retry events for this issue | difficulty signal |
| `merge_clean` | whether merge succeeded without conflict | code quality signal |
| `notes_injected` | count of notes given to worker | knowledge transfer |
| `notes_produced` | count of notes harvested from worker | knowledge production |
| `tokens_in` | from OpenCode SSE usage events | cost |
| `tokens_out` | from OpenCode SSE usage events | cost |

### From OpenCode SSE stream

The SSE stream from OpenCode sessions emits usage data. Currently we consume it for completion detection. We should also extract:

- Token counts (input/output per message)
- Tool call counts and types
- Session duration
- Error events

This requires tapping the SSE consumer in `OrchestratorSession` to forward usage events to a metrics collector, rather than only watching for completion signals.

### Derived/aggregate metrics

Computed from the raw data, surfaced by `hive metrics`:

- **Model success rate** — `done / (done + failed + escalated)` per model, optionally segmented by issue type
- **Cost per issue** — average tokens × model pricing, per issue type
- **Escalation rate** — how often issues need retry/escalation before resolution
- **Mean time to resolution** — from issue creation to finalization
- **Note utility** — correlation between notes_injected and success rate (do notes help?)
- **Merge health** — % of completions that merge cleanly
- **Model ROI** — success rate / cost (which model gives best bang for buck)

## Storage

### Option A: Dedicated metrics table (recommended)

```sql
CREATE TABLE agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    issue_id TEXT NOT NULL,
    project TEXT NOT NULL,
    issue_type TEXT,
    model TEXT,
    outcome TEXT NOT NULL,       -- done, failed, escalated, canceled
    duration_s REAL,
    retry_number INTEGER DEFAULT 0,
    merge_clean BOOLEAN,
    notes_injected INTEGER DEFAULT 0,
    notes_produced INTEGER DEFAULT 0,
    tokens_in INTEGER,
    tokens_out INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);
```

Why a separate table instead of computing from events? Events are an append-only log — great for audit trail, bad for fast aggregation. `agent_runs` is a materialized summary, written once at agent completion.

### Option B: Compute from events table

No new table. Use SQL aggregations over the existing events table. Simpler schema, but slower queries and requires careful event taxonomy.

**Recommendation:** Option A. The events table is for history/debugging; metrics need fast reads.

## CLI: `hive metrics`

```bash
# Overall dashboard
hive metrics
# ┌─────────────────────────────────────────┐
# │ Hive Metrics — my-app (last 7 days)     │
# ├─────────┬────────┬─────────┬────────────┤
# │ Model   │ Runs   │ Success │ Avg Tokens │
# ├─────────┼────────┼─────────┼────────────┤
# │ sonnet  │ 42     │ 88%     │ 12.4k     │
# │ opus    │ 8      │ 100%    │ 31.2k     │
# │ haiku   │ 15     │ 73%     │ 4.1k      │
# └─────────┴────────┴─────────┴────────────┘
# Escalation rate: 14%  |  Avg retries: 0.3  |  Merge success: 91%

# Filter by time range
hive metrics --since 2026-01-01
hive metrics --last 30d

# Detailed breakdown
hive metrics --by-type     # segment by issue type
hive metrics --by-model    # segment by model (default)

# JSON for piping to analysis tools
hive --json metrics
```

## The feedback loop (future)

The real payoff: metrics inform orchestrator decisions.

### Adaptive model routing

Currently, model selection is static (per-issue `model` field or project default). With enough data:

```python
def select_model_for_issue(self, issue):
    """Pick the best model based on historical performance."""
    # Query agent_runs for this issue_type
    stats = self.db.get_model_stats_for_type(issue["type"])

    # If a cheaper model has >90% success rate, use it
    # If success rate is low, escalate to a more capable model
    # Factor in cost (tokens × price) for ROI ranking
    ...
```

This is probably premature until we have enough data (50+ runs per model/type combo), but the metrics table makes it possible.

### Prompt effectiveness tracking

If we version prompts (even loosely, via a hash or label), we can correlate prompt versions with success rates. "Did the prompt change we made last week actually improve outcomes?"

### Anomaly detection

Flag when metrics deviate from baselines: "Sonnet's success rate dropped from 88% to 60% this week — investigate."

## Implementation approach

1. **Add `agent_runs` table** to DB schema
2. **Record runs** in `handle_agent_complete()` — most data is already available there
3. **Tap SSE for tokens** — extend the SSE consumer to extract usage data, store on agent record or pass to metrics
4. **`hive metrics` command** — SQL aggregations over `agent_runs`, formatted output
5. **Data accumulates** — revisit adaptive routing once there's enough signal

## Open questions

- How do we get token counts? OpenCode's SSE format needs investigation — does it emit usage data per-message or per-session?
- Should metrics be per-project or global? Per-project for success rates (different codebases have different difficulty), global for model comparisons.
- How long to retain raw `agent_runs`? Probably indefinitely — rows are small and the data gets more valuable over time.
