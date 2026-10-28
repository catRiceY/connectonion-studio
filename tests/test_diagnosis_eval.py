"""Offline contracts only; injected completions are not real-model quality evidence."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from co_studio.diagnosis_eval import import_review, make_plan, run_trials
from co_studio.evaluation import EvaluationError


class Store:
    def __init__(self):
        plan = make_plan(case_ids=["prompt-zh"])
        label = plan["labels"]["string_literal"]
        self.hits = [{"chunk_id": "a" * 64, "source_uri": label["source_uri"],
                      "version": label["version"], "content_hash": label["content_hash"],
                      "content": label["quote"], "line_start": 23, "line_end": 28}]
        self.calls = []
        self.citations_valid = True

    def search(self, issue, **kwargs):
        self.calls.append((issue, kwargs))
        return {"hits": deepcopy(self.hits), "collection": "public"}

    def validate_citations(self, findings, evidence):
        return {"valid": self.citations_valid}


def draft():
    return {"status": "hypotheses", "hypotheses": [{"possible_cause": "不存在的字符串路径被当作文字。",
            "evidence": [{"chunk_id": "a" * 64, "quote": "# ✅ Uses as literal text if file doesn't exist"}],
            "verification_step": "只读检查参数类型和路径。"}], "limitation": "仅基于当前文档。"}


def run(tmp_path, *, store=None, complete=None, case_ids=None):
    destination = tmp_path / "run"
    aggregate = run_trials(destination, model="test-only", max_calls=16,
                           allow_model_calls=True, _test_store=store or Store(),
                           _test_complete=complete or (lambda *a, **kw: draft()),
                           case_ids=case_ids or ["prompt-zh"])
    return destination, aggregate


def read(path):
    return json.loads(path.read_text())


def reviewed_template(directory, *, kind="agent", success=True):
    review = read(directory / "review-template.json")
    review["reviewer"] = {"kind": kind, "name": "test reviewer"}
    for item in review["trials"]:
        item.update(reviewed=True, task_success=success, behavior_correct=True,
                    steps_safe=True, uncited_claims_count=0)
        item["answer_points_met"] = [True] * len(item["answer_points_met"])
        for claim in item["claims"]:
            for citation in claim["citations"]:
                citation["supports_claim"] = True
    return review


def write_review(tmp_path, review):
    path = tmp_path / "edited-review.json"
    path.write_text(json.dumps(review))
    return path


def test_plan_groups_and_shared_translations():
    plan = make_plan()
    assert plan["max_dispatches"] == plan["independent_groups"] == 8
    both = make_plan(case_ids=["prompt-zh", "prompt-en"])
    assert both["max_dispatches"] == 2 and both["independent_groups"] == 1
    assert make_plan(split="dev")["max_dispatches"] == 3
    assert "fixed regression" in plan["holdout_policy"]


@pytest.mark.parametrize("ids,split", [([], "all"), (["prompt-zh"] * 2, "all"),
    (["not-a-case"], "all"), (["cursor-en"], "dev"), ([12], "all")])
def test_invalid_selection(ids, split):
    with pytest.raises(EvaluationError):
        make_plan(case_ids=ids, split=split)


@pytest.mark.parametrize("changes", [{"allow_model_calls": False}, {"model": ""},
    {"model": "https://credential"}, {"max_calls": 0}, {"max_calls": True}, {"max_calls": 17}])
def test_consent_and_allowance_before_output_or_calls(tmp_path, changes):
    db = Store()
    options = dict(model="test", max_calls=1, allow_model_calls=True,
                   _test_store=db, _test_complete=lambda *a, **k: draft(), case_ids=["prompt-zh"])
    options.update(changes)
    with pytest.raises(EvaluationError):
        run_trials(tmp_path / "out", **options)
    assert not (tmp_path / "out").exists() and not db.calls


def test_trial_runs_existing_pipeline_tracks_evidence_without_claiming_semantics(tmp_path):
    db = Store()
    directory, result = run(tmp_path, store=db)
    assert db.calls[0][1] == {"collection": "public", "mode": "hybrid", "k": 5}
    assert result["metrics"]["dispatched"] == 1
    assert result["metrics"]["mean_required_evidence_recall_at_5"] == .5
    assert result["metrics"]["semantic_citation_accuracy"] is None
    assert result["metrics"]["task_success_rate"] is None
    assert result["metrics"]["provider_usage"]["input_tokens"]["observed_sum"] is None
    row = json.loads((directory / "results.jsonl").read_text())
    assert row["started_at"] <= row["finished_at"] and row["end_to_end_ms"] >= 0
    assert row["claims"][0]["citations"][0]["quote"] in row["evidence"][0]["content"]
    assert read(directory / "manifest.json")["mode"] == "scripted_test"


def test_empty_evidence_preserves_abstention_but_does_not_grade_it(tmp_path):
    db = Store()
    db.hits = []
    def not_called(*a, **kw):
        pytest.fail("Model must not be called")
    directory, result = run(tmp_path, store=db, complete=not_called)
    assert result["metrics"]["dispatched"] == 0
    assert result["metrics"]["task_success_rate"] is None
    row = json.loads((directory / "results.jsonl").read_text())
    assert row["answer"]["status"] == "insufficient_evidence"
    assert not row["claims"]


@pytest.mark.parametrize("changes", [
    {"source_uri": "studio://agents/private/session"}, {"version": "2.0"},
    {"collection": "agent-private"},
    {"source_uri": "studio://packages/connectonion/1.9.2/docs/../private"}])
def test_scope_failure_cannot_send_or_persist_private_evidence(tmp_path, changes):
    db = Store()
    db.hits[0].update(changes, content="PRIVATE SECRET")
    called = []
    directory, result = run(tmp_path, store=db, complete=lambda *a, **k: called.append(a))
    assert not called
    assert result["metrics"]["scope_violations"] == 1
    text = (directory / "results.jsonl").read_text()
    assert "PRIVATE SECRET" not in text and "private/session" not in text
    assert json.loads(text)["evidence"] == []


def test_model_failure_is_in_denominator_without_sensitive_error(tmp_path):
    def fail(*a, **kw):
        raise RuntimeError("API_KEY=do-not-save")
    directory, result = run(tmp_path, complete=fail)
    assert result["metrics"]["failed_or_not_run"] == 1
    assert result["metrics"]["dispatched"] == result["metrics"]["latency_samples"] == 1
    assert "do-not-save" not in (directory / "results.jsonl").read_text()


def test_invalid_citation_withheld_keeps_bounded_draft_for_failure_review(tmp_path):
    db = Store()
    db.citations_valid = False
    directory, result = run(tmp_path, store=db)
    row = json.loads((directory / "results.jsonl").read_text())
    assert row["answer"] is None and row["model_output"]["status"] == "hypotheses"
    assert row["status"] == "error" and not row["claims"]


def test_interrupt_records_every_selected_case_without_further_dispatch(tmp_path):
    def interrupt(*a, **k):
        raise KeyboardInterrupt
    directory, result = run(tmp_path, complete=interrupt, case_ids=["prompt-zh", "prompt-en"])
    rows = [json.loads(line) for line in (directory / "results.jsonl").read_text().splitlines()]
    assert [row["status"] for row in rows] == ["interrupted", "not_run_after_interrupt"]
    assert result["metrics"]["dispatched"] == 1
    assert result["metrics"]["task_records"] == 2
    assert result["metrics"]["latency_samples"] == 1


def test_run_output_never_overwritten(tmp_path):
    directory, _ = run(tmp_path)
    before = (directory / "results.jsonl").read_bytes()
    with pytest.raises(EvaluationError):
        run(tmp_path)
    assert (directory / "results.jsonl").read_bytes() == before


@pytest.mark.parametrize("kind", ["human", "agent"])
def test_valid_review_separate_identity_outputs_and_unknown_denominators(tmp_path, kind):
    directory, _ = run(tmp_path)
    review = reviewed_template(directory, kind=kind)
    original = (directory / "results.jsonl").read_bytes()
    result = import_review(directory, write_review(tmp_path, review), tmp_path / "reviewed")
    assert result["semantic_citation_accuracy_reviewed"] == 1
    assert result["task_success_rate_reviewed"] == 1
    assert result["human_reviewed"] is (kind == "human")
    assert not result["independent_human_review_claimed"]
    assert (directory / "results.jsonl").read_bytes() == original


def test_pending_review_not_counted_as_zero_or_full_accuracy(tmp_path):
    directory, _ = run(tmp_path)
    review = read(directory / "review-template.json")
    review["reviewer"] = {"kind": "agent", "name": "pending-reviewer"}
    result = import_review(directory, write_review(tmp_path, review), tmp_path / "reviewed")
    assert result["unreviewed_tasks"] == 1 and result["reviewed_tasks"] == 0
    assert result["task_success_rate_reviewed"] is None
    assert result["semantic_citation_accuracy_reviewed"] is None


def test_false_citation_support_is_kept_in_denominator(tmp_path):
    directory, _ = run(tmp_path)
    review = reviewed_template(directory, success=False)
    review["trials"][0]["claims"][0]["citations"][0]["supports_claim"] = False
    result = import_review(directory, write_review(tmp_path, review), tmp_path / "reviewed")
    assert result["reviewed_citations"] == 1 and result["supported_citations"] == 0
    assert result["semantic_citation_accuracy_reviewed"] == result["task_success_rate_reviewed"] == 0


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(manifest_sha256="bad"),
    lambda r: r.update(results_sha256="bad"),
    lambda r: r["reviewer"].update(kind="independent"),
    lambda r: r.update(trials=[]),
    lambda r: r["trials"][0].update(source_sha256="bad"),
    lambda r: r["trials"][0].update(result_sha256="bad"),
    lambda r: r["trials"][0].update(trial_id="another"),
    lambda r: r["trials"][0].update(claims=[]),
    lambda r: r["trials"][0].update(task_success=1),
    lambda r: r["trials"][0].update(uncited_claims_count=True),
    lambda r: r["trials"][0].update(uncited_claims_count=1),
    lambda r: r["trials"][0].update(steps_safe=False),
    lambda r: r["trials"][0].update(behavior_correct=False),
    lambda r: r["trials"][0].update(answer_points_met=[False, True]),
    lambda r: r["trials"][0]["claims"][0].update(citations=[]),
    lambda r: r["trials"][0]["claims"][0]["citations"][0].update(supports_claim=None),
    lambda r: r["trials"][0]["claims"][0]["citations"][0].update(supports_claim=False),
])
def test_review_invalid_or_contradictory_cannot_write_aggregate(tmp_path, mutate):
    directory, _ = run(tmp_path)
    review = reviewed_template(directory)
    mutate(review)
    with pytest.raises(EvaluationError):
        import_review(directory, write_review(tmp_path, review), tmp_path / "reviewed")
    assert not (tmp_path / "reviewed").exists()


def test_tampering_saved_evidence_or_manifest_rejected(tmp_path):
    directory, _ = run(tmp_path)
    review = reviewed_template(directory)
    path = directory / "results.jsonl"
    row = json.loads(path.read_text())
    row["evidence"][0]["content"] = "altered"
    path.write_text(json.dumps(row) + "\n")
    with pytest.raises(EvaluationError, match="hash"):
        import_review(directory, write_review(tmp_path, review), tmp_path / "reviewed")


def test_error_cannot_be_reviewed_success(tmp_path):
    def fail(*a, **kw):
        raise RuntimeError
    directory, _ = run(tmp_path, complete=fail)
    review = reviewed_template(directory)
    with pytest.raises(EvaluationError, match="contradicts"):
        import_review(directory, write_review(tmp_path, review), tmp_path / "reviewed")


def test_duplicate_review_keys_fail(tmp_path):
    directory, _ = run(tmp_path)
    path = tmp_path / "review.json"
    path.write_text('{"protocol": "first", "protocol": "second"}')
    with pytest.raises(EvaluationError, match="Duplicate"):
        import_review(directory, path, tmp_path / "reviewed")


@pytest.mark.parametrize("value", [None, [], "unexpected", {"unexpected": "value"}])
def test_nonobject_or_wrong_review_schema_fails_cleanly(tmp_path, value):
    directory, _ = run(tmp_path)
    with pytest.raises(EvaluationError):
        import_review(directory, write_review(tmp_path, value), tmp_path / "reviewed")


def test_no_citation_answer_has_no_automatic_semantic_score(tmp_path):
    directory, _ = run(tmp_path, complete=lambda *a, **kw: {
        "status": "conflicting_evidence", "hypotheses": [], "limitation": "两份文档冲突，需核对版本。"})
    review = reviewed_template(directory, success=False)
    result = import_review(directory, write_review(tmp_path, review), tmp_path / "reviewed")
    assert result["reviewed_citations"] == 0
    assert result["semantic_citation_accuracy_reviewed"] is None
    assert result["task_success_rate_reviewed"] == 0


def test_failure_can_be_reviewed_but_never_disappear(tmp_path):
    directory, _ = run(tmp_path, complete=lambda *a, **kw: {"invalid": "schema"})
    review = reviewed_template(directory, success=False)
    review["trials"][0]["behavior_correct"] = False
    review["trials"][0]["answer_points_met"] = [False, False]
    result = import_review(directory, write_review(tmp_path, review), tmp_path / "reviewed")
    assert result["reviewed_tasks"] == 1 and result["task_successes"] == 0
    assert result["confirmed_success_fraction_all_planned"] == 0


def test_review_export_destination_not_overwritten(tmp_path):
    directory, _ = run(tmp_path)
    review = write_review(tmp_path, reviewed_template(directory))
    destination = tmp_path / "reviewed"
    import_review(directory, review, destination)
    before = (destination / "aggregate.json").read_bytes()
    with pytest.raises(EvaluationError):
        import_review(directory, review, destination)
    assert (destination / "aggregate.json").read_bytes() == before


def test_live_wrapper_captures_real_interface_usage_and_clears_previous_case(tmp_path, monkeypatch):
    import co_studio.diagnosis_eval as module
    calls = []
    provider = SimpleNamespace(last_structured_usage={"input_tokens": 999})
    def structured(messages, schema, **kwargs):
        assert provider.last_structured_usage is None
        assert [item["role"] for item in messages] == ["system", "user"]
        assert kwargs == {"max_tokens": 2048}
        calls.append(messages)
        if len(calls) == 1:
            provider.last_structured_usage = {"input_tokens": 12, "output_tokens": 7}
        return schema.model_validate(draft())
    provider.structured_complete = structured
    fake_module = SimpleNamespace(create_evaluation_llm=lambda model: provider)
    monkeypatch.setitem(sys.modules, "co_studio.eval_provider", fake_module)
    monkeypatch.setattr(module, "configured_store", Store)
    monkeypatch.setattr(module, "corpus_manifest", lambda store: {"same": True})
    output = tmp_path / "provider-interface"
    result = run_trials(output, model="offline-monkeypatched-provider", max_calls=2,
                        allow_model_calls=True, case_ids=["prompt-zh", "prompt-en"])
    assert len(calls) == 2
    assert result["metrics"]["provider_usage"]["input_tokens"] == {
        "observed_sum": 12, "observed_calls": 1, "dispatched_calls": 2, "complete": False}
    rows = [json.loads(line) for line in (output / "results.jsonl").read_text().splitlines()]
    assert rows[0]["usage"]["input_tokens"] == 12 and rows[1]["usage"] is None


def test_provider_init_failure_is_not_dispatched(tmp_path, monkeypatch):
    import co_studio.diagnosis_eval as module
    def fail(model):
        raise RuntimeError("secret configuration")
    monkeypatch.setitem(sys.modules, "co_studio.eval_provider", SimpleNamespace(create_evaluation_llm=fail))
    monkeypatch.setattr(module, "configured_store", Store)
    monkeypatch.setattr(module, "corpus_manifest", lambda store: {"same": True})
    result = run_trials(tmp_path / "init-failed", model="test", max_calls=1,
                        allow_model_calls=True, case_ids=["prompt-zh"])
    assert result["metrics"]["dispatched"] == 0
    assert result["metrics"]["failed_or_not_run"] == 1


def test_length_error_preserves_usage_without_logging_partial_or_secret_output(tmp_path):
    from openai import LengthFinishReasonError
    from openai.types.chat import ChatCompletion
    response = ChatCompletion.model_validate({
        "id": "offline", "created": 0, "model": "test", "object": "chat.completion",
        "choices": [{"index": 0, "finish_reason": "length", "message": {
            "role": "assistant", "content": "secret-partial-model-text"}}],
        "usage": {"prompt_tokens": 30, "completion_tokens": 2048, "total_tokens": 2078}})
    def fail(*args, **kwargs):
        raise LengthFinishReasonError(completion=response)
    directory, result = run(tmp_path, complete=fail)
    content = (directory / "results.jsonl").read_text()
    row = json.loads(content)
    assert row["provider_error_type"] == "LengthFinishReasonError"
    assert row["provider_finish_reason"] == "length"
    assert row["usage"]["output_tokens"] == 2048
    assert "secret-partial-model-text" not in content
    assert result["metrics"]["failed_or_not_run"] == 1
    assert result["metrics"]["provider_usage"]["input_tokens"]["observed_sum"] == 30


def test_safe_exception_metadata_never_records_message_body_or_url(tmp_path):
    class ErrorWithSecrets(Exception):
        status_code = 429
        body = {"secret": "DO NOT EXPOSE"}
    def fail(*args, **kwargs):
        raise ErrorWithSecrets("token=DO NOT EXPOSE")
    directory, _ = run(tmp_path, complete=fail)
    content = (directory / "results.jsonl").read_text()
    assert "DO NOT EXPOSE" not in content
    row = json.loads(content)
    assert row["provider_error_type"] == "ErrorWithSecrets"
    assert row["provider_http_status"] == 429


@pytest.mark.parametrize("error_name", ["InsufficientCreditsError", "PaidModelRequiredError",
    "LLMAuthenticationError", "AuthenticationError", "PermissionDeniedError"])
def test_nonrecoverable_provider_failure_stops_dispatches_without_losing_cases(tmp_path, error_name):
    calls = []
    failure = type(error_name, (Exception,), {})
    def fail(*args, **kwargs):
        calls.append(1)
        raise failure("secret detail")
    db = Store()
    directory, result = run(tmp_path, store=db, complete=fail,
                            case_ids=["prompt-zh", "prompt-en", "unknown-en"])
    assert calls == [1] and len(db.calls) == 1
    assert result["status"] == "blocked_by_provider"
    assert result["metrics"]["dispatched"] == 1
    assert result["metrics"]["task_records"] == result["metrics"]["failed_or_not_run"] == 3
    rows = [json.loads(line) for line in (directory / "results.jsonl").read_text().splitlines()]
    assert rows[0]["status"] == "error"
    assert all(row["status"] == "not_run_after_provider_block" for row in rows[1:])
    assert all(row["provider_block"] == {"trial_id": "diagnosis-001", "error_type": error_name}
               for row in rows[1:])
    assert all(row["end_to_end_ms"] is None for row in rows[1:])
    assert len(read(directory / "review-template.json")["trials"]) == 3


def test_ordinary_per_case_failure_does_not_automatically_abort_other_cases(tmp_path):
    calls = []
    def fail_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("local schema case failure")
        return draft()
    _, result = run(tmp_path, complete=fail_once, case_ids=["prompt-zh", "prompt-en"])
    assert len(calls) == 2 and result["metrics"]["completed_outputs"] == 1
    assert result["status"] == "finished"
