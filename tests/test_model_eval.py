"""Model trial plumbing, never live provider requests or quality claims."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from connectonion.core.usage import TokenUsage

from co_studio.context_eval import context_cases
from co_studio.evaluation import EvaluationError
from co_studio.model_eval import make_plan, main, normalize_usage, run_trials, summarize


class FakeProvider:
    def __init__(self):
        self.calls = []

    def complete(self, messages, **kwargs):
        self.calls.append((deepcopy(messages), kwargs))
        return SimpleNamespace(content="no-payment-write region-eu-west verify-after-approval", tool_calls=[],
                               usage=TokenUsage(input_tokens=23, output_tokens=11))


def run(tmp_path, provider=None, **kwargs):
    return run_trials(tmp_path / "new-results", model="offline-test", allow_model_calls=True,
                      max_calls=3, case_ids=["task-fields"], _test_provider=provider or FakeProvider(), **kwargs)


def test_offline_plan_is_reproducible_paired_and_honest():
    one = make_plan(repeats=2)
    assert one == make_plan(repeats=2)
    assert one["max_dispatches"] == 36
    assert one["review_status"].endswith("human semantic review required")
    for case in one["cases"]:
        selected = [row for row in one["schedule"] if row["case_id"] == case["case_id"]]
        assert len(selected) == 6
        assert {row["strategy"] for row in selected} == {"full", "truncate", "studio_extractive"}
    assert one["dataset_sha256"] == make_plan(repeats=1)["dataset_sha256"]


@pytest.mark.parametrize("kwargs", [{"repeats": 0}, {"repeats": True}, {"repeats": 11},
                                  {"seed": -1}, {"seed": True}, {"case_ids": []},
                                  {"case_ids": ["not-a-case"]}, {"case_ids": ["task-fields", "task-fields"]}])
def test_invalid_plan_does_not_need_provider(kwargs):
    with pytest.raises(EvaluationError):
        make_plan(**kwargs)


@pytest.mark.parametrize("patch", [{"allow_model_calls": False}, {"model": ""}, {"model": "https://private.example"},
                                 {"max_calls": 1}, {"max_calls": True}, {"max_calls": 181},
                                 {"max_output_tokens": 0}, {"max_output_tokens": True}])
def test_permission_and_call_allowance_checked_before_files_or_provider(tmp_path, patch, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Provider initialized before validation")
    monkeypatch.setattr("co_studio.eval_provider.create_evaluation_llm", forbidden)
    args = dict(model="offline-test", allow_model_calls=True, max_calls=3, case_ids=["task-fields"])
    args.update(patch)
    with pytest.raises(EvaluationError):
        run_trials(tmp_path / "absent", **args)
    assert not (tmp_path / "absent").exists()


def test_actual_usage_missing_is_unknown_not_zero():
    assert normalize_usage(None) is None
    assert normalize_usage(TokenUsage())["input_tokens"] is None
    data = normalize_usage(TokenUsage(input_tokens=0, output_tokens=11, cost=0))
    assert data["input_tokens"] == 0
    assert data["total_tokens"] is None
    assert data["framework_estimated_cost_usd"] == 0
    assert data["provider_reported_cost_usd"] is None
    data = normalize_usage({"input_tokens": True, "output_tokens": -1, "cost": float("nan"),
                            "provider_reported_cost_usd": float("inf"), "secret": "private"})
    assert all(value is None for value in data.values())


def test_real_co_legacy_total_zero_sentinel_is_not_a_known_zero_bill():
    from connectonion.core.llm import _managed_token_usage

    # The real pinned adapter writes an explicit 0 when the server did not
    # supply a larger total; this is not a default/unset Pydantic field.
    usage = _managed_token_usage(SimpleNamespace(prompt_tokens=23, completion_tokens=11,
                                                cost_usd=.001), "offline")
    assert usage.total_tokens == 0 and "total_tokens" in usage.model_fields_set
    normalized = normalize_usage(usage)
    assert normalized["total_tokens"] is None
    assert normalized["input_tokens"] == 23 and normalized["output_tokens"] == 11
    assert normalize_usage({"total_tokens": 0})["total_tokens"] == 0


def test_mutating_provider_cannot_change_frozen_inputs_or_other_trial_inputs(tmp_path):
    before = deepcopy(context_cases())
    class Mutating(FakeProvider):
        def complete(self, messages, **kwargs):
            assert all(item.get("content") != "MUTATED" for item in messages)
            messages[0]["content"] = "MUTATED"
            return SimpleNamespace(content="Offline answer", tool_calls=[], usage=None)
    report = run(tmp_path, Mutating())
    assert context_cases() == before
    assert all(row["completed"] == 1 for row in report["summary"].values())


def test_trials_keep_source_unknown_billing_and_pending_review(tmp_path):
    before = deepcopy(context_cases())
    provider = FakeProvider()
    result = run(tmp_path, provider)
    assert len(provider.calls) == 3
    assert all(call[1] == {"max_tokens": 512} for call in provider.calls)
    assert context_cases() == before
    assert result["mode"] == "scripted_test"
    assert result["semantic_quality_verified"] is False
    assert result["real_agent_end_to_end_measured"] is False
    assert result["billing_complete"] is False
    assert result["sdk_retry_count"] is None
    records = [json.loads(line) for line in (tmp_path / "new-results/results.jsonl").read_text().splitlines()]
    assert all(row["source_unchanged"] and row["status"] == "completed" for row in records)
    assert next(row for row in records if row["strategy"] == "full")["within_local_budget"] is False
    for stats in result["summary"].values():
        assert stats["human_task_success_rate"] is None
        assert stats["small_sample_warning"] is True
        assert stats["usage"]["total_tokens"]["observed_sum"] is None
        assert stats["usage"]["input_tokens"]["observed_sum"] == 23
    review = json.loads((tmp_path / "new-results/human-review.json").read_text())
    assert review["status"] == "pending"
    assert all(row["reviewer"] is None and row["task_success"] is None for row in review["trials"])


def test_provider_failure_records_once_no_retry_or_secret(tmp_path):
    class Failed(FakeProvider):
        def complete(self, messages, **kwargs):
            self.calls.append(messages)
            raise RuntimeError("Bearer secret-DO-NOT-OUTPUT")
    provider = Failed()
    report = run(tmp_path, provider)
    assert len(provider.calls) == 3
    raw = (tmp_path / "new-results/results.jsonl").read_text()
    assert "DO-NOT-OUTPUT" not in raw
    assert all(row["failed_or_refused"] == 1 for row in report["summary"].values())
    assert all(row["p95_sample_count"] == 1 for row in report["summary"].values())


def test_interrupt_preserves_partial_results_and_does_not_dispatch_more(tmp_path):
    class Interrupted(FakeProvider):
        def complete(self, messages, **kwargs):
            self.calls.append(messages)
            raise KeyboardInterrupt()
    provider = Interrupted()
    report = run(tmp_path, provider)
    assert report["status"] == "interrupted"
    assert report["completed_trial_records"] == 1
    assert len(provider.calls) == 1
    assert (tmp_path / "new-results/aggregate.json").exists()


def test_initialization_failure_is_safe_and_has_zero_dispatches(tmp_path, monkeypatch):
    def failed(*args, **kwargs):
        raise ValueError("secret-credential")
    monkeypatch.setattr("co_studio.eval_provider.create_evaluation_llm", failed)
    with pytest.raises(EvaluationError, match="no requests dispatched"):
        run_trials(tmp_path / "new", model="explicit-model", allow_model_calls=True, max_calls=3,
                   case_ids=["task-fields"])
    assert "credential" not in (tmp_path / "new/aggregate.json").read_text()


def test_context_refusal_does_not_call_provider_for_studio(tmp_path):
    provider = FakeProvider()
    report = run_trials(tmp_path / "new", model="offline", allow_model_calls=True, max_calls=3,
                        case_ids=["oversized-current"], _test_provider=provider)
    assert report["summary"]["studio_extractive"]["dispatched"] == 0
    assert len(provider.calls) == 2


@pytest.mark.parametrize("content,tools,status", [(None, [], "unexpected_response"),
    ("x", ["not-executed"], "unexpected_response"), ("x" * 20001, [], "answer_too_large")])
def test_invalid_response_does_not_become_task_success(tmp_path, content, tools, status):
    class Bad(FakeProvider):
        def complete(self, messages, **kwargs):
            return SimpleNamespace(content=content, tool_calls=tools, usage=None)
    run(tmp_path, Bad())
    rows = [json.loads(line) for line in (tmp_path / "new-results/results.jsonl").read_text().splitlines()]
    assert all(row["status"] == status and row["answer"] is None for row in rows)


def test_output_never_overwrites_existing_directory(tmp_path):
    (tmp_path / "new-results").mkdir()
    (tmp_path / "new-results/keep.txt").write_text("keep")
    with pytest.raises(EvaluationError):
        run(tmp_path)
    assert (tmp_path / "new-results/keep.txt").read_text() == "keep"


def test_summary_failed_usage_not_dropped_and_p95_not_success_only():
    rows = [dict(strategy="full", dispatched=True, status=status, provider_elapsed_ms=latency,
                 usage=normalize_usage({"input_tokens": 9}) if latency == 1 else None,
                 answer_exact_checks_passed=False)
            for status, latency in (("completed", 1), ("provider_error", 90))]
    stats = summarize(rows)["full"]
    assert stats["provider_p95_ms_all_dispatches"] == 90
    assert stats["usage"]["input_tokens"] == dict(observed_sum=9, observed_calls=1, dispatched_calls=2, complete=False)
    assert stats["failed_or_refused"] == 1


def test_plan_cli_no_network_or_provider(capsys):
    main(["plan", "--case", "task-fields"])
    plan = json.loads(capsys.readouterr().out)
    assert plan["max_dispatches"] == 3
    assert "cases" not in plan


def test_run_cli_missing_consent_errors_before_files(tmp_path):
    with pytest.raises(SystemExit) as err:
        main(["run", "--output", str(tmp_path / "new"), "--model", "offline", "--max-calls", "18"])
    assert err.value.code == 2
    assert not (tmp_path / "new").exists()


@pytest.mark.parametrize("name", ["InsufficientCreditsError", "LLMAuthenticationError", "PaidModelRequiredError",
                                 "AuthenticationError", "PermissionDeniedError"])
def test_unrecoverable_provider_error_stops_remaining_trials_and_keeps_failure(tmp_path, name):
    class Failed(FakeProvider):
        def complete(self, messages, **kwargs):
            self.calls.append(messages)
            raise type(name, (Exception,), {})("DO-NOT-EXPOSE-private-account")
    provider = Failed()
    result = run(tmp_path, provider)
    assert len(provider.calls) == 1
    assert result["status"] == "stopped" and result["stop_reason"] == name
    assert result["unstarted_trials"] == 2 and result["completed_trial_records"] == 1
    review = json.loads((tmp_path / "new-results/human-review.json").read_text())
    assert len(review["trials"]) == 1 and review["trials"][0]["task_success"] is False
    assert review["trials"][0]["assessment"] == "automatic_failure"
    assert "DO-NOT-EXPOSE" not in (tmp_path / "new-results/results.jsonl").read_text()


def test_stopped_model_run_remains_partial_after_review_import(tmp_path):
    from co_studio.model_review import import_review
    class Failed(FakeProvider):
        def complete(self, *args, **kwargs):
            raise type("InsufficientCreditsError", (Exception,), {})("private")
    run(tmp_path, Failed())
    report = import_review(tmp_path / "new-results", tmp_path / "new-results/human-review.json", tmp_path / "reviewed")
    assert report["status"] == "partial" and not report["assessment_complete"]
    assert sum(row["automatic_failures"] for row in report["summary"].values()) == 1
    assert sum(row["unstarted_trials"] for row in report["summary"].values()) == 2
    assert not report["human_semantic_review_complete"]
