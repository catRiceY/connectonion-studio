"""Manual browser fixture: synthetic data only, no lifespan or outbound network.

Run from the repository with PYTHONPATH=. uv run --locked python tests/manual/history_qa_app.py.
Add --demo for readable, clearly labeled sample sessions and read-only HTTP access.
The temporary directory is removed when the server exits. Never points at real Studio data.
"""

import json
import argparse
import os
from pathlib import Path
import socket
import tempfile
from unittest.mock import patch


def forbidden(*args, **kwargs):
    raise RuntimeError("Browser QA fixture forbids outbound networking")


def run(*, demo=False):
    with tempfile.TemporaryDirectory(prefix="studio-history-browser-") as temporary:
        root = Path(temporary)
        with patch.object(Path, "home", return_value=root), patch.dict(os.environ, {"AGENT_CONFIG_PATH": str(root / ".co")}), patch.object(socket.socket, "connect", forbidden), patch.object(socket.socket, "connect_ex", forbidden):
            from co_studio import config, registry, setup_check, update_check
            from co_studio.app import create_app
            import uvicorn

            config.STUDIO_PORT = 9902
            config.save_appearance("lavender" if demo else "warm")
            setup_check.status = lambda: {
                "co_auth_ok": True, "key_ok": True, "framework_ok": True,
                "studio_version": "qa-fixture", "agents_dir": str(config.AGENTS_DIR),
                "manager_url": "http://127.0.0.1:9902", "doctor": [],
            }
            update_check.check = lambda: {"update_available": False, "current": "qa-fixture"}
            for index, (slug, name) in enumerate([
                ("recorded", "Recorded Agent (synthetic)"),
                ("missing", "No history (synthetic)"),
                ("empty", "Empty file (synthetic)"),
                ("broken", "Unreadable rows (synthetic)"),
            ]):
                directory = config.AGENTS_DIR / slug
                directory.mkdir(parents=True)
                demo_names = {"recorded": "[演示] 有历史记录的诊断助手", "missing": "[演示] 尚无历史文件", "empty": "[演示] 空历史文件", "broken": "[演示] 损坏记录提示"}
                registry.save(registry.AgentMeta(slug=slug, name=demo_names[slug] if demo else name, address="0xsynthetic", port=8090 + index, model="offline", capabilities=[], created_at=f"2026-10-27T00:00:0{index}Z"))
            (config.AGENTS_DIR / "empty" / ".co").mkdir()
            (config.AGENTS_DIR / "empty" / ".co" / "session_results.jsonl").write_text("")
            (config.AGENTS_DIR / "broken" / ".co").mkdir()
            (config.AGENTS_DIR / "broken" / ".co" / "session_results.jsonl").write_text("{bad json\n")
            trace = [{"type": "runtime_input", "id": "unassigned", "content": "Unassigned internal message"}]
            malicious = '<img src=x onerror="window.__HISTORY_XSS__=true"><script>window.__HISTORY_XSS__=true</script>'
            for turn in range(1, 24):
                trace.extend([
                    {"type": "user_input", "id": f"u{turn}", "turn": turn, "content": f"Question {turn}: " + (malicious if turn == 1 else "Describe this offline fixture")},
                    {"type": "llm_call", "id": f"l{turn}", "model": "fixture-model"},
                    {"type": "llm_result", "id": f"l{turn}", "model": "fixture-model", "duration_ms": 0 if turn == 1 else 12, "usage": {"input_tokens": 40, "output_tokens": 8}},
                    {"type": "tool_call", "id": f"tc{turn}", "tool_id": f"t{turn}", "name": "read_order", "args": {"id": f"order-{turn}", "authorization": "Bearer synthetic-secret"}},
                    {"type": "tool_result", "id": f"tr{turn}", "tool_id": f"t{turn}", "name": "read_order", "status": "error" if turn == 2 else "success", "timing_ms": 2, "result": "Synthetic timeout" if turn == 2 else "Order metadata " + "LongUnbrokenPayload" * 30},
                    {"type": "turn_result", "id": f"r{turn}", "turn": turn, "reason": "natural"},
                ])
            rows = []
            for index in range(24):
                rows.append({
                    "session_id": "long/session/" + "opaque-session-" * 12 if index == 23 else f"session-{index:02d}",
                    "status": "done", "prompt": "Question 23: Describe this offline fixture", "result": "Final answer " + malicious,
                    "created": 1793059200 + index, "updated_at": 1793059300 + index,
                    "revision": 7, "duration_ms": 44,
                    "session": {"turn": 23, "trace": trace if index == 23 else trace[:7]},
                })
            source = config.AGENTS_DIR / "recorded" / ".co" / "session_results.jsonl"
            source.parent.mkdir()
            if demo:
                from history_demo_data import demo_rows
                rows = demo_rows()
            source.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows) + ("" if demo else "{partial-tail\n"))
            print(f"Synthetic QA root: {root}", flush=True)
            app = create_app()
            if demo:
                from fastapi.responses import HTMLResponse, JSONResponse

                @app.middleware("http")
                async def label_demo(request, call_next):
                    if request.method not in {"GET", "HEAD"}:
                        return JSONResponse({"detail": "演示环境只读：不会启动 Agent 或修改设置。"}, status_code=403)
                    response = await call_next(request)
                    if request.url.path == "/" and request.method == "GET":
                        html = (b"".join([chunk async for chunk in response.body_iterator])).decode()
                        html = html.replace("<h1>History</h1>", "<h1>History · Demo</h1>")
                        html = html.replace("Saved sessions</p>", "演示数据 · 未调用真实模型 · 与你的 Agent 数据隔离</p>")
                        return HTMLResponse(html, headers={"Cache-Control": "no-store"})
                    return response

            uvicorn.run(app, host="127.0.0.1", port=9902, lifespan="off", log_level="warning")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true", help="Readable, labeled, read-only demo for manual testing")
    run(demo=parser.parse_args().demo)
