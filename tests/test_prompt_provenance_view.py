"""Read-only attribution from synthetic traces; no runtime, Agent or database."""

from copy import deepcopy
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from co_studio import history_archive, prompt_provenance
from co_studio.api.sessions import _public_turns
from co_studio.execution_adapter import convert_snapshot, read_execution_history, read_execution_stream
from co_studio.prompt_provenance_view import project_prompt_provenance


VERSION = "a" * 32
BASE_SHA = "b" * 64
SYSTEM_SHA = "c" * 64
OWNER = {"agent_slug": "synthetic", "session_id": "session"}


def payload(call_id="call", **overrides):
    return {
        "provenance_version": 1, "status": "bound", "version_id": VERSION,
        "base_sha256": BASE_SHA, "system_sha256": SYSTEM_SHA,
        "launch_version_id": VERSION, "llm_call_id": call_id, "model": "offline",
        "reason": "new_session", **overrides,
    }


def recorded(call_id="call", **overrides):
    return {"type": "prompt_provenance", "id": f"provenance-{call_id}", **payload(call_id, **overrides)}


def call(call_id="call", **overrides):
    return {"type": "llm_call", "id": call_id, "model": "offline", **overrides}


def raw_snapshot(trace, **overrides):
    return {
        "session_id": "session", "status": "running", "prompt": "question",
        "revision": 1, "created": 100.0, "updated_at": 101.0,
        "session": {"session_id": "session", "turn": 1, "trace": trace},
        **overrides,
    }


def converted(*events):
    return convert_snapshot(raw_snapshot([
        {"type": "user_input", "id": "input", "turn": 1, "content": "question"},
        *events,
    ]), agent_slug=OWNER["agent_slug"], provenance_verifier=lambda *_: True)


def view(*events):
    data = converted(*events)
    result = project_prompt_provenance(data.events, **OWNER)
    return result.get(1)


def assert_unknown(result, reason):
    assert result.model_dump() == {
        "status": "unknown", "version_id": None, "base_sha256": None, "reason": reason,
    }


def test_all_calls_must_record_same_actual_base_but_may_have_distinct_effective_systems():
    result = view(
        call("first"), recorded("first", launch_version_id="d" * 32),
        call("second"), recorded("second", system_sha256="e" * 64, launch_version_id="baseline"),
    )
    assert result.model_dump() == {
        "status": "bound", "version_id": VERSION, "base_sha256": BASE_SHA, "reason": "recorded",
    }


def test_baseline_is_explicit_not_a_missing_version_fallback():
    assert view(call(), recorded(version_id="baseline", launch_version_id=None)).version_id == "baseline"
    assert_unknown(view(call(), recorded(version_id=None)), "invalid")


def test_legacy_calls_and_results_have_unknown_not_recorded_attribution():
    assert_unknown(view(call(), {"type": "llm_result", "id": "call", "status": "success"}), "not_recorded")
    data = converted({"type": "tool_call", "id": "tool", "name": "offline"})
    assert project_prompt_provenance(data.events, **OWNER) == {}
    assert_unknown(_public_turns(data.turns, data.events)[0].prompt_provenance, "not_recorded")


@pytest.mark.parametrize("events", [
    [call("first"), recorded("first"), call("second")],
    [recorded()],
    [{"type": "llm_call", "model": "offline"}, recorded()],
    [call(), recorded(), recorded("extra")],
    [call(), recorded(), {"type": "llm_result", "id": "missing-call"}],
    [call(), recorded(), {"type": "llm_result"}],
])
def test_partial_or_unpairable_recordings_cannot_claim_entire_turn_coverage(events):
    assert_unknown(view(*events), "incomplete")


def test_a_missing_or_failed_result_does_not_change_known_input_or_invent_success():
    pending = converted(call(), recorded())
    failed = converted(call(), recorded(), {
        "type": "llm_result", "id": "call", "status": "error", "error_type": "TimeoutError",
    })
    for data in (pending, failed):
        projection = _public_turns(data.turns, data.events)[0]
        assert projection.prompt_provenance.status == "bound"
        assert projection.result is projection.duration_ms is projection.finish_reason is None
        assert "success" not in projection.model_dump()
    assert failed.events[-1].payload == {"status": "error", "error_type": "TimeoutError"}


@pytest.mark.parametrize("overrides", [
    {"provenance_version": True}, {"provenance_version": 2},
    {"status": "success"}, {"version_id": "current"}, {"version_id": "A" * 32},
    {"base_sha256": "b" * 63}, {"system_sha256": "C" * 64},
    {"system_sha256": None}, {"base_sha256": None},
    {"launch_version_id": "<img src=x>"}, {"llm_call_id": "with whitespace"},
    {"llm_call_id": None}, {"model": ["offline"]}, {"model": ""},
    {"model": "other-model"}, {"reason": "<script>not an enum</script>"},
    {"reason": "unrecognized_enum"}, {"reason": "unverified_session"},
    {"reason": ["new_session"]},
])
def test_invalid_metadata_remains_unknown_without_discarding_trace_events(overrides):
    data = converted(call(), recorded(**overrides), {"type": "llm_result", "id": "call"})
    assert data.issues == ()
    assert [event.kind for event in data.events] == ["user_input", "llm_call", "prompt_provenance", "llm_result"]
    assert [event.turn_no for event in data.events] == [1, 1, 1, 1]
    assert_unknown(project_prompt_provenance(data.events, **OWNER)[1], "invalid")


def test_all_fields_are_required_even_when_nullable():
    incomplete = recorded()
    del incomplete["launch_version_id"]
    assert_unknown(view(call(), incomplete), "invalid")


@pytest.mark.parametrize("extra", [{"prompt": "not an allowed field"}, {"future": True}])
def test_direct_stored_payload_with_unrecognized_fields_cannot_be_called_bound(extra):
    data = converted(call(), recorded())
    event = data.events[-1].model_copy(update={"payload": {**data.events[-1].payload, **extra}})
    assert_unknown(project_prompt_provenance((*data.events[:-1], event), **OWNER)[1], "invalid")


def test_unknown_actual_version_is_never_recovered_from_launch_version():
    result = view(call(), recorded(
        status="unknown", version_id=None, base_sha256=None, system_sha256=None,
        launch_version_id=VERSION, reason="unverified_session",
    ))
    assert_unknown(result, "unbound")


@pytest.mark.parametrize("events", [
    [call(), recorded(), deepcopy(recorded())],
    [call(), call(), recorded()],
    [call("first"), recorded("first"), call("second"), recorded("second", version_id="d" * 32)],
    [call("first"), recorded("first"), call("second"), recorded("second", base_sha256="d" * 64)],
])
def test_conflicting_or_duplicate_evidence_is_not_resolved_by_latest_or_majority(events):
    assert_unknown(view(*events), "conflict")


@pytest.mark.parametrize("field,value", [("agent_slug", "other"), ("session_id", "other")])
def test_foreign_owner_evidence_cannot_bind_local_calls(field, value):
    data = converted(call(), recorded())
    foreign = data.events[-1].model_copy(update={field: value})
    assert_unknown(project_prompt_provenance((*data.events[:-1], foreign), **OWNER)[1], "invalid")


def test_allowlist_retains_only_metadata_and_projection_does_not_mutate_persisted_models():
    raw = raw_snapshot([
        {"type": "user_input", "id": "input", "turn": 1, "content": "question"},
        call(), {**recorded(), "prompt": "private base body", "system": "private effective body",
                 "messages": [{"role": "system", "content": "private effective body"}],
                 "api_key": "synthetic-secret", "unknown": "discard"},
    ])
    before = deepcopy(raw)
    data = convert_snapshot(raw, agent_slug="synthetic", provenance_verifier=lambda *_: True)
    assert data.events[-1].payload == payload()
    capture = history_archive.prepare_capture(data)
    before_capture = deepcopy(capture)
    public = _public_turns(capture.turns, capture.events)
    assert public[0].prompt_provenance.status == "bound"
    assert capture == before_capture and raw == before
    assert "prompt_provenance" not in capture.turns[0].model_dump()
    assert "prompt_provenance" not in capture.session.model_dump()
    assert history_archive._validated_capture(capture).capture_id == capture.capture_id
    assert "private" not in json.dumps([event.model_dump() for event in capture.events])


def test_added_provenance_inside_turn_does_not_break_completed_answer_restoration(tmp_path, monkeypatch):
    monkeypatch.setattr(prompt_provenance, "event_verifier", lambda _: lambda *_: True)
    trace = [
        {"type": "user_input", "id": "first-input", "turn": 1, "content": "question"},
        call(), recorded(), {"type": "llm_result", "id": "call", "status": "success"},
        {"type": "turn_result", "id": "first-end", "turn": 1, "reason": "natural"},
    ]
    first = raw_snapshot(trace, status="done", result="first answer", duration_ms=8.0)
    second = raw_snapshot([
        *trace, {"type": "user_input", "id": "second-input", "turn": 2, "content": "next question"},
    ], prompt="next question", revision=2, updated_at=102.0)
    second["session"]["turn"] = 2
    source = tmp_path / "session_results.jsonl"
    source.write_text("\n".join(json.dumps(item) for item in (first, second)) + "\n", encoding="utf-8")
    data = read_execution_history(source, agent_slug="synthetic").sessions[0]
    turns = _public_turns(data.turns, data.events)
    assert turns[0].result == "first answer" and turns[0].duration_ms == 8
    assert turns[0].prompt_provenance.status == "bound"
    assert turns[1].result is turns[1].duration_ms is None
    assert_unknown(turns[1].prompt_provenance, "not_recorded")


@pytest.mark.parametrize("verifier", [None, lambda *_: False, lambda *_: "truthy-but-not-bool"])
def test_unverified_raw_provenance_cannot_claim_actual_prompt_or_expose_signature(verifier):
    raw = raw_snapshot([
        {"type": "user_input", "id": "input", "turn": 1, "content": "question"},
        call(), {**recorded(), "_studio_signature": "synthetic-private-proof"},
    ])
    before = deepcopy(raw)
    data = convert_snapshot(raw, agent_slug="synthetic", provenance_verifier=verifier)
    event = data.events[-1]
    assert event.payload == {**payload(), "status": "unknown", "version_id": None,
                             "base_sha256": None, "reason": "unverified_event"}
    assert_unknown(project_prompt_provenance(data.events, **OWNER)[1], "unbound")
    capture = history_archive.prepare_capture(data)
    assert "synthetic-private-proof" not in repr(capture)
    assert raw == before


def test_verification_exception_fails_closed_without_losing_turn_boundary():
    def unavailable(*args):
        raise OSError("Do not expose a private path or key")

    raw = raw_snapshot([
        {"type": "user_input", "id": "input", "turn": 1, "content": "question"},
        call(), recorded(), {"type": "llm_result", "id": "call"},
    ])
    data = convert_snapshot(raw, agent_slug="synthetic", provenance_verifier=unavailable)
    assert data.issues == () and [event.turn_no for event in data.events] == [1] * 4
    assert data.events[2].payload["reason"] == "unverified_event"
    assert "private path" not in repr(data)


def test_file_reader_initializes_verifier_once_lazily_and_checks_every_processed_snapshot(tmp_path, monkeypatch):
    agent_dir = tmp_path / "synthetic"
    source = agent_dir / ".co" / "session_results.jsonl"
    source.parent.mkdir(parents=True)
    initialized = []
    calls = []

    def factory(directory):
        initialized.append(directory)

        def verify(raw, slug, session_id):
            calls.append((raw["id"], slug, session_id))
            return raw["id"] != "tampered"

        return verify

    monkeypatch.setattr(prompt_provenance, "event_verifier", factory)
    legacy = raw_snapshot([{"type": "user_input", "id": "input", "turn": 1, "content": "question"}])
    source.write_text(json.dumps(legacy) + "\n", encoding="utf-8")
    read_execution_history(source, agent_slug="synthetic")
    assert initialized == calls == []

    trace = [*legacy["session"]["trace"], call(), recorded(),
             {"type": "turn_result", "id": "end", "turn": 1, "reason": "natural"}]
    complete = raw_snapshot(trace, status="done", result="done")
    latest = raw_snapshot([
        *trace, {"type": "user_input", "id": "input-2", "turn": 2, "content": "second"},
        call("second"), {**recorded("second"), "id": "tampered"},
    ], revision=2)
    latest["session"]["turn"] = 2
    source.write_text("\n".join(json.dumps(item) for item in (complete, latest)) + "\n", encoding="utf-8")
    result = read_execution_history(source, agent_slug="synthetic").sessions[0]
    assert initialized == [agent_dir.resolve()]
    assert calls == [("provenance-call", "synthetic", "session")] * 2 + [("tampered", "synthetic", "session")]
    projection = project_prompt_provenance(result.events, **OWNER)
    assert projection[1].status == "bound"
    assert_unknown(projection[2], "unbound")


def test_stream_reader_passes_the_explicit_verifier_without_loading_any_key(tmp_path, monkeypatch):
    def forbidden(*args):
        pytest.fail("An already-open stream must use its caller's verifier")

    monkeypatch.setattr(prompt_provenance, "event_verifier", forbidden)
    raw = raw_snapshot([
        {"type": "user_input", "id": "input", "turn": 1, "content": "question"},
        call(), recorded(),
    ])
    source = tmp_path / "source.jsonl"
    source.write_text(json.dumps(raw) + "\n", encoding="utf-8")
    checked = []

    def verify(raw, slug, session_id):
        checked.append((raw["id"], slug, session_id))
        return True

    with source.open("rb") as handle:
        data = read_execution_stream(handle, agent_slug="synthetic", provenance_verifier=verify).sessions[0]
    assert checked == [("provenance-call", "synthetic", "session")]
    assert project_prompt_provenance(data.events, **OWNER)[1].status == "bound"


def test_real_signed_event_is_verified_from_temporary_local_key_and_tampering_is_unknown(tmp_path):
    agent_dir = tmp_path / "synthetic"
    prompt_dir = agent_dir / ".studio-prompts"
    prompt_dir.mkdir(parents=True)
    key = bytes.fromhex("0f" * 32)
    key_path = prompt_dir / "provenance.key"
    key_path.write_text(key.hex(), encoding="utf-8")
    key_path.chmod(0o600)
    source = agent_dir / ".co" / "session_results.jsonl"
    source.parent.mkdir()
    event = {**recorded(), "turn": 1}
    event["_studio_signature"] = prompt_provenance._sign(key, "studio-prompt-event-v1", {
        **OWNER, "event": event,
    })
    raw = raw_snapshot([
        {"type": "user_input", "id": "input", "turn": 1, "content": "question"},
        call(), event,
    ])
    source.write_text(json.dumps(raw) + "\n", encoding="utf-8")
    data = read_execution_history(source, agent_slug="synthetic").sessions[0]
    capture = history_archive.prepare_capture(data)
    assert _public_turns(capture.turns, capture.events)[0].prompt_provenance.status == "bound"
    assert all("_studio_signature" not in item.payload for item in capture.events)
    assert key.hex() not in repr(capture)
    for tampered in ({**event, "version_id": "d" * 32}, {**event, "turn": 2}):
        raw["session"]["trace"][-1] = tampered
        source.write_text(json.dumps(raw) + "\n", encoding="utf-8")
        unverified = read_execution_history(source, agent_slug="synthetic").sessions[0]
        metadata = unverified.events[-1].payload
        assert metadata["status"] == "unknown" and metadata["reason"] == "unverified_event"
        assert metadata["version_id"] is metadata["base_sha256"] is None
        assert all(turn.prompt_provenance.status == "unknown" for turn in _public_turns(unverified.turns, unverified.events))
    # Archived, validated facts are stable even after the live source changes.
    assert history_archive._validated_capture(capture).capture_id == capture.capture_id
    assert _public_turns(capture.turns, capture.events)[0].prompt_provenance.version_id == VERSION


def test_frozen_legacy_v1_capture_hash_and_postgres_read_remain_compatible(monkeypatch):
    # Fixed before provenance existed, not recomputed from the new projection.
    fixed_id = "7f157b6f6f1c76e38cfa4635a7f3878e573ff70bd89f1794f663cf03d0e7c0e2"
    raw = {
        "session_id": "legacy-session", "status": "done", "prompt": "Legacy question",
        "result": "Legacy answer", "created": 100.0, "updated_at": 101.0, "revision": 1,
        "duration_ms": 25.0,
        "session": {"session_id": "legacy-session", "turn": 1, "trace": [
            {"type": "user_input", "id": "old-input", "turn": 1, "content": "Legacy question", "ts": 100.0},
            {"type": "llm_call", "id": "old-model", "model": "offline", "ts": 100.1},
            {"type": "llm_result", "id": "old-model", "model": "offline", "status": "success", "ts": 100.2},
            {"type": "turn_result", "id": "old-result", "turn": 1, "reason": "natural", "ts": 101.0},
        ]},
    }
    capture = history_archive.prepare_capture(convert_snapshot(raw, agent_slug="legacy-agent"))
    assert capture.capture_id == fixed_id
    assert history_archive._validated_capture(capture).capture_id == fixed_id
    # Exercise the actual PostgreSQL read/revalidate path with JSON-shaped rows,
    # without opening a database, migrating schema or writing any capture.
    stored_session = json.loads(capture.session.model_dump_json())
    stored_turns = [(json.loads(turn.model_dump_json()),) for turn in capture.turns]
    stored_events = [(json.loads(event.model_dump_json()),) for event in capture.events]
    queries = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, sql, args=None):
            queries.append((sql, args))
            assert sql.startswith(("SELECT", "SET TRANSACTION"))
            if "schema_version" in sql:
                return SimpleNamespace(fetchone=lambda: (1,))
            if "FROM studio_history.captures" in sql:
                assert args == (fixed_id, "legacy-agent")
                return SimpleNamespace(fetchone=lambda: (
                    stored_session, {}, len(stored_turns), len(stored_events),
                    "legacy-agent", "legacy-session", datetime(2026, 1, 1, tzinfo=timezone.utc),
                ))
            if "FROM studio_history.turns" in sql:
                return SimpleNamespace(fetchall=lambda: stored_turns)
            if "FROM studio_history.events" in sql:
                return SimpleNamespace(fetchall=lambda: stored_events)
            return None

    class DriverError(Exception):
        pass

    store = history_archive.PostgresArchive("postgresql://synthetic.invalid/not-used")
    monkeypatch.setattr(history_archive, "_driver", lambda: SimpleNamespace(Error=DriverError))
    monkeypatch.setattr(store, "_connect", Connection)
    loaded = store.get_owned("legacy-agent", fixed_id)["capture"]
    assert loaded.capture_id == fixed_id and len(queries) == 5
    projected = _public_turns(loaded.turns, loaded.events)[0]
    assert_unknown(projected.prompt_provenance, "not_recorded")
    assert projected.result == "Legacy answer" and projected.duration_ms == 25
    assert history_archive._validated_capture(loaded).capture_id == fixed_id
    assert all("prompt_provenance" not in row[0] for row in stored_turns)
