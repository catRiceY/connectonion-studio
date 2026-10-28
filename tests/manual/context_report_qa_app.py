"""Isolated History context-report UI QA; real CO Host, scripted model only.

PYTHONPATH=. .venv/bin/python tests/manual/context_report_qa_app.py

Creates temporary, explicitly labelled sessions on port 9903. No paid model,
personal data, database, running Agent, or application lifespan is accessed.
The legacy case deliberately has no policy/report. A fourth case deliberately
corrupts a report after persistence to exercise untrusted-data presentation.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import signal
import socket
import tempfile
from unittest.mock import patch


def forbidden(*args, **kwargs):
    raise RuntimeError("Context QA forbids outbound connections")


def terminate(*args):
    raise SystemExit(0)


def build_fixture():
    from connectonion import Agent
    from connectonion.core.llm import LLMResponse
    from connectonion.network.host.session import SessionStorage
    from connectonion.network.host.session.turn import input_handler
    from co_studio import config, registry, setup_check, update_check
    from co_studio.app import create_app
    from co_studio.context_eval import context_cases
    from co_studio.context_policy import ContextPolicy
    from co_studio.context_runtime import attach_context_policy
    from fastapi.responses import JSONResponse

    config.STUDIO_PORT = 9903
    config.save_appearance("warm")
    setup_check.status = lambda: {
        "co_auth_ok": True, "key_ok": True, "framework_ok": True,
        "studio_version": "isolated-context-qa", "agents_dir": str(config.AGENTS_DIR),
        "manager_url": "http://127.0.0.1:9903", "doctor": [],
    }
    update_check.check = lambda: {"update_available": False, "current": "isolated-context-qa"}
    directory = config.AGENTS_DIR / "context-report-qa"
    co_dir = directory / ".co"
    co_dir.mkdir(parents=True)
    registry.save(registry.AgentMeta(
        slug=directory.name, name="Context QA · isolated", address="0xsynthetic",
        port=8099, model="offline", capabilities=[], created_at="2026-10-28T00:00:00Z"))
    storage = SessionStorage(co_dir / "session_results.jsonl")

    class ScriptedModel:
        model = "offline-context-qa"

        def complete(self, messages, **kwargs):
            return LLMResponse(content="Offline scripted answer for UI verification only.",
                               tool_calls=[], raw_response=None, usage=None)

    def run(identity, history, budget):
        def factory():
            agent = Agent("Isolated Context QA", llm=ScriptedModel(), quiet=True, log=False,
                          co_dir=co_dir, state_dir=co_dir)
            return attach_context_policy(agent, agent_slug=directory.name, agent_dir=directory,
                                         policy=ContextPolicy(budget) if budget else None)
        return input_handler(factory, storage, "Continue this offline test.", result_ttl=86400,
                             session={"session_id": identity, "messages": deepcopy(history)})

    short = [{"role": "system", "content": "Use only the supplied offline test data."}]
    long = context_cases()[0].messages[:-1]
    run("01-compressed", long, 1100)
    run("02-within-budget", short, 1100)
    run("03-legacy-no-report", short, None)
    run("04-unverified-report", short, 1100)
    rows = [json.loads(line) for line in storage.path.read_text().splitlines()]
    for row in rows:
        if row.get("session_id") == "04-unverified-report":
            for event in row.get("session", {}).get("trace", []):
                if event.get("type") == "context_report":
                    event["estimated_tokens"] = 1  # Signature no longer matches.
    storage.path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    app = create_app()

    @app.middleware("http")
    async def read_only(request, call_next):
        if request.method not in {"GET", "HEAD"}:
            return JSONResponse({"detail": "Isolated context QA is read-only."}, status_code=403)
        return await call_next(request)

    print("Context QA: compressed, within budget, legacy absent, tampered unknown.", flush=True)
    return app


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, terminate)
    with tempfile.TemporaryDirectory(prefix="studio-context-report-qa-") as temporary:
        root = Path(temporary).resolve()
        with patch.object(Path, "home", return_value=root), patch.dict(os.environ, {
            "AGENT_CONFIG_PATH": str(root / ".co"),
            "STUDIO_HISTORY_DATABASE_URL": "", "STUDIO_HISTORY_CONFIG": "",
        }), patch.object(socket.socket, "connect", forbidden), patch.object(socket.socket, "connect_ex", forbidden), patch.object(socket, "create_connection", forbidden):
            import uvicorn
            uvicorn.run(build_fixture(), host="127.0.0.1", port=9903, lifespan="off", log_level="warning")
