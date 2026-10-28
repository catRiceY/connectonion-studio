"""Read-only Prompt provenance browser fixture, isolated from personal data.

PYTHONPATH=. .venv/bin/python tests/manual/prompt_trace_qa_app.py

Real ConnectOnion 1.9.2 Agent/input_handler/SessionStorage produce the records;
the provider is entirely scripted. No Agent service, model/network call, database
or application lifespan is started. All fixtures disappear when this server exits.
"""
from collections import deque
import os
from pathlib import Path
import signal
import socket
import tempfile
from unittest.mock import patch


def forbidden(*args, **kwargs):
    raise RuntimeError("Prompt provenance QA forbids outbound connections")


def terminate(*args):
    raise SystemExit(0)


def build_fixture():
    from connectonion import Agent
    from connectonion.core.llm import LLMResponse
    from connectonion.core.usage import TokenUsage
    from connectonion.network.host.session import SessionStorage
    from connectonion.network.host.session.turn import input_handler
    from co_studio import config, creator, registry, setup_check, update_check
    from co_studio.app import create_app
    from co_studio.prompt_provenance import attach_provenance
    from co_studio.prompt_versions import PromptVersions
    from fastapi.responses import JSONResponse

    config.STUDIO_PORT = 9903
    config.save_appearance("lavender")
    setup_check.status = lambda: {
        "co_auth_ok": True, "key_ok": True, "framework_ok": True,
        "studio_version": "isolated-prompt-trace-qa", "agents_dir": str(config.AGENTS_DIR),
        "manager_url": "http://127.0.0.1:9903", "doctor": [],
    }
    update_check.check = lambda: {"update_available": False, "current": "isolated-prompt-trace-qa"}
    directory = config.AGENTS_DIR / "prompt-trace-qa"
    directory.mkdir(parents=True)
    co_dir = directory / ".co"
    co_dir.mkdir()
    registry.save(registry.AgentMeta(
        slug="prompt-trace-qa", name="Prompt Trace QA · isolated", address="0xsynthetic",
        port=8099, model="offline", capabilities=[], created_at="2026-10-28T00:00:00Z"))
    (directory / "agent.py").write_text(creator.render("Prompt Trace QA", "offline", 8099, [], "open"))
    versions = PromptVersions(directory)
    first = versions.save("baseline", "Version A: explain only the supplied offline test data.")["current"]
    storage = SessionStorage(co_dir / "session_results.jsonl")

    class ScriptedLLM:
        model = "offline-prompt-trace-qa"

        def __init__(self):
            self.responses = deque([
                "Version A was used for this first turn.",
                "This restored session still uses Version A after restart selected Version B.",
                "This new session uses Version B.",
                "This old session has no trusted binding, so its version is unknown.",
            ])

        def complete(self, messages, **kwargs):
            return LLMResponse(content=self.responses.popleft(), tool_calls=[], raw_response=None,
                               usage=TokenUsage(input_tokens=24, output_tokens=12, cost=0.0))

    provider = ScriptedLLM()

    def factory(revision):
        def create():
            agent = Agent("Offline Prompt QA", system_prompt=revision["text"], llm=provider,
                          quiet=True, log=False, co_dir=co_dir, state_dir=co_dir)
            return attach_provenance(agent, agent_dir=directory, revision=revision)
        return create

    def run(revision, prompt, session):
        return input_handler(factory(revision), storage, prompt, result_ttl=86400, session=session)

    run(first, "First turn: verify the initial Prompt version.", {"session_id": "old-session"})
    second = versions.save(first["id"], "Version B: explain test results with a short conclusion.")["current"]
    run(second, "Second turn: the process selected B; did this session retain A?", {"session_id": "old-session"})
    run(second, "New session: verify the selected Version B.", {"session_id": "new-session"})
    run(second, "Legacy session: do not infer the version from matching text.", {
        "session_id": "legacy-session",
        "messages": [{"role": "system", "content": second["text"]}],
    })

    app = create_app()

    @app.middleware("http")
    async def read_only(request, call_next):
        if request.method not in {"GET", "HEAD"}:
            return JSONResponse({"detail": "Isolated provenance fixture is read-only."}, status_code=403)
        return await call_next(request)

    print("Prompt trace QA sessions: old-session (A/A; launch A/B), new-session (B), legacy-session (unknown).", flush=True)
    return app


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, terminate)
    with tempfile.TemporaryDirectory(prefix="studio-prompt-trace-qa-") as temporary:
        root = Path(temporary).resolve()
        with patch.object(Path, "home", return_value=root), patch.dict(os.environ, {
            "AGENT_CONFIG_PATH": str(root / ".co"),
            "STUDIO_HISTORY_DATABASE_URL": "", "STUDIO_HISTORY_CONFIG": "",
        }), patch.object(socket.socket, "connect", forbidden), patch.object(socket.socket, "connect_ex", forbidden), patch.object(socket, "create_connection", forbidden):
            import uvicorn
            app = build_fixture()
            print(f"Isolated Prompt trace QA: {root}", flush=True)
            uvicorn.run(app, host="127.0.0.1", port=9903, lifespan="off", log_level="warning")
