"""Explicit synthetic acceptance probe, only inside the isolated Compose app.

seed writes one clearly-labelled fixture to the dedicated DB/home volume;
verify reads it again. Neither mode launches an Agent or calls a chat model.
The local public embedding model must be explicitly prepared or mounted first.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from co_studio import config, registry
from co_studio.execution_adapter import ConvertedSession
from co_studio.execution_models import SessionRecord, TurnRecord
from co_studio.history_archive import PostgresArchive, prepare_capture
from co_studio.mcp_readonly import open_fixture

SLUG = "synthetic-deploy-probe"
SESSION = "synthetic-container-persistence-v1"
URI = "studio://synthetic-deploy-probe/reference"
COLLECTION = "synthetic-deploy-probe"
BASE = "http://127.0.0.1:8080"


def request(path, *, data=None, headers=None):
    body = None if data is None else json.dumps(data).encode()
    req = Request(BASE + path, data=body, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urlopen(req, timeout=45) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None, {key.lower(): value for key, value in response.headers.items()}
    except HTTPError as exc:
        return exc.code, None, {key.lower(): value for key, value in exc.headers.items()}


def capture():
    return prepare_capture(ConvertedSession(
        SessionRecord(agent_slug=SLUG, session_id=SESSION, source_status="synthetic"),
        (TurnRecord(agent_slug=SLUG, session_id=SESSION, turn_no=1,
                    prompt="Synthetic deployment persistence check; no model invocation.",
                    result="A seeded fixture, not an Agent-generated answer."),), ()))


def isolated_archive():
    if (not Path("/.dockerenv").exists() or os.getuid() != 10001
            or config.STUDIO_HOME != Path("/home/studio/.co-studio")):
        raise RuntimeError("Probe requires the isolated non-root Studio container")
    password = os.environ.get("STUDIO_DATABASE_PASSWORD", "")
    if len(password) < 16:
        raise RuntimeError("Container database password missing")
    return PostgresArchive(f"postgresql://studio:{quote(password, safe='')}@db:5432/studio")


async def mcp_check():
    async with open_fixture() as client:
        result = await client.call("studio_get_guide", {"guide_id": "history"})
        assert result["source"] == "bundled-fixture" and result["id"] == "history"
        return {"transport": "real-sdk-stdio", "protocol": client.protocol_version,
                "tool_count": len(client.tools()), "guide": result["id"]}


def run(mode):
    archive = isolated_archive()
    record = capture()
    if mode == "seed":
        directory = registry.agent_dir(SLUG)
        directory.mkdir(parents=True, exist_ok=True)
        if not (directory / "meta.json").exists():
            registry.save(registry.AgentMeta(
                slug=SLUG, name="Synthetic deployment probe (never started)", address="synthetic",
                port=8099, model="synthetic-no-model", capabilities=[], created_at="2026-10-28T00:00:00Z"))
        archive.save(record)
        code, result, _ = request("/api/knowledge/documents", data={
            "source_uri": URI, "title": "Synthetic deployment persistence fixture",
            "text": "Synthetic deployment probe: persistent history captures and local knowledge documents survive container replacement. This is generated test content, not user data.",
            "version": "synthetic-v1", "license": "CC0-1.0", "collection": COLLECTION,
        })
        assert code == 200, f"Knowledge fixture import failed: HTTP {code}"
    assert archive.get(record.capture_id) == record
    status, storage, headers = request("/api/history/storage")
    assert status == 200 and storage["default_source"] == "archive"
    assert headers.get("cache-control") == "no-store"
    assert headers.get("x-content-type-options") == "nosniff"
    status, detail, _ = request(f"/api/agents/{SLUG}/archives/{record.capture_id}")
    assert status == 200 and detail["turns"]["total"] == 1
    assert detail["turns"]["items"][0]["result"] == record.turns[0].result
    status, docs, _ = request(f"/api/knowledge/documents?collection={COLLECTION}")
    assert status == 200 and docs["total"] == 1 and docs["items"][0]["source_uri"] == URI
    for mode_name in ("bm25", "vector", "hybrid"):
        status, found, _ = request("/api/knowledge/search", data={
            "query": "persistent history deployment", "collection": COLLECTION, "mode": mode_name})
        assert status == 200 and found["hits"][0]["source_uri"] == URI
    assert request("/api/knowledge/status", headers={"Origin": BASE})[0] == 200
    assert request("/api/knowledge/status", headers={"Origin": "https://evil.invalid"})[0] == 403
    assert request("/api/knowledge/status", headers={"Host": "evil.invalid"})[0] == 403
    assert request("/api/knowledge/status", headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert request("/api/knowledge/status", headers={"X-Forwarded-For": "203.0.113.1"})[0] == 200
    # Exercise gateway-wide protection, not only History/Knowledge dependencies.
    # This nonexistent target cannot start any Agent even if protection regresses.
    assert request("/api/agents/not-a-real-agent/start", data={}, headers={"Origin": "https://evil.invalid"})[0] == 403
    assert request("/api/agents", headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert request("/ws/status", headers={"Origin": "https://evil.invalid", "Upgrade": "websocket", "Connection": "Upgrade"})[0] == 403
    return {"probe_version": 1, "mode": mode, "synthetic": True, "uid": os.getuid(),
            "capture_id": record.capture_id, "knowledge_documents": docs["total"],
            "search_modes": ["bm25", "vector", "hybrid"], "local_guards": "pass",
            "mcp": asyncio.run(mcp_check()), "chat_model_calls": 0,
            "tokens": None, "note": "Local ONNX embeddings only; fixture is not model quality evidence."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("seed", "verify"))
    args = parser.parse_args()
    try:
        result = run(args.mode)
    except Exception:
        raise SystemExit("Deployment probe failed; no credentials or provider details are logged") from None
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
