"""Bounded retries for the two shipped, locally authorized read-only MCP tools.

No caller-supplied server, tool contract, write operation, or exception-message
matching. Each attempt owns its SDK contexts in one task. Cleanup is awaited
before another process can start; cleanup may outlast the logical deadline.
"""
from __future__ import annotations

import asyncio
import math

from . import mcp_readonly as readonly

TOTAL_TIMEOUT_SECONDS = 10.0
ATTEMPT_TIMEOUT_SECONDS = 5.0
RETRY_DELAY_SECONDS = 0.1
MAX_ATTEMPTS = 2


async def cancel_and_wait(task, *, cancel=True):
    """Cancel owned work once, then drain without moving SDK exit to a new task.

    Returns whether the waiting caller was cancelled during cleanup. Repeated
    cancellation must not release a capacity slot while a subprocess is alive.
    """
    caller = asyncio.current_task()
    before = caller.cancelling()
    if cancel and not task.done() and not task.cancelling():
        task.cancel()
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except Exception:
            break
    if not task.cancelled():
        task.exception()  # Retrieve failures; never return a late success.
    return caller.cancelling() > before


def _arguments(name, arguments):
    from .mcp_docs_server import document_tool_definitions
    definition = next((item for item in document_tool_definitions() if item["name"] == name), None)
    if definition is None or type(arguments) is not dict:
        raise readonly.McpPolicyError("Tool is not on the public guide allowlist or arguments are invalid")
    copied = readonly._json_copy(arguments, 4096, "Arguments")
    readonly._validate(copied, definition["inputSchema"], "Arguments")
    if "limit" in copied:
        copied["limit"] = int(copied["limit"])
    return copied


async def _attempt(name, arguments, deadline, is_cancelled):
    loop = asyncio.get_running_loop()
    duration = min(ATTEMPT_TIMEOUT_SECONDS, deadline - loop.time())
    if duration <= 0:
        raise readonly.McpTimeoutError("MCP total request deadline expired")
    state = {"cleanup_verified": True, "closing": False}

    def closing():
        state["closing"] = True

    async def owned():
        try:
            limits = readonly.ReadonlyLimits(timeout_seconds=duration)
            async with readonly.open_documents(limits, on_closing=closing) as client:
                try:
                    return await client.call(name, arguments)
                finally:
                    closing()
        except BaseException as exc:
            state["cleanup_verified"] = getattr(exc, "cleanup_verified", True)
            raise

    task = asyncio.create_task(owned())
    attempt_deadline = loop.time() + duration
    try:
        while not task.done() and loop.time() < attempt_deadline:
            if is_cancelled is not None and is_cancelled():
                raise asyncio.CancelledError()
            await asyncio.wait({task}, timeout=min(0.05, max(0, attempt_deadline - loop.time())))
        if not task.done() or loop.time() >= attempt_deadline:
            if await cancel_and_wait(task, cancel=not state["closing"]):
                raise asyncio.CancelledError()
            # Slow cleanup cannot promote a policy/business failure into a
            # retryable timeout. Keep an observed failure's original class.
            if not task.cancelled() and task.exception() is not None:
                raise task.exception()
            error = readonly.McpTimeoutError("MCP attempt deadline expired")
            error.cleanup_verified = state["cleanup_verified"]
            raise error
        return task.result()
    except asyncio.CancelledError:
        await cancel_and_wait(task, cancel=not state["closing"])
        raise


async def call_document_with_retry(name, arguments, *, deadline=None, on_attempt=None, is_cancelled=None):
    """Call a fixed public guide tool at most twice, with a shared deadline.

    The Agent supplies its admission-time deadline (including its queue wait).
    Observations contain classifications only, never input/output/error text.
    A retry-scheduled observation is intent, not proof another attempt started.
    """
    loop = asyncio.get_running_loop()
    if deadline is None:
        deadline = loop.time() + TOTAL_TIMEOUT_SECONDS
    if isinstance(deadline, bool) or not isinstance(deadline, (float, int)) or not math.isfinite(deadline):
        raise ValueError("deadline must be a finite monotonic time")
    # Snapshot before dispatch. A caller cannot change the second attempt's args.
    arguments = _arguments(name, arguments)

    def check_cancelled():
        if (asyncio.current_task().cancelling()
                or is_cancelled is not None and is_cancelled()):
            raise asyncio.CancelledError()

    def emit(number, status, error_type, started, retry=False):
        if on_attempt is not None:
            on_attempt({"attempt": number, "max_attempts": MAX_ATTEMPTS,
                        "status": status, "retry_scheduled": retry, "error_type": error_type,
                        "elapsed_ms": round((loop.time() - started) * 1000, 3)})

    for number in range(1, MAX_ATTEMPTS + 1):
        check_cancelled()
        if loop.time() >= deadline:
            raise readonly.McpTimeoutError("MCP total request deadline expired")
        started = loop.time()
        try:
            result = await _attempt(name, arguments, deadline, is_cancelled)
            check_cancelled()
            if loop.time() >= deadline:
                raise readonly.McpTimeoutError("MCP total request deadline expired")
        except asyncio.CancelledError:
            emit(number, "cancelled", "CancelledError", started)
            raise
        except Exception as exc:
            if isinstance(exc, readonly.McpTimeoutError):
                status, error_type = "timeout", "McpTimeoutError"
            elif isinstance(exc, readonly.McpTransportError):
                status, error_type = "transport_error", "McpTransportError"
            elif isinstance(exc, readonly.McpPolicyError):
                status, error_type = "rejected", "McpPolicyError"
            elif isinstance(exc, readonly.McpReadonlyError):
                status, error_type = "tool_error", "McpReadonlyError"
            else:
                status, error_type = "error", "UnexpectedError"
            retry = (number < MAX_ATTEMPTS and status in {"timeout", "transport_error"}
                     and getattr(exc, "cleanup_verified", True) is True
                     and loop.time() + RETRY_DELAY_SECONDS < deadline
                     and not asyncio.current_task().cancelling()
                     and not (is_cancelled is not None and is_cancelled()))
            emit(number, status, error_type, started, retry)
            check_cancelled()
            if not retry:
                raise
            # Cancellation and the shared deadline can prevent this scheduled
            # retry. Backoff alone does not produce a second attempt record.
            await asyncio.sleep(RETRY_DELAY_SECONDS)
        else:
            emit(number, "success", None, started)
            return result
