# Session Context

## User Prompts

### Prompt 1

can you read @../mission-control/SPEC-dispatch.md 

so, we're moving up the abstraction stack and i'm thinking of having a queen + refinery available per project. for certain kinds of tasks, i think it'd make way more sense to quickly delegate and merge the tasks. a lot of development already, in fact, has been done this way (duh look at this repo's git history), so there's no reason not to move up this level of management and let me be a little more removed from certain details.

what do you...

### Prompt 2

hrm, well i'm still somewhat leaning towards a per-project queen and refinery, specifically because i foresee myself quickly growing scope and dispatching feature development, or perf work, or whatever via this headless dispatch, non-interactive method as well. we'll add more automated checks and guardrails of course, but this enables us to strive for modestly high quality code.

like my thinking here is you want per-project context/memory so that the project's queen/refinery has more local c...

### Prompt 3

yeah can you plan what 1+2 would look like. don't make any changes yet; let me review before you start implementing

### Prompt 4

seems reasonable; implement

### Prompt 5

merge into main; change seems reasonable

### Prompt 6

wait prior to that -- do we have per project refineries and queens? more importantly, can we think of how we can make these sessions more effective at their roles (e.g. queen, getting tasks, finding relevant files, planning out changes at a higher level; the refinery, updating context on how to build + test, and project/dependency specific gotchas and patterns that should be enforced)

### Prompt 7

this is pretty reasonable. i think we should start an LLM instance that searches the repo and has some guidelines for figuring out guidelines, conventions, idioms, and mapping out the repo. maybe lik ean init-prompt.md or something like that. it should encourage the use of some static analysis tools too, during the init. this might be a bit heavy handed at first, so maybe we just start with a high level `tree` view of the source?

i'm still thinking about the convention of layered agents.md f...

### Prompt 8

yes this is reasonable as a first step to getting per project context that gets better over time

### Prompt 9

hive init --analyze
/Users/tau/projects/hive/.hive.toml already exists.
╭───────────────────────────────── Traceback (most recent call last) ──────────────────────────────────╮
│ /Users/tau/.local/share/uv/tools/hive/lib/python3.14/site-packages/hive/config.py:183 in __getattr__ │
│                                                                                                      │
│   180 │   │   # Delegate attribute access to the global config for backward compat.                  │
│   1...

### Prompt 10

ok, so on the topic of a headless dispatch mode -- i'm not convinced we need an entirely new CLI command and API for this

why shouldn't we use the existing hive create <issue> command?

another agent could use the --help flag to figure out the right syntax

the biggest reason i can think of is that we need a queen instance to spin up with per-project context, do a file system search, and then also get auto-approval permission so it will create that issue by itself.

is this something reasona...

