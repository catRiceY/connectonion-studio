---
name: upgrade-dependencies
title: Upgrade Dependencies
description: Safely bump project dependencies, run the test suite, and resolve any
  breakages. Use when the user asks to upgrade, update, or bump dependencies, or to
  fix outdated or vulnerable packages.
category: delivery-ops
tools:
- read_file
- edit_file
- glob
- grep
- list_files
- Bash(git status *)
- Bash(git diff *)
- Bash(git checkout *)
- Bash(npm *)
- Bash(pip *)
- Bash(pytest *)
- Bash(go *)
---

# Upgrade Dependencies

Bump dependencies safely: upgrade in controlled batches, run tests after each, and fix breakages before moving on.

## Instructions

1. Establish a clean baseline. Run `git status` and confirm the tree is clean (or stash). Run the full test suite first and record that it passes - never start an upgrade against a red suite.

2. Survey what is outdated. Use the ecosystem's report command:
   - Node: `npm outdated` (and `npm audit` for vulnerabilities)
   - Python: `pip list --outdated`
   - Go: `go list -m -u all`
   Read the manifest and lockfile to see current version constraints.

3. Classify each upgrade by semver:
   - **patch/minor** (backward-compatible): safe to batch together.
   - **major** (breaking): handle one at a time, and read its changelog/migration guide before bumping.
   Prioritize security fixes from the audit report.

4. Upgrade in small batches, committing between them so each step is revertible:
   - Apply patch and minor bumps first as one batch. Update the manifest, refresh the lockfile, install, then run the full test suite.
   - If green, commit. If red, go to step 6.

5. Handle major upgrades individually. Bump one package, refresh the lockfile, install, and run tests. Apply the documented migration (renamed APIs, changed defaults, dropped options) using `grep` to find every call site. Commit once green before starting the next major.

6. When tests break, diagnose before mass-editing:
   - Read the failure output and the package's changelog to find the exact breaking change.
   - Fix call sites to the new API; do NOT silence tests or loosen assertions to make them pass.
   - Re-run the suite. If a single upgrade proves too costly, revert just that package with `git checkout` and report it as needing manual attention.

7. Final verification. With all batches applied, run the full suite once more, plus lint/build if defined, and `npm audit` / equivalent to confirm known vulnerabilities are cleared.

8. Report a summary: packages bumped (old -> new version), any majors and the migrations applied, remaining vulnerabilities, and any upgrade you deliberately skipped and why.

## Checks
- Every batch was validated by a green test run before the next.
- Lockfile is updated and committed alongside the manifest.
- No test was weakened or skipped to force a pass.
- Skipped/reverted upgrades are called out explicitly.
