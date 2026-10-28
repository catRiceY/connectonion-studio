# Local source deployment

This is an isolated, single-machine Docker Compose prototype, not a public or
multi-user hosting recipe. It builds the checked-out source against `uv.lock`.
It does not bundle credentials, user Agents, history, or embedding weights.
Only `127.0.0.1:9904` is published; the separate pgvector PostgreSQL service has
no host port. Existing desktop Studio / PostgreSQL services are not used.

## Start

Create a private local env file (mode `0600`) containing only
`POSTGRES_PASSWORD=<random value of at least 16 characters>`. Never commit it.
The following examples assume `.co-studio/deploy.env`; an existing private
env file with this variable also works. Avoid printing resolved Compose config
or container environment: Docker administrators can read this password.

```sh
docker compose --env-file .co-studio/deploy.env -f compose.studio.yml -p co-studio-deploy build
docker compose --env-file .co-studio/deploy.env -f compose.studio.yml -p co-studio-deploy up -d --wait
```

Open <http://127.0.0.1:9904>. The fresh instance intentionally has no API key.
Auto-authentication is disabled. Doctor may report missing identity / managed
key; this does not block history, bundled MCP, or local knowledge APIs. No chat
model is invoked by deployment or the acceptance probe.

The app runs as UID `10001` with dropped capabilities and no-new-privileges.
Nginx terminates the Docker-network hop and forwards to Uvicorn on the same
container's loopback interface. It rejects non-local Host values, preserves the
Host and Origin used by the browser, overwrites X-Forwarded-For, and disables
buffering for SSE. Existing API same-origin / fetch-site checks remain active.
The gateway additionally checks Origin and fetch-site for every route, including
legacy Agent lifecycle and WebSocket routes that lack those API dependencies.
This proxy is **not authentication**. Do not change the published address to
`0.0.0.0` or expose the Compose network to untrusted machines.

`studio-home` stores only this instance's settings / Agent folders / optional
model cache. `studio-db` stores this instance's archive and knowledge schemas.
The database initialization role is local-prototype privileged, not a
least-privilege production-role deployment. Chat/QR Agent ports are not
published by this recipe; remote Agent connectivity is not part of acceptance.

## Explicit local model preparation

Schema readiness does **not** mean embedding weights are present. To download
the public `BAAI/bge-small-en-v1.5` ONNX model once into the named home volume:

```sh
docker compose --env-file .co-studio/deploy.env -f compose.studio.yml -p co-studio-deploy exec -T app python -m co_studio.knowledge_cli prepare
```

This is a public model download, not paid inference. Normal import / search
loads only cached files and fails visibly if they are missing. Alternatively,
mount **only** an existing public embedding cache read-only (never your home,
`.co`, Agent directory, or API-key file):

```sh
STUDIO_EMBEDDING_CACHE=/absolute/public-model-cache docker compose --env-file .co-studio/deploy.env -f compose.studio.yml -f deploy/compose.model-cache.yml -p co-studio-deploy up -d --wait
```

Use the same optional overlay for subsequent `up` operations when retaining
that mount. The base recipe works without any host bind mount.

## Explicit synthetic acceptance and persistence

After model preparation, the seed probe registers an inert, clearly-labelled
synthetic Agent record, inserts one immutable archive capture, and imports one
generated document. It never starts the Agent or reads user data. It verifies
real stdio MCP SDK discovery/call, all three retrieval modes, API ownership
projection, cache headers, and Host / Origin / fetch-site guards.

```sh
docker compose --env-file .co-studio/deploy.env -f compose.studio.yml -p co-studio-deploy exec -T app python /app/deploy/smoke.py seed
docker compose --env-file .co-studio/deploy.env -f compose.studio.yml -p co-studio-deploy restart
docker compose --env-file .co-studio/deploy.env -f compose.studio.yml -p co-studio-deploy exec -T app python /app/deploy/smoke.py verify
```

Wait for app health after restart before verifying. Repeat `up -d --force-recreate
--wait` with the same files/project to additionally test container replacement.
`verify` does not reseed: a missing fixture is a failure. The probe only runs
inside the designated non-root container and builds a fixed `db:5432/studio`
connection; it does not accept a caller-selected database or environment DSN.
Its JSON output contains no password or DSN. `tokens: null` means unknown,
not zero; local embedding and synthetic contract success are not LLM quality.

Health is a local HTTP check of knowledge schema availability. Uvicorn gets
five seconds for graceful HTTP/SSE shutdown; the small parent supervisor
terminates both children and enforces a final bounded wait. The Compose stop
grace is 30 seconds. No automatic restart policy masks repeated startup errors.

To stop this isolated deployment while retaining data, use Compose `down`
with the same project/files. Do **not** add `--volumes` unless you deliberately
want to destroy this deployment's stored data. Never use global prune or stop
other projects as part of this workflow.

Offline packaging/supervision regression tests:

```sh
uv run pytest tests/test_deployment.py
```
