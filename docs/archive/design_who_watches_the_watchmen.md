The value isn't just another layer of indirection — it's persistent domain context. A worker gets a
one-shot prompt and forgets everything. A sub-queen accumulates understanding of its domain across
many issues: which files interact, what the testing conventions are, what the architectural
constraints are, how to decompose work in that area.

Today's Queen is a generalist decomposing at the strategic level. A sub-queen is a domain specialist
decomposing at the tactical level — it knows exactly which files need to change and in what order.

What It Would Look Like

Human ←→ Queen Bee (Opus, strategic)
↓ creates epics, assigns to domains
Sub-Queens (Sonnet, long-lived, domain-specific)
↓ hive CLI access, creates child issues
Daemon → Workers (Sonnet, ephemeral) → Merge
↑
Sub-Queen monitors children, re-plans on failure

The Natural Mapping to Existing Primitives

The interesting thing is that most of the machinery already exists:

┌─────────────────────────┬──────────────────────────────────────────────┐
│ Concept │ Maps To │
├─────────────────────────┼──────────────────────────────────────────────┤
│ Domain assignment │ New domain_id column on issues │
├─────────────────────────┼──────────────────────────────────────────────┤
│ Epic / delegation │ New issue type — parent with children │
├─────────────────────────┼──────────────────────────────────────────────┤
│ Sub-queen session │ Long-lived agent (like refinery already is) │
├─────────────────────────┼──────────────────────────────────────────────┤
│ Sub-queen creating work │ Same hive CLI the Queen already uses │
├─────────────────────────┼──────────────────────────────────────────────┤
│ Progress monitoring │ Same hive status / hive show the Queen uses │
├─────────────────────────┼──────────────────────────────────────────────┤
│ Completion signal │ Epic is done when all children are finalized │
└─────────────────────────┴──────────────────────────────────────────────┘

Data Model Changes

-- New table
CREATE TABLE domains (
id TEXT PRIMARY KEY,
name TEXT NOT NULL, -- "frontend", "api", "data-pipeline"
description TEXT,
file_patterns TEXT, -- glob patterns: "src/web/**,src/components/**"
system_prompt TEXT, -- domain-specific context/conventions
model TEXT -- override model for this domain's sub-queen
);

-- Existing tables, new columns
ALTER TABLE issues ADD COLUMN domain_id TEXT REFERENCES domains(id);
ALTER TABLE issues ADD COLUMN parent_id TEXT REFERENCES issues(id);
ALTER TABLE agents ADD COLUMN agent_type TEXT DEFAULT 'worker';
-- agent_type: worker | sub_queen

Sub-Queen Prompt Structure

The key differentiator from a worker prompt — a sub-queen gets:

1. Domain context: "You own the frontend. Here are the key modules, conventions, test patterns..."
2. CLI access: Same hive create, hive dep add the Queen uses
3. Scoping constraint: "Only create issues within your domain. Escalate cross-domain needs to the
   Queen."
4. Epic objective: The high-level goal from the Queen
5. Accumulated notes: All notes from previous workers in this domain (the notes system already
   supports this)

## Your Domain: {{domain_name}}

{{domain_description}}

### Key Paths

{{file_patterns}}

### Domain Conventions

{{system_prompt}}

## Your Objective

{{epic_description}}

## Instructions

1. Analyze the objective in the context of your domain
2. Decompose into concrete issues using `hive create`
3. Wire dependencies using `hive dep add`
4. Monitor progress using `hive show` / `hive status`
5. When workers fail, assess and retry with better context
6. When all children are finalized, signal epic completion

Orchestrator Changes

The daemon already has the patterns needed:

- Long-lived sessions: The refinery session is already long-lived with eager creation and health
  checks. Sub-queen sessions would follow the same pattern.
- CLI access: Workers are sandboxed away from hive CLI. Sub-queens would need it — a new permission
  tier between worker (no CLI) and Queen (full TUI).
- Epic monitoring: When all child issues of an epic reach finalized, the daemon marks the epic done
  and enqueues it (though epics may not need merge — they're coordination artifacts, not code
  artifacts).

The Lifecycle

1. Human: "Rebuild the checkout flow with better error handling"
2. Queen: Creates epic, assigns domain=frontend
3. Daemon: Spawns sub-queen session for frontend domain
4. Sub-Queen: Reads domain context, analyzes epic
5. Sub-Queen: `hive create "Refactor CheckoutForm error states" ...`
6. Sub-Queen: `hive create "Add error boundary to payment step" ...`
7. Sub-Queen: `hive dep add <payment> <checkout>`
8. Daemon: Dispatches child issues to workers as usual
9. Workers: Execute, merge
10. Sub-Queen: Monitors. If worker-3 fails → re-plans, creates replacement issue with failure context
11. All children finalized → Epic done → Sub-queen session freed

Cross-Domain Coordination

This is the hard part. Options:

A. Queen as router (simplest): Sub-queens escalate cross-domain needs back to the Queen, who creates
issues in the other domain. The Queen is the only entity that sees across domains.

B. Shared issues: An issue can be tagged with multiple domains. Both sub-queens see it, but only one
owns it. The other gets notified via events.

C. Interface contracts: Sub-queens can declare "I need X from domain Y" — the Queen or daemon routes
this as a dependency. This is essentially what already happens with issue dependencies, just with
domain awareness.

Option A is where I'd start. It's the simplest and the Queen already has the coordination role.

What This Costs

The honest tradeoffs:

- Latency: Another planning round before workers start. For small requests this is pure overhead.
- Token cost: Sub-queens are long-lived Sonnet sessions — cheaper than Opus but not free.
- Complexity: New entity type, new lifecycle, new failure modes (what if the sub-queen creates bad
  decompositions?).
- When it's NOT worth it: Single-domain changes, small features, bug fixes. The Queen can dispatch
  those directly to workers as it does today.

Incremental Path

Rather than building the full hierarchy upfront:

1. Start with domains as metadata only — add domain_id to issues, let the Queen tag them. No
   sub-queens yet. This gives you domain-aware filtering and analytics.
2. Add epic issue type — parent/child relationships, "epic done when children done" semantics. The
   Queen creates epics and decomposes them directly. This is useful even without sub-queens.
3. Promote epics to sub-queen sessions — when an epic is complex enough (Queen's judgment or heuristic
   like >5 children), spawn a sub-queen to manage it instead of having the Queen micro-manage.
4. Long-lived domain sub-queens — persistent sessions that accumulate domain knowledge across multiple
   epics.

Step 1 and 2 are low-risk and immediately useful. Step 3 is where the real architectural shift
happens. Step 4 is the aspirational end state.

---

The thing I find most compelling about this is that it solves the decomposition quality problem — the
Queen doesn't need to know that CheckoutForm uses a specific state management pattern or that the
payment module has its own test fixtures. The sub-queen does, because it lives in that domain. The
Queen just needs to know "this is a frontend problem."

===

Domain Coordinators: The Missing Middle Layer

What you're describing, precisely

Right now the hierarchy is flat:

Human (Queen) → Orchestrator → Workers

The Queen is the only entity that decomposes work. She creates issues (atomic tasks) or epics (sequential
multi-step tasks), and workers execute them. This works well when:

- The human understands the full codebase
- The tasks are small enough to describe directly
- There aren't many interacting subsystems

For a larger repo, you want:

Human (Queen) → Orchestrator → Domain Coordinators → Workers
↕ (cross-domain deps)

The domain coordinator is an AI agent that decomposes, not executes. It has deep context about its subdomain and turns
vague high-level requests into precise, worker-ready issues.

How it maps onto the existing architecture

The good news: you already have most of the primitives.

Epics are the key. A domain coordinator is essentially an agent whose output is a epic — a set of issues with
parent/child relationships and dependency edges. The coordinator doesn't write code; it writes issues.

Here's the lifecycle:

1. Human says: hive add "Add rate limiting to the API" --domain backend
2. Orchestrator sees domain=backend, routes to the backend coordinator
3. Coordinator agent spins up in a read-only session (no worktree needed, or a shared read-only one)
4. Coordinator explores src/api/, reads existing middleware, understands the patterns
5. Coordinator outputs a decomposition plan as structured JSON — a epic with steps:
   - Step 1: "Create rate limit middleware in src/api/middleware/rate_limit.py" (small, python)
   - Step 2: "Add Redis counter backend in src/api/services/rate_store.py" (small, python)
   - Step 3: "Wire middleware into route handlers in src/api/routes/" (medium, python)
   - Step 4: "Add integration tests for rate limit behavior" (medium, python, test)

6. Orchestrator creates the epic + steps + dependencies in the DB
7. Workers pick them up as normal

Concrete schema changes

Minimal. You'd add:

-- New table: domain definitions
CREATE TABLE IF NOT EXISTS domains (
id TEXT PRIMARY KEY,
name TEXT NOT NULL UNIQUE, -- "backend", "frontend", "auth"
description TEXT, -- what this domain covers
paths TEXT, -- JSON array of glob patterns: ["src/api/**", "src/middleware/**"]
coordinator_prompt TEXT, -- domain-specific system prompt
model TEXT, -- model override for coordinator
created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Add domain column to issues
ALTER TABLE issues ADD COLUMN domain TEXT REFERENCES domains(id);

The coordinator_prompt is the key differentiator — it's where the domain expertise lives. For a backend coordinator:

You are the Backend Domain Coordinator for project ${project}.

Your responsibility: the API layer, database access, middleware, and
background jobs. You know these paths intimately:

- src/api/ — FastAPI route handlers
- src/services/ — business logic layer
- src/models/ — SQLAlchemy models
- tests/api/ — API test patterns

When decomposing work:

- Prefer small, atomic steps (one file or one logical unit per step)
- Always include a test step as the final step
- Note cross-domain dependencies (e.g., "frontend will need to update
  the API client after this lands")
- Use the project's existing patterns — check existing middleware
  before creating new ones

The coordinator agent type

In config.py, alongside WORKER_PERMISSIONS:

COORDINATOR_PERMISSIONS = {
"read": ["**/*"], # Can read everything
"write": [], # Cannot write code
"execute": ["rg", "find"], # Can search
}

A coordinator gets a special prompt (new template prompts/coordinator.md) and a special completion signal. Instead of
.hive-result.jsonl with files_changed, it writes:

{
"status": "success",
"decomposition": [
{
"title": "Create rate limit middleware",
"description": "...",
"priority": 1,
"tags": ["feature", "python", "small"],
"depends_on": []
},
{
"title": "Add Redis counter backend",
"description": "...",
"priority": 1,
"tags": ["feature", "python", "small"],
"depends_on": []
},
{
"title": "Wire middleware into route handlers",
"description": "...",
"priority": 2,
"tags": ["feature", "python", "medium"],
"depends_on": [0, 1]
},
{
"title": "Add integration tests",
"description": "...",
"priority": 2,
"tags": ["test", "python", "medium"],
"depends_on": [2]
}
],
"cross_domain_notes": ["Frontend API client will need updating after this lands"]
}

The orchestrator parses this and creates the epic + steps + dependency edges in one transaction.

The orchestrator changes

In main_loop and spawn_worker, you'd add a routing decision:

async def main_loop(self):
while self.running:
if len(self.active_agents) < Config.MAX_AGENTS:
ready = self.db.get_ready_queue(limit=1)
if ready:
issue = ready[0]

                  # NEW: Does this issue need coordination first?
                  if self._needs_coordinator(issue):
                      await self.spawn_coordinator(issue)
                  else:
                      await self.spawn_worker(issue)

Where \_needs_coordinator checks:

- Issue has a domain set
- Issue type is not already step (steps are worker-ready)
- No existing decomposition (epic children) exists yet

The tricky parts

1. Cross-domain work. A feature like "add user authentication" touches backend (API routes), frontend (login form), and
   infra (JWT secret management). Options:

- The Queen decomposes into domain-scoped sub-issues manually
- A "meta-coordinator" (your queen-like agent) does the first split, then domain coordinators refine
- Allow coordinators to flag cross-domain dependencies, and a separate reconciliation step wires them

2. Coordinator context window. A coordinator needs to understand its subdomain. For large repos, even reading
   src/api/\*\* might exceed context. You'd want:

- A pre-computed domain summary (auto-generated, cached) — basically a condensed "here's what's in this domain"
- Smart file selection: coordinator starts with the directory tree + key files, drills in as needed
- This is where the vector notes design (design_vector_notes.md) becomes relevant — semantic search over the domain

3. When NOT to coordinate. Small tasks ("fix typo in error message") shouldn't go through a coordinator. You'd want a
   complexity heuristic — maybe based on tags, or the Queen explicitly marks issues as needs_decomposition.

4. Coordinator failure. If a coordinator produces a bad decomposition, you burn N worker sessions before discovering
   the plan was wrong. Mitigation: the coordinator's decomposition could go through a human approval gate (optional), or
   workers could signal "this step description doesn't match reality" via notes.

What I'd actually build first

Don't build the full domain system yet. Build the coordinator as a simpler concept first: an agent whose job is to take
a vague issue and produce a epic.

hive add "Improve error handling across the API" --coordinate

This creates an issue with type=coordinate. The orchestrator spawns a coordinator agent (using the same OpenCode
infra), which explores the codebase, produces a structured decomposition, and the orchestrator creates the epic. No
domain registry needed yet — the coordinator just explores whatever is relevant.

This gets you 80% of the value with 20% of the complexity. Domains become a natural refinement later: "instead of
exploring the whole repo, here's your scope."

Summary

Concept: Parent/child issues
Already Exists: parent_id column
Needs Building: -
────────────────────────────────────────
Concept: Sequential multi-step
Already Exists: Epics + cycle_agent_to_next_step
Needs Building: -
────────────────────────────────────────
Concept: Dependency DAG
Already Exists: dependencies table
Needs Building: -
────────────────────────────────────────
Concept: Agent types
Already Exists: Worker + Refinery
Needs Building: Coordinator agent type
────────────────────────────────────────
Concept: Domain-scoped prompts
Already Exists: -
Needs Building: domains table + coordinator prompt template
────────────────────────────────────────
Concept: Decomposition output
Already Exists: -
Needs Building: Structured completion signal for coordinators
────────────────────────────────────────
Concept: Routing logic
Already Exists: get_ready_queue filters epics
Needs Building: \_needs_coordinator check
────────────────────────────────────────
Concept: Cross-domain deps
Already Exists: -
Needs Building: Coordinator cross_domain_notes → future issue linking

The architecture is remarkably ready for this. The epic primitive was the hard part, and it's already there.
