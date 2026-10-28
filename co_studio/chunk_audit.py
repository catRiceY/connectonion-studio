"""Read-only audit of indexed passages using the actual cached tokenizer."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.metadata
from pathlib import Path

from .evidence_eval import corpus_manifest
from .knowledge import configured_store, fingerprint
from .knowledge_cli import write_report
from .retrieval_eval import percentile


def token_lengths(tokenizer, texts):
    """Clone, never disable truncation on the production embedding tokenizer."""
    from tokenizers import Tokenizer
    copy = Tokenizer.from_str(tokenizer.to_str())
    truncation = copy.truncation
    if not truncation or not truncation.get("max_length"):
        raise ValueError("Embedding tokenizer has no explicit truncation limit")
    copy.no_truncation()
    copy.no_padding()
    return truncation["max_length"], [len(copy.encode(text).ids) for text in texts]


def run(output):
    store = configured_store()
    before = corpus_manifest(store)
    # LocalEmbedder is local_files_only; this neither downloads nor runs inference.
    tokenizer = store.embedder.model.model.tokenizer
    from psycopg.rows import dict_row
    with store.connect() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        with conn.cursor(row_factory=dict_row) as cursor:
            cursor.execute("""SELECT c.chunk_id,c.content,c.line_start,c.line_end,d.source_uri,d.version
                FROM studio_knowledge.chunks c JOIN studio_knowledge.documents d USING(doc_id)
                WHERE collection='public' AND model=%s ORDER BY source_uri,c.ordinal""", (store.model_id,))
            chunks = cursor.fetchall()
    maximum, lengths = token_lengths(tokenizer, [chunk["content"] for chunk in chunks])
    records = [{"chunk_id": chunk["chunk_id"], "source_uri": chunk["source_uri"], "version": chunk["version"],
                "line_start": chunk["line_start"], "line_end": chunk["line_end"],
                "characters": len(chunk["content"]), "tokens_including_special": length,
                "truncated": length > maximum, "over_limit_tokens": max(0, length - maximum),
                "content_sha256": fingerprint(chunk["content"])} for chunk, length in zip(chunks, lengths)]
    after = corpus_manifest(store)
    result = {"created_at": datetime.now(timezone.utc).isoformat(), "model": store.model_id,
              "fastembed_version": importlib.metadata.version("fastembed"),
              "tokenizer_sha256": fingerprint(tokenizer.to_str()), "tokenizer_limit": maximum,
              "chunks": len(chunks), "truncated_chunks": sum(row["truncated"] for row in records),
              "truncation_rate": sum(row["truncated"] for row in records) / len(records) if records else None,
              "token_p50": percentile(lengths, .5), "token_p95": percentile(lengths, .95),
              "token_max": max(lengths) if lengths else None,
              "corpus_before": before, "corpus_after": after, "corpus_unchanged": before == after,
              "model_inference_calls": 0, "records": records,
              "limitations": ["Existing fixed-character chunks only; no new index or chunker applied.",
                              "Counts include special tokens, match the cached passage tokenizer without a prefix.",
                              "Indexed line spans identify sanitized imported text, not always original-file offsets.",
                              "Token length does not establish whether any dropped text changes retrieval quality."]}
    write_report(output, result)
    return {key: value for key, value in result.items() if key not in {"records", "corpus_before", "corpus_after"}}


if __name__ == "__main__":
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".co-studio/reports/chunk-token-audit-v1.json"))
    print(json.dumps(run(parser.parse_args().output), ensure_ascii=False, indent=2))
