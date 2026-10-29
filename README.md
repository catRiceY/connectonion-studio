<div align="center">

<img src="https://raw.githubusercontent.com/catRiceY/connectonion-studio/main/co_studio/frontend/assets/onion/onion_full.png" width="96" alt="" />
<br />
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/catRiceY/connectonion-studio/main/assets/wordmark-dark.png">
  <img src="https://raw.githubusercontent.com/catRiceY/connectonion-studio/main/assets/wordmark-light.png" width="360" alt="ConnectOnion Studio" />
</picture>

**A local test cockpit for [ConnectOnion](https://github.com/openonion/connectonion) agents.**

Click toolkits to build an agent · scan its QR from the iOS app · watch live logs

[![PyPI](https://img.shields.io/pypi/v/connectonion-studio.svg)](https://pypi.org/project/connectonion-studio/)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey.svg)](#requirements)

</div>

---

## Install

The main branch includes **unreleased source-development features** described
below. Published PyPI/macOS 0.1.8 artifacts do not automatically contain them;
use the locked source checkout or source container for this engineering prototype.

```bash
pipx install connectonion-studio      # or: pip install connectonion-studio
co-studio                             # opens http://127.0.0.1:9900
```

That's it. The first launch **auto-creates your identity and activates your managed model key** (needs internet — it's your own key, one per user). Offline on first run? Just run `co auth` once you're back online.

## Desktop app (macOS)

A native macOS app (SwiftUI + WKWebView) lives in [`macos/`](macos/) — the same cockpit in a real window with the traffic-light chrome and app icon. Build and run it from Xcode (`macos/ConnectOnionStudio/`); a signed `.dmg` for one-click install is on the way.

[Native compile smoke](.github/workflows/native-build.yml) builds the Debug app on
GitHub's macOS runner using the committed Swift package lock, with signing
disabled. This checks **native shell compilation only**, not full bundled-runtime acceptance:
it does not download/package Python, launch the app, sign, notarize or publish a
release. A green run does not prove the bundled backend or installed app works.

## What it does

|  |  |
|---|---|
| 🧩 **Create** | Form: name · model · toolkit checkboxes → identity + QR ready instantly |
| ▶️ **Run** | One process per agent, health-polled every 5 s, live state over WebSocket |
| 📱 **QR** | Encodes `connectonion://add?address&name&endpoint` — one scan fills all three iOS "Add agent" fields (direct LAN endpoint, bypasses the relay) |
| 📜 **Logs** | Master–detail cockpit: per-agent live console (this run only), stat tiles, follow / errors-only, reveal-in-Finder |
| 🩺 **Copy for Claude** | Paste-ready markdown diagnostics bundle, one click |
| 🗑️ **Delete** | Two-step confirm, then a permanent delete — identity, keys, and logs are removed for good |

**Toolkits:** `utility` · `web` · `files` · `shell` (approval-gated) · `image`

## Requirements

**Python 3.11+** · macOS, Linux, or Windows (WSL recommended)

<details>
<summary><b>API reference</b></summary>

<br>

| Method | Path | Returns |
|---|---|---|
| `GET` | `/api/agents` | `{"agents": [AgentSummary]}` |
| `POST` | `/api/agents` | AgentDetail — body `{name, model, toolkits, trust}` |
| `GET` | `/api/agents/{slug}` | AgentDetail |
| `POST` | `/api/agents/{slug}/start` · `/stop` · `/restart` | `{"state": …}` |
| `POST` | `/api/agents/{slug}/rename` | AgentSummary — body `{name}` |
| `DELETE` | `/api/agents/{slug}` | `204` (stops the agent, then permanently deletes it) |
| `GET` | `/api/agents/{slug}/qr.svg` | SVG QR of the `connectonion://add` deep link (address · name · endpoint) |
| `POST` | `/api/agents/{slug}/reveal-logs` | Opens the agent's `runs/` folder in the OS file browser (local) |
| `GET` | `/api/agents/{slug}/diagnostics` | Markdown bundle |
| `GET` | `/api/setup/status` | Doctor checks |
| `GET` | `/api/setup/update` | PyPI update check — `{current, latest, update_available}` |
| `WS` | `/ws/status` | Status frames (each change + every 5 s) |
| `WS` | `/ws/agents/{slug}/logs` | `{"source": "stdout", "line": …}` — the current run, from its first line |

</details>

<details>
<summary><b>Storage &amp; internals</b></summary>

<br>

```
~/.co-studio/agents/<slug>/   meta.json · agent.py · .env · .co/ · runs/<timestamp>.log
```

- One stdout log **per run** under `runs/`; the live console (and diagnostics) read only the current run.
- Agent ports `8000–8099`, allocated by socket probe (busy ports skipped, re-probed at start).
- `agent.py` is standalone — eject it anywhere and `python agent.py` still works (the announce IP-trim patch is inlined).

</details>

<details>
<summary><b>Development (from source)</b></summary>

<br>

```bash
git clone https://github.com/catRiceY/connectonion-studio && cd connectonion-studio
uv sync --locked --python 3.12
uv run --locked pytest
uv run --locked co-studio
```

Development is pinned to **ConnectOnion 1.9.2**; `uv.lock` also fixes the resolved
dependencies. The default tests use temporary user data and block Python socket
connections; they do not require model credentials. PostgreSQL integration tests
skip unless explicitly configured with a disposable local test database.
Starting `co-studio` is
different: it uses your normal Studio data and may perform first-run authentication.
The existing macOS app has its own bundled Python and is not updated by these commands.

The 1.9.2 migration reuses native per-agent trust storage. Coding-agent delegation
and watch tools are not enabled in Studio until their workspace and lifecycle
boundaries are explicitly supported. Existing workspace-confined tools remain available.

Without uv, `./install.sh` still installs the pinned framework into `.venv`, but
does not apply the full development lockfile.

| Env | Effect |
|---|---|
| `CONNECTONION_PATH=~/repo/connectonion ./install.sh` | Install the framework from a local checkout instead of PyPI |
| `PYTHON=python3.12 ./install.sh` | Pick the interpreter |

</details>

## Optional history archive (source development)

`uv sync --locked --extra history` installs the PostgreSQL driver. The explicit
`co-studio-history` CLI can initialize the schema, import one registered Agent's
session, list captures, and read a capture independently of its source JSONL.
Use `co-studio-history --help` for commands. Configure the connection through
`STUDIO_HISTORY_DATABASE_URL`, or `--config` with a local JSON file containing
`database_url`. Keep credentials out of Git; `.co-studio/` is ignored.

`compose.history.yml` provides only a local PostgreSQL 17 service (loopback port
55432 by default), requires `POSTGRES_PASSWORD`, and uses a persistent volume.
It does **not** containerize Studio or its Agents. Imports are transactional:
repeated content is deduplicated, changed snapshots are retained. There is no
automatic retention deletion. Polling cannot recover records that the framework
has already removed before Studio observes them.
Recognizable credentials are masked before insertion, but free text is not fully
anonymized. The archive is a single-user local feature, not a shared authenticated
service. No upstream ConnectOnion data is modified.

History presents **one row per session**, without a storage-source selector.
To enable PostgreSQL history in the source-development server,
initialize the schema with the CLI, start PostgreSQL separately, and launch:

```bash
STUDIO_HISTORY_CONFIG=/absolute/path/to/history-db.json uv run --locked --extra history co-studio
```

Alternatively, set the server's `STUDIO_HISTORY_DATABASE_URL`. Connection details
stay server-side. With archive configuration present, a single background worker
catches up registered Agents' retained JSONL sessions at startup, checks file
changes every 2 seconds, and retries failed writes with backoff capped at 60
seconds. A 60-second reconciliation retries unchanged sources too. The worker
requires macOS/Linux secure file-descriptor APIs and rejects sources larger than
64 MiB instead of silently truncating them. It does not initialize the schema,
start Agents, call models, or alter source files. Studio shutdown stops polling;
Agents can keep running independently, so this is **not a zero-loss guarantee**.

With no archive configured, History uses the file backend as a whole. Database
or configuration errors never silently switch to files. GET requests stay
read-only; refresh queries saved state, not an import operation. Sync failures
are visible. History chooses a structurally readable capture, then source update
time/revision, never just the most recently imported capture. Ambiguous or absent
source timestamps cannot prove chronology. All saved versions remain accessible
under **Session details → History versions**; detail pagination stays on its
fixed capture ID. The lower-level file/capture APIs remain diagnostic interfaces.
The CLI's `--config` flag alone does not configure an already-running Studio server.

### History automatic updates

`GET /api/agents/{slug}/history/stream` exposes a local, same-origin SSE endpoint
when PostgreSQL history is configured. History subscribes while the view is open
and closes the subscription when leaving the view or switching Agents. It sends
**invalidation notifications**, not model tokens, stdout, or conversation/tool payloads:

- `history.changed`: a new capture has committed; includes Agent/session/capture IDs.
- `history.sync`: synchronization state changed; read History for current status.
- `history.reset`: reload authoritative History (initial connect, expired cursor,
  process restart, slow-reader overflow, or 60-second reconciliation).

Opaque `Last-Event-ID` cursors support same-process replay within a shared
256-notification buffer. This is not a durable queue or exactly-once delivery.
Clients must handle every reset (even if its ID repeats), reconcile GET results
by stable IDs, and preserve fixed old-version views. Heartbeats are 15 seconds;
socket sends time out after 5 seconds; at most 32 streams are admitted per process.
External CLI/database imports do not publish directly: periodic reset requests
cover them by asking clients to reload, without per-client SQL/source polling.
File-only mode returns 409; invalid configuration/unavailable service returns 503;
capacity exhaustion returns 429. A connected stream is not a database health check.
Use the single-process Studio server; this is not a distributed pub/sub service.

The frontend coalesces notifications and fetches authoritative saved state before
updating the screen. It retains the loaded pagination window, event filter,
scroll position and expanded details. Explicitly selecting a saved version pins
that view; a newer version only shows a **View latest** notice. Failed refreshes
keep existing content and show an inline Retry action. The normal view has no
permanent Refresh button or connection badge.
Native reconnects retain the browser cursor; permanently closed streams are retried
with capped backoff and an initial reset. Connection interruptions are only shown
while recovery is needed. In file-only mode, leave and reopen History to read new
sessions; it does not subscribe to SSE. Retry rereads saved state, not restart an
Agent or the sync worker.

Each conversation turn has a **View execution** timeline, loaded on demand with
independent pagination. It follows recorded sequence order and distinguishes model
requests, model responses, tool requests, tool results, errors and turn endings.
Missing durations/results stay unknown; recorded request status is not a live
process indicator. Raw fields remain available in collapsed details, and the
full event list remains under **All execution events**.

## Prompt versions

Open **Agents → Detail → Prompt versions** for a Studio-generated custom Agent.
Edit its base prompt, save an immutable version, compare with an earlier version,
or restore old content as a new version. Concurrent edits use an optimistic base
version check; a conflict preserves the local draft rather than overwriting it.
Drafts survive drawer navigation, not a browser reload.

Saving does **not** start or restart the Agent. On its next explicit launch through
Studio, new conversations use the selected base prompt. Existing conversations may
retain their original system messages. The selected version is frozen at process
launch; running processes are not hot-swapped. For version-enabled custom Agents,
History now shows **Base prompt** per turn, based on observations at the model
provider interface. A resumed conversation can retain its old version even when
the process has selected a newer version. Missing, modified or unverifiable
records stay unknown; the current selection never backfills old history. A
standalone `python agent.py` bypasses this Studio runner integration.

Versions are local to the registered Agent's `.studio-prompts/` directory, outside
its tool workspace. Content is limited to 64 KiB UTF-8, with validated hashes,
atomic current-version updates, immutable parent history and rollback provenance.
This does not add multi-user authorization. Co-ai's built-in safety prompt and
non-generated/customized scripts are intentionally not editable through this UI.

Attribution stores revision IDs and hashes, not prompt text. It is not proof of
model success, a provider HTTP-body audit, or an immutable multi-user audit log.
Plugins that change system/developer instructions are conservatively unconfirmed.
Opt-in Studio tools/context also save credential-free startup fingerprints for
the model, tool schemas and readable implementation source. Missing source stays
unknown. Complete failed-turn retention remains a separate boundary.

## Knowledge, approvals and context

These are optional, local-owner features; the Studio does not become a remote,
multi-tenant service. No model is invoked when opening the Knowledge page.

```sh
uv sync --locked --extra history --extra mcp --extra knowledge
# Creates private local credentials; refuses to overwrite an existing setup.
.venv/bin/python -m co_studio.knowledge_cli bootstrap
docker compose --env-file .co-studio/knowledge.env -f compose.knowledge.yml -p co-studio-knowledge up -d --wait
export STUDIO_KNOWLEDGE_CONFIG="$PWD/.co-studio/knowledge-db.json"
.venv/bin/python -m co_studio.knowledge_cli initialize
.venv/bin/python -m co_studio.knowledge_cli prepare
.venv/bin/python -m co_studio.knowledge_cli ingest-package
STUDIO_HISTORY_CONFIG="$PWD/.co-studio/history-db.json" .venv/bin/co-studio --no-browser
```

`prepare` downloads a public local embedding model; indexing/search do not use
paid LLM APIs. Knowledge uses **BM25**, real **pgvector** dense search and **RRF**,
with source/version/hash metadata and exact-quote validation. BM25-only remains
available for comparison. The default UI diagnosis extracts evidence; it does
not prove a root cause. The separate Chinese diagnosis action requires a model
ID and explicit consent to send the question/evidence to that provider and incur
charges. Versioned `diagnostic-v2` distinguishes factual answers, hypotheses,
clarification, conflict, insufficient evidence and refusal. The model selects
evidence IDs; the server supplies exact original quotes and checks source currency.
Factual limitations and suggested verification steps also require citations.
Verification steps use a fixed `readonly-checks-v1` allowlist: the model selects
a check kind and evidence IDs, while the server supplies the manual-check wording.
No free-form command is accepted in that channel; this is not a guarantee that
all free-text claims are semantically safe or correct.
It does not execute suggested actions. Web and CLI share a **4,096-token output
default**; the web confirmation explicitly offers 2,048/4,096 and changing the
selection resets consent. Caps are not fee ceilings; budgets never automatically
increase after a failure. Generation has bounded workers, a deadline and cancellation;
local cancellation cannot reverse provider charges. The explicit
`co_studio.diagnostic --model ... --allow-model-call` CLI is also available.
Quote validity is not semantic entailment. The first small real-model evaluation
found generation failures; it is not a passed quality acceptance (see below).

Chinese technical queries keep their original text and may add a small, auditable
English glossary query under the same candidate budget. This is not a general
translator or multilingual embedding model. Ordinary Search preserves its ranked
hits. Generated answers use a separate bounded context-assembly policy: primary
hits remain intact while same-version adjacent passages and additional document
context can supplement them. The earlier rank-replacing adjacent experiment stays
separate; context assembly does not guarantee full evidence or conflict coverage.
Supplemental passages are not counted as ranked Recall@5 hits in evaluations.

With History and Knowledge configured, a bounded worker indexes completed turns
and whitelisted Agent configuration into separate Agent collections. The source
History remains unchanged. **History → Find evidence** opens that Agent's scope;
public documents are separately selectable. Sync is eventual, not atomic with
Agent execution. Indexed line numbers refer to sanitized indexed text.

For a generated **custom** Agent, open **Detail → Studio tools**. Enable shipped
read-only MCP documentation tools and/or per-call human approvals; these settings
apply on the next explicit restart. No arbitrary MCP command, shell or URL is
accepted. **Approvals** binds each decision to its exact session, turn, call and
arguments, with expiry and single consumption. Parameters are checked against
local JSON Schema before execution. Native hard denials still apply. Cancellation
cannot undo already-executed external side effects.

The optional context input budget preserves system instructions, recent turns,
tool-call/result pairs and explicitly structured task facts while compressing old
history into an extractive provider-only view. Original saved messages are not
overwritten. The budget uses an **estimate**, not provider billing tokens; a
mandatory context that exceeds the budget fails explicitly. This is not a
cross-user memory service or a guarantee of lossless semantic compression.
The v3 policy can retain bounded complete tool-output quotations without treating
their contents as instructions or permissions. Larger results and insufficient
budgets can still lose facts. Known generated-Host exception paths preserve the
observed failed execution through the normal Host commit; process death or disk
failure is not a zero-loss guarantee.

## Reproducible evaluation

`python -m co_studio.evaluation --help` provides versioned engineering contracts,
isolated synthetic archive capacity tests and report merging. `python -m
co_studio.retrieval_eval --help` compares three retrievers on the frozen questions
in `evaluation/retrieval-v1.json`. Reports keep code/data hashes, failures,
denominators and unknown usage. Capacity tests create dedicated local benchmark
databases; they never write synthetic tasks into normal History.

The 2026-10-28 local experiment indexed **963 independent public documentation
files** (3,543 chunks), round-tripped **10,000 synthetic task turns** through
PostgreSQL, and evaluated **72 distinct offline engineering/retrieval cases**.
These are not production customers, real-model task runs, or human-reviewed
external benchmarks. Hybrid Recall@5 was 90.625% on 32 documentation questions;
BM25 achieved 93.75%, so hybrid was not uniformly superior. These offline counts
do not include the separate paid-model pilot below.

Additional evaluation commands keep these baseline counts unchanged:

```sh
# Verify the separate 16-query / 8-group source-evidence dataset, without a model.
.venv/bin/python -m co_studio.evidence_eval --split all --validate-only
# Inspect a three-strategy model experiment plan; this command makes no API calls.
.venv/bin/python -m co_studio.model_eval plan --case task-fields --repeats 3
```

The new evidence labels still require human review; translated pairs are not
independent scenarios. Actual model experiments require an explicit model and
call allowance; scripted checks and estimated tokens are not business success or
billing measurements. Endpoint and provider secrets must not be committed.

### Bounded live-model pilot

The October 28–29 pilot dispatched **23 requests**, within an authorized limit
of 30, using public evidence and synthetic tasks only. It stopped when the
provider reported insufficient credits; no automatic top-up or account switch
was performed. Original failed results remain part of the reports.

| Experiment | Observed result | Boundary |
|---|---|---|
| Context: 3 cases × 3 strategies | Full history 2/3; truncation 1/3; Studio 2/3 | Agent-reviewed answers, not human review; two output truncations count as failures |
| Context input tokens | Full 14,146; Studio 1,907 | Provider-reported totals on these three synthetic cases only |
| Generated diagnosis: 8 cases | 1/8 task success | Seven generation failures; no delivered citations available for semantic scoring |
| Diagnostic follow-up: 1 case | InsufficientCreditsError | Does not retrospectively explain the seven earlier unclassified failures |
| Read-only Agent loop: 3 tasks / 5 requests | 2/3 task success; observed tool success 1/2 | Missing-service handling is a successful task but a failed tool call; third task failed on credits |
| Agent task latency | P95 7,817 ms, n=3 | Includes isolated worker/model startup and failures; not HTTP/mobile or production-load latency |

This is a small failed/partial quality acceptance, **not** evidence of production
accuracy. Citation semantic accuracy is unknown, not 100%. Remaining model
quality checks need available provider credit and a new explicit bounded run.
See [evaluation instructions](evaluation/README.md) for commands, grading and
report provenance. Private run artifacts and credentials are not published.

CI is configured for Python/Node regression, disposable PostgreSQL/pgvector
integration tests and a source image build. It does not deploy or publish a
binary. Container configuration is in `Dockerfile` and `compose.studio.yml`;
this is a loopback-only, isolated prototype, not a public deployment.

## Security

- The manager binds **`127.0.0.1:9900` only**.
- Agent ports are **unauthenticated** and exposed on your LAN by `host()` — that is how the phone connects. **Don't run on hostile networks.**
