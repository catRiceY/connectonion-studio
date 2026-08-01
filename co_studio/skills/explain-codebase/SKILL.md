---
name: explain-codebase
title: Explain Codebase
description: Map an unfamiliar repository from the ground up — locate entry points,
  reconstruct the architecture, and trace how data flows through the layers. Use when
  the user says "explain this codebase", "how does this project work", "give me an
  overview", "where do I start", "walk me through the repo", or asks how a request
  or piece of data moves through the system.
category: debug-understand
tools:
- read_file
- glob
- grep
- list_files
- Bash(git log *)
- Bash(git ls-files *)
---

# Explain Codebase

Build an accurate mental model of an unfamiliar repo: entry points, architecture, and data flow. Read only — do not modify files.

## Step 1: Establish scope and stack

- `list_files` at the repo root; read `README*`, `CONTRIBUTING*`, and any `docs/` index.
- Identify the language(s) and tooling from manifests: `package.json`, `pyproject.toml`/`setup.py`, `go.mod`, `Cargo.toml`, `pom.xml`, `Gemfile`.
- `git ls-files | wc -l` and skim top-level dirs to gauge size and layout. Note what to ignore (`node_modules`, `.venv`, `dist`, `vendor`, generated code).

## Step 2: Find the entry points

- Look for process starts: `main`, `if __name__ == "__main__"`, `cmd/`, CLI definitions, server bootstrap (`app.listen`, `uvicorn`, `http.ListenAndServe`), scheduled jobs.
- `grep -rn "def main\|func main\|createServer\|FastAPI(\|express()" <src>` and read manifest `scripts`/`entry_points`/`[project.scripts]`.
- Record how the app is actually started (dev command, container `CMD`).

## Step 3: Map the structure

- Enumerate top-level modules/packages and write a one-line purpose for each.
- Distinguish layers: interface (HTTP/CLI/UI) → application/services → domain → data access → integrations.
- Note the dependency direction between layers and any obvious violations.

## Step 4: Trace one representative flow end-to-end

- Pick a core operation (e.g. a key API route or CLI command).
- Follow it: route/handler → service → domain logic → persistence/external call → response.
- Use `grep` to jump between definition and call sites; read each hop's function.
- Capture where state is read/written (DB, cache, queue, files) and where external calls happen.

## Step 5: Locate cross-cutting concerns

- Config and secrets loading, dependency injection/wiring, auth, error handling, logging.
- External dependencies and data stores (from manifests + connection setup code).

## Step 6: Sanity-check with history

- `git log --oneline -20` and `git log --oneline -- <hot dir>` to see what changes most and recent direction.

## Step 7: Report

Produce, grounded in real file paths and line references:
1. Entry points — how it starts and what it does.
2. Architecture — layers/modules and their responsibilities (a short text diagram).
3. Data flow — the traced path, step by step.
4. Key files to read first (5–10, with why).
5. Glossary of domain terms and open questions.

Verify every claim against code before stating it; mark anything inferred as an assumption.
