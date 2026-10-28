"""Offline synthetic context-policy experiment, not model-quality evidence.

Run separately with ``python -m co_studio.context_eval``. Framework auto_compact
is the installed implementation, driven through real Agent events; only its
summary model is scripted. Do not run its temporary mock inside a live server.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import importlib
from importlib.metadata import version
import json
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

from .context_policy import (ESTIMATOR, POLICY_VERSION, ContextError, ContextPolicy,
                             build_context, estimate_tokens, message_groups)

DATASET_VERSION = "synthetic-context-contract-v1"


@dataclass(frozen=True)
class ContextCase:
    case_id: str
    category: str
    messages: list[dict]
    expected: tuple[str, ...]
    budget: int = 1100


def _dialogue():
    messages = []
    for index in range(16):
        messages.extend([{"role": "user", "content": f"Background exchange {index}: " + "unrelated detail " * 65},
                         {"role": "assistant", "content": "Acknowledged unrelated background. " * 25}])
    return messages


def _pair(call_id, content):
    return [{"role": "assistant", "content": "", "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": "inspect", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": call_id, "name": "inspect", "content": content}]


def context_cases() -> list[ContextCase]:
    """Six distinct, versioned contract scenarios; not 6 production tasks."""
    system = {"role": "system", "content": "Use recorded evidence. Never invent successful tool results."}
    question = {"role": "user", "content": "Report the retained task constraints and evidence; do not invent missing facts."}
    task = {"role": "user", "content": json.dumps({"studio_task": {
        "goal": "repair checkout flow", "constraints": ["no-payment-write", "region-eu-west"],
        "pending": "verify-after-approval"}})}
    old_error = _pair("error-call", "x" * 600 + "\n" + json.dumps({"error": "E_LATE_FIELD"}))
    structured = _pair("evidence-call", json.dumps({"studio_result": {
        "error": "E_SCHEMA_42", "artifact": "report-rev-7", "status": "failed"}, "padding": "x" * 4000}))
    # Exact five-message suffix starts with a tool result, exposing the upstream
    # plugin's boundary rather than asserting every framework summary is broken.
    boundary = _pair("boundary-call", "PAIR_RESULT_9") + [
        {"role": "assistant", "content": "Read evidence."},
        {"role": "user", "content": "Continue."},
        {"role": "assistant", "content": "Ready."}, question]
    return [
        ContextCase("task-fields", "old-explicit-task", [system, task, *_dialogue(), question],
                    ("no-payment-write", "region-eu-west", "verify-after-approval")),
        ContextCase("tool-evidence", "structured-failed-tool", [system, *structured, *_dialogue(), question],
                    ("E_SCHEMA_42", "report-rev-7", "failed")),
        ContextCase("tool-boundary", "pair-crosses-last-five", [system, *_dialogue(), *boundary], ("PAIR_RESULT_9",)),
        ContextCase("unstructured-tail", "known-extraction-limit", [system, *old_error, *_dialogue(), question], ("E_LATE_FIELD",)),
        ContextCase("oversized-current", "mandatory-budget-refusal", [system, task, *_dialogue(),
                    {"role": "user", "content": "CURRENT_WORK_4 " + "cannot drop current work " * 900}], ("CURRENT_WORK_4",)),
        ContextCase("updated-task", "latest-explicit-value", [system, task, *_dialogue(),
                    {"role": "user", "content": json.dumps({"studio_task": {"goal": "repair checkout flow", "approval": "approval-denied", "constraints": ["read-only-now"]}})},
                    {"role": "assistant", "content": "Acknowledged."}, question], ("approval-denied", "read-only-now")),
    ]


def _framework_auto_compact(messages):
    from connectonion import Agent
    from connectonion.core.llm import LLMResponse
    from connectonion.core.usage import TokenUsage, get_context_limit
    from connectonion.useful_plugins import auto_compact

    summary_calls = []

    class ScriptedModel:
        model = "offline-context-script"

        def complete(self, messages, **kwargs):
            # A deliberately transparent scripted model, not an oracle using
            # case.expected. It repeats only explicit structured task lines
            # visible in the actual framework summary input.
            content = "\n".join(item["content"] for item in messages)
            retained = [line for line in content.splitlines() if '"studio_task"' in line]
            text = "Scripted extract, not semantic summarization:\n" + "\n".join(retained)
            summary_calls.append({"estimated_input_tokens": estimate_tokens(messages),
                                  "estimated_output_tokens": estimate_tokens([{"role": "assistant", "content": text}]),
                                  "provider_input_tokens": None, "provider_output_tokens": None})
            return LLMResponse(content=text, tool_calls=[], raw_response=None, usage=None)

    model = ScriptedModel()
    def scripted_llm_do(prompt, **kwargs):
        return model.complete([{"role": "user", "content": prompt}]).content

    with tempfile.TemporaryDirectory(prefix="studio-context-eval-") as directory:
        agent = Agent("offline context evaluation", llm=model, plugins=[auto_compact], quiet=True, log=False,
                      co_dir=Path(directory), state_dir=Path(directory))
        agent.current_session = {"session_id": "offline-auto", "messages": deepcopy(messages), "trace": [], "iteration": 1}
        # Synthetic trigger only, not measured/billed usage; never included in
        # the report's provider token totals. All strategies use the same input.
        agent.last_usage = TokenUsage(input_tokens=int(get_context_limit(model.model) * .95))
        module = importlib.import_module("connectonion.llm_do")
        with patch.object(module, "llm_do", scripted_llm_do):
            agent._invoke_events("after_llm")
        return deepcopy(agent.current_session["messages"]), summary_calls


def evaluate_context_case(case: ContextCase, strategy: str) -> dict:
    """One context transformation plus explicit, non-semantic task assertions."""
    source = deepcopy(case.messages)
    started = time.perf_counter()
    error, summary_calls = None, []
    try:
        if strategy == "full":
            messages = deepcopy(source)
        elif strategy == "truncate":
            # Naive budgeted suffix, intentionally not pair-aware; report that
            # failure separately instead of silently repairing the baseline.
            messages = [source[0]]
            for message in reversed(source[1:]):
                candidate = [source[0], message, *messages[1:]]
                if estimate_tokens(candidate) > case.budget:
                    break
                messages = candidate
        elif strategy == "framework_auto_compact":
            messages, summary_calls = _framework_auto_compact(source)
        elif strategy == "studio_extractive":
            messages = build_context(source, ContextPolicy(case.budget), agent_slug="eval-agent",
                                     session_id=case.case_id).messages
        else:
            raise ValueError("Unknown context evaluation strategy")
    except ContextError as exc:
        messages, error = [], type(exc).__name__
    text = json.dumps(messages, ensure_ascii=False)
    assertions = [{"expected": value, "retained": value in text} for value in case.expected]
    try:
        message_groups(messages)
        pairs_valid = error is None
    except ContextError:
        pairs_valid = False
    estimated = estimate_tokens(messages) if error is None else None
    within_budget = estimated is not None and estimated <= case.budget
    return {"case_id": case.case_id, "category": case.category, "strategy": strategy,
            "budget": case.budget, "estimated_input_tokens": estimated,
            "local_transform_elapsed_ms": (time.perf_counter() - started) * 1000,
            "provider_input_tokens": None, "provider_output_tokens": None, "provider_total_tokens": None,
            "summary_calls": summary_calls, "summary_call_count": len(summary_calls),
            "assertions": assertions, "fields_retained": sum(item["retained"] for item in assertions),
            "fields_expected": len(assertions), "pairs_valid": pairs_valid, "within_budget": within_budget,
            "scripted_task_pass": error is None and within_budget and pairs_valid and all(item["retained"] for item in assertions),
            "error": error, "source_unchanged": case.messages == source}


def run_context_evaluation() -> dict:
    """Reusable report. No file output/network/model calls or live runtime patch."""
    strategies = ("full", "truncate", "framework_auto_compact", "studio_extractive")
    cases = context_cases()
    results = [evaluate_context_case(case, strategy) for case in cases for strategy in strategies]
    return {"dataset_version": DATASET_VERSION, "data_origin": "synthetic_offline_contract_scenarios",
            "dataset_sha256": hashlib.sha256(json.dumps([asdict(case) for case in cases], sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest(),
            "framework_version": version("connectonion"), "case_count": len(cases),
            "policy_version": POLICY_VERSION, "estimator": ESTIMATOR,
            "quality_scope": "exact-field retention and pair/budget assertions; not semantic or production task success",
            "usage_status": "unknown; all providers are scripted; forced 95% auto_compact trigger is synthetic",
            "results": results,
            "summary": {strategy: {"cases": len(cases),
                                    "scripted_task_passes": sum(row["scripted_task_pass"] for row in results if row["strategy"] == strategy),
                                    "fields_retained": sum(row["fields_retained"] for row in results if row["strategy"] == strategy),
                                    "fields_expected": sum(row["fields_expected"] for row in results if row["strategy"] == strategy)}
                        for strategy in strategies}}


if __name__ == "__main__":
    print(json.dumps(run_context_evaluation(), ensure_ascii=False, indent=2))
