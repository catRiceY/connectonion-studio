"""Same-machine knowledge management. Imports are explicit pasted text, not URLs."""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from typing import Literal

from ..knowledge import Document, KnowledgeError, MODEL, configured_store
from .sessions import _local_history_request

router = APIRouter(prefix="/knowledge", tags=["knowledge"], dependencies=[Depends(_local_history_request)])


class ImportBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_uri: str = Field(min_length=1, max_length=2000)
    title: str = Field(min_length=1, max_length=240)
    text: str = Field(min_length=1, max_length=512000)
    version: str = Field(min_length=1, max_length=128)
    license: str = Field(min_length=1, max_length=240)
    collection: str = Field(default="public", pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    kind: Literal["document", "configuration", "execution"] = "document"


class SearchBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1, max_length=2000)
    collection: str = Field(default="public", pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    mode: Literal["hybrid", "bm25", "vector"] = "hybrid"
    k: int = Field(default=5, ge=1, le=20)


class GenerateBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1, max_length=2000)
    collection: str = Field(default="public", pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    mode: Literal["hybrid", "bm25", "vector"] = "hybrid"
    model: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$")
    allow_model_call: bool

    @field_validator("allow_model_call")
    @classmethod
    def require_consent(cls, value):
        if value is not True:
            raise ValueError("Explicit model-call consent is required")
        return value


def call(action):
    try:
        return action(configured_store())
    except KnowledgeError as exc:
        raise HTTPException(503, str(exc)) from None


@router.get("/status")
def status():
    import os
    from .. import registry
    from ..knowledge_sync import collection_for
    agent_collections = [{"collection": collection_for(meta.slug), "agent_slug": meta.slug,
                          "agent_name": meta.name} for meta in registry.load_all()]
    if not (os.environ.get("STUDIO_KNOWLEDGE_CONFIG") or os.environ.get("STUDIO_KNOWLEDGE_DATABASE_URL")):
        return {"configured": False, "ready": False, "embedding_model": MODEL, "documents": 0, "chunks": 0, "collections": [], "agent_collections": agent_collections}
    return {**call(lambda store: store.status()), "agent_collections": agent_collections}


@router.get("/documents")
def documents(collection: str = Query(default="public", pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$"), offset: int = Query(default=0, ge=0), limit: int = Query(default=20, ge=1, le=100)):
    return call(lambda store: store.documents(collection, offset, limit))


@router.post("/documents")
def import_document(body: ImportBody):
    return call(lambda store: store.import_document(Document(**body.model_dump())))


@router.delete("/documents/{doc_id}")
def delete_document(doc_id: str):
    if len(doc_id) != 64 or any(char not in "0123456789abcdef" for char in doc_id):
        raise HTTPException(422, "Invalid document identifier")
    return call(lambda store: store.delete(doc_id))


@router.post("/search")
def search(body: SearchBody):
    return call(lambda store: store.search(**body.model_dump()))


@router.post("/diagnose")
def diagnose(body: SearchBody):
    return call(lambda store: store.diagnose(**body.model_dump()))


@router.post("/generate")
async def generate_diagnosis(body: GenerateBody, request: Request):
    from .. import registry
    from ..knowledge_sync import collection_for
    from ..diagnostic_web import run
    if not body.query.strip():
        raise HTTPException(422, "A nonempty question is required")
    # Never silently broaden a private Agent request to the public collection.
    if body.collection != "public" and body.collection not in {
        collection_for(meta.slug) for meta in registry.load_all()
    }:
        raise HTTPException(404, "Knowledge scope is unavailable")
    return await run(request, body.model_dump(exclude={"allow_model_call"}))
