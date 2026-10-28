"""Actual local Agent/Host calls with scripted providers, never paid models."""
from contextvars import copy_context
from copy import copy, deepcopy
import json
import threading

import pytest
from connectonion import Agent
from connectonion.core.llm import LLMResponse, ToolCall
from connectonion.network.host.session import SessionStorage
from connectonion.network.host.session.turn import input_handler

from co_studio.context_eval import context_cases
from co_studio.context_policy import ContextBudgetExceeded, ContextPolicy, ContextShapeError, estimate_tokens
from co_studio.context_report import (COUNTS, FIELDS, SIGNATURE, event_verifier,
                                      history_event_verifier, sign_event)
from co_studio.context_runtime import attach_context_policy
from co_studio.execution_adapter import convert_snapshot, read_execution_history
from co_studio.history_archive import prepare_capture


class ScriptedModel:
    model = "offline-context-report-contract"

    def __init__(self):
        self.inputs = []

    def complete(self, messages, **kwargs):
        self.inputs.append((deepcopy(messages), deepcopy(kwargs)))
        return LLMResponse(content="Offline answer", tool_calls=[], raw_response=None, usage=None)


def make_agent(tmp_path, *, budget=1100, model=None, slug="demo", **kwargs):
    directory = tmp_path / slug
    co_dir = directory / ".co"
    co_dir.mkdir(parents=True, exist_ok=True)
    model = model or ScriptedModel()
    agent = Agent("Context report", llm=model, system_prompt="Local rules", quiet=True, log=False,
                  co_dir=co_dir, state_dir=co_dir, **kwargs)
    attach_context_policy(agent, agent_slug=slug, policy=ContextPolicy(budget), agent_dir=directory)
    return agent, model, directory


def reports(agent):
    return [event for event in agent.current_session["trace"] if event.get("type") == "context_report"]


def snapshot(agent, *, status="done"):
    return {"session_id": agent.current_session["session_id"], "status": status,
            "prompt": agent.current_session.get("user_prompt", "Continue"),
            "result": "Offline answer" if status == "done" else None,
            "session": deepcopy(agent.current_session)}


def legacy_v1_snapshot():
    # Frozen before the v2 policy change; never derive legacy labels/proof from
    # the current runtime constants or re-sign this fixture on read.
    report = {
        "type": "context_report", "id": "legacy-report", "ts": 101.0, "turn": 1,
        "report_version": 1, "status": "prepared", "llm_call_id": "legacy-call",
        "policy_version": "studio-extractive-v1", "estimator": "utf8-json-bytes-div4-v1",
        "input_budget": 1100, "full_estimated_tokens": 42, "estimated_tokens": 42,
        "source_messages": 2, "retained_messages": 2, "input_messages": 2,
        "task_fields": 0, "tool_references": 0, "omitted_tool_references": 0,
        "compacted": False, "summary_kind": "none",
        SIGNATURE: "5fcb95d7aad096d9b4bf1277e36a72f1c6a467c3e4dedab9d4b75ca713856f0a",
    }
    return {
        "session_id": "legacy-session", "status": "done", "prompt": "Continue",
        "result": "Offline answer", "created": 100.0, "updated_at": 103.0,
        "revision": 2, "duration_ms": 10.0,
        "session": {"session_id": "legacy-session", "turn": 1, "trace": [
            {"type": "user_input", "id": "legacy-input", "ts": 100.0, "turn": 1,
             "content": "Continue"},
            {"type": "llm_call", "id": "legacy-call", "ts": 101.0,
             "model": "offline", "status": "running"},
            report,
            {"type": "llm_result", "id": "legacy-call", "ts": 102.0,
             "model": "offline", "status": "success", "duration_ms": 10.0},
            {"type": "turn_result", "id": "legacy-result", "ts": 103.0,
             "turn": 1, "reason": "natural"},
        ]},
    }


def test_frozen_v1_signature_and_capture_fingerprint_survive_v2_reader(tmp_path):
    directory = tmp_path / "demo"
    source = directory / ".co/session_results.jsonl"
    key_path = directory / ".studio-context/reports.key"
    source.parent.mkdir(parents=True)
    key_path.parent.mkdir()
    key = bytes(range(32))  # Public synthetic test key, not a runtime secret.
    key_path.write_text(key.hex())
    key_path.chmod(0o600)
    raw = legacy_v1_snapshot()
    source.write_text(json.dumps(raw) + "\n")
    source_bytes, key_bytes = source.read_bytes(), key_path.read_bytes()
    proof = raw["session"]["trace"][2]
    assert sign_event(key, proof, "demo", "legacy-session") == proof[SIGNATURE]
    assert event_verifier(directory)(proof, "demo", "legacy-session")

    # This literal is the capture produced by the v1 reader, not a fingerprint
    # computed from the new result. Reads must preserve every normalized field.
    expected_capture = "0a9979ae46c6b49f09a20a6a77ab2a627e908bf400438ccda92a5ebc6a90d279"
    for _ in range(2):
        history = read_execution_history(source, agent_slug="demo")
        assert not history.issues and not history.sessions[0].issues
        capture = prepare_capture(history.sessions[0])
        assert capture.capture_id == expected_capture
        report, = (event for event in capture.events if event.kind == "context_report")
        assert report.payload == {name: proof[name] for name in FIELDS}
        assert report.payload["policy_version"] == "studio-extractive-v1"
        assert report.call_id == "legacy-call"
    assert source.read_bytes() == source_bytes and key_path.read_bytes() == key_bytes


@pytest.mark.parametrize("version,verified", [
    ("studio-extractive-v1", True), ("studio-extractive-v2", True),
    ("studio-extractive-v3", True), ("studio-extractive-v4", False), ("studio-extractive-v2-untrusted", False),
])
def test_real_host_signed_policy_version_allowlist(tmp_path, monkeypatch, version, verified):
    # Exercise actual Agent trace signing and Host JSONL, not a verifier stub.
    monkeypatch.setattr("co_studio.context_runtime.POLICY_VERSION", version)
    agent, model, directory = make_agent(tmp_path)
    storage = SessionStorage(directory / ".co/session_results.jsonl")
    input_handler(lambda: agent, storage, "Continue", result_ttl=3600,
                  session={"session_id": "version-session"})
    proof, = reports(agent)
    assert len(model.inputs) == 1 and proof["policy_version"] == version
    assert event_verifier(directory)(proof, "demo", "version-session") is verified
    history = read_execution_history(storage.path, agent_slug="demo")
    report, = (event for event in history.sessions[0].events if event.kind == "context_report")
    if verified:
        assert report.payload == {name: proof[name] for name in FIELDS}
        assert report.call_id == proof["llm_call_id"]
    else:
        assert report.payload["status"] == "unknown" and report.call_id is None
        assert report.payload["policy_version"] is None
        assert all(report.payload[name] is None for name in COUNTS)


def test_supported_policy_version_cannot_be_changed_without_resigning(tmp_path, monkeypatch):
    monkeypatch.setattr("co_studio.context_runtime.POLICY_VERSION", "studio-extractive-v1")
    agent, _, directory = make_agent(tmp_path)
    agent.input("Continue", session={"session_id": "version-session"})
    raw = snapshot(agent)
    proof = next(event for event in raw["session"]["trace"] if event["type"] == "context_report")
    proof["policy_version"] = "studio-extractive-v2"
    assert not event_verifier(directory)(proof, "demo", "version-session")
    converted = convert_snapshot(raw, agent_slug="demo", provenance_verifier=history_event_verifier(directory))
    report, = (event for event in converted.events if event.kind == "context_report")
    assert report.payload["status"] == "unknown"
    assert all(report.payload[name] is None for name in COUNTS)


@pytest.mark.parametrize("compact", [False, True])
def test_real_host_signed_report_persists_before_provider_and_keeps_history(tmp_path, compact):
    agent, model, directory = make_agent(tmp_path)
    original = {"session_id": "report-session"}
    if compact:
        original["messages"] = deepcopy(context_cases()[0].messages[:-1])
    untouched = deepcopy(original)
    storage = SessionStorage(directory / ".co/session_results.jsonl")
    input_handler(lambda: agent, storage, "Continue", result_ttl=3600, session=original)
    assert original == untouched
    observation, = reports(agent)
    assert observation["status"] == "prepared" and observation["compacted"] is compact
    assert observation["turn"] == 1
    assert observation["input_messages"] == len(model.inputs[0][0])
    assert observation["estimated_tokens"] == estimate_tokens(model.inputs[0][0], model.inputs[0][1]["tools"])
    assert observation["input_messages"] == observation["retained_messages"] + int(compact)
    assert event_verifier(directory)(observation, "demo", "report-session")
    assert (directory / ".studio-context/reports.key").stat().st_mode & 0o777 == 0o600
    for omitted in ("messages", "retained_message_indices", "provider_input_tokens", "provider_output_tokens"):
        assert omitted not in observation
    assert "no-payment-write" not in json.dumps(observation)
    history = read_execution_history(storage.path, agent_slug="demo")
    saved, = (event for event in history.sessions[0].events if event.kind == "context_report")
    assert saved.payload == {name: observation[name] for name in FIELDS}
    assert saved.call_id == observation["llm_call_id"]
    assert SIGNATURE not in repr(prepare_capture(history.sessions[0]))
    trace = agent.current_session["trace"]
    call = next(event for event in trace if event["type"] == "llm_call")
    result = next(event for event in trace if event["type"] == "llm_result")
    assert trace.index(call) < trace.index(observation) < trace.index(result)
    assert history.sessions[0].turns[0].result == "Offline answer"


@pytest.mark.parametrize("invalid", [False, True])
def test_rejection_reports_known_counts_without_calling_model(tmp_path, invalid):
    agent, model, directory = make_agent(tmp_path, budget=256)
    supplied = {"session_id": "rejected"}
    if invalid:
        supplied["messages"] = [{"role": "tool", "tool_call_id": "orphan", "content": "Private result"}]
    with pytest.raises(ContextShapeError if invalid else ContextBudgetExceeded):
        agent.input("Continue" if invalid else "private oversized input " * 1000, session=supplied)
    assert not model.inputs
    observation, = reports(agent)
    assert observation["status"] == ("invalid_context" if invalid else "budget_exceeded")
    assert observation["estimated_tokens"] is observation["retained_messages"] is observation["compacted"] is None
    assert observation["source_messages"] >= 2
    assert (observation["full_estimated_tokens"] is None) is invalid
    converted = convert_snapshot(snapshot(agent, status="failed"), agent_slug="demo",
                                 provenance_verifier=history_event_verifier(directory))
    normalized, = (event for event in converted.events if event.kind == "context_report")
    assert normalized.payload["status"] == observation["status"]
    assert converted.turns[0].finish_reason == "error"
    assert agent.current_session["trace"][-1]["type"] == "turn_result"


def test_no_connection_host_error_retains_existing_persistence_limitation(tmp_path):
    agent, model, directory = make_agent(tmp_path, budget=256)
    storage = SessionStorage(directory / ".co/session_results.jsonl")
    with pytest.raises(ContextBudgetExceeded):
        input_handler(lambda: agent, storage, "oversized " * 1000, result_ttl=3600,
                      session={"session_id": "rejected"})
    assert reports(agent)[0]["status"] == "budget_exceeded"
    assert storage.get("rejected").status == "failed"
    # CO 1.9.2 saves the old claimed session on its disconnected error path.
    # Do not synthesize a durable report or rewrite the caller's source history.
    assert not any(event.kind == "context_report" for event in read_execution_history(
        storage.path, agent_slug="demo").sessions[0].events)


@pytest.mark.parametrize("mutation", ["unsigned", "metric", "boolean_count", "unknown_label", "session", "agent", "turn", "call", "before_call", "after_terminal", "duplicate", "duplicate_call", "missing_call"])
def test_forged_moved_or_replayed_reports_are_unknown_without_metrics(tmp_path, mutation):
    agent, _, directory = make_agent(tmp_path)
    agent.input("Continue", session={"session_id": "report-session"})
    raw = snapshot(agent)
    trace = raw["session"]["trace"]
    report = next(event for event in trace if event["type"] == "context_report")
    call = next(event for event in trace if event["type"] == "llm_call")
    slug = "demo"
    if mutation == "unsigned":
        report.pop(SIGNATURE)
    elif mutation == "metric":
        report["estimated_tokens"] = 1
    elif mutation == "boolean_count":
        report["source_messages"] = True
    elif mutation == "unknown_label":
        report["summary_kind"] = "Private source text"
    elif mutation == "session":
        raw["session_id"] = raw["session"]["session_id"] = "foreign-session"
    elif mutation == "agent":
        slug = "foreign"
    elif mutation == "turn":
        report["turn"] = 2
    elif mutation == "call":
        report["llm_call_id"] = "foreign-call"
    elif mutation == "before_call":
        trace.remove(report)
        trace.insert(trace.index(call), report)
    elif mutation == "after_terminal":
        trace.remove(report)
        trace.append(report)
    elif mutation == "duplicate":
        trace.insert(trace.index(report), deepcopy(report))
    elif mutation == "duplicate_call":
        trace.insert(trace.index(call), deepcopy(call))
    elif mutation == "missing_call":
        trace.remove(call)
    converted = convert_snapshot(raw, agent_slug=slug, provenance_verifier=history_event_verifier(directory))
    observations = [event for event in converted.events if event.kind == "context_report"]
    assert observations
    for event in observations:
        assert event.payload["status"] == "unknown" and event.call_id is None
        assert all(event.payload[name] is None for name in COUNTS)
        assert "Private source text" not in json.dumps(event.payload)


def test_reader_never_creates_missing_key_and_default_conversion_does_not_trust_proof(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    verifier = event_verifier(empty)
    assert not verifier({"type": "context_report"}, "empty", "session")
    assert list(empty.iterdir()) == []
    agent, _, _ = make_agent(tmp_path)
    agent.input("Continue", session={"session_id": "report-session"})
    result = convert_snapshot(snapshot(agent), agent_slug="demo")
    assert next(event for event in result.events if event.kind == "context_report").payload["status"] == "unknown"


def test_shared_provider_and_shallow_copy_report_only_the_actual_owner(tmp_path):
    shared = ScriptedModel()
    one, _, first_dir = make_agent(tmp_path, model=shared, slug="first")
    two, _, second_dir = make_agent(tmp_path, model=shared, slug="second")
    one.input("One", session={"session_id": "one"})
    frozen = deepcopy(one.current_session)
    copied = copy(one)
    copied.input("Copy", session={"session_id": "copy"})
    two.input("Two", session={"session_id": "two"})
    assert one.current_session == frozen
    assert event_verifier(first_dir)(reports(copied)[0], "first", "copy")
    assert event_verifier(second_dir)(reports(two)[0], "second", "two")
    assert not event_verifier(second_dir)(reports(copied)[0], "first", "copy")
    assert shared.complete.__func__ is ScriptedModel.complete


def test_abandoned_worker_cannot_report_or_invoke_model_in_the_next_turn(tmp_path, monkeypatch):
    agent, model, _ = make_agent(tmp_path)
    abandoned = []

    def interruptible(fn, io, **kwargs):
        if not abandoned:
            abandoned.append((copy_context(), fn))
            return None, True
        old_context, old_fn = abandoned[0]
        with pytest.raises(ContextShapeError, match="inactive model call"):
            old_context.run(old_fn)
        return fn(), False

    monkeypatch.setattr("connectonion.core.agent.run_interruptible", interruptible)
    agent.input("Cancelled", session={"session_id": "report-session"})
    assert not reports(agent) and not model.inputs
    first_trace = deepcopy(agent.current_session["trace"])
    agent.input("Next", session=deepcopy(agent.current_session))
    assert len(reports(agent)) == len(model.inputs) == 1
    assert reports(agent)[0]["turn"] == 2
    assert agent.current_session["trace"][:len(first_trace)] == first_trace
    assert agent.current_session["trace"][-1]["type"] == "turn_result"


def test_real_worker_timeout_during_preparation_cannot_report_late_or_call_provider(tmp_path, monkeypatch):
    import importlib
    from co_studio import context_runtime
    from connectonion.core.interrupt import InterruptibleStepTimeout

    framework = importlib.import_module("connectonion.core.agent")
    real_interruptible = framework.run_interruptible
    real_build = context_runtime.build_context
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    preparation_threads = []

    def delayed_build(*args, **kwargs):
        preparation_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(5)
        return real_build(*args, **kwargs)

    def observe_real_worker(fn, io, **kwargs):
        def tracked():
            try:
                return fn()
            finally:
                finished.set()
        return real_interruptible(tracked, io, **kwargs)

    monkeypatch.setattr(context_runtime, "build_context", delayed_build)
    monkeypatch.setattr(framework, "run_interruptible", observe_real_worker)
    agent, model, _ = make_agent(tmp_path)
    agent.current_session = {"session_id": "timed-out", "turn": 1, "iteration": 1,
                             "user_prompt": "Continue", "trace": [],
                             "messages": [{"role": "system", "content": "Rules"},
                                          {"role": "user", "content": "Continue"}]}
    old_session = agent.current_session
    try:
        with pytest.raises(InterruptibleStepTimeout):
            agent._get_llm_decision(timeout_seconds=.1)
        assert entered.is_set() and not finished.is_set()
        assert len(preparation_threads) == 1
        assert preparation_threads[0] != threading.get_ident()
        assert not reports(agent) and not model.inputs
        assert old_session["trace"][-1]["type"] == "llm_result"
        assert old_session["trace"][-1]["status"] == "error"
        frozen_old = deepcopy(old_session)
        agent.current_session = {"session_id": "next-session", "turn": 1, "trace": [],
                                 "messages": [{"role": "user", "content": "Next"}]}
        frozen_next = deepcopy(agent.current_session)
        release.set()
        assert finished.wait(2)
        assert old_session == frozen_old
        assert agent.current_session == frozen_next
        assert not model.inputs
    finally:
        release.set()
        assert finished.wait(2)


def test_each_iteration_has_a_distinct_report_before_its_model_call(tmp_path):
    def add(a: int, b: int) -> int:
        """Add small integers."""
        return a + b

    class ToolModel(ScriptedModel):
        def complete(self, messages, **kwargs):
            result = super().complete(messages, **kwargs)
            assert len(reports(agent)) == len(self.inputs)
            if len(self.inputs) == 1:
                return LLMResponse(content="", tool_calls=[ToolCall(id="tool-1", name="add", arguments={"a": 2, "b": 3})],
                                   raw_response=None, usage=None)
            return result

    agent, model, _ = make_agent(tmp_path, model=ToolModel(), tools=[add])
    agent.input("Use add", session={"session_id": "two-calls"})
    assert len(reports(agent)) == 2
    assert len({event["llm_call_id"] for event in reports(agent)}) == 2
    assert all(event["turn"] == 1 for event in reports(agent))
