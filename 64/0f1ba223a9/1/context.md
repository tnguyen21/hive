# Session Context

## User Prompts

### Prompt 1

Implement the following plan:

# Plan: Retry Counter Reset via Watermark Event

## Context

When an issue repeatedly fails/escalates, the retry counter (computed by counting `retry`, `agent_switch`, and `incomplete` events) prevents it from being retried further. After fixing the underlying cause, there's no CLI way to reset these counters — users must manually delete rows in SQLite. We'll add a "watermark" event (`retry_reset`) that resets the counters without destroying the audit trail.

##...

### Prompt 2

can we do a hard re-install of hive on the system; seems like i can't pull in this update. issues still being esclated and reseting

### Prompt 3

[Request interrupted by user]

### Prompt 4

not reseting

### Prompt 5

Needs attention (2):
  w-9640ff3de673   [escalated] Wire briefing generation into game flow
  w-09ccf79f8172   [escalated] Instrument game.ts with structured loggi

poke at these two issues; they're still not being opened and in-progress properly

