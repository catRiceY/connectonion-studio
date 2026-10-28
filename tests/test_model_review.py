"""Review binding, strict scoring, honest denominators; no network/model calls."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from co_studio.evaluation import EvaluationError
from co_studio.model_eval import run_trials
from co_studio.model_review import import_review, main, make_review_template, write_review_template


class Provider:
    def complete(self, messages, **kwargs):
        return SimpleNamespace(content="Synthetic plumbing answer", tool_calls=[], usage=None)


def make_run(tmp_path, *, cases=None, provider=None, repeats=1):
    run = tmp_path / "run"
    cases = cases or ["task-fields"]
    run_trials(run, model="test", allow_model_calls=True, max_calls=3 * len(cases) * repeats,
               case_ids=cases, repeats=repeats, _test_provider=provider or Provider())
    return run


def score(review, *, kind="agent", success=True, limit=None):
    count = 0
    for row in review["trials"]:
        if row["assessment"] != "manual_required" or (limit is not None and count >= limit):
            continue
        row.update(reviewer={"kind": kind, "name": "test-reviewer", "method": "offline rubric inspection"},
                   task_success=success, unsupported_claims=False,
                   notes="Test fixture rationale; not a real task-quality judgment.")
        count += 1
    return review


def save(tmp_path, review):
    path = tmp_path / "edited-review.json"
    path.write_text(json.dumps(review), encoding="utf-8")
    return path


def apply_review(tmp_path, run, review):
    return import_review(run, save(tmp_path, review), tmp_path / "report")


def test_run_emits_hash_bound_template_and_import_is_read_only(tmp_path):
    run = make_run(tmp_path)
    before = {path.name: path.read_bytes() for path in run.iterdir()}
    review = json.loads((run / "human-review.json").read_text())
    assert review == make_review_template(run)
    report = apply_review(tmp_path, run, score(review))
    assert report["assessment_complete"] is True
    assert report["declared_reviewer_kinds"] == ["agent"]
    assert report["human_semantic_review_complete"] is False
    assert report["semantic_quality_verified"] is False
    assert report["real_provider_evidence"] is False
    assert report["reviewer_identity_authenticated"] is False
    assert report["real_agent_end_to_end_measured"] is False
    for stats in report["summary"].values():
        assert stats["agent_reviewed_task_success_rate"] == 1
        assert stats["human_task_success_rate"] is None
        assert stats["task_success_rate"] == 1
    assert before == {path.name: path.read_bytes() for path in run.iterdir()}
    assert (tmp_path / "report/review.json").exists()


def test_partial_review_reports_coverage_not_full_success(tmp_path):
    run = make_run(tmp_path, repeats=2)
    report = apply_review(tmp_path, run, score(make_review_template(run), limit=1))
    assert report["status"] == "partial" and not report["assessment_complete"]
    assert sum(s["reviewed_answers"] for s in report["summary"].values()) == 1
    assert sum(s["pending_reviews"] for s in report["summary"].values()) == 5
    for stats in report["summary"].values():
        assert stats["task_success_rate"] is None
        assert stats["human_task_success_rate"] is None
        assert stats["agent_reviewed_task_success_rate"] is None
        assert stats["attempted_success_rate_upper_bound"] == 1


def test_preparation_failure_is_in_denominator_and_not_reviewable(tmp_path):
    run = make_run(tmp_path, cases=["task-fields", "oversized-current"])
    review = score(make_review_template(run))
    automatic = [row for row in review["trials"] if row["assessment"] == "automatic_failure"]
    assert automatic and all(row["task_success"] is False and row["reviewer"] is None for row in automatic)
    report = apply_review(tmp_path, run, review)
    stats = report["summary"]["studio_extractive"]
    assert stats["attempted_trials"] == 2 and stats["automatic_failures"] == 1
    assert stats["reviewed_answers"] == 1 and stats["task_success_rate"] == .5


def test_provider_failures_are_not_hidden_or_marked_human_reviewed(tmp_path):
    class Failed(Provider):
        def complete(self, *args, **kwargs):
            raise RuntimeError("not exposed")
    run = make_run(tmp_path, provider=Failed())
    report = apply_review(tmp_path, run, make_review_template(run))
    assert report["status"] == "reviewed"
    assert report["declared_reviewer_kinds"] == []
    assert report["human_semantic_review_complete"] is False
    for stats in report["summary"].values():
        assert stats["task_success_rate"] == 0
        assert stats["automatic_failures"] == stats["attempted_trials"] == 1
        assert stats["review_coverage"] is None
        assert stats["human_task_success_rate"] is None


def test_interruption_keeps_unstarted_trials_separate_and_cannot_claim_completion(tmp_path):
    class Interrupt(Provider):
        def complete(self, *args, **kwargs):
            raise KeyboardInterrupt()
    run = make_run(tmp_path, provider=Interrupt())
    report = apply_review(tmp_path, run, make_review_template(run))
    assert report["status"] == "partial"
    assert sum(s["unstarted_trials"] for s in report["summary"].values()) == 2
    assert sum(s["attempted_trials"] for s in report["summary"].values()) == 1


@pytest.mark.parametrize("mutation", ["duplicate", "missing", "unstarted", "new_field", "answer", "expected", "status", "binding", "rubric", "repeat_type"])
def test_rejects_changed_immutable_fields_duplicate_missing_or_unknown_trials(tmp_path, mutation):
    run = make_run(tmp_path)
    review = score(make_review_template(run))
    if mutation == "duplicate":
        review["trials"][1] = deepcopy(review["trials"][0])
    elif mutation == "missing":
        review["trials"].pop()
    elif mutation == "unstarted":
        review["trials"][0]["trial_id"] = "trial-9999"
    elif mutation == "new_field":
        review["trials"][0]["adjusted_score"] = 1
    elif mutation == "answer":
        review["trials"][0]["answer"] = "Replaced answer"
    elif mutation == "expected":
        review["trials"][0]["expected_exact_fields"] = []
    elif mutation == "status":
        review["trials"][0]["trial_status"] = "provider_error"
    elif mutation == "binding":
        review["binding"]["results_sha256"] = "0" * 64
    elif mutation == "repeat_type":
        review["trials"][0]["repeat"] = True
    else:
        review["rubric"] = "Always pass"
    with pytest.raises(EvaluationError):
        apply_review(tmp_path, run, review)
    assert not (tmp_path / "report").exists()


@pytest.mark.parametrize("field,value", [("task_success", True), ("task_success", 0),
    ("unsupported_claims", False), ("reviewer", {"kind": "agent", "name": "a", "method": "b"})])
def test_failed_trials_cannot_be_manually_rescored(tmp_path, field, value):
    run = make_run(tmp_path, cases=["oversized-current"])
    review = make_review_template(run)
    row = next(row for row in review["trials"] if row["assessment"] == "automatic_failure")
    row[field] = value
    with pytest.raises(EvaluationError, match="cannot be manually rescored"):
        apply_review(tmp_path, run, review)


@pytest.mark.parametrize("field,value", [("task_success", 1), ("unsupported_claims", "false"),
    ("unsupported_claims", True), ("reviewer", "human"),
    ("reviewer", {"kind": "unknown", "name": "a", "method": "b"}),
    ("reviewer", {"kind": "human", "name": " ", "method": "b"}),
    ("reviewer", {"kind": [], "name": "a", "method": "b"}),
    ("notes", ""), ("notes", "x" * 4001)])
def test_review_requires_strict_boolean_identity_and_rationale(tmp_path, field, value):
    run = make_run(tmp_path)
    review = score(make_review_template(run))
    review["trials"][0][field] = value
    with pytest.raises(EvaluationError):
        apply_review(tmp_path, run, review)


def test_pending_score_cannot_have_half_filled_review(tmp_path):
    run = make_run(tmp_path)
    review = make_review_template(run)
    review["trials"][0]["unsupported_claims"] = False
    with pytest.raises(EvaluationError, match="Pending"):
        apply_review(tmp_path, run, review)


def test_human_review_of_scripted_provider_never_claims_real_model_quality(tmp_path):
    run = make_run(tmp_path)
    report = apply_review(tmp_path, run, score(make_review_template(run), kind="human"))
    assert report["human_semantic_review_complete"] is True
    assert report["semantic_quality_verified"] is False
    assert report["real_provider_evidence"] is False
    assert all(s["human_task_success_rate"] == 1 for s in report["summary"].values())


def test_mixed_reviewers_do_not_become_human_only_success_rate(tmp_path):
    run = make_run(tmp_path, repeats=2)
    review = score(make_review_template(run))
    for strategy in ("full", "truncate", "studio_extractive"):
        next(row for row in review["trials"] if row["strategy"] == strategy)["reviewer"]["kind"] = "human"
    report = apply_review(tmp_path, run, review)
    assert report["declared_reviewer_kinds"] == ["agent", "human"]
    assert not report["human_semantic_review_complete"]
    for stats in report["summary"].values():
        assert stats["task_success_rate"] == 1
        assert stats["human_task_success_rate"] is stats["agent_reviewed_task_success_rate"] is None


@pytest.mark.parametrize("filename", ["manifest.json", "results.jsonl"])
def test_original_artifact_change_invalidates_existing_review(tmp_path, filename):
    run = make_run(tmp_path)
    review = score(make_review_template(run))
    path = run / filename
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(EvaluationError):
        apply_review(tmp_path, run, review)


def test_result_prefix_validation_rejects_duplicate_and_unscheduled_rows(tmp_path):
    run = make_run(tmp_path)
    path = run / "results.jsonl"
    lines = path.read_text().splitlines()
    path.write_text("\n".join([lines[0], lines[0], lines[2]]) + "\n")
    with pytest.raises(EvaluationError, match="identity/order"):
        make_review_template(run)


def test_manifest_expected_fields_cannot_change_without_dataset_hash(tmp_path):
    run = make_run(tmp_path)
    path = run / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["cases"][0]["expected"] = []
    path.write_text(json.dumps(manifest))
    with pytest.raises(EvaluationError, match="dataset hash"):
        make_review_template(run)


def test_review_rejects_duplicate_json_keys_and_nan(tmp_path):
    run = make_run(tmp_path)
    path = tmp_path / "bad.json"
    for data in ('{"trials": [], "trials": []}', '{"score": NaN}'):
        path.write_text(data)
        with pytest.raises(EvaluationError):
            import_review(run, path, tmp_path / "absent")
    assert not (tmp_path / "absent").exists()


def test_template_and_report_never_overwrite_original_or_prior_results(tmp_path):
    run = make_run(tmp_path)
    with pytest.raises(EvaluationError, match="new file"):
        write_review_template(run, run / "manifest.json")
    path = save(tmp_path, score(make_review_template(run)))
    for target in (run, run / "nested"):
        with pytest.raises(EvaluationError, match="outside"):
            import_review(run, path, target)
    import_review(run, path, tmp_path / "report")
    with pytest.raises(EvaluationError, match="new directory"):
        import_review(run, path, tmp_path / "report")


def test_symlink_input_and_output_rejected(tmp_path):
    run = make_run(tmp_path)
    linked = tmp_path / "linked"
    linked.symlink_to(run, target_is_directory=True)
    with pytest.raises(EvaluationError, match="Linked"):
        make_review_template(linked)
    target = tmp_path / "target"
    target.symlink_to(run / "manifest.json")
    with pytest.raises(EvaluationError, match="Linked"):
        write_review_template(run, target)


def test_cli_is_offline_and_imports_review(tmp_path, monkeypatch, capsys):
    run = make_run(tmp_path)
    monkeypatch.setattr("co_studio.eval_provider.create_evaluation_llm", lambda *args, **kwargs: pytest.fail("No model call"))
    main(["template", "--run", str(run), "--output", str(tmp_path / "new-template.json")])
    assert json.loads(capsys.readouterr().out)["model_calls"] == 0
    path = save(tmp_path, score(make_review_template(run)))
    main(["import", "--run", str(run), "--review", str(path), "--output", str(tmp_path / "report")])
    assert json.loads(capsys.readouterr().out)["status"] == "reviewed"


def test_cli_missing_review_safe_failure(tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(["import", "--run", str(tmp_path / "absent"), "--output", str(tmp_path / "new")])
    assert exc.value.code == 2
