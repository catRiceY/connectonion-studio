---
name: setup-ci
title: Setup CI
description: Add a CI pipeline that installs dependencies, lints, and runs tests on
  every push and pull request. Use when the user asks to set up CI, add a CI pipeline,
  or add a GitHub Actions / GitLab CI workflow.
category: delivery-ops
tools:
- read_file
- write_file
- glob
- grep
- list_files
- Bash(git remote *)
- Bash(git config *)
---

# Setup CI

Add a continuous-integration pipeline that installs, lints, and tests the project on push and pull request.

## Instructions

1. Identify the CI provider. Check the git remote (`git remote -v`):
   - `github.com` -> GitHub Actions at `.github/workflows/ci.yml`
   - `gitlab.com` -> `.gitlab-ci.yml`
   If ambiguous, default to GitHub Actions and tell the user.

2. Detect the stack and its commands. Read the manifest (`package.json` scripts, `pyproject.toml`, `Makefile`, `go.mod`, etc.) to find the real install, lint, and test commands. Do NOT invent commands - use the ones already defined. Note the runtime version to pin.

3. Check for an existing pipeline first. If a workflow file already exists, read it and extend rather than overwrite; ask before replacing.

4. Write the workflow with these stages, each failing the build on error:
   - **checkout** the repo.
   - **setup** the language runtime, pinned to the project's version, with dependency caching keyed on the lockfile.
   - **install** using the lockfile-honoring command (`npm ci`, `pip install -r requirements.txt`, `go mod download`).
   - **lint** (e.g. `eslint`, `ruff check`, `golangci-lint`, `cargo clippy`). If no linter is configured, note it and skip rather than fabricate one.
   - **test** (the project's real test command).

5. Set sensible triggers: run on `push` to the default branch and on `pull_request`. If a test matrix makes sense (multiple language versions or OSes), add a small one.

6. Keep it least-privilege and reproducible:
   - Pin action versions to a major tag (e.g. `actions/checkout@v4`), not a floating branch.
   - Set minimal `permissions:` (usually `contents: read`).
   - Enable dependency caching to keep runs fast.

7. Validate the YAML locally (indentation, keys). If `act` or the provider CLI is available, do a dry parse; otherwise re-read the file and sanity-check structure.

8. Report the file path, what each stage runs, and the trigger events. Remind the user to push the branch and confirm the first run goes green, adjusting any command that fails.

## Checks
- Install uses the lockfile (reproducible), not a loose install.
- Lint and test each independently fail the pipeline.
- Action/image versions are pinned.
- Only commands that actually exist in the project are referenced.
