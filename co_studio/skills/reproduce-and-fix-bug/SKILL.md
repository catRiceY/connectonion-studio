---
name: reproduce-and-fix-bug
title: Reproduce and Fix Bug
description: Turn a bug report into an automated failing test that reproduces it,
  then make that test pass with the smallest correct fix. Use when the user reports
  a bug, pastes a stack trace or error message, or says "reproduce this", "write a
  failing test", "fix this bug", "why does X break", or "this returns the wrong result".
category: debug-understand
tools:
- read_file
- glob
- grep
- write_file
- edit_file
- Bash(git diff *)
- Bash(git log *)
- Bash(pytest *)
- Bash(npm test *)
- Bash(go test *)
- Bash(cargo test *)
---

# Reproduce and Fix Bug

Reproduce the bug as a failing test first, then fix it. A test that fails before and passes after is the proof — never skip it.

## Step 1: Extract the reproduction

From the report, pin down:
- Exact inputs / steps and the environment.
- Expected behavior vs actual (error, wrong value, crash).
- Any stack trace — note the failing file, function, and line.
If critical details are missing, state your assumptions explicitly and proceed with the most likely case.

## Step 2: Locate the code path

- `grep -rn` the error message, symbols, and frames from the trace to find the involved functions.
- Read the implicated code and its callers; form a hypothesis about the fault.

## Step 3: Learn the test harness

- `glob` for existing tests (`test_*`, `*_test.*`, `*.spec.*`) near the affected code.
- Read one to copy conventions (fixtures, naming, assertions).
- Confirm how tests run and that the suite is green now: `pytest -q` / `npm test` / `go test ./...` / `cargo test`.

## Step 4: Write a failing test

- Add a focused test that encodes the *expected* behavior for the reported input.
- Run just that test and confirm it FAILS — and fails for the real reason (asserts on the bug), not a setup/import error.
- If it passes unexpectedly, your reproduction is wrong; refine inputs until it fails as reported.

## Step 5: Diagnose the root cause

- Trace from the failing assertion to the origin; distinguish the root cause from its symptom.
- Avoid fixing at the surface if the defect is upstream.

## Step 6: Apply the minimal fix

- Use `edit_file` for the smallest change that corrects the root cause.
- Do not reformat unrelated code or bundle in extra changes.

## Step 7: Verify

- Re-run the new test — it must PASS.
- Run the surrounding suite to catch regressions: full module or `pytest -q` / `npm test` / `go test ./...`.
- Consider adjacent edge cases (empty, null, boundary, negative) and add a case if warranted.

## Step 8: Report

- Show `git diff` of the fix plus the new test.
- State the root cause in one sentence and why the change resolves it.
- Note any remaining risks or follow-ups. Do not commit unless asked.
