---
name: dockerize-app
title: Dockerize App
description: Write a correct, minimal multi-stage Dockerfile and matching .dockerignore
  for the project. Use when the user asks to dockerize, containerize, or add a Dockerfile
  to an app.
category: delivery-ops
tools:
- read_file
- write_file
- glob
- grep
- list_files
- Bash(docker build *)
- Bash(docker run *)
- Bash(docker image *)
---

# Dockerize App

Produce a small, correct multi-stage Dockerfile plus a `.dockerignore`, then verify it builds and runs.

## Instructions

1. Detect the stack. Look for manifests to identify language and package manager:
   - `package.json` (+ `package-lock.json` / `yarn.lock` / `pnpm-lock.yaml`) -> Node
   - `pyproject.toml` / `requirements.txt` -> Python
   - `go.mod` -> Go; `Cargo.toml` -> Rust; `pom.xml` / `build.gradle` -> JVM
   Read the manifest to find the start command, build script, and runtime version.

2. Pin a base image. Prefer a specific minor tag on a slim/alpine or `-slim` variant (e.g. `node:20-slim`, `python:3.12-slim`). Never use bare `latest`.

3. Write a multi-stage `Dockerfile`:
   - **builder stage**: copy only manifest + lockfile first, install deps (use the lockfile-honoring command: `npm ci`, `pip install -r`, `go build`, `cargo build --release`), then copy source and build.
   - **runtime stage**: start from a minimal base, copy only built artifacts and production deps from the builder. Do NOT copy the toolchain.
   - Order layers cheapest-changing first (deps before source) to maximize cache hits.

4. Harden the runtime stage:
   - Create and switch to a non-root user (`USER app`).
   - Set `WORKDIR`, `ENV NODE_ENV=production` (or equivalent), and a minimal `EXPOSE`.
   - Use exec-form `CMD ["..."]` (JSON array), not shell form.
   - Add a `HEALTHCHECK` only if the app serves a port.

5. Write `.dockerignore`. Exclude `.git`, `node_modules`, build output, `__pycache__`, `.venv`, `target`, `dist`, secrets/`.env`, test fixtures, and CI files. This keeps the build context small and prevents leaking secrets.

6. Verify the build:
   - Run `docker build -t app:test .` and confirm it completes.
   - Check the final size with `docker image ls app:test`; if it is large, confirm the toolchain did not leak into the runtime stage.
   - If the app is runnable, `docker run --rm -p <port>:<port> app:test` and confirm it starts without crashing.

7. Report the image name, final size, and the exact run command. Note any build args or env vars the user must supply at runtime.

## Checks
- Runtime stage contains no compilers/dev dependencies.
- Container runs as non-root.
- No secrets or `.env` files copied into the image.
- Base image tag is pinned, not `latest`.
