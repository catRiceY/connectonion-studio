"""Offline retry fault injection: no stdio process, model, or user data."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
import json
import math

import pytest

from co_studio import mcp_readonly as readonly
from co_studio import mcp_retry as retry


READ_NAME = "studio_read_doc"
READ_ARGS = {"document_id": "history"}
RESULT = {"source": "studio-public-guides", "id": "history", "title": "History",
          "sha256": "a" * 64, "text": "Synthetic offline document"}
ATTEMPT_FIELDS = {"attempt", "max_attempts", "status", "retry_scheduled", "error_type", "elapsed_ms"}


@dataclass
class Plan:
    error: BaseException | None = None
    call: object = None
    cleanup: object = None
    enter_error: BaseException | None = None


class Connections:
    """Fresh context/client per attempt with observable teardown ordering."""

    def __init__(self, plans):
        self.plans = list(plans)
        self.limits = []
        self.calls = []
        self.argument_objects = []
        self.lifecycle = []
        self.context_tasks = []

    def open(self, limits, *, on_closing=None):
        number = len(self.limits) + 1
        assert number <= len(self.plans), "Unexpected extra connection/retry"
        plan = self.plans[number - 1]
        self.limits.append(limits)
        owner = self

        class Client:
            async def call(self, name, arguments):
                owner.lifecycle.append((number, "call"))
                owner.calls.append((name, deepcopy(arguments)))
                owner.argument_objects.append(arguments)
                if plan.call is not None:
                    return await plan.call(name, arguments)
                if plan.error is not None:
                    raise plan.error
                return deepcopy(RESULT)

        @asynccontextmanager
        async def connection():
            owner.lifecycle.append((number, "enter"))
            entered_task = asyncio.current_task()
            try:
                if plan.enter_error is not None:
                    raise plan.enter_error
                yield Client()
            finally:
                if on_closing is not None:
                    on_closing()
                owner.lifecycle.append((number, "exit_start"))
                if plan.cleanup is not None:
                    await plan.cleanup()
                owner.lifecycle.append((number, "exit_done"))
                owner.context_tasks.append((entered_task, asyncio.current_task()))

        return connection()


@pytest.fixture(autouse=True)
def short_offline_limits(monkeypatch):
    monkeypatch.setattr(retry, "ATTEMPT_TIMEOUT_SECONDS", .05)
    monkeypatch.setattr(retry, "RETRY_DELAY_SECONDS", .001)
    monkeypatch.setattr(retry, "TOTAL_TIMEOUT_SECONDS", .3)


def install(monkeypatch, *plans):
    connections = Connections(plans)
    monkeypatch.setattr(readonly, "open_documents", connections.open)
    return connections


def assert_safe_events(events):
    for event in events:
        assert set(event) == ATTEMPT_FIELDS
        assert type(event["attempt"]) is int and 1 <= event["attempt"] <= 2
        assert event["max_attempts"] == 2
        assert type(event["retry_scheduled"]) is bool
        assert event["status"] in {"success", "transport_error", "timeout", "rejected",
                                   "tool_error", "cancelled", "error"}
        assert event["error_type"] is None or isinstance(event["error_type"], str)
        assert isinstance(event["elapsed_ms"], (int, float)) and not isinstance(event["elapsed_ms"], bool)
        assert math.isfinite(event["elapsed_ms"]) and event["elapsed_ms"] >= 0
    encoded = json.dumps(events)
    for private in ("secret transport detail", "secret tool detail", "private-query-sentinel",
                    "document_id", "Synthetic offline document"):
        assert private not in encoded


def assert_contexts_closed(connections):
    for number in range(1, len(connections.limits) + 1):
        assert connections.lifecycle.count((number, "exit_done")) == 1
        if number > 1:
            assert connections.lifecycle.index((number - 1, "exit_done")) < connections.lifecycle.index((number, "enter"))
    assert all(entered is exited for entered, exited in connections.context_tasks)


def test_first_success_has_one_connection_and_one_content_free_attempt(monkeypatch):
    async def exercise():
        connections = install(monkeypatch, Plan())
        events = []
        assert await retry.call_document_with_retry(READ_NAME, READ_ARGS, on_attempt=events.append) == RESULT
        assert connections.calls == [(READ_NAME, READ_ARGS)]
        assert [(event["attempt"], event["status"], event["retry_scheduled"], event["error_type"])
                for event in events] == [(1, "success", False, None)]
        assert 0 < connections.limits[0].timeout_seconds <= retry.ATTEMPT_TIMEOUT_SECONDS
        assert_safe_events(events)
        assert_contexts_closed(connections)
    asyncio.run(exercise())


@pytest.mark.parametrize("error_name,status", [("McpTransportError", "transport_error"), ("McpTimeoutError", "timeout")])
@pytest.mark.parametrize("phase", ["call", "discovery"])
def test_retryable_failure_reopens_only_after_cleanup_and_then_succeeds(monkeypatch, error_name, status, phase):
    async def exercise():
        error = getattr(readonly, error_name)("secret transport detail")
        first = Plan(error=error) if phase == "call" else Plan(enter_error=error)
        connections = install(monkeypatch, first, Plan())
        events = []
        result = await retry.call_document_with_retry(READ_NAME, READ_ARGS, on_attempt=events.append)
        assert result == RESULT
        assert len(connections.limits) == 2
        assert len(connections.calls) == (2 if phase == "call" else 1)
        assert [(event["attempt"], event["status"], event["retry_scheduled"])
                for event in events] == [(1, status, True), (2, "success", False)]
        assert events[0]["error_type"] == error_name
        assert_safe_events(events)
        assert_contexts_closed(connections)
    asyncio.run(exercise())


@pytest.mark.parametrize("error_name,status", [("McpTransportError", "transport_error"), ("McpTimeoutError", "timeout")])
def test_two_failures_stop_at_fixed_attempt_limit(monkeypatch, error_name, status):
    async def exercise():
        error_type = getattr(readonly, error_name)
        connections = install(monkeypatch, Plan(error=error_type("secret transport detail")),
                              Plan(error=error_type("secret transport detail")))
        events = []
        with pytest.raises(error_type):
            await retry.call_document_with_retry(READ_NAME, READ_ARGS, on_attempt=events.append)
        assert len(connections.calls) == len(connections.limits) == 2
        assert [(event["attempt"], event["status"], event["retry_scheduled"])
                for event in events] == [(1, status, True), (2, status, False)]
        assert_safe_events(events)
        assert_contexts_closed(connections)
    asyncio.run(exercise())


@pytest.mark.parametrize("error_name,status", [
    ("McpPolicyError", "rejected"), ("McpReadonlyError", "tool_error"),
    ("McpBusyError", "tool_error"), ("RuntimeError", "error"),
])
def test_nonretryable_failures_do_not_open_a_second_connection(monkeypatch, error_name, status):
    async def exercise():
        error_type = RuntimeError if error_name == "RuntimeError" else getattr(readonly, error_name)
        connections = install(monkeypatch, Plan(error=error_type("secret tool detail")))
        events = []
        with pytest.raises(error_type):
            await retry.call_document_with_retry(READ_NAME, READ_ARGS, on_attempt=events.append)
        assert len(connections.calls) == len(connections.limits) == 1
        assert [(event["status"], event["retry_scheduled"]) for event in events] == [(status, False)]
        assert_safe_events(events)
        assert_contexts_closed(connections)
    asyncio.run(exercise())


@pytest.mark.parametrize("error_name", ["McpTransportError", "McpTimeoutError"])
def test_unverified_cleanup_blocks_retry_even_for_transient_error(monkeypatch, error_name):
    async def exercise():
        error_type = getattr(readonly, error_name)
        error = error_type("secret transport detail")
        error.cleanup_verified = False
        connections = install(monkeypatch, Plan(error=error))
        events = []
        with pytest.raises(error_type):
            await retry.call_document_with_retry(READ_NAME, READ_ARGS, on_attempt=events.append)
        assert len(connections.limits) == len(connections.calls) == 1
        assert len(events) == 1 and events[0]["retry_scheduled"] is False
        assert_safe_events(events)
        assert_contexts_closed(connections)
    asyncio.run(exercise())


@pytest.mark.parametrize("name,arguments", [
    ("studio_get_guide", {"guide_id": "history"}), ("studio_search_knowledge", {"query": "history"}),
    ("delete_document", {}), ("studio_read_doc", {"document_id": "../../secret"}),
    ("studio_read_doc", {"document_id": "history", "path": "/private"}),
    ("studio_read_doc", {}), ("studio_read_doc", []),
    ("studio_search_docs", {"query": ""}), ("studio_search_docs", {"query": "x" * 121}),
    ("studio_search_docs", {"query": "history", "limit": True}),
    ("studio_search_docs", {"query": "history", "limit": 5}),
    ("studio_search_docs", {"query": float("nan")}),
])
def test_local_argument_and_tool_rejection_precedes_any_connection(monkeypatch, name, arguments):
    async def exercise():
        connections = install(monkeypatch)
        with pytest.raises(readonly.McpPolicyError):
            await retry.call_document_with_retry(name, arguments)
        assert not connections.limits and not connections.calls
    asyncio.run(exercise())


def test_search_is_allowlisted_and_integral_limit_is_validated_before_dispatch(monkeypatch):
    async def exercise():
        connections = install(monkeypatch, Plan())
        events = []
        await retry.call_document_with_retry("studio_search_docs", {"query": "private-query-sentinel", "limit": 2.0},
                                             on_attempt=events.append)
        assert connections.calls == [("studio_search_docs", {"query": "private-query-sentinel", "limit": 2})]
        assert type(connections.calls[0][1]["limit"]) is int
        assert_safe_events(events)
    asyncio.run(exercise())


def test_caller_argument_mutation_cannot_change_current_or_retried_request(monkeypatch):
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        async def first_call(name, arguments):
            entered.set()
            await release.wait()
            assert arguments == {"query": "original", "limit": 2}
            raise readonly.McpTransportError("secret transport detail")
        connections = install(monkeypatch, Plan(call=first_call), Plan())
        arguments = {"query": "original", "limit": 2}
        task = asyncio.create_task(retry.call_document_with_retry("studio_search_docs", arguments))
        await asyncio.wait_for(entered.wait(), 1)
        arguments.clear()
        arguments.update(query="caller mutation", limit=4, path="/private")
        release.set()
        assert await asyncio.wait_for(task, 1) == RESULT
        assert connections.calls == [("studio_search_docs", {"query": "original", "limit": 2})] * 2
        assert all(captured is not arguments for captured in connections.argument_objects)
        assert_contexts_closed(connections)
    asyncio.run(exercise())


def test_cancelled_before_start_never_opens_connection(monkeypatch):
    async def exercise():
        connections = install(monkeypatch)
        with pytest.raises(asyncio.CancelledError):
            await retry.call_document_with_retry(READ_NAME, READ_ARGS, is_cancelled=lambda: True)
        assert not connections.limits and not connections.calls
    asyncio.run(exercise())


def test_cancel_during_call_is_propagated_after_cleanup_and_never_retried(monkeypatch):
    async def exercise():
        entered = asyncio.Event()
        async def wait_forever(name, arguments):
            entered.set()
            await asyncio.Event().wait()
        connections = install(monkeypatch, Plan(call=wait_forever))
        events = []
        task = asyncio.create_task(retry.call_document_with_retry(READ_NAME, READ_ARGS, on_attempt=events.append))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert len(connections.calls) == len(connections.limits) == 1
        assert [(event["status"], event["retry_scheduled"]) for event in events] == [("cancelled", False)]
        assert_safe_events(events)
        assert_contexts_closed(connections)
    asyncio.run(exercise())


def test_cancel_during_backoff_prevents_second_connection(monkeypatch):
    async def exercise():
        monkeypatch.setattr(retry, "RETRY_DELAY_SECONDS", .2)
        monkeypatch.setattr(retry, "TOTAL_TIMEOUT_SECONDS", 1.0)
        reported = asyncio.Event()
        events = []
        def observe(event):
            events.append(event)
            reported.set()
        connections = install(monkeypatch, Plan(error=readonly.McpTransportError("secret transport detail")))
        task = asyncio.create_task(retry.call_document_with_retry(READ_NAME, READ_ARGS, on_attempt=observe))
        await asyncio.wait_for(reported.wait(), 1)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert len(connections.calls) == len(connections.limits) == 1
        assert not any(event["status"] == "success" for event in events)
        assert_safe_events(events)
        assert_contexts_closed(connections)
    asyncio.run(exercise())


def test_cancellation_flag_after_response_rejects_late_success(monkeypatch):
    async def exercise():
        cancelled = False
        async def become_cancelled(name, arguments):
            nonlocal cancelled
            cancelled = True
            return deepcopy(RESULT)
        connections = install(monkeypatch, Plan(call=become_cancelled))
        events = []
        with pytest.raises(asyncio.CancelledError):
            await retry.call_document_with_retry(READ_NAME, READ_ARGS, on_attempt=events.append,
                                                 is_cancelled=lambda: cancelled)
        assert len(connections.calls) == 1
        assert not any(event["status"] == "success" for event in events)
        assert_safe_events(events)
        assert_contexts_closed(connections)
    asyncio.run(exercise())


def test_callee_swallowing_external_cancellation_cannot_return_late_success(monkeypatch):
    async def exercise():
        entered, swallowed = asyncio.Event(), asyncio.Event()
        async def late_response(name, arguments):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                swallowed.set()
                return deepcopy(RESULT)
        connections = install(monkeypatch, Plan(call=late_response))
        events = []
        task = asyncio.create_task(retry.call_document_with_retry(READ_NAME, READ_ARGS, on_attempt=events.append))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        assert swallowed.is_set() and len(connections.calls) == 1
        assert not any(event["status"] == "success" for event in events)
        assert_safe_events(events)
        assert_contexts_closed(connections)
    asyncio.run(exercise())


def test_expired_total_deadline_never_opens_connection(monkeypatch):
    async def exercise():
        connections = install(monkeypatch)
        with pytest.raises(readonly.McpTimeoutError):
            await retry.call_document_with_retry(READ_NAME, READ_ARGS, deadline=asyncio.get_running_loop().time() - 1)
        assert not connections.calls and not connections.limits
    asyncio.run(exercise())


def test_total_deadline_bounds_call_and_clamps_transport_timeout(monkeypatch):
    async def exercise():
        async def wait_forever(name, arguments):
            await asyncio.Event().wait()
        connections = install(monkeypatch, Plan(call=wait_forever))
        events = []
        with pytest.raises(readonly.McpTimeoutError):
            await asyncio.wait_for(retry.call_document_with_retry(
                READ_NAME, READ_ARGS, deadline=asyncio.get_running_loop().time() + .015,
                on_attempt=events.append), 1)
        assert len(connections.calls) == len(connections.limits) == 1
        assert 0 < connections.limits[0].timeout_seconds <= .015
        assert not any(event["status"] == "success" for event in events)
        assert_safe_events(events)
        assert_contexts_closed(connections)
    asyncio.run(exercise())


def test_total_deadline_includes_retry_backoff(monkeypatch):
    async def exercise():
        monkeypatch.setattr(retry, "RETRY_DELAY_SECONDS", .2)
        connections = install(monkeypatch, Plan(error=readonly.McpTransportError("secret transport detail")))
        # The original controlled failure may be returned immediately when the
        # remaining budget cannot cover a backoff. It must not open attempt 2.
        with pytest.raises(readonly.McpReadonlyError):
            await asyncio.wait_for(retry.call_document_with_retry(
                READ_NAME, READ_ARGS, deadline=asyncio.get_running_loop().time() + .015), 1)
        assert len(connections.calls) == len(connections.limits) == 1
        assert_contexts_closed(connections)
    asyncio.run(exercise())


def test_cleanup_may_finish_after_deadline_but_cannot_start_another_attempt(monkeypatch):
    async def exercise():
        cleaning, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def slow_cleanup():
            cleaning.set()
            await release.wait()
            closed.set()
        connections = install(monkeypatch, Plan(error=readonly.McpTransportError("secret transport detail"),
                                               cleanup=slow_cleanup))
        events = []
        task = asyncio.create_task(retry.call_document_with_retry(
            READ_NAME, READ_ARGS, deadline=asyncio.get_running_loop().time() + .01, on_attempt=events.append))
        try:
            await asyncio.wait_for(cleaning.wait(), 1)
            await asyncio.sleep(.03)
            assert not closed.is_set()
            assert not task.done(), "Cleanup must be awaited even when its request budget has expired"
            assert len(connections.limits) == 1
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
        with pytest.raises(readonly.McpReadonlyError):
            await asyncio.wait_for(task, 1)
        assert closed.is_set() and len(connections.limits) == 1
        assert not any(event["status"] == "success" for event in events)
        assert_safe_events(events)
        assert_contexts_closed(connections)
    asyncio.run(exercise())


def test_callee_swallowing_deadline_cancellation_cannot_return_late_success(monkeypatch):
    async def exercise():
        swallowed = asyncio.Event()
        async def late_response(name, arguments):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                swallowed.set()
                return deepcopy(RESULT)
        connections = install(monkeypatch, Plan(call=late_response))
        events = []
        with pytest.raises(readonly.McpTimeoutError):
            await asyncio.wait_for(retry.call_document_with_retry(
                READ_NAME, READ_ARGS, deadline=asyncio.get_running_loop().time() + .015,
                on_attempt=events.append), 1)
        assert swallowed.is_set() and len(connections.limits) == 1
        assert not any(event["status"] == "success" for event in events)
        assert_safe_events(events)
        assert_contexts_closed(connections)
    asyncio.run(exercise())


def test_two_attempts_share_one_deadline_including_backoff(monkeypatch):
    async def exercise():
        monkeypatch.setattr(retry, "ATTEMPT_TIMEOUT_SECONDS", .1)
        monkeypatch.setattr(retry, "RETRY_DELAY_SECONDS", .06)
        async def wait_forever(name, arguments):
            await asyncio.Event().wait()
        connections = install(monkeypatch, Plan(error=readonly.McpTransportError("secret transport detail")),
                              Plan(call=wait_forever))
        events = []
        with pytest.raises(readonly.McpTimeoutError):
            await asyncio.wait_for(retry.call_document_with_retry(
                READ_NAME, READ_ARGS, deadline=asyncio.get_running_loop().time() + .12,
                on_attempt=events.append), 1)
        assert len(connections.calls) == 2
        assert 0 < connections.limits[1].timeout_seconds < connections.limits[0].timeout_seconds
        assert [(event["attempt"], event["status"], event["retry_scheduled"])
                for event in events] == [(1, "transport_error", True), (2, "timeout", False)]
        assert_safe_events(events)
        assert_contexts_closed(connections)
    asyncio.run(exercise())


def test_repeated_caller_cancellation_does_not_interrupt_owned_cleanup(monkeypatch):
    async def exercise():
        entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def wait_forever(name, arguments):
            entered.set()
            await asyncio.Event().wait()
        async def slow_cleanup():
            cleaning.set()
            await release.wait()
        connections = install(monkeypatch, Plan(call=wait_forever, cleanup=slow_cleanup))
        events = []
        task = asyncio.create_task(retry.call_document_with_retry(READ_NAME, READ_ARGS, on_attempt=events.append))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            await asyncio.wait_for(cleaning.wait(), 1)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert (1, "exit_done") not in connections.lifecycle
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(connections.calls) == 1
        assert [(event["status"], event["retry_scheduled"]) for event in events] == [("cancelled", False)]
        assert_safe_events(events)
        assert_contexts_closed(connections)
    asyncio.run(exercise())


def test_cleanup_failure_prevents_returning_success_or_retrying(monkeypatch):
    async def exercise():
        async def fail_cleanup():
            error = readonly.McpTransportError("secret transport detail")
            error.cleanup_verified = False
            raise error
        connections = install(monkeypatch, Plan(cleanup=fail_cleanup))
        events = []
        with pytest.raises(readonly.McpTransportError):
            await retry.call_document_with_retry(READ_NAME, READ_ARGS, on_attempt=events.append)
        assert len(connections.calls) == len(connections.limits) == 1
        assert [(event["status"], event["retry_scheduled"]) for event in events] == [("transport_error", False)]
        assert (1, "exit_start") in connections.lifecycle and (1, "exit_done") not in connections.lifecycle
        assert_safe_events(events)
    asyncio.run(exercise())


def test_cancellation_flag_set_after_attempt_observation_prevents_retry(monkeypatch):
    async def exercise():
        cancelled = False
        events = []
        def observe(event):
            nonlocal cancelled
            events.append(event)
            cancelled = True
        connections = install(monkeypatch, Plan(error=readonly.McpTransportError("secret transport detail")))
        with pytest.raises(asyncio.CancelledError):
            await retry.call_document_with_retry(READ_NAME, READ_ARGS, on_attempt=observe,
                                                 is_cancelled=lambda: cancelled)
        assert len(connections.calls) == len(connections.limits) == 1
        assert not any(event["status"] == "success" for event in events)
        assert_safe_events(events)
        assert_contexts_closed(connections)
    asyncio.run(exercise())


@pytest.mark.parametrize("phase", ["success", "call_error", "discovery_error"])
def test_cancellation_during_existing_cleanup_waits_without_interrupting_it(monkeypatch, phase):
    async def exercise():
        monkeypatch.setattr(retry, "ATTEMPT_TIMEOUT_SECONDS", .5)
        monkeypatch.setattr(retry, "TOTAL_TIMEOUT_SECONDS", 1.0)
        cleaning, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def slow_cleanup():
            cleaning.set()
            await release.wait()
            closed.set()
        error = readonly.McpTransportError("secret transport detail")
        plan = Plan(cleanup=slow_cleanup)
        if phase == "call_error":
            plan.error = error
        elif phase == "discovery_error":
            plan.enter_error = error
        connections = install(monkeypatch, plan)
        events = []
        task = asyncio.create_task(retry.call_document_with_retry(READ_NAME, READ_ARGS, on_attempt=events.append))
        try:
            await asyncio.wait_for(cleaning.wait(), 1)
            task.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert not task.done() and not closed.is_set()
            assert (1, "exit_done") not in connections.lifecycle
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set() and len(connections.limits) == 1
        assert [(event["status"], event["retry_scheduled"]) for event in events] == [("cancelled", False)]
        assert_safe_events(events)
        assert_contexts_closed(connections)
    asyncio.run(exercise())


def test_default_total_budget_is_enforced_without_explicit_deadline(monkeypatch):
    async def exercise():
        monkeypatch.setattr(retry, "TOTAL_TIMEOUT_SECONDS", .015)
        monkeypatch.setattr(retry, "ATTEMPT_TIMEOUT_SECONDS", .1)
        async def wait_forever(name, arguments):
            await asyncio.Event().wait()
        connections = install(monkeypatch, Plan(call=wait_forever))
        with pytest.raises(readonly.McpTimeoutError):
            await asyncio.wait_for(retry.call_document_with_retry(READ_NAME, READ_ARGS), 1)
        assert len(connections.calls) == len(connections.limits) == 1
        assert 0 < connections.limits[0].timeout_seconds <= .015
        assert_contexts_closed(connections)
    asyncio.run(exercise())


def test_concurrent_logical_calls_keep_independent_attempt_counts_and_arguments(monkeypatch):
    async def exercise():
        both_entered, release = asyncio.Event(), asyncio.Event()
        count = 0
        async def fail_after_both_started(name, arguments):
            nonlocal count
            count += 1
            if count == 2:
                both_entered.set()
            await release.wait()
            raise readonly.McpTransportError("secret transport detail")
        connections = install(monkeypatch, Plan(call=fail_after_both_started), Plan(call=fail_after_both_started),
                              Plan(), Plan())
        first_events, second_events = [], []
        calls = [asyncio.create_task(retry.call_document_with_retry(
            READ_NAME, {"document_id": doc}, on_attempt=events.append))
            for doc, events in [("history", first_events), ("prompt-versions", second_events)]]
        try:
            await asyncio.wait_for(both_entered.wait(), 1)
        finally:
            release.set()
        assert await asyncio.wait_for(asyncio.gather(*calls), 1) == [RESULT, RESULT]
        assert len(connections.limits) == len(connections.calls) == 4
        for doc in ("history", "prompt-versions"):
            assert connections.calls.count((READ_NAME, {"document_id": doc})) == 2
        for events in (first_events, second_events):
            assert [(event["attempt"], event["status"], event["retry_scheduled"])
                    for event in events] == [(1, "transport_error", True), (2, "success", False)]
            assert_safe_events(events)
        assert all(connections.lifecycle.count((number, "exit_done")) == 1 for number in range(1, 5))
        assert all(entered is exited for entered, exited in connections.context_tasks)
    asyncio.run(exercise())


@pytest.mark.parametrize("error_name,status", [
    ("McpPolicyError", "rejected"), ("McpReadonlyError", "tool_error"),
    ("McpTransportError", "transport_error"),
])
def test_slow_cleanup_cannot_turn_a_nonretryable_error_into_retryable_timeout(monkeypatch, error_name, status):
    async def exercise():
        monkeypatch.setattr(retry, "ATTEMPT_TIMEOUT_SECONDS", .01)
        monkeypatch.setattr(retry, "TOTAL_TIMEOUT_SECONDS", .3)
        error_type = getattr(readonly, error_name)
        error = error_type("secret tool detail")
        if error_name == "McpTransportError":
            error.cleanup_verified = False
        async def slow_cleanup():
            await asyncio.sleep(.03)
        connections = install(monkeypatch, Plan(error=error, cleanup=slow_cleanup))
        events = []
        with pytest.raises(error_type) as raised:
            await asyncio.wait_for(retry.call_document_with_retry(READ_NAME, READ_ARGS,
                                                                 on_attempt=events.append), 1)
        assert type(raised.value) is error_type
        assert len(connections.calls) == len(connections.limits) == 1
        assert [(event["status"], event["retry_scheduled"]) for event in events] == [(status, False)]
        assert_safe_events(events)
        assert_contexts_closed(connections)
    asyncio.run(exercise())
