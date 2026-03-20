# Session Context

## User Prompts

### Prompt 1

Implement the following plan:

# Plan: Centralize SQL into the DB layer

## Context

The CLI (`cli/core.py`, `cli/global_status.py`) and diagnostics (`diag.py`) bypass the Database class and run raw SQL via `self.db.conn.execute(...)`. This creates two problems:

1. **Duplication**: The same queries appear in 2-3 places (status counts, running merge, escalated issues, merge queue joins)
2. **Leaky abstraction**: The CLI knows about table schemas and JOIN patterns it shouldn't need to

~180 li...

### Prompt 2

[Request interrupted by user]

### Prompt 3

keep going, this is a reasonable plan

### Prompt 4

wait do tests pasts? shouldn't users across orchestrator/cli/etc now call these fns

### Prompt 5

would you merge this work? we save 2 lines. cleaning up the sql is good but hrm

### Prompt 6

eh whatever this is fine as is; merge into main

### Prompt 7

This session is being continued from a previous conversation that ran out of context. The summary below covers the earlier portion of the conversation.

Summary:
1. Primary Request and Intent:
   The user asked to implement a plan to centralize SQL queries out of the CLI layer (`cli/core.py`, `cli/global_status.py`) and diagnostics (`diag.py`) into the DB layer (`db/core.py`, `db/issues.py`). The goals were: eliminate duplicated SQL queries that appeared in 2-3 places, establish a clean abstr...

### Prompt 8

can you sweep through src with some agents and see if we have any opportunities to reduce complexity/LOC

things we might be able to consolidate or simplify?

### Prompt 9

can you do all tier 1 changes; dispatch them to the hive. draft issues first and let me review

### Prompt 10

approve; dispatch to hive

### Prompt 11

can you run tests and see if all of these got merged in reasonable

### Prompt 12

okay there are some moderate effort things you pointed out

  │                                    Opportunity                                     │               Where                │  ~LOC saved   │
  ├────────────────────────────────────────────────────────────────────────────────────┼────────────────────────────────────┼───────────────┤
  │ CLI worker enrichment loop (identical agent→issue_title loop in 4 places)          │ cli/core.py, global_status.py      │ ~40           │
  ├────────...

### Prompt 13

This session is being continued from a previous conversation that ran out of context. The summary below covers the earlier portion of the conversation.

Summary:
1. Primary Request and Intent:
   The user asked for a comprehensive complexity/LOC reduction sweep of the `src/hive/` codebase. The workflow was:
   - First: sweep the codebase with agents to identify consolidation opportunities
   - Second: execute all Tier 1 (quick win) changes by dispatching them as hive issues to be worked by au...

### Prompt 14

watch the hive more carefully; we didn't give as much detail in these issue descriptions so we should be careful in reviewing their diffs even after the refinery merges them

### Prompt 15

yeah just do it here rather than delegate to the hive

### Prompt 16

commit please

### Prompt 17

can we use vulture and see if it sniffs out any other dead code too?

### Prompt 18

commit

