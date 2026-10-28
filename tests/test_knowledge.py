from dataclasses import replace
import pytest
from fastapi.testclient import TestClient
from co_studio.knowledge import Document, KnowledgeError, KnowledgeStore, bm25, chunks, rrf, tokenize, vector_literal
from co_studio.api import knowledge as api
from co_studio.app import create_app


def document(**kwargs):
    return replace(Document("https://example.org/guide", "Tool guide", "The Agent retries safe read operations but requires approval for write operations.", "v1", "MIT"), **kwargs)


@pytest.mark.parametrize("changes", [
    {"source_uri": "file:///etc/passwd"}, {"source_uri": "javascript:alert(1)"},
    {"source_uri": "https://user:password@host.com/x"}, {"text": "tiny"},
    {"collection": "../../secret"}, {"kind": "unknown"}, {"license": ""},
    {"text": "a" * 512001}, {"version": "x" * 129}, {"title": "x" * 241},
])
def test_import_policy(changes):
    with pytest.raises(KnowledgeError): document(**changes).clean()


def test_chunks_are_bounded_and_locatable():
    text = "First line\n" + "x" * 3100 + "\nLast line"
    result = chunks(text)
    assert all(len(part["content"]) <= 1400 for part in result)
    assert result[0]["line_start"] == result[0]["line_end"] == 1
    assert [part["line_start"] for part in result[1:-1]] == [2, 2, 2]
    assert result[-1]["line_start"] == 3


def test_lexical_search_and_fusion_are_deterministic():
    rows = [{"chunk_id": "a", "content": "restart port reuse"}, {"chunk_id": "b", "content": "a request for context compression"}]
    assert bm25("port restart", rows, 5)[0][0] == "a"
    assert bm25("unrelated", rows, 5) == []
    fused = rrf([("a", 99), ("b", 1)], [("b", .9)])
    assert fused[0][0] == "b"
    assert len(rrf([("a", 3), ("a", 2)])) == 1
    assert "工" in tokenize("工具")


@pytest.mark.parametrize("values", [[0.0] * 383, [float("nan")] * 384, [float("inf")] * 384])
def test_invalid_vectors_are_rejected(values):
    with pytest.raises(KnowledgeError): vector_literal(values)


@pytest.mark.parametrize("query,k,mode", [("", 5, "hybrid"), ("!!!", 5, "hybrid"), ("x", True, "hybrid"), ("x", 21, "hybrid"), ("x", 5, "bad")])
def test_search_rejects_bad_arguments_without_connecting(query, k, mode):
    with pytest.raises(KnowledgeError): KnowledgeStore("never connect").search(query, k=k, mode=mode)


class FakeStore:
    def search(self, **kwargs): return {**kwargs, "hits": []}
    def diagnose(self, **kwargs): return {"status": "no_evidence", "evidence": [], "findings": [], "method": "extractive"}
    def import_document(self, value):
        value.clean()
        return {"doc_id": "a" * 64, "changed": True}
    def delete(self, identifier): return {"deleted": True}
    def documents(self, *args): return {"items": [], "total": 0, "next_offset": None}
    def status(self): return {"configured": True, "ready": True}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(api, "configured_store", FakeStore)
    return TestClient(create_app(), base_url="http://127.0.0.1:9900", client=("127.0.0.1", 40000))


def test_api_success_and_no_network_fetch(client):
    assert client.post("/api/knowledge/documents", json=document().__dict__).status_code == 200
    assert client.post("/api/knowledge/search", json={"query": "approval"}).json()["hits"] == []
    assert client.post("/api/knowledge/diagnose", json={"query": "approval"}).json()["status"] == "no_evidence"
    assert client.delete("/api/knowledge/documents/" + "a" * 64).json() == {"deleted": True}
    assert client.get("/api/knowledge/documents").headers["cache-control"] == "no-store"


@pytest.mark.parametrize("body", [{"query": "", "k": 5}, {"query": "q", "k": "5"}, {"query": "q", "k": True}, {"query": "q", "mode": "sql"}, {"query": "q", "extra": "bad"}])
def test_api_validation(client, body):
    assert client.post("/api/knowledge/search", json=body).status_code == 422


def test_cross_origin_and_remote_access_denied(client):
    assert client.post("/api/knowledge/search", json={"query": "q"}, headers={"Origin": "https://evil.invalid"}).status_code == 403
    remote = TestClient(create_app(), base_url="http://127.0.0.1:9900", client=("192.0.2.1", 40000))
    assert remote.get("/api/knowledge/documents").status_code == 403


def test_error_no_fallback(client, monkeypatch):
    def fail(): raise KnowledgeError("Knowledge storage unavailable")
    monkeypatch.setattr(api, "configured_store", fail)
    result = client.get("/api/knowledge/documents")
    assert result.status_code == 503
    assert result.headers["cache-control"] == "no-store"


def test_no_config_status_is_explicit(client, monkeypatch):
    monkeypatch.delenv("STUDIO_KNOWLEDGE_CONFIG", raising=False)
    monkeypatch.delenv("STUDIO_KNOWLEDGE_DATABASE_URL", raising=False)
    assert client.get("/api/knowledge/status").json()["configured"] is False
