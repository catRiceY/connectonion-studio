"""Offline, hash-bound semantic review of saved paired context model trials.

Reviews are declarations by named human/agent reviewers, not authenticated
identities or an automatic truth oracle. Original run artifacts are never edited.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import stat

from .evaluation import EvaluationError, _new_output, _write_json

REVIEW_PROTOCOL = "paired-context-review-v1"
MAX_FILE_BYTES = 16 * 1024 * 1024
STRATEGIES = ("full", "truncate", "studio_extractive")
STATUSES = {"completed", "preparation_refused", "preparation_error", "provider_error",
            "unexpected_response", "answer_too_large", "interrupted"}
EDITABLE_FIELDS = {"reviewer", "task_success", "unsupported_claims", "notes"}
RUBRIC = ("Read the original messages and final request in manifest.json. Success requires "
          "a correct answer to that request, retention of relevant latest constraints/evidence, "
          "and no invented facts or successful tool execution. Exact substring matches alone "
          "are insufficient. Mark missing evidence or incorrect/stale constraints as failure.")


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvaluationError("Duplicate JSON keys are not allowed")
        result[key] = value
    return result


def _parse(data):
    try:
        return json.loads(data, object_pairs_hook=_unique_object,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise EvaluationError("Invalid review/run JSON") from None


def _read(path):
    path = Path(path).absolute()
    if path.resolve() != path:
        raise EvaluationError("Linked review/run paths are not supported")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                raise EvaluationError("Review/run artifact must be a bounded regular file")
            data = handle.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise EvaluationError("Review/run artifact is too large")
            return data
    except OSError:
        raise EvaluationError("Cannot read review/run artifact") from None


def _require(condition, message):
    if not condition:
        raise EvaluationError(message)


def _read_run(run):
    manifest_bytes = _read(Path(run) / "manifest.json")
    result_bytes = _read(Path(run) / "results.jsonl")
    manifest = _parse(manifest_bytes)
    _require(isinstance(manifest, dict)
             and manifest.get("protocol") == "paired-context-model-trials-v1"
             and isinstance(manifest.get("mode"), str)
             and manifest["mode"] in {"live_provider", "scripted_test"}, "Unsupported trial manifest")
    cases = manifest.get("cases")
    schedule = manifest.get("schedule")
    _require(isinstance(cases, list) and 1 <= len(cases) <= 6
             and isinstance(schedule, list) and 1 <= len(schedule) <= 180, "Invalid manifest cases/schedule")
    case_map = {}
    for case in cases:
        _require(isinstance(case, dict) and isinstance(case.get("case_id"), str)
                 and case["case_id"] not in case_map and isinstance(case.get("messages"), list)
                 and isinstance(case.get("expected"), list)
                 and all(isinstance(value, str) for value in case["expected"]), "Invalid/duplicate manifest case")
        case_map[case["case_id"]] = case
    # Same canonical serialization used by the producer, not a mutable aggregate.
    dataset_hash = _hash(json.dumps(cases, ensure_ascii=False, sort_keys=True, allow_nan=False).encode())
    _require(manifest.get("dataset_sha256") == dataset_hash, "Manifest dataset hash mismatch")
    combinations = set()
    for item in schedule:
        _require(isinstance(item, dict) and set(item) == {"case_id", "repeat", "strategy"}
                 and isinstance(item["case_id"], str) and item["case_id"] in case_map
                 and isinstance(item["strategy"], str) and item["strategy"] in STRATEGIES
                 and type(item["repeat"]) is int and 1 <= item["repeat"] <= 10, "Invalid scheduled trial")
        key = (item["case_id"], item["repeat"], item["strategy"])
        _require(key not in combinations, "Duplicate scheduled trial")
        combinations.add(key)
    _require(all((case, repeat, strategy) in combinations for case, repeat, _ in combinations
                 for strategy in STRATEGIES), "Incomplete paired schedule")
    _require(manifest.get("max_dispatches") == len(schedule), "Dispatch count does not match schedule")
    lines = result_bytes.splitlines()
    _require(0 < len(lines) <= len(schedule) and all(line.strip() for line in lines),
             "Results must contain a nonempty started prefix of the schedule")
    rows = []
    for index, line in enumerate(lines):
        row = _parse(line)
        item = schedule[index]
        _require(isinstance(row, dict) and row.get("trial_id") == f"trial-{index + 1:04d}"
                 and type(row.get("repeat")) is int
                 and all(row.get(key) == value for key, value in item.items()),
                 "Result trial identity/order does not match schedule")
        _require(isinstance(row.get("status"), str) and row["status"] in STATUSES
                 and type(row.get("dispatched")) is bool
                 and row.get("source_unchanged") is True, "Invalid trial status or mutated source")
        _require({"provider_elapsed_ms", "usage", "answer_exact_checks_passed"} <= set(row),
                 "Missing trial metric fields")
        elapsed = row["provider_elapsed_ms"]
        _require(elapsed is None or (type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0),
                 "Invalid elapsed-time metric")
        _require(row["usage"] is None or isinstance(row["usage"], dict), "Invalid usage metrics")
        if row["usage"] is not None:
            for name in ("input_tokens", "output_tokens", "total_tokens", "cached_tokens", "cache_write_tokens",
                         "framework_estimated_cost_usd", "provider_reported_cost_usd"):
                value = row["usage"].get(name)
                _require(value is None or (type(value) in (int, float) and math.isfinite(value) and value >= 0),
                         "Invalid usage metric value")
        if row["status"] in {"preparation_refused", "preparation_error"}:
            _require(not row["dispatched"], "Preparation failure cannot be a dispatched completion")
        elif row["status"] != "interrupted":
            _require(row["dispatched"], "Provider outcome requires a dispatched trial")
        _require((row["status"] == "completed" and isinstance(row.get("answer"), str)
                  and len(row["answer"]) <= 20000)
                 or (row["status"] != "completed" and row.get("answer") is None),
                 "Trial answer does not match outcome")
        rows.append(row)
    binding = {"manifest_sha256": _hash(manifest_bytes), "results_sha256": _hash(result_bytes)}
    return manifest, rows, case_map, binding


def make_review_template(run):
    manifest, rows, cases, binding = _read_run(run)
    trials = []
    for row in rows:
        trials.append({"trial_id": row["trial_id"], "case_id": row["case_id"],
                       "strategy": row["strategy"], "repeat": row["repeat"],
                       "trial_status": row["status"], "answer": row["answer"],
                       "expected_exact_fields": cases[row["case_id"]]["expected"],
                       "assessment": "manual_required" if row["status"] == "completed" else "automatic_failure",
                       "reviewer": None, "task_success": None if row["status"] == "completed" else False,
                       "unsupported_claims": None, "notes": ""})
    return {"protocol": REVIEW_PROTOCOL, "status": "pending", "binding": binding,
            "run_mode": manifest["mode"], "rubric": RUBRIC, "trials": trials}


def write_review_template(run, output):
    result = make_review_template(run)
    target = Path(output).absolute()
    _require(target.resolve() == target, "Linked review output paths are not supported")
    try:
        _write_json(target, result)
    except OSError:
        raise EvaluationError("Review template output must be a new file") from None
    return result


def _validate_review(review, template):
    _require(isinstance(review, dict) and set(review) == set(template), "Invalid review envelope fields")
    _require(all(review[key] == template[key] for key in template if key != "trials"),
             "Review manifest/results binding or rubric was changed")
    trials = review["trials"]
    _require(isinstance(trials, list) and len(trials) == len(template["trials"]),
             "Review must retain every started trial, including pending/failed rows")
    expected = {row["trial_id"]: row for row in template["trials"]}
    seen = set()
    for row in trials:
        _require(isinstance(row, dict) and isinstance(row.get("trial_id"), str)
                 and row["trial_id"] in expected and row["trial_id"] not in seen,
                 "Duplicate, unknown, or unstarted review trial")
        seen.add(row["trial_id"])
        source = expected[row["trial_id"]]
        _require(set(row) == set(source) and type(row.get("repeat")) is int and all(row[key] == source[key]
                 for key in source if key not in EDITABLE_FIELDS), "Immutable review trial fields were changed")
        _require(isinstance(row["notes"], str) and len(row["notes"]) <= 4000, "Invalid review notes")
        if source["assessment"] == "automatic_failure":
            _require(row["task_success"] is False and row["unsupported_claims"] is None
                     and row["reviewer"] is None, "Failed/refused trials cannot be manually rescored")
            continue
        reviewer = row["reviewer"]
        if row["task_success"] is None:
            _require(reviewer is None and row["unsupported_claims"] is None,
                     "Pending review must not contain a partial score/identity")
            continue
        _require(type(row["task_success"]) is bool and type(row["unsupported_claims"]) is bool,
                 "Semantic review outcomes must be booleans")
        _require(isinstance(reviewer, dict) and set(reviewer) == {"kind", "name", "method"}
                 and isinstance(reviewer["kind"], str) and reviewer["kind"] in {"human", "agent"}
                 and all(isinstance(reviewer[key], str) and 0 < len(reviewer[key].strip()) <= 300
                         for key in ("name", "method")), "Provide declared human/agent reviewer name and method")
        _require(bool(row["notes"].strip()), "Reviewed answers require a rationale in notes")
        _require(not (row["task_success"] and row["unsupported_claims"]),
                 "Unsupported claims cannot receive task success")


def _review_summary(manifest, rows, review):
    from .model_eval import summarize

    result = summarize(rows)
    for strategy in STRATEGIES:
        selected = [row for row in review["trials"] if row["strategy"] == strategy]
        eligible = [row for row in selected if row["assessment"] == "manual_required"]
        scored = [row for row in eligible if row["reviewer"] is not None]
        pending = len(eligible) - len(scored)
        planned = sum(item["strategy"] == strategy for item in manifest["schedule"])
        attempted = len(selected)
        successes = sum(row["task_success"] is True for row in scored)
        complete = attempted == planned and pending == 0
        rate = successes / attempted if complete and attempted else None
        kinds = sorted({row["reviewer"]["kind"] for row in scored})
        result[strategy].update({"planned_trials": planned, "attempted_trials": attempted,
            "unstarted_trials": planned - attempted, "automatic_failures": attempted - len(eligible),
            "reviewable_answers": len(eligible), "reviewed_answers": len(scored), "pending_reviews": pending,
            "review_coverage": len(scored) / len(eligible) if eligible else None,
            "declared_reviewer_kinds": kinds, "assessment_complete": complete,
            "task_successes": successes, "task_success_rate": rate,
            "human_task_success_rate": rate if kinds == ["human"] else None,
            "agent_reviewed_task_success_rate": rate if kinds == ["agent"] else None,
            "attempted_success_rate_lower_bound": successes / attempted if attempted else None,
            "attempted_success_rate_upper_bound": (successes + pending) / attempted if attempted else None})
    return result


def import_review(run, review_file, output):
    # All input validation happens before output creation. Never write to the
    # original run directory, even when the caller chooses a nested output.
    root = Path(run).resolve()
    destination = Path(output).absolute()
    _require(not destination.resolve().is_relative_to(root), "Review report must be outside the original run")
    manifest, rows, _, binding = _read_run(run)
    template = make_review_template(run)
    _require(template["binding"] == binding, "Run changed while loading review")
    review_bytes = _read(review_file)
    review = _parse(review_bytes)
    _validate_review(review, template)
    summary = _review_summary(manifest, rows, review)
    complete = all(stats["assessment_complete"] for stats in summary.values())
    kinds = sorted({row["reviewer"]["kind"] for row in review["trials"] if row["reviewer"]})
    report = {"protocol": REVIEW_PROTOCOL, "mode": manifest["mode"], "binding": binding,
              "review_sha256": _hash(review_bytes), "created_at": datetime.now(timezone.utc).isoformat(),
              "review_code_sha256": _hash(Path(__file__).read_bytes()),
              "status": "reviewed" if complete else "partial", "assessment_complete": complete,
              "declared_reviewer_kinds": kinds, "reviewer_identity_authenticated": False,
              "real_provider_evidence": manifest["mode"] == "live_provider",
              "human_semantic_review_complete": complete and kinds == ["human"],
              "semantic_quality_verified": complete and kinds == ["human"] and manifest["mode"] == "live_provider",
              "real_agent_end_to_end_measured": False,
              "metric_scope": "Single-completion answer task success on synthetic context cases; not full Agent workflow success",
              "denominator": "All started trial records, including preparation refusals and provider errors; unstarted trials reported separately",
              "summary": summary}
    destination = _new_output(destination)
    _write_json(destination / "review.json", review)
    _write_json(destination / "review-aggregate.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("template", "import"))
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--review", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "template":
            write_review_template(args.run, args.output)
            print(json.dumps({"status": "pending", "model_calls": 0}))
        else:
            if args.review is None:
                parser.error("--review is required for import")
            print(json.dumps(import_review(args.run, args.review, args.output), ensure_ascii=False))
    except EvaluationError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
