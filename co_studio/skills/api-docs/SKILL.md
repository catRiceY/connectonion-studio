---
name: api-docs
title: Document API
description: Generate reference documentation for a project's public API or HTTP endpoints
  directly from the source — signatures, parameters, return values, status codes,
  and examples. Use when the user says "document the API", "write API docs", "document
  these endpoints", or wants a reference for public functions/routes.
category: docs-security
tools:
- read_file
- glob
- grep
- list_files
- write_file
- edit_file
---

# API Docs Skill

Document the public surface exactly as the code defines it. Read the source; do not guess.

## Step 1: Determine the API type

- **HTTP service**: `grep` for route decorators/registrations — `@app.route`, `@router.(get|post|put|delete)`, `app.get(`, `@GetMapping`, `http.HandleFunc`.
- **Library**: `grep` for the public surface — `__all__`, `export function`, `export const`, `pub fn`, public classes.
- If both exist, ask nothing — document whichever the user named; default to the one with more matches.

## Step 2: Enumerate the surface

- Collect every public route or exported symbol with its file:line.
- For each, read the surrounding source to extract: name/path + HTTP method, parameters (path, query, body, or function args) with types and whether required, return value / response shape, and status codes or raised errors.
- Pull descriptions from existing docstrings/JSDoc/comments; do not paraphrase away important detail.

## Step 3: Extract request/response shapes

- For endpoints: find the request model and response model (Pydantic/DTO/schema/serializer) and read its fields.
- For functions: capture the full signature including defaults and type hints.
- Note authentication requirements (grep for `Depends(`, middleware, `@login_required`, auth headers).

## Step 4: Write the reference

Write to `docs/API.md` (or the path the user gives). For each item, use a consistent block:

```
### <METHOD /path>  or  <function_name(args)>
<one-line description>

- Auth: <required? which>
- Parameters: name — type — required? — description
- Returns / Response: shape + status codes
- Errors: condition → status/exception

Example request / call
Example response / return value
```

Group related endpoints/modules under headings. Keep a table of contents at the top for large surfaces.

## Step 5: Verify

- Every documented parameter and field must appear in the source — re-grep names you are unsure of.
- Do NOT invent endpoints, fields, or status codes not present in the code.
- Flag any public item lacking a docstring so the user can add intent.

## Rules

- Document only the PUBLIC surface (skip private/underscore-prefixed unless asked).
- Prefer real example values found in tests/examples over made-up ones.
- Keep types faithful to the source language's declarations.
