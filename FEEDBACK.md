# UX Feedback — Consolidated

## 1. Discoverability

**Problem:** Many commands that make the system feel alive (`logs`, `watch`, `notes`, `merges`, `ui`) are hidden from `hive -h`.

**Recommendation:** Rethink the two-tier help. Either add a third category ("monitoring" commands visible in help) or group commands by workflow stage so users naturally discover them.

**Comments:** Add a monitoring category in help.

**Files:** `src/hive/cli.py`

---

## 2. Safer Default Merge Posture

**Problem:** New users get auto-merge by default, which requires trust they haven't built yet.

**Recommendation:**

- During `hive setup`, ask "auto-merge to main?" and default to manual review mode
- Toggle `merge_queue_enabled` / `test_command` in `.hive.toml`
- Add `hive review` to list "done but not finalized" issues with `git diff` hints and a one-liner to finalize/merge

**Files:** `src/hive/config.py`, `src/hive/cli.py`

---

## 3. Per-Project Database

**Problem:** Global `~/.hive/hive.db` is confusing — "what is this thing doing across repos?" Agents aren't project-scoped in schema.

**Recommendation:** Default DB to per-project (e.g. `.hive/hive.db`) instead of global, or at least prefer local if present.

**Comments:** We should somehow make agents project aware. What this might look like is adding a project id to notes (first thing that comes to mind, but also means we may have to modify our tables and how we think of events). Table this for a future refactor.

**Files:** `src/hive/config.py`, `src/hive/cli.py`, `src/hive/db.py`

---

## 4. Split README from Architecture

**Problem:** README mixes onboarding with deep internals.

**Recommendation:** Keep `README.md` as "5-minute getting started + workflows + safety". Move deep internals to `docs/TECHNICAL_DESIGN_DOC.md`.

**Files:** `README.md`, `docs/TECHNICAL_DESIGN_DOC.md`

---

## 5. Notes as First-Class Onboarding Feature

**Problem:** Notes are a real leverage point for multi-agent coordination but users have to discover them on their own.

**Recommendation:**

- Have `hive setup` optionally seed a `.hive` context note (test command, lint rules, repo conventions)
- Surface notes in onboarding and worker prompts

**Files:** `src/hive/prompts/worker.md`, `src/hive/cli.py`

---

## 6. Queen Should Auto-Start the Daemon

**Problem:** `hive queen` launches Queen Bee but the user still needs `hive start` in another terminal. Two processes, two terminals, easy to forget.

**Recommendation:** `hive queen` should auto-start the daemon if it's not running (and tell the user it did). One command, one terminal.

```
$ hive queen
Starting daemon... done (PID 12345)
Launching Queen Bee...
```

---

## 7. Terminology is a Wall

**Problem:** 10 new concepts is too many for a first encounter. Some leak implementation details (Refinery, Worktree, Molecule, Merge Queue).

**Recommendation:** Simplify README/help to a 3-concept model for newcomers:

| User-facing concept | What it is                                                   | Maps to internally         |
| ------------------- | ------------------------------------------------------------ | -------------------------- |
| Queen               | "Your project manager" — you talk to her, she plans the work | Queen Bee TUI              |
| Workers             | "Your team" — they implement tasks in parallel               | Worker agents in worktrees |
| Issues              | "The task board" — what needs to get done                    | SQLite issues table        |

Everything else (Refinery, Molecule, Merge Queue, Daemon, Orchestrator) is an advanced concept introduced later.

---

## 8. `hive watch` Should Be the Default Experience

**Problem:** After `hive start`, users stare at a silent terminal with no feedback. They have to know to run `hive status`, `hive logs -f`, or `hive watch` separately.

**Recommendation:** `hive start` defaults to foreground mode with a live dashboard (like `docker compose up`). Background mode with `hive start -d` (detach).

```
Hive — myproject                           3 workers / 10 max

 ISSUE              STATUS        AGENT         ELAPSED
 w-abc: Add auth    in_progress   worker-001    1m 23s
 w-def: Write tests blocked       —             —
 w-ghi: Add logging in_progress   worker-002    0m 45s

 MERGE QUEUE: 0 queued, 0 running, 2 merged

 [Recent events stream here]
```

Single biggest UX win — users see the system working in real time without learning any additional commands. Rich would work well here. Keep it simple!

---

## 9. "When Should I Use Hive vs Claude Code?"

**Problem:** Users need to know when Hive adds value. This framing is absent.

**Recommendation:** Add a section near the top of README:

| Task                                | Just use Claude Code | Use Hive                 |
| ----------------------------------- | -------------------- | ------------------------ |
| Fix a bug                           | Yes                  | —                        |
| Add a single feature                | Yes                  | —                        |
| Add auth + tests + docs + migration | —                    | Yes (4 parallel workers) |
| Refactor 3 modules simultaneously   | —                    | Yes (3 parallel workers) |
| Implement a full feature spec       | —                    | Yes (Queen decomposes)   |

Rule of thumb: if you'd break it into subtasks anyway, let Hive do it in parallel.

---

## 10. Cost Visibility Upfront

**Problem:** Users starting with `HIVE_MAX_AGENTS=10` may not realize what 10 concurrent Sonnet sessions cost. No warning, no estimate.

**Recommendation:** During `hive setup` or before first `hive start`, show a brief cost model:

```
You're about to start 10 concurrent Claude Sonnet workers.
With Claude Max subscription, this uses your included credits.
Estimated: ~$0.50-2.00 per issue (varies by complexity).

Tip: Start with --max-agents 3 to test the workflow first.
```
