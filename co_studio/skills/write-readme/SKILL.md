---
name: write-readme
title: Write README
description: Author or refresh a clear, accurate README from the actual code — what
  the project is, why it exists, how to install it, how to use it, and how to develop
  on it. Use when the user says "write a README", "update the README", "document this
  project", or a repo has no usable top-level README.
category: docs-security
tools:
- read_file
- glob
- grep
- list_files
- write_file
- edit_file
- Bash(cat *)
- Bash(ls *)
---

# Write README Skill

Produce a README that reflects what the code actually does — never invent features, commands, or badges.

## Step 1: Identify the project

Read the manifest to learn name, purpose, entry points, and dependencies:
- `glob("{package.json,pyproject.toml,setup.py,Cargo.toml,go.mod,composer.json,Gemfile}")`
- Read the first match; capture project name, version, description, scripts, and dependencies.
- `list_files(".")` to see top-level layout (src/, bin/, docs/, examples/).

## Step 2: Discover how it is used

- `grep` for entry points: CLI (`bin`, `console_scripts`, `main`), library exports (`__all__`, `index.*`), or a server (`app.listen`, `uvicorn`, `flask run`).
- Read any `examples/`, `docs/`, or existing README to reuse still-accurate content.
- Note the real install command from the manifest (npm/pip/cargo), not a guessed one.

## Step 3: Check for an existing README

- If `README*` exists, read it and preserve accurate sections; only rewrite stale or missing parts.
- Preserve existing badges, license, and links unless clearly wrong.

## Step 4: Write the README

Write `README.md` with these sections, omitting any that do not apply:
1. **Title + one-line description** — what it is, in one sentence.
2. **Why / features** — the problem it solves; 3–6 concrete bullets.
3. **Install** — exact commands verified against the manifest.
4. **Usage** — a minimal runnable example (copy the smallest real example you found).
5. **Configuration** — required env vars / flags, if any (grep for `process.env`, `os.getenv`, config loaders).
6. **Development** — how to set up, run tests, and build (pull real scripts from the manifest).
7. **License** — from the manifest or LICENSE file.

## Step 5: Verify accuracy

- Every command shown must exist as a real script/entry point — cross-check against Step 1–2.
- Do NOT claim test coverage, CI, or badges that are not present in the repo.
- Keep prose tight; prefer fenced code blocks with language tags over long paragraphs.

## Rules

- Never fabricate install steps, APIs, or example output.
- Match the project's own terminology and casing for the name.
- If a fact cannot be confirmed from the code, leave it out or mark it with a clear TODO.
