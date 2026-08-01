---
name: git-bisect-bug
title: Git Bisect Bug Hunt
description: Find the exact commit that introduced a bug or regression using git bisect,
  automated with a test command when possible. Use when the user says "find the commit
  that broke X", "this used to work", "bisect this regression", or wants to pinpoint
  when a bug was introduced.
category: version-control
tools:
- Bash(git bisect *)
- Bash(git status *)
- Bash(git log *)
- Bash(git checkout *)
- Bash(git show *)
- read_file
---

# Git Bisect Bug Hunt Skill

Binary-search git history to identify the commit that introduced a defect.

## Instructions

1. **Define good and bad precisely.** Pin down a reproducible check that returns pass/fail for the bug. Confirm the tree is clean first (`git status`) and stash or commit any local changes — bisect checks out old commits and will refuse or lose uncommitted work.

2. **Establish endpoints.**
   - BAD: a commit where the bug is present (usually `HEAD`). Verify the reproduction actually fails there.
   - GOOD: a commit/tag where the behavior was correct. Use `git log --oneline` or a known-good tag/date. Verify the reproduction passes there before trusting it — a wrong "good" invalidates the whole search.

3. **Start the session:**
   ```
   git bisect start
   git bisect bad <bad-rev>
   git bisect good <good-rev>
   ```
   Git checks out the midpoint and reports how many revisions/steps remain.

4. **Prefer automation.** Write a one-shot check script that exits `0` when good and non-zero (1-124, excluding 125) when bad. Use exit code `125` to signal "cannot test / skip this commit" (e.g. it won't build). Then run:
   ```
   git bisect run <command>
   ```
   Examples: `git bisect run ./repro.sh`, `git bisect run npm test -- path/to/spec`, `git bisect run pytest tests/test_x.py::test_y`. Make the command test ONLY the target bug so unrelated failures don't mislead the search.

5. **Or step manually** when no scriptable check exists: at each checkout, reproduce the issue, then run `git bisect good` or `git bisect bad`. Use `git bisect skip` for untestable commits.

6. **Read the culprit.** Git prints `<sha> is the first bad commit`. Inspect it with `git show <sha>` and `git log -1 <sha>` — read the diff to confirm it plausibly causes the bug (not just a coincidental neighbor). Watch for large/merge commits that may need a follow-up narrower bisect.

7. **Clean up.** Always run `git bisect reset` to return to the original branch/HEAD. Restore any stash. Confirm `git status` matches the starting state.

8. **Report** the offending commit SHA, its subject/author/date, the relevant hunk of the diff, and a suggested fix or next step.
