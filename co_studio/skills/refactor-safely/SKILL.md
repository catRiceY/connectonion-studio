---
name: refactor-safely
title: Refactor Safely
description: Restructure code behind existing tests without changing observable behavior,
  keeping the suite green after every step. Use when the user says "refactor", "clean
  up", "tidy this code", or asks to improve structure without changing what the code
  does.
category: code-quality
tools:
- read_file
- glob
- grep
- list_files
- edit_file
- write_file
- Bash(pytest *)
- Bash(npm test *)
- Bash(go test *)
- Bash(cargo test *)
---

# Refactor Safely Skill

Restructure code behind existing tests without changing observable behavior. The test suite is the contract; it must stay green throughout.

## Instructions

### 1. Establish a green baseline
- Identify and run the tests covering the target first: `pytest`, `npm test`, `go test ./...`, `cargo test`, etc.
- If they are not green before you start, stop and report — never refactor on a red baseline.
- Record the exact run command and current pass count; this is your safety net.

### 2. Assess coverage
- grep and read the tests to confirm they actually exercise the code you plan to change.
- If a behavior you will touch is untested, add a characterization test that pins the current behavior FIRST, then refactor.

### 3. Refactor in small steps
- Make one mechanical change at a time: rename, extract function, inline, dedupe, reorder — never a behavior change.
- Keep public APIs, signatures, and observable output identical unless the task explicitly permits changes.
- Do not mix refactoring with feature work or bug fixes in the same step.

### 4. Re-run tests after each step
- Run the same recorded command; it must stay green with the same pass count.
- If it goes red, revert the last step immediately and retry with a smaller change.

### 5. Final verification
- Run the full test suite, not just the targeted subset.
- Review `git diff` and confirm the change is behavior-preserving: no altered outputs, no changed error semantics, no new public surface.
- Summarize what was restructured and confirm the baseline command still passes.
