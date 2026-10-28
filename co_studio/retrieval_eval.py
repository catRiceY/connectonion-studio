"""Reproducible real local-embedding / pgvector retrieval comparison."""
import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
import math
from pathlib import Path
import platform
import time

from .knowledge import configured_store, fingerprint
from .knowledge_cli import write_report


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def run(dataset_path: Path, output: Path):
    source = dataset_path.read_text()
    dataset = json.loads(source)
    cases = dataset["cases"]
    if len({case["case_id"] for case in cases}) != len(cases):
        raise ValueError("Duplicate case identifiers")
    store = configured_store()
    # Warm the local model outside measured per-request latency. No paid model.
    store.embedder.encode(["Warm local embedding cache"], query=True)
    before = store.status()
    results = []
    version = importlib.metadata.version("connectonion")
    for case in cases:
        expected = f"studio://packages/connectonion/{version}/docs/" + case["expected_path"]
        for mode in ("bm25", "vector", "hybrid"):
            started = time.perf_counter()
            try:
                answer = store.search(case["query"], mode=mode, k=5)
                elapsed = (time.perf_counter() - started) * 1000
                # Multiple chunks of one document do not count as extra labels.
                rank = next((i for i, hit in enumerate(answer["hits"], 1) if hit["source_uri"] == expected), None)
                findings = [{"text": hit["content"][:300], "citations": [hit["chunk_id"]]} for hit in answer["hits"]]
                verified = store.validate_citations(findings, answer["hits"]) if findings else {"valid": False}
                record = {"case_id": case["case_id"], "mode": mode, "expected_source": expected,
                          "rank": rank, "recall_at_5": float(rank is not None),
                          "reciprocal_rank_at_5": 1/rank if rank else 0,
                          "quote_integrity": verified["valid"], "latency_ms": elapsed,
                          "retrieved_sources": [hit["source_uri"] for hit in answer["hits"]],
                          "retrieved_chunk_ids": [hit["chunk_id"] for hit in answer["hits"]],
                          "error": None}
            except Exception as exc:
                record = {"case_id": case["case_id"], "mode": mode, "expected_source": expected,
                          "rank": None, "recall_at_5": 0., "reciprocal_rank_at_5": 0.,
                          "quote_integrity": False, "latency_ms": (time.perf_counter()-started)*1000,
                          "error": type(exc).__name__}
            results.append(record)
        print(f"Evaluated {case['case_id']}", flush=True)
    metrics = {}
    for mode in ("bm25", "vector", "hybrid"):
        group = [row for row in results if row["mode"] == mode]
        metrics[mode] = {"queries": len(group), "recall_at_5": sum(row["recall_at_5"] for row in group)/len(group),
                         "mrr_at_5": sum(row["reciprocal_rank_at_5"] for row in group)/len(group),
                         "exact_quote_integrity_rate": sum(row["quote_integrity"] for row in group)/len(group),
                         "error_rate": sum(row["error"] is not None for row in group)/len(group),
                         "latency_p50_ms": percentile([row["latency_ms"] for row in group], .5),
                         "latency_p95_ms": percentile([row["latency_ms"] for row in group], .95)}
    report = {"dataset_id": dataset["dataset_id"], "dataset_sha256": fingerprint(source),
              "created_at": datetime.now(timezone.utc).isoformat(), "independent_cases": len(cases),
              "request_count": len(results), "corpus": before, "framework_version": version,
              "hardware": platform.platform(), "embedding_model": store.model_id,
              "code_sha256": fingerprint(Path(__file__).with_name("knowledge.py").read_text()),
              "metrics": metrics, "provider_tokens": None, "model_api_calls": 0,
              "scope": dataset["scope"],
              "limitations": ["32 source-inspected agent-authored questions, not a human-reviewed external holdout",
                             "Quote integrity checks exact text/version; it does not measure semantic entailment",
                             "Single-process sequential warm-model retrieval; no production concurrency claim",
                             "No model-generated diagnoses or end-user task completion measured"],
              "results": results}
    write_report(output, report)
    rows = output.with_suffix(".jsonl")
    rows.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in results))
    print(json.dumps(metrics, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("evaluation/retrieval-v1.json"))
    parser.add_argument("--output", type=Path, default=Path(".co-studio/reports/retrieval-v1.json"))
    args = parser.parse_args()
    run(args.dataset, args.output)


if __name__ == "__main__":
    main()
