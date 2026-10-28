"""Versioned synthetic engineering scenarios, independent of pytest collection.

These are contracts, not a natural-language reasoning benchmark. Fixture secrets
are invented strings; no personal history, credentials, Agent hosts or models.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
import json
from pathlib import Path
import tempfile

from .execution_adapter import ConvertedSession, convert_snapshot
from .execution_models import ExecutionEvent, SessionRecord, TurnRecord
from .execution_privacy import redact_text, redact_value
from .history_archive import _validated_capture, prepare_capture

CASES_VERSION = "studio-engineering-contracts-v1"


@dataclass(frozen=True)
class EngineeringCase:
    case_id: str
    category: str
    operation: str
    inputs: dict
    expected: object
    scoring_rule: str = "Exact JSON equality; exceptions are compared by class, never by private message"


def engineering_cases() -> list[EngineeringCase]:
    cases = []
    def add(identity, category, operation, inputs, expected):
        cases.append(EngineeringCase(identity, category, operation, inputs, expected))

    add("privacy-nested-credential", "privacy", "redact_value", {"value": {"nested": {"api_key": "synthetic-secret"}, "answer": 7}}, {"nested": {"api_key": "[REDACTED]"}, "answer": 7})
    add("privacy-bearer-header", "privacy", "redact_text", {"value": "Authorization: Bearer syntheticTokenABC123"}, "Authorization: [REDACTED]")
    add("privacy-unterminated-key", "privacy", "redact_text", {"value": "before\n-----BEGIN PRIVATE KEY-----\nsynthetic"}, "before\n[REDACTED]")
    add("privacy-usage-not-secret", "privacy", "redact_value", {"value": {"usage": {"input_tokens": 17, "output_tokens": None}}}, {"usage": {"input_tokens": 17, "output_tokens": None}})
    add("privacy-source-detached", "privacy", "redaction_detached", {"value": {"outer": [{"password": "synthetic-only"}]}}, {"source_unchanged": True, "password": "[REDACTED]"})
    add("privacy-unlabelled-limit", "privacy", "redact_text", {"value": "unlabelled synthetic-value-123"}, "unlabelled synthetic-value-123")

    for identity, mutation in [
        ("archive-cross-agent", "agent"), ("archive-cross-session", "session"),
        ("archive-duplicate-turn", "duplicate_turn"), ("archive-reversed-sequence", "reverse"),
        ("archive-duplicate-event-id", "duplicate_event"), ("archive-orphan-turn", "orphan"),
    ]:
        add(identity, "archive-integrity", "archive_reject", {"mutation": mutation}, "ArchiveError")
    add("archive-boolean-turn", "archive-integrity", "archive_reject", {"mutation": "boolean_turn"}, "ValidationError")
    add("archive-tampered-fingerprint", "archive-integrity", "archive_fingerprint", {}, "ArchiveError")
    add("archive-jsonb-number-stability", "archive-integrity", "archive_numeric", {}, {"same_fingerprint": True})
    add("archive-source-detached", "archive-integrity", "archive_detached", {}, {"source_unchanged": True})

    add("adapter-missing-observation", "history-normalization", "adapter", {"variant": "missing"}, {"result": None, "duration_ms": None})
    add("adapter-unknown-kind", "history-normalization", "adapter", {"variant": "unknown"}, {"payload": {}, "issue": "unknown_event_kind"})
    add("adapter-foreign-session", "history-normalization", "adapter", {"variant": "foreign"}, "ValueError")
    add("adapter-duplicate-content", "history-normalization", "adapter", {"variant": "duplicate"}, {"unique_ids": True, "duplicate_issue": True})
    add("adapter-invalid-boundary", "history-normalization", "adapter", {"variant": "boundary"}, {"last_turn": None, "invalid_turn": True})
    add("adapter-unverified-provenance", "history-normalization", "adapter", {"variant": "provenance"}, {"status": "unknown", "version_id": None, "base_sha256": None, "signature_exposed": False})

    for identity, name, arguments in [
        ("tool-unknown-command", "shell", {}),
        ("tool-traversal-guide", "studio_get_guide", {"guide_id": "../../private"}),
        ("tool-extra-argument", "studio_get_guide", {"guide_id": "history", "path": "/tmp/private"}),
        ("tool-boolean-limit", "studio_list_guides", {"limit": True}),
        ("tool-schema-bound", "studio_list_guides", {"limit": 4}),
    ]:
        add(identity, "tool-allowlist", "tool_arguments", {"name": name, "arguments": arguments}, "McpPolicyError")
    add("tool-integral-json-normalization", "tool-allowlist", "tool_arguments", {"name": "studio_list_guides", "arguments": {"limit": 2.0}}, {"limit": 2})

    for variant, expected in [
        ("natural", {"terminal_reason": "natural", "tool_statuses": [], "executions": 0, "raised": None}),
        ("tool-success", {"terminal_reason": "natural", "tool_statuses": ["success"], "executions": 1, "raised": None}),
        ("tool-error", {"terminal_reason": "natural", "tool_statuses": ["error"], "executions": 1, "raised": None}),
        ("max-iterations", {"terminal_reason": "max_iterations", "tool_statuses": ["success"], "executions": 1, "raised": None}),
        ("provider-error", {"terminal_reason": "error", "tool_statuses": [], "executions": 0, "raised": "RuntimeError"}),
        ("tool-invalid-arguments", {"terminal_reason": "natural", "tool_statuses": ["error"], "executions": 0, "raised": None}),
    ]:
        add("runtime-" + variant, "real-co-scripted-provider", "runtime", {"variant": variant}, expected)
    return cases


def _fixture():
    owner = {"agent_slug": "synthetic-contract", "session_id": "session-1"}
    return ConvertedSession(SessionRecord(**owner, source_status="done"),
        (TurnRecord(**owner, turn_no=1, prompt="Synthetic task", result="Synthetic result"),
         TurnRecord(**owner, turn_no=2, prompt="Follow up")),
        (ExecutionEvent(**owner, event_id="event-1", sequence=0, kind="user_input", turn_no=1, payload={"content": "Synthetic task"}),
         ExecutionEvent(**owner, event_id="event-2", sequence=1, kind="tool_result", turn_no=1, payload={"result": "Synthetic result", "number": 1.0})))


def _snapshot(trace):
    return {"session_id": "session-1", "status": "done", "prompt": "Synthetic task", "result": None,
            "session": {"session_id": "session-1", "turn": 1, "trace": trace}}


def _exception(operation):
    try:
        operation()
    except Exception as exc:
        return type(exc).__name__
    return "accepted"


def evaluate_engineering_case(case: EngineeringCase, *, studio_gate: bool = False):
    """Execute the named implementation path against only case-owned fixtures."""
    data, operation = deepcopy(case.inputs), case.operation
    if operation == "redact_value":
        return redact_value(data["value"])
    if operation == "redact_text":
        return redact_text(data["value"])
    if operation == "redaction_detached":
        value = deepcopy(data["value"])
        result = redact_value(value)
        return {"source_unchanged": value == data["value"], "password": result["outer"][0]["password"]}
    if operation.startswith("archive_"):
        original = _fixture()
        if operation == "archive_reject":
            mutation = data["mutation"]
            if mutation in {"agent", "session"}:
                field = "agent_slug" if mutation == "agent" else "session_id"
                original = replace(original, turns=(original.turns[0].model_copy(update={field: "foreign"}), original.turns[1]))
            elif mutation == "duplicate_turn":
                original = replace(original, turns=(original.turns[0], original.turns[0]))
            elif mutation == "reverse":
                original = replace(original, events=tuple(reversed(original.events)))
            elif mutation == "duplicate_event":
                original = replace(original, events=(original.events[0], original.events[1].model_copy(update={"event_id": "event-1"})))
            elif mutation == "orphan":
                original = replace(original, events=(original.events[0].model_copy(update={"turn_no": 9}),))
            elif mutation == "boolean_turn":
                original = replace(original, turns=(original.turns[0].model_copy(update={"turn_no": True}),))
            return _exception(lambda: prepare_capture(original))
        first = prepare_capture(original)
        if operation == "archive_fingerprint":
            return _exception(lambda: _validated_capture(replace(first, capture_id="0" * 64)))
        if operation == "archive_numeric":
            changed = original.events[1].model_copy(update={"payload": {"result": "Synthetic result", "number": 1}})
            return {"same_fingerprint": first.capture_id == prepare_capture(replace(original, events=(original.events[0], changed))).capture_id}
        if operation == "archive_detached":
            before = deepcopy(original)
            first.events[1].payload["number"] = 99
            return {"source_unchanged": original == before}
    if operation == "adapter":
        variant = data["variant"]
        trace = [{"type": "user_input", "id": "u1", "turn": 1, "content": "Synthetic task"}]
        if variant == "unknown":
            trace.append({"type": "future_kind", "secret_body": "must-not-leak"})
        elif variant == "duplicate":
            trace.extend([{"type": "llm_call", "id": "same", "model": "offline"}] * 2)
        elif variant == "boundary":
            trace.extend([{"type": "tool_call", "turn": False, "name": "inspect"}, {"type": "tool_result", "name": "inspect"}])
        elif variant == "provenance":
            trace.append({"type": "prompt_provenance", "status": "bound", "version_id": "a" * 32,
                          "base_sha256": "b" * 64, "_studio_signature": "not-real"})
        snapshot = _snapshot(trace)
        if variant == "foreign":
            snapshot["session"]["session_id"] = "other"
            return _exception(lambda: convert_snapshot(snapshot, agent_slug="synthetic-contract"))
        result = convert_snapshot(snapshot, agent_slug="synthetic-contract")
        if variant == "missing":
            return {"result": result.turns[0].result, "duration_ms": result.turns[0].duration_ms}
        if variant == "unknown":
            return {"payload": result.events[-1].payload, "issue": result.issues[-1].code}
        if variant == "duplicate":
            return {"unique_ids": len({event.event_id for event in result.events}) == len(result.events),
                    "duplicate_issue": any(item.code == "duplicate_event_content" for item in result.issues)}
        if variant == "boundary":
            return {"last_turn": result.events[-1].turn_no, "invalid_turn": any(item.code == "invalid_turn" for item in result.issues)}
        payload = result.events[-1].payload
        return {"status": payload.get("status"), "version_id": payload.get("version_id"),
                "base_sha256": payload.get("base_sha256"), "signature_exposed": "_studio_signature" in payload}
    if operation == "tool_arguments":
        from .mcp_readonly import validate_arguments
        try:
            return validate_arguments(data["name"], data["arguments"])
        except Exception as exc:
            return type(exc).__name__
    if operation == "runtime":
        return _runtime_case(data["variant"], studio_gate=studio_gate)
    raise ValueError("Unknown engineering evaluation operation")


def _runtime_case(variant, *, studio_gate=False):
    from connectonion import Agent
    from connectonion.core.llm import LLMResponse, ToolCall

    executions, calls = [], []
    def calculate(a: int, b: int) -> int:
        """Add two synthetic integers; no external effects."""
        executions.append((a, b))
        if variant == "tool-error":
            raise ValueError("synthetic tool failure")
        return a + b

    class Scripted:
        model = "offline-engineering-contract"
        def complete(self, messages, **kwargs):
            calls.append(True)
            if variant == "provider-error":
                raise RuntimeError("synthetic provider failure")
            use_tool = variant != "natural" and (len(calls) == 1 or variant == "max-iterations")
            args = {"a": "not-an-integer", "b": 3} if variant == "tool-invalid-arguments" else {"a": 2, "b": 3}
            return LLMResponse(content="" if use_tool else "offline final",
                               tool_calls=[ToolCall(id=f"call-{len(calls)}", name="calculate", arguments=args)] if use_tool else [],
                               raw_response=None, usage=None)

    with tempfile.TemporaryDirectory(prefix="studio-contract-") as directory:
        agent = Agent("Synthetic engineering contract", llm=Scripted(), tools=[calculate], max_iterations=1 if variant == "max-iterations" else 3,
                      quiet=True, log=False, co_dir=Path(directory), state_dir=Path(directory))
        if studio_gate:
            if variant != "tool-invalid-arguments":
                raise ValueError("This offline gate comparison only exercises invalid arguments")
            from .mcp_agent import attach_studio_tools
            attach_studio_tools(agent, agent_dir=Path(directory).resolve(),
                                configuration={"mcp_enabled": False, "approvals_enabled": True,
                                               "approval_timeout_seconds": 30}, run_id="offline-eval")
        raised = None
        try:
            agent.input("Perform the synthetic operation", session={"session_id": "offline-contract"})
        except Exception as exc:
            raised = type(exc).__name__
        trace = agent.current_session["trace"]
        terminal = [item.get("reason") for item in trace if item.get("type") == "turn_result"]
        return {"terminal_reason": terminal[-1] if terminal else None,
                "tool_statuses": [item.get("status") for item in trace if item.get("type") == "tool_result"],
                "executions": len(executions), "raised": raised}
