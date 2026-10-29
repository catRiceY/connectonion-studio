"""Opt-in generated diagnosis trials and hash-bound claim/citation review.

The frozen retrieval cases are small engineering regression cases, not an
independent enterprise benchmark. Planning and importing reviews make no model
calls. Running uses public pinned-package evidence only, never Agent history.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import time

from .diagnostic import DEFAULT_MAX_OUTPUT_TOKENS, DiagnosticDraft, generate, validate_output_budget
from .evidence_eval import corpus_manifest, resolve_labels, score_evidence
from .evaluation import EvaluationError, _new_output, _write_json
from .knowledge import KnowledgeError, configured_store
from .model_eval import normalize_usage

PROTOCOL = "generated-diagnosis-review-v1"
DEFAULT_DATASET = Path(__file__).resolve().parents[1] / "evaluation/retrieval-evidence-v1.json"
_PROVIDER_BLOCKS = frozenset({"InsufficientCreditsError", "PaidModelRequiredError",
                              "LLMAuthenticationError", "AuthenticationError", "PermissionDeniedError"})


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _failure_details(exc):
    """Safe SDK error categories and observed usage, never exception messages."""
    completion = getattr(exc, "completion", None)
    raw_usage = getattr(completion, "usage", None)
    usage = normalize_usage(getattr(exc, "usage", None))
    if usage is None and raw_usage is not None:
        if hasattr(raw_usage, "model_dump"):
            raw_usage = raw_usage.model_dump(exclude_unset=True)
        if isinstance(raw_usage, dict):
            usage = normalize_usage({
                "input_tokens": raw_usage.get("prompt_tokens", raw_usage.get("input_tokens")),
                "output_tokens": raw_usage.get("completion_tokens", raw_usage.get("output_tokens")),
                "total_tokens": raw_usage.get("total_tokens"),
                "provider_reported_cost_usd": raw_usage.get("provider_reported_cost_usd"),
            })
    choices = getattr(completion, "choices", []) or []
    reason = getattr(choices[0], "finish_reason", None) if choices else getattr(exc, "reason", None)
    if reason not in {"length", "max_tokens", "max_output_tokens", "stop", "content_filter", "tool_calls", "function_call"}:
        reason = None
    status = getattr(exc, "status_code", None)
    return {"provider_error_type": type(exc).__name__, "provider_finish_reason": reason,
            "provider_http_status": status if type(status) is int and 100 <= status <= 599 else None,
            "usage": usage}


def make_plan(*, dataset=DEFAULT_DATASET, case_ids=None, split="all",
              max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS):
    try:
        validate_output_budget(max_output_tokens)
    except KnowledgeError:
        raise EvaluationError("Output token budget must be an integer between 256 and 8192") from None
    if split not in {"dev", "holdout", "all"}:
        raise EvaluationError("Invalid split")
    source = Path(dataset).read_text(encoding="utf-8")
    distribution = importlib.metadata.distribution("connectonion")
    try:
        labels, all_cases = resolve_labels(json.loads(source),
            Path(distribution.locate_file("connectonion/docs")), distribution.version)
    except (ValueError, KeyError, TypeError):
        raise EvaluationError("Frozen evidence labels are invalid or stale") from None
    eligible = [case for case in all_cases if split == "all" or case["split"] == split]
    if case_ids is None:
        groups = set()
        cases = []
        for case in eligible:
            if case["group_id"] not in groups:
                cases.append(case)
                groups.add(case["group_id"])
    else:
        if (not isinstance(case_ids, (list, tuple)) or not 1 <= len(case_ids) <= 16
                or any(not isinstance(value, str) for value in case_ids)
                or len(set(case_ids)) != len(case_ids)
                or set(case_ids) - {case["case_id"] for case in eligible}):
            raise EvaluationError("Select 1 to 16 unique case IDs within the requested split")
        cases = [case for case in eligible if case["case_id"] in case_ids]
    if not cases or len(cases) > 16:
        raise EvaluationError("Plan must contain 1 to 16 cases")
    return {"protocol": PROTOCOL, "dataset_id": json.loads(source)["dataset_id"],
            "dataset_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "framework_version": distribution.version, "collection": "public", "split": split,
            "cases": cases, "labels": labels, "max_dispatches": len(cases),
            "max_output_tokens_requested": max_output_tokens,
            "output_budget_note": "Fixed before dispatch and never automatically increased or retried; changing this cap defines a new experiment, not a replacement of failed trials.",
            "independent_groups": len({case["group_id"] for case in cases}),
            "labels_review_status": "agent_source_checked_pending_human_review",
            "holdout_policy": "Previously exposed holdout is fixed regression, not fresh generalization evidence; no tuning on results.",
            "cost_warning": "At most one completion dispatch per case; supported OpenAI-compatible SDK retries disabled with 45-second timeout. Other SDK transport limits are unknown; requests may still incur charges.",
            "latency_scope": "Knowledge search through generated diagnosis validation; not a multi-turn Agent task.",
            "usage_note": "Evaluation-only structured completion captures provider usage when available; null is unknown, not zero."}


class _ObservedPublicStore:
    """Capture the real search and fail closed before persisting/sending scope violations."""
    def __init__(self, store, version):
        self.store = store
        self.version = version
        self.evidence = []
        self.query_plan = None
        self.scope_violation = False
        self.search_attempted = False

    def search(self, issue, *, collection, mode, k):
        if collection != "public" or mode != "hybrid" or k != 5:
            raise EvaluationError("Only public hybrid Top-5 retrieval is permitted")
        self.search_attempted = True
        response = self.store.search(issue, collection="public", mode="hybrid", k=5)
        hits = response["hits"]
        prefix = f"studio://packages/connectonion/{self.version}/docs/"
        if (response.get("collection", "public") != "public" or len(hits) > 5
                or any(not isinstance(row, dict) or row.get("version") != self.version
                       or not row.get("source_uri", "").startswith(prefix)
                       or ".." in row.get("source_uri", "")[len(prefix):].split("/")
                       or row.get("collection", "public") != "public" for row in hits)):
            self.scope_violation = True
            raise EvaluationError("Search returned evidence outside pinned public package scope")
        self.evidence = deepcopy(hits)
        self.query_plan = deepcopy(response.get("query_plan"))
        return {**response, "hits": deepcopy(hits)}

    def validate_citations(self, findings, evidence):
        return self.store.validate_citations(findings, evidence)


def _claims(answer):
    return [{"claim_id": f"hypothesis-{index + 1}", "text": hypothesis["possible_cause"],
             "verification_step": hypothesis["verification_step"],
             "citations": [{"citation_id": f"hypothesis-{index + 1}-quote-{q + 1}",
                            "chunk_id": item["chunk_id"], "quote": item["quote"]}
                           for q, item in enumerate(hypothesis["evidence"])]}
            for index, hypothesis in enumerate((answer or {}).get("hypotheses", []))]


def summarize(rows):
    latencies = [row["end_to_end_ms"] for row in rows if row["end_to_end_ms"] is not None]
    positive = [row["retrieval"]["evidence_recall_at_5"] for row in rows
                if row["retrieval"]["evidence_recall_at_5"] is not None]
    usage = {}
    called = [row for row in rows if row["dispatched"]]
    for field in ("input_tokens", "output_tokens", "total_tokens", "framework_estimated_cost_usd", "provider_reported_cost_usd"):
        known = [row["usage"][field] for row in called
                 if row["usage"] is not None and row["usage"].get(field) is not None]
        usage[field] = {"observed_sum": sum(known) if known else None,
                        "observed_calls": len(known), "dispatched_calls": len(called),
                        "complete": bool(called) and len(known) == len(called)}
    return {"task_records": len(rows), "dispatched": len(called),
            "completed_outputs": sum(row["status"] == "completed" for row in rows),
            "failed_or_not_run": sum(row["status"] != "completed" for row in rows),
            "scope_violations": sum(row["scope_violation"] for row in rows),
            "mean_required_evidence_recall_at_5": sum(positive) / len(positive) if positive else None,
            "positive_retrieval_cases": len(positive),
            "end_to_end_p95_ms_all_attempts": sorted(latencies)[math.ceil(.95 * len(latencies)) - 1] if latencies else None,
            "latency_samples": len(latencies), "small_sample_warning": len(latencies) < 20,
            "semantic_citation_accuracy": None, "task_success_rate": None,
            "reviewed_tasks": 0, "unreviewed_tasks": len(rows), "provider_usage": usage}


def _review_template(manifest_hash, rows):
    return {"protocol": PROTOCOL, "manifest_sha256": manifest_hash, "results_sha256": _hash(rows),
            "reviewer": {"kind": None, "name": None},
            "instructions": "Judge each cited quote against its specific claim, not just exact text validity. Review the full answer/limitation for uncited assertions and safe steps. Status labels alone never prove correct behavior. Use null for pending judgments; never delete failed trials.",
            "trials": [{"trial_id": row["trial_id"], "result_sha256": _hash(row),
                        "source_sha256": row["source_sha256"], "reviewed": False,
                        "task_success": None, "behavior_correct": None, "steps_safe": None,
                        "uncited_claims_count": None, "notes": "",
                        "answer_points_met": [None] * len(row["answer_points"]),
                        "claims": [{"claim_id": claim["claim_id"],
                                    "citations": [{"citation_id": item["citation_id"], "supports_claim": None,
                                                   "notes": ""} for item in claim["citations"]]}
                                   for claim in row["claims"]]}
                       for row in rows]}


def run_trials(output, *, model, max_calls, allow_model_calls=False, dataset=DEFAULT_DATASET,
               case_ids=None, split="all", max_output_tokens=DEFAULT_MAX_OUTPUT_TOKENS,
               _test_store=None, _test_complete=None):
    if allow_model_calls is not True:
        raise EvaluationError("Explicit --allow-model-calls consent is required")
    if (not isinstance(model, str) or not model.strip() or len(model) > 200
            or any(ord(ch) < 32 for ch in model) or "://" in model):
        raise EvaluationError("An explicit model ID, not an endpoint or credential, is required")
    if (_test_store is None) != (_test_complete is None):
        raise EvaluationError("Test store and completion must be supplied together")
    plan = make_plan(dataset=dataset, case_ids=case_ids, split=split, max_output_tokens=max_output_tokens)
    if type(max_calls) is not int or not len(plan["cases"]) <= max_calls <= 16:
        raise EvaluationError("Max calls must cover the full plan and be at most 16")
    store = _test_store if _test_store is not None else configured_store()
    before = corpus_manifest(store) if _test_store is None else {"mode": "scripted_test"}
    destination = _new_output(Path(output))
    root = Path(__file__).parent
    manifest = {**plan, "model": model.strip(), "max_calls": max_calls,
                "mode": "scripted_test" if _test_complete is not None else "live_provider",
                "created_at": datetime.now(timezone.utc).isoformat(), "corpus_before": before,
                "code_sha256": {name: _file_hash(root / name) for name in
                                ("diagnosis_eval.py", "diagnostic.py", "knowledge.py", "evidence_eval.py", "query_expansion.py", "model_eval.py")},
                "evaluation_provider_sha256": _file_hash(root / "eval_provider.py") if (root / "eval_provider.py").is_file() else None}
    _write_json(destination / "manifest.json", manifest)
    rows, interrupted, provider, provider_block = [], False, None, None
    with (destination / "results.jsonl").open("x", encoding="utf-8") as handle:
        for index, case in enumerate(plan["cases"]):
            observed = _ObservedPublicStore(store, plan["framework_version"])
            row = {"trial_id": f"diagnosis-{index + 1:03d}", "case_id": case["case_id"],
                   "group_id": case["group_id"], "split": case["split"], "query": case["query"],
                   "answer_points": case["answer_points"], "expected_behavior": case["expected_behavior"],
                   "status": ("not_run_after_interrupt" if interrupted else
                              "not_run_after_provider_block" if provider_block else "error"),
                   "dispatched": False, "provider_block": deepcopy(provider_block),
                   "provider_error_type": None, "provider_finish_reason": None, "provider_http_status": None,
                   "failure_phase": None, "validation_errors": [],
                   "model_output": None, "answer": None, "claims": [], "error_type": None,
                   "started_at": None, "finished_at": None, "end_to_end_ms": None, "usage": None}
            def complete(*args, **kwargs):
                nonlocal provider
                if row["dispatched"]:
                    raise EvaluationError("Only one evaluator completion dispatch is allowed per case")
                callback = _test_complete
                if callback is None:
                    if provider is None:
                        from .eval_provider import create_evaluation_llm
                        provider = create_evaluation_llm(model.strip())
                    provider.last_structured_usage = None
                    row["dispatched"] = True
                    try:
                        raw = provider.structured_complete(
                            [{"role": "system", "content": kwargs["system_prompt"]},
                             {"role": "user", "content": args[0]}],
                            kwargs["output"], max_tokens=kwargs["max_tokens"])
                    except Exception as exc:
                        row.update(_failure_details(exc))
                        raise
                    finally:
                        observed_usage = normalize_usage(getattr(provider, "last_structured_usage", None))
                        if observed_usage is not None:
                            row["usage"] = observed_usage
                else:
                    row["dispatched"] = True
                    try:
                        raw = callback(*args, **kwargs)
                    except Exception as exc:
                        row.update(_failure_details(exc))
                        raise
                try:
                    # Only store the bounded known schema, never provider metadata.
                    row["model_output"] = DiagnosticDraft.model_validate(raw).model_dump()
                except (ValueError, TypeError):
                    row["model_output"] = None
                return raw
            if not interrupted and provider_block is None:
                row["started_at"] = datetime.now(timezone.utc).isoformat()
                start = time.perf_counter()
                try:
                    row["answer"] = generate(observed, case["query"], model=model.strip(), complete=complete,
                                             max_output_tokens=plan["max_output_tokens_requested"])
                    row["status"] = "completed"
                except KeyboardInterrupt:
                    row["status"], interrupted = "interrupted", True
                except Exception as exc:
                    row["error_type"] = type(exc).__name__
                    if row["provider_error_type"] is None:
                        row["provider_error_type"] = getattr(exc, "provider_error_type", None)
                    row["failure_phase"] = getattr(exc, "failure_phase", None)
                    row["validation_errors"] = getattr(exc, "validation_errors", [])
                finally:
                    row["end_to_end_ms"] = (time.perf_counter() - start) * 1000
                    row["finished_at"] = datetime.now(timezone.utc).isoformat()
                if row["provider_error_type"] in _PROVIDER_BLOCKS:
                    provider_block = {"trial_id": row["trial_id"], "error_type": row["provider_error_type"]}
            row.update(evidence=observed.evidence, query_plan=observed.query_plan,
                       scope_violation=observed.scope_violation, search_attempted=observed.search_attempted)
            row["retrieval"] = score_evidence(observed.evidence, case["evidence_requirements"], plan["labels"])
            row["source_sha256"] = _hash(observed.evidence)
            row["claims"] = _claims(row["answer"])
            rows.append(row)
            handle.write(_json(row) + "\n")
            handle.flush()
    try:
        after = corpus_manifest(store) if _test_store is None else before
        corpus_unchanged = before == after
    except Exception:
        after, corpus_unchanged = None, None
    aggregate = {"protocol": PROTOCOL, "mode": manifest["mode"], "manifest_sha256": _hash(manifest),
                 "results_sha256": _hash(rows),
                 "status": "interrupted" if interrupted else "blocked_by_provider" if provider_block else "finished",
                 "provider_block": provider_block,
                 "corpus_after": after, "corpus_unchanged": corpus_unchanged,
                 "latency_scope": plan["latency_scope"], "semantic_quality_verified": False,
                 "metrics": summarize(rows)}
    _write_json(destination / "aggregate.json", aggregate)
    _write_json(destination / "review-template.json", _review_template(_hash(manifest), rows))
    return aggregate


def _strict_json(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise EvaluationError("Duplicate JSON keys are not allowed")
            result[key] = value
        return result
    try:
        return json.loads(text, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError):
        raise EvaluationError("Invalid JSON review/report") from None


def _strict_load(path):
    path = Path(path).absolute()
    if path.resolve() != path or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise EvaluationError("Review/report files must be bounded regular files")
    try:
        return _strict_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        raise EvaluationError("Review/report file unavailable") from None


def import_review(run_dir, review_path, output):
    """Never edit original evidence/results or silently treat agent review as human."""
    directory = Path(run_dir)
    manifest = _strict_load(directory / "manifest.json")
    aggregate = _strict_load(directory / "aggregate.json")
    review = _strict_load(review_path)
    results_file = directory / "results.jsonl"
    if (not results_file.is_file() or results_file.absolute().resolve() != results_file.absolute()
            or results_file.stat().st_size > 32 * 1024 * 1024):
        raise EvaluationError("Invalid result stream")
    try:
        rows = [_strict_json(line) for line in results_file.read_text(encoding="utf-8").splitlines()]
    except (ValueError, OSError, UnicodeError):
        raise EvaluationError("Invalid result stream") from None
    if (not isinstance(manifest, dict) or not isinstance(aggregate, dict)
            or not isinstance(manifest.get("cases"), list) or not all(isinstance(row, dict) for row in rows)
            or manifest.get("protocol") != PROTOCOL or aggregate.get("protocol") != PROTOCOL
            or aggregate.get("manifest_sha256") != _hash(manifest)
            or aggregate.get("results_sha256") != _hash(rows)
            or len(rows) != len(manifest["cases"])):
        raise EvaluationError("Run manifest or results hash does not match")
    template = _review_template(_hash(manifest), rows)
    if (not isinstance(review, dict) or set(review) != set(template) or review["protocol"] != PROTOCOL
            or review["instructions"] != template["instructions"]
            or review["manifest_sha256"] != template["manifest_sha256"]
            or review["results_sha256"] != template["results_sha256"]
            or not isinstance(review["reviewer"], dict) or set(review["reviewer"]) != {"kind", "name"}
            or review["reviewer"]["kind"] not in {"human", "agent"}
            or not isinstance(review["reviewer"]["name"], str) or not review["reviewer"]["name"].strip()
            or len(review["reviewer"]["name"]) > 200
            or not isinstance(review["trials"], list) or len(review["trials"]) != len(rows)):
        raise EvaluationError("Review identity, manifest/results binding or complete trial list is invalid")
    reviewed, success, supported, citations, missing, behavioral = 0, 0, 0, 0, 0, 0
    for row, expected, item in zip(rows, template["trials"], review["trials"]):
        if (not isinstance(item, dict) or set(item) != set(expected)
                or any(item[key] != expected[key] for key in ("trial_id", "result_sha256", "source_sha256"))
                or type(item["reviewed"]) is not bool or not isinstance(item["notes"], str)
                or len(item["notes"]) > 10000
                or not isinstance(item["claims"], list) or len(item["claims"]) != len(expected["claims"])
                or not isinstance(item["answer_points_met"], list)
                or len(item["answer_points_met"]) != len(expected["answer_points_met"])):
            raise EvaluationError("Review trial identity or claim coverage is invalid")
        done = item["reviewed"]
        judgment_fields = ("task_success", "behavior_correct", "steps_safe")
        if any((type(item[key]) is not bool if done else item[key] is not None) for key in judgment_fields):
            raise EvaluationError("Reviewed tasks require boolean judgments; pending tasks require null")
        if (any(type(value) is not bool if done else value is not None for value in item["answer_points_met"])
                or (type(item["uncited_claims_count"]) is not int or item["uncited_claims_count"] < 0
                    if done else item["uncited_claims_count"] is not None)):
            raise EvaluationError("Answer point and missing citation judgments are invalid")
        case_supported = True
        for claim, expected_claim in zip(item["claims"], expected["claims"]):
            if (not isinstance(claim, dict) or set(claim) != {"claim_id", "citations"}
                    or claim["claim_id"] != expected_claim["claim_id"]
                    or not isinstance(claim["citations"], list)
                    or len(claim["citations"]) != len(expected_claim["citations"])):
                raise EvaluationError("Claim IDs or citation coverage changed")
            for citation, expected_citation in zip(claim["citations"], expected_claim["citations"]):
                if (not isinstance(citation, dict) or set(citation) != set(expected_citation)
                        or citation["citation_id"] != expected_citation["citation_id"]
                        or not isinstance(citation["notes"], str) or len(citation["notes"]) > 10000
                        or (type(citation["supports_claim"]) is not bool if done else citation["supports_claim"] is not None)):
                    raise EvaluationError("Citation-specific semantic judgment is invalid")
                if done:
                    citations += 1
                    supported += citation["supports_claim"]
                    case_supported &= citation["supports_claim"]
        if done:
            reviewed += 1
            missing += item["uncited_claims_count"]
            behavioral += item["behavior_correct"]
            if item["task_success"] and (row["status"] != "completed" or row["scope_violation"]
                    or not item["behavior_correct"] or not item["steps_safe"]
                    or not all(item["answer_points_met"]) or item["uncited_claims_count"] or not case_supported):
                raise EvaluationError("Successful task contradicts failure, unsafe behavior or unsupported/missing claims")
            success += item["task_success"]
    result = {"protocol": PROTOCOL, "mode": manifest["mode"], "reviewer": review["reviewer"],
              "manifest_sha256": _hash(manifest), "results_sha256": _hash(rows), "review_sha256": _hash(review),
              "reviewed_tasks": reviewed, "unreviewed_tasks": len(rows) - reviewed,
              "task_successes": success, "task_success_rate_reviewed": success / reviewed if reviewed else None,
              "confirmed_success_fraction_all_planned": success / len(rows) if rows else None,
              "behavior_correct_rate_reviewed": behavioral / reviewed if reviewed else None,
              "reviewed_citations": citations, "supported_citations": supported,
              "semantic_citation_accuracy_reviewed": supported / citations if citations else None,
              "uncited_claims_count": missing, "human_reviewed": review["reviewer"]["kind"] == "human",
              "independent_human_review_claimed": False,
              "limitations": ["Named reviewer identity is declared, not authenticated by this CLI.",
                              "Agent review is not independent human review; small related-case sample.",
                              "Pending tasks are not silently excluded: reviewed and all-planned denominators are separate."]}
    destination = _new_output(Path(output))
    _write_json(destination / "review.json", review)
    _write_json(destination / "aggregate.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "run", "import-review"))
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--split", choices=("dev", "holdout", "all"), default="all")
    parser.add_argument("--model")
    parser.add_argument("--allow-model-calls", action="store_true")
    parser.add_argument("--max-calls", type=int)
    parser.add_argument("--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--review", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "plan":
            result = make_plan(dataset=args.dataset, case_ids=args.case_ids, split=args.split,
                               max_output_tokens=args.max_output_tokens)
            result = {**result, "cases": [{key: row[key] for key in ("case_id", "group_id", "split", "query")}
                                          for row in result["cases"]]}
            result.pop("labels")
        elif args.output is None:
            parser.error("--output must specify a new directory")
        elif args.action == "run":
            result = run_trials(args.output, model=args.model, max_calls=args.max_calls,
                                allow_model_calls=args.allow_model_calls, dataset=args.dataset,
                                case_ids=args.case_ids, split=args.split,
                                max_output_tokens=args.max_output_tokens)
        else:
            if args.run_dir is None or args.review is None:
                parser.error("--run-dir and --review are required")
            result = import_review(args.run_dir, args.review, args.output)
        print(_json(result))
    except (EvaluationError, FileNotFoundError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
