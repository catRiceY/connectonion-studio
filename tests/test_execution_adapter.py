"""Synthetic corruption/legacy cases, alongside real-framework contract tests."""

from copy import deepcopy
import json
from pathlib import Path

import pytest

from co_studio.execution_adapter import convert_snapshot, read_execution_history


def entry(kind, source_id, **fields):
    return {"type": kind, "id": source_id, "ts": 100.0, **fields}


def snapshot(**overrides):
    raw = {
        "session_id": "session-1", "status": "done", "prompt": "second question",
        "result": "second answer", "created": 100.0, "updated_at": 103.0,
        "revision": 4, "duration_ms": 25,
        "session": {
            "session_id": "session-1", "turn": 2,
            "trace": [
                entry("user_input", "u1", turn=1, content="first question"),
                entry("llm_call", "l1", model="offline", status="running"),
                entry("llm_result", "l1", model="offline", duration_ms=10,
                      status="success", usage={"input_tokens": 8, "output_tokens": 3}),
                entry("turn_result", "r1", turn=1, reason="natural"),
                entry("user_input", "u2", turn=2, content="second question"),
                entry("tool_call", "t-start", tool_id="tool-1", name="read_file",
                      args={"path": "missing.txt"}),
                entry("tool_result", "t-end", tool_id="tool-1", name="read_file",
                      timing_ms=2.5, status="error", result="missing file",
                      error="missing file", error_type="FileNotFoundError"),
                entry("turn_result", "r2", turn=2, reason="natural"),
            ],
        },
    }
    raw.update(overrides)
    return raw


def convert(raw):
    return convert_snapshot(raw, agent_slug="demo")


def write_rows(path, *rows):
    # Synthetic data only, written inside pytest's tmp_path.
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")


def test_normalizes_multiple_turns_and_two_kinds_of_duration():
    result = convert(snapshot())
    assert not result.issues
    assert result.session.source_revision == 4  # Not the number of turns.
    assert [event.turn_no for event in result.events] == [1] * 4 + [2] * 4
    assert [turn.turn_no for turn in result.turns] == [1, 2]
    assert result.turns[0].prompt == "first question"
    assert result.turns[0].result is result.turns[0].duration_ms is None
    assert result.turns[1].result == "second answer"
    assert result.turns[1].duration_ms == 25
    assert result.turns[1].finish_reason == "natural"  # Tool error is not turn failure.
    assert result.events[2].duration_ms == 10
    assert result.events[6].duration_ms == 2.5
    assert result.events[6].payload["error_type"] == "FileNotFoundError"
    assert result.events[1].call_id == result.events[2].call_id == "l1"
    assert result.events[1].source_id == result.events[2].source_id
    assert result.events[1].event_id != result.events[2].event_id
    assert result.events[5].call_id == result.events[6].call_id == "tool-1"
    assert result.events[5].source_id != result.events[6].source_id


@pytest.mark.parametrize("timing, expected", [(None, None), (0, 0), (2.5, 2.5)])
def test_missing_duration_is_not_zero(timing, expected):
    raw = snapshot()
    tool = raw["session"]["trace"][6]
    tool.pop("timing_ms")
    if timing is not None:
        tool["timing_ms"] = timing
    assert convert(raw).events[6].duration_ms == expected


def test_missing_usage_timestamp_id_and_legacy_metadata_stay_unknown():
    raw = snapshot()
    for key in ("created", "updated_at", "revision"):
        raw.pop(key)
    raw["session"]["trace"] = [{"type": "llm_result", "status": "success"}]
    result = convert(raw)
    event = result.events[0]
    assert result.session.created_at is result.session.updated_at is None
    assert result.session.source_revision is None
    assert event.source_id is event.call_id is event.occurred_at is event.turn_no is None
    assert "usage" not in event.payload
    assert result.turns == ()


def test_does_not_mutate_source_or_expose_whole_session():
    raw = snapshot()
    raw["session"]["requester"] = {"address": "private-address"}
    raw["session"]["messages"] = [{"role": "system", "content": "private-system"}]
    raw["metadata"] = {"secret": "private-metadata"}
    raw["session"]["trace"][5]["args"] = {
        "path": "example.txt", "API-Key": "secret-1",
        "nested": [{"Authorization": "secret-2", "input_tokens": 12}],
    }
    raw["session"]["trace"][5]["unknown_raw_field"] = "secret-3"
    before = deepcopy(raw)
    result = convert(raw)
    assert raw == before
    payload = result.events[5].payload
    assert payload["args"]["API-Key"] == "[REDACTED]"
    assert payload["args"]["nested"] == [{"Authorization": "[REDACTED]", "input_tokens": 12}]
    assert "unknown_raw_field" not in payload
    assert "private-system" not in repr(result)
    assert "private-address" not in repr(result)
    assert "private-metadata" not in repr(result)
    payload["args"]["nested"][0]["input_tokens"] = 99
    assert raw == before


def test_free_text_is_not_claimed_to_be_anonymized():
    raw = snapshot()
    raw["session"]["trace"][6]["result"] = "a private free-text answer"
    assert convert(raw).events[6].payload["result"] == "a private free-text answer"


def test_ids_stable_across_read_revision_append_and_front_truncation():
    raw = snapshot()
    original = convert(raw)
    again = convert(deepcopy(raw))
    assert original == again
    raw["revision"] += 1
    raw["updated_at"] += 1
    raw["session"]["trace"].append(entry("plan", "p1", entries=[]))
    appended = convert(raw)
    assert [e.event_id for e in appended.events[:8]] == [e.event_id for e in original.events]
    raw["session"]["trace"] = raw["session"]["trace"][4:]
    truncated = convert(raw)
    assert truncated.events[0].sequence == 0
    assert truncated.events[0].event_id == original.events[4].event_id
    assert truncated.events[0].turn_no == 2


def test_changed_content_at_same_position_is_not_the_same_event():
    raw = snapshot()
    original = convert(raw).events[6]
    raw["session"]["trace"][6]["result"] = "revised result"
    changed = convert(raw).events[6]
    assert changed.sequence == original.sequence
    assert changed.source_id == original.source_id
    assert changed.event_id != original.event_id


def test_ids_and_ownership_separate_agents_and_sessions():
    raw = snapshot()
    one = convert_snapshot(raw, agent_slug="one")
    two = convert_snapshot(raw, agent_slug="two")
    assert one.events[0].event_id != two.events[0].event_id
    assert all(event.agent_slug == "two" for event in two.events)
    assert all(turn.agent_slug == "two" for turn in two.turns)
    raw["session_id"] = raw["session"]["session_id"] = "another-session"
    assert convert_snapshot(raw, agent_slug="one").events[0].event_id != one.events[0].event_id


def test_identical_duplicate_entries_are_unique_but_flagged():
    raw = snapshot()
    raw["session"]["trace"].append(deepcopy(raw["session"]["trace"][6]))
    result = convert(raw)
    assert len({event.event_id for event in result.events}) == 9
    assert any(issue.code == "duplicate_event_content" for issue in result.issues)
    assert result.events[-1].turn_no is None  # After terminal boundary.


def test_unknown_kind_retains_metadata_not_unreviewed_payload():
    raw = snapshot()
    raw["session"]["trace"].append(entry("plugin_future", "future", secret="do-not-copy"))
    result = convert(raw)
    assert result.events[-1].kind == "plugin_future"
    assert result.events[-1].payload == {}
    assert result.issues[-1].code == "unknown_event_kind"


@pytest.mark.parametrize("broken", [None, "oops", {}, {"type": []},
                                     {"type": "llm_result", "duration_ms": -1}])
def test_malformed_entry_is_reported_without_renumbering_trace(broken):
    raw = snapshot()
    raw["session"]["trace"].insert(5, broken)
    result = convert(raw)
    assert [event.sequence for event in result.events] == [0, 1, 2, 3, 4, 6, 7, 8]
    assert any(issue.code == "invalid_event" and issue.sequence == 5 for issue in result.issues)
    assert result.events[5].turn_no is None  # Corruption might have hidden a boundary.


def test_bad_turn_or_missing_boundary_is_not_guessed_from_iteration():
    raw = snapshot()
    raw["session"]["trace"] = [
        entry("llm_call", "a", iteration=7),
        entry("user_input", "u", turn=True, content="bad turn"),
        entry("llm_call", "b", iteration=1),
        entry("user_input", "u2", turn=3, content="valid turn"),
        entry("llm_call", "c", iteration=1),
        entry("turn_result", "r", turn=3, reason="stopped"),
        entry("plan", "p", entries=[]),
    ]
    result = convert(raw)
    assert [event.turn_no for event in result.events] == [None, None, None, 3, 3, 3, None]
    assert [turn.turn_no for turn in result.turns] == [3]
    assert result.turns[0].finish_reason == "stopped"


@pytest.mark.parametrize("status", ["running", "failed", "unknown_future_status"])
def test_host_status_does_not_assign_latest_result_or_duration_to_previous_turn(status):
    result = convert(snapshot(status=status, prompt="third question", result=None))
    assert result.session.source_status == status
    assert result.turns[-1].prompt == "second question"
    assert result.turns[-1].result is result.turns[-1].duration_ms is None
    assert len(result.turns) == 2  # Do not infer turn 3 from Host.prompt.


def test_host_answer_not_attached_when_prompt_does_not_match():
    result = convert(snapshot(prompt="different question"))
    assert result.turns[-1].result is None
    assert result.issues[-1].code == "unattributed_host_result"


@pytest.mark.parametrize("marker", ["runtime_input", "internal"])
def test_additional_user_input_in_same_turn_preserves_primary_prompt(marker):
    raw = snapshot()
    raw["session"]["trace"].insert(5, entry(
        "user_input", "additional", turn=2, content="additional context",
        iteration=1, **{marker: True},
    ))
    result = convert(raw)
    assert not result.issues
    assert len(result.turns) == 2
    assert result.turns[1].prompt == "second question"
    assert result.turns[1].result == "second answer"
    assert result.events[5].turn_no == 2
    assert result.events[5].payload[marker] is True


def test_only_runtime_input_without_primary_does_not_invent_prompt():
    raw = snapshot()
    raw["session"]["trace"] = [
        entry("user_input", "additional", turn=2, content="additional", runtime_input=True),
        entry("turn_result", "r", turn=2, reason="natural"),
    ]
    result = convert(raw)
    assert result.turns[0].prompt is result.turns[0].result is None


@pytest.mark.parametrize("changes", [
    {"session_id": ""}, {"revision": True}, {"created": float("nan")},
    {"status": 2}, {"session": []}, {"session": {"session_id": "wrong"}},
])
def test_invalid_snapshot_has_safe_error(changes):
    raw = snapshot(**changes)
    raw["api_key"] = "private-input-should-not-be-in-error"
    with pytest.raises(ValueError, match="Invalid ConnectOnion Host snapshot") as error:
        convert(raw)
    assert "private-input" not in str(error.value)


def test_non_list_trace_and_legacy_absent_trace_are_not_crashes():
    result = convert(snapshot(session={"trace": "bad"}))
    assert result.events == result.turns == ()
    assert result.issues[0].code == "invalid_trace"
    result = convert(snapshot(session=None, result=None))
    assert result.events == result.turns == ()
    assert not result.issues


def test_last_valid_snapshot_wins_not_largest_revision(tmp_path):
    path = tmp_path / "sessions.jsonl"
    write_rows(path, snapshot(revision=20, result="old"),
               snapshot(revision=1, result="last"), snapshot(status=7))
    result = read_execution_history(path, agent_slug="demo")
    assert len(result.sessions) == 1
    assert result.sessions[0].session.source_revision == 1
    assert result.sessions[0].turns[-1].result == "last"
    assert result.issues[0].code == "invalid_snapshot"
    assert result.issues[0].line_no == 3


def test_invalid_lines_and_torn_tail_do_not_break_earlier_records(tmp_path):
    path = tmp_path / "sessions.jsonl"
    path.write_bytes(
        b"\nnot-json\n" + json.dumps(snapshot()).encode() +
        b"\n\xff\n[]\n" + b'{"session_id":"torn-secret'
    )
    result = read_execution_history(path, agent_slug="demo")
    assert len(result.sessions) == 1
    assert [issue.line_no for issue in result.issues] == [2, 4, 5, 6]
    assert "torn-secret" not in repr(result.issues)


def test_final_complete_row_without_newline_is_accepted(tmp_path):
    path = tmp_path / "sessions.jsonl"
    path.write_text(json.dumps(snapshot()))
    assert len(read_execution_history(path, agent_slug="demo").sessions) == 1


def test_missing_source_is_readonly_and_empty_source_is_distinct(tmp_path):
    path = tmp_path / "absent" / "sessions.jsonl"
    result = read_execution_history(path, agent_slug="demo")
    assert not result.source_exists
    assert not path.parent.exists()
    path = tmp_path / "empty.jsonl"
    path.write_text("")
    result = read_execution_history(path, agent_slug="demo")
    assert result.source_exists
    assert result.sessions == ()


def test_read_does_not_write_or_create_framework_sidecars(tmp_path):
    path = tmp_path / "sessions.jsonl"
    write_rows(path, snapshot(expires=1))  # History reader doesn't apply Host TTL.
    before = path.read_bytes(), path.stat().st_mtime_ns, set(tmp_path.iterdir())
    first = read_execution_history(path, agent_slug="demo")
    assert len(first.sessions) == 1
    assert read_execution_history(path, agent_slug="demo") == first
    assert (path.read_bytes(), path.stat().st_mtime_ns, set(tmp_path.iterdir())) == before


def test_permission_error_is_not_disguised_as_empty_history(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("denied")
    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(PermissionError):
        read_execution_history(tmp_path / "sessions.jsonl", agent_slug="demo")


def test_multiple_sessions_and_conversion_issue_line_numbers(tmp_path):
    one = snapshot()
    two = snapshot(session_id="session-2")
    two["session"]["session_id"] = "session-2"
    two["session"]["trace"].append(entry("future", "x"))
    path = tmp_path / "sessions.jsonl"
    write_rows(path, one, two, one)
    result = read_execution_history(path, agent_slug="demo")
    assert [session.session.session_id for session in result.sessions] == ["session-1", "session-2"]
    assert result.issues[0].line_no == 2
    assert result.issues[0].sequence == 8


def test_concurrent_append_is_left_for_next_read(tmp_path, monkeypatch):
    path = tmp_path / "sessions.jsonl"
    first = snapshot(result="initial")
    second = snapshot(result="appended")
    write_rows(path, first)
    import co_studio.execution_adapter as adapter
    original = adapter.os.fstat

    def fstat_then_append(fd):
        stat = original(fd)
        with path.open("ab") as handle:
            handle.write((json.dumps(second) + "\n").encode())
        return stat

    monkeypatch.setattr(adapter.os, "fstat", fstat_then_append)
    result = read_execution_history(path, agent_slug="demo")
    assert result.sessions[0].turns[-1].result == "initial"
    monkeypatch.setattr(adapter.os, "fstat", original)
    assert read_execution_history(path, agent_slug="demo").sessions[0].turns[-1].result == "appended"


def first_completion():
    raw = snapshot(prompt="first question", result="first answer", duration_ms=12,
                   revision=2, updated_at=101.0)
    raw["session"]["turn"] = 1
    raw["session"]["trace"] = raw["session"]["trace"][:4]
    return raw


def test_reader_restores_completed_turns_without_merging_old_trace(tmp_path):
    path = tmp_path / "sessions.jsonl"
    first, latest = first_completion(), snapshot()
    write_rows(path, first, latest)
    before = path.read_bytes(), path.stat().st_mtime_ns
    history = read_execution_history(path, agent_slug="demo")
    assert history.issues == ()
    result = history.sessions[0]
    assert [(turn.result, turn.duration_ms) for turn in result.turns] == [
        ("first answer", 12), ("second answer", 25),
    ]
    single = convert(latest)
    assert result.session == single.session
    assert result.events == single.events
    assert [turn.prompt for turn in result.turns] == [turn.prompt for turn in single.turns]
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


@pytest.mark.parametrize("changed", ["prompt", "primary_id", "terminal_id", "tool_result", "timestamp"])
def test_same_number_or_prompt_is_not_enough_to_recover_an_answer(tmp_path, changed):
    first, latest = first_completion(), snapshot()
    trace = latest["session"]["trace"]
    if changed == "prompt":
        trace[0]["content"] = "different question"
    elif changed == "primary_id":
        trace[0]["id"] = "new-attempt"
    elif changed == "terminal_id":
        trace[3]["id"] = "new-terminal"
    elif changed == "tool_result":
        # A change even inside the same boundaries invalidates the fingerprint.
        trace[2]["status"] = "error"
    else:
        trace[1]["ts"] = 110.0
    path = tmp_path / "sessions.jsonl"
    write_rows(path, first, latest)
    turn = read_execution_history(path, agent_slug="demo").sessions[0].turns[0]
    assert turn.result is turn.duration_ms is None


@pytest.mark.parametrize("malformed", ["extra_primary", "extra_terminal", "no_primary_id", "bad_event"])
def test_ambiguous_completion_is_not_a_donor_even_when_trace_matches(tmp_path, malformed):
    first, latest = first_completion(), snapshot()
    if malformed == "extra_primary":
        first["session"]["trace"].insert(1, entry("user_input", "another", turn=1, content="first question"))
    elif malformed == "extra_terminal":
        first["session"]["trace"].append(entry("turn_result", "another", turn=1, reason="natural"))
    elif malformed == "no_primary_id":
        first["session"]["trace"][0].pop("id")
    else:
        first["session"]["trace"].insert(1, "broken")
    latest["session"]["trace"] = deepcopy(first["session"]["trace"]) + latest["session"]["trace"][4:]
    path = tmp_path / "sessions.jsonl"
    write_rows(path, first, latest)
    turn = read_execution_history(path, agent_slug="demo").sessions[0].turns[0]
    assert turn.result is turn.duration_ms is None


@pytest.mark.parametrize("changes", [
    {"status": "running"}, {"status": "failed"}, {"prompt": "different question"},
    {"created": 99.0}, {"revision": 4}, {"revision": 10}, {"updated_at": 999.0},
])
def test_unfinished_unmatched_or_stale_snapshot_cannot_donate(tmp_path, changes):
    first = first_completion()
    first.update(changes)
    path = tmp_path / "sessions.jsonl"
    write_rows(path, first, snapshot())
    turn = read_execution_history(path, agent_slug="demo").sessions[0].turns[0]
    assert turn.result is turn.duration_ms is None


@pytest.mark.parametrize("status", ["running", "failed"])
def test_saved_completed_turn_survives_host_starting_or_failing_next_request(tmp_path, status):
    first = first_completion()
    latest = deepcopy(first)
    latest.update(status=status, prompt="next question", result=None, duration_ms=None,
                  revision=3, updated_at=102.0)
    path = tmp_path / "sessions.jsonl"
    write_rows(path, first, latest)
    result = read_execution_history(path, agent_slug="demo").sessions[0]
    assert result.session.source_status == status
    assert len(result.turns) == 1  # No invented new turn from Host.prompt.
    assert result.turns[0].prompt == "first question"
    assert result.turns[0].result == "first answer"
    assert result.turns[0].duration_ms == 12


@pytest.mark.parametrize("result,duration", [("", 0), (None, None), ("corrected", 15)])
def test_last_completion_is_authoritative_including_empty_or_missing_fields(tmp_path, result, duration):
    first = first_completion()
    correction = deepcopy(first)
    correction.update(result=result, duration_ms=duration, revision=3, updated_at=102.0)
    path = tmp_path / "sessions.jsonl"
    write_rows(path, first, correction, snapshot())
    restored = read_execution_history(path, agent_slug="demo").sessions[0].turns[0]
    assert restored.result == result
    assert restored.duration_ms == duration
    write_rows(path, first, correction)
    current = read_execution_history(path, agent_slug="demo").sessions[0].turns[0]
    assert current.result == result
    assert current.duration_ms == duration


def test_missing_legacy_metadata_allows_exact_identity_match(tmp_path):
    first, latest = first_completion(), snapshot()
    for raw in (first, latest):
        for key in ("created", "updated_at", "revision"):
            raw.pop(key)
    path = tmp_path / "sessions.jsonl"
    write_rows(path, first, latest)
    assert read_execution_history(path, agent_slug="demo").sessions[0].turns[0].result == "first answer"


def test_removed_turn_and_other_sessions_are_never_resurrected(tmp_path):
    first, latest = first_completion(), snapshot()
    latest["session"]["trace"] = latest["session"]["trace"][4:]
    path = tmp_path / "sessions.jsonl"
    write_rows(path, first, latest)
    result = read_execution_history(path, agent_slug="demo").sessions[0]
    assert [turn.turn_no for turn in result.turns] == [2]
    assert len(result.events) == 4
    other = first_completion()
    other["session_id"] = other["session"]["session_id"] = "different-session"
    write_rows(path, other, snapshot())
    result = next(item for item in read_execution_history(path, agent_slug="demo").sessions
                  if item.session.session_id == "session-1")
    assert result.turns[0].result is None


@pytest.mark.parametrize("marker", ["runtime_input", "internal"])
def test_exact_runtime_inputs_do_not_prevent_restore_or_replace_primary_prompt(tmp_path, marker):
    first, latest = first_completion(), snapshot()
    first["session"]["trace"].insert(1, entry(
        "user_input", "extra-input", turn=1, content="additional context", **{marker: True},
    ))
    latest["session"]["trace"] = deepcopy(first["session"]["trace"]) + latest["session"]["trace"][4:]
    path = tmp_path / "sessions.jsonl"
    write_rows(path, first, latest)
    turn = read_execution_history(path, agent_slug="demo").sessions[0].turns[0]
    assert (turn.prompt, turn.result, turn.duration_ms) == ("first question", "first answer", 12)
    latest["session"]["trace"][1]["content"] = "changed additional input"
    write_rows(path, first, latest)
    assert read_execution_history(path, agent_slug="demo").sessions[0].turns[0].result is None


def test_compacted_away_completions_remain_unknown_without_a_stale_memory_cache(tmp_path):
    path = tmp_path / "sessions.jsonl"
    write_rows(path, first_completion(), snapshot())
    assert read_execution_history(path, agent_slug="demo").sessions[0].turns[0].result == "first answer"
    write_rows(path, snapshot())  # Emulate upstream compact retaining only latest.
    turn = read_execution_history(path, agent_slug="demo").sessions[0].turns[0]
    assert turn.result is turn.duration_ms is None


@pytest.mark.parametrize("also_in_donor", [False, True])
def test_unassigned_boundary_inside_turn_prevents_recovery(tmp_path, also_in_donor):
    first, latest = first_completion(), snapshot()
    # This resets active_turn without an explicit malformed value; it is not a
    # conversion error, but filtering by turn alone would hide the interruption.
    unknown = entry("user_input", "unassigned-input", content="Which turn is this?")
    latest["session"]["trace"].insert(3, deepcopy(unknown))
    if also_in_donor:
        first["session"]["trace"].insert(3, deepcopy(unknown))
    path = tmp_path / "sessions.jsonl"
    write_rows(path, first, latest)
    history = read_execution_history(path, agent_slug="demo")
    assert history.issues == ()
    turn = history.sessions[0].turns[0]
    assert turn.result is turn.duration_ms is None
