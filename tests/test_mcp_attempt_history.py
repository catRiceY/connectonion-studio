"""Synthetic MCP attempt observations: no tools, model or personal history."""

from copy import deepcopy

import pytest

from co_studio.execution_adapter import convert_snapshot


def attempt(index, status, **extra):
    return {
        "type": "studio_mcp_attempt", "id": f"attempt-{index}", "turn": 1,
        "ts": 100.0 + index, "tool_id": "tool-read-1", "name": "studio_read_doc",
        "attempt": index, "max_attempts": 2, "status": status,
        "retry_scheduled": index == 1, "error_type": "ConnectionError" if index == 1 else None,
        "duration_ms": 12.5, "elapsed_ms": 20.5 + index, **extra,
    }


def snapshot(*events):
    return {
        "session_id": "attempt-session", "status": "running", "prompt": "Read the guide.",
        "session": {"session_id": "attempt-session", "turn": 1, "trace": [
            {"type": "user_input", "id": "user-1", "turn": 1, "content": "Read the guide."},
            *events,
        ]},
    }


def test_attempts_share_tool_call_without_merging_or_exposing_unreviewed_fields():
    raw = snapshot(attempt(1, "transport_error", args={"token": "private"},
                           result="private result", error="unreviewed error", permission="approved"),
                   attempt(2, "success"))
    original = deepcopy(raw)
    converted = convert_snapshot(raw, agent_slug="qa-agent")
    assert not converted.issues
    first, second = converted.events[1:]
    assert first.kind == second.kind == "studio_mcp_attempt"
    assert first.call_id == second.call_id == "tool-read-1"
    assert first.source_id == "attempt-1" and second.source_id == "attempt-2"
    assert first.event_id != second.event_id
    assert [first.sequence, second.sequence] == [1, 2]
    assert first.turn_no == second.turn_no == 1
    assert first.duration_ms == second.duration_ms == 12.5
    assert first.occurred_at == 101.0
    assert first.payload == {
        "name": "studio_read_doc", "attempt": 1, "max_attempts": 2,
        "status": "transport_error", "retry_scheduled": True,
        "error_type": "ConnectionError", "elapsed_ms": 21.5,
    }
    assert second.payload["status"] == "success" and second.payload["retry_scheduled"] is False
    assert "private" not in repr(converted)
    assert raw == original


@pytest.mark.parametrize("status", ["success", "transport_error", "timeout", "rejected", "tool_error", "cancelled", "error"])
def test_attempt_status_is_an_observation_not_a_synthesized_final_tool_result(status):
    converted = convert_snapshot(snapshot(attempt(1, status, retry_scheduled=False)), agent_slug="qa-agent")
    assert not converted.issues
    assert [item.kind for item in converted.events] == ["user_input", "studio_mcp_attempt"]
    assert converted.events[-1].payload["status"] == status
    assert converted.session.source_status == "running"
    assert converted.turns[0].result is None


def test_attempt_missing_tool_id_is_not_replaced_by_event_id_or_name():
    raw_event = attempt(1, "transport_error")
    for key in ("tool_id", "duration_ms", "elapsed_ms", "retry_scheduled"):
        raw_event.pop(key)
    raw_event["timing_ms"] = 999  # Only native tool_result uses this key.
    event = convert_snapshot(snapshot(raw_event), agent_slug="qa-agent").events[-1]
    assert event.call_id is None and event.source_id == "attempt-1"
    assert event.duration_ms is None
    assert "elapsed_ms" not in event.payload and "retry_scheduled" not in event.payload


def test_attempt_zero_duration_is_not_unknown():
    event = convert_snapshot(snapshot(attempt(1, "cancelled", duration_ms=0, elapsed_ms=0)),
                             agent_slug="qa-agent").events[-1]
    assert event.duration_ms == event.payload["elapsed_ms"] == 0
