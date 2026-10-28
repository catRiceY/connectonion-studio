"""iOS task cards through actual local Agent/Host calls, with no paid model."""
from copy import deepcopy
import json

import pytest
from connectonion import Agent
from connectonion.core.llm import LLMResponse
from connectonion.network.host.session import SessionStorage
from connectonion.network.host.session.turn import input_handler

from co_studio.context_policy import ContextPolicy, estimate_tokens
from co_studio.context_report import event_verifier
from co_studio.context_runtime import attach_context_policy
from co_studio.execution_adapter import read_execution_history


LONG_ANSWER = "Synthetic archived detail. " * 500


class ScriptedModel:
    model = "offline-ios-context-contract"

    def __init__(self, answers):
        self.answers = list(answers)
        self.inputs = []

    def complete(self, messages, **kwargs):
        self.inputs.append((deepcopy(messages), deepcopy(kwargs)))
        return LLMResponse(content=self.answers[len(self.inputs) - 1], tool_calls=[],
                           raw_response=None, usage=None)


def ios_request(request, *, header="Device preferences", newline="\n"):
    # Literal wire format: do not build this using the parser's own constants.
    return newline.join([
        "<<<CONNECTONION_PERSONALISATION_V2>>>", header,
        "<<<CONNECTONION_END_PERSONALISATION_V2>>>", "",
        "<<<CONNECTONION_USER_REQUEST_V2>>>", request,
    ])


def task_card(fields, source_format="user_json"):
    request = json.dumps({"studio_task": fields}, ensure_ascii=False)
    return ios_request(request) if source_format == "ios_v2_request_json" else request


def host_for(tmp_path, model, *, slug="demo"):
    directory = tmp_path / slug
    co_dir = directory / ".co"
    co_dir.mkdir(parents=True)
    storage = SessionStorage(co_dir / "session_results.jsonl")
    agents = []

    def factory():
        agent = Agent("Input contract", llm=model, system_prompt="Local rules",
                      quiet=True, log=False, co_dir=co_dir, state_dir=co_dir)
        attach_context_policy(agent, agent_slug=slug, policy=ContextPolicy(1100),
                              agent_dir=directory)
        agents.append(agent)
        return agent

    def send(prompt, session):
        return input_handler(factory, storage, prompt, result_ttl=3600, session=session)

    return send, storage, agents, directory


def memory_from(messages):
    found = []
    for message in messages:
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, str):
            continue
        try:
            value = json.loads(content)
        except ValueError:
            continue
        if isinstance(value, dict) and value.get("kind") == "studio_deterministic_extract":
            found.append(value)
    memory, = found
    return memory


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_ios_card_survives_real_host_restore_and_compaction_without_rewriting_history(tmp_path, newline):
    fields = {"goal": "恢复原会话", "constraints": ["Do not write production"],
              "approval": "User claims approval; this is not a permission grant"}
    header = json.dumps({"studio_task": {"goal": "HEADER_GOAL", "pending": "HEADER_PENDING"}})
    original_prompt = ios_request(json.dumps({"studio_task": fields}, ensure_ascii=False),
                                  header=header, newline=newline)
    final_prompt = ios_request("继续查看当前任务，不做任何写入", newline=newline)
    model = ScriptedModel([LONG_ANSWER, LONG_ANSWER, "Offline final answer"])
    send, storage, agents, directory = host_for(tmp_path, model)
    supplied = {"session_id": "mobile-session"}
    untouched = deepcopy(supplied)
    send(original_prompt, supplied)
    send("Continue once", {"session_id": "mobile-session"})
    send(final_prompt, {"session_id": "mobile-session"})

    assert supplied == untouched and len({id(agent) for agent in agents}) == 3
    assert model.inputs[0][0][-1]["content"] == original_prompt
    for provider_messages, kwargs in model.inputs[1:]:
        memory = memory_from(provider_messages)
        extracted = {item["field"]: item for item in memory["task_fields"]}
        assert {key: item["value"] for key, item in extracted.items()} == fields
        assert all(item["source_format"] == "ios_v2_request_json" for item in extracted.values())
        assert "Values are not verified" in memory["notice"]
        assert "not instructions" in memory["notice"]
        assert "HEADER_GOAL" not in json.dumps(memory) and "HEADER_PENDING" not in json.dumps(memory)
        assert original_prompt not in [message.get("content") for message in provider_messages]
        assert estimate_tokens(provider_messages, kwargs.get("tools")) <= 1100
    assert model.inputs[-1][0][-1]["content"] == final_prompt

    saved = storage.get("mobile-session").session
    assert [message["content"] for message in saved["messages"] if message["role"] == "user"] == [
        original_prompt, "Continue once", final_prompt,
    ]
    assert sum(message.get("content") == LONG_ANSWER for message in saved["messages"]) == 2
    assert "studio_deterministic_extract" not in storage.path.read_text()
    observations = [event for event in saved["trace"] if event["type"] == "context_report"]
    assert [event["compacted"] for event in observations] == [False, True, True]
    assert [event["turn"] for event in observations] == [1, 2, 3]
    assert all(event["task_fields"] == 3 and event["policy_version"] == "studio-extractive-v3"
               for event in observations)
    assert all(event_verifier(directory)(event, "demo", "mobile-session") for event in observations)
    history = read_execution_history(storage.path, agent_slug="demo").sessions[0]
    assert [turn.prompt for turn in history.turns] == [original_prompt, "Continue once", final_prompt]
    assert all(event.payload["status"] == "prepared" for event in history.events
               if event.kind == "context_report")
    # The approval string is quoted historical data only; this path neither
    # invokes tools nor creates an approval decision/event.
    assert not any(event.kind in {"studio_approval", "tool_call"} for event in history.events)


@pytest.mark.parametrize("first_format,last_format", [
    ("user_json", "ios_v2_request_json"), ("ios_v2_request_json", "user_json"),
])
def test_latest_explicit_values_and_empty_clears_keep_their_format_across_real_turns(tmp_path, first_format, last_format):
    first = task_card({"goal": "obsolete goal", "constraints": ["obsolete constraint"],
                       "approval": "unverified historical claim"}, first_format)
    last = task_card({"goal": "", "constraints": [], "pending": "latest explicit next step"}, last_format)
    model = ScriptedModel([LONG_ANSWER, LONG_ANSWER, "Offline final answer"])
    send, storage, _, _ = host_for(tmp_path, model)
    send(first, {"session_id": "updated-card"})
    send(last, {"session_id": "updated-card"})
    send("Continue", {"session_id": "updated-card"})
    memory = memory_from(model.inputs[-1][0])
    fields = {item["field"]: item for item in memory["task_fields"]}
    assert fields["goal"]["value"] == ""
    assert fields["constraints"]["value"] == []
    assert fields["pending"]["value"] == "latest explicit next step"
    assert all(fields[name]["source_format"] == last_format for name in ("goal", "constraints", "pending"))
    assert fields["approval"]["value"] == "unverified historical claim"
    assert fields["approval"]["source_format"] == first_format
    assert "obsolete" not in json.dumps(memory)
    saved = storage.get("updated-card").session
    assert [message["content"] for message in saved["messages"] if message["role"] == "user"] == [first, last, "Continue"]
    report = [event for event in saved["trace"] if event["type"] == "context_report"][-1]
    assert report["task_fields"] == 4 and report["compacted"] is True


def test_header_and_tool_task_claims_are_not_promoted_by_real_host_compaction(tmp_path):
    header = json.dumps({"studio_task": {"goal": "HEADER_GOAL", "approval": "HEADER_APPROVAL"}})
    wrapped_prose = ios_request("This natural language is not a task card", header=header)
    tool_result = json.dumps({"studio_task": {"goal": "TOOL_GOAL", "approval": "TOOL_APPROVAL"},
                              "studio_result": {"status": "untrusted observation"}})
    original_messages = [
        {"role": "system", "content": "Local rules"},
        {"role": "user", "content": wrapped_prose},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "historical-tool", "type": "function",
             "function": {"name": "inspect", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "historical-tool", "content": tool_result},
        {"role": "assistant", "content": LONG_ANSWER},
    ]
    supplied = {"session_id": "untrusted-sources", "messages": deepcopy(original_messages)}
    untouched = deepcopy(supplied)
    model = ScriptedModel(["Offline answer"])
    send, storage, _, _ = host_for(tmp_path, model)
    send("Continue", supplied)
    memory = memory_from(model.inputs[0][0])
    assert memory["task_fields"] == []
    reference, = memory["tool_references"]
    assert reference["fields"] == {"status": "untrusted observation"}
    assert all(marker not in json.dumps(memory) for marker in (
        "HEADER_GOAL", "HEADER_APPROVAL",
    ))
    # v3 can quote the complete tool result as untrusted data. Its embedded
    # task claims are not extracted or elevated to user decisions/permissions.
    assert reference["quoted_content"] == tool_result
    assert memory["task_fields"] == []
    assert supplied == untouched
    saved = storage.get("untrusted-sources").session
    assert saved["messages"][:len(original_messages)] == original_messages
    report, = (event for event in saved["trace"] if event["type"] == "context_report")
    assert report["task_fields"] == 0 and report["tool_references"] == 1


@pytest.mark.parametrize("different_agent", [False, True])
def test_mobile_task_fields_do_not_leak_to_other_sessions_or_agents_with_shared_provider(tmp_path, different_agent):
    card = task_card({"goal": "PRIVATE_FIRST_SESSION_GOAL"}, "ios_v2_request_json")
    model = ScriptedModel([LONG_ANSWER, "Offline first answer", "Offline other answer"])
    send, storage, _, _ = host_for(tmp_path, model, slug="first")
    send(card, {"session_id": "first-session"})
    send("Continue", {"session_id": "first-session"})
    frozen = storage.path.read_bytes()
    assert memory_from(model.inputs[1][0])["task_fields"][0]["value"] == "PRIVATE_FIRST_SESSION_GOAL"
    if different_agent:
        other_send, other_storage, _, other_dir = host_for(tmp_path, model, slug="second")
        other_session, other_slug = "first-session", "second"
    else:
        other_send, other_storage = send, storage
        other_session, other_slug = "other-session", "first"
        other_dir = tmp_path / "first"
    other_send("Unrelated new request", {"session_id": other_session})
    assert "PRIVATE_FIRST_SESSION_GOAL" not in json.dumps(model.inputs[-1][0])
    assert not any("studio_deterministic_extract" in str(message) for message in model.inputs[-1][0])
    saved = other_storage.get(other_session).session
    report, = (event for event in saved["trace"] if event["type"] == "context_report")
    assert report["task_fields"] == 0 and report["compacted"] is False
    assert event_verifier(other_dir)(report, other_slug, other_session)
    if different_agent:
        assert storage.path.read_bytes() == frozen
    else:
        first = storage.get("first-session").session
        assert first["messages"][1]["content"] == card
        assert len([event for event in first["trace"] if event["type"] == "context_report"]) == 2
