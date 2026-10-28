"""Isolated Knowledge / approvals browser QA on 9903.

Uses the real routes and temporary ApprovalStore, an in-memory knowledge fixture,
and the real frontend. No Agent process, model, external fetch, or personal data.
Run: PYTHONPATH=. .venv/bin/python tests/manual/workspace_qa_app.py
"""
import hashlib
import os
from pathlib import Path
import signal
import socket
import tempfile
from unittest.mock import patch


class FixtureKnowledge:
    def __init__(self):
        self.rows = [{"doc_id": "a" * 64, "title": "Agent timeout troubleshooting", "source_uri": "fixture:studio-timeouts",
                      "version": "1", "kind": "document", "chunk_count": 1,
                      "content": "Check the recorded tool timeout before retrying a call. A retry must not duplicate a completed side effect. Review the exact call and its approval state in History."},
                     {"doc_id": "b" * 64, "title": "Human approval and call identity", "source_uri": "fixture:studio-approvals",
                      "version": "2", "kind": "document", "chunk_count": 1,
                      "content": "Approval authorizes one tool call, not a session or tool category. The decision is bound to the Agent, session, turn, call identifier and argument fingerprint."}]

    def status(self):
        return {"configured": True, "ready": True, "embedding_model": "isolated QA fixture · no model",
                "documents": len(self.rows), "chunks": len(self.rows), "collections": ["public"]}

    def documents(self, collection, offset, limit):
        rows = [{key: value for key, value in row.items() if key != "content"} for row in self.rows] if collection == "public" else []
        return {"items": rows[offset:offset + limit], "total": len(rows), "offset": offset, "limit": limit,
                "next_offset": offset + limit if offset + limit < len(rows) else None}

    def import_document(self, document):
        doc_id = hashlib.sha256((document.source_uri + document.version).encode()).hexdigest()
        self.rows = [row for row in self.rows if row["doc_id"] != doc_id]
        self.rows.append({"doc_id": doc_id, "title": document.title, "source_uri": document.source_uri,
                          "version": document.version, "kind": document.kind, "chunk_count": 1, "content": document.text})
        return {"doc_id": doc_id, "chunk_count": 1}

    def delete(self, doc_id):
        self.rows = [row for row in self.rows if row["doc_id"] != doc_id]
        return {"deleted": True}

    def search(self, query, mode, k, collection):
        hits = [{**row, "chunk_id": row["doc_id"][:12] + "-001", "line_start": 1, "line_end": 3, "score": 0.5}
                for row in self.rows if any(word.lower() in (row["title"] + row["content"]).lower() for word in query.split())]
        return {"hits": hits[:k] if collection == "public" else [], "mode": mode, "query": query}

    def diagnose(self, **kwargs):
        hits = self.search(**kwargs)["hits"]
        return {"status": "evidence" if hits else "no_evidence", "issue": kwargs["query"], "method": "extractive",
                "findings": [{"text": hit["content"], "citations": [hit["chunk_id"]]} for hit in hits],
                "evidence": hits, "next_steps": []}


def forbidden(*args, **kwargs):
    raise RuntimeError("Browser QA forbids outbound connections")


def terminate(*args):
    raise SystemExit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, terminate)
    with tempfile.TemporaryDirectory(prefix="studio-workspace-qa-") as temporary:
        root = Path(temporary).resolve()
        with patch.object(Path, "home", return_value=root), patch.dict(os.environ, {
            "AGENT_CONFIG_PATH": str(root / ".co"), "STUDIO_HISTORY_CONFIG": "", "STUDIO_HISTORY_DATABASE_URL": "",
            "STUDIO_KNOWLEDGE_CONFIG": str(root / "fixture-only"), "STUDIO_KNOWLEDGE_DATABASE_URL": "",
        }), patch.object(socket.socket, "connect", forbidden), patch.object(socket.socket, "connect_ex", forbidden):
            from co_studio import config, creator, registry, setup_check, update_check
            from co_studio.api import approvals, knowledge
            from co_studio.approvals import ApprovalStore
            from co_studio.app import create_app
            from fastapi.responses import JSONResponse
            import uvicorn

            config.STUDIO_PORT = 9903
            config.save_appearance("lavender")
            setup_check.status = lambda: {"co_auth_ok": True, "key_ok": True, "framework_ok": True,
                "studio_version": "isolated-qa", "agents_dir": str(config.AGENTS_DIR),
                "manager_url": "http://127.0.0.1:9903", "doctor": []}
            update_check.check = lambda: {"update_available": False, "current": "isolated-qa"}
            for slug, name in [("workspace-qa", "Workspace QA · isolated"), ("empty-qa", "Empty QA · isolated")]:
                directory = config.AGENTS_DIR / slug
                directory.mkdir(parents=True)
                registry.save(registry.AgentMeta(slug=slug, name=name, address="0xsynthetic-" + slug,
                    port=8099, model="offline", capabilities=[], created_at="2026-10-28T00:00:00Z"))
                (directory / "agent.py").write_text(creator.render(name, "offline", 8099, [], "open"), encoding="utf-8")
            store = ApprovalStore(config.AGENTS_DIR / "workspace-qa")
            store.request(run_id="fixture-run", session_id="fixture-session", turn_no=1, call_id="fixture-call-1",
                tool_name="write_note", arguments={"path": "notes/diagnosis.md", "content": "Record the timeout investigation."}, ttl=300)
            store.request(run_id="fixture-run", session_id="fixture-session", turn_no=2, call_id="fixture-call-2",
                tool_name="calculate", arguments={"expression": "17 * 23"}, ttl=300)
            knowledge.configured_store = lambda: fixture
            fixture = FixtureKnowledge()
            app = create_app()
            if not any(getattr(route, "path", "") == "/api/agents/{slug}/tools/config" for route in app.routes):
                app.include_router(approvals.router, prefix="/api")

            @app.middleware("http")
            async def isolated_writes_only(request, call_next):
                path = request.url.path
                allowed = (path.startswith("/api/knowledge/") or path.startswith("/api/agents/workspace-qa/approvals/")
                           or path == "/api/agents/workspace-qa/tools/config")
                if request.method not in {"GET", "HEAD"} and not allowed:
                    return JSONResponse({"detail": "Only isolated knowledge/policy/approval writes are permitted."}, status_code=403)
                return await call_next(request)

            print("Isolated workspace QA: http://127.0.0.1:9903 — temporary data, no Agent/model calls", flush=True)
            uvicorn.run(app, host="127.0.0.1", port=9903, lifespan="off", log_level="warning")
