"""Isolated Knowledge UI fixture: synthetic evidence/answers, no model or user data.

PYTHONPATH=. .venv/bin/python tests/manual/diagnosis_qa_app.py
"""
import asyncio
import os
from pathlib import Path
import signal
import socket
import tempfile
from unittest.mock import patch


def forbidden(*args, **kwargs):
    raise RuntimeError("Synthetic diagnosis QA forbids outbound connections")


def terminate(*args):
    raise SystemExit(0)


def build_fixture():
    from co_studio import config, registry, setup_check, update_check, diagnostic_web
    from co_studio.api import knowledge
    from co_studio.app import create_app
    from fastapi.responses import JSONResponse, HTMLResponse
    config.STUDIO_PORT = 9905
    config.save_appearance("warm")
    (config.AGENTS_DIR / "synthetic-diagnosis-qa").mkdir(parents=True)
    registry.save(registry.AgentMeta(slug="synthetic-diagnosis-qa", name="Synthetic diagnosis QA",
        address="0xsynthetic", port=8099, model="offline", capabilities=[], created_at="2026-10-28T00:00:00Z"))
    setup_check.status = lambda: {"co_auth_ok": True, "key_ok": True, "framework_ok": True,
        "studio_version": "synthetic-diagnosis-qa", "agents_dir": str(config.AGENTS_DIR),
        "manager_url": "http://127.0.0.1:9905", "doctor": []}
    update_check.check = lambda: {"update_available": False, "current": "synthetic-diagnosis-qa"}
    hit = {"chunk_id": "a" * 64, "doc_id": "b" * 64, "title": "Synthetic reconnect guide",
           "source_uri": "studio://qa/reconnect", "version": "qa-v1", "line_start": 1, "line_end": 3,
           "content": "Reconnect with the saved session ID. Restoring the UI alone does not restore server state.", "chunk_count": 1}
    other = {**hit, "chunk_id": "c" * 64, "doc_id": "d" * 64, "title": "Synthetic client-specific guide",
             "source_uri": "studio://qa/client-specific", "content": "This client reconnects automatically. Check the client version before choosing either behavior."}
    class Store:
        def status(self):
            return {"configured": True, "ready": True, "embedding_model": "synthetic-no-embedding"}
        def documents(self, *args):
            return {"items": [hit], "total": 1, "next_offset": None}
        def search(self, **kwargs):
            return {"hits": [hit]}
        def search_context(self, query, **kwargs):
            return {"hits": [hit, other] if "冲突" in query else [hit]}
        def validate_citations(self, *args):
            return {"valid": True}
        def diagnose(self, **kwargs):
            return {"status": "evidence", "evidence": [hit], "findings": [], "next_steps": []}
    knowledge.configured_store = Store
    async def synthetic(request, body):
        if "余额不足" in body["query"]:
            from fastapi import HTTPException
            status, detail = diagnostic_web.PROVIDER_FAILURES["insufficient_credits"]
            raise HTTPException(status, detail)
        if "取消" in body["query"]:
            await asyncio.sleep(3)
        from co_studio.diagnostic import generate
        import json
        def fixture_complete(text, **kwargs):
            units = json.loads(text)["untrusted_evidence"]
            result = {"status": "hypotheses", "claims": [], "limitations": [], "verification_steps": [], "questions": []}
            if "澄清" in body["query"]:
                return {**result, "status": "needs_clarification", "questions": ["请提供具体错误信息、客户端和版本。", "请描述复现步骤，以及期望看到的结果。"]}
            if "越权" in body["query"]:
                return {**result, "status": "refused"}
            if "无证据" in body["query"]:
                return {**result, "status": "insufficient_evidence"}
            if "冲突" in body["query"]:
                return {**result, "status": "conflicting_evidence", "claims": [
                    {"text": "【模拟】重连指南要求使用原 session ID。", "evidence_ids": [units[0]["evidence_id"]]},
                    {"text": "【模拟】另一份客户端指南说明支持自动重连；需要核对客户端版本。", "evidence_ids": [units[1]["evidence_id"]]}]}
            if "资料" in body["query"]:
                return {**result, "status": "answered", "claims": [{"text": "【模拟】使用已保存的会话 ID 重连。恢复界面本身不等于恢复服务端状态。", "evidence_ids": [units[0]["evidence_id"]]}]}
            return {**result, "claims": [{"text": "【模拟】重连时可能没有携带原会话 ID。", "evidence_ids": [units[0]["evidence_id"]]}],
                    "verification_steps": [{"kind": "inspect_session_identifier", "evidence_ids": [units[0]["evidence_id"]]}]}
        return generate(Store(), body["query"], model=body["model"], collection=body["collection"],
                        max_output_tokens=body["max_output_tokens"], complete=fixture_complete)
    diagnostic_web.run = synthetic
    app = create_app()
    @app.middleware("http")
    async def isolated(request, call_next):
        if request.method not in {"GET", "HEAD"} and request.url.path not in {
            "/api/knowledge/search", "/api/knowledge/diagnose", "/api/knowledge/generate"
        }:
            return JSONResponse({"detail": "Synthetic QA denies mutations"}, status_code=403)
        response = await call_next(request)
        if request.url.path == "/":
            html = b"".join([part async for part in response.body_iterator]).decode()
            html = html.replace("<h1>Knowledge</h1>", "<h1>Knowledge · Synthetic QA</h1>")
            html = html.replace("Search versioned documents with traceable evidence.", "模拟资料与回答 · 未调用模型 · 与用户数据隔离")
            return HTMLResponse(html, headers={"Cache-Control": "no-store"})
        return response
    return app


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, terminate)
    with tempfile.TemporaryDirectory(prefix="studio-diagnosis-ui-qa-") as temporary:
        root = Path(temporary).resolve()
        with patch.object(Path, "home", return_value=root), patch.dict(os.environ, {
            "AGENT_CONFIG_PATH": str(root / ".co"), "STUDIO_AUTO_AUTH": "0",
            "STUDIO_HISTORY_DATABASE_URL": "", "STUDIO_HISTORY_CONFIG": "",
            "STUDIO_KNOWLEDGE_DATABASE_URL": "", "STUDIO_KNOWLEDGE_CONFIG": "synthetic-only",
        }), patch.object(socket.socket, "connect", forbidden), patch.object(socket.socket, "connect_ex", forbidden), patch.object(socket, "create_connection", forbidden):
            import uvicorn
            uvicorn.run(build_fixture(), host="127.0.0.1", port=9905, lifespan="off", log_level="warning")
