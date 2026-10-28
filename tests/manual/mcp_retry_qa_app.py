"""Read-only synthetic retry UI fixture; no model, personal data or Agent.

PYTHONPATH=. .venv/bin/python tests/manual/mcp_retry_qa_app.py
Serves the existing History renderer on 127.0.0.1:9903 with temporary data.
All records are explicitly synthetic observations, not actual MCP executions.
"""
import json
import os
from pathlib import Path
import signal
import socket
import tempfile
from unittest.mock import patch


def forbidden(*args, **kwargs):
    raise RuntimeError("Retry UI QA forbids outbound connections")


def terminate(*args):
    raise SystemExit(0)


def build_fixture():
    from co_studio import config, registry, setup_check, update_check
    from co_studio.app import create_app
    from fastapi.responses import JSONResponse, HTMLResponse

    config.STUDIO_PORT = 9903
    config.save_appearance("warm")
    setup_check.status = lambda: {
        "co_auth_ok": True, "key_ok": True, "framework_ok": True,
        "studio_version": "synthetic-retry-qa", "agents_dir": str(config.AGENTS_DIR),
        "manager_url": "http://127.0.0.1:9903", "doctor": [],
    }
    update_check.check = lambda: {"update_available": False, "current": "synthetic-retry-qa"}
    directory = config.AGENTS_DIR / "synthetic-retry-qa"
    directory.mkdir(parents=True)
    registry.save(registry.AgentMeta(
        slug=directory.name, name="Synthetic retry QA", address="0xsynthetic",
        port=8099, model="offline", capabilities=[], created_at="2026-10-28T00:00:00Z"))
    trace = []
    for turn, (title, statuses) in enumerate([
        ("Synthetic: one disconnect, then success", ["transport_error", "success"]),
        ("Synthetic: both attempts disconnected", ["transport_error", "transport_error"]),
        ("Synthetic: cancelled; no retry", ["cancelled"]),
    ], start=1):
        trace.extend([
            {"type": "user_input", "id": f"u-{turn}", "turn": turn, "content": title},
            {"type": "tool_call", "id": f"start-{turn}", "tool_id": f"call-{turn}", "name": "studio_read_doc"},
        ])
        for index, status in enumerate(statuses, start=1):
            trace.append({
                "type": "studio_mcp_attempt", "id": f"attempt-{turn}-{index}",
                "tool_id": f"call-{turn}", "turn": turn, "ts": 1793185200.0 + turn + index,
                "name": "studio_read_doc", "attempt": index, "max_attempts": 2,
                "status": status, "retry_scheduled": index == 1 and len(statuses) == 2,
                "error_type": "ConnectionError" if status == "transport_error" else None,
                "duration_ms": 12.5, "elapsed_ms": 12.5 if index == 1 else 125,
            })
        trace.append({"type": "turn_result", "id": f"end-{turn}", "turn": turn, "reason": "natural"})
    source = directory / ".co" / "session_results.jsonl"
    source.parent.mkdir()
    source.write_text(json.dumps({
        "session_id": "synthetic-mcp-retry", "status": "done", "revision": 1,
        "created": 1793185200.0, "updated_at": 1793185300.0,
        "prompt": "Synthetic: cancelled; no retry", "result": "Synthetic UI data only; no model was called.",
        "session": {"session_id": "synthetic-mcp-retry", "turn": 3, "trace": trace},
    }) + "\n")
    app = create_app()

    @app.middleware("http")
    async def isolated_read_only(request, call_next):
        if request.method not in {"GET", "HEAD"}:
            return JSONResponse({"detail": "Synthetic retry QA is read-only."}, status_code=403)
        response = await call_next(request)
        if request.url.path == "/" and request.method == "GET":
            html = b"".join([chunk async for chunk in response.body_iterator]).decode()
            html = html.replace("<h1>History</h1>", "<h1>History · Synthetic QA</h1>")
            html = html.replace("Saved execution history", "Synthetic retry records · No model or personal data")
            return HTMLResponse(html, headers={"Cache-Control": "no-store"})
        return response

    return app


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, terminate)
    with tempfile.TemporaryDirectory(prefix="studio-retry-ui-qa-") as temporary:
        root = Path(temporary).resolve()
        with patch.object(Path, "home", return_value=root), patch.dict(os.environ, {
            "AGENT_CONFIG_PATH": str(root / ".co"),
            "STUDIO_HISTORY_DATABASE_URL": "", "STUDIO_HISTORY_CONFIG": "",
            "STUDIO_KNOWLEDGE_DATABASE_URL": "", "STUDIO_KNOWLEDGE_CONFIG": "",
        }), patch.object(socket.socket, "connect", forbidden), patch.object(socket.socket, "connect_ex", forbidden), patch.object(socket, "create_connection", forbidden):
            import uvicorn
            print(f"Synthetic retry UI fixture: {root}", flush=True)
            uvicorn.run(build_fixture(), host="127.0.0.1", port=9903, lifespan="off", log_level="warning")
