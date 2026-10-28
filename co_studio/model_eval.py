"""Explicitly opt-in, paired context/model trials on frozen synthetic inputs.

Planning is offline. Running requires a named model and a call allowance; this
module never reads personal Agent history, executes tools, or judges semantics.
SDK retries/charges may be opaque: a dispatch allowance is NOT a dollar cap.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import math
from pathlib import Path
import random
import time

from .context_eval import DATASET_VERSION, context_cases
from .context_policy import ContextError, ContextPolicy, POLICY_VERSION, build_context, estimate_tokens, message_groups
from .evaluation import EvaluationError, _new_output, _write_json
from .eval_provider import is_unrecoverable

PROTOCOL_VERSION = "paired-context-model-trials-v1"
STRATEGIES = ("full", "truncate", "studio_extractive")
MAX_REPEATS = 10
MAX_ANSWER_CHARS = 20000


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _sha(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def make_plan(*, repeats=1, case_ids=None, seed=1729):
    if type(repeats) is not int or not 1 <= repeats <= MAX_REPEATS:
        raise EvaluationError("Repeats must be an integer between 1 and 10")
    if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
        raise EvaluationError("Seed must be an unsigned 32-bit integer")
    cases = context_cases()
    if case_ids is not None:
        if (not isinstance(case_ids, (list, tuple)) or not case_ids
                or any(not isinstance(value, str) for value in case_ids)
                or len(set(case_ids)) != len(case_ids)
                or set(case_ids) - {case.case_id for case in cases}):
            raise EvaluationError("Select unique existing context case IDs")
        cases = [case for case in cases if case.case_id in case_ids]
    manifest = [asdict(case) for case in cases]
    schedule = []
    rng = random.Random(seed)
    for repeat in range(repeats):
        # Pair the exact same source input. Randomize ordering to reduce a fixed
        # warm/cache ordering advantage; record it, do not claim a cold cache.
        order = list(cases)
        rng.shuffle(order)
        for case in order:
            strategies = list(STRATEGIES)
            rng.shuffle(strategies)
            schedule.extend({"case_id": case.case_id, "repeat": repeat + 1, "strategy": strategy}
                            for strategy in strategies)
    return {"protocol": PROTOCOL_VERSION, "dataset_version": DATASET_VERSION,
            "dataset_sha256": _sha(manifest), "dataset_origin": "synthetic_context_contracts",
            "review_status": "agent-authored; human semantic review required",
            "policy_version": POLICY_VERSION, "cases": manifest, "schedule": schedule,
            "seed": seed, "max_dispatches": len(schedule), "concurrency": 1,
            "cost_warning": "SDK retries and provider charges are not bounded by max_dispatches",
            "latency_scope": "One provider completion including SDK overhead; NOT Agent end-to-end P95",
            "cache_condition": "Uncontrolled provider cache; record returned cache usage when available"}


def prepare_messages(case, strategy):
    source = deepcopy(case.messages)
    if strategy == "full":
        messages = source
    elif strategy == "truncate":
        messages = [source[0]]
        for message in reversed(source[1:]):
            candidate = [source[0], message, *messages[1:]]
            if estimate_tokens(candidate) > case.budget:
                break
            messages = candidate
    elif strategy == "studio_extractive":
        messages = build_context(source, ContextPolicy(case.budget), agent_slug="model-eval",
                                 session_id=case.case_id).messages
    else:
        raise EvaluationError("Unknown strategy")
    # A naive truncation may orphan tool results. Preserve that failure instead
    # of silently fixing the baseline or sending an invalid request.
    message_groups(messages)
    return messages


def normalize_usage(usage):
    from connectonion.core.usage import TokenUsage

    framework_usage = isinstance(usage, TokenUsage)
    if hasattr(usage, "model_dump"):
        # Pydantic defaults of zero are not proof the provider reported zero.
        usage = usage.model_dump(exclude_unset=True)
    if not isinstance(usage, dict):
        return None
    result = {}
    for field in ("input_tokens", "output_tokens", "total_tokens", "cached_tokens", "cache_write_tokens"):
        value = usage.get(field)
        result[field] = value if type(value) is int and value >= 0 else None
    # CO 1.9.2 also *explicitly sets* total_tokens=0 for unknown totals in
    # managed legacy responses. exclude_unset alone cannot remove this sentinel.
    if framework_usage and result["total_tokens"] == 0:
        result["total_tokens"] = None
    for source, target in (("cost", "framework_estimated_cost_usd"),
                           ("provider_reported_cost_usd", "provider_reported_cost_usd")):
        value = usage.get(source)
        result[target] = float(value) if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None
    # Do not serialize arbitrary provider payloads, raw responses or credentials.
    return result


def _percentile95(values):
    return sorted(values)[math.ceil(.95 * len(values)) - 1] if values else None


def summarize(rows):
    summaries = {}
    for strategy in STRATEGIES:
        selected = [row for row in rows if row["strategy"] == strategy]
        called = [row for row in selected if row["dispatched"]]
        successful = [row for row in called if row["status"] == "completed"]
        elapsed = [row["provider_elapsed_ms"] for row in called if row["provider_elapsed_ms"] is not None]
        usage_rows = [row["usage"] for row in called if row["usage"] is not None]
        totals = {}
        for field in ("input_tokens", "output_tokens", "total_tokens", "framework_estimated_cost_usd", "provider_reported_cost_usd"):
            known = [usage[field] for usage in usage_rows if usage.get(field) is not None]
            totals[field] = {"observed_sum": sum(known) if known else None,
                             "observed_calls": len(known), "dispatched_calls": len(called),
                             "complete": bool(called) and len(known) == len(called)}
        summaries[strategy] = {"trials": len(selected), "dispatched": len(called),
                               "completed": len(successful), "failed_or_refused": len(selected) - len(successful),
                               "exact_answer_checks_passed": sum(row["answer_exact_checks_passed"] is True for row in selected),
                               "human_task_success_rate": None,
                               "provider_p95_ms_all_dispatches": _percentile95(elapsed),
                               "p95_sample_count": len(elapsed), "small_sample_warning": len(elapsed) < 20,
                               "usage": totals}
    return summaries


def run_trials(output, *, model, allow_model_calls=False, max_calls, max_output_tokens=512,
               repeats=1, case_ids=None, seed=1729, _test_provider=None):
    # Consent and full allowance checked BEFORE output creation/provider init.
    if allow_model_calls is not True:
        raise EvaluationError("Explicit --allow-model-calls consent is required")
    if (not isinstance(model, str) or not model.strip() or len(model) > 200
            or any(ord(ch) < 32 for ch in model) or "://" in model):
        raise EvaluationError("An explicit model identifier (not an endpoint or credential) is required")
    if type(max_output_tokens) is not int or not 64 <= max_output_tokens <= 4096:
        raise EvaluationError("Output token request must be between 64 and 4096")
    plan = make_plan(repeats=repeats, case_ids=case_ids, seed=seed)
    if type(max_calls) is not int or not plan["max_dispatches"] <= max_calls <= 180:
        raise EvaluationError("Max calls must cover the full paired plan and be at most 180")
    destination = _new_output(Path(output))
    source_root = Path(__file__).parent
    manifest = {**plan, "model": model.strip(), "max_calls": max_calls,
                "max_output_tokens_requested": max_output_tokens,
                "mode": "scripted_test" if _test_provider is not None else "live_provider",
                "created_at": datetime.now(timezone.utc).isoformat(), "framework_version": version("connectonion"),
                "code_sha256": {name: hashlib.sha256((source_root / name).read_bytes()).hexdigest()
                                for name in ("model_eval.py", "model_review.py", "eval_provider.py",
                                             "context_eval.py", "context_policy.py", "context_input.py")}}
    _write_json(destination / "manifest.json", manifest)
    cases = {case.case_id: case for case in context_cases()}
    rows = []
    provider = _test_provider
    try:
        if provider is None:
            from .eval_provider import create_evaluation_llm
            provider = create_evaluation_llm(model.strip())
    except Exception as exc:
        error_type = type(exc).__name__
        _write_json(destination / "aggregate.json", {"status": "provider_initialization_failed", "dispatched": 0,
                    "error_type": error_type, "stop_reason": error_type if is_unrecoverable(error_type) else None,
                    "planned_trials": len(plan["schedule"]), "unstarted_trials": len(plan["schedule"])})
        raise EvaluationError("Model initialization failed; no requests dispatched") from None
    interrupted = False
    stop_reason = None
    with (destination / "results.jsonl").open("x", encoding="utf-8") as handle:
        for position, item in enumerate(plan["schedule"]):
            case = cases[item["case_id"]]
            source_before = _sha(case.messages)
            row = {**item, "trial_id": f"trial-{position + 1:04d}", "status": "preparation_refused",
                   "dispatched": False, "error_type": None, "usage": None, "answer": None,
                   "answer_exact_checks_passed": None, "human_review": "pending",
                   "provider_elapsed_ms": None, "estimated_input_tokens": None,
                   "within_local_budget": None, "input_sha256": None}
            started = None
            try:
                messages = prepare_messages(case, item["strategy"])
                row.update(estimated_input_tokens=estimate_tokens(messages), input_sha256=_sha(messages))
                row["within_local_budget"] = row["estimated_input_tokens"] <= case.budget
                row["dispatched"] = True
                started = time.perf_counter()
                # Tools are never supplied/executed. Each trial is one explicit
                # completion, no automatic evaluator-side retries or judge calls.
                response = provider.complete(deepcopy(messages), max_tokens=max_output_tokens)
                row["usage"] = normalize_usage(getattr(response, "usage", None))
                answer = getattr(response, "content", None)
                if not isinstance(answer, str) or getattr(response, "tool_calls", None):
                    row["status"] = "unexpected_response"
                elif len(answer) > MAX_ANSWER_CHARS:
                    row["status"] = "answer_too_large"
                else:
                    row.update(status="completed", answer=answer,
                               answer_exact_checks_passed=all(value in answer for value in case.expected))
            except ContextError as exc:
                if row["dispatched"]:
                    row["status"] = "provider_error"
                row["error_type"] = type(exc).__name__
            except KeyboardInterrupt:
                row["status"] = "interrupted"
                interrupted = True
            except Exception as exc:
                row["status"] = "provider_error" if row["dispatched"] else "preparation_error"
                row["error_type"] = type(exc).__name__
                row["usage"] = normalize_usage(getattr(exc, "usage", None))
            finally:
                if started is not None:
                    row["provider_elapsed_ms"] = (time.perf_counter() - started) * 1000
                row["source_unchanged"] = _sha(case.messages) == source_before
                handle.write(_json(row) + "\n")
                handle.flush()
                rows.append(row)
            if interrupted:
                stop_reason = "interrupted"
                break
            if is_unrecoverable(row.get("error_type")):
                stop_reason = row["error_type"]
                break
    aggregate = {"protocol": PROTOCOL_VERSION, "mode": manifest["mode"],
                 "status": "interrupted" if interrupted else ("stopped" if stop_reason else "finished"),
                 "stop_reason": stop_reason, "planned_trials": len(plan["schedule"]),
                 "unstarted_trials": len(plan["schedule"]) - len(rows),
                 "completed_trial_records": len(rows), "semantic_quality_verified": False,
                 "real_agent_end_to_end_measured": False, "latency_scope": plan["latency_scope"],
                 "sdk_retry_count": None, "billing_complete": False,
                 "summary": summarize(rows)}
    _write_json(destination / "aggregate.json", aggregate)
    from .model_review import write_review_template
    write_review_template(destination, destination / "human-review.json")
    return aggregate


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "run"))
    parser.add_argument("--case", action="append", dest="case_ids")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--seed", type=int, default=1729)
    parser.add_argument("--model")
    parser.add_argument("--allow-model-calls", action="store_true")
    parser.add_argument("--max-calls", type=int)
    parser.add_argument("--max-output-tokens", type=int, default=512)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "plan":
            plan = make_plan(repeats=args.repeats, case_ids=args.case_ids, seed=args.seed)
            print(_json({key: value for key, value in plan.items() if key != "cases"}))
        else:
            if args.output is None:
                parser.error("--output must name a new directory")
            result = run_trials(args.output, model=args.model, allow_model_calls=args.allow_model_calls,
                                max_calls=args.max_calls, max_output_tokens=args.max_output_tokens,
                                repeats=args.repeats, case_ids=args.case_ids, seed=args.seed)
            print(_json(result))
    except EvaluationError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
