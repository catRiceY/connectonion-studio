"""V2 evaluation captures all evidence-bearing sections; no real model calls."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from co_studio.diagnosis_eval import REVIEW_PROTOCOL_V2, import_review, make_plan, run_trials
from co_studio.evidence_eval import score_evidence, score_context_evidence
from co_studio.evaluation import EvaluationError


class Store:
    def __init__(self):
        self.label = make_plan(case_ids=["prompt-zh"])["labels"]["string_literal"]
        self.hits = [{"chunk_id": "a" * 64, "doc_id": "b" * 64,
                      "source_uri": self.label["source_uri"], "version": "1.9.2",
                      "content_hash": self.label["content_hash"], "ordinal": 0,
                      "content": self.label["quote"], "line_start": 23, "line_end": 28}]
        self.context_calls = 0

    def search(self, *args, **kwargs):
        raise AssertionError("V2 must use search_context when available")

    def search_context(self, *args, **kwargs):
        self.context_calls += 1
        return {"hits": deepcopy(self.hits), "collection": "public",
                "query_plan": {"context_policy": "test-bounded"}}

    def validate_citations(self, findings, evidence):
        return {"valid": True}


def complete(*args, **kwargs):
    payload = json.loads(args[0])
    evidence_id = payload["untrusted_evidence"][0]["evidence_id"]
    return {"status": "answered", "claims": [{"text": "缺失文件的字符串作为字面量。", "evidence_ids": [evidence_id]}],
            "limitations": [{"text": "这条来源描述字符串参数。", "evidence_ids": [evidence_id]}],
            "verification_steps": [], "questions": []}


def test_v2_real_pipeline_schema_capture_all_fact_sections(tmp_path):
    store = Store()
    output = tmp_path / "v2"
    aggregate = run_trials(output, model="test", max_calls=1, allow_model_calls=True,
                           case_ids=["prompt-zh"], _test_store=store, _test_complete=complete)
    assert store.context_calls == 1
    assert aggregate["metrics"]["completed_outputs"] == 1
    result = json.loads((output / "results.jsonl").read_text())
    assert result["model_output"]["status"] == "answered"
    assert result["answer"]["protocol_version"] == "diagnostic-v2"
    assert [c["claim_id"] for c in result["claims"]] == ["claims-1", "limitations-1"]
    assert all(c["citations"][0]["quote"] in store.hits[0]["content"] for c in result["claims"])
    assert result["retrieval_context"]["evidence_recall_in_context"] == .5
    review = json.loads((output / "review-template.json").read_text())
    assert len(review["trials"][0]["claims"]) == 2
    assert review["trials"][0]["task_success"] is None


def test_run_manifest_hashes_bounded_retrieval_and_verification_implementation(tmp_path):
    import co_studio.diagnosis_eval as evaluator
    output = tmp_path / "provenance"
    run_trials(output, model="test", max_calls=1, allow_model_calls=True,
               case_ids=["prompt-zh"], _test_store=Store(), _test_complete=complete)
    manifest = json.loads((output / "manifest.json").read_text())
    root = Path(evaluator.__file__).parent
    # The fixed read-only verification map currently lives in diagnostic.py.
    for name in ("diagnosis_eval.py", "diagnostic.py", "knowledge.py", "retrieval_context.py",
                 "evidence_eval.py", "query_expansion.py", "model_eval.py"):
        assert manifest["code_sha256"][name] == hashlib.sha256((root / name).read_bytes()).hexdigest()


def test_context_private_scope_failure_is_not_sent_or_saved(tmp_path):
    store = Store()
    store.hits[0].update(source_uri="studio://agents/private/log", content="PRIVATE DATA")
    called = []
    output = tmp_path / "scope"
    result = run_trials(output, model="test", max_calls=1, allow_model_calls=True,
                        case_ids=["prompt-zh"], _test_store=store,
                        _test_complete=lambda *args, **kwargs: called.append(1))
    assert not called
    assert result["metrics"]["scope_violations"] == 1
    assert "PRIVATE DATA" not in (output / "results.jsonl").read_text()


def test_context_coverage_does_not_masquerade_as_top5():
    store = Store()
    label = store.label
    irrelevant = {**store.hits[0], "content": "unrelated"}
    hits = [deepcopy(irrelevant) for _ in range(5)] + store.hits
    labels = {"required": label}
    requirements = [["required"]]
    assert score_evidence(hits, requirements, labels)["evidence_recall_at_5"] == 0
    context = score_context_evidence(hits, requirements, labels)
    assert context["evidence_recall_in_context"] == 1
    assert context["context_passages"] == 6
    assert "evidence_recall_at_5" not in context


def test_v2_labels_are_versioned_and_new_topics_separate():
    root = Path(__file__).resolve().parents[1] / "evaluation"
    old = make_plan(dataset=root / "retrieval-evidence-v1.json", case_ids=["prompt-zh"])
    new = make_plan(case_ids=["prompt-zh"])
    assert old["dataset_sha256"] != new["dataset_sha256"]
    assert "存在则读文件" in old["cases"][0]["answer_points"][0]
    assert "且文件不存在" in new["cases"][0]["answer_points"][0]
    transfer = make_plan(dataset=root / "diagnosis-transfer-v1.json")
    assert transfer["max_dispatches"] == 8 and transfer["independent_groups"] == 4
    assert not {c["group_id"] for c in transfer["cases"]} & {c["group_id"] for c in make_plan()["cases"]}


def _joint_review(tmp_path, *, reviewed=True, status="answered"):
    store = Store()
    store.hits = [{**store.hits[0], "content": "First condition is supported here."},
                  {**store.hits[0], "chunk_id": "b" * 64, "ordinal": 1,
                   "content": "Second condition is supported in the continuation."}]
    def answer(text, **kwargs):
        units = json.loads(text)["untrusted_evidence"]
        return {"status": status,
                "claims": [{"text": "First condition and second condition.",
                            "evidence_ids": [unit["evidence_id"] for unit in units]}] if status == "answered" else [],
                "limitations": [], "verification_steps": [], "questions": []}
    directory = tmp_path / "joint-run"
    run_trials(directory, model="offline", max_calls=1, allow_model_calls=True,
               case_ids=["scope-en" if status == "refused" else "prompt-zh"],
               _test_store=store, _test_complete=answer)
    review = json.loads((directory / "review-template.json").read_text())
    review["reviewer"] = {"kind": "agent", "name": "offline-test-reviewer"}
    if reviewed:
        item = review["trials"][0]
        item.update(reviewed=True, task_success=True, behavior_correct=True, steps_safe=True,
                    uncited_claims_count=0, answer_points_met=[True] * len(item["answer_points_met"]))
        for claim in item["claims"]:
            claim.update(fully_supported=True, notes="The two exact units collectively cover both conditions.")
            for citation in claim["citations"]:
                citation.update(contributes_support=True, notes="Supports one required part, not the entire compound statement.")
    return directory, review


def _import(tmp_path, directory, review):
    path = tmp_path / "review.json"
    path.write_text(json.dumps(review))
    return import_review(directory, path, tmp_path / "reviewed")


def test_v2_review_separates_collective_entailment_from_individual_contribution(tmp_path):
    directory, review = _joint_review(tmp_path)
    assert review["protocol"] == REVIEW_PROTOCOL_V2
    assert "fully_supported" in review["trials"][0]["claims"][0]
    assert "supports_claim" not in review["trials"][0]["claims"][0]["citations"][0]
    result = _import(tmp_path, directory, review)
    assert result["reviewed_claims"] == result["fully_supported_claims"] == 1
    assert result["reviewed_citations"] == result["contributing_citations"] == 2
    assert result["collective_claim_support_rate_reviewed"] == 1
    assert result["citation_contribution_rate_reviewed"] == 1
    assert result["semantic_citation_accuracy_reviewed"] is None
    assert result["task_success_rate_reviewed"] == 1
    assert result["human_reviewed"] is False


def test_relevant_citations_do_not_imply_full_claim_support(tmp_path):
    directory, review = _joint_review(tmp_path)
    review["trials"][0]["task_success"] = False
    review["trials"][0]["claims"][0].update(fully_supported=False,
        notes="Both quotes are relevant but a further assertion in the whole claim is unsupported.")
    result = _import(tmp_path, directory, review)
    assert result["citation_contribution_rate_reviewed"] == 1
    assert result["collective_claim_support_rate_reviewed"] == 0
    assert result["task_success_rate_reviewed"] == 0


def test_irrelevant_extra_citation_is_not_hidden_by_full_claim_support(tmp_path):
    directory, review = _joint_review(tmp_path)
    review["trials"][0]["task_success"] = False
    review["trials"][0]["claims"][0]["citations"][1]["contributes_support"] = False
    result = _import(tmp_path, directory, review)
    assert result["citation_contribution_rate_reviewed"] == .5
    assert result["collective_claim_support_rate_reviewed"] == 1
    assert result["task_success_rate_reviewed"] == 0


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(protocol="generated-diagnosis-review-v1"),
    lambda r: r["trials"][0]["claims"][0].pop("fully_supported"),
    lambda r: r["trials"][0]["claims"][0].update(fully_supported=1),
    lambda r: r["trials"][0]["claims"][0].update(fully_supported=None),
    lambda r: r["trials"][0]["claims"][0].update(fully_supported=False),
    lambda r: r["trials"][0]["claims"][0].update(notes=[]),
    lambda r: r["trials"][0]["claims"][0]["citations"][0].update(supports_claim=True),
    lambda r: r["trials"][0]["claims"][0]["citations"][0].update(contributes_support=None),
    lambda r: r["trials"][0]["claims"][0]["citations"][0].update(contributes_support=False),
])
def test_v2_incomplete_or_contradictory_review_cannot_report_success(tmp_path, mutate):
    directory, review = _joint_review(tmp_path)
    mutate(review)
    with pytest.raises(EvaluationError):
        _import(tmp_path, directory, review)
    assert not (tmp_path / "reviewed").exists()


def test_collective_true_without_any_contributing_evidence_is_invalid(tmp_path):
    directory, review = _joint_review(tmp_path)
    review["trials"][0]["task_success"] = False
    for citation in review["trials"][0]["claims"][0]["citations"]:
        citation["contributes_support"] = False
    with pytest.raises(EvaluationError, match="contributing evidence"):
        _import(tmp_path, directory, review)


def test_v2_pending_semantics_remain_unknown_not_zero_or_success(tmp_path):
    directory, review = _joint_review(tmp_path, reviewed=False)
    result = _import(tmp_path, directory, review)
    assert result["reviewed_claims"] == result["reviewed_citations"] == 0
    assert result["collective_claim_support_rate_reviewed"] is None
    assert result["citation_contribution_rate_reviewed"] is None
    assert result["task_success_rate_reviewed"] is None
    assert result["unreviewed_tasks"] == 1


def test_correct_abstention_has_no_citation_accuracy_denominator(tmp_path):
    directory, review = _joint_review(tmp_path, status="refused")
    result = _import(tmp_path, directory, review)
    assert result["task_success_rate_reviewed"] == 1
    assert result["reviewed_claims"] == result["reviewed_citations"] == 0
    assert result["collective_claim_support_rate_reviewed"] is None
    assert result["citation_contribution_rate_reviewed"] is None
