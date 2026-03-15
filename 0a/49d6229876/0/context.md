# Session Context

## User Prompts

### Prompt 1

Implement the following plan:

# Plan: Add Mutation Testing to Hive

## Context

Hive has a solid test suite (19 files, pytest + xdist parallel, async support) but no way to measure test *quality*. Mutation testing systematically introduces small code changes (mutants) and checks whether tests catch them. Surviving mutants reveal weak assertions, missing edge cases, and undertested branches. The recent refactoring pass has stabilized the codebase — good time to add this before new feature wor...

### Prompt 2

update the README with how to run mutation tests

### Prompt 3

sweet; let's merge this into main

### Prompt 4

can we add ci/cd to this add have mutation testing be a part of it? no need to have it run every time we do things locally, though

