"""Local, versioned evidence retrieval. No URL fetching or paid model calls.

BM25 and dense candidates share the same collection filter; RRF merges ranks.
Embeddings are produced by a cached local ONNX model. The optional PostgreSQL
database is independent of the history archive and never silently falls back.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit

from .execution_privacy import redact_text

MODEL = "BAAI/bge-small-en-v1.5"
DIMENSIONS = 384
MAX_CHUNKS = 20000
_GATE = threading.BoundedSemaphore(4)
_MODEL_LOCK = threading.Lock()


class KnowledgeError(RuntimeError):
    """Public-safe error without SQL, credentials, or provider exception text."""


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9_]+|[\u3400-\u9fff]", text.casefold())


def chunks(text: str, maximum: int = 1400) -> list[dict]:
    """Bounded line-aware chunks; line spans refer to sanitized imported text."""
    result, buffer, size, start = [], [], 0, 1
    for number, line in enumerate(text.splitlines(), 1):
        if buffer and size + len(line) + 1 > maximum:
            result.append({"content": "\n".join(buffer), "line_start": start, "line_end": number - 1})
            buffer, size, start = [], 0, number
        # Long lines retain the line number on every segment, never unbounded.
        if len(line) > maximum:
            for offset in range(0, len(line), maximum):
                result.append({"content": line[offset:offset + maximum], "line_start": number, "line_end": number})
            start = number + 1
        else:
            if not buffer:
                start = number
            buffer.append(line)
            size += len(line) + 1
    if buffer:
        result.append({"content": "\n".join(buffer), "line_start": start, "line_end": len(text.splitlines())})
    return [item for item in result if item["content"].strip()]


def bm25(query: str, rows: list[dict], k: int) -> list[tuple[str, float]]:
    terms = set(tokenize(query))
    documents = [Counter(tokenize(row["content"])) for row in rows]
    if not documents or not terms:
        return []
    lengths = [sum(document.values()) for document in documents]
    average = sum(lengths) / len(lengths) or 1
    frequencies = {term: sum(term in document for document in documents) for term in terms}
    scores = []
    for row, document, length in zip(rows, documents, lengths):
        score = 0.0
        for term in terms:
            frequency = document[term]
            idf = math.log(1 + (len(rows) - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
            score += idf * frequency * 2.2 / (frequency + 1.2 * (0.25 + 0.75 * length / average))
        if score > 0:
            scores.append((row["chunk_id"], score))
    return sorted(scores, key=lambda item: (-item[1], item[0]))[:k]


def rrf(*rankings: list[tuple[str, float]], constant: int = 60) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        seen = set()
        for rank, (identifier, _) in enumerate(ranking, 1):
            if identifier not in seen:
                scores[identifier] = scores.get(identifier, 0) + 1 / (constant + rank)
                seen.add(identifier)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


@dataclass(frozen=True)
class Document:
    source_uri: str
    title: str
    text: str
    version: str
    license: str
    collection: str = "public"
    kind: str = "document"

    def clean(self) -> "Document":
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", self.collection):
            raise KnowledgeError("Invalid collection")
        if self.kind not in {"document", "configuration", "execution"}:
            raise KnowledgeError("Invalid source kind")
        if not all(isinstance(value, str) and value.strip() for value in
                   (self.source_uri, self.title, self.text, self.version, self.license)):
            raise KnowledgeError("Source, title, text, version and license are required")
        try:
            uri = urlsplit(self.source_uri)
            if uri.scheme not in {"https", "studio"} or not uri.netloc or uri.username or uri.password:
                raise ValueError
        except ValueError:
            raise KnowledgeError("Source must be a credential-free https:// or studio:// URI") from None
        if (len(self.text.encode()) > 512000 or len(self.source_uri) > 2000
                or len(self.title) > 240 or len(self.version) > 128 or len(self.license) > 240):
            raise KnowledgeError("Document exceeds the import limit")
        if any(redact_text(value) != value for value in (self.source_uri, self.title, self.version, self.license)):
            raise KnowledgeError("Document metadata contains a credential-like value")
        text = redact_text(self.text).replace("\r\n", "\n").strip()
        if len(tokenize(text)) < 8:
            raise KnowledgeError("Document contains too little indexable content")
        return Document(self.source_uri, self.title, text, self.version, self.license, self.collection, self.kind)


class LocalEmbedder:
    model_id = MODEL

    def __init__(self, *, download: bool = False):
        try:
            from fastembed import TextEmbedding
            from . import config
            self.model = TextEmbedding(MODEL, cache_dir=str(config.STUDIO_HOME / "embedding-cache"),
                                       local_files_only=not download, threads=2)
        except Exception:
            raise KnowledgeError("Local embedding model unavailable; run the explicit knowledge prepare command") from None
        self._lock = threading.Lock()

    def encode(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        with self._lock:
            values = self.model.query_embed(texts) if query else self.model.passage_embed(texts, batch_size=32)
            return [list(map(float, value)) for value in values]


@lru_cache(maxsize=1)
def _embedder() -> LocalEmbedder:
    with _MODEL_LOCK:
        return LocalEmbedder()


def vector_literal(vector: list[float]) -> str:
    if len(vector) != DIMENSIONS or any(not math.isfinite(float(x)) for x in vector):
        raise KnowledgeError("Invalid embedding dimensions or values")
    return "[" + ",".join(format(float(x), ".8g") for x in vector) + "]"


_SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS studio_knowledge;
CREATE TABLE IF NOT EXISTS studio_knowledge.documents (
 doc_id text PRIMARY KEY, collection text NOT NULL, source_uri text NOT NULL,
 title text NOT NULL, version text NOT NULL, license text NOT NULL, kind text NOT NULL,
 content_hash text NOT NULL, model text NOT NULL, updated_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(collection, source_uri), UNIQUE(collection, content_hash)
);
CREATE TABLE IF NOT EXISTS studio_knowledge.chunks (
 chunk_id text PRIMARY KEY, doc_id text NOT NULL REFERENCES studio_knowledge.documents ON DELETE CASCADE,
 ordinal integer NOT NULL, content text NOT NULL, line_start integer NOT NULL, line_end integer NOT NULL,
 embedding vector(384) NOT NULL, UNIQUE(doc_id, ordinal)
);
CREATE INDEX IF NOT EXISTS knowledge_collection ON studio_knowledge.documents(collection);
"""


class KnowledgeStore:
    def __init__(self, dsn: str, embedder=None):
        self._dsn = dsn
        self._embedding = embedder

    @property
    def embedder(self):
        return self._embedding or _embedder()

    @property
    def model_id(self):
        return self._embedding.model_id if self._embedding is not None else MODEL

    @contextmanager
    def connect(self):
        import psycopg
        if not _GATE.acquire(timeout=2):
            raise KnowledgeError("Knowledge service is busy; retry later")
        try:
            with psycopg.connect(self._dsn, connect_timeout=5,
                                options="-c statement_timeout=15000 -c lock_timeout=5000",
                                application_name="studio-knowledge") as connection:
                yield connection
        except psycopg.Error:
            raise KnowledgeError("Knowledge storage unavailable; verify the configured pgvector service") from None
        finally:
            _GATE.release()

    def initialize(self):
        with self.connect() as conn:
            conn.execute(_SCHEMA)

    def status(self):
        with self.connect() as conn:
            documents, = conn.execute("SELECT count(*) FROM studio_knowledge.documents").fetchone()
            total, = conn.execute("SELECT count(*) FROM studio_knowledge.chunks").fetchone()
            collections = [row[0] for row in conn.execute("SELECT DISTINCT collection FROM studio_knowledge.documents ORDER BY collection")]
        return {"configured": True, "ready": True, "embedding_model": MODEL,
                "documents": documents, "chunks": total, "collections": collections}

    def import_document(self, document: Document):
        doc = document.clean()
        digest = fingerprint(doc.text)
        identifier = fingerprint(doc.collection + "\0" + doc.source_uri)
        with self.connect() as conn:
            row = conn.execute("SELECT doc_id, content_hash, version, model, title, license, kind FROM studio_knowledge.documents WHERE collection=%s AND (source_uri=%s OR content_hash=%s) ORDER BY (source_uri=%s) DESC LIMIT 1",
                               (doc.collection, doc.source_uri, digest, doc.source_uri)).fetchone()
        if row and row[1:] == (digest, doc.version, self.model_id, doc.title, doc.license, doc.kind):
            return {"doc_id": row[0], "changed": False, "duplicate": row[0] != identifier}
        parts = chunks(doc.text)
        if len(parts) > 512:
            raise KnowledgeError("Document has too many chunks")
        vectors = self.embedder.encode([part["content"] for part in parts])
        if len(vectors) != len(parts):
            raise KnowledgeError("Embedding result count mismatch")
        prepared = [(fingerprint(identifier + digest + str(i)), identifier, i, part["content"],
                     part["line_start"], part["line_end"], vector_literal(vector))
                    for i, (part, vector) in enumerate(zip(parts, vectors))]
        with self.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(672914021)")
            existing = conn.execute("SELECT doc_id FROM studio_knowledge.documents WHERE collection=%s AND content_hash=%s AND doc_id<>%s", (doc.collection, digest, identifier)).fetchone()
            if existing:
                return {"doc_id": existing[0], "changed": False, "duplicate": True}
            count, = conn.execute("SELECT count(*) FROM studio_knowledge.chunks WHERE doc_id<>%s", (identifier,)).fetchone()
            if count + len(parts) > MAX_CHUNKS:
                raise KnowledgeError("Knowledge chunk limit reached; remove unused indexed sources")
            conn.execute("""INSERT INTO studio_knowledge.documents(doc_id,collection,source_uri,title,version,license,kind,content_hash,model)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(doc_id) DO UPDATE SET
                title=excluded.title,version=excluded.version,license=excluded.license,kind=excluded.kind,
                content_hash=excluded.content_hash,model=excluded.model,updated_at=now()""",
                         (identifier, doc.collection, doc.source_uri, doc.title, doc.version, doc.license, doc.kind, digest, self.model_id))
            conn.execute("DELETE FROM studio_knowledge.chunks WHERE doc_id=%s", (identifier,))
            with conn.cursor() as cursor:
                cursor.executemany("INSERT INTO studio_knowledge.chunks VALUES (%s,%s,%s,%s,%s,%s,%s::vector)", prepared)
        return {"doc_id": identifier, "changed": True, "duplicate": False, "chunk_count": len(parts)}

    def documents(self, collection="public", offset=0, limit=20):
        with self.connect() as conn:
            total, = conn.execute("SELECT count(*) FROM studio_knowledge.documents WHERE collection=%s", (collection,)).fetchone()
            from psycopg.rows import dict_row
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("""SELECT d.doc_id,d.title,d.source_uri,d.version,d.kind,d.license,d.content_hash,
                    count(c.chunk_id)::int AS chunk_count FROM studio_knowledge.documents d
                    LEFT JOIN studio_knowledge.chunks c USING(doc_id) WHERE collection=%s
                    GROUP BY d.doc_id ORDER BY d.source_uri LIMIT %s OFFSET %s""", (collection, limit, offset))
                items = cur.fetchall()
        return {"items": items, "total": total, "offset": offset, "limit": limit,
                "next_offset": offset + limit if offset + limit < total else None}

    def prune_session(self, collection, source_prefix, keep_sources):
        """Remove obsolete execution projections, never original History data.

        Called only by reconciliation after all current documents were imported.
        Exact prefix comparison (not LIKE) avoids wildcard interpretation of IDs.
        """
        if (not re.fullmatch(r"agent-[a-f0-9]{24}", collection)
                or not re.fullmatch(r"studio://agents/[a-z0-9-]+/sessions/[^/]+/turns/", source_prefix)
                or any(not source.startswith(source_prefix) for source in keep_sources)):
            raise KnowledgeError("Invalid private execution projection scope")
        with self.connect() as conn:
            conn.execute("""DELETE FROM studio_knowledge.documents
                WHERE collection=%s AND kind='execution'
                AND left(source_uri,%s)=%s AND NOT(source_uri=ANY(%s::text[]))""",
                (collection, len(source_prefix), source_prefix, list(keep_sources)))

    def delete(self, doc_id):
        with self.connect() as conn:
            result = conn.execute("DELETE FROM studio_knowledge.documents WHERE doc_id=%s", (doc_id,))
        return {"deleted": bool(result.rowcount)}

    def search(self, query: str, *, collection="public", mode="hybrid", k=5, expand_query=True, adjacent_context=False):
        if not isinstance(query, str) or not tokenize(query) or len(query) > 2000 or mode not in {"bm25", "vector", "hybrid"} or type(k) is not int or not 1 <= k <= 20:
            raise KnowledgeError("Invalid search request")
        if type(expand_query) is not bool or type(adjacent_context) is not bool:
            raise KnowledgeError("Invalid query expansion option")
        from .query_expansion import query_plan
        plan = query_plan(query, enabled=expand_query)
        variants = plan["variants"]
        vectors = [vector_literal(value) for value in self.embedder.encode(variants, query=True)] if mode != "bm25" else []
        if mode != "bm25" and len(vectors) != len(variants):
            raise KnowledgeError("Embedding result count mismatch")
        # Fixed total candidate budget per channel, even with an extra query.
        candidate_budget = max(50, k * 4)
        reserve = k if adjacent_context else 0
        lexical_budget = candidate_budget - (math.ceil(reserve / 2) if mode == "hybrid" else reserve)
        dense_budget = candidate_budget - (reserve // 2 if mode == "hybrid" else reserve)
        lexical_limits = [lexical_budget // len(variants) + (i < lexical_budget % len(variants)) for i in range(len(variants))]
        dense_limits = [dense_budget // len(variants) + (i < dense_budget % len(variants)) for i in range(len(variants))]
        from psycopg.rows import dict_row
        with self.connect() as conn:
            # One consistent snapshot: deletion/update cannot mix lexical and vector versions.
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("""SELECT c.chunk_id,c.doc_id,c.ordinal,c.content,c.line_start,c.line_end,
                    d.title,d.source_uri,d.version,d.content_hash FROM studio_knowledge.chunks c
                    JOIN studio_knowledge.documents d USING(doc_id) WHERE collection=%s AND model=%s
                    ORDER BY c.chunk_id LIMIT %s""", (collection, self.model_id, MAX_CHUNKS + 1))
                rows = cur.fetchall()
            if len(rows) > MAX_CHUNKS:
                raise KnowledgeError("Collection exceeds the supported exact-search capacity")
            lexical_lists = [bm25(variant, rows, limit) for variant, limit in zip(variants, lexical_limits)] if mode != "vector" else []
            dense_lists = []
            for vector, limit in zip(vectors, dense_limits):
                dense_lists.append([(row[0], float(row[1])) for row in conn.execute("""SELECT c.chunk_id,1-(embedding <=> %s::vector) AS similarity
                    FROM studio_knowledge.chunks c JOIN studio_knowledge.documents d USING(doc_id)
                    WHERE collection=%s AND model=%s ORDER BY embedding <=> %s::vector,c.chunk_id LIMIT %s""",
                    (vector, collection, self.model_id, vector, limit))])
        # Do not rescale the old single-query channel scores.
        lexical = (rrf(*lexical_lists) if len(lexical_lists) > 1 else lexical_lists[0]) if lexical_lists else []
        dense = (rrf(*dense_lists) if len(dense_lists) > 1 else dense_lists[0]) if dense_lists else []
        ranking = rrf(lexical, dense) if mode == "hybrid" else lexical if mode == "bm25" else dense
        by_id = {row["chunk_id"]: row for row in rows}
        hits = [{**by_id[key], "score": score} for key, score in ranking[:k]]
        if adjacent_context:
            from .query_expansion import with_next_passages
            hits = with_next_passages(hits, rows, k)
        return {"query": query, "mode": mode, "collection": collection,
                "hits": hits, "embedding_model": self.model_id,
                "query_plan": {**plan, "candidate_budget_per_channel": candidate_budget,
                               "candidate_budget_total": candidate_budget * (2 if mode == "hybrid" else 1),
                               "lexical_candidate_budget": lexical_budget if mode != "vector" else 0,
                               "dense_candidate_budget": dense_budget if mode != "bm25" else 0,
                               "adjacent_candidate_budget": reserve,
                               "context_policy": "next-passage-experiment-v1" if adjacent_context else "none"}}

    def validate_citations(self, findings: list[dict], evidence: list[dict]) -> dict:
        """Check current version + exact supporting quote, not semantic entailment."""
        known = {row["chunk_id"]: row for row in evidence}
        if not findings:
            return {"valid": False, "reason": "No findings"}
        with self.connect() as conn:
            for finding in findings:
                citations = finding.get("citations", [])
                quote = finding.get("text", "")
                if not citations or not quote.strip():
                    return {"valid": False, "reason": "Missing evidence"}
                for identifier in citations:
                    row = known.get(identifier)
                    if not row or quote not in row["content"]:
                        return {"valid": False, "reason": "Quote is not supported by supplied evidence"}
                    current = conn.execute("SELECT c.content,d.version FROM studio_knowledge.chunks c JOIN studio_knowledge.documents d USING(doc_id) WHERE chunk_id=%s", (identifier,)).fetchone()
                    if not current or current != (row["content"], row["version"]):
                        return {"valid": False, "reason": "Evidence version is stale or removed"}
        return {"valid": True, "reason": "Current sources and exact quotes verified; no semantic entailment claim"}

    def diagnose(self, query: str, **kwargs):
        results = self.search(query, **kwargs)
        # Dense nearest neighbours always exist, even for an unrelated question.
        # Require lexical overlap before presenting excerpts; still not proof of relevance.
        terms = set(tokenize(" ".join(results.get("query_plan", {}).get("variants", [query]))))
        evidence = [row for row in results["hits"] if len(terms & set(tokenize(row["content"]))) >= min(2, len(terms))]
        findings = [{"text": row["content"][:700], "citations": [row["chunk_id"]]} for row in evidence[:3]]
        checked = self.validate_citations(findings, evidence) if findings else {"valid": False, "reason": "No matching evidence"}
        if not checked["valid"]:
            findings, evidence = [], []
        return {"status": "evidence" if findings else "no_evidence", "issue": query,
                "findings": findings, "evidence": evidence, "method": "extractive",
                "citation_check": checked,
                "next_steps": ["Compare the quoted documentation with the failing run and its recorded tool arguments."] if findings else ["No verified evidence: refine the query or import relevant documentation; do not infer a cause."]}


def configured_store() -> KnowledgeStore:
    dsn = os.environ.get("STUDIO_KNOWLEDGE_DATABASE_URL")
    if not dsn:
        path = os.environ.get("STUDIO_KNOWLEDGE_CONFIG")
        try:
            dsn = json.loads(Path(path).read_text())["database_url"] if path else None
        except (OSError, ValueError, KeyError, TypeError):
            raise KnowledgeError("Invalid knowledge configuration") from None
    if not isinstance(dsn, str) or not dsn:
        raise KnowledgeError("Knowledge database is not configured")
    return KnowledgeStore(dsn)
