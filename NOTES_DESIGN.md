# Notes System: Complete Implementation Plan

## Context

The notes system lets workers share knowledge across sessions — a worker can write discoveries, gotchas, and patterns to `.hive-notes.jsonl`, and future workers get those notes injected into their prompts. The foundation is solid (DB schema, CRUD, file I/O, prompt template all done and tested), but the wiring is missing: CLI commands, orchestrator harvest/inject, and one DB helper. These were the two failed issues (`w-49ea04`, `w-f3c2b8`) that triggered the infinite spawn loop we just fixed.

## What Already Exists

| Component | Status | Location |
|-----------|--------|----------|
| DB schema (`notes` table + indexes) | Done | `db.py:94-105` |
| `add_note()`, `get_notes()`, `get_notes_for_molecule()`, `get_recent_project_notes()` | Done | `db.py:918-1015` |
| `read_notes_file()`, `remove_notes_file()` | Done | `prompts.py:341-366` |
| `build_worker_prompt(notes=...)` parameter | Done | `prompts.py:50-119` |
| Worker template `${notes_section}` + KNOWLEDGE SHARING section | Done | `prompts/worker.md:11,76-98` |
| 25+ tests for DB + prompts notes functions | Done | `test_db.py`, `test_prompts.py` |

## What's Missing

| Component | Status | Location |
|-----------|--------|----------|
| CLI commands (`hive note`, `hive notes`) | Missing | `cli.py`, `tools.py` |
| Orchestrator harvest (read `.hive-notes.jsonl` on completion) | Missing | `orchestrator.py:handle_agent_complete` |
| Orchestrator inject (pass notes to worker prompts) | Missing | `orchestrator.py:spawn_worker`, `cycle_agent_to_next_step` |
| `get_completed_molecule_steps()` DB helper | Missing | `db.py` |

## Files to Modify

| File | Changes |
|------|---------|
| `src/hive/db.py` | Add `get_completed_molecule_steps()` |
| `src/hive/tools.py` | Add `handle_hive_add_note()`, `handle_hive_get_notes()` |
| `src/hive/cli.py` | Add subparsers, dispatch, `add_note()`, `list_notes()` methods |
| `src/hive/orchestrator.py` | Add imports, harvest in `handle_agent_complete`, inject in `spawn_worker` + `cycle_agent_to_next_step`, add `_gather_notes_for_worker` helper |
| `tests/test_db.py` | Tests for `get_completed_molecule_steps` |
| `tests/test_cli.py` | Tests for `hive note` and `hive notes` commands |
| `tests/test_orchestrator.py` | Tests for harvest, inject, and dedup logic |

## Implementation Steps

### Step 1: `db.py` — Add `get_completed_molecule_steps()` (~line 993)

After existing `get_notes_for_molecule()`. Returns completed/finalized sibling issues for a molecule, ordered by creation time. Used by `cycle_agent_to_next_step` to populate the `completed_steps` param of `build_worker_prompt()` (which already accepts it but was never being given data).

```python
def get_completed_molecule_steps(self, parent_id: str) -> List[Dict]:
    if not self.conn:
        raise RuntimeError("Database not connected")
    rows = self.conn.execute('''
        SELECT id, title, description, status FROM issues
        WHERE parent_id = ? AND status IN ('done', 'finalized')
        ORDER BY created_at ASC
    ''', (parent_id,)).fetchall()
    return [dict(row) for row in rows]
```

### Step 2: `tools.py` — Add note tool handlers (~line 487)

After `handle_hive_get_events`. Two handlers following the existing pattern:

- `handle_hive_add_note(content, issue_id=None, category='discovery')` — calls `self.db.add_note(agent_id=None, ...)`, returns dict with note_id
- `handle_hive_get_notes(issue_id=None, category=None, limit=20)` — calls `self.db.get_notes(...)`, returns dict with notes list and count

### Step 3: `cli.py` — Add CLI commands

**Subparsers** (after `watch_parser` at line 1082, before `args = parser.parse_args()`):
- `note` parser: positional `content`, optional `--issue`, `--category` with choices
- `notes` parser: optional `--issue`, `--category`, `--limit`

**HiveCLI methods** (after `close()` at line 424):
- `add_note()` — calls `_run_tool('hive_add_note', ...)`, prints confirmation in non-JSON mode
- `list_notes()` — calls `_run_tool('hive_get_notes', ...)`, prints formatted table in non-JSON mode

**Dispatch** (before `else: parser.print_help()` at line 1234):
- `elif args.command == "note":` → `cli.add_note(...)`
- `elif args.command == "notes":` → `cli.list_notes(...)`

### Step 4: `orchestrator.py` — Harvest + Inject

**4a. Imports** (line 17-23): Add `read_notes_file`, `remove_notes_file` to the prompts import block.

**4b. Harvest in `handle_agent_complete()`** (after `remove_result_file` at line 811, BEFORE the canceled/finalized check at line 813):

```python
# Harvest notes (best-effort, wrapped in try/except/finally)
try:
    notes_data = read_notes_file(agent.worktree)
    if notes_data:
        for note in notes_data:
            self.db.add_note(
                issue_id=agent.issue_id, agent_id=agent.agent_id,
                content=note.get("content", ""), category=note.get("category", "discovery"),
            )
        self.db.log_event(agent.issue_id, agent.agent_id, "notes_harvested", {"count": len(notes_data)})
        logger.info(f"Harvested {len(notes_data)} notes from {agent.name}")
except Exception as e:
    logger.warning(f"Failed to harvest notes from {agent.name}: {e}")
finally:
    remove_notes_file(agent.worktree)
```

Key: Harvest BEFORE the canceled check so even canceled/failed workers' discoveries are saved. The try/except/finally ensures note harvesting never blocks the completion flow, and the file always gets cleaned up.

**4c. Helper `_gather_notes_for_worker(issue_id)`** (near `_is_issue_canceled` at line 664):

```python
def _gather_notes_for_worker(self, issue_id: str) -> Optional[List[Dict[str, Any]]]:
    """Gather relevant notes for a worker prompt.

    Combines molecule-specific notes (if the issue is a step) with
    recent project-wide notes, deduplicating by note ID.
    Returns None if no notes found (build_worker_prompt skips the section).
    """
    notes = []
    seen_ids = set()

    # 1. If this is a molecule step, get notes from sibling steps
    issue = self.db.get_issue(issue_id)
    if issue and issue.get("parent_id"):
        molecule_notes = self.db.get_notes_for_molecule(issue["parent_id"])
        for note in molecule_notes:
            notes.append(note)
            seen_ids.add(note["id"])

    # 2. Get recent project-wide notes (regardless of molecule)
    recent_notes = self.db.get_recent_project_notes(limit=10)
    for note in recent_notes:
        if note["id"] not in seen_ids:
            notes.append(note)
            seen_ids.add(note["id"])

    return notes if notes else None
```

**4d. Inject in `spawn_worker()`** (before `build_worker_prompt` at line 608):

```python
worker_notes = self._gather_notes_for_worker(issue_id)
prompt = build_worker_prompt(..., notes=worker_notes)
```

**4e. Inject + completed_steps in `cycle_agent_to_next_step()`** (before `build_worker_prompt` at line 1042):

```python
worker_notes = self._gather_notes_for_worker(next_step["id"])
completed_steps = None
if next_step.get("parent_id"):
    completed_issues = self.db.get_completed_molecule_steps(next_step["parent_id"])
    completed_steps = [f"{s['title']}: {(s.get('description') or '')[:100]}" for s in completed_issues]
prompt = build_worker_prompt(..., notes=worker_notes, completed_steps=completed_steps)
```

This also fixes the existing gap where `completed_steps` was never populated.

## Design Decisions

### What We're NOT Doing

- **Gas Town-style channels/mail**: Over-engineering. Gas Town has a full mail system with direct/queue/announce/channel/group addressing. Our notes system with 5 categories (discovery, gotcha, dependency, pattern, context) covers the core use case — asynchronous knowledge sharing between workers. If real-time inter-agent messaging becomes needed, it can be layered on later by adding `recipient_agent_id` and `channel` columns to the notes table.

- **Note TTL/expiration**: Notes could accumulate over time, but the `limit` parameter on queries already prevents unbounded growth in prompt injection. Cleanup can be added later if the table gets large.

- **Note deduplication by content**: ID-based dedup (when combining molecule + project notes) is sufficient. Content-based dedup adds complexity for minimal gain.

### Why harvest before canceled check?

A worker that gets canceled externally may still have written useful notes about what it discovered before cancellation. By harvesting first, we capture all knowledge regardless of the issue's terminal state.

### Why a shared `_gather_notes_for_worker` helper?

Both `spawn_worker` and `cycle_agent_to_next_step` need the same logic: molecule notes + project notes, deduped. A single helper prevents divergence and makes the injection logic easy to evolve.

## Verification

```bash
# Run tests
uv run python -m pytest tests/test_db.py tests/test_cli.py tests/test_orchestrator.py -v

# Lint + format
uvx ruff check src/hive/db.py src/hive/cli.py src/hive/tools.py src/hive/orchestrator.py
uvx ruff format --line-length=144 src/hive/db.py src/hive/cli.py src/hive/tools.py src/hive/orchestrator.py

# Manual smoke test
hive note "Test note from CLI"
hive note --issue w-abc123 --category gotcha "Watch out for X"
hive notes
hive notes --category gotcha
hive --json notes
```
