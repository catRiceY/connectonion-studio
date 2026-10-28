"""Offline policy tests and real SDK stdio subprocess round trips."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("mcp", reason="Install optional mcp dependencies")
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError
from mcp.types import CallToolResult, ErrorData, ImageContent, ListToolsResult, TextContent, Tool
import psutil

from co_studio import mcp_readonly as readonly


def success(name="studio_get_guide", arguments=None):
    arguments = arguments or {"guide_id": "history"}
    if name == "studio_list_guides":
        data = {"source": "bundled-fixture", "guides": []}
    else:
        data = {"source": "bundled-fixture", "id": arguments["guide_id"],
                "title": "History", "text": "A fixed record."}
    return CallToolResult(content=[TextContent(type="text", text=json.dumps(data))], structuredContent=data)


class FakeSession:
    def __init__(self):
        self.catalog = [Tool(**definition) for definition in readonly.tool_definitions()]
        self.calls = []
        self.active = 0
        self.maximum_active = 0
        self.release = None
        self.result = None
        self.error = None

    async def initialize(self):
        return SimpleNamespace(protocolVersion="test")

    async def list_tools(self):
        return ListToolsResult(tools=self.catalog)

    async def call_tool(self, name, arguments, **kwargs):
        self.calls.append((name, deepcopy(arguments)))
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        try:
            if self.release is not None:
                await self.release.wait()
            if self.error is not None:
                raise self.error
            return self.result or success(name, arguments)
        finally:
            self.active -= 1


async def make_client(session=None, **limits):
    session = session or FakeSession()
    client = readonly.ReadonlyMcpClient(session, readonly.ReadonlyLimits(**limits))
    await client._discover()
    return client, session


@pytest.mark.parametrize("field,value", [("timeout_seconds", 0), ("timeout_seconds", float("nan")),
                                        ("timeout_seconds", True), ("timeout_seconds", 61),
                                        ("max_concurrency", 0), ("max_concurrency", 9),
                                        ("max_pending", 65), ("max_pending", True),
                                        ("max_argument_bytes", 4097), ("max_result_bytes", 65537)])
def test_limits_are_bounded(field, value):
    with pytest.raises(ValueError):
        readonly.ReadonlyLimits(**{field: value})


def test_pending_budget_cannot_be_less_than_concurrency():
    with pytest.raises(ValueError):
        readonly.ReadonlyLimits(max_concurrency=2, max_pending=1)


@pytest.mark.parametrize("name,arguments", [
    ("delete_document", {}), ("shell", {"command": "echo nope"}), ("", {}),
    ("studio_get_guide", {"guide_id": "../../secret"}),
    ("studio_get_guide", {}), ("studio_get_guide", {"guide_id": "history", "path": "/tmp"}),
    ("studio_list_guides", {"limit": True}), ("studio_list_guides", {"limit": 4}),
    ("studio_list_guides", {"query": "x" * 81}), ("studio_list_guides", {"query": float("nan")}),
    ("studio_list_guides", []), ("studio_list_guides", {"query": "\ud800"}),
])
def test_invalid_calls_never_reach_transport(name, arguments):
    async def exercise():
        client, session = await make_client()
        with pytest.raises(readonly.McpPolicyError):
            await client.call(name, arguments)
        assert session.calls == []
    asyncio.run(exercise())


def test_byte_limits_and_circular_arguments_are_rejected():
    circular = {}
    circular["query"] = circular
    for data, maximum in [({"query": "界" * 10}, 20), (circular, 4096)]:
        with pytest.raises(readonly.McpPolicyError):
            readonly.validate_arguments("studio_list_guides", data, maximum)


def test_tool_descriptions_do_not_mutate_policy():
    definitions = readonly.tool_definitions()
    definitions[0]["inputSchema"]["additionalProperties"] = True
    with pytest.raises(readonly.McpPolicyError):
        readonly.validate_arguments("studio_list_guides", {"command": "no"})


def test_json_schema_integer_normalizes_integral_float():
    assert readonly.validate_arguments("studio_list_guides", {"limit": 2.0}) == {"limit": 2}
    assert type(readonly.validate_arguments("studio_list_guides", {"limit": 2.0})["limit"]) is int


@pytest.mark.parametrize("mode", ["unknown_readonly_hint", "duplicate", "missing", "schema", "writable"])
def test_discovery_fails_closed(mode):
    async def exercise():
        session = FakeSession()
        if mode == "unknown_readonly_hint":
            session.catalog[0].name = "delete_document"
            assert session.catalog[0].annotations.readOnlyHint is True
        elif mode == "duplicate":
            session.catalog[1] = session.catalog[0]
        elif mode == "missing":
            session.catalog.pop()
        elif mode == "schema":
            session.catalog[0].inputSchema = {"$ref": "https://example.invalid/schema"}
        else:
            session.catalog[0].annotations.readOnlyHint = False
        with pytest.raises(readonly.McpPolicyError):
            await make_client(session)
        assert not session.calls
    asyncio.run(exercise())


def test_annotations_are_not_required_for_local_authorization():
    async def exercise():
        session = FakeSession()
        for tool in session.catalog:
            tool.annotations = None
        client, _ = await make_client(session)
        assert (await client.call("studio_get_guide", {"guide_id": "history"}))["id"] == "history"
    asyncio.run(exercise())


@pytest.mark.parametrize("mode", ["oversize", "error", "image", "missing", "schema", "wrong_id"])
def test_result_limits_and_local_schema(mode):
    async def exercise():
        client, session = await make_client()
        result = success()
        if mode == "oversize":
            result.content[0].text = "x" * 17000
        elif mode == "error":
            result.isError = True
        elif mode == "image":
            result.content = [ImageContent(type="image", data="AA==", mimeType="image/png")]
        elif mode == "missing":
            result.structuredContent = None
        elif mode == "schema":
            result.structuredContent["secret"] = "not permitted"
        else:
            result.structuredContent["id"] = "prompt-versions"
        session.result = result
        with pytest.raises(readonly.McpReadonlyError):
            await client.call("studio_get_guide", {"guide_id": "history"})
        assert len(session.calls) == 1
    asyncio.run(exercise())


def test_bounded_running_and_pending_calls():
    async def exercise():
        client, session = await make_client(max_concurrency=2, max_pending=3)
        session.release = asyncio.Event()
        tasks = [asyncio.create_task(client.call("studio_get_guide", {"guide_id": "history"})) for _ in range(3)]
        for _ in range(3):
            await asyncio.sleep(0)
        assert session.active == 2
        with pytest.raises(readonly.McpBusyError):
            await client.call("studio_get_guide", {"guide_id": "history"})
        session.release.set()
        assert len(await asyncio.gather(*tasks)) == 3
        assert session.maximum_active == 2
        assert not client._tasks
    asyncio.run(exercise())


def test_deadline_includes_semaphore_queue_and_invalidates_connection():
    async def exercise():
        client, session = await make_client(timeout_seconds=0.03, max_concurrency=1)
        session.release = asyncio.Event()
        tasks = [asyncio.create_task(client.call("studio_get_guide", {"guide_id": "history"})) for _ in range(2)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(isinstance(result, readonly.McpReadonlyError) for result in results)
        assert any(isinstance(result, readonly.McpTimeoutError) for result in results)
        assert len(session.calls) == 1
        assert not client._tasks
        with pytest.raises(readonly.McpReadonlyError, match="not open"):
            await client.call("studio_get_guide", {"guide_id": "history"})
    asyncio.run(exercise())


@pytest.mark.parametrize("error", [McpError(ErrorData(code=408, message="secret timeout")),
                                  RuntimeError("secret provider details")])
def test_sdk_failure_is_sanitized_and_connection_not_reused(error):
    async def exercise():
        client, session = await make_client()
        session.error = error
        with pytest.raises(readonly.McpReadonlyError) as raised:
            await client.call("studio_get_guide", {"guide_id": "history"})
        assert "secret" not in str(raised.value)
        if isinstance(error, McpError):
            assert isinstance(raised.value, readonly.McpTimeoutError)
        with pytest.raises(readonly.McpReadonlyError, match="not open"):
            await client.call("studio_get_guide", {"guide_id": "history"})
    asyncio.run(exercise())


def test_discovery_timeout():
    async def exercise():
        session = FakeSession()
        async def never():
            await asyncio.Event().wait()
        session.initialize = never
        with pytest.raises(readonly.McpTimeoutError):
            await make_client(session, timeout_seconds=0.01)
    asyncio.run(exercise())


def test_close_cancels_inflight_and_waiting_calls():
    async def exercise():
        client, session = await make_client(max_concurrency=1)
        session.release = asyncio.Event()
        tasks = [asyncio.create_task(client.call("studio_get_guide", {"guide_id": "history"})) for _ in range(2)]
        for _ in range(3):
            await asyncio.sleep(0)
        await client._close()
        assert all(task.cancelled() for task in tasks)
        assert not client._tasks and session.active == 0
        with pytest.raises(readonly.McpReadonlyError):
            client.tools()
    asyncio.run(exercise())


def test_cancel_before_child_starts_invalidates_connection():
    async def exercise():
        client, session = await make_client()
        caller = asyncio.create_task(client.call("studio_get_guide", {"guide_id": "history"}))
        await asyncio.sleep(0)  # call created its operation; operation has not run
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert client._closed
        await client._close()
        assert not client._tasks
    asyncio.run(exercise())


def test_cancelled_caller_does_not_drop_unfinished_operation():
    async def exercise():
        client, session = await make_client()
        started, release = asyncio.Event(), asyncio.Event()
        async def delayed_cancel(name, arguments, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
            return success(name, arguments)
        session.call_tool = delayed_cancel
        caller = asyncio.create_task(client.call("studio_get_guide", {"guide_id": "history"}))
        await started.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert client._closed
        assert len(client._tasks) == 1
        assert not next(iter(client._tasks)).done()
        release.set()
        await asyncio.gather(*client._tasks)
        assert not client._tasks
    asyncio.run(exercise())


def fixture_processes():
    found = []
    for process in psutil.Process().children(recursive=True):
        try:
            if "co_studio.mcp_fixture_server" in process.cmdline():
                found.append(process)
        except psutil.NoSuchProcess:
            pass
    return found


def test_real_sdk_stdio_roundtrip_and_process_cleanup(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-inherited")
    monkeypatch.setenv("PYTHONPATH", "/invalid/untrusted")
    async def exercise():
        async with readonly.open_fixture() as client:
            children = fixture_processes()
            assert len(children) == 1
            assert "OPENAI_API_KEY" not in children[0].environ()
            assert "PYTHONPATH" not in children[0].environ()
            assert client.protocol_version
            assert {item["name"] for item in client.tools()} == {"studio_list_guides", "studio_get_guide"}
            listing = await client.call("studio_list_guides", {"query": "prompt", "limit": 1})
            assert listing["guides"][0]["id"] == "prompt-versions"
            results = await asyncio.gather(*[
                client.call("studio_get_guide", {"guide_id": guide_id})
                for guide_id in readonly.REFERENCE_IDS])
            assert [item["id"] for item in results] == list(readonly.REFERENCE_IDS)
            assert all(item["source"] == "bundled-fixture" for item in results)
        assert not fixture_processes()
        assert all(not child.is_running() for child in children)
    asyncio.run(exercise())


def test_real_server_also_rejects_bypassed_client_policy():
    async def exercise():
        parameters = StdioServerParameters(command=sys.executable,
                                           args=["-I", "-m", "co_studio.mcp_fixture_server"])
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                await client.list_tools()
                invalid = await client.call_tool("studio_get_guide", {"guide_id": "../../secret"})
                unknown = await client.call_tool("delete_document", {})
                assert invalid.isError and unknown.isError
                integral = await client.call_tool("studio_list_guides", {"limit": 2.0})
                assert not integral.isError
                assert len(integral.structuredContent["guides"]) == 2
        assert not fixture_processes()
    asyncio.run(exercise())


def test_real_context_cleanup_after_policy_error():
    async def exercise():
        with pytest.raises(readonly.McpPolicyError):
            async with readonly.open_fixture() as client:
                await client.call("delete_document", {})
        assert not fixture_processes()
    asyncio.run(exercise())


def test_real_context_cleanup_after_caller_cancellation():
    async def exercise():
        ready = asyncio.Event()
        async def owner():
            async with readonly.open_fixture():
                ready.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(owner())
        await asyncio.wait_for(ready.wait(), 5)
        assert fixture_processes()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not fixture_processes()
    asyncio.run(exercise())


def test_real_process_cleanup_when_initialize_deadline_expires(monkeypatch):
    original = ClientSession.initialize
    async def stalled_after_handshake(self):
        await original(self)
        await asyncio.Event().wait()
    monkeypatch.setattr(ClientSession, "initialize", stalled_after_handshake)
    async def exercise():
        with pytest.raises(readonly.McpTimeoutError):
            async with readonly.open_fixture(readonly.ReadonlyLimits(timeout_seconds=0.1)):
                pytest.fail("Discovery should not complete")
        assert not fixture_processes()
    asyncio.run(exercise())


def test_real_process_cleanup_after_tool_wait_timeout(monkeypatch):
    original = ClientSession.call_tool
    async def stalled_after_response(self, *args, **kwargs):
        await original(self, *args, **kwargs)
        await asyncio.Event().wait()
    monkeypatch.setattr(ClientSession, "call_tool", stalled_after_response)
    async def exercise():
        with pytest.raises(readonly.McpTimeoutError):
            async with readonly.open_fixture(readonly.ReadonlyLimits(timeout_seconds=1)) as client:
                await client.call("studio_get_guide", {"guide_id": "history"})
        assert not fixture_processes()
    asyncio.run(exercise())


def test_context_does_not_wait_for_callers_unrelated_work(monkeypatch):
    started = None
    original = ClientSession.call_tool
    async def stall(self, *args, **kwargs):
        await original(self, *args, **kwargs)
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(ClientSession, "call_tool", stall)

    async def exercise():
        nonlocal started
        started = asyncio.Event()
        release_caller = asyncio.Event()
        async def caller(client):
            try:
                await client.call("studio_get_guide", {"guide_id": "history"})
            except asyncio.CancelledError:
                await release_caller.wait()
        worker = None
        try:
            async with asyncio.timeout(5):
                async with readonly.open_fixture() as client:
                    worker = asyncio.create_task(caller(client))
                    await started.wait()
                assert not fixture_processes()
                assert not worker.done()
        finally:
            release_caller.set()
            if worker:
                await worker
    asyncio.run(exercise())


def test_cli_is_honestly_labeled_and_closes_connection():
    completed = subprocess.run([sys.executable, "-m", "co_studio.mcp_readonly"],
                               cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["mode"] == "bundled-fixture-only"
    assert report["connection_closed"] is True
    assert len(report["tools"]) == 2
    assert "No user documents" in report["note"]
