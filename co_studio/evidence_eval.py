"""Read-only bilingual retrieval evaluation against versioned original evidence.

No hosted model, no automatic model download, no corpus changes. This scores
retrieved evidence, NOT answer correctness, semantic entailment or refusal.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import time

from .execution_privacy import redact_text
from .knowledge import configured_store, fingerprint
from .knowledge_cli import write_report
from .retrieval_eval import percentile


def resolve_labels(dataset: dict, docs_root: Path, installed_version: str) -> tuple[dict, list[dict]]:
    """Fail closed on stale text, unsafe paths, missing labels or split leakage."""
    if dataset.get("schema_version") != 1:
        raise ValueError("Unsupported evidence dataset schema")
    sources = {}
    for key, doc in dataset["documents"].items():
        relative = Path(doc["path"])
        path = docs_root / relative
        if relative.is_absolute() or ".." in relative.parts or not path.resolve().is_relative_to(docs_root.resolve()) or path.is_symlink():
            raise ValueError("Evidence source must be inside the pinned package documentation")
        text = path.read_text(encoding="utf-8")
        if doc["version"] != installed_version or fingerprint(text) != doc["sha256"]:
            raise ValueError("Evidence source version or original hash changed; review labels before evaluation")
        indexed_text = redact_text(text).replace("\r\n", "\n").strip()
        sources[key] = {**doc, "text": text, "content_hash": fingerprint(indexed_text),
                        "source_uri": f"studio://packages/connectonion/{doc['version']}/docs/{doc['path']}"}
    labels = {}
    for key, label in dataset["evidence"].items():
        source = sources[label["document"]]
        start, end = label["line_start"], label["line_end"]
        lines = source["text"].splitlines()
        quote = label["quote"]
        if (type(start) is not int or type(end) is not int or not 1 <= start <= end <= len(lines)
                or not isinstance(quote, str) or not quote.strip() or quote not in "\n".join(lines[start - 1:end])):
            raise ValueError("Evidence quote does not match its original line range")
        labels[key] = {**label, "source_uri": source["source_uri"], "version": source["version"],
                       "content_hash": source["content_hash"], "original_sha256": source["sha256"]}
    groups, identifiers, queries, cases = set(), set(), {}, []
    for group in dataset["groups"]:
        if group["group_id"] in groups or group["split"] not in {"dev", "holdout"}:
            raise ValueError("Duplicate group or invalid split")
        groups.add(group["group_id"])
        if not group["answer_points"] or not group["expected_behavior"] or not group["review_status"]:
            raise ValueError("Answer expectations and review provenance required")
        for alternatives in group["evidence_requirements"]:
            if not alternatives or any(key not in labels for key in alternatives):
                raise ValueError("Unknown or empty required evidence alternatives")
        for query in group["queries"]:
            if set(query) != {"case_id", "query", "language", "origin"}:
                raise ValueError("Query fields must not override group split, evidence or review metadata")
            if query["case_id"] in identifiers or not query["query"].strip():
                raise ValueError("Duplicate case or empty query")
            # Exact duplicates cannot leak across groups. Paraphrase/translation
            # grouping is explicit and still requires human review.
            canonical = " ".join(query["query"].casefold().split())
            if canonical in queries and queries[canonical] != group["group_id"]:
                raise ValueError("Query duplicated across groups")
            queries[canonical] = group["group_id"]
            identifiers.add(query["case_id"])
            cases.append({**{key: value for key, value in group.items() if key != "queries"}, **query})
    return labels, cases


def score_evidence(hits: list[dict], requirements: list[list[str]], labels: dict) -> dict:
    # A sparse result may have fewer than five anchors followed immediately by
    # supplements. Those passages belong only to full-context metrics. Legacy
    # search() results carry no role and remain ordinary ranked anchors.
    hits = [hit for hit in hits if hit.get("retrieval_role", "anchor") == "anchor"][:5]
    matched, relevant_ranks = set(), []
    allowed = {key for alternatives in requirements for key in alternatives}
    for rank, hit in enumerate(hits, 1):
        found = {key for key in allowed if
                 hit.get("source_uri") == labels[key]["source_uri"] and
                 hit.get("version") == labels[key]["version"] and
                 hit.get("content_hash") == labels[key]["content_hash"] and
                 labels[key]["quote"] in hit.get("content", "")}
        if found:
            matched.update(found)
            relevant_ranks.append(rank)
    covered = sum(bool(set(alternatives) & matched) for alternatives in requirements)
    # Negative/ambiguous questions are not counted as failed positive retrieval
    # cases. Retrieving a nearest neighbour is also NOT a generated bad answer.
    return {"required_evidence_units": len(requirements), "covered_evidence_units": covered,
            "matched_evidence_ids": sorted(matched),
            "evidence_recall_at_5": covered / len(requirements) if requirements else None,
            "complete_evidence_at_5": covered == len(requirements) if requirements else None,
            "reciprocal_rank_at_5": (1 / relevant_ranks[0] if relevant_ranks else 0.) if requirements else None,
            "unlabelled_hit_fraction": (1 - len(relevant_ranks) / len(hits)) if hits and requirements else None,
            "answer_behavior_evaluated": False,
            "public_scope_violation": any(hit.get("source_uri", "").startswith("studio://agents/") for hit in hits)}


def corpus_manifest(store) -> dict:
    with store.connect() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        rows = conn.execute("""SELECT source_uri,version,content_hash,model,count(c.chunk_id)
            FROM studio_knowledge.documents d LEFT JOIN studio_knowledge.chunks c USING(doc_id)
            WHERE collection='public' GROUP BY d.doc_id ORDER BY source_uri""").fetchall()
    return {"collection": "public", "documents": len(rows), "chunks": sum(row[-1] for row in rows),
            "manifest_sha256": fingerprint(json.dumps(rows, ensure_ascii=False))}


def score_context_evidence(hits: list[dict], requirements: list[list[str]], labels: dict) -> dict:
    """Score the complete bounded context, never relabel it as Top-5 recall."""
    allowed = {key for alternatives in requirements for key in alternatives}
    matched = {key for hit in hits for key in allowed if
               hit.get("source_uri") == labels[key]["source_uri"] and
               hit.get("version") == labels[key]["version"] and
               hit.get("content_hash") == labels[key]["content_hash"] and
               labels[key]["quote"] in hit.get("content", "")}
    covered = sum(bool(set(alternatives) & matched) for alternatives in requirements)
    return {"required_evidence_units": len(requirements), "covered_evidence_units": covered,
            "matched_evidence_ids": sorted(matched), "context_passages": len(hits),
            "context_documents": len({row.get("source_uri") for row in hits}),
            "context_bytes": sum(len(row.get("content", "").encode("utf-8")) for row in hits),
            "evidence_recall_in_context": covered / len(requirements) if requirements else None,
            "complete_evidence_in_context": covered == len(requirements) if requirements else None,
            "answer_behavior_evaluated": False}


def summarize(results: list[dict]) -> dict:
    positive = [row for row in results if row["required_evidence_units"]]
    return {"queries": len(results), "independent_groups": len({row["group_id"] for row in results}),
            "positive_queries": len(positive),
            "mean_evidence_recall_at_5": sum(row["evidence_recall_at_5"] for row in positive) / len(positive) if positive else None,
            "complete_evidence_queries": sum(bool(row["complete_evidence_at_5"]) for row in positive),
            "mrr_at_5": sum(row["reciprocal_rank_at_5"] for row in positive) / len(positive) if positive else None,
            "errors": sum(row["error"] is not None for row in results),
            "public_scope_violations": sum(row["public_scope_violation"] for row in results),
            "latency_p95_ms": percentile([row["latency_ms"] for row in results], .95),
            "answer_accuracy": None, "semantic_citation_support": None, "refusal_accuracy": None}


def strategy_order(case_index: int, *, adjacent_experiment=False):
    """Alternate the paired baseline order independently of extra strategies."""
    paired = [(False, False), (True, False)] if case_index % 2 == 0 else [(True, False), (False, False)]
    return paired + ([(True, True)] if adjacent_experiment else [])


def run(dataset_path: Path, output: Path, *, split="dev", validate_only=False, adjacent_experiment=False):
    text = dataset_path.read_text(encoding="utf-8")
    dataset = json.loads(text)
    distribution = importlib.metadata.distribution("connectonion")
    labels, cases = resolve_labels(dataset, Path(distribution.locate_file("connectonion/docs")), distribution.version)
    cases = [case for case in cases if split == "all" or case["split"] == split]
    if adjacent_experiment and split != "dev":
        raise ValueError("Post-baseline adjacency experiment is development-only; do not retune the exposed holdout")
    if not cases:
        raise ValueError("No cases selected")
    if validate_only:
        return {"valid": True, "queries": len(cases), "groups": len({case["group_id"] for case in cases}),
                "review_status": dataset["review_status"], "dataset_sha256": fingerprint(text)}
    store = configured_store()
    before = corpus_manifest(store)
    store.embedder.encode(["Warm local retrieval model"], query=True)
    rows = []
    for case_index, case in enumerate(cases):
        # Paired order alternates to reduce a systematic cache-order advantage.
        strategies = strategy_order(case_index, adjacent_experiment=adjacent_experiment)
        for expand, adjacent in strategies:
            start = time.perf_counter()
            error, response = None, {"hits": []}
            try:
                response = store.search(case["query"], collection="public", mode="hybrid", k=5,
                                        expand_query=expand, adjacent_context=adjacent)
            except Exception as exc:
                error = type(exc).__name__  # Never dump database credentials.
            elapsed = (time.perf_counter() - start) * 1000
            score = score_evidence(response["hits"], case["evidence_requirements"], labels)
            rows.append({"case_id": case["case_id"], "group_id": case["group_id"], "split": case["split"],
                         "language": case["language"], "origin": case["origin"], "query": case["query"],
                         "review_status": case["review_status"], "expected_behavior": case["expected_behavior"],
                         "strategy": "glossary_adjacent" if adjacent else "glossary_hybrid" if expand else "original_hybrid", **score,
                         "latency_ms": elapsed, "error": error, "query_plan": response.get("query_plan"),
                         "hits": response["hits"]})
        print(f"Evaluated {case['case_id']} ({case['split']})", flush=True)
    after = corpus_manifest(store)
    metrics = {}
    for strategy in ("original_hybrid", "glossary_hybrid", *(["glossary_adjacent"] if adjacent_experiment else [])):
        selected = [row for row in rows if row["strategy"] == strategy]
        metrics[strategy] = {"all": summarize(selected),
                             "by_split": {name: summarize([row for row in selected if row["split"] == name]) for name in {row["split"] for row in selected}},
                             "by_language": {name: summarize([row for row in selected if row["language"] == name]) for name in {row["language"] for row in selected}}}
    code_files = [Path(__file__), Path(__file__).with_name("knowledge.py"), Path(__file__).with_name("query_expansion.py")]
    report = {"dataset_id": dataset["dataset_id"], "dataset_sha256": fingerprint(text),
              "created_at": datetime.now(timezone.utc).isoformat(), "review_status": dataset["review_status"],
              "code_sha256": {file.name: fingerprint(file.read_text()) for file in code_files},
              "framework_version": distribution.version, "embedding_model": store.model_id,
              "corpus_before": before, "corpus_after": after, "corpus_unchanged": before == after,
              "query_count": len(cases), "independent_groups": len({case["group_id"] for case in cases}),
              "requests": len(rows), "hosted_model_calls": 0, "metrics": metrics,
              "adjacent_experiment": adjacent_experiment,
              "labels": labels, "results": rows,
              "limitations": ["Agent-authored/source-checked labels await human review; not an independent external test set.",
                              "Translations share group/split; multiple queries are not independent scenarios.",
                              "Evidence coverage requires exact labelled source/version/hash/quote, not merely the file name.",
                              "Unlabelled-hit fraction uses incomplete relevance labels and is not a general precision score.",
                              "Abstain/clarify/refuse and conflict expectations are saved but no answer behavior is graded.",
                              "Local sequential warm-model retrieval latency only; not Agent end-to-end latency.",
                              "Finite glossary, not multilingual embeddings or general translation; original query and candidate budget retained."]}
    write_report(output, report)
    print(json.dumps({"metrics": metrics, "corpus_unchanged": before == after}, ensure_ascii=False, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("evaluation/retrieval-evidence-v1.json"))
    parser.add_argument("--output", type=Path, default=Path(".co-studio/reports/retrieval-evidence-v1.json"))
    parser.add_argument("--split", choices=["dev", "holdout", "all"], default="dev")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--adjacent-experiment", action="store_true", help="Development-only bounded next-passage candidate")
    args = parser.parse_args()
    result = run(args.dataset, args.output, split=args.split, validate_only=args.validate_only,
                 adjacent_experiment=args.adjacent_experiment)
    if args.validate_only:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
