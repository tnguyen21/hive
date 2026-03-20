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

