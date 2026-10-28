"""Real CO Agent + scripted providers; all data is temporary and offline."""
from copy import copy, deepcopy
import json
from pathlib import Path
import threading

import pytest

from connectonion import Agent
from connectonion.core.events import before_llm
from connectonion.core.llm import LLMResponse
from connectonion.useful_plugins import auto_compact
from connectonion.useful_plugins.auto_compact import check_and_compact

from co_studio.context_eval import context_cases
from co_studio.context_policy import ContextBudgetExceeded, ContextError, ContextPolicy, ContextShapeError
from co_studio.context_runtime import attach_context_policy, policy_from_env, wrap_host_for_context


class ScriptedModel:
    model = "offline-context-contract"

    def __init__(self):
        self.inputs = []

    def complete(self, messages, **kwargs):
        self.inputs.append(deepcopy(messages))
        return LLMResponse(content="Offline answer", tool_calls=[], raw_response=None, usage=None)


def agent_for(tmp_path, model=None, **kwargs):
    return Agent("Context contract", llm=model or ScriptedModel(), quiet=True, log=False,
                 co_dir=tmp_path, state_dir=tmp_path, **kwargs)


def session(session_id="session-a", messages=None):
    return {"session_id": session_id, "messages": deepcopy(messages if messages is not None else context_cases()[0].messages[:-1]),
            "trace": [], "turn": 0, "iteration": 1}


def test_disabled_policy_is_identical_and_keeps_original_plugin(tmp_path):
    agent = agent_for(tmp_path, plugins=[auto_compact])
    original_class, provider, events = type(agent), agent.llm, agent.events
    assert attach_context_policy(agent, agent_slug="demo", policy=None) is agent
    assert type(agent) is original_class and agent.llm is provider and agent.events is events
    assert check_and_compact in agent.events["after_llm"]


def test_actual_input_compacts_only_provider_view_and_keeps_raw_history(tmp_path):
    model = ScriptedModel()
    agent = agent_for(tmp_path, model, plugins=[auto_compact])
    registry = agent.events
    attach_context_policy(agent, agent_slug="demo", policy=ContextPolicy(1100))
    original = session()
    untouched = deepcopy(original)
    answer = agent.input("Continue", session=original)
    assert answer == "Offline answer"
    assert len(model.inputs[0]) < len(untouched["messages"])
    assert any("studio_deterministic_extract" in item.get("content", "") for item in model.inputs[0])
    assert agent.current_session["messages"][:len(untouched["messages"])] == untouched["messages"]
    assert original == untouched
    assert agent.current_session["messages"][-2]["content"] == "Continue"
    assert agent.current_session["messages"][-1]["content"] == "Offline answer"
    assert check_and_compact not in agent.events["after_llm"]
    assert check_and_compact in registry["after_llm"]
    assert all("studio_deterministic_extract" not in str(item) for item in agent.current_session["trace"])


def test_actual_host_persists_and_restores_full_history_across_fresh_factories(tmp_path):
    from connectonion.network.host.session import SessionStorage
    from connectonion.network.host.session.turn import input_handler

    storage = SessionStorage(tmp_path / "session_results.jsonl")
    model = ScriptedModel()
    created = []

    def factory():
        agent = attach_context_policy(agent_for(tmp_path, model, plugins=[auto_compact]),
                                      agent_slug="demo", policy=ContextPolicy(1100))
        created.append(agent)
        return agent

    original = session()
    input_handler(factory, storage, "Continue once", result_ttl=3600, session=original)
    input_handler(factory, storage, "Continue again", result_ttl=3600, session={"session_id": "session-a"})
    saved = storage.get("session-a").session
    assert len(created) == 2 and created[0] is not created[1]
    assert len(model.inputs[0]) < len(original["messages"])
    assert len(model.inputs[1]) < len(original["messages"])
    assert saved["messages"][:len(original["messages"])] == original["messages"]
    assert saved["messages"][-2]["content"] == "Continue again"
    assert "studio_deterministic_extract" not in storage.path.read_text()


def test_actual_host_budget_refusal_is_failed_not_a_successful_answer(tmp_path):
    from connectonion.network.host.session import SessionStorage
    from connectonion.network.host.session.turn import input_handler

    storage = SessionStorage(tmp_path / "session_results.jsonl")
    model = ScriptedModel()
    def factory():
        return attach_context_policy(agent_for(tmp_path, model), agent_slug="demo", policy=ContextPolicy(256))

    with pytest.raises(ContextBudgetExceeded):
        input_handler(factory, storage, "too long " * 1000, result_ttl=3600, session={"session_id": "refused"})
    assert storage.get("refused").status == "failed"
    assert storage.get("refused").result is None
    assert not model.inputs


def test_multiple_sessions_never_reuse_memory(tmp_path):
    model = ScriptedModel()
    agent = attach_context_policy(agent_for(tmp_path, model), agent_slug="demo", policy=ContextPolicy(1100))
    agent.input("Continue A", session=session("session-a"))
    agent.input("Continue B", session=session("session-b", [{"role": "system", "content": "Other rules"}]))
    assert "no-payment-write" in json.dumps(model.inputs[0])
    assert "no-payment-write" not in json.dumps(model.inputs[1])
    assert "session-a" not in json.dumps(model.inputs[1])


def test_actual_tool_loop_keeps_result_pairs_and_raw_large_results(tmp_path):
    from connectonion.core.llm import ToolCall

    class ToolModel(ScriptedModel):
        def complete(self, messages, **kwargs):
            self.inputs.append(deepcopy(messages))
            if len(self.inputs) == 1:
                return LLMResponse(content="", tool_calls=[ToolCall(id="call-1", name="inspect", arguments={})], raw_response=None, usage=None)
            return LLMResponse(content="Offline answer", tool_calls=[], raw_response=None, usage=None)

    def inspect():
        """Return inspection evidence."""
        return "tool evidence"

    model = ToolModel()
    agent = attach_context_policy(agent_for(tmp_path, model, tools=[inspect]), agent_slug="demo", policy=ContextPolicy(1500))
    assert agent.input("Inspect", session=session()) == "Offline answer"
    assert [item["role"] for item in model.inputs[1][-2:]] == ["assistant", "tool"]
    assert model.inputs[1][-1]["tool_call_id"] == "call-1"
    assert "tool evidence" in model.inputs[1][-1]["content"]


def test_later_user_role_reminder_cannot_discard_actual_current_task(tmp_path):
    @before_llm
    def add_reminder(agent):
        agent.current_session["messages"].append({"role": "user", "content": "A short plugin reminder"})

    model = ScriptedModel()
    agent = attach_context_policy(agent_for(tmp_path, model, on_events=[add_reminder]),
                                  agent_slug="demo", policy=ContextPolicy(1100))
    agent.input("CURRENT_TASK_SENTINEL " + "important current detail " * 50, session=session())
    assert "CURRENT_TASK_SENTINEL" in json.dumps(model.inputs[0])
    assert model.inputs[0][-1]["content"] == "A short plugin reminder"


def test_over_budget_never_calls_provider_and_preserves_source(tmp_path):
    model = ScriptedModel()
    agent = attach_context_policy(agent_for(tmp_path, model), agent_slug="demo", policy=ContextPolicy(256))
    agent.current_session = session(messages=[{"role": "system", "content": "Rules"}, {"role": "user", "content": "x" * 5000}])
    source = deepcopy(agent.current_session["messages"])
    with pytest.raises(ContextBudgetExceeded):
        agent._get_llm_decision()
    assert not model.inputs
    assert agent.current_session["messages"] == source


def test_shared_provider_is_not_globally_wrapped_and_configuration_forwards(tmp_path):
    model = ScriptedModel()

    @before_llm
    def change_model(agent):
        agent.llm.model = "changed-offline-model"
        agent.llm.temperature = .1

    one = agent_for(tmp_path, model, on_events=[change_model])
    other = agent_for(tmp_path, model)
    attach_context_policy(one, agent_slug="demo", policy=ContextPolicy(1100))
    one.input("Continue", session=session())
    assert other.llm is model
    assert model.model == "changed-offline-model" and model.temperature == .1
    del one.llm.temperature
    assert not hasattr(model, "temperature")


def test_copied_agent_uses_its_own_session_scope(tmp_path):
    model = ScriptedModel()
    original = attach_context_policy(agent_for(tmp_path, model), agent_slug="demo", policy=ContextPolicy(1100))
    original.current_session = session("original")
    copied = copy(original)
    copied.current_session = session("copied")
    copied._get_llm_decision()
    assert '"session":"copied"' in json.dumps(model.inputs[0]).replace("\\\"", '"')
    assert original.current_session["trace"] == []


def test_worker_scope_survives_real_interruptible_worker(tmp_path):
    model = ScriptedModel()
    agent = attach_context_policy(agent_for(tmp_path, model), agent_slug="demo", policy=ContextPolicy(1100))
    agent.current_session = session("worker-session")
    agent._get_llm_decision(timeout_seconds=2)
    assert "worker-session" in json.dumps(model.inputs[0])


def test_late_cancelled_worker_cannot_read_next_session_or_write_a_report(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class BlockingModel(ScriptedModel):
        def complete(self, messages, **kwargs):
            self.inputs.append(deepcopy(messages))
            entered.set()
            assert release.wait(2)
            return LLMResponse(content="Late answer", tool_calls=[], raw_response=None, usage=None)

    from connectonion.core.interrupt import InterruptibleStepTimeout
    model = BlockingModel()
    agent = attach_context_policy(agent_for(tmp_path, model), agent_slug="demo", policy=ContextPolicy(1100))
    agent.current_session = session("old-session")
    try:
        with pytest.raises(InterruptibleStepTimeout):
            agent._get_llm_decision(timeout_seconds=.02)
        assert entered.is_set()
        agent.current_session = session("next-session", [{"role": "system", "content": "Next rules"}])
        frozen = deepcopy(agent.current_session)
    finally:
        release.set()
    assert "old-session" in json.dumps(model.inputs[0])
    assert "next-session" not in json.dumps(model.inputs[0])
    assert agent.current_session == frozen
    assert not hasattr(agent, "last_context_report")


def test_direct_provider_call_without_scope_fails_closed(tmp_path):
    agent = attach_context_policy(agent_for(tmp_path), agent_slug="demo", policy=ContextPolicy())
    with pytest.raises(ContextShapeError, match="active scoped"):
        agent.llm.complete([])
    with pytest.raises(ContextError, match="already attached"):
        attach_context_policy(agent, agent_slug="demo", policy=ContextPolicy())


@pytest.mark.parametrize("value", ["-2", "0", "127", "2000001", "2.4", "١٢٨", "hello", " 1000 "])
def test_invalid_opt_in_never_silently_disables(value):
    with pytest.raises(ContextError, match="CO_STUDIO_CONTEXT_BUDGET"):
        policy_from_env({"CO_STUDIO_CONTEXT_BUDGET": value})


def test_environment_and_disabled_host_contract():
    assert policy_from_env({}) is None
    assert policy_from_env({"CO_STUDIO_CONTEXT_BUDGET": ""}) is None
    assert policy_from_env({"CO_STUDIO_CONTEXT_BUDGET": "2048"}) == ContextPolicy(2048)
    sentinel = object()
    assert wrap_host_for_context(sentinel, Path("not-read.py"), policy=None) is sentinel


@pytest.mark.parametrize("compact", [False, True])
def test_generated_host_factory_and_prompt_wrapping_composition(tmp_path, compact):
    from co_studio.prompt_versions import PromptVersions, wrap_host_for_prompt
    from co_studio.prompt_provenance import event_verifier, system_sha256
    from co_studio.context_report import event_verifier as context_verifier

    directory = tmp_path / "demo"
    directory.mkdir()
    script = directory / "agent.py"
    script.write_text('''"""Demo — generated by ConnectOnion Studio."""
from connectonion import Agent
SYSTEM_PROMPT = "Original instructions"
def create_agent():
    return Agent("Context contract", system_prompt=SYSTEM_PROMPT, llm=OFFLINE_MODEL,
                 quiet=True, log=False, co_dir=CO_DIR, state_dir=CO_DIR)
''')
    (directory / "meta.json").write_text(json.dumps({"slug": "demo", "preset": "custom"}))
    store = PromptVersions(directory)
    revision = store.save("baseline", "Selected instructions")["current"]
    model = ScriptedModel()
    namespace = {"__file__": str(script), "CO_DIR": directory / ".co", "OFFLINE_MODEL": model}
    exec(compile(script.read_text(), str(script), "exec"), namespace)
    selected = []
    def host(factory, **kwargs):
        selected.append(factory())
    wrapped = wrap_host_for_context(host, script, policy=ContextPolicy(1100))
    wrapped = wrap_host_for_prompt(wrapped, script)
    wrapped(namespace["create_agent"], co_dir=directory / ".co")
    agent = selected[0]
    source = {"session_id": "composed"}
    if compact:
        source["messages"] = deepcopy(context_cases()[0].messages[1:-1])
    agent.input("Continue", session=source)
    observations = [event for event in agent.current_session["trace"] if event.get("type") == "prompt_provenance"]
    assert observations and observations[0]["version_id"] == revision["id"]
    assert event_verifier(directory)(observations[0], "demo", "composed")
    assert model.inputs[0][0]["content"] == "Selected instructions"
    context_report, = [event for event in agent.current_session["trace"] if event.get("type") == "context_report"]
    model_call, = [event for event in agent.current_session["trace"] if event.get("type") == "llm_call"]
    assert context_verifier(directory)(context_report, "demo", "composed")
    assert context_report["status"] == "prepared" and context_report["compacted"] is compact
    assert context_report["llm_call_id"] == observations[0]["llm_call_id"] == model_call["id"]
    assert context_report["turn"] == observations[0]["turn"] == 1
    assert observations[0]["system_sha256"] == system_sha256(model.inputs[0])
    assert context_report["input_messages"] == len(model.inputs[0])
    trace = agent.current_session["trace"]
    assert trace.index(model_call) < trace.index(context_report) < trace.index(observations[0])
