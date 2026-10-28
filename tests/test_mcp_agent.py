"""Real CO 1.9.2 + SDK subprocesses + scripted model; never a paid provider."""
import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from copy import deepcopy
import json
import threading
import time
from types import SimpleNamespace

import pytest
from connectonion import Agent
from connectonion.core.llm import LLMResponse, ToolCall
from connectonion.core.usage import TokenUsage
from connectonion.core.interrupt import UserInterrupt, InterruptibleIO
from connectonion.network.host.session import SessionStorage
from connectonion.network.host.session.turn import input_handler
from connectonion.useful_plugins.tool_approval.approval import check_approval

from co_studio.approvals import ApprovalStore, ToolsConflict, ToolsInvalid, save_tool_config
from co_studio.mcp_agent import attach_studio_tools, _document_call, _Gate, wrap_host_for_studio_tools
from co_studio.mcp_readonly import open_documents, McpPolicyError
from co_studio.mcp_docs_server import read_document, execute_document_tool

CONFIG = {"mcp_enabled": True, "approvals_enabled": True, "approval_timeout_seconds": 3}


def response(text="done", calls=()):
    return LLMResponse(content=text, tool_calls=list(calls), raw_response=None,
                       usage=TokenUsage(input_tokens=1, output_tokens=1, cost=0))


class ScriptedLLM:
    model = "offline-approval-test"
    def __init__(self, responses):
        self.responses = deque(responses)
    def complete(self, messages, **kwargs):
        return self.responses.popleft()


@pytest.fixture
def directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    directory = tmp_path / "demo"
    directory.mkdir()
    (directory / ".co").mkdir()
    return directory


def agent_for(directory, name, arguments, *, tools=(), handlers=(), config=None):
    llm = ScriptedLLM([response("", [ToolCall(name=name, arguments=arguments, id="call-1")]), response()])
    agent = Agent("Approval contract", system_prompt="Offline test", llm=llm, tools=list(tools),
        on_events=list(handlers), quiet=True, log=False, co_dir=directory / ".co",
        state_dir=directory / ".co", max_iterations=2)
    return attach_studio_tools(agent, agent_dir=directory, configuration=config or CONFIG, run_id="run-1")


def run(agent, **session):
    return agent.input("offline request", session={"session_id": "approval-session", **session})


def pending(store, future):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        items = store.list()["items"]
        if items:
            return items[0]
        if future.done():
            pytest.fail(f"Agent ended before requesting approval: {future.result()}")
        time.sleep(0.01)
    pytest.fail("Agent did not request approval")


def test_real_sdk_public_guides_and_input_restrictions():
    async def test():
        async with open_documents() as client:
            assert {item["name"] for item in client.tools()} == {"studio_search_docs", "studio_read_doc"}
            result = await client.call("studio_search_docs", {"query": "history", "limit": 2.0})
            assert result["source"] == "studio-public-guides"
            document = await client.call("studio_read_doc", {"document_id": "history"})
            assert document == read_document("history")
            with pytest.raises(McpPolicyError):
                await client.call("studio_read_doc", {"document_id": "../../.env"})
            with pytest.raises(McpPolicyError):
                await client.call("write_file", {"path": ".env"})
    asyncio.run(test())


def test_docs_reject_symlinks_and_oversize(tmp_path, monkeypatch):
    import co_studio.mcp_docs_server as docs
    monkeypatch.setattr(docs, "DOC_ROOT", tmp_path)
    other = tmp_path / "other.md"
    other.write_text("outside")
    guide = tmp_path / "history.md"
    guide.symlink_to(other)
    with pytest.raises(McpPolicyError):
        read_document("history")
    guide.unlink()
    guide.write_text("x" * 8001)
    with pytest.raises(McpPolicyError):
        read_document("history")


def test_real_agent_mcp_tool_native_trace_and_host_persistence(directory):
    storage = SessionStorage(directory / ".co/session_results.jsonl")
    agent = agent_for(directory, "studio_read_doc", {"document_id": "prompt-versions"})
    manifest = json.loads((directory / ".studio-tools/runtime/run-1.json").read_text())
    assert manifest["model"] == "offline-approval-test"
    assert all(item["source_status"] == "known" for item in manifest["tools"])
    assert {item["name"] for item in manifest["tools"]} == {
        "studio_search_docs", "studio_read_doc", "studio_search_knowledge"}
    result = input_handler(lambda: agent, storage, "offline", 3600, session={"session_id": "mcp-session"})
    trace = result["session"]["trace"]
    tool = next(event for event in trace if event["type"] == "tool_result")
    assert tool["status"] == "success" and "studio-public-guides" in tool["result"], tool.get("error", tool)
    assert not ApprovalStore(directory).path.exists()  # own read-only tool does not ask
    saved = json.loads(storage.path.read_text().splitlines()[-1])
    assert any(event["type"] == "tool_result" for event in saved["session"]["trace"])


@pytest.mark.parametrize("decision", ["approve", "reject", "expire"])
def test_real_agent_one_call_approval_decision(directory, decision):
    effects = []
    def record_note(text: str) -> str:
        """Record one synthetic note."""
        effects.append(text)
        return "recorded"
    agent = agent_for(directory, "record_note", {"text": "one"}, tools=[record_note],
                      config=CONFIG | {"approval_timeout_seconds": 1})
    store = ApprovalStore(directory)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(run, agent)
        item = pending(store, future)
        if decision != "expire":
            store.decide(item["id"], binding=item["binding"], decision=decision)
        future.result(timeout=5)
    expected = {"approve": "succeeded", "reject": "rejected", "expire": "expired"}[decision]
    assert store.get(item["id"])["status"] == expected
    assert effects == (["one"] if decision == "approve" else [])
    events = [event for event in agent.current_session["trace"] if event["type"] == "studio_approval"]
    assert events[0]["action"] == "requested"
    assert events[-1]["status"] == expected


def test_native_approval_bridge_and_full_access_still_asks(directory):
    effects = []
    def record_note(text: str) -> str:
        """Record a synthetic note."""
        effects.append(text)
        return "done"
    # Native check_approval is installed, but the standalone Agent has no websocket.
    agent = agent_for(directory, "record_note", {"text": "one"}, tools=[record_note], handlers=[check_approval])
    store = ApprovalStore(directory)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(run, agent, mode="full-access")
        item = pending(store, future)
        store.decide(item["id"], binding=item["binding"], decision="approve")
        future.result(timeout=5)
    assert effects == ["one"]
    assert store.get(item["id"])["status"] == "succeeded"


def test_native_explicit_deny_cannot_be_approved(directory):
    agent = agent_for(directory, "studio_read_doc", {"document_id": "history"})
    run(agent, permissions={"studio_read_doc": {"allowed": False, "source": "config"}})
    result = next(event for event in agent.current_session["trace"] if event["type"] == "tool_result")
    assert result["status"] == "error" and result.get("declined")
    assert not ApprovalStore(directory).path.exists()


def test_pending_call_mutation_invalidates_approval(directory):
    effects = []
    def record_note(text: str) -> str:
        """Record a synthetic note."""
        effects.append(text)
        return "done"
    agent = agent_for(directory, "record_note", {"text": "one"}, tools=[record_note])
    store = ApprovalStore(directory)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(run, agent)
        item = pending(store, future)
        agent.current_session["pending_tool"]["arguments"]["text"] = "changed"
        store.decide(item["id"], binding=item["binding"], decision="approve")
        future.result(timeout=5)
    assert effects == [] and store.get(item["id"])["status"] == "cancelled"


def test_owned_mcp_operation_honors_io_cancellation(directory, monkeypatch):
    import co_studio.mcp_readonly as module
    async def test():
        entered, closed = asyncio.Event(), asyncio.Event()
        class Client:
            async def call(self, *args):
                entered.set()
                await asyncio.Event().wait()
        @asynccontextmanager
        async def connection(limits=None, *, on_closing=None):
            try:
                yield Client()
            finally:
                if on_closing:
                    on_closing()
                closed.set()
        monkeypatch.setattr(module, "open_documents", connection)
        io = InterruptibleIO(SimpleNamespace(send=lambda event: None))
        task = asyncio.create_task(_document_call(SimpleNamespace(io=io), "studio_read_doc", {"document_id": "history"}))
        await asyncio.wait_for(entered.wait(), 1)
        io.cancel()
        with pytest.raises(UserInterrupt):
            await asyncio.wait_for(task, 1)
        assert closed.is_set()
    asyncio.run(test())


def test_duplicate_readonly_name_fails_closed(directory):
    def studio_read_doc(document_id: str) -> str:
        """A different function must never gain the trusted bypass."""
        return "different"
    with pytest.raises(ToolsConflict):
        agent_for(directory, "studio_read_doc", {"document_id": "history"}, tools=[studio_read_doc])


def test_wrapper_disabled_is_exact_noop(directory):
    host = lambda factory, **kwargs: factory
    assert wrap_host_for_studio_tools(host, directory / "agent.py") is host


def test_public_knowledge_never_exposes_collection_parameter(monkeypatch):
    from co_studio.mcp_agent import studio_search_knowledge
    import co_studio.knowledge as knowledge
    calls = []
    class Store:
        def search(self, query, **kwargs):
            calls.append((query, kwargs))
            return {"hits": []}
    monkeypatch.setattr(knowledge, "configured_store", lambda: Store())
    assert studio_search_knowledge("history") == {"hits": []}
    assert calls == [("history", {"collection": "public", "mode": "hybrid", "k": 5})]


@pytest.mark.parametrize("arguments", [{"a": "not-an-integer"}, {"a": True}, {},
                                      {"a": 1, "unexpected": 2}, {"a": float("nan")}])
def test_local_schema_failure_never_requests_or_runs_tool(directory, arguments):
    effects = []
    def add_one(a: int) -> int:
        """Add one to the provided integer."""
        effects.append(a)
        return a + 1
    agent = agent_for(directory, "add_one", arguments, tools=[add_one])
    run(agent)
    assert effects == []
    result = next(event for event in agent.current_session["trace"] if event["type"] == "tool_result")
    assert result["status"] == "error" and result["declined"]
    assert result["error_type"] == "ToolsInvalid"
    assert not ApprovalStore(directory).path.exists()


def test_real_native_dialog_is_satisfied_only_after_local_owner_decides(directory):
    effects = []
    def record_note(text: str) -> str:
        """Record one synthetic note."""
        effects.append(text)
        return "recorded"
    agent = agent_for(directory, "record_note", {"text": "one"}, tools=[record_note], handlers=[check_approval])
    store = ApprovalStore(directory)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(run, agent, mode="read-only")
        row = pending(store, future)
        assert not effects
        store.decide(row["id"], binding=row["binding"], decision="approve")
        future.result(timeout=5)
    assert effects == ["one"]
    assert agent.current_session.get("approval", {}).get("approved_tools", {}) == {}


def test_scoped_rag_tool_schema_has_no_scope_escape(directory, monkeypatch):
    import co_studio.knowledge as knowledge
    from co_studio.knowledge_sync import collection_for
    calls = []
    class Store:
        def search(self, query, **kwargs):
            calls.append(kwargs)
            return {"hits": [{"chunk_id": kwargs["collection"], "content": "evidence", "score": 1}]}
    monkeypatch.setattr(knowledge, "configured_store", lambda: Store())
    agent = agent_for(directory, "studio_search_knowledge", {"query": "history"})
    schema = agent.tools.get("studio_search_knowledge").get_parameters_schema()
    assert set(schema["properties"]) == {"query", "summary"}
    run(agent)
    assert [item["collection"] for item in calls] == ["public", collection_for("demo")]
    event = next(event for event in agent.current_session["trace"] if event["type"] == "tool_result")
    assert event["status"] == "success" and "scope_fusion" in event["result"]


def test_owned_readonly_tool_does_not_override_existing_policy_deny(directory):
    from connectonion.core.events import before_each_tool
    @before_each_tool
    def deny(agent):
        agent.current_session["pending_tool"]["approval_policy"] = {"decision": "deny", "reason": "test"}
    agent = agent_for(directory, "studio_read_doc", {"document_id": "history"}, handlers=[deny])
    run(agent)
    result = next(event for event in agent.current_session["trace"] if event["type"] == "tool_result")
    assert result["status"] == "error" and "denied" in result["error"]


def test_tool_body_failure_is_durable(directory):
    def fail_task(value: str) -> str:
        """Fail without side effects."""
        raise RuntimeError("synthetic failure")
    agent = agent_for(directory, "fail_task", {"value": "x"}, tools=[fail_task])
    manifest = json.loads((directory / ".studio-tools/runtime/run-1.json").read_text())
    local = next(item for item in manifest["tools"] if item["name"] == "fail_task")
    assert local["source_status"] == "known" and len(local["source_sha256"]) == 64
    store = ApprovalStore(directory)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(run, agent)
        row = pending(store, future)
        store.decide(row["id"], binding=row["binding"], decision="approve")
        future.result(timeout=5)
    assert store.get(row["id"])["status"] == "failed"


def test_pending_approval_interrupt_cancels_without_effect(directory):
    effects = []
    interrupted = threading.Event()
    class IO:
        def send(self, event):
            pass
        def receive_all(self, message_type=None):
            if message_type == "INTERRUPT" and interrupted.is_set():
                interrupted.clear()
                return [{"type": "INTERRUPT"}]
            return []
    def record_note(text: str) -> str:
        """Record one synthetic note."""
        effects.append(text)
        return "recorded"
    agent = agent_for(directory, "record_note", {"text": "one"}, tools=[record_note])
    agent.io = IO()
    store = ApprovalStore(directory)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(run, agent)
        row = pending(store, future)
        interrupted.set()
        future.result(timeout=5)
    assert not effects
    assert store.get(row["id"])["status"] == "cancelled"


def test_prompt_context_tools_factory_composition_and_frozen_settings(directory):
    from co_studio.context_policy import ContextPolicy
    from co_studio.context_runtime import wrap_host_for_context
    from co_studio.prompt_versions import PromptVersions, wrap_host_for_prompt
    script = directory / "agent.py"
    source = '''"""Demo — generated by ConnectOnion Studio."""
from connectonion import Agent
SYSTEM_PROMPT = "Original instructions"
def create_agent():
    return Agent("demo", system_prompt=SYSTEM_PROMPT, llm=OFFLINE, quiet=True,
                 log=False, co_dir=CO_DIR, state_dir=CO_DIR, max_iterations=2)
'''
    script.write_text(source)
    (directory / "meta.json").write_text(json.dumps({"slug": "demo", "preset": "custom"}))
    save_tool_config(directory, 0, mcp_enabled=True, approvals_enabled=True,
                     approval_timeout_seconds=30, context_budget=8000)
    prompts = PromptVersions(directory)
    revision = prompts.save("baseline", "Selected custom base prompt")["current"]
    namespace = {"__file__": str(script), "CO_DIR": directory / ".co", "OFFLINE": ScriptedLLM([
        response("", [ToolCall(name="studio_read_doc", arguments={"document_id": "history"}, id="c1")]), response()])}
    exec(compile(source, str(script), "exec"), namespace)
    wrapper = wrap_host_for_studio_tools(lambda factory, **kwargs: factory(), script)
    wrapper = wrap_host_for_context(wrapper, script, policy=ContextPolicy(8000))
    wrapper = wrap_host_for_prompt(wrapper, script)
    # A running-process wrapper keeps its captured config after a later UI edit.
    save_tool_config(directory, 1, mcp_enabled=False, approvals_enabled=False,
                     approval_timeout_seconds=30, context_budget=None)
    agent = wrapper(namespace["create_agent"], co_dir=directory / ".co")
    assert "studio_read_doc" in agent.tools and agent._studio_context_attached
    run(agent)
    provenance = next(e for e in agent.current_session["trace"] if e["type"] == "prompt_provenance")
    assert provenance["version_id"] == revision["id"]
    result = next(e for e in agent.current_session["trace"] if e["type"] == "tool_result")
    assert result["status"] == "success", result.get("error")


def test_mcp_pending_capacity_refuses_before_launch(monkeypatch):
    import co_studio.mcp_agent as module
    from co_studio.approvals import ToolsError
    class Full:
        def acquire(self, blocking=False):
            return False
    monkeypatch.setattr(module, "_MCP_PENDING", Full())
    with pytest.raises(ToolsError, match="capacity"):
        asyncio.run(_document_call(None, "studio_read_doc", {"document_id": "history"}))
