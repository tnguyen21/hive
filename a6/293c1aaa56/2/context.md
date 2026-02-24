# Session Context

## User Prompts

### Prompt 1

critical error; somehow we can create worktrees/agents for work when we flood the orchestrator with a bunch of issues at once. e.g. i created 18 issues and now we hav e50 agents. what?

please help me fix this

### Prompt 2

[Request interrupted by user for tool use]

### Prompt 3

import sys, json
      d = json.load(sys.stdin)
      print(f'Total working agents: {len(d[\"agents\"])}')
      from collections import Counter
      issues = Counter(a.get('current_issue_title','?') for a in d['agents'])
      for title, count in issues.most_common():
          print(f'  {count}x {title}')
      ")
  ⎿  Total working agents: 54
       3x Client photos — placeholder gallery
       3x Tech mobile UX — personal schedule view
       3x Reports and analytics dashboard
      ...

### Prompt 4

[Request interrupted by user]

### Prompt 5

keep going

### Prompt 6

}

⏺ Issues #5 and #6 are open (waiting on their dependency). The other 4 must have been picked up already. Let me check.

⏺ Bash(hive --json show w-093d2b1a6898 2>&1 | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'{d[\"id\"]}: {d[\"title\"]} —
      {d[\"status\"]}' + (f' (agent:…)
  ⎿  Error: Exit code 1
     Traceback (most recent call last):
       File "<string>", line 1, in <module>
     KeyError: 'id'

     Traceback (most recent call last):
       File "<string>...

### Prompt 7

[Request interrupted by user]

### Prompt 8

maybe not creating just whaever this error is.

