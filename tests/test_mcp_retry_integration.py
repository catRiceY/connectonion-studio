"""Real stdio processes and CO Host, with offline failure injection only."""
import asyncio
from collections import deque
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace

from connectonion import Agent
from connectonion.core.llm import LLMResponse, ToolCall
from connectonion.core.interrupt import InterruptibleIO, UserInterrupt
from connectonion.network.host.session import SessionStorage
from connectonion.network.host.session.turn import input_handler
from mcp import ClientSession
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, CONNECTION_CLOSED
import anyio
import psutil
import pytest

from co_studio import mcp_agent, mcp_readonly as readonly, mcp_retry as retry
from co_studio.execution_adapter import convert_snapshot


def document_processes():
    found = []
    for process in psutil.Process().children(recursive=True):
        try:
            if "co_studio.mcp_docs_server" in process.cmdline():
                found.append(process)
        except psutil.NoSuchProcess:
            pass
    return found


@pytest.mark.parametrize("error,kind", [
    (TimeoutError("private"), readonly.McpTimeoutError),
    (McpError(ErrorData(code=408, message="private")), readonly.McpTimeoutError),
    (McpError(ErrorData(code=CONNECTION_CLOSED, message="private")), readonly.McpTransportError),
    (ConnectionResetError("private"), readonly.McpTransportError),
    (ConnectionAbortedError("private"), readonly.McpTransportError),
    (BrokenPipeError("private"), readonly.McpTransportError),
    (anyio.EndOfStream(), readonly.McpTransportError),
    (anyio.BrokenResourceError(), readonly.McpTransportError),
    (anyio.ClosedResourceError(), readonly.McpTransportError),
    (ExceptionGroup("private", [BrokenPipeError(), anyio.EndOfStream()]), readonly.McpTransportError),
    (ExceptionGroup("private", [BrokenPipeError(), TimeoutError()]), readonly.McpTimeoutError),
    (ExceptionGroup("private", [BrokenPipeError(), ValueError()]), readonly.McpReadonlyError),
    (ExceptionGroup("private", [TimeoutError(), readonly.McpPolicyError()]), readonly.McpReadonlyError),
    (FileNotFoundError("private"), readonly.McpReadonlyError),
    (PermissionError("private"), readonly.McpReadonlyError),
    (RuntimeError("private"), readonly.McpReadonlyError),
    (McpError(ErrorData(code=-32602, message="private")), readonly.McpReadonlyError),
])
def test_exception_classification_is_narrow_and_sanitized(error, kind):
    converted = readonly._transport_error(error, "call")
    assert type(converted) is kind
    assert "private" not in str(converted)


@pytest.mark.parametrize("failure,status", [(ConnectionResetError("private"), "transport_error"),
                                           (TimeoutError("private"), "timeout")])
def test_real_host_retry_is_persisted_and_processes_never_overlap(tmp_path, monkeypatch, failure, status):
    original = ClientSession.call_tool
    pids = []

    async def fail_first(self, *args, **kwargs):
        live = document_processes()
        assert len(live) == 1
        assert all(not process.is_running() for process in pids)
        pids.append(live[0])
        result = await original(self, *args, **kwargs)
        if len(pids) == 1:
            # The read may already have executed: replay is permitted only
            # because these shipped operations have no writes/side effects.
            raise failure
        return result

    monkeypatch.setattr(ClientSession, "call_tool", fail_first)
    directory = tmp_path / "retry-agent"
    directory.mkdir()
    responses = deque([
        LLMResponse(content="", tool_calls=[ToolCall(name="studio_read_doc", arguments={"document_id": "history"},
                                                   id="retry-call-1")], raw_response=None, usage=None),
        LLMResponse(content="offline result", tool_calls=[], raw_response=None, usage=None),
    ])

    class Model:
        model = "offline-retry-contract"
        def complete(self, messages, **kwargs):
            return responses.popleft()

    agent = Agent("Retry contract", llm=Model(), system_prompt="Offline", quiet=True, log=False,
                  co_dir=directory / ".co", state_dir=directory / ".co", max_iterations=2)
    mcp_agent.attach_studio_tools(agent, agent_dir=directory,
                                 configuration={"mcp_enabled": True, "approvals_enabled": True,
                                                "approval_timeout_seconds": 3}, run_id="retry-run")
    storage = SessionStorage(directory / ".co/session_results.jsonl")
    result = input_handler(lambda: agent, storage, "offline request", 3600, session={"session_id": "retry-session"})
    saved = json.loads(storage.path.read_text().splitlines()[-1])
    for snapshot in (result, saved):
        events = snapshot["session"]["trace"]
        attempts = [item for item in events if item["type"] == "studio_mcp_attempt"]
        assert [item["attempt"] for item in attempts] == [1, 2]
        assert [item["status"] for item in attempts] == [status, "success"]
        assert [item["retry_scheduled"] for item in attempts] == [True, False]
        assert all(item["tool_id"] == "retry-call-1" for item in attempts)
        assert "private" not in json.dumps(attempts)
        results = [item for item in events if item["type"] == "tool_result"]
        assert len(results) == 1 and results[0]["status"] == "success"
    mapped = convert_snapshot(saved, agent_slug="retry-agent")
    attempts = [item for item in mapped.events if item.kind == "studio_mcp_attempt"]
    assert [item.call_id for item in attempts] == ["retry-call-1", "retry-call-1"]
    assert [item.payload["attempt"] for item in attempts] == [1, 2]
    assert all(item.duration_ms == item.payload["elapsed_ms"] for item in attempts)
    assert len(pids) == 2 and not document_processes()
    assert all(not process.is_running() for process in pids)


def test_real_stdio_cancellation_cleans_process_before_return(monkeypatch):
    original = ClientSession.call_tool

    async def exercise():
        started = asyncio.Event()
        observations = []
        async def stalled(self, *args, **kwargs):
            await original(self, *args, **kwargs)
            started.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(ClientSession, "call_tool", stalled)
        task = asyncio.create_task(retry.call_document_with_retry(
            "studio_read_doc", {"document_id": "history"}, on_attempt=observations.append))
        await asyncio.wait_for(started.wait(), 3)
        children = document_processes()
        assert len(children) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 4)
        assert not document_processes()
        assert all(not child.is_running() for child in children)
        assert [row["status"] for row in observations] == ["cancelled"]
    asyncio.run(exercise())


def test_transient_failure_followed_by_shutdown_error_cannot_retry(monkeypatch):
    original = ClientSession.__aexit__
    enters = []
    async def failed_call(self, *args, **kwargs):
        enters.append(self)
        raise ConnectionResetError("private")
    async def broken_exit(self, *args):
        await original(self, *args)
        raise RuntimeError("private cleanup failure")
    monkeypatch.setattr(ClientSession, "call_tool", failed_call)
    monkeypatch.setattr(ClientSession, "__aexit__", broken_exit)
    observations = []
    with pytest.raises(readonly.McpTransportError) as raised:
        asyncio.run(retry.call_document_with_retry("studio_read_doc", {"document_id": "history"},
                                                  on_attempt=observations.append))
    assert raised.value.cleanup_verified is False
    assert len(enters) == 1 and not document_processes()
    assert observations[0]["retry_scheduled"] is False


def test_queue_wait_uses_total_budget_and_does_not_open_connection(monkeypatch):
    class NoSlots:
        def acquire(self, **kwargs):
            return False
        def release(self):
            pytest.fail("No slot was acquired")
    monkeypatch.setattr(mcp_agent, "_MCP_SLOTS", NoSlots())
    monkeypatch.setattr(retry, "TOTAL_TIMEOUT_SECONDS", 0.01)
    @asynccontextmanager
    async def forbidden(*args, **kwargs):
        pytest.fail("Queue timeout must not open a connection")
        yield
    monkeypatch.setattr(readonly, "open_documents", forbidden)
    with pytest.raises(mcp_agent.ToolsError, match="timed out"):
        asyncio.run(mcp_agent._document_call(None, "studio_read_doc", {"document_id": "history"}))
    assert mcp_agent._MCP_PENDING._value == 8


def test_io_cancel_at_completion_is_not_a_success(monkeypatch):
    io = InterruptibleIO(SimpleNamespace(send=lambda event: None))
    async def completed(*args, **kwargs):
        io.cancel()
        return {"ignored": "late result"}
    monkeypatch.setattr(retry, "call_document_with_retry", completed)
    with pytest.raises(UserInterrupt):
        asyncio.run(mcp_agent._document_call(SimpleNamespace(io=io), "studio_read_doc", {"document_id": "history"}))
    assert mcp_agent._MCP_PENDING._value == 8


@pytest.mark.parametrize("change", ["session", "turn", "call", "terminal"])
def test_late_attempt_observation_cannot_enter_a_changed_call_scope(monkeypatch, change):
    old = {"session_id": "original", "turn": 1, "_active_tool_call_id": "call-1",
           "trace": [{"type": "tool_call", "name": "studio_read_doc", "tool_id": "call-1"}]}
    records = []
    agent = SimpleNamespace(current_session=old, io=None, _record_trace=records.append)
    async def late_observer(*args, on_attempt, **kwargs):
        if change == "session":
            agent.current_session = {"session_id": "different", "turn": 1, "trace": []}
        elif change == "turn":
            old["turn"] = 2
        elif change == "call":
            old["_active_tool_call_id"] = "call-2"
        else:
            old["trace"].append({"type": "turn_result", "turn": 1})
        on_attempt({"attempt": 1, "max_attempts": 2, "status": "success", "retry_scheduled": False,
                    "error_type": None, "elapsed_ms": 1})
        return {}
    monkeypatch.setattr(retry, "call_document_with_retry", late_observer)
    asyncio.run(mcp_agent._document_call(agent, "studio_read_doc", {"document_id": "history"}))
    assert records == []
