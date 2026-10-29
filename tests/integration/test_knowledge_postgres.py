"""Opt-in real pgvector checks in a newly-created disposable database only."""
from dataclasses import replace
import json
import os
from pathlib import Path
from uuid import uuid4
import pytest
from co_studio.knowledge import Document, KnowledgeStore, MODEL

pytestmark = pytest.mark.skipif(not os.environ.get("STUDIO_TEST_KNOWLEDGE_CONFIG"), reason="Explicit pgvector test admin config required")


class Embedder:
    model_id = MODEL
    def encode(self, texts, **kwargs):
        # Controlled vectors exercise real pgvector SQL, not model quality.
        return [[1., float("restart" in text), float("approval" in text)] + [0.] * 381 for text in texts]


@pytest.fixture(scope="module")
def store():
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    from psycopg.sql import SQL, Identifier
    dsn = json.loads(Path(os.environ["STUDIO_TEST_KNOWLEDGE_CONFIG"]).read_text())["database_url"]
    settings = conninfo_to_dict(dsn)
    assert settings.get("host") == "127.0.0.1" and "service" not in settings
    name = "studio_knowledge_test_" + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(SQL("CREATE DATABASE {}").format(Identifier(name)))
    value = KnowledgeStore(make_conninfo(dsn, dbname=name, hostaddr="127.0.0.1"), Embedder())
    value.initialize()
    try:
        yield value
    finally:
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(SQL("DROP DATABASE {}").format(Identifier(name)))


def doc(**changes):
    return replace(Document("studio://test/" + uuid4().hex, "restart", "Restart keeps the original port stable and preserves all saved history records.", "v1", "CC0", collection="test"), **changes)


def test_import_dedup_update_citations_delete(store):
    item = doc(collection="lifecycle")
    first = store.import_document(item)
    assert first["changed"]
    assert not store.import_document(item)["changed"]
    assert store.import_document(replace(item, source_uri="studio://duplicate/source"))["duplicate"]
    result = store.search("restart port", collection=item.collection)
    evidence = result["hits"]
    findings = [{"text": evidence[0]["content"][:30], "citations": [evidence[0]["chunk_id"]]}]
    assert store.validate_citations(findings, evidence)["valid"]
    assert not store.validate_citations([{"text": "Invented", "citations": findings[0]["citations"]}], evidence)["valid"]
    store.import_document(replace(item, version="v2", text=item.text + " The new version adds checks."))
    assert not store.validate_citations(findings, evidence)["valid"]
    assert store.documents("lifecycle")["total"] == 1
    assert store.delete(first["doc_id"])["deleted"]
    assert store.search("restart", collection="lifecycle")["hits"] == []


@pytest.mark.parametrize("mode", ["bm25", "vector", "hybrid"])
def test_real_filtered_search_and_diagnose(store, mode):
    store.import_document(doc(collection="a_" + mode))
    store.import_document(doc(collection="b_" + mode, text="Approval denied for writing files without human authorization. Only read operations are permitted."))
    found = store.search("restart", collection="a_" + mode, mode=mode)
    assert len(found["hits"]) == 1
    assert "Restart" in found["hits"][0]["content"]
    report = store.diagnose("restart port", collection="a_" + mode, mode=mode)
    assert report["status"] == "evidence" and report["citation_check"]["valid"]
    assert store.diagnose("unicornzz bananaqq", collection="a_" + mode)["status"] == "no_evidence"


def test_metadata_update_and_parameterization(store):
    item = doc(collection="metadata")
    store.import_document(item)
    update = store.import_document(replace(item, title="'; DROP TABLE documents;--"))
    assert update["changed"]
    assert store.documents("metadata")["items"][0]["title"] == "'; DROP TABLE documents;--"
    assert store.search("restart", collection="' OR true;--")["hits"] == []


def test_prune_respects_session_collection_and_kind(store):
    from co_studio.knowledge_sync import collection_for
    scope = collection_for("one")
    prefix = "studio://agents/one/sessions/abc/turns/"
    sources = [prefix + "1", prefix + "2", "studio://agents/one/sessions/other/turns/1"]
    for i, source in enumerate(sources):
        store.import_document(doc(collection=scope, source_uri=source, kind="execution", text=f"Saved execution turn number {i} with original state and stable recorded result."))
    store.import_document(doc(collection=collection_for("two"), source_uri=prefix + "2", kind="execution"))
    store.prune_session(scope, prefix, [sources[0]])
    assert {item["source_uri"] for item in store.documents(scope)["items"]} == {sources[0], sources[2]}
    assert store.documents(collection_for("two"))["total"] == 1
    store.prune_session(scope, prefix, [])
    assert [item["source_uri"] for item in store.documents(scope)["items"]] == [sources[2]]


@pytest.mark.parametrize("mode", ["bm25", "vector", "hybrid"])
def test_query_expansion_preserves_collection_and_bounded_results(store, mode):
    collection = "expanded_" + mode
    store.import_document(doc(collection=collection, text="Reconnect the session to restore previous conversation history and messages."))
    store.import_document(doc(collection="secret_" + mode, text="Private session history and messages must not leak across the collection boundary."))
    result = store.search("断线后恢复聊天记录", collection=collection, mode=mode)
    assert result["query"] == "断线后恢复聊天记录"
    assert result["query_plan"]["expanded"]
    assert result["query_plan"]["candidate_budget_per_channel"] == 50
    assert len(result["hits"]) == 1
    assert result["hits"][0]["content"].startswith("Reconnect")
    original = store.search("断线后恢复聊天记录", collection=collection, mode=mode, expand_query=False)
    assert original["query_plan"]["policy"] == "original-only"
    assert original["query_plan"]["variants"] == ["断线后恢复聊天记录"]


def test_english_results_unchanged_when_glossary_enabled(store):
    collection = "english_unchanged"
    store.import_document(doc(collection=collection))
    original = store.search("restart port", collection=collection, expand_query=False)
    expanded = store.search("restart port", collection=collection, expand_query=True)
    assert original["hits"] == expanded["hits"]


@pytest.mark.parametrize("mode", ["bm25", "vector", "hybrid"])
def test_adjacent_experiment_total_budget_and_revision_integrity(store, mode):
    collection = "neighbours_" + mode
    item = doc(collection=collection, text=("reconnect session history " * 50) + "\nNext passage restores the saved messages and session identifier.")
    store.import_document(item)
    result = store.search("恢复聊天记录", collection=collection, mode=mode, adjacent_context=True, k=5)
    plan = result["query_plan"]
    assert plan["lexical_candidate_budget"] + plan["dense_candidate_budget"] + plan["adjacent_candidate_budget"] == plan["candidate_budget_total"]
    assert plan["candidate_budget_total"] == (100 if mode == "hybrid" else 50)
    assert len(result["hits"]) <= 5
    assert len({hit["chunk_id"] for hit in result["hits"]}) == len(result["hits"])
    assert all(hit["source_uri"] == item.source_uri for hit in result["hits"])


@pytest.mark.parametrize("mode", ["bm25", "vector", "hybrid"])
def test_context_snapshot_keeps_original_rank_and_scope(store, mode):
    scope = "context_" + mode
    item = doc(collection=scope, text=("restart session recorded history " * 40)
               + "\n## Restore:\nOnly running sessions replay stored events. Completed sessions retain history without replay.")
    other = doc(collection="private_context_" + mode, text="Private restart secret records must never cross the selected collection boundary.")
    store.import_document(item)
    store.import_document(other)
    old = store.search("restart history", collection=scope, mode=mode, k=1)
    result = store.search_context("restart history", collection=scope, mode=mode, k=1)
    assert result["hits"][0]["chunk_id"] == old["hits"][0]["chunk_id"]
    assert result["hits"][0]["score"] == old["hits"][0]["score"]
    assert result["query_plan"]["anchor_count"] == 1
    assert len(result["hits"]) > 1
    assert all(hit["collection"] == scope and hit["source_uri"] == item.source_uri for hit in result["hits"])
    assert len({hit["chunk_id"] for hit in result["hits"]}) == len(result["hits"])
    findings = [{"text": hit["content"], "citations": [hit["chunk_id"]]} for hit in result["hits"]]
    assert store.validate_citations(findings, result["hits"])["valid"]
    for key, value in (("collection", "public"), ("content_hash", "wrong"),
                       ("source_uri", "studio://forged/source"), ("doc_id", "wrong")):
        tampered = [{**hit, key: value} for hit in result["hits"]]
        assert not store.validate_citations(findings, tampered)["valid"]
    store.import_document(replace(item, version="v2", text=item.text + "\nAn updated source revision."))
    assert not store.validate_citations(findings, result["hits"])["valid"]


def test_context_and_original_search_are_read_only(store):
    collection = "read_only_context"
    store.import_document(doc(collection=collection))
    before = store.documents(collection)
    found = store.search_context("restart port", collection=collection)
    after = store.documents(collection)
    assert before == after
    assert found["query_plan"]["context_policy"] == "snapshot-context-v2"
    assert found["query_plan"]["evidence_json_bytes"] <= found["query_plan"]["max_total_bytes"]
