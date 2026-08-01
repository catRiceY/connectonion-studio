---
name: commit
title: Git Commit
description: Create well-formed git commits with clear, conventional messages from
  the current changes. Use when the user says "commit", "commit this", "make a commit",
  or asks to save/record staged or unstaged changes to git.
category: version-control
tools:
- Bash(git status *)
- Bash(git diff *)
- Bash(git log *)
- Bash(git add *)
- Bash(git commit *)
- read_file
---

# Git Commit Skill

Produce a clean, well-scoped commit with a message that matches the repository's existing style.

## Instructions

1. **Survey the working tree.** Run in parallel:
   - `git status --short` — see what is staged vs unstaged vs untracked.
   - `git diff` and `git diff --staged` — read the actual changes.
   - `git log --oneline -10` — learn the repo's message conventions (Conventional Commits? scopes? language? emoji?).

2. **Decide what goes in.** If nothing is staged, stage the changes that form one coherent logical unit with `git add <path>` (avoid `git add -A` unless everything truly belongs together). If the diff mixes unrelated concerns, either stage only the relevant files or tell the user it should be split into multiple commits.

3. **Sanity-check the diff before writing the message.** Confirm no debug prints, secrets, large binaries, or stray files (e.g. `.env`, `node_modules`, build artifacts) are being committed. If found, unstage them and warn the user.

4. **Draft the message** matching the observed convention:
   - Subject line: imperative mood, <=50-72 chars, no trailing period. Prefer `type(scope): summary` (feat, fix, docs, refactor, test, chore) if the repo already uses it.
   - Body (optional): wrap at ~72 cols. Explain *why*, not just *what*, when the change is non-obvious. Reference issues/tickets if the repo does.
   - Match the repo's language (e.g. if recent commits are in Chinese, mirror that).

5. **Commit.** Use a single command with explicit `-m` flags so the message is exactly as drafted:
   `git commit -m "subject" -m "body..."`
   Do NOT add co-author trailers or tool advertising unless the user asks. Never pass `--no-verify` unless explicitly requested — let pre-commit hooks run.

6. **Verify.** Run `git status` and `git log --oneline -1` to confirm the commit landed and the tree is in the expected state. If a hook rejected the commit, report the hook output and fix or ask before retrying.

7. **Report** the final subject line and the files included. Do not push unless the user explicitly asks.
