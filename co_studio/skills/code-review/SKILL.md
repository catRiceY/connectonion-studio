---
name: code-review
title: Code Review
description: Review a code diff or pull request for correctness, security, and style,
  reporting findings ranked by severity with concrete failure scenarios and fixes.
  Use when the user says "review this", "code review", "look over my PR", or asks
  to check a diff before merging.
category: code-quality
tools:
- Bash(git status)
- Bash(git diff *)
- Bash(git log *)
- Bash(gh pr view *)
- Bash(gh pr diff *)
- read_file
- glob
- grep
---

# Code Review Skill

Review a diff or pull request for correctness, security, and style. Report findings ranked by severity, each with a concrete failure scenario and a suggested fix.

## Instructions

### 1. Gather the change
- If reviewing a PR: `gh pr view <n>` for intent, `gh pr diff <n>` for the diff.
- Otherwise diff the working branch: `git status`, then `git diff main...HEAD` (or `git diff --staged`).
- `git log --oneline -10` for surrounding context.
- Note the scope: which files, roughly how many lines, what subsystem.

### 2. Build context beyond the hunk
- read_file each changed file in full — a diff hunk hides callers, invariants, and error paths.
- grep for callers of changed functions to see how new behavior propagates.
- Identify the language, framework, and existing conventions so style notes match the repo.

### 3. Review for correctness
- Trace edge cases: empty/null inputs, boundary values, concurrency, error and exception paths.
- Look for off-by-one errors, resource leaks (unclosed files/handles), and unchecked return values.
- Confirm the logic matches the stated intent and that existing tests still cover it.

### 4. Review for security
- Untrusted input reaching SQL, shell, eval, or file paths without validation or parameterization.
- Secrets or credentials committed; sensitive data written to logs.
- Authz/authn gaps, unsafe deserialization, injection, and path traversal.

### 5. Review for style and maintainability
- Naming, dead code, duplicated logic, oversized functions.
- Missing or misleading tests, comments, and docs.
- Match the repo's existing conventions; do not impose personal preference.

### 6. Report findings
- Group by severity: blocker, major, minor, nit.
- For each: `file:line`, the concrete problem, a failure scenario (inputs -> wrong result), and a suggested fix.
- Verify each claim against the code before reporting; drop anything you cannot substantiate.
- If nothing is wrong, say so explicitly rather than inventing nits.
