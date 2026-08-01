---
name: changelog
title: Update Changelog
description: Generate or update a CHANGELOG entry from git history since the last
  release, grouped by change type. Use when the user says "update the changelog",
  "add a changelog entry", "generate release notes", or is cutting a release and needs
  the CHANGELOG filled in.
category: version-control
tools:
- Bash(git log *)
- Bash(git tag *)
- Bash(git describe *)
- Bash(git diff *)
- read_file
- edit_file
- grep
---

# Changelog Skill

Turn commits since the last release into a human-readable CHANGELOG entry that matches the file's existing format.

## Instructions

1. **Locate the changelog.** Look for `CHANGELOG.md`, `docs/CHANGELOG.md`, `HISTORY.md`, or `NEWS`. Read the top of it to learn the exact format: heading style (`## X.Y.Z — date`), grouping (Added/Changed/Fixed/Removed vs custom sections), ordering (newest-first is standard), and language. Reproduce that style — do not impose "Keep a Changelog" if the repo uses something else.

2. **Find the range.** Determine the last released version:
   - `git describe --tags --abbrev=0` for the most recent tag, or read the topmost version header in the changelog.
   - Collect commits since then: `git log <last-tag>..HEAD --no-merges --pretty=format:"%h %s"`.
   - If there is no tag yet, use the last version header's date or the full history.

3. **Determine the new version.** Ask the user if unspecified, or infer from semver: breaking changes → major, new features (`feat`) → minor, fixes only (`fix`/`chore`) → patch. Note the project may have multiple independent release tracks — check RELEASING docs if present and confirm which track.

4. **Classify each commit.** Map commit subjects to changelog sections:
   - `feat` → Added / Features; `fix` → Fixed; `perf`/`refactor` → Changed; `BREAKING CHANGE` → a called-out Breaking section; `docs`/`chore`/`test`/`ci` → usually omit unless the repo lists them.
   - Skip merge commits, version-bump commits, and pure-internal noise.

5. **Rewrite for humans.** Each bullet should read as a user-facing outcome, not a raw commit subject. Strip the type prefix, use past-tense or noun phrases consistent with existing entries, and merge duplicates. Add issue/PR references (`(#123)`) if the repo does.

6. **Insert the entry** at the top (below any header/intro block) with `edit_file`. Include the version and today's date in the repo's exact header format. Preserve the `---` separators, spacing, and any "Unreleased" section conventions.

7. **Verify.** Re-read the edited region to confirm formatting is intact and no existing entries were clobbered. Report the new version header and bullet count; do not commit or tag unless asked.
