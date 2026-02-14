# Design: Vector-Augmented Notes (Institutional Memory)

## Problem

The notes system currently retrieves by recency: "give me the last N notes for this project/molecule." This works early on, but degrades as notes accumulate:

- A gotcha discovered 200 sessions ago about a tricky API is lost to recency
- A pattern note about testing conventions from month one is buried
- Workers re-discover things that were already documented
- The system forgets its own hard-won knowledge

We want Hive to behave like an institution that accumulates and retrieves relevant operational memory, regardless of when it was written.

## Vision

When a worker starts an issue titled "Fix authentication timeout in OAuth flow," it should receive:
- Recent notes (current context, what just happened)
- Semantically similar notes (that gotcha about OAuth token refresh from 3 months ago)
- High-value notes (patterns/gotchas that have been validated by multiple workers)

## Architecture

```
Worker spawns
  → embed issue title + description
  → query note_embeddings for top-K similar
  → also pull recent N notes (existing behavior)
  → merge, deduplicate, rank
  → inject into worker prompt
```

### Storage: sqlite-vec

[sqlite-vec](https://github.com/asg017/sqlite-vec) is a SQLite extension for vector search. It fits naturally:

- No new infrastructure (stays in hive.db)
- Installable as a Python package (`pip install sqlite-vec`)
- Supports cosine similarity search
- Works with the global DB design

```sql
-- Virtual table for embeddings
CREATE VIRTUAL TABLE note_embeddings USING vec0(
    note_id INTEGER PRIMARY KEY,
    embedding FLOAT[384]         -- dimension depends on model
);
```

### Embedding model

| Option | Pros | Cons |
|--------|------|------|
| `sentence-transformers` (local) | Free, fast, no API dependency | Heavy dependency (~500MB), GPU helps |
| `text-embedding-3-small` (OpenAI API) | Tiny, cheap ($0.02/1M tokens), high quality | API dependency, needs key |
| `nomic-embed-text` (via Ollama) | Local, good quality, moderate size | Requires Ollama running |

**Recommendation:** Support both local and API, with config to choose. Default to API if an OpenAI key is available (most Hive users will have one for the LLM anyway), fall back to local.

For the `uv tool install` packaging story, the local model is a heavy dependency. Making it optional (`hive[local-embeddings]`) keeps the base install light.

### Embedding lifecycle

**On `add_note()`:**
```python
def add_note(self, ...):
    note_id = ...  # existing insert

    if self.embeddings_enabled:
        embedding = self.embed(content)
        self.conn.execute(
            "INSERT INTO note_embeddings(note_id, embedding) VALUES (?, ?)",
            (note_id, embedding)
        )

    return note_id
```

**On `_gather_notes_for_worker(issue_id)`:**
```python
def _gather_notes_for_worker(self, issue_id):
    issue = self.db.get_issue(issue_id)
    query_text = f"{issue['title']} {issue.get('description', '')}"

    # Semantic retrieval
    if self.db.embeddings_enabled:
        similar_notes = self.db.find_similar_notes(
            query_text,
            project=self.project_name,
            limit=10
        )
    else:
        similar_notes = []

    # Recency retrieval (existing)
    recent_notes = self.db.get_recent_project_notes(limit=5)

    # Molecule notes (existing, if applicable)
    molecule_notes = ...

    # Merge and deduplicate
    all_notes = _merge_notes(similar_notes, recent_notes, molecule_notes)
    return all_notes
```

### Ranking and merging

When combining semantic and recency results, we want a blended score:

```python
def _merge_notes(similar, recent, molecule):
    """Merge note sources, deduplicate, rank."""
    seen = set()
    scored = []

    # Semantic matches get their similarity score
    for note, similarity in similar:
        if note["id"] not in seen:
            seen.add(note["id"])
            scored.append((note, similarity))

    # Recent notes get a recency boost (but lower than high-similarity matches)
    for i, note in enumerate(recent):
        if note["id"] not in seen:
            seen.add(note["id"])
            recency_score = 0.5 - (i * 0.02)  # decaying boost
            scored.append((note, recency_score))

    # Molecule notes always included (highest priority for current workflow)
    for note in molecule:
        if note["id"] not in seen:
            seen.add(note["id"])
            scored.append((note, 1.0))

    # Sort by score, take top N
    scored.sort(key=lambda x: x[1], reverse=True)
    return [note for note, _ in scored[:15]]
```

### Graceful degradation

If sqlite-vec is not installed or embeddings are not configured:
- All embedding operations are no-ops
- `_gather_notes_for_worker` falls back to the current recency-only behavior
- No errors, no degraded functionality — just less intelligent retrieval

This means the base Hive install works without vector support. Users opt in when they want it.

## Note quality over time

Not all notes are equally valuable. Over time, we might want:

### Implicit signals
- Notes referenced by successful workers (correlation with good outcomes) are higher quality
- Notes from issues that needed 0 retries are likely more accurate
- Notes that get re-discovered (similar content from different workers) are validated

### Explicit signals
- `hive notes --promote <id>` — manually boost a note's weight
- Category weighting: `gotcha` notes are generally more actionable than `context` notes
- Staleness: notes about code that has changed significantly might be outdated

This is future work — start with uniform weighting and add signals as we learn what matters.

## Prompt budget

Injecting too many notes wastes context window. We need a budget:

- **Default:** ~2000 tokens of notes per worker prompt
- **Configurable:** via `.hive.toml` or issue-level setting
- **Prioritized:** high-similarity and molecule notes fill the budget first; recency fills the rest
- **Truncation:** long notes get truncated with `...` suffix

The current `build_worker_prompt(notes=...)` already formats notes into the prompt. The change is in what gets passed, not how it's rendered.

## Implementation sequence

1. **Add sqlite-vec dependency** (optional extra)
2. **Embedding abstraction** — `EmbeddingProvider` interface with API and local implementations
3. **Schema** — `note_embeddings` virtual table, created if sqlite-vec is available
4. **Write path** — embed on `add_note()`, store in virtual table
5. **Read path** — `find_similar_notes()` method, update `_gather_notes_for_worker`
6. **Ranking** — blended scoring for semantic + recency + molecule
7. **`hive notes --similar "query"`** — CLI for manual semantic search (useful for debugging)

## Dependencies

- Requires global DB (design_global_db.md) — vector search across projects is the point
- Benefits from metrics (design_metrics.md) — note utility metrics tell us if this is working
- Independent of packaging (design_packaging.md) — but sqlite-vec as optional extra matters for install story

## Open questions

- What embedding dimension? 384 (MiniLM) is small and fast. 1536 (OpenAI) is higher quality but 4x storage. Probably start with whatever the default model produces.
- Should we re-embed notes when the embedding model changes? Probably yes — a one-time migration. Store the model name alongside embeddings.
- How do we handle note updates/deletions? CASCADE from notes table, or manual sync?
- Is there a simpler first step? E.g., keyword/TF-IDF search over notes using SQLite FTS5 (no embeddings needed). This gets 80% of the value with zero new dependencies.
