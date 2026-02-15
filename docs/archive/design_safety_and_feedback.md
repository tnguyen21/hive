# Design: Safety Guardrails & Feedback Loops

## Problem

Hive generates a lot of signal — events, rejection reasons, test output, notes, cost data — but most of it doesn't flow back to where it would improve decisions. Workers retry blind. The refinery's observations die in the events table. Cost accumulates with no ceiling. The system works, but it can't protect itself from runaway failures or learn from its own mistakes.

This doc covers two related concerns:

1. **Safety & observability** — cost guardrails, invariant checking, worker output validation
2. **Feedback loops** — retry context injection, refinery feedback routing, per-issue test commands

The unifying theme: take existing data and route it to where it changes outcomes.

---

## 1. Cost Guardrails

### Motivation

The 11-hour spawn loop post-mortem (IMPLEMENTATION_NOTES.md) burned 319 agents and significant API credits. The retry budget fix prevents infinite cycles, but there's no defense against a single worker burning tokens endlessly on a task it can't solve, or a run accumulating cost across many issues without limit.

### Design

Three tiers of enforcement, checked in `monitor_agent` and `handle_agent_complete`:

**Per-issue token budget.** Kill a worker session if the issue's cumulative token spend exceeds a threshold.

```python
# In monitor_agent, on each SSE usage event:
issue_tokens = self.db.get_issue_token_total(agent.issue_id)
if issue_tokens > Config.MAX_TOKENS_PER_ISSUE:
    logger.warning(f"Issue {agent.issue_id} exceeded token budget ({issue_tokens} > {Config.MAX_TOKENS_PER_ISSUE})")
    await self._cleanup_session(agent)
    self._handle_agent_failure(agent, CompletionResult(
        status="failed",
        summary=f"Terminated: exceeded per-issue token budget ({issue_tokens} tokens)",
    ))
```

**Per-run budget cap.** Pause the orchestrator (stop spawning new workers) when total spend for the current daemon run exceeds a threshold. Existing workers finish but no new ones start.

```python
# In main_loop, before spawning:
run_tokens = self.db.get_run_token_total(self.run_id)
if run_tokens > Config.MAX_TOKENS_PER_RUN:
    if not self._budget_paused:
        logger.warning(f"Run budget exceeded ({run_tokens} tokens). Pausing new spawns.")
        self._budget_paused = True
        self.db.log_event(None, None, "budget_paused", {"total_tokens": run_tokens})
    continue  # skip spawn, but keep monitoring active agents
```

**Anomaly detection.** If N workers fail on the same issue within M minutes, auto-escalate instead of retrying. This catches cases where the retry budget hasn't technically been exhausted but the pattern is clearly degenerate.

```python
# In _handle_agent_failure, before deciding to retry:
recent_failures = self.db.count_events_since(
    issue_id=issue_id,
    event_type="failed",
    since=datetime.now() - timedelta(minutes=Config.ANOMALY_WINDOW_MINUTES),
)
if recent_failures >= Config.ANOMALY_FAILURE_THRESHOLD:
    logger.warning(f"Anomaly: {recent_failures} failures on {issue_id} in {Config.ANOMALY_WINDOW_MINUTES}m")
    self._escalate_issue(issue_id, reason="Anomaly detection: rapid repeated failures")
    return
```

### Configuration

```toml
# .hive.toml or environment variables
[safety]
max_tokens_per_issue = 200000     # HIVE_MAX_TOKENS_PER_ISSUE
max_tokens_per_run = 2000000      # HIVE_MAX_TOKENS_PER_RUN
anomaly_window_minutes = 10       # HIVE_ANOMALY_WINDOW_MINUTES
anomaly_failure_threshold = 3     # HIVE_ANOMALY_FAILURE_THRESHOLD
```

### Dependency

Token counting requires tapping the SSE stream for usage data. This overlaps with the metrics design (`design_metrics.md`). The `agent_runs` table from that design provides `tokens_in`/`tokens_out` per agent. Cost guardrails can use the same data path — the SSE consumer forwards usage events to both the metrics collector and the budget checker.

If metrics aren't implemented yet, a simpler interim approach: count SSE `usage` events and multiply by a rough tokens-per-event estimate. Imprecise but sufficient for circuit-breaking.

---

## 2. `hive doctor` — Invariant Checker

### Motivation

The post-mortem documented 5 invariants (INV-1 through INV-5). These exist as prose and diagnostic SQL queries in IMPLEMENTATION_NOTES.md but aren't enforced programmatically. A `hive doctor` command turns them into a runnable health check.

### Invariants

| ID | Rule | Diagnostic query |
|----|------|-----------------|
| INV-1 | Retry budget is universal | Open issues with retry count >= MAX_RETRIES |
| INV-2 | assignee/status consistency | `open` with non-null assignee, or `in_progress` with null assignee |
| INV-3 | No unbounded loops | Issues with agent count > MAX_RETRIES + MAX_AGENT_SWITCHES + reasonable margin |
| INV-4 | State transitions funneled | (structural — not checkable at runtime, but the other invariants catch its violations) |
| INV-5 | Events are source of truth for retry budgets | Issues where retry count from events disagrees with expected state |

Additional checks beyond the original 5:

| ID | Rule | Diagnostic query |
|----|------|-----------------|
| INV-6 | No orphaned agents | Agents in `active`/`idle` status with no corresponding live OpenCode session |
| INV-7 | No stuck merges | Merge queue entries in `running` status for > 30 minutes |
| INV-8 | No ghost worktrees | Git worktrees on disk with no corresponding active agent in DB |

### CLI

```bash
hive doctor
# ┌──────────────────────────────────────────────────┐
# │ Hive Health Check                                │
# ├──────┬────────┬──────────────────────────────────┤
# │ ID   │ Status │ Description                      │
# ├──────┼────────┼──────────────────────────────────┤
# │ INV-1│ OK     │ Retry budgets respected           │
# │ INV-2│ WARN   │ 1 issue with inconsistent state   │
# │ INV-3│ OK     │ No unbounded loops                │
# │ INV-5│ OK     │ Event counts consistent            │
# │ INV-6│ FAIL   │ 2 orphaned agents found            │
# │ INV-7│ OK     │ No stuck merges                    │
# │ INV-8│ WARN   │ 1 ghost worktree on disk           │
# └──────┴────────┴──────────────────────────────────┘
# 1 failure, 1 warning. Run `hive doctor --fix` to attempt auto-repair.

hive doctor --fix    # auto-repair what's safe (mark orphaned agents failed, reset stuck merges)
hive doctor --json   # machine-readable output
```

### When to run

- **Daemon startup**: `doctor` runs automatically in `initialize()`. Failures are logged; warnings are logged. The daemon starts regardless (don't block on warnings), but failures could optionally block startup with a `--strict` flag.
- **CLI**: `hive doctor` available anytime for manual checks.
- **Periodic**: Optionally, run every N minutes in the daemon's background loop. Lightweight — it's just SQL queries.

### Implementation

Add a `DoctorCheck` protocol and a list of check functions in a new `doctor.py` module:

```python
@dataclass
class CheckResult:
    id: str
    status: str          # "ok", "warn", "fail"
    description: str
    details: list[dict]  # affected rows, for --verbose or --fix

def check_inv1_retry_budgets(db: HiveDB) -> CheckResult:
    """Open issues with exhausted retry budget."""
    rows = db.conn.execute("""
        SELECT i.id, i.title,
               (SELECT COUNT(*) FROM events WHERE issue_id = i.id AND event_type = 'retry') as retries
        FROM issues i
        WHERE i.status = 'open'
          AND (SELECT COUNT(*) FROM events WHERE issue_id = i.id AND event_type = 'retry') >= ?
    """, (Config.MAX_RETRIES,)).fetchall()

    if rows:
        return CheckResult("INV-1", "fail", f"{len(rows)} open issue(s) with exhausted retry budget", rows)
    return CheckResult("INV-1", "ok", "Retry budgets respected", [])

ALL_CHECKS = [check_inv1_retry_budgets, check_inv2_assignee_status, ...]
```

---

## 3. Worker Output Validation

### Motivation

Workers write `.hive-result.jsonl` to signal completion. The orchestrator trusts this file. But a worker can claim `status: done` and `tests_run: true` without actually making commits, running tests, or producing meaningful changes. Cheap post-completion checks catch the most obvious cases of "happy path" result files that don't reflect reality.

### Checks

Run after parsing `.hive-result.jsonl`, before entering the merge queue:

```python
async def _validate_worker_output(self, agent: AgentIdentity, result: CompletionResult) -> list[str]:
    """Returns a list of validation warnings. Empty = clean."""
    warnings = []
    worktree = self._get_worktree_path(agent)

    # 1. Did the worker actually make commits?
    diff = subprocess.run(
        ["git", "diff", "--stat", "main...HEAD"],
        cwd=worktree, capture_output=True, text=True
    )
    if not diff.stdout.strip():
        warnings.append("Worker claimed success but made no commits relative to main")

    # 2. Does the claimed files_changed list match reality?
    if result.files_changed:
        actual_files = set(diff.stdout.strip().splitlines())  # parse diffstat
        claimed_files = set(result.files_changed)
        if not claimed_files.intersection(actual_files):
            warnings.append(f"Claimed files_changed {claimed_files} but actual diff shows {actual_files}")

    # 3. Did they claim tests_run but the test command doesn't exist?
    if result.tests_run and result.test_command:
        test_bin = result.test_command.split()[0]
        which = subprocess.run(["which", test_bin], cwd=worktree, capture_output=True)
        if which.returncode != 0:
            warnings.append(f"Claimed tests_run=true but test binary '{test_bin}' not found")

    return warnings
```

### Behavior on validation failure

Validation warnings don't block the merge queue — they're recorded as events and available for review. A hard failure (no commits at all on a `status: done` result) should route through `_handle_agent_failure` instead of the merge queue.

```python
warnings = await self._validate_worker_output(agent, result)
if warnings:
    self.db.log_event(issue_id, agent.agent_id, "validation_warning", {"warnings": warnings})

no_commits = any("no commits" in w for w in warnings)
if result.status == "done" and no_commits:
    logger.warning(f"Agent {agent.agent_id} claimed done but made no commits — treating as failure")
    self._handle_agent_failure(agent, CompletionResult(
        status="failed",
        summary="Validation failure: no commits despite claiming success",
    ))
    return
```

---

## 4. Retry Context Injection

### Motivation

When a worker fails and the issue is retried, the next worker gets the exact same prompt. The failure reason exists in the events table. The refinery rejection summary exists. None of it flows into the retry prompt.

A worker retrying "Add auth middleware" after a rejection for "test failures in conftest.py fixtures" should know that going in. This is the single highest-ROI feedback loop to close.

### Design

In `_build_worker_prompt()`, when the issue has prior attempts, inject a failure context section:

```python
def _build_retry_context(self, issue_id: str) -> str | None:
    """Build context from prior failures for retry attempts."""
    failure_events = self.db.get_events_by_type(issue_id, ["failed", "stalled", "merge_rejected"])
    if not failure_events:
        return None

    sections = []
    for event in failure_events:
        data = json.loads(event["data"]) if event["data"] else {}
        timestamp = event["created_at"]
        event_type = event["event_type"]

        if event_type == "failed":
            sections.append(f"- **Attempt failed** ({timestamp}): {data.get('reason', 'Unknown')}")
        elif event_type == "stalled":
            sections.append(f"- **Worker stalled** ({timestamp}): Agent timed out without completing")
        elif event_type == "merge_rejected":
            sections.append(f"- **Merge rejected** ({timestamp}): {data.get('rejection_reason', 'Unknown')}")

            # Include refinery observations if available
            if data.get("refinery_summary"):
                sections.append(f"  Refinery notes: {data['refinery_summary']}")

    return "## Prior Attempts\n\nThis issue has been attempted before. Previous attempts failed for the following reasons:\n\n" + "\n".join(sections) + "\n\nUse this context to avoid repeating the same mistakes. Address the specific failure reasons above."
```

### Prompt integration

In the worker prompt template (`prompts/worker.md`), add a conditional section:

```markdown
$prior_attempts
```

In `_build_worker_prompt()`:

```python
retry_context = self._build_retry_context(issue_id) or ""
prompt = template.substitute(
    ...,
    prior_attempts=retry_context,
)
```

---

## 5. Refinery Feedback Loop

### Motivation

When the refinery rejects a branch, the rejection reason is logged as an event but doesn't persist as a note on the issue. If the issue is retried, the retry context injection (#4 above) can pull from the events table — but the refinery's observations are particularly high-value because it's the only agent with the integration picture.

### Design

When the refinery rejects a merge, auto-create a structured note:

```python
# In merge.py, after refinery rejection:
async def _handle_refinery_rejection(self, entry, rejection_data):
    issue_id = entry["issue_id"]

    # Log the event (existing behavior)
    self.db.log_event(issue_id, None, "merge_rejected", rejection_data)

    # NEW: Create a structured note from the rejection
    note_content = (
        f"[Refinery rejection] {rejection_data.get('rejection_reason', 'Unknown')}\n"
        f"Branch: {entry.get('branch')}\n"
    )
    if rejection_data.get("test_output"):
        # Truncate test output to keep notes concise
        test_snippet = rejection_data["test_output"][:500]
        note_content += f"Test output (truncated):\n```\n{test_snippet}\n```\n"

    if rejection_data.get("refinery_summary"):
        note_content += f"Refinery analysis: {rejection_data['refinery_summary']}\n"

    self.db.add_note(
        project=self.project_name,
        source="refinery",
        content=note_content,
        issue_id=issue_id,
        note_type="rejection",
    )
```

This differs from generic note harvesting (which captures worker-produced `.hive-notes.jsonl`) in that:
- It's automatic — the refinery doesn't need to explicitly write a notes file
- It's structured — the note has a known format that retry context injection can parse
- It's scoped to the issue — future workers on the same issue get it via both notes injection and retry context

### Note schema change

Add an optional `note_type` column to distinguish rejection notes from regular notes:

```sql
ALTER TABLE notes ADD COLUMN note_type TEXT DEFAULT 'general';
-- Values: 'general', 'rejection', 'gotcha', 'pattern'
```

This also enables filtering in `hive notes --type rejection` and could feed into the vector notes ranking (rejection notes might be weighted higher for retry contexts).

---

## 6. Per-Issue Test Commands

### Motivation

Workers write `test_command` in their `.hive-result.jsonl`, but the merge pipeline uses only the global `HIVE_TEST_COMMAND`. For large repos, running the full suite on every merge is expensive. If a worker knows the relevant test file, the refinery should use that information.

### Design

In the merge pipeline, prefer the worker's test command when available:

```python
async def _run_tests(self, entry):
    """Run tests for a merge candidate. Prefer worker-specified command, fall back to global."""
    worktree = entry["worktree_path"]

    # Check if the worker specified a test command
    worker_test_cmd = entry.get("test_command")  # from .hive-result.jsonl
    global_test_cmd = Config.TEST_COMMAND

    if worker_test_cmd and global_test_cmd:
        # Run both: worker-specific first (fast feedback), then global (full validation)
        specific_ok, specific_output = run_command_in_worktree(worktree, worker_test_cmd, timeout=120)
        if not specific_ok:
            return False, specific_output

        global_ok, global_output = run_command_in_worktree(worktree, global_test_cmd, timeout=300)
        return global_ok, global_output

    elif worker_test_cmd:
        return run_command_in_worktree(worktree, worker_test_cmd, timeout=120)

    elif global_test_cmd:
        return run_command_in_worktree(worktree, global_test_cmd, timeout=300)

    else:
        return True, ""  # no tests configured
```

### Merge queue schema

Store the worker's test command in the merge queue entry so it's available at merge time:

```sql
ALTER TABLE merge_queue ADD COLUMN test_command TEXT;
```

Populated when enqueueing:

```python
def enqueue_merge(self, issue_id, agent_id, branch, worktree_path, test_command=None):
    self.conn.execute(
        "INSERT INTO merge_queue (issue_id, agent_id, branch, worktree_path, test_command, status) VALUES (?, ?, ?, ?, ?, 'queued')",
        (issue_id, agent_id, branch, worktree_path, test_command),
    )
```

---

## Implementation Order

```
1. Retry context injection        — highest ROI, no schema changes, ~50 lines
2. Cost guardrails                — safety-critical, depends on token counting from SSE
3. Refinery feedback loop         — small, leverages existing notes system
4. Worker output validation       — cheap sanity checks, no schema changes
5. hive doctor                    — new module, references existing invariants
6. Per-issue test commands        — merge queue schema change, straightforward
```

Items 1 and 3 close feedback loops. Items 2 and 4 are safety nets. Item 5 is a diagnostic tool. Item 6 is an efficiency improvement. All are independent and can be built in any order, but the suggested sequence front-loads the highest-impact, lowest-effort changes.

## Dependencies

- **Metrics** (`design_metrics.md`): Cost guardrails share the token-counting data path. Can be built independently (guardrails just need a running total, not the full metrics table), but implementing both together avoids duplicate SSE tapping.
- **Race condition fixes** (`RACE_CONDITION_AUDIT.md`): Worker output validation runs subprocess calls — these should use `run_in_executor` per DC-1 to avoid blocking the event loop.
- **Notes system** (Phase 7): Refinery feedback loop extends the existing notes infrastructure. Requires the `notes` table to exist (it does).
