"""Real pinned ConnectOnion Agent loop with scripted LLM; no live model calls."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from connectonion.core.llm import LLMResponse, ToolCall
from connectonion.core.usage import TokenUsage

from co_studio import task_eval
from co_studio.evaluation import EvaluationError


def case_named(name):
    return next(case for case in task_eval.cases() if case["case_id"] == name)


def response(content=None, calls=()):
    return LLMResponse(content=content, tool_calls=list(calls), raw_response=None,
                       usage=TokenUsage(input_tokens=20, output_tokens=7))


class Scripted:
    model = "offline-real-agent-test"

    def __init__(self, responses):
        self.responses = list(responses)
        self.inputs = []

    def complete(self, messages, **kwargs):
        self.inputs.append(deepcopy(messages))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def correct_script(case):
    calls = [ToolCall(name=name, arguments=args, id=f"test-tool-{i}")
             for i, (name, args) in enumerate(case["calls"])]
    return Scripted(([response(calls=calls)] if calls else []) + [response(json.dumps(case["expected"]))])


@pytest.mark.parametrize("case", task_eval.cases(), ids=lambda value: value["case_id"])
def test_real_agent_good_cases_natural_termination_parameters_and_source(case, tmp_path):
    provider = correct_script(case)
    before = deepcopy(task_eval.SERVICES)
    journal = tmp_path / "dispatch.jsonl"
    row = task_eval.execute_case(case, provider, tmp_path, journal=journal)
    assert row["status"] == "completed" and row["finish_reason"] == "natural"
    assert row["task_success"] and row["exact_answer_pass"] and row["tool_arguments_pass"]
    assert row["trace_valid"] and row["source_unchanged"]
    assert task_eval.SERVICES == before
    assert row["tool_requested_count"] == len(case["calls"])
    assert len(row["calls"]) == len(provider.inputs) == (2 if case["calls"] else 1)
    assert len(journal.read_text().splitlines()) == len(provider.inputs)
    for tool in row["tool_results"]:
        assert tool["call_id"].startswith("test-tool-")
        assert isinstance(tool["arguments"], dict)
    if case["case_id"] == "missing":
        assert row["tool_results"][0]["status"] != "success"
        assert row["task_success"]  # Correct handling of a failed lookup is a successful task.


def test_successful_task_and_tool_success_have_distinct_denominators(tmp_path):
    rows = [task_eval.execute_case(case, correct_script(case), tmp_path / case["case_id"])
            for case in task_eval.cases()]
    report = task_eval.summarize(rows, 6)
    assert report["task_success_rate"] == 1
    assert report["logical_tool_results"] == 6
    assert report["tool_success_rate_observed"] == pytest.approx(5 / 6)
    assert report["provider_dispatches"] == 11
    assert report["usage"]["input_tokens"]["observed_sum"] == 220
    assert report["usage"]["total_tokens"]["observed_sum"] is None


def test_answer_without_requested_tool_is_not_a_success(tmp_path):
    case = case_named("multiply")
    row = task_eval.execute_case(case, Scripted([response('{"result":391}')]), tmp_path)
    assert row["exact_answer_pass"] and not row["tool_arguments_pass"]
    assert not row["task_success"] and row["tool_requested_count"] == 0


@pytest.mark.parametrize("answer", ['{"result":true}', '{"result":392}', '{"result":391,"invented":true}',
    '```json\n{"result":391}\n```', '{"result":999,"result":391}', '{"result":NaN}', '{"result":Infinity}'])
def test_wrong_or_non_strict_final_json_is_not_success(tmp_path, answer):
    case = case_named("multiply")
    provider = correct_script(case)
    provider.responses[-1] = response(answer)
    row = task_eval.execute_case(case, provider, tmp_path)
    assert row["tool_arguments_pass"] and not row["exact_answer_pass"] and not row["task_success"]


def test_unknown_extra_call_cannot_hide_behind_correct_answer(tmp_path):
    case = case_named("multiply")
    provider = correct_script(case)
    provider.responses[0].tool_calls.append(ToolCall(name="restart_service", arguments={}, id="unknown-1"))
    row = task_eval.execute_case(case, provider, tmp_path)
    assert row["exact_answer_pass"] and row["tool_requested_count"] == 2
    assert not row["tool_arguments_pass"] and not row["task_success"]


def test_duplicate_tool_ids_make_trace_invalid(tmp_path):
    case = case_named("two-tools")
    provider = correct_script(case)
    for call in provider.responses[0].tool_calls:
        call.id = "duplicate"
    row = task_eval.execute_case(case, provider, tmp_path)
    assert not row["trace_valid"] and not row["task_success"]


def test_invalid_arguments_do_not_execute_or_become_success(tmp_path):
    case = case_named("multiply")
    provider = correct_script(case)
    provider.responses[0].tool_calls[0].arguments["a"] = "17"
    row = task_eval.execute_case(case, provider, tmp_path)
    assert row["executed_arguments"] == []
    assert row["tool_results"][0]["status"] != "success"
    assert not row["task_success"]


def test_infinite_tool_loop_stops_at_two_provider_dispatches(tmp_path):
    case = case_named("multiply")
    provider = Scripted([response(calls=[ToolCall(name="multiply", arguments={"a":17,"b":23}, id=f"loop-{i}")])
                         for i in range(5)])
    row = task_eval.execute_case(case, provider, tmp_path)
    assert len(provider.inputs) == len(row["calls"]) == 2
    assert row["finish_reason"] != "natural" and not row["task_success"]


def test_provider_exception_stays_failure_with_safe_error_type(tmp_path):
    row = task_eval.execute_case(case_named("multiply"), Scripted([RuntimeError("DO-NOT-REPORT-private")]), tmp_path)
    assert row["status"] == "error" and not row["task_success"]
    assert row["error_type"] == "RuntimeError"
    assert len(row["calls"]) == 1 and row["calls"][0]["status"] == "error"
    assert "DO-NOT-REPORT-private" not in json.dumps(row)


def test_source_mutation_cannot_pass_success(tmp_path, monkeypatch):
    monkeypatch.setattr(task_eval, "SERVICES", deepcopy(task_eval.SERVICES))
    case = case_named("refuse-write")
    class Mutating(Scripted):
        def complete(self, *args, **kwargs):
            task_eval.SERVICES["demo-ready"]["port"] = 9999
            return super().complete(*args, **kwargs)
    provider = Mutating([response(json.dumps(case["expected"]))])
    row = task_eval.execute_case(case, provider, tmp_path)
    assert not row["source_unchanged"] and not row["task_success"]


@pytest.mark.parametrize("args", [{"repeats":True}, {"repeats":0}, {"repeats":6}, {"case_ids":[]},
    {"case_ids":["unknown"]}, {"case_ids":["multiply","multiply"]}, {"case_ids":[{}]}])
def test_plan_validation(args):
    with pytest.raises(EvaluationError):
        task_eval.plan(**args)


def test_plan_reserves_two_calls_per_readonly_task_and_is_frozen():
    one = task_eval.plan()
    assert one == task_eval.plan()
    assert len(one["schedule"]) == 6 and one["max_dispatches"] == 12
    assert task_eval.plan(["multiply"], repeats=3)["max_dispatches"] == 6


@pytest.mark.parametrize("patch", [{"allow_model_calls":False}, {"max_calls":1}, {"max_calls":True},
    {"max_calls":61}, {"model":""}, {"model":"https://not-a-model"}, {"timeout":0}, {"timeout":True}, {"timeout":181}])
def test_run_preconditions_before_files_or_worker(tmp_path, monkeypatch, patch):
    monkeypatch.setattr(task_eval.subprocess, "run", lambda *a, **kw: pytest.fail("worker invoked"))
    args = dict(model="test", allow_model_calls=True, max_calls=2, case_ids=["multiply"], timeout=2)
    args.update(patch)
    with pytest.raises(EvaluationError):
        task_eval.run(tmp_path / "absent", **args)
    assert not (tmp_path / "absent").exists()


def test_timeout_and_worker_error_stay_in_task_denominator(tmp_path, monkeypatch):
    def child(command, **kwargs):
        body = json.loads(kwargs["input"])
        assert kwargs["timeout"] == 2
        if body["case_id"] == "multiply":
            Path(body["journal"]).write_text('{"sequence":1,"event":"dispatch"}\n')
            raise subprocess.TimeoutExpired(command, 2)
        return SimpleNamespace(returncode=1, stdout="private ignored", stderr="private ignored")
    monkeypatch.setattr(task_eval.subprocess, "run", child)
    report = task_eval.run(tmp_path / "run", model="test", allow_model_calls=True, max_calls=4,
                           case_ids=["multiply","ready"], timeout=2)
    stats = report["summary"]
    assert stats["started_tasks"] == 2 and stats["run_complete"]
    assert stats["task_success_rate"] == 0 and stats["provider_dispatches"] == 1
    assert stats["tool_success_rate_observed"] is None and stats["tools_unobserved_on_timeout"]
    assert "private ignored" not in (tmp_path / "run/results.jsonl").read_text()


def test_interrupt_stops_next_task_and_records_started_failure(tmp_path, monkeypatch):
    calls = []
    def child(*args, **kwargs):
        calls.append(args)
        raise KeyboardInterrupt()
    monkeypatch.setattr(task_eval.subprocess, "run", child)
    report = task_eval.run(tmp_path / "run", model="test", allow_model_calls=True, max_calls=12)
    assert len(calls) == 1 and report["summary"]["started_tasks"] == 1
    assert not report["summary"]["run_complete"]
    assert report["summary"]["task_success_rate"] == 0


def test_existing_output_is_never_overwritten(tmp_path, monkeypatch):
    root = tmp_path / "run"
    root.mkdir()
    (root / "keep").write_text("preserve")
    monkeypatch.setattr(task_eval.subprocess, "run", lambda *a, **kw: pytest.fail("worker invoked"))
    with pytest.raises(EvaluationError):
        task_eval.run(root, model="test", allow_model_calls=True, max_calls=12)
    assert (root / "keep").read_text() == "preserve"


def test_worker_without_consent_never_initializes_provider(monkeypatch):
    import io
    monkeypatch.setattr(sys, "argv", ["task_eval", "worker"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"case_id":"multiply", "model":"test", "journal":"unused"})))
    monkeypatch.setattr("co_studio.eval_provider.create_evaluation_llm", lambda *a, **kw: pytest.fail("no consent"))
    with pytest.raises((EvaluationError, SystemExit, ValueError)):
        task_eval.main()


def test_real_isolated_process_is_killed_and_reaped_at_task_deadline(tmp_path, monkeypatch):
    original_run = subprocess.run
    child_pids = []
    def bounded_child(command, **kwargs):
        assert json.loads(kwargs["input"])["allow_model_calls"] is True
        # Exercise an actual process and the parent's timeout path without
        # ever importing a provider or sending a network request in the child.
        try:
            return original_run([sys.executable, "-c", "import os,time;print(os.getpid(),flush=True);time.sleep(10)"],
                                capture_output=True, text=True, timeout=kwargs["timeout"])
        except subprocess.TimeoutExpired as exc:
            child_pids.append(int(exc.stdout.strip()))
            raise
    monkeypatch.setattr(task_eval.subprocess, "run", bounded_child)
    start = time.perf_counter()
    report = task_eval.run(tmp_path / "run", model="test", allow_model_calls=True, max_calls=2,
                          case_ids=["multiply"], timeout=1)
    assert time.perf_counter() - start < 5
    assert report["summary"]["task_success_rate"] == 0
    assert report["summary"]["provider_dispatches"] == 0
    assert len(child_pids) == 1
    with pytest.raises(ProcessLookupError):
        os.kill(child_pids[0], 0)


def test_parent_latency_covers_worker_startup_for_success_and_failure(tmp_path, monkeypatch):
    def child(command, **kwargs):
        body = json.loads(kwargs["input"])
        if body["case_id"] == "multiply":
            return SimpleNamespace(returncode=0, stdout=json.dumps({"status":"completed", "task_success":False,
                 "duration_ms":999999, "calls":[], "tool_results":[]}), stderr="")
        raise subprocess.TimeoutExpired(command, 2)
    monkeypatch.setattr(task_eval.subprocess, "run", child)
    task_eval.run(tmp_path / "run", model="test", allow_model_calls=True, max_calls=4,
                  case_ids=["multiply","ready"], timeout=2)
    rows = [json.loads(line) for line in (tmp_path / "run/results.jsonl").read_text().splitlines()]
    assert all(row["latency_includes_worker_startup"] is True for row in rows)
    assert rows[0]["agent_duration_ms"] == 999999
    assert rows[1]["agent_duration_ms"] is None
    assert all(row["duration_ms"] < 999999 for row in rows)


@pytest.mark.parametrize("error_type", ["InsufficientCreditsError", "LLMAuthenticationError", "PaidModelRequiredError",
                                       "AuthenticationError", "PermissionDeniedError"])
def test_unrecoverable_provider_errors_stop_remaining_task_dispatches(tmp_path, monkeypatch, error_type):
    invoked = []
    def child(command, **kwargs):
        invoked.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"status":"error", "error_type":error_type,
            "task_success":False, "duration_ms":1, "calls":[], "tool_results":[]}), stderr="")
    monkeypatch.setattr(task_eval.subprocess, "run", child)
    report = task_eval.run(tmp_path / "run", model="test", allow_model_calls=True, max_calls=12)
    assert len(invoked) == 1
    assert report["summary"]["started_tasks"] == 1
    assert report["summary"]["task_success_rate"] == 0
    assert report["summary"]["run_complete"] is False
    assert report["summary"]["unstarted_tasks"] == 5
    assert report["stop_reason"] == error_type and report["status"] == "stopped"


@pytest.mark.parametrize("error_type", ["LLMRateLimitError", "ProviderServiceError", "RuntimeError"])
def test_transient_or_task_specific_failure_does_not_look_like_expired_credentials(tmp_path, monkeypatch, error_type):
    invoked = []
    def child(command, **kwargs):
        invoked.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"status":"error", "error_type":error_type,
            "task_success":False, "duration_ms":1, "calls":[], "tool_results":[]}), stderr="")
    monkeypatch.setattr(task_eval.subprocess, "run", child)
    report = task_eval.run(tmp_path / "run", model="test", allow_model_calls=True, max_calls=4,
                           case_ids=["multiply","ready"])
    assert len(invoked) == 2 and report["summary"]["run_complete"]
    assert report["summary"]["task_success_rate"] == 0


def test_worker_initialization_failure_preserves_only_safe_error_type(tmp_path, monkeypatch, capsys):
    import io
    error = type("LLMAuthenticationError", (Exception,), {})
    def failed(*args, **kwargs):
        raise error("SECRET-account-token")
    journal = tmp_path / "never-dispatched.jsonl"
    monkeypatch.setattr(sys, "argv", ["task_eval", "worker"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"case_id":"multiply", "model":"test",
                        "allow_model_calls":True, "journal":str(journal)})))
    monkeypatch.setattr("co_studio.eval_provider.create_evaluation_llm", failed)
    task_eval.main()
    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["error_type"] == "LLMAuthenticationError"
    assert result["initialization_failed"] and not result["task_success"]
    assert result["calls"] == [] and result["duration_ms"] is None
    assert "SECRET-account-token" not in output and not journal.exists()
