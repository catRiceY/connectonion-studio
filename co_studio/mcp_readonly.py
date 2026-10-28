"""Allowlisted offline stdio MCP transport; Agent mounting lives in mcp_agent.

Only the bundled reference server can be launched. Server annotations are hints,
not authorization. The local contracts below decide which tools may be called.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
import json
import math
from pathlib import Path
import sys
from typing import Any, AsyncIterator


REFERENCE_IDS = ("history", "prompt-versions", "mcp-scope")


def _object(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required,
            "additionalProperties": False}


_SUMMARY = _object({"id": {"type": "string", "enum": list(REFERENCE_IDS)},
                    "title": {"type": "string", "maxLength": 120}}, ["id", "title"])
_INPUTS = {
    "studio_list_guides": _object({"query": {"type": "string", "maxLength": 80},
                                  "limit": {"type": "integer", "minimum": 1, "maximum": 3}}, []),
    "studio_get_guide": _object({"guide_id": {"type": "string", "enum": list(REFERENCE_IDS)}},
                               ["guide_id"]),
}
_OUTPUTS = {
    "studio_list_guides": _object({"source": {"const": "bundled-fixture"},
                                  "guides": {"type": "array", "items": _SUMMARY, "maxItems": 3}},
                                 ["source", "guides"]),
    "studio_get_guide": _object({"source": {"const": "bundled-fixture"},
                                "id": {"type": "string", "enum": list(REFERENCE_IDS)},
                                "title": {"type": "string", "maxLength": 120},
                                "text": {"type": "string", "maxLength": 2000}},
                               ["source", "id", "title", "text"]),
}
_DESCRIPTIONS = {
    "studio_list_guides": "List the three bundled Studio reference guides; no user documents are read.",
    "studio_get_guide": "Read one bundled Studio reference guide by its fixed identifier.",
}


class McpReadonlyError(RuntimeError):
    """A controlled discovery, policy, transport, or tool failure."""

    cleanup_verified = True


class McpTransportError(McpReadonlyError):
    """A narrowly classified broken connection, not a server/business error."""


class McpPolicyError(McpReadonlyError):
    """A tool or payload is outside the local read-only policy."""


class McpBusyError(McpReadonlyError):
    """The bounded pending-call budget is full."""


class McpTimeoutError(McpReadonlyError):
    """Discovery or a tool call exceeded its deadline."""


@dataclass(frozen=True)
class ReadonlyLimits:
    timeout_seconds: float = 5.0
    max_concurrency: int = 2
    max_pending: int = 8
    max_argument_bytes: int = 4096
    max_result_bytes: int = 16384

    def __post_init__(self):
        if (isinstance(self.timeout_seconds, bool)
                or not isinstance(self.timeout_seconds, (int, float))
                or not math.isfinite(self.timeout_seconds)
                or not 0 < self.timeout_seconds <= 60):
            raise ValueError("timeout_seconds must be positive and at most 60")
        for field, upper in (("max_concurrency", 8), ("max_pending", 64),
                             ("max_argument_bytes", 4096), ("max_result_bytes", 65536)):
            value = getattr(self, field)
            if type(value) is not int or not 1 <= value <= upper:
                raise ValueError(f"{field} must be an integer from 1 to {upper}")
        if self.max_pending < self.max_concurrency:
            raise ValueError("max_pending must be at least max_concurrency")


def tool_definitions() -> list[dict]:
    """Fresh local contracts; callers cannot mutate the authorization policy."""
    return [{"name": name, "description": _DESCRIPTIONS[name],
             "inputSchema": deepcopy(schema), "outputSchema": deepcopy(_OUTPUTS[name]),
             "annotations": {"readOnlyHint": True, "destructiveHint": False,
                             "idempotentHint": True, "openWorldHint": False}}
            for name, schema in _INPUTS.items()]


def _json_copy(value: Any, maximum: int, label: str) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise McpPolicyError(f"{label} must be valid finite JSON") from None
    if len(encoded) > maximum:
        raise McpPolicyError(f"{label} exceeds its {maximum}-byte limit")
    return json.loads(encoded)


def _validate(value: Any, schema: dict, label: str) -> None:
    # Only local schemas are used here. They have no remote or dynamic references.
    from jsonschema import Draft202012Validator
    if not Draft202012Validator(schema).is_valid(value):
        raise McpPolicyError(f"{label} does not match the local tool schema")


def _transport_error(exc: Exception, operation: str) -> McpReadonlyError:
    import anyio
    from mcp.shared.exceptions import McpError
    from mcp.types import CONNECTION_CLOSED

    def category(error):
        # Never retry a mixed group merely because one leaf is transient.
        if isinstance(error, BaseExceptionGroup):
            kinds = [category(item) for item in error.exceptions]
            return ("timeout" if "timeout" in kinds else "transport") if kinds and all(kinds) else None
        if isinstance(error, TimeoutError) or (isinstance(error, McpError) and error.error.code == 408):
            return "timeout"
        if (isinstance(error, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError,
                               anyio.EndOfStream, anyio.BrokenResourceError, anyio.ClosedResourceError))
                or isinstance(error, McpError) and error.error.code == CONNECTION_CLOSED):
            return "transport"
        return None

    kind = category(exc)
    if kind == "timeout":
        return McpTimeoutError(f"MCP {operation} timed out (including queue wait)")
    if kind == "transport":
        return McpTransportError(f"MCP {operation} lost its connection")
    return McpReadonlyError(f"MCP {operation} failed; connection must be reopened")


def validate_arguments(name: str, arguments: Any, maximum: int = 4096) -> dict:
    if not isinstance(name, str) or name not in _INPUTS:
        raise McpPolicyError("Tool is not on the local read-only allowlist")
    if type(arguments) is not dict:
        raise McpPolicyError("Tool arguments must be a JSON object")
    copied = _json_copy(arguments, maximum, "Arguments")
    _validate(copied, _INPUTS[name], "Arguments")
    # JSON Schema integer accepts integral JSON numbers such as 2.0. Normalize
    # after validation so the server can safely use the value as a slice bound.
    if name == "studio_list_guides" and "limit" in copied:
        copied["limit"] = int(copied["limit"])
    return copied


class ReadonlyMcpClient:
    """Used only inside open_fixture(); contains no subprocess-command setting."""

    def __init__(self, session: Any, limits: ReadonlyLimits, *, definitions: list[dict] | None = None):
        self._session = session
        self.limits = limits
        self._gate = asyncio.Semaphore(limits.max_concurrency)
        self._tasks: set[asyncio.Task] = set()
        self._ready = False
        self._closed = False
        self.protocol_version: str | None = None
        self._definitions = deepcopy(definitions or tool_definitions())
        self._inputs = {item["name"]: item["inputSchema"] for item in self._definitions}
        self._outputs = {item["name"]: item["outputSchema"] for item in self._definitions}

    async def _discover(self) -> None:
        try:
            async with asyncio.timeout(self.limits.timeout_seconds):
                initialized = await self._session.initialize()
                self.protocol_version = initialized.protocolVersion
                page = await self._session.list_tools()
                _json_copy(page.model_dump(mode="json", exclude_none=True), 16384, "Tool discovery")
                if page.nextCursor or len(page.tools) != len(self._inputs):
                    raise McpPolicyError("Unexpected tool catalog for the bundled server")
                seen = set()
                for tool in page.tools:
                    if tool.name not in self._inputs or tool.name in seen:
                        raise McpPolicyError("Unexpected or duplicate tool in bundled catalog")
                    seen.add(tool.name)
                    if tool.inputSchema != self._inputs[tool.name] or tool.outputSchema != self._outputs[tool.name]:
                        raise McpPolicyError("Server schema differs from the local tool contract")
                    if tool.annotations and tool.annotations.readOnlyHint is False:
                        raise McpPolicyError("Server marks an allowlisted tool as writable")
                self._ready = True
        except McpReadonlyError:
            self._closed = True
            raise
        except Exception as exc:
            self._closed = True
            raise _transport_error(exc, "initialization/discovery") from None

    def tools(self) -> list[dict]:
        if not self._ready or self._closed:
            raise McpReadonlyError("MCP connection is not open")
        return deepcopy(self._definitions)

    async def call(self, name: str, arguments: dict) -> dict:
        if not self._ready or self._closed:
            raise McpReadonlyError("MCP connection is not open")
        if not isinstance(name, str) or name not in self._inputs or type(arguments) is not dict:
            raise McpPolicyError("Tool is not on the local allowlist or arguments are invalid")
        arguments = _json_copy(arguments, self.limits.max_argument_bytes, "Arguments")
        _validate(arguments, self._inputs[name], "Arguments")
        if "limit" in arguments and type(arguments["limit"]) is float:
            arguments["limit"] = int(arguments["limit"])
        if len(self._tasks) >= self.limits.max_pending:
            raise McpBusyError("MCP pending-call limit reached")
        # Own only this operation, never the caller's entire task. A caller may
        # catch cancellation and continue unrelated work after this call ends.
        task = asyncio.create_task(self._perform(name, arguments))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Also covers cancellation before the child coroutine first runs.
            # Keep ownership until the child is actually done, even if a lower
            # layer takes another await to react to cancellation.
            self._closed = True
            task.cancel()
            raise

    async def _perform(self, name: str, arguments: dict) -> dict:
        try:
            # This session never retries. The fixed document connection owner
            # can retry only after this entire context has closed successfully.
            async with asyncio.timeout(self.limits.timeout_seconds):
                async with self._gate:
                    if self._closed:
                        raise McpReadonlyError("MCP connection is closed")
                    result = await self._session.call_tool(
                        name, arguments=arguments,
                        read_timeout_seconds=timedelta(seconds=self.limits.timeout_seconds),
                    )
                    _json_copy(result.model_dump(mode="json", exclude_none=True),
                               self.limits.max_result_bytes, "Tool result")
                    if result.isError:
                        raise McpReadonlyError("Bundled MCP tool reported an error")
                    if any(block.type != "text" for block in result.content):
                        raise McpPolicyError("Only text/structured results are allowed")
                    data = _json_copy(result.structuredContent, self.limits.max_result_bytes, "Structured result")
                    _validate(data, self._outputs[name], "Structured result")
                    if name == "studio_get_guide" and data["id"] != arguments["guide_id"]:
                        raise McpPolicyError("Returned guide does not match the request")
                    if name == "studio_read_doc" and data["id"] != arguments["document_id"]:
                        raise McpPolicyError("Returned document does not match the request")
                    return data
        except McpReadonlyError:
            self._closed = True
            raise
        except asyncio.CancelledError:
            # The SDK cancels the wait, not necessarily work already started by
            # the peer. Never silently reuse this connection after cancellation.
            self._closed = True
            raise
        except Exception as exc:
            self._closed = True
            raise _transport_error(exc, "call") from None

    async def _close(self) -> None:
        self._closed = True
        pending = [task for task in self._tasks if task is not asyncio.current_task()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


@asynccontextmanager
async def _open_bundle(module: str, definitions: list[dict], limits: ReadonlyLimits | None = None,
                       *, on_closing=None) -> AsyncIterator[ReadonlyMcpClient]:
    """Launch fixed bundled stdio server; enter and exit in the same asyncio task.

    Uses the SDK's subprocess ownership and shutdown (stdin close, timeout, then
    process-tree termination). No arbitrary server, executable, environment,
    credentials, filesystem roots, sampling, or elicitation are exposed.
    """
    if module not in {"co_studio.mcp_fixture_server", "co_studio.mcp_docs_server"}:
        raise McpPolicyError("Only built-in MCP servers can be launched")
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError:
        raise McpReadonlyError("Install the Studio 'mcp' optional dependency first") from None
    limits = limits or ReadonlyLimits()
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-I", "-m", module],
        cwd=str(Path(__file__).resolve().parent),
    )
    failure: BaseException | None = None
    try:
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream,
                                     read_timeout_seconds=timedelta(seconds=limits.timeout_seconds)) as session:
                client = ReadonlyMcpClient(session, limits, definitions=definitions)
                # Save caller exceptions until both SDK contexts have exited.
                # AnyIO otherwise wraps policy errors in ExceptionGroups.
                try:
                    await client._discover()
                    yield client
                except BaseException as exc:
                    failure = exc
                finally:
                    if on_closing is not None:
                        on_closing()
                    await client._close()
    except Exception as exc:
        # Preserve the original failure, but an error during shutdown prevents
        # retry even when the original error was a transient timeout/disconnect.
        if failure is None:
            failure = _transport_error(exc, "connection/shutdown")
        failure.cleanup_verified = False
    if failure is not None:
        raise failure


@asynccontextmanager
async def open_fixture(limits: ReadonlyLimits | None = None) -> AsyncIterator[ReadonlyMcpClient]:
    async with _open_bundle("co_studio.mcp_fixture_server", tool_definitions(), limits) as client:
        yield client


@asynccontextmanager
async def open_documents(limits: ReadonlyLimits | None = None, *, on_closing=None) -> AsyncIterator[ReadonlyMcpClient]:
    """Read the fixed public Studio guides shipped with the package, not user files."""
    from .mcp_docs_server import document_tool_definitions
    async with _open_bundle("co_studio.mcp_docs_server", document_tool_definitions(), limits,
                            on_closing=on_closing) as client:
        yield client


async def _self_test() -> dict:
    async with open_fixture() as client:
        tools = [tool["name"] for tool in client.tools()]
        listing = await client.call("studio_list_guides", {"query": "prompt", "limit": 3})
        guide = await client.call("studio_get_guide", {"guide_id": "prompt-versions"})
        return {"mode": "bundled-fixture-only", "protocol_version": client.protocol_version,
                "tools": tools, "listing": listing, "guide": guide,
                "note": "No user documents, model calls, Agent integration, or RAG retrieval."}


def main() -> None:
    try:
        result = asyncio.run(_self_test())
    except McpReadonlyError as exc:
        print(f"MCP self-test failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps({**result, "connection_closed": True}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
