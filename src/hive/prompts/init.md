You are analyzing a software project to generate structured context for autonomous
coding agents. Your output will be read by workers (who implement tasks), a refinery
(who reviews and merges branches), and a queen (who decomposes work into issues).

Write your findings to `.hive/project-context.md` using the exact structure below.

## ANALYSIS STEPS

### 1. High-level structure

Run `tree -L 3 -I 'node_modules|.venv|venv|__pycache__|.git|dist|build|.mypy_cache|.ruff_cache|.pytest_cache|*.egg-info|.tox|.worktrees' --dirsfirst` to get the project layout. If the project is large, use `-L 2` first and go deeper into key directories.

### 2. Build system and tooling

Read the build/config files to understand the project's toolchain:
- Python: `pyproject.toml`, `setup.cfg`, `setup.py`, `requirements*.txt`
- JavaScript/TypeScript: `package.json`, `tsconfig.json`
- Rust: `Cargo.toml`
- Go: `go.mod`
- General: `Makefile`, `justfile`, `Dockerfile`, `.github/workflows/`

From these, determine:
- Language(s) and version constraints
- Package manager and install command
- Build command (if applicable)
- Test command (exact invocation)
- Lint/format commands
- Any pre-commit hooks or CI checks

### 3. Architecture and module boundaries

Read entry points (main, CLI, app factory) and key module `__init__.py` files.
Identify:
- What the project does (one sentence)
- Major modules/packages and their responsibilities
- Key abstractions (core classes, interfaces, protocols)
- Data flow: where does input come in, how does it get processed, where does output go?

### 4. Conventions and idioms

Look for patterns in the existing code:
- Naming conventions (snake_case, camelCase, file naming)
- Error handling patterns (exceptions, Result types, error codes)
- Test organization (mirrored structure? flat? fixtures?)
- Import style (absolute, relative, specific patterns)
- Any project-specific idioms visible in 2-3 representative files

Read the existing `CLAUDE.md` or `CONTRIBUTING.md` if present — these contain
explicit conventions the project author has already documented.

### 5. Static analysis (if available)

If the project has linting/type-checking configured, note the tools and their
config. Don't run a full analysis — just identify what's set up:
- Linter (ruff, eslint, clippy, golangci-lint)
- Type checker (mypy, pyright/ty, tsc)
- Formatter (ruff format, prettier, rustfmt, gofmt)
- Their config files and any notable settings (line length, rule sets, strictness)

### 6. Test landscape

Look at the test directory structure and a few representative test files:
- Test framework (pytest, jest, cargo test, go test)
- Fixture patterns (conftest.py, beforeEach, test helpers)
- Any test quirks (in-memory DB, mock patterns, slow integration tests)
- Approximate coverage (many tests? sparse? well-organized?)

## OUTPUT FORMAT

Write `.hive/project-context.md` with this structure. Be concrete and specific —
agents will use this to make real decisions. Avoid vague statements like "follows
best practices." If a section doesn't apply, write "N/A" rather than omitting it.

```markdown
# Project Context — <project-name>

## Overview
<One sentence: what this project does>

## Architecture
<2-5 bullet points describing major modules and their responsibilities>
<Data flow summary if non-trivial>

## Key Files
<8-15 most important files an agent should know about, with one-line descriptions>
- `path/to/file.py` — description

## Build & Test
- **Language**: <language and version>
- **Package manager**: <tool and install command>
- **Build**: <command or N/A>
- **Test**: <exact command>
- **Lint**: <command>
- **Format**: <command>
- **Type check**: <command or N/A>
- **Pre-commit**: <what hooks run, or N/A>
- **Quirks**: <anything non-obvious about the build/test process>

## Conventions
<5-10 concrete conventions observed in the codebase>
- <convention 1>
- <convention 2>
...

## Dependencies & Integration
<Key external dependencies and how they're used>
<Any API integrations, databases, external services>

## Gotchas
<Things that would trip up an unfamiliar developer>
<Non-obvious constraints, ordering requirements, platform quirks>
```

## GUIDELINES

- Be specific. "Tests use pytest" is ok. "Tests use pytest with xdist for parallel
  execution and an in-memory SQLite for isolation" is better.
- Focus on what an autonomous agent needs to know to work effectively in this repo.
  Skip history, motivation, and future plans.
- If you find an existing CLAUDE.md, don't duplicate its content — reference it
  and focus on what it doesn't cover (architecture, key files, gotchas).
- Keep the total output under 200 lines. This file gets injected into agent prompts,
  so every line costs tokens. Be dense.
