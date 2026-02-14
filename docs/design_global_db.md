# Design: Global Shared Database

## Problem

Currently, Hive's DB lives relative to the project (passed via CLI arg or defaulting to `./hive.db`). This means:
- Each project has its own isolated DB
- Cross-project queries (metrics, agent history) are impossible
- No single place to see "what is Hive doing across all my projects"

## Change

Move the default DB to `~/.hive/hive.db`. Everything else already works — the `project` column on `issues` scopes all queries.

### What changes

| Component | Before | After |
|-----------|--------|-------|
| Default DB path | `./hive.db` or CLI arg | `~/.hive/hive.db` |
| DB resolution | hardcoded | `--db` flag > `HIVE_DB` env > config.toml > `~/.hive/hive.db` |
| Cross-project queries | impossible | natural (just don't filter by project) |
| `hive metrics` | per-project only | can aggregate across projects |

### What doesn't change

- Schema (already has `project` column everywhere)
- All existing queries (already filter by `project`)
- SQLite as the engine (single-writer is fine for single-machine use)

## Migration

For existing users with a `./hive.db`:

```bash
hive db migrate    # copies ./hive.db contents into ~/.hive/hive.db
```

Or just start fresh — the old DB is still there if needed.

## Future: Remote / Redundant

Eventually, we might want:
- Multiple machines orchestrating the same project
- Backup/replication for durability
- A web dashboard querying the DB

### Options when the time comes

**Turso / libSQL** — drop-in SQLite replacement with replication. Minimal code changes (swap the connection string). Good for "I want my DB to survive a disk failure" and "I want to query from a different machine." This is the most natural upgrade path from SQLite.

**PostgreSQL** — if we need true multi-writer concurrency (multiple orchestrators writing simultaneously). More operational overhead. Would require query syntax changes (datetime functions, etc.).

**Litestream** — streams SQLite WAL to S3 for continuous backup. Zero code changes. Doesn't help with multi-writer, but gives durability cheaply.

### Recommended path

1. **Now:** `~/.hive/hive.db` (this design)
2. **When durability matters:** Litestream for backup
3. **When multi-machine matters:** Turso/libSQL

The `Database` class already abstracts all SQL access, so swapping the backend is contained.

## Implementation

Minimal — mostly config wiring:

1. Create `~/.hive/` directory on first run (in CLI startup or `Database.connect()`)
2. Change default `db_path` to `~/.hive/hive.db`
3. Respect `--db` flag and `HIVE_DB` env var for overrides
4. Update tests to use temp dirs (already do via fixtures)
