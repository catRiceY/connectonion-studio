---
name: write-unit-tests
title: Write Unit Tests
description: Add focused unit tests for a target module, covering the happy path and
  edge cases, then run them to green using the repo's existing framework. Use when
  the user says "write tests", "add unit tests", "cover this module with tests", or
  points at code that lacks test coverage.
category: code-quality
tools:
- read_file
- glob
- grep
- list_files
- write_file
- edit_file
- Bash(pytest *)
- Bash(npm test *)
- Bash(go test *)
- Bash(cargo test *)
---

# Write Unit Tests Skill

Add focused unit tests for a target module, covering the happy path and edge cases, then run them to green.

## Instructions

### 1. Locate the target and framework
- read_file the target module fully; list its public functions/classes and their contracts.
- Detect the test framework and layout: glob for existing tests (`*test*`, `*spec*`) and config (pytest.ini, jest.config, go.mod, Cargo.toml).
- Mirror the existing test file naming and directory convention exactly.

### 2. Map behaviors to test
- For each public unit, enumerate: normal inputs, boundary values, empty/null, invalid input that should raise or error, and documented side effects.
- Note external dependencies (I/O, network, clock, randomness) that need stubbing or mocking.

### 3. Write the tests
- One behavior per test; name each test after the behavior it asserts.
- Follow arrange-act-assert; keep fixtures small and local.
- Cover edge cases explicitly: off-by-one, overflow, unicode/empty strings, and concurrent access where relevant.
- Assert on observable behavior and return values, not private internals.
- Never weaken an assertion just to make a test pass.

### 4. Run and iterate
- Run only the new tests first, e.g. `pytest path -k Name`, `npm test -- <file>`, `go test ./pkg -run Name`, `cargo test name`.
- On failure, decide: is it a real bug in the target or a wrong expectation? Fix wrong expectations; report suspected product bugs instead of papering over them.
- Run the full suite once to confirm no regressions.

### 5. Verify coverage of intent
- Confirm every behavior enumerated in step 2 now has a test.
- Prefer a few meaningful cases over many trivial ones.
- Report which behaviors are covered and any that remain untestable without refactoring.
