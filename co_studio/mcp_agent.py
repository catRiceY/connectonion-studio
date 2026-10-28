"""Opt-in custom-Agent tools and local one-call approvals, without patching CO."""
from __future__ import annotations

import asyncio
from copy import copy, deepcopy
import functools
import inspect
from pathlib import Path
import threading
import time
import uuid

from .approvals import (ApprovalStore, ToolsConflict, ToolsError, ToolsInvalid,
                        call_binding, canonical, check_custom, tool_config)

_MCP_SLOTS = threading.BoundedSemaphore(4)
_MCP_PENDING = threading.BoundedSemaphore(8)
_TOOL_NAMES = ("studio_search_docs", "studio_read_doc", "studio_search_knowledge")


async def _document_call(agent, name, arguments):
    from connectonion.core.interrupt import UserInterrupt
    from .mcp_readonly import McpReadonlyError
    from . import mcp_retry

    loop = asyncio.get_running_loop()
    deadline = loop.time() + mcp_retry.TOTAL_TIMEOUT_SECONDS
    session = getattr(agent, "current_session", None)
    # Pinned CO 1.9.2 removes pending_tool after permission hooks, then sets
    # this identity on the actual (possibly transactional) tool-session copy.
    call_id = session.get("_active_tool_call_id") if isinstance(session, dict) else None
    if not isinstance(call_id, str) or not any(
        item.get("type") == "tool_call" and item.get("tool_id") == call_id and item.get("name") == name
        for item in session.get("trace", [])
    ):
        call_id = None
    turn = session.get("turn") if isinstance(session, dict) else None
    active = True

    def cancelled():
        io = getattr(agent, "io", None)
        check = getattr(type(io), "is_cancelled", None)
        return bool(check and check(io))

    def observe(record):
        # Do not append a delayed worker observation to a later turn/session.
        current = getattr(agent, "current_session", None)
        if (not active or not call_id or current is not session or current.get("turn") != turn
                or current.get("_active_tool_call_id") != call_id
                or any(item.get("type") == "turn_result" and item.get("turn") == turn
                       for item in current.get("trace", []))):
            return
        agent._record_trace({"type": "studio_mcp_attempt", "tool_id": call_id,
                             "name": name, "turn": turn,
                             "duration_ms": record["elapsed_ms"], **record})

    if not _MCP_PENDING.acquire(blocking=False):
        raise ToolsError("Studio MCP pending-call capacity reached")

    async def work():
        acquired = False
        try:
            while not acquired:
                if cancelled():
                    raise asyncio.CancelledError()
                if loop.time() >= deadline:
                    raise TimeoutError()
                acquired = _MCP_SLOTS.acquire(blocking=False)
                if not acquired:
                    await asyncio.sleep(min(0.05, max(0, deadline - loop.time())))
            return await mcp_retry.call_document_with_retry(
                name, arguments, deadline=deadline, on_attempt=observe, is_cancelled=cancelled)
        finally:
            if acquired:
                _MCP_SLOTS.release()

    task = asyncio.create_task(work())
    try:
        while not task.done():
            if cancelled():
                raise UserInterrupt()
            await asyncio.wait({task}, timeout=0.05)
        if cancelled():
            raise UserInterrupt()
        return await task
    except (McpReadonlyError, TimeoutError):
        raise ToolsError("Studio MCP document request failed or timed out") from None
    finally:
        try:
            interrupted = await mcp_retry.cancel_and_wait(task)
        finally:
            active = False
            _MCP_PENDING.release()
        if interrupted:
            raise asyncio.CancelledError()


async def studio_search_docs(query: str, limit: int = 4, agent=None) -> dict:
    """Search shipped Studio operation guides (not user files or the RAG index)."""
    return await _document_call(agent, "studio_search_docs", {"query": query, "limit": limit})


async def studio_read_doc(document_id: str, agent=None) -> dict:
    """Read a shipped guide: history, prompt-versions, tool-safety, or connectivity."""
    return await _document_call(agent, "studio_read_doc", {"document_id": document_id})


def studio_search_knowledge(query: str, *, _agent_slug=None) -> dict:
    """Internal search helper; private scope never appears in a registered schema."""
    if not isinstance(query, str) or not query.strip() or len(query) > 500:
        raise ToolsInvalid("Knowledge query must contain 1–500 characters")
    try:
        from .knowledge import configured_store
        store = configured_store()
        public = store.search(query, collection="public", mode="hybrid", k=5)
        if _agent_slug is None:
            return public
        from .knowledge_sync import collection_for
        own = store.search(query, collection=collection_for(_agent_slug), mode="hybrid", k=5)
        hits = {}
        for scope, result in (("public", public), ("agent", own)):
            for rank, hit in enumerate(result["hits"], 1):
                key = hit["chunk_id"]
                entry = hits.setdefault(key, {**hit, "scope": scope, "scope_score": 0.0})
                entry["scope_score"] += 1 / (60 + rank)
        selected = sorted(hits.values(), key=lambda item: (-item["scope_score"], item["chunk_id"]))[:5]
        result = {"query": query, "mode": "hybrid", "scopes": ["public", "agent"],
                  "scope_fusion": "rrf-60", "hits": selected}
        canonical(result)  # Same 16 KiB finite JSON ceiling as local calls.
        return result
    except Exception:
        raise ToolsError("Scoped knowledge search is unavailable; configure the index first") from None


def _knowledge_tool(agent_slug):
    def studio_search_knowledge(query: str) -> dict:
        """Search public documentation and this Agent's own indexed history with citations."""
        return globals()["studio_search_knowledge"](query, _agent_slug=agent_slug)
    return studio_search_knowledge


def _validate_pending(agent, pending):
    """Validate before permission requests or side effects, using CO's local schema."""
    from jsonschema import Draft202012Validator
    from referencing import Registry
    from referencing.exceptions import NoSuchResource
    tool = agent.tools.get(pending.get("name"))
    if tool is None:
        raise ToolsInvalid("Tool is not registered")
    arguments = pending.get("arguments")
    if type(arguments) is not dict:
        raise ToolsInvalid("Tool arguments must be a JSON object")
    canonical(arguments)
    schema = deepcopy(tool.get_parameters_schema())
    # CO injects a provider-only display summary then strips it before execution.
    # Validate the actual function arguments, not that already-consumed envelope.
    if not getattr(tool, "_summary_is_function_argument", False):
        schema.get("properties", {}).pop("summary", None)
        schema["required"] = [key for key in schema.get("required", []) if key != "summary"]
    schema.setdefault("additionalProperties", False)
    def deny_reference(uri):
        raise NoSuchResource(ref=uri)
    try:
        valid = Draft202012Validator(schema, registry=Registry(retrieve=deny_reference)).is_valid(arguments)
    except Exception:
        raise ToolsInvalid("Tool argument schema cannot be validated locally") from None
    if not valid:
        raise ToolsInvalid("Tool arguments do not match the registered schema")


class _ApprovalIO:
    """Adapt only CO's approval dialog; do not change its hard denial checks."""
    def __init__(self, original, pending, authorize):
        self.original = original
        self.pending = pending
        self.authorize = authorize
        self.requested = False

    def __getattr__(self, name):
        if self.original is None:
            raise AttributeError(name)
        return getattr(self.original, name)

    def send(self, event):
        if event.get("type") == "approval_needed":
            if (event.get("tool_call_id") != self.pending["id"]
                    or event.get("tool") != self.pending["name"]
                    or canonical(event.get("arguments")) != canonical(self.pending["arguments"])):
                raise ToolsConflict("Framework approval does not match the pending call")
            self.requested = True
        elif self.original is not None:
            self.original.send(event)

    def receive(self, *args, **kwargs):
        if not self.requested:
            raise ToolsError("Unexpected approval response request")
        self.authorize()
        return {"approved": True, "scope": "once"}


class _Gate:
    def __init__(self, agent_dir, config, safe_tools, *, native_installed, run_id):
        self.store = ApprovalStore(agent_dir)
        self.config = config
        self.safe_tools = safe_tools
        self.native_installed = native_installed
        self.run_id = run_id
        self.active: dict[tuple[int, str], str] = {}

    @staticmethod
    def _trace(agent, record, action):
        agent._record_trace({"type": "studio_approval", "approval_id": record["id"],
                             "action": action, "tool_id": record["call_id"], "name": record["tool_name"],
                             "status": record["status"], "turn": record["turn_no"]})

    @staticmethod
    def _interrupted(agent):
        from connectonion.core.interrupt import _take_interrupt
        io = getattr(agent, "io", None)
        return bool(io is not None and hasattr(io, "receive_all") and _take_interrupt(io))

    def before(self, agent):
        from connectonion.core.interrupt import UserInterrupt
        from connectonion.useful_plugins.tool_approval.approval import check_approval
        pending = agent.current_session.get("pending_tool")
        if not isinstance(pending, dict):
            raise ToolsInvalid("Missing pending tool identity")
        _validate_pending(agent, pending)
        name = pending.get("name")
        safe = name in self.safe_tools and agent.tools.get(name) is self.safe_tools[name]
        if safe:
            # Hard CO control-file/explicit-deny checks still precede this allow.
            view = copy(agent)
            policy = pending.get("approval_policy")
            if not isinstance(policy, dict) or policy.get("decision") != "deny":
                policy = {"decision": "allow", "reason": "Studio-owned read-only tool"}
            view.current_session = {**agent.current_session, "pending_tool": {**pending, "approval_policy": policy}}
            check_approval(view)
            return
        if not self.config["approvals_enabled"]:
            if self.native_installed:
                check_approval(agent)
            return
        session = agent.current_session
        identity = {"run_id": self.run_id, "session_id": session.get("session_id"),
                    "turn_no": session.get("turn"), "call_id": pending.get("id"),
                    "tool_name": name, "arguments": deepcopy(pending.get("arguments"))}
        record = None
        consumed = False

        def authorize():
            nonlocal record, consumed
            current = {**identity, "arguments": pending.get("arguments"),
                       "call_id": pending.get("id"), "tool_name": pending.get("name")}
            binding = call_binding(self.store.agent_slug, **current)
            if binding != call_binding(self.store.agent_slug, **identity):
                raise ToolsConflict("Tool arguments changed during approval")
            if consumed:
                return
            record = self.store.request(**identity, ttl=self.config["approval_timeout_seconds"])
            self._trace(agent, record, "requested")
            if getattr(agent, "storage", None):
                agent.storage.checkpoint(agent.current_session)
            while True:
                if self._interrupted(agent):
                    record = self.store.finish(record["id"], "cancelled", "Interrupted before execution")
                    self._trace(agent, record, "decision")
                    raise UserInterrupt()
                record = self.store.get(record["id"])
                if record["status"] == "approved":
                    current = {**identity, "arguments": pending.get("arguments"),
                               "call_id": pending.get("id"), "tool_name": pending.get("name")}
                    if binding != call_binding(self.store.agent_slug, **current):
                        self.store.finish(record["id"], "cancelled", "Arguments changed before execution")
                        raise ToolsConflict("Tool arguments changed during approval")
                    record = self.store.consume(record["id"], binding)
                    consumed = True
                    self.active[(identity["turn_no"], identity["call_id"])] = record["id"]
                    self._trace(agent, record, "decision")
                    return
                if record["status"] != "pending":
                    self._trace(agent, record, "decision")
                    agent.current_session["stop_signal"] = f"Studio approval {record['status']}; tool did not run"
                    raise ToolsConflict(f"Studio approval {record['status']}; tool did not run")
                time.sleep(0.05)

        try:
            if self.native_installed:
                view = copy(agent)
                view.io = _ApprovalIO(agent.io, pending, authorize)
                check_approval(view)  # Preserves native deny, policy, requester checks.
            authorize()  # Native auto/full-access grants cannot bypass this gate.
        except BaseException:
            if record is not None:
                # Still inside before_each_tool: even if authorization was
                # consumed, the body cannot have started when this gate fails.
                self.store.finish(record["id"], "cancelled", "Approval gate ended before execution")
                self.active.pop((identity["turn_no"], identity["call_id"]), None)
            raise

    def after(self, agent):
        trace = agent.current_session.get("trace", [])
        result = next((entry for entry in reversed(trace) if entry.get("type") == "tool_result"), None)
        if not result:
            return
        key = (agent.current_session.get("turn"), result.get("tool_id"))
        record_id = self.active.pop(key, None)
        if record_id:
            status = {"success": "succeeded", "interrupted": "cancelled"}.get(result.get("status"), "failed")
            record = self.store.finish(record_id, status, "Framework tool execution completed")
            self._trace(agent, record, "outcome")

    def stopped(self, agent):
        for key, record_id in list(self.active.items()):
            record = self.store.finish(record_id, "cancelled", "Turn interrupted; external effects may be incomplete")
            self._trace(agent, record, "outcome")
            self.active.pop(key, None)


def attach_studio_tools(agent, *, agent_dir: Path, configuration: dict, run_id: str | None = None):
    """Attach only to this factory instance; no upstream class/global patch."""
    from connectonion.useful_plugins.tool_approval.approval import check_approval
    from .runtime_manifest import attach_runtime_manifest
    run_id = run_id or uuid.uuid4().hex
    if not configuration["mcp_enabled"] and not configuration["approvals_enabled"]:
        return attach_runtime_manifest(agent, agent_dir=agent_dir, configuration=configuration, run_id=run_id)
    safe_tools = {}
    if configuration["mcp_enabled"]:
        functions = (studio_search_docs, studio_read_doc, _knowledge_tool(Path(agent_dir).name))
        for function in functions:
            if function.__name__ in agent.tools:
                raise ToolsConflict("Agent already defines a Studio-owned tool name")
        for function in functions:
            agent.add_tool(function)
            safe_tools[function.__name__] = agent.tools.get(function.__name__)
    handlers = list(agent.events.get("before_each_tool", []))
    native_installed = any(handler is check_approval for handler in handlers)
    gate = _Gate(agent_dir, configuration, safe_tools, native_installed=native_installed,
                 run_id=run_id)
    # Existing policy and workspace checks stay in place. Replace only this
    # instance's interactive handler; our bridge still calls the native check.
    agent.events["before_each_tool"] = [handler for handler in handlers if handler is not check_approval] + [gate.before]
    agent.events.setdefault("after_each_tool", []).append(gate.after)
    agent.events.setdefault("on_stop_signal", []).append(gate.stopped)
    return attach_runtime_manifest(agent, agent_dir=agent_dir, configuration=configuration, run_id=run_id)


def wrap_host_for_studio_tools(host_fn, script: Path):
    script = Path(script).absolute()
    configuration = tool_config(script.parent)
    if (not configuration["mcp_enabled"] and not configuration["approvals_enabled"]
            and configuration.get("context_budget") is None):
        return host_fn
    check_custom(script.parent)
    run_id = uuid.uuid4().hex  # Frozen for the hosted process, not restored from a client session.

    @functools.wraps(host_fn)
    def wrapped(factory, *args, **kwargs):
        original = inspect.unwrap(factory)
        co_dir = kwargs.get("co_dir")
        if (not inspect.isfunction(original) or original.__name__ != "create_agent"
                or Path(original.__code__.co_filename).absolute() != script
                or co_dir is None or Path(co_dir).absolute() != script.parent / ".co"):
            raise ToolsInvalid("Studio tools cannot be applied to this Agent factory")

        @functools.wraps(factory)
        def configured_factory():
            return attach_studio_tools(factory(), agent_dir=script.parent,
                                       configuration=configuration, run_id=run_id)
        return host_fn(configured_factory, *args, **kwargs)
    return wrapped
