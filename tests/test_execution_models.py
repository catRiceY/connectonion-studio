"""Synthetic examples for the first Studio execution-data contract increment."""

import pytest
from pydantic import ValidationError

from co_studio.execution_models import ExecutionEvent, SessionRecord, TurnRecord


OWNER = {"agent_slug": "demo-agent", "session_id": "session-demo"}


def example_event(sequence, kind, **fields):
    # Explicit, synthetic IDs, NOT the future adapter's ID-generation algorithm.
    return ExecutionEvent(
        **OWNER, event_id=f"example-event-{sequence}", sequence=sequence,
        kind=kind, turn_no=1, **fields,
    )


def test_plain_question_and_shared_llm_source_id():
    session = SessionRecord(**OWNER, source_status="done", source_revision=2)
    turn = TurnRecord(
        **OWNER, turn_no=1, prompt="What is an API?",
        result="An interface between programs.", finish_reason="natural",
    )
    events = [
        example_event(0, "user_input", payload={"content": turn.prompt}),
        example_event(1, "llm_call", source_id="llm-a", call_id="llm-a"),
        example_event(
            2, "llm_result", source_id="llm-a", call_id="llm-a",
            duration_ms=120.0,
            payload={"status": "success", "usage": {"input_tokens": 8, "output_tokens": 5}},
        ),
        example_event(3, "turn_result", payload={"reason": "natural"}),
    ]

    assert events[1].source_id == events[2].source_id
    assert events[1].call_id == events[2].call_id
    assert events[1].event_id != events[2].event_id
    assert all((event.agent_slug, event.session_id) == (session.agent_slug, session.session_id)
               for event in events)
    assert all(event.turn_no == turn.turn_no for event in events)
    for record in [session, turn, *events]:
        assert type(record).model_validate_json(record.model_dump_json()) == record


def test_tool_failure_does_not_assign_a_business_outcome_to_turn():
    call = example_event(
        2, "tool_call", source_id="trace-start", call_id="tool-a",
        payload={"name": "read_file", "args": {"path": "missing.txt"}},
    )
    result = example_event(
        3, "tool_result", source_id="trace-end", call_id="tool-a",
        duration_ms=2.0,
        payload={"name": "read_file", "status": "error", "error": "file not found"},
    )
    turn = TurnRecord(
        **OWNER, turn_no=1, prompt="Read missing.txt",
        result="The file was not found.", finish_reason="natural",
    )

    assert call.call_id == result.call_id
    assert call.source_id != result.source_id
    assert result.payload["status"] == "error"
    assert turn.finish_reason == "natural"
    # Ending normally is not proof the requested business result was achieved.
    assert "success" not in turn.model_dump()


def test_missing_facts_remain_unknown():
    session = SessionRecord(**OWNER)
    turn = TurnRecord(**OWNER, turn_no=1)
    event = ExecutionEvent(**OWNER, event_id="e-1", sequence=0, kind="llm_result")

    assert session.source_status is session.source_revision is None
    assert session.created_at is session.updated_at is None
    assert turn.prompt is turn.result is turn.finish_reason is turn.duration_ms is None
    assert event.turn_no is event.source_id is event.call_id is None
    assert event.occurred_at is event.duration_ms is None
    assert "usage" not in event.payload  # Not silently filled with zero tokens.


def test_source_names_remain_extensible():
    assert SessionRecord(**OWNER, source_status="future_host_state").source_status == "future_host_state"
    assert TurnRecord(**OWNER, turn_no=1, finish_reason="plugin_stop").finish_reason == "plugin_stop"
    assert example_event(0, "plugin_event", payload={"custom": [1, "value", None]}).kind == "plugin_event"


def test_identity_is_explicit_and_serialization_is_repeatable():
    data = {**OWNER, "event_id": "stable-source-position", "sequence": 0, "kind": "user_input"}
    assert ExecutionEvent(**data).model_dump_json() == ExecutionEvent(**data).model_dump_json()
    del data["event_id"]
    with pytest.raises(ValidationError):
        ExecutionEvent(**data)


def test_payload_defaults_are_not_shared_and_only_accept_json_values():
    first, second = example_event(0, "user_input"), example_event(1, "llm_call")
    first.payload["content"] = "example"
    assert second.payload == {}
    with pytest.raises(ValidationError):
        example_event(2, "plugin_event", payload={"not_json": object()})
    with pytest.raises(ValidationError):
        example_event(3, "plugin_event", payload={"not_json": float("nan")})


def test_agent_ownership_is_part_of_session_and_turn_identity():
    other = {**OWNER, "agent_slug": "another-agent"}
    first, second = SessionRecord(**OWNER), SessionRecord(**other)
    assert first.session_id == second.session_id
    assert (first.agent_slug, first.session_id) != (second.agent_slug, second.session_id)
    turn_one, turn_two = TurnRecord(**OWNER, turn_no=1), TurnRecord(**OWNER, turn_no=2)
    assert turn_one.turn_no != turn_two.turn_no


@pytest.mark.parametrize(("model", "data"), [
    (SessionRecord, {**OWNER, "agent_slug": ""}),
    (SessionRecord, {**OWNER, "session_id": " "}),
    (SessionRecord, {**OWNER, "source_revision": -1}),
    (SessionRecord, {**OWNER, "source_revision": True}),
    (SessionRecord, {**OWNER, "created_at": float("nan")}),
    (SessionRecord, {**OWNER, "updated_at": float("inf")}),
    (TurnRecord, {**OWNER, "turn_no": 0}),
    (TurnRecord, {**OWNER, "turn_no": True}),
    (TurnRecord, {**OWNER, "turn_no": "1"}),
    (TurnRecord, {**OWNER, "turn_no": 1, "duration_ms": -1.0}),
    (SessionRecord, {**OWNER, "schema_version": 2}),
    (SessionRecord, {**OWNER, "schema_version": True}),
    (SessionRecord, {**OWNER, "schema_version": 1.0}),
    (SessionRecord, {**OWNER, "sessoin_id": "typo"}),
])
def test_rejects_invalid_or_misspelled_fields(model, data):
    with pytest.raises(ValidationError):
        model(**data)


@pytest.mark.parametrize("changes", [
    {"event_id": ""}, {"sequence": -1}, {"sequence": True}, {"turn_no": 0},
    {"occurred_at": -1.0}, {"duration_ms": float("nan")}, {"duration_ms": -1.0},
])
def test_event_constraints(changes):
    data = {**OWNER, "event_id": "e-1", "sequence": 0, "kind": "llm_call", **changes}
    with pytest.raises(ValidationError):
        ExecutionEvent(**data)
