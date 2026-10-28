"""Offline contracts between Studio's generated agents and ConnectOnion 1.9.2."""

import importlib
import os
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from connectonion.core.llm import LLMResponse
from connectonion.core.usage import TokenUsage, turn_usage_from_trace
from connectonion.network.host.session import Session, SessionStorage

from co_studio import creator, sandbox
from co_studio.app import create_app
from co_studio.scoped_trust import scope_host


class FakeLLM:
    model = creator.DEFAULT_MODEL

    def complete(self, *args, **kwargs):
        return LLMResponse(
            content="offline response", tool_calls=[], raw_response=None,
            usage=TokenUsage(input_tokens=8, output_tokens=2, cost=0.001),
        )


def generated_factory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Exercise the real template without changing the test process's home/cwd.
    monkeypatch.setattr(sandbox, "configure_process_environment", Mock())
    agent_module = importlib.import_module("connectonion.core.agent")
    monkeypatch.setattr(agent_module, "create_llm", lambda **kwargs: FakeLLM())
    namespace = {"__name__": "studio_test_agent", "__file__": str(tmp_path / "agent.py")}
    source = creator.render("Contract", creator.DEFAULT_MODEL, 8000, ["utility"], "open")
    exec(compile(source, str(tmp_path / "agent.py"), "exec"), namespace)
    return namespace["create_agent"]


def test_generated_factory_runs_without_sharing_conversation(tmp_path, monkeypatch):
    factory = generated_factory(tmp_path, monkeypatch)
    first, second = factory(), factory()
    assert first is not second
    assert first.input("test") == "offline response"
    assert second.current_session is None
    assert first.current_session["messages"][0]["role"] == "system"
    usage = turn_usage_from_trace(first.current_session["trace"])
    assert usage["input_tokens"] == 8
    assert usage["output_tokens"] == 2
    assert usage["cost"] == 0.001


def test_host_uses_explicit_agent_directory_and_keeps_health_contract(tmp_path, monkeypatch):
    factory = generated_factory(tmp_path, monkeypatch)
    server = importlib.import_module("connectonion.network.host.server")
    co_dir = tmp_path / ".co"
    with (
        patch.object(server, "_port_in_use", return_value=False),
        patch("connectonion.cli.commands.agent_index.refresh", return_value=[]),
        patch.object(server, "_print_host_banner"),
        patch.object(server, "asgi_create_app", wraps=server.asgi_create_app) as build_app,
        patch.object(server.uvicorn, "run") as serve,
    ):
        scope_host(server.host)(factory, port=8000, trust="open", co_dir=co_dir, relay_url=None)

    options = build_app.call_args.kwargs
    storage = options["storage"]
    assert storage.path == co_dir / "session_results.jsonl"
    # Exercise the actual ASGI app, but do not start relay/scheduler/auth lifespan.
    client = TestClient(serve.call_args.args[0], base_url="http://localhost:8000")
    try:
        assert client.get("/health").json()["status"] == "healthy"
        assert client.get("/info").json()["address"]
    finally:
        client.close()

    storage.save(Session(session_id="one", status="completed", prompt="test", result="saved"))
    reloaded = SessionStorage(storage.path)
    assert reloaded.get("one").result == "saved"
    other = tmp_path / "other" / ".co"
    other.mkdir(parents=True)
    assert SessionStorage(other / "session_results.jsonl").get("one") is None


def test_co_ai_loads_project_settings_before_construction(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # A harmless setting proves ordering without supplying real credentials.
    key = "CO_STUDIO_ENV_ORDER_TEST"
    monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(f"{key}=project-setting\n")
    monkeypatch.setattr(sandbox, "configure_process_environment", Mock())
    monkeypatch.setattr(sandbox, "sandbox_coding_agent", Mock())
    factory = importlib.import_module("connectonion.cli.co_ai.agent")
    captured = []

    def fake_factory(**kwargs):
        captured.append(os.environ.get(key))
        return object()

    monkeypatch.setattr(factory, "create_coding_agent", fake_factory)
    monkeypatch.setattr("connectonion.host", Mock())
    source = creator.render(
        "Coder", creator.DEFAULT_MODEL, 8000, [], "strict", preset="co-ai", invite_code="test-code"
    )
    exec(compile(source, str(tmp_path / "agent.py"), "exec"), {
        "__name__": "__main__", "__file__": str(tmp_path / "agent.py"),
    })
    assert captured == ["project-setting"]


def test_runner_loads_project_settings_for_existing_scripts(tmp_path, monkeypatch):
    from co_studio.runner import co_studio_runner

    key = "CO_STUDIO_RUNNER_ENV_TEST"
    monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(f"{key}=existing-agent\n")
    script = tmp_path / "agent.py"
    monkeypatch.setattr("sys.argv", ["runner", str(script)])
    monkeypatch.setattr(co_studio_runner, "_patch_scoped_trust", Mock())
    monkeypatch.setattr(co_studio_runner, "_patch_get_ips", Mock())
    captured = []
    monkeypatch.setattr(co_studio_runner.runpy, "run_path", lambda *a, **kw: captured.append(os.environ.get(key)))
    co_studio_runner.main()
    assert captured == ["existing-agent"]


def test_studio_readonly_routes_and_framework_doctor():
    # No TestClient context manager: Studio lifespan would authenticate/adopt agents.
    client = TestClient(create_app(), base_url="http://localhost:9900")
    try:
        for route in ("/", "/api/agents", "/api/skills/catalog", "/api/setup/status"):
            assert client.get(route).status_code == 200
        assert client.get("/api/setup/status").json()["framework_ok"]
    finally:
        client.close()
