"""Timeline facts exposed by the existing file and fixed-capture HTTP APIs.

All traces are synthetic. The real adapter, privacy projection and routers run
against tmp_path and an in-memory archive reader. TestClient never enters the
application lifespan; no Agent, model, PostgreSQL connection or sync is started.
These contracts preserve observations for a UI; they do not implement pairing
or manufacture completion records on the server.
"""

from copy import deepcopy
import json
from types import SimpleNamespace
from urllib.parse import quote

from fastapi.testclient import TestClient
import pytest

from co_studio import config, history_archive, prompt_versions, registry
from co_studio.api import archives as archive_api, sessions as sessions_api
from co_studio.app import create_app
from co_studio.execution_adapter import convert_snapshot
from co_studio.execution_models import TurnRecord
from co_studio.history_archive import prepare_capture


def entry(kind, source_id=None, **fields):
    return {"type": kind, **({"id": source_id} if source_id is not None else {}), **fields}


def snapshot(trace, *, turn=1, session_id="timeline-session", **fields):
    return {
        "session_id": session_id, "status": "running", "prompt": "question",
        "revision": 1, "created": 100.0, "updated_at": 200.0,
        "session": {"session_id": session_id, "turn": turn, "trace": trace},
        **fields,
    }


def forbidden(*args, **kwargs):
    pytest.fail("Timeline reads must not initialize, persist, connect or start an Agent")


class MemoryCaptureReader:
    def __init__(self):
        self.captures = {}
        self.calls = []

    def get_owned(self, slug, capture_id):
        self.calls.append((slug, capture_id))
        capture = self.captures.get(capture_id)
        if capture is None or capture.session.agent_slug != slug:
            return None
        return {"capture": capture, "archived_at": "2026-10-28T00:00:00Z"}

    initialize = save = forbidden


@pytest.fixture
def timeline_api(tmp_path, monkeypatch):
    monkeypatch.delenv("STUDIO_HISTORY_DATABASE_URL", raising=False)
    monkeypatch.delenv("STUDIO_HISTORY_CONFIG", raising=False)
    monkeypatch.setattr(history_archive, "_driver", forbidden)
    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    directory = agents / "timeline"
    directory.mkdir()
    registry.save(registry.AgentMeta(
        slug="timeline", name="Synthetic timeline", address="0xsynthetic", port=9991,
        model="offline", capabilities=[], created_at="2026-10-28T00:00:00Z",
    ))
    monkeypatch.setattr(registry, "save", forbidden)
    monkeypatch.setattr(registry, "ensure_dirs", forbidden)
    monkeypatch.setattr(registry, "migrate_capability_aliases", forbidden)
    source = directory / ".co" / "session_results.jsonl"
    source.parent.mkdir()
    store = MemoryCaptureReader()
    monkeypatch.setattr(archive_api, "_archive", lambda: store)
    client = TestClient(create_app(), base_url="http://127.0.0.1:9900",
                        client=("127.0.0.1", 50000))

    def publish(raw, backend, *, provenance_verifier=None):
        if backend == "file":
            source.write_text(json.dumps(raw) + "\n", encoding="utf-8")
            return f"/api/agents/timeline/sessions/{quote(raw['session_id'], safe='')}"
        verification = {"provenance_verifier": provenance_verifier} if provenance_verifier is not None else {}
        capture = prepare_capture(convert_snapshot(raw, agent_slug="timeline", **verification))
        store.captures[capture.capture_id] = capture
        return f"/api/agents/timeline/archives/{capture.capture_id}"

    def get(path, **params):
        before = (source.read_bytes(), source.stat().st_mtime_ns) if source.exists() else None
        response = client.get(path, params=params)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        if before is not None:
            assert (source.read_bytes(), source.stat().st_mtime_ns) == before
        else:
            assert not source.exists()
        return response.json()

    try:
        yield SimpleNamespace(publish=publish, get=get, store=store, source=source)
    finally:
        client.close()


@pytest.fixture
def verified_provenance(timeline_api, monkeypatch):
    # These tests cover response projection, not runtime signatures. Trust only
    # their synthetic provenance through an explicit verifier; production reads
    # must authenticate the actual Studio-owned event before projecting it.
    from co_studio import prompt_provenance

    def verify(raw, agent_slug, session_id, **kwargs):
        return (raw.get("type") == "prompt_provenance" and agent_slug == "timeline"
                and session_id == "timeline-session")

    def verifier_for(agent_dir):
        assert agent_dir == timeline_api.source.parent.parent.resolve()
        return verify

    monkeypatch.setattr(prompt_provenance, "event_verifier", verifier_for)
    publish = timeline_api.publish
    monkeypatch.setattr(timeline_api, "publish", lambda raw, backend:
                        publish(raw, backend, provenance_verifier=verify))
    return lambda raw: convert_snapshot(raw, agent_slug="timeline", provenance_verifier=verify)


@pytest.mark.parametrize("backend", ["file", "archive"])
def test_timeline_uses_trace_sequence_even_when_timestamps_run_backwards(timeline_api, backend):
    raw = snapshot([
        entry("user_input", "user", turn=1, content="question", ts=110.0),
        entry("llm_call", "model-call", ts=120.0),
        entry("llm_result", "model-call", ts=105.0, status="error", error_type="TimeoutError"),
        entry("tool_call", "tool-start", tool_id="tool-call", ts=100.0, name="lookup"),
        entry("tool_result", "tool-end", tool_id="tool-call", ts=100.0, result="observed"),
    ])
    body = timeline_api.get(timeline_api.publish(raw, backend))
    events = body["events"]["items"]
    assert [event["sequence"] for event in events] == [0, 1, 2, 3, 4]
    assert [event["occurred_at"] for event in events] == [110, 120, 105, 100, 100]
    assert [event["kind"] for event in events] == [event["type"] for event in raw["session"]["trace"]]
    assert events[1]["source_id"] == events[2]["source_id"] == "model-call"
    assert len({event["event_id"] for event in events}) == 5


@pytest.mark.parametrize("backend", ["file", "archive"])
def test_turn_filters_isolate_reused_call_ids_and_preserve_call_families(timeline_api, backend):
    trace = [entry("plan", "unassigned", entries=[])]
    for turn in (1, 2):
        trace.extend([
            entry("user_input", f"user-{turn}", turn=turn, content=f"question {turn}"),
            entry("llm_call", "shared", model=f"model-{turn}"),
            entry("tool_call", f"tool-start-{turn}", tool_id="shared", name=f"lookup_{turn}"),
            entry("llm_result", "shared", status="success", iteration=turn),
            entry("tool_result", f"tool-end-{turn}", tool_id="shared", result=f"turn {turn}"),
            entry("turn_result", f"end-{turn}", turn=turn, reason="natural"),
        ])
    trace.append(entry("tool_result", "late", tool_id="shared", result="unassigned late observation"))
    path = timeline_api.publish(snapshot(trace, turn=2), backend)
    whole = timeline_api.get(path)
    assert whole["events"]["items"][0]["turn_no"] is None
    assert whole["events"]["items"][-1]["turn_no"] is None
    for turn in (1, 2):
        body = timeline_api.get(path, turn_no=turn, event_offset=1, event_limit=4)
        assert [item["turn_no"] for item in body["turns"]["items"]] == [turn]
        events = body["events"]["items"]
        assert [item["kind"] for item in events] == ["llm_call", "tool_call", "llm_result", "tool_result"]
        assert all(item["turn_no"] == turn and item["call_id"] == "shared" for item in events)
        assert events[1]["payload"]["name"] == f"lookup_{turn}"
        assert events[3]["payload"]["result"] == f"turn {turn}"
        assert body["events"]["total"] == 6 and body["events"]["next_offset"] == 5
    assert timeline_api.get(path, turn_no=3)["events"]["items"] == []


@pytest.mark.parametrize("backend", ["file", "archive"])
def test_missing_call_or_result_remains_an_observation_without_inferred_success(timeline_api, backend):
    trace = [
        entry("user_input", "user", turn=1, content="question"),
        entry("tool_call", "pending-start", tool_id="pending-tool", name="unfinished", ts=100.0),
        entry("tool_result", "orphan-result", tool_id="orphan-tool", name="orphan", ts=190.0),
        entry("llm_call", "pending-model", ts=110.0),
        entry("llm_result", "orphan-model", ts=180.0),
        entry("turn_result", "end", turn=1, reason="max_iterations"),
    ]
    body = timeline_api.get(timeline_api.publish(snapshot(trace, status="done"), backend))
    assert [item["kind"] for item in body["events"]["items"]] == [item["type"] for item in trace]
    for item in body["events"]["items"]:
        assert item["duration_ms"] is None
        assert "status" not in item["payload"]
        assert "success" not in item and "success" not in item["payload"]
    turn = body["turns"]["items"][0]
    assert turn["result"] is None and turn["duration_ms"] is None
    assert turn["finish_reason"] == "max_iterations"
    assert "success" not in turn and "success" not in body["session"]


@pytest.mark.parametrize("backend", ["file", "archive"])
def test_mcp_attempt_observations_survive_both_history_read_paths(timeline_api, backend):
    raw = snapshot([
        entry("user_input", "user", turn=1, content="question"),
        entry("tool_call", "request", tool_id="read-1", name="studio_read_doc"),
        entry("studio_mcp_attempt", "attempt-1", tool_id="read-1", turn=1,
              name="studio_read_doc", attempt=1, max_attempts=2, status="transport_error",
              retry_scheduled=True, error_type="ConnectionError", duration_ms=12, elapsed_ms=20,
              args={"token": "synthetic-secret"}, result="unreviewed-result", authorization="allow"),
        entry("studio_mcp_attempt", "attempt-2", tool_id="read-1", turn=1,
              name="studio_read_doc", attempt=2, max_attempts=2, status="success",
              retry_scheduled=False, error_type=None, duration_ms=9, elapsed_ms=129),
    ])
    path = timeline_api.publish(raw, backend)
    body = timeline_api.get(path, turn_no=1)
    first, second = body["events"]["items"][2:]
    assert first["kind"] == second["kind"] == "studio_mcp_attempt"
    assert first["call_id"] == second["call_id"] == "read-1"
    assert first["event_id"] != second["event_id"]
    assert first["payload"] == {
        "name": "studio_read_doc", "attempt": 1, "max_attempts": 2,
        "status": "transport_error", "retry_scheduled": True,
        "error_type": "ConnectionError", "elapsed_ms": 20,
    }
    assert second["payload"]["attempt"] == 2 and second["payload"]["status"] == "success"
    assert [first["duration_ms"], second["duration_ms"]] == [12, 9]
    assert "synthetic-secret" not in json.dumps(body)
    assert "unreviewed-result" not in json.dumps(body)
    assert body["turns"]["items"][0]["result"] is None
    assert not any(event["kind"] == "tool_result" for event in body["events"]["items"])


@pytest.mark.parametrize("backend", ["file", "archive"])
def test_tool_native_ids_are_not_a_fallback_pairing_key(timeline_api, backend):
    path = timeline_api.publish(snapshot([
        entry("user_input", "user", turn=1, content="question"),
        entry("tool_call", "reused-native-id", name="lookup"),
        entry("tool_result", "reused-native-id", result="unpaired"),
        entry("llm_call", "model-id", tool_id="irrelevant-tool-field"),
        entry("llm_result", "model-id", tool_id="different-irrelevant-field"),
        entry("llm_result", status="error", error_type="UnknownModelCall"),
    ]), backend)
    events = timeline_api.get(path)["events"]["items"]
    assert events[1]["source_id"] == events[2]["source_id"] == "reused-native-id"
    assert events[1]["call_id"] is events[2]["call_id"] is None
    assert events[3]["call_id"] == events[4]["call_id"] == "model-id"
    assert events[5]["call_id"] is events[5]["source_id"] is None
    assert len({item["event_id"] for item in events}) == len(events)


@pytest.mark.parametrize("backend", ["file", "archive"])
def test_duration_uses_recorded_family_field_and_distinguishes_zero_from_missing(timeline_api, backend):
    path = timeline_api.publish(snapshot([
        entry("user_input", "user", turn=1, content="question"),
        entry("tool_call", "start", tool_id="tool", ts=100.0),
        entry("tool_result", "zero", tool_id="tool", ts=200.0, timing_ms=0, duration_ms=987),
        entry("tool_result", "unknown", tool_id="other", ts=300.0, duration_ms=987),
        entry("llm_result", "model-zero", duration_ms=0, timing_ms=987),
        entry("llm_result", "model-unknown", timing_ms=987),
    ]), backend)
    events = timeline_api.get(path)["events"]["items"]
    assert [event["duration_ms"] for event in events] == [None, None, 0, None, 0, None]


@pytest.mark.parametrize("backend", ["file", "archive"])
def test_error_payload_remains_readable_with_existing_credential_redaction(timeline_api, backend):
    raw = snapshot([
        entry("user_input", "user", turn=1, content="question"),
        entry("tool_result", "failure", tool_id="failed-call", name="lookup_order", status="error",
              args={"order_id": "DEMO-7", "password": "synthetic-secret"},
              result={"code": "ORDER_UNAVAILABLE", "retryable": True},
              error="Lookup refused for DEMO-7; password=synthetic-secret",
              error_type="LookupError", declined=False, unknown_raw_field="do-not-expose"),
        entry("llm_result", "failed-model", status="error", error_type="TimeoutError"),
        entry("turn_result", "end", turn=1, reason="natural"),
    ], status="done", result="Reported the observed errors")
    before = deepcopy(raw)
    body = timeline_api.get(timeline_api.publish(raw, backend))
    tool = body["events"]["items"][1]
    assert tool["payload"] == {
        "name": "lookup_order", "status": "error", "error_type": "LookupError", "declined": False,
        "args": {"order_id": "DEMO-7", "password": "[REDACTED]"},
        "result": {"code": "ORDER_UNAVAILABLE", "retryable": True},
        "error": "Lookup refused for DEMO-7; password=[REDACTED]",
    }
    assert body["events"]["items"][2]["payload"] == {"status": "error", "error_type": "TimeoutError"}
    assert body["session"]["source_status"] == "done"
    assert body["turns"]["items"][0]["finish_reason"] == "natural"
    assert tool["duration_ms"] is None
    assert "synthetic-secret" not in json.dumps(body)
    assert raw == before


@pytest.mark.parametrize("backend", ["file", "archive"])
def test_duplicate_native_records_are_not_silently_dropped_or_reused_as_event_identity(timeline_api, backend):
    result = entry("tool_result", "duplicate-native", tool_id="tool", result="same result", ts=100.0)
    raw = snapshot([
        entry("user_input", "user", turn=1, content="question"),
        result, deepcopy(result), {**result, "result": "a distinct observation"},
    ])
    path = timeline_api.publish(raw, backend)
    first = timeline_api.get(path)
    second = timeline_api.get(path)
    events = first["events"]["items"]
    assert first == second
    assert [event["sequence"] for event in events] == [0, 1, 2, 3]
    assert [event["source_id"] for event in events[1:]] == ["duplicate-native"] * 3
    assert len({event["event_id"] for event in events}) == 4
    assert events[1]["event_id"].rsplit(":", 1)[0] == events[2]["event_id"].rsplit(":", 1)[0]
    assert events[1]["event_id"].endswith(":0") and events[2]["event_id"].endswith(":1")
    assert first["issue_counts"] == {"duplicate_event_content": 1}


@pytest.mark.parametrize("backend", ["file", "archive"])
def test_corrupt_boundaries_leave_sequence_gaps_and_do_not_leak_events_into_previous_turn(timeline_api, backend):
    raw = snapshot([
        entry("user_input", "user-1", turn=1, content="question one"),
        entry("tool_call", "start", tool_id="shared", name="lookup"),
        None,
        entry("tool_result", "after-corruption", tool_id="shared", result="unknown turn"),
        entry("user_input", "invalid-turn", turn=0, content="invalid boundary"),
        entry("llm_call", "unassigned-model", iteration=2),
        entry("user_input", "user-2", turn=2, content="question two"),
        entry("tool_result", "turn-two", tool_id="shared", result="second turn"),
        entry("turn_result", "end", turn=2, reason="natural"),
    ], turn=2)
    path = timeline_api.publish(raw, backend)
    body = timeline_api.get(path)
    assert [item["sequence"] for item in body["events"]["items"]] == [0, 1, 3, 4, 5, 6, 7, 8]
    assert [item["turn_no"] for item in body["events"]["items"]] == [1, 1, None, None, None, 2, 2, 2]
    assert body["issue_counts"] == {"invalid_event": 1, "invalid_turn": 1}
    assert [item["sequence"] for item in timeline_api.get(path, turn_no=1)["events"]["items"]] == [0, 1]
    assert [item["sequence"] for item in timeline_api.get(path, turn_no=2)["events"]["items"]] == [6, 7, 8]


@pytest.mark.parametrize("backend", ["file", "archive"])
def test_explicit_event_turn_does_not_change_inherited_turn_and_terminal_closes_it(timeline_api, backend):
    path = timeline_api.publish(snapshot([
        entry("user_input", "user-three", turn=3, content="third question"),
        entry("tool_call", "start", tool_id="pair", name="lookup"),
        entry("tool_result", "explicit-one", tool_id="pair", turn=1, result="explicit first turn"),
        entry("llm_call", "still-three", model="offline"),
        entry("turn_result", "end-one", turn=1, reason="natural"),
        entry("llm_result", "still-three", status="success"),
    ], turn=3), backend)
    body = timeline_api.get(path)
    assert [item["turn_no"] for item in body["turns"]["items"]] == [1, 3]
    assert [item["turn_no"] for item in body["events"]["items"]] == [3, 3, 1, 3, 1, None]
    assert body["turns"]["items"][0]["prompt"] is None
    assert body["turns"]["items"][1]["finish_reason"] is None
    assert [item["sequence"] for item in timeline_api.get(path, turn_no=3)["events"]["items"]] == [0, 1, 3]
    assert [item["sequence"] for item in timeline_api.get(path, turn_no=1)["events"]["items"]] == [2, 4]


@pytest.mark.parametrize("backend", ["file", "archive"])
def test_pairing_metadata_survives_a_call_and_result_split_across_event_pages(timeline_api, backend):
    raw = snapshot([
        entry("user_input", "user", turn=1, content="question"),
        entry("tool_call", "start", tool_id="pair", name="lookup"),
        entry("plan", "intervening", entries=[{"description": "Wait for lookup"}]),
        entry("tool_result", "end", tool_id="pair", status="success", timing_ms=5, result="observed"),
    ])
    path = timeline_api.publish(raw, backend)
    first = timeline_api.get(path, turn_no=1, event_limit=2)["events"]
    assert first["next_offset"] == 2
    second = timeline_api.get(path, turn_no=1, event_offset=first["next_offset"], event_limit=2)["events"]
    start, end = first["items"][1], second["items"][1]
    assert (start["kind"], end["kind"]) == ("tool_call", "tool_result")
    assert (start["call_id"], start["turn_no"]) == (end["call_id"], end["turn_no"]) == ("pair", 1)
    assert start["event_id"] != end["event_id"]
    assert [item["sequence"] for item in first["items"] + second["items"]] == [0, 1, 2, 3]
    assert first["total"] == second["total"] == 4 and second["next_offset"] is None


def test_fixed_capture_paging_does_not_switch_to_newer_timeline(timeline_api, monkeypatch):
    old = snapshot([
        entry("user_input", "user", turn=1, content="question"),
        entry("llm_call", "model", model="offline"),
        entry("llm_result", "model", status="success", duration_ms=1),
        entry("tool_call", "start", tool_id="tool", name="lookup"),
        entry("tool_result", "end", tool_id="tool", result="old observation"),
        entry("turn_result", "done", turn=1, reason="natural"),
    ], status="done", result="old answer", duration_ms=10)
    old_path = timeline_api.publish(old, "archive")
    first = timeline_api.get(old_path, event_limit=3)
    newer = deepcopy(old)
    newer["revision"] = 2
    newer["result"] = "new answer"
    newer["session"]["trace"][4]["result"] = "new observation"
    new_path = timeline_api.publish(newer, "archive")
    assert old_path != new_path
    monkeypatch.setattr(sessions_api, "_history", forbidden)
    second = timeline_api.get(old_path, event_offset=first["events"]["next_offset"], event_limit=3)
    current = timeline_api.get(new_path)
    assert second["capture_id"] == first["capture_id"]
    assert second["session"]["source_revision"] == first["session"]["source_revision"] == 1
    assert second["turns"]["items"][0]["result"] == "old answer"
    assert second["events"]["items"][1]["payload"]["result"] == "old observation"
    assert current["events"]["items"][4]["payload"]["result"] == "new observation"
    assert second["events"]["items"][1]["source_id"] == current["events"]["items"][4]["source_id"]
    assert second["events"]["items"][1]["event_id"] != current["events"]["items"][4]["event_id"]
    combined = first["events"]["items"] + second["events"]["items"]
    assert [item["sequence"] for item in combined] == list(range(6))
    assert len({item["event_id"] for item in combined}) == 6
    assert timeline_api.store.calls == [
        ("timeline", old_path.rsplit("/", 1)[1]),
        ("timeline", old_path.rsplit("/", 1)[1]),
        ("timeline", new_path.rsplit("/", 1)[1]),
    ]


def provenance_entry(call_id, *, turn=1, version_id="a" * 32, base_sha256="b" * 64,
                     system_sha256="c" * 64, model="offline"):
    return entry(
        "prompt_provenance", f"provenance-{turn}-{call_id}", turn=turn,
        provenance_version=1, status="bound", version_id=version_id,
        base_sha256=base_sha256, system_sha256=system_sha256,
        launch_version_id=None, llm_call_id=call_id, model=model, reason="new_session",
    )


def bound_prompt(version_id="a" * 32, base_sha256="b" * 64):
    return {"status": "bound", "version_id": version_id,
            "base_sha256": base_sha256, "reason": "recorded"}


@pytest.mark.parametrize("backend", ["file", "archive"])
def test_prompt_provenance_projects_complete_events_before_independent_pagination(timeline_api, backend, verified_provenance):
    trace = []
    for number in (1, 2):
        trace.append(entry("user_input", f"user-{number}", turn=number, content=f"question {number}"))
        for iteration in (1, 2):
            call_id = f"call-{number}-{iteration}"
            trace.extend([
                entry("llm_call", call_id, model="offline"),
                provenance_entry(call_id, turn=number, system_sha256=str(iteration) * 64),
                entry("llm_result", call_id, model="offline"),
            ])
        trace.append(entry("turn_result", f"end-{number}", turn=number, reason="natural"))
    path = timeline_api.publish(snapshot(trace, turn=2), backend)
    for turn_offset in (0, 1):
        for event_offset in (0, 3, len(trace)):
            body = timeline_api.get(path, turn_offset=turn_offset, turn_limit=1,
                                    event_offset=event_offset, event_limit=1)
            assert [item["turn_no"] for item in body["turns"]["items"]] == [turn_offset + 1]
            assert body["turns"]["items"][0]["prompt_provenance"] == bound_prompt()
            assert body["turns"]["total"] == 2
            assert body["events"]["total"] == len(trace)
            assert len(body["events"]["items"]) == (0 if event_offset == len(trace) else 1)
    filtered = timeline_api.get(path, turn_no=2, event_offset=len(trace), event_limit=1)
    assert filtered["events"]["items"] == []
    assert filtered["turns"]["items"][0]["prompt_provenance"] == bound_prompt()


@pytest.mark.parametrize("backend", ["file", "archive"])
def test_prompt_provenance_isolates_turns_that_reuse_a_model_call_id(timeline_api, backend, verified_provenance):
    trace = []
    expected = {1: bound_prompt("baseline", "1" * 64), 2: bound_prompt()}
    for number, binding in expected.items():
        trace.extend([
            entry("user_input", f"user-{number}", turn=number, content=f"question {number}"),
            entry("llm_call", "shared-call", model=f"offline-{number}"),
            provenance_entry("shared-call", turn=number, model=f"offline-{number}",
                             version_id=binding["version_id"], base_sha256=binding["base_sha256"]),
            entry("llm_result", "shared-call", model=f"offline-{number}"),
            entry("turn_result", f"end-{number}", turn=number, reason="natural"),
        ])
    path = timeline_api.publish(snapshot(trace, turn=2), backend)
    body = timeline_api.get(path)
    assert {item["turn_no"]: item["prompt_provenance"] for item in body["turns"]["items"]} == expected
    for number, binding in expected.items():
        filtered = timeline_api.get(path, turn_no=number, event_limit=1)
        assert filtered["turns"]["items"][0]["prompt_provenance"] == binding


@pytest.mark.parametrize("backend", ["file", "archive"])
def test_prompt_provenance_is_incomplete_when_any_model_call_lacks_its_record(timeline_api, backend, verified_provenance):
    for missing in ("first", "second"):
        trace = [entry("user_input", "user", turn=1, content="question")]
        for call_id in ("first", "second"):
            trace.append(entry("llm_call", call_id, model="offline"))
            if call_id != missing:
                trace.append(provenance_entry(call_id))
            trace.append(entry("llm_result", call_id, model="offline"))
        trace.append(entry("turn_result", "end", turn=1, reason="natural"))
        body = timeline_api.get(timeline_api.publish(snapshot(trace), backend), event_limit=1)
        assert body["turns"]["items"][0]["prompt_provenance"] == {
            "status": "unknown", "version_id": None, "base_sha256": None, "reason": "incomplete",
        }


@pytest.mark.parametrize("backend", ["file", "archive"])
def test_legacy_prompt_provenance_never_reads_current_prompt_configuration(timeline_api, backend, monkeypatch):
    # state() exposes current; _records() resolves current.json. None may supply
    # missing historical evidence, even when the Agent has current settings.
    for name in ("state", "get", "_records", "_supported_baseline"):
        monkeypatch.setattr(prompt_versions.PromptVersions, name, forbidden)
    monkeypatch.setattr(prompt_versions, "_json", forbidden)
    current = timeline_api.source.parent.parent / ".studio-prompts" / "current.json"
    current.parent.mkdir()
    current.write_text(json.dumps({"version_id": "d" * 32}), encoding="utf-8")
    before = current.read_bytes(), current.stat().st_mtime_ns
    path = timeline_api.publish(snapshot([
        entry("user_input", "user", turn=1, content="question"),
        entry("llm_call", "model-call", model="offline"),
        entry("llm_result", "model-call", model="offline"),
        entry("turn_result", "end", turn=1, reason="natural"),
    ]), backend)
    body = timeline_api.get(path, event_offset=4, event_limit=1)
    assert body["turns"]["items"][0]["prompt_provenance"] == {
        "status": "unknown", "version_id": None, "base_sha256": None, "reason": "not_recorded",
    }
    assert (current.read_bytes(), current.stat().st_mtime_ns) == before


def test_new_capture_does_not_change_an_older_captures_prompt_binding(timeline_api, monkeypatch, verified_provenance):
    old = snapshot([
        entry("user_input", "user", turn=1, content="question"),
        entry("llm_call", "model-call", model="offline"),
        provenance_entry("model-call", version_id="baseline", base_sha256="1" * 64),
        entry("llm_result", "model-call", model="offline"),
        entry("turn_result", "end", turn=1, reason="natural"),
    ])
    old_path = timeline_api.publish(old, "archive")
    first = timeline_api.get(old_path, event_limit=1)
    newer = deepcopy(old)
    newer["revision"] = 2
    newer["session"]["trace"][2] = provenance_entry("model-call")
    new_path = timeline_api.publish(newer, "archive")
    assert old_path != new_path
    monkeypatch.setattr(sessions_api, "_history", forbidden)
    current = timeline_api.get(new_path, event_limit=1)
    older = timeline_api.get(old_path, event_offset=99, event_limit=1)
    assert first["turns"]["items"][0]["prompt_provenance"] == bound_prompt("baseline", "1" * 64)
    assert older["turns"] == first["turns"]
    assert older["capture_id"] == first["capture_id"]
    assert older["events"]["items"] == []
    assert current["turns"]["items"][0]["prompt_provenance"] == bound_prompt()


@pytest.mark.parametrize("backend", ["file", "archive"])
def test_prompt_provenance_view_never_mutates_turn_records_or_capture_identity(timeline_api, backend, monkeypatch, verified_provenance):
    raw = snapshot([
        entry("user_input", "user", turn=1, content="question"),
        entry("llm_call", "model-call", model="offline"),
        provenance_entry("model-call"),
        entry("llm_result", "model-call", model="offline"),
    ])
    path = timeline_api.publish(raw, backend)
    observed = []
    if backend == "file":
        read_history = sessions_api._history

        def retain_original_models(slug):
            history = read_history(slug)
            for converted in history.sessions:
                observed.append((converted.turns, deepcopy(converted.turns)))
            return history

        monkeypatch.setattr(sessions_api, "_history", retain_original_models)
    else:
        capture = timeline_api.store.captures[path.rsplit("/", 1)[1]]
        before = deepcopy(capture)
        observed.append((capture.turns, deepcopy(capture.turns)))
    fingerprint = prepare_capture(verified_provenance(raw)).capture_id
    body = timeline_api.get(path, event_limit=1)
    assert body["turns"]["items"][0]["prompt_provenance"] == bound_prompt()
    assert "prompt_provenance" not in TurnRecord.model_fields
    assert observed
    for turns, originals in observed:
        assert turns == originals
        assert all(type(turn) is TurnRecord and "prompt_provenance" not in turn.model_dump() for turn in turns)
    if backend == "archive":
        assert timeline_api.store.captures[capture.capture_id] == before
        assert capture.capture_id == fingerprint
    else:
        persisted = json.loads(timeline_api.source.read_text(encoding="utf-8"))
        assert persisted == raw
        assert prepare_capture(verified_provenance(persisted)).capture_id == fingerprint
