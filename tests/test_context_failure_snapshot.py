"""Actual pinned Host failures, detached caller data, and narrow recovery guards."""
from copy import deepcopy
import json

import pytest
from connectonion import Agent
from connectonion.core.llm import LLMResponse
from connectonion.network.host.session import SessionStorage
from connectonion.network.host.session.mode import HostPermissionPolicy, claim_host_prompt
from connectonion.network.host.session.turn import input_handler

from co_studio.context_failure import capture_host_claim
from co_studio.context_policy import ContextBudgetExceeded, ContextPolicy, ContextShapeError
from co_studio.context_report import event_verifier
from co_studio.context_runtime import attach_context_policy, wrap_host_for_context
from co_studio.execution_adapter import read_execution_history


class Model:
    model = "offline-context-failure-contract"

    def __init__(self, error=None):
        self.error = error
        self.inputs = []

    def complete(self, messages, **kwargs):
        self.inputs.append(deepcopy(messages))
        if self.error:
            raise self.error
        return LLMResponse(content="Offline answer", tool_calls=[], raw_response=None, usage=None)


def make_agent(tmp_path, *, budget=1100, model=None, enabled=True):
    directory = tmp_path / "demo"
    co_dir = directory / ".co"
    co_dir.mkdir(parents=True, exist_ok=True)
    model = model or Model()
    agent = Agent("Failure snapshot", llm=model, system_prompt="Local rules", quiet=True,
                  log=False, co_dir=co_dir, state_dir=co_dir)
    attach_context_policy(agent, agent_slug="demo", policy=ContextPolicy(budget),
                          agent_dir=directory, host_failure_snapshot=enabled)
    return agent, model, directory, SessionStorage(co_dir / "session_results.jsonl")


@pytest.mark.parametrize("failure", ["budget", "shape", "provider", "connection"])
def test_actual_host_failure_keeps_signed_report_and_original_client_input(tmp_path, failure):
    model = Model(RuntimeError("offline provider failure") if failure == "provider" else None)
    agent, model, directory, storage = make_agent(tmp_path, budget=256 if failure == "budget" else 1100, model=model)
    original = {"session_id": "failed-turn"}
    if failure == "shape":
        original["messages"] = [{"role": "tool", "tool_call_id": "orphan", "content": "private raw result"}]
    untouched = deepcopy(original)
    prompt = "oversized input " * 1000 if failure == "budget" else "Continue"
    connection = None
    if failure == "connection":
        class BrokenConnection:
            def send(self, event):
                if event.get("type") == "context_report":
                    raise ConnectionError("offline disconnected")

            def receive_all(self, kind):
                return []

        connection = BrokenConnection()
    error = {"budget": ContextBudgetExceeded, "shape": ContextShapeError,
             "provider": RuntimeError, "connection": ConnectionError}[failure]
    with pytest.raises(error):
        input_handler(lambda: agent, storage, prompt, result_ttl=3600, session=original, connection=connection)
    assert original == untouched
    saved = storage.get("failed-turn")
    assert saved.status == "failed" and saved.result is None
    assert saved.session["messages"] == agent.current_session["messages"]
    assert saved.session["trace"] == agent.current_session["trace"]
    assert saved.session["trace"][-1]["reason"] == "error"
    report, = (event for event in saved.session["trace"] if event["type"] == "context_report")
    assert event_verifier(directory)(report, "demo", "failed-turn")
    assert report["status"] == {"budget": "budget_exceeded", "shape": "invalid_context"}.get(failure, "prepared")
    assert len(model.inputs) == int(failure == "provider")
    history = read_execution_history(storage.path, agent_slug="demo")
    observation, = (event for event in history.sessions[0].events if event.kind == "context_report")
    assert observation.payload["status"] == report["status"]
    assert history.sessions[0].turns[0].finish_reason == "error"
    assert history.sessions[0].turns[0].result is None


def test_failure_then_new_factory_preserves_failed_turn_and_original_jsonl_rows(tmp_path):
    agent, _, directory, storage = make_agent(tmp_path, model=Model(RuntimeError("offline")))
    with pytest.raises(RuntimeError):
        input_handler(lambda: agent, storage, "First attempt", result_ttl=3600,
                      session={"session_id": "resume"})
    previous_bytes = storage.path.read_bytes()
    previous_trace = deepcopy(storage.get("resume").session["trace"])
    replacement, _, _, _ = make_agent(tmp_path)
    input_handler(lambda: replacement, storage, "Try again", result_ttl=3600, session={"session_id": "resume"})
    saved = storage.get("resume")
    assert saved.status == "done" and saved.session["turn"] == 2
    assert saved.session["trace"][:len(previous_trace)] == previous_trace
    assert storage.path.read_bytes().startswith(previous_bytes)
    history = read_execution_history(storage.path, agent_slug="demo")
    assert [turn.finish_reason for turn in history.sessions[0].turns] == ["error", "natural"]
    assert [turn.result for turn in history.sessions[0].turns] == [None, "Offline answer"]
    observations = [event for event in history.sessions[0].events if event.kind == "context_report"]
    assert len(observations) == 2
    assert all(event.payload["status"] == "prepared" for event in observations)


def test_host_keeps_verified_permissions_when_failed_agent_attempts_to_change_them(tmp_path):
    model = Model()
    agent, _, _, storage = make_agent(tmp_path, model=model)
    def fail(messages, **kwargs):
        agent.current_session.update(requester={"address": "attacker"}, mode="full_access",
                                     permissions={"all": True}, approval={"approved": True})
        raise RuntimeError("offline provider error")
    model.complete = fail
    original = {"session_id": "authority", "requester": {"address": "client-forged"}, "mode": "full_access"}
    untouched = deepcopy(original)
    with pytest.raises(RuntimeError):
        input_handler(lambda: agent, storage, "Read only", result_ttl=3600, session=original,
                      requester={"address": "verified-owner"}, mode_policy=HostPermissionPolicy())
    saved = storage.get("authority")
    assert saved.status == "failed"
    assert saved.session["requester"] == {"address": "verified-owner"}
    assert saved.session["mode"] == "auto"
    assert saved.session.get("permissions") != {"all": True}
    assert saved.session.get("approval") != {"approved": True}
    assert any(event["type"] == "context_report" for event in saved.session["trace"])
    assert original == untouched


def test_generated_host_wrapper_explicitly_enables_failure_recovery(tmp_path):
    directory = tmp_path / "demo"
    directory.mkdir()
    script = directory / "agent.py"
    script.write_text('''"""Demo — generated by ConnectOnion Studio."""
from connectonion import Agent
SYSTEM_PROMPT = "Local rules"
def create_agent():
    return Agent("Generated", system_prompt=SYSTEM_PROMPT, llm=OFFLINE_MODEL,
                 quiet=True, log=False, co_dir=CO_DIR, state_dir=CO_DIR)
''')
    (directory / "meta.json").write_text(json.dumps({"slug": "demo", "preset": "custom"}))
    model = Model()
    namespace = {"CO_DIR": directory / ".co", "OFFLINE_MODEL": model}
    exec(compile(script.read_text(), str(script), "exec"), namespace)
    captured = []
    def host(factory, **kwargs):
        captured.append(factory)
    wrap_host_for_context(host, script, policy=ContextPolicy(256))(
        namespace["create_agent"], co_dir=directory / ".co")
    storage = SessionStorage(directory / ".co/session_results.jsonl")
    with pytest.raises(ContextBudgetExceeded):
        input_handler(captured[0], storage, "long " * 1000, result_ttl=3600,
                      session={"session_id": "generated"})
    saved = storage.get("generated")
    assert saved.status == "failed"
    assert any(event["type"] == "context_report" for event in saved.session["trace"])
    assert model.inputs == []


def test_direct_input_without_active_host_claim_never_changes_source(tmp_path):
    agent, _, _, _ = make_agent(tmp_path, budget=256)
    supplied = {"session_id": "direct"}
    untouched = deepcopy(supplied)
    with pytest.raises(ContextBudgetExceeded):
        agent.input("long " * 1000, session=supplied)
    assert supplied == untouched


def claimed_fixture(tmp_path):
    agent, _, directory, storage = make_agent(tmp_path)
    record, _ = claim_host_prompt(storage, "guarded", "Continue", 3600, {"session_id": "guarded"},
                                 requester=None, policy=None, is_admin=False)
    agent.storage = storage
    guard = capture_host_claim(agent, record.session, "Continue", agent_dir=directory)
    assert guard is not None
    current = deepcopy(record.session)
    current.update(turn=1, user_prompt="Continue", messages=[{"role": "user", "content": "Continue"}],
                   trace=[{"type": "user_input", "turn": 1, "content": "Continue"},
                          {"type": "turn_result", "turn": 1, "reason": "error"}])
    return agent, directory, storage, record, guard, current


@pytest.mark.parametrize("mutation", ["session", "turn", "prompt", "terminal", "claimed",
                                      "trace_prefix", "new_claim", "finished", "owner"])
def test_changed_scope_or_durable_claim_cannot_receive_late_failure_snapshot(tmp_path, mutation):
    _, _, storage, record, guard, current = claimed_fixture(tmp_path)
    if mutation == "session":
        current["session_id"] = "other-session"
    elif mutation == "turn":
        current["turn"] = 2
    elif mutation == "prompt":
        current["user_prompt"] = "Other input"
    elif mutation == "terminal":
        current["trace"][-1]["reason"] = "natural"
    elif mutation == "claimed":
        record.session["messages"].append({"role": "user", "content": "changed"})
    else:
        updated = storage.get("guarded")
        if mutation == "trace_prefix":
            updated.session["trace"] = [{"type": "user_input", "content": "Other execution"}]
        elif mutation == "new_claim":
            updated.expires += 1
        elif mutation == "finished":
            updated.status = "failed"
        elif mutation == "owner":
            updated.session["requester"] = {"address": "other"}
        storage.save(updated)
    untouched = deepcopy(record.session)
    before_bytes = storage.path.read_bytes()
    assert guard.recover(current) is False
    assert record.session == untouched
    assert storage.path.read_bytes() == before_bytes


def test_recovery_failure_never_masks_original_exception_or_writes_storage(tmp_path, monkeypatch):
    _, _, storage, record, guard, current = claimed_fixture(tmp_path)
    untouched, before = deepcopy(record.session), storage.path.read_bytes()
    def broken_get(_):
        raise OSError("private disk details")
    monkeypatch.setattr(storage, "get", broken_get)
    assert guard.recover(current) is False
    assert record.session == untouched and storage.path.read_bytes() == before


@pytest.mark.parametrize("mutation", ["path", "missing_storage", "prompt", "session_id", "different_source",
                                      "invalid_turn", "boolean_turn", "finished"])
def test_non_host_or_mismatched_capture_never_enables_recovery(tmp_path, mutation):
    agent, directory, storage, record, _, _ = claimed_fixture(tmp_path)
    supplied, prompt = deepcopy(record.session), "Continue"
    if mutation == "path":
        storage.path = tmp_path / "unrelated-session-results.jsonl"
    elif mutation == "missing_storage":
        del agent.storage
    elif mutation == "prompt":
        prompt = "Other request"
    elif mutation == "session_id":
        supplied["session_id"] = "other"
    elif mutation == "different_source":
        supplied["messages"].append({"role": "user", "content": "extra"})
    elif mutation in {"invalid_turn", "boolean_turn"}:
        supplied["turn"] = -1 if mutation == "invalid_turn" else True
        record.session = deepcopy(supplied)
        storage.save(record)
    else:
        record.status = "failed"
        storage.save(record)
    assert capture_host_claim(agent, supplied, prompt, agent_dir=directory) is None
