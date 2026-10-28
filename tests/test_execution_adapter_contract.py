"""Adapter contracts exercised against records produced by ConnectOnion itself.

Only the model provider is a scripted fake. Agent.input, tool execution, Host
turn handling and JSONL persistence are the installed 1.9.2 implementations.
All generated data lives in pytest's temporary directory; no network is used.
"""

from collections import deque
from copy import deepcopy
from importlib.metadata import version
import json

import pytest

from connectonion import Agent
from connectonion.core.events import before_iteration
from connectonion.core.llm import LLMResponse, ToolCall
from connectonion.core.tool_result import ToolFailure
from connectonion.core.usage import TokenUsage
from connectonion.network.host.session import SessionStorage
from connectonion.network.host.session.turn import input_handler
from connectonion.useful_plugins.runtime_input import _append_runtime_inputs
from connectonion.useful_plugins.watch_events import watch_events

from co_studio.execution_adapter import convert_snapshot, read_execution_history


class ScriptedLLM:
    model = "offline-contract-model"

    def __init__(self, responses):
        self.responses = deque(responses)

    def complete(self, *args, **kwargs):
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


def response(content, *, tool_calls=()):
    return LLMResponse(
        content=content,
        tool_calls=list(tool_calls),
        raw_response=None,
        usage=TokenUsage(input_tokens=8, output_tokens=2, cost=0.001),
    )


def host_runtime(tmp_path, monkeypatch, responses, tools=(), *, plugins=(), on_events=()):
    monkeypatch.chdir(tmp_path)
    co_dir = tmp_path / ".co"
    co_dir.mkdir()
    storage = SessionStorage(co_dir / "session_results.jsonl")
    llm = ScriptedLLM(responses)

    def factory():
        return Agent(
            "Adapter contract", llm=llm, tools=list(tools),
            system_prompt="Use the supplied offline test tools when requested.",
            quiet=True, log=False, co_dir=co_dir, state_dir=co_dir,
            max_iterations=3,
            plugins=list(plugins), on_events=list(on_events),
        )

    def run(prompt):
        return input_handler(
            factory, storage, prompt, result_ttl=3600,
            session={"session_id": "contract-session"},
        )

    return storage, llm, run


def latest_row(path):
    # Read actual framework output, not a hand-authored imitation of its shape.
    return json.loads(path.read_text(encoding="utf-8").splitlines()[-1])


def file_state(root):
    return {
        path.relative_to(root): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*") if path.is_file()
    }


@pytest.mark.parametrize("outcome", ["success", "failure_value", "exception"])
def test_native_multiturn_tool_records_convert_without_mutation(
    tmp_path, monkeypatch, outcome,
):
    assert version("connectonion") == "1.9.2"

    def lookup_order(order_id: str) -> str:
        """Look up a synthetic order in the offline contract test."""
        if outcome == "failure_value":
            return ToolFailure("Order lookup refused")
        if outcome == "exception":
            raise ValueError("Synthetic order unavailable")
        return f"{order_id}: dispatched"

    storage, llm, run = host_runtime(
        tmp_path, monkeypatch,
        [
            response("First answer"),
            response(None, tool_calls=[
                ToolCall("lookup_order", {"order_id": "TEST-123"}, "tool-call-1"),
            ]),
            response("Final answer"),
        ],
        tools=[lookup_order],
    )
    run("First question")
    run("Look up TEST-123")
    assert not llm.responses

    raw = latest_row(storage.path)
    before = deepcopy(raw)
    converted = convert_snapshot(raw, agent_slug="contract-agent")
    assert raw == before
    assert converted.issues == ()
    assert converted.session.source_status == raw["status"] == "done"
    assert converted.session.source_revision == raw["revision"]
    assert converted.session.created_at == raw["created"]
    assert converted.session.updated_at == raw["updated_at"]
    assert [turn.turn_no for turn in converted.turns] == [1, 2]
    assert [turn.prompt for turn in converted.turns] == ["First question", "Look up TEST-123"]
    assert all(turn.finish_reason == "natural" for turn in converted.turns)
    # Host fields belong only to the last turn. Old messages are not a safe
    # substitute for immutable per-turn answers because they may be compacted.
    assert converted.turns[0].result is None
    assert converted.turns[0].duration_ms is None
    assert converted.turns[1].result == raw["result"] == "Final answer"
    assert converted.turns[1].duration_ms == raw["duration_ms"]
    assert "success" not in converted.session.model_dump()
    assert all("success" not in turn.model_dump() for turn in converted.turns)

    trace = raw["session"]["trace"]
    assert len(converted.events) == len(trace)
    for sequence, (native, event) in enumerate(zip(trace, converted.events)):
        assert event.sequence == sequence
        assert event.kind == native["type"]
        assert event.source_id == native["id"]
        assert event.occurred_at == native["ts"]
        assert event.agent_slug == "contract-agent"
        assert event.session_id == "contract-session"
        if event.kind == "llm_result":
            assert event.duration_ms == native["duration_ms"]
            assert event.payload["usage"] == native["usage"]
        if event.kind == "tool_result":
            assert event.duration_ms == native["timing_ms"]

    user_events = [event for event in converted.events if event.kind == "user_input"]
    assert [event.turn_no for event in user_events] == [1, 2]
    boundary = user_events[1].sequence
    assert all(event.turn_no == 1 for event in converted.events[:boundary])
    assert all(event.turn_no == 2 for event in converted.events[boundary:])

    tool_start, tool_end = [
        event for event in converted.events if event.kind in {"tool_call", "tool_result"}
    ]
    assert tool_start.call_id == tool_end.call_id == "tool-call-1"
    assert tool_start.source_id != tool_end.source_id
    assert tool_start.event_id != tool_end.event_id
    assert tool_end.payload["name"] == "lookup_order"
    assert tool_end.payload["args"] == {"order_id": "TEST-123"}
    expected_status = "success" if outcome == "success" else "error"
    assert tool_end.payload["status"] == expected_status
    if outcome == "failure_value":
        assert tool_end.payload["error_type"] == "ToolFailure"
        assert tool_end.payload["error"] == "Order lookup refused"
    elif outcome == "exception":
        assert tool_end.payload["error_type"] == "ValueError"
        assert tool_end.payload["error"] == "Synthetic order unavailable"

    llm_events = [event for event in converted.events if event.kind.startswith("llm_")]
    assert len(llm_events) == 6
    for start, end in zip(llm_events[::2], llm_events[1::2]):
        # Native LLM start/result share an id; this is NOT a unique event key.
        assert start.kind == "llm_call" and end.kind == "llm_result"
        assert start.source_id == end.source_id == start.call_id == end.call_id
        assert start.event_id != end.event_id

    before_files = file_state(tmp_path)
    history = read_execution_history(storage.path, agent_slug="contract-agent")
    again = read_execution_history(storage.path, agent_slug="contract-agent")
    assert history.issues == ()
    # The file reader can restore a prior answer from its own completed Host
    # snapshot; converting a single latest snapshot alone still cannot.
    restored = history.sessions[0]
    assert restored.session == converted.session
    assert restored.events == converted.events
    assert restored.turns[1] == converted.turns[1]
    first_done = next(json.loads(line) for line in storage.path.read_text().splitlines()
                      if json.loads(line)["status"] == "done")
    assert restored.turns[0].result == first_done["result"] == "First answer"
    assert restored.turns[0].duration_ms == first_done["duration_ms"]
    assert restored.turns[0].prompt == converted.turns[0].prompt
    assert again == history
    assert file_state(tmp_path) == before_files


def test_native_model_exception_preserves_failed_host_status(tmp_path, monkeypatch):
    storage, llm, run = host_runtime(
        tmp_path, monkeypatch, [RuntimeError("Synthetic model failure")],
    )
    with pytest.raises(RuntimeError, match="Synthetic model failure"):
        run("An offline failing turn")
    assert not llm.responses
    raw = latest_row(storage.path)
    assert raw["status"] == "failed"
    converted = convert_snapshot(raw, agent_slug="contract-agent")
    assert converted.session.source_status == "failed"
    assert all(turn.result is None for turn in converted.turns)
    assert all("success" not in turn.model_dump() for turn in converted.turns)
    before_files = file_state(tmp_path)
    history = read_execution_history(storage.path, agent_slug="contract-agent")
    assert history.sessions == (converted,)
    assert file_state(tmp_path) == before_files


def test_native_inflight_inputs_do_not_replace_primary_turn_prompt(tmp_path, monkeypatch):
    runtime_inputs = [{"id": "runtime-input-1", "prompt": "Also check the delivery date"}]
    watch_batches = deque([[{"id": "watch-1", "content": "A synthetic shipment changed"}]])

    @before_iteration
    def supply_runtime_input(agent):
        # Invoke the native runtime-input appender with a drained input batch;
        # it authors the actual trace shape, rather than the test fabricating it.
        if runtime_inputs:
            _append_runtime_inputs(agent, [runtime_inputs.pop()])

    def poll_watch():
        return watch_batches.popleft() if watch_batches else []

    storage, llm, run = host_runtime(
        tmp_path, monkeypatch, [response("Answered the original request and updates")],
        plugins=[watch_events(poll_watch)], on_events=[supply_runtime_input],
    )
    run("Look up my order")
    assert not llm.responses
    raw = latest_row(storage.path)
    native_inputs = [event for event in raw["session"]["trace"] if event["type"] == "user_input"]
    assert len(native_inputs) == 3
    assert any(event.get("runtime_input") is True for event in native_inputs)
    assert any(event.get("internal") is True for event in native_inputs)

    converted = convert_snapshot(raw, agent_slug="contract-agent")
    assert not converted.issues
    assert len(converted.turns) == 1
    turn = converted.turns[0]
    assert turn.turn_no == 1
    assert turn.prompt == raw["prompt"] == "Look up my order"
    assert turn.result == raw["result"] == "Answered the original request and updates"
    assert turn.duration_ms == raw["duration_ms"]
    assert turn.finish_reason == "natural"
    events = [event for event in converted.events if event.kind == "user_input"]
    assert len(events) == 3
    assert all(event.turn_no == 1 for event in converted.events)
    assert any(event.payload.get("runtime_input") is True for event in events)
    watcher = next(event for event in events if event.payload.get("internal") is True)
    assert watcher.payload["watch_event_ids"] == ["watch-1"]
