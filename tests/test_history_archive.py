"""Pure archive validation/CLI regressions: synthetic records, no PostgreSQL.

These tests deliberately do not create an Agent, start a service, read a real
history/config file or connect to a database. PostgreSQL integration is separate.
"""

import builtins
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from co_studio import history_archive as archive
from co_studio.execution_adapter import ConvertedSession, ReadIssue
from co_studio.execution_models import ExecutionEvent, SessionRecord, TurnRecord


@pytest.fixture(autouse=True)
def no_personal_archive_config(monkeypatch):
    monkeypatch.delenv("STUDIO_HISTORY_DATABASE_URL", raising=False)


@pytest.fixture
def converted():
    owner = {"agent_slug": "synthetic-agent", "session_id": "opaque/session-1"}
    return ConvertedSession(
        session=SessionRecord(**owner, source_status="done", source_revision=3,
                              created_at=100.0, updated_at=105.0),
        turns=(
            TurnRecord(**owner, turn_no=1, prompt="first question", result="first answer",
                       duration_ms=0.0, finish_reason="natural"),
            TurnRecord(**owner, turn_no=3, prompt="third question", result=None),
        ),
        events=(
            ExecutionEvent(**owner, event_id="event-0", sequence=0, kind="user_input",
                           turn_no=1, payload={"content": "first question"}),
            ExecutionEvent(**owner, event_id="event-2", sequence=2, kind="tool_result",
                           turn_no=3, source_id="shared-source", call_id="tool-call",
                           payload={"name": "lookup", "result": "synthetic result",
                                    "usage": {"input_tokens": 8, "output_tokens": 2}}),
            ExecutionEvent(**owner, event_id="event-4", sequence=4, kind="runtime_input",
                           turn_no=None, source_id="shared-source", payload={"content": "unassigned"}),
        ),
    )


def test_prepare_capture_accepts_gaps_and_unassigned_events_without_inventing_values(converted):
    capture = archive.prepare_capture(converted)
    assert len(capture.capture_id) == 64
    assert set(capture.capture_id) <= set("0123456789abcdef")
    assert [turn.turn_no for turn in capture.turns] == [1, 3]
    assert [event.sequence for event in capture.events] == [0, 2, 4]
    assert capture.events[-1].turn_no is None
    assert capture.turns[0].duration_ms == 0
    assert capture.turns[1].result is capture.turns[1].duration_ms is None
    assert capture.session.source_status == "done"
    assert capture.issue_counts == {}


@pytest.mark.parametrize("collection", ["turns", "events"])
@pytest.mark.parametrize("field,value", [("agent_slug", "another-agent"), ("session_id", "another-session")])
def test_prepare_capture_rejects_cross_owner_children(converted, collection, field, value):
    items = getattr(converted, collection)
    modified = items[0].model_copy(update={field: value})
    malformed = replace(converted, **{collection: (modified, *items[1:])})
    with pytest.raises(archive.ArchiveError, match="ownership"):
        archive.prepare_capture(malformed)


@pytest.mark.parametrize("collection,field", [("turns", "turn_no"), ("events", "sequence")])
@pytest.mark.parametrize("violation", ["duplicate", "decreasing"])
def test_prepare_capture_requires_unique_increasing_order(converted, collection, field, violation):
    items = list(getattr(converted, collection))
    if violation == "duplicate":
        items[1] = items[1].model_copy(update={field: getattr(items[0], field)})
    else:
        items.reverse()
    with pytest.raises(archive.ArchiveError, match="order"):
        archive.prepare_capture(replace(converted, **{collection: tuple(items)}))


def test_prepare_capture_rejects_duplicate_event_ids_even_at_distinct_sequences(converted):
    events = (*converted.events[:2], converted.events[2].model_copy(update={"event_id": "event-0"}))
    with pytest.raises(archive.ArchiveError, match="identity"):
        archive.prepare_capture(replace(converted, events=events))


def test_prepare_capture_rejects_an_event_referencing_a_missing_turn(converted):
    events = (converted.events[0].model_copy(update={"turn_no": 2}), *converted.events[1:])
    with pytest.raises(archive.ArchiveError, match="unknown turn"):
        archive.prepare_capture(replace(converted, events=events))


def test_prepare_capture_revalidates_models_built_without_validation(converted):
    invalid = converted.turns[0].model_copy(update={"turn_no": True})
    with pytest.raises(ValidationError):
        archive.prepare_capture(replace(converted, turns=(invalid,)))


def test_capture_is_detached_from_source_including_nested_payloads(converted):
    before = deepcopy(converted)
    capture = archive.prepare_capture(converted)
    assert converted == before
    assert capture.session is not converted.session
    assert capture.turns[0] is not converted.turns[0]
    assert capture.events[1] is not converted.events[1]
    capture.events[1].payload["usage"]["input_tokens"] = 999
    assert converted == before


def test_same_normalized_capture_has_a_deterministic_canonical_fingerprint(converted):
    first = archive.prepare_capture(converted)
    second = archive.prepare_capture(deepcopy(converted))
    assert first.capture_id == second.capture_id
    canonical = {
        "format_version": 1, "redaction_version": 1,
        "session": first.session.model_dump(),
        "turns": [turn.model_dump() for turn in first.turns],
        "events": [event.model_dump() for event in first.events],
        "issue_counts": first.issue_counts,
    }
    # Independent expected representation for the fixture's known numbers:
    # JSONB can return integral JSON floats as integers. The fingerprint must
    # encode their values alike instead of retaining Python's '.0' spelling.
    canonical["session"]["created_at"] = 100
    canonical["session"]["updated_at"] = 105
    canonical["turns"][0]["duration_ms"] = 0
    encoded = json.dumps(canonical, sort_keys=True, ensure_ascii=True, allow_nan=False,
                         separators=(",", ":")).encode()
    assert first.capture_id == hashlib.sha256(encoded).hexdigest()


@pytest.mark.parametrize("value,expected", [
    (0, "0"), (0.0, "0"), (-0.0, "0"),
    (1, "1"), (1.0, "1"), (-123.0, "-123"),
    (1.2300, "1.23"), (1e-7, "0.0000001"), (-1.234e-7, "-0.0000001234"),
    (1e20, "100000000000000000000"),
    (1.2345678901234567e30, "12345678901234567" + "0" * 14),
    (1e100, "1" + "0" * 100),
    (2**53 + 1, "9007199254740993"),
    (1234567890123456789012345678901234567890, "1234567890123456789012345678901234567890"),
    ("1", '"1"'), (True, "true"), (False, "false"),
])
def test_canonical_json_uses_exact_type_aware_numeric_spellings(value, expected):
    assert archive._canonical_json(value) == expected


def test_canonical_json_normalizes_nested_numbers_and_sorts_keys():
    value = {"z": True, "numbers": [-0.0, 1e20, 1.0, 1.2300], "a": "1"}
    assert archive._canonical_json(value) == (
        '{"a":"1","numbers":[0,100000000000000000000,1,1.23],"z":true}'
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_canonical_json_rejects_nonfinite_values(value):
    with pytest.raises(archive.ArchiveError, match="Non-finite"):
        archive._canonical_json({"nested": [value]})


def capture_with_value(converted, value):
    events = (converted.events[0].model_copy(update={"payload": {"value": value}}), *converted.events[1:])
    return archive.prepare_capture(replace(converted, events=events))


@pytest.mark.parametrize("before,after", [
    (-0.0, 0), (0.0, 0), (1.0, 1),
    (1e20, 100000000000000000000),
    (1.2345678901234567e30, 1234567890123456700000000000000),
    ({"amount": 1e20, "trace": [-0.0, 1.0, 1e-7]},
     {"trace": [0, 1, 0.0000001], "amount": 100000000000000000000}),
])
def test_jsonb_equivalent_numeric_values_keep_the_same_capture_fingerprint(converted, before, after):
    original = capture_with_value(converted, before)
    reloaded = capture_with_value(converted, after)
    assert original.capture_id == reloaded.capture_id
    # Simulate a JSONB read projection, retaining the original saved fingerprint.
    assert archive._validated_capture(replace(original, events=reloaded.events)).capture_id == original.capture_id


@pytest.mark.parametrize("lower,higher", [
    (2**53, 2**53 + 1),
    (1234567890123456789012345678901234567890, 1234567890123456789012345678901234567891),
    (10**100, 10**100 + 1),
])
def test_large_adjacent_integers_do_not_lose_precision_or_collide(converted, lower, higher):
    assert archive._canonical_json(lower) == str(lower)
    assert archive._canonical_json(higher) == str(higher)
    assert capture_with_value(converted, lower).capture_id != capture_with_value(converted, higher).capture_id


def test_number_string_and_boolean_are_distinct_fingerprint_values(converted):
    numeric = capture_with_value(converted, 1)
    assert numeric.capture_id == capture_with_value(converted, 1.0).capture_id
    assert len({numeric.capture_id, capture_with_value(converted, "1").capture_id,
                capture_with_value(converted, True).capture_id}) == 3
    assert capture_with_value(converted, 0).capture_id != capture_with_value(converted, False).capture_id


def test_json_key_order_does_not_change_the_fingerprint(converted):
    first = archive.prepare_capture(converted)
    payload = converted.events[1].payload
    reordered = {"usage": {"output_tokens": 2, "input_tokens": 8},
                 "result": payload["result"], "name": payload["name"]}
    events = (converted.events[0], converted.events[1].model_copy(update={"payload": reordered}), converted.events[2])
    assert archive.prepare_capture(replace(converted, events=events)).capture_id == first.capture_id


@pytest.mark.parametrize("change", ["revision", "timestamp", "answer", "event_payload", "issues", "owner"])
def test_changed_observation_produces_a_distinct_capture_not_an_overwrite(converted, change):
    if change == "revision":
        changed = replace(converted, session=converted.session.model_copy(update={"source_revision": 4}))
    elif change == "timestamp":
        changed = replace(converted, session=converted.session.model_copy(update={"updated_at": 106.0}))
    elif change == "answer":
        changed = replace(converted, turns=(converted.turns[0], converted.turns[1].model_copy(update={"result": "new answer"})))
    elif change == "event_payload":
        changed = replace(converted, events=(converted.events[0].model_copy(update={"payload": {"content": "new input"}}), *converted.events[1:]))
    elif change == "issues":
        changed = replace(converted, issues=(ReadIssue("invalid_event"),))
    else:
        changed = replace(converted,
                          session=converted.session.model_copy(update={"agent_slug": "another-agent"}),
                          turns=tuple(turn.model_copy(update={"agent_slug": "another-agent"}) for turn in converted.turns),
                          events=tuple(event.model_copy(update={"agent_slug": "another-agent"}) for event in converted.events))
    assert archive.prepare_capture(changed).capture_id != archive.prepare_capture(converted).capture_id


def with_credentials(converted, value):
    return replace(converted,
                   session=converted.session.model_copy(update={"source_status": f"api_key={value}"}),
                   turns=(converted.turns[0].model_copy(update={
                       "prompt": f"password={value}", "result": f"api_key={value}",
                       "finish_reason": f"secret={value}",
                   }), converted.turns[1]),
                   events=(converted.events[0].model_copy(update={"payload": {
                       "args": {"Authorization": f"Bearer {value}", "order_id": "order-1"},
                       "result": f"password={value}",
                   }}), *converted.events[1:]))


def test_redaction_happens_before_fingerprinting_and_does_not_mutate_source(converted):
    raw = with_credentials(converted, "synthetic-secret-one")
    before = deepcopy(raw)
    first = archive.prepare_capture(raw)
    second = archive.prepare_capture(with_credentials(converted, "synthetic-secret-two"))
    assert first.capture_id == second.capture_id, "only the API-redacted projection should influence the hash"
    assert "synthetic-secret-one" not in repr(first)
    assert first.session.source_status == "api_key=[REDACTED]"
    assert first.turns[0].prompt == "password=[REDACTED]"
    assert first.turns[0].result == "api_key=[REDACTED]"
    assert first.turns[0].finish_reason == "secret=[REDACTED]"
    assert first.events[0].payload["args"] == {"Authorization": "[REDACTED]", "order_id": "order-1"}
    assert raw == before


def test_issue_summary_counts_codes_not_source_locations_or_order(converted):
    first = replace(converted, issues=(ReadIssue("invalid_event", 2, 4), ReadIssue("invalid_event", 3, 8), ReadIssue("unknown_kind", 9, 3)))
    second = replace(converted, issues=(ReadIssue("unknown_kind", 90, 30), ReadIssue("invalid_event"), ReadIssue("invalid_event")))
    first_capture = archive.prepare_capture(first)
    assert first_capture.issue_counts == {"invalid_event": 2, "unknown_kind": 1}
    assert first_capture.capture_id == archive.prepare_capture(second).capture_id
    assert "line_no" not in repr(first_capture.issue_counts)


def test_validated_capture_accepts_the_prepared_projection(converted):
    capture = archive.prepare_capture(replace(converted, issues=(ReadIssue("invalid_event"),)))
    assert archive._validated_capture(capture) == capture


@pytest.mark.parametrize("mutation", ["fingerprint", "content", "ownership"])
def test_validated_capture_rejects_hand_constructed_tampering(converted, mutation):
    capture = archive.prepare_capture(converted)
    if mutation == "fingerprint":
        invalid = replace(capture, capture_id="0" * 64)
    elif mutation == "content":
        invalid = replace(capture, turns=(capture.turns[0].model_copy(update={"result": "tampered"}), capture.turns[1]))
    else:
        invalid = replace(capture, events=(capture.events[0].model_copy(update={"agent_slug": "wrong-agent"}), *capture.events[1:]))
    with pytest.raises(archive.ArchiveError):
        archive._validated_capture(invalid)


@pytest.mark.parametrize("issue_counts", [
    {"invalid_event": -1}, {"invalid_event": 0}, {"invalid_event": True}, {"invalid_event": 1.5},
    {"invalid_event": "1"}, {"invalid_event": 100001}, {"": 1},
    {"invalid event": 1}, {"secret=value": 1}, {3: 1}, {"非英文": 1},
    {"_": 1}, {"a" * 65: 1}, None, [],
])
def test_validated_capture_rejects_invalid_issue_summaries(converted, issue_counts):
    capture = replace(archive.prepare_capture(converted), issue_counts=issue_counts)
    with pytest.raises(archive.ArchiveError, match="issue summary"):
        archive._validated_capture(capture)


@pytest.mark.parametrize("dsn", [None, "", " ", 12])
def test_archive_rejects_missing_dsn_without_loading_the_driver(dsn):
    with pytest.raises(archive.ArchiveError, match="not configured"):
        archive.PostgresArchive(dsn)


def test_archive_construction_never_loads_a_driver_or_connects(monkeypatch):
    def forbidden_driver():
        raise AssertionError("constructing the archive must not load or connect a database driver")
    monkeypatch.setattr(archive, "_driver", forbidden_driver)
    archive.PostgresArchive("postgresql://synthetic.invalid/archive")


@pytest.mark.parametrize("command", ["initialize", "save", "get", "list_captures"])
def test_missing_optional_driver_has_a_clear_safe_error(monkeypatch, converted, command):
    original_import = builtins.__import__
    def no_psycopg(name, *args, **kwargs):
        if name == "psycopg" or name.startswith("psycopg."):
            raise ImportError("simulated optional dependency absence")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", no_psycopg)
    store = archive.PostgresArchive("postgresql://synthetic:do-not-leak@synthetic.invalid/archive")
    args = {"initialize": (), "save": (archive.prepare_capture(converted),),
            "get": ("a" * 64,), "list_captures": ("synthetic-agent",)}[command]
    with pytest.raises(archive.ArchiveError, match="history extra") as error:
        getattr(store, command)(*args)
    assert "do-not-leak" not in str(error.value)
    assert "ImportError" not in str(error.value)


def test_connection_error_does_not_expose_dsn_or_driver_diagnostics(monkeypatch):
    class DriverError(Exception):
        pass
    def connect(*args, **kwargs):
        assert kwargs["connect_timeout"] == 5
        assert "statement_timeout" in kwargs["options"]
        raise DriverError("synthetic-password SQL internal detail")
    monkeypatch.setattr(archive, "_driver", lambda: SimpleNamespace(Error=DriverError, connect=connect))
    store = archive.PostgresArchive("postgresql://synthetic:synthetic-password@synthetic.invalid/archive")
    with pytest.raises(archive.ArchiveError, match="Cannot connect") as error:
        store._connect()
    assert "synthetic-password" not in str(error.value)
    assert "SQL" not in str(error.value)


def test_cli_reports_missing_configuration_without_attempting_database_access(monkeypatch, capsys):
    monkeypatch.setattr(archive.PostgresArchive, "_connect", lambda _: pytest.fail("must not connect"))
    assert archive.main(["init"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "STUDIO_HISTORY_DATABASE_URL" in output.err
    assert "Traceback" not in output.err


def test_cli_reports_missing_driver_without_traceback_or_dsn(monkeypatch, capsys):
    monkeypatch.setenv("STUDIO_HISTORY_DATABASE_URL", "postgresql://synthetic:do-not-leak@synthetic.invalid/archive")
    original_import = builtins.__import__
    def no_psycopg(name, *args, **kwargs):
        if name == "psycopg" or name.startswith("psycopg."):
            raise ImportError("simulated optional dependency absence")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", no_psycopg)
    assert archive.main(["init"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "history extra" in output.err
    assert "Traceback" not in output.err
    assert "do-not-leak" not in output.err


@pytest.mark.parametrize("contents", ["not json", "null", "[]", "12", "{}", '{"database_url":12}', '{"database_url":""}', '{"database_url":" "}'])
def test_cli_rejects_invalid_local_config_without_exposing_its_content(tmp_path, monkeypatch, capsys, contents):
    config_path = tmp_path / "synthetic-archive.json"
    config_path.write_text(contents)
    monkeypatch.setattr(archive.PostgresArchive, "_connect", lambda _: pytest.fail("must not connect"))
    assert archive.main(["--config", str(config_path), "init"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.strip()
    assert "Traceback" not in output.err
    assert str(config_path) not in output.err


def test_cli_reports_unreadable_config_safely(tmp_path, capsys):
    assert archive.main(["--config", str(tmp_path / "not-present.json"), "init"]) == 1
    assert "Traceback" not in capsys.readouterr().err


def test_environment_config_takes_precedence_without_reading_local_file(tmp_path, monkeypatch):
    dsn = "postgresql://synthetic.invalid/archive"
    monkeypatch.setenv("STUDIO_HISTORY_DATABASE_URL", dsn)
    assert archive._configured_dsn(tmp_path / "not-present.json") == dsn


def test_local_json_config_is_supported_without_environment(tmp_path):
    config_path = tmp_path / "synthetic-archive.json"
    dsn = "postgresql://synthetic.invalid/archive"
    config_path.write_text(json.dumps({"database_url": dsn}))
    assert archive._configured_dsn(config_path) == dsn
