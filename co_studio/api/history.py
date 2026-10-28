"""A single read-only History view; storage selection is server-side.

The background sync service owns imports. GET requests never import, initialize,
start Agents, or fall back to files when a configured archive fails.
"""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import Field, ValidationError

from ..execution_models import SessionRecord
from ..history_archive import ArchiveError
from . import archives, sessions
from .sessions import Limit, Offset, Page, Slug, _ResponseModel, _local_history_request, _public_session


router = APIRouter(prefix="/agents/{slug}/history", tags=["history"],
                   dependencies=[Depends(_local_history_request)])


class SyncStatus(_ResponseModel):
    state: Literal["disabled", "pending", "syncing", "ok", "warning", "error", "stopped"]
    checked_at: str | None = None
    synced_at: str | None = None
    message: str | None = None


class HistorySession(SessionRecord):
    capture_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    archived_at: str | None = None
    version_count: int = Field(default=1, ge=1)
    selection_warning: str | None = None


class HistoryList(_ResponseModel):
    agent_slug: str
    source: Literal["archive", "file"]
    source_exists: bool
    issue_counts: dict[str, int]
    sync: SyncStatus
    sessions: Page[HistorySession]


class HistoryVersion(_ResponseModel):
    capture_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    archived_at: str
    session: SessionRecord


class HistoryVersions(_ResponseModel):
    agent_slug: str
    session_id: str
    versions: Page[HistoryVersion]


def _sync_status(request: Request, slug: str) -> SyncStatus:
    if getattr(request.app.state, "history_sync_error", False):
        return SyncStatus(state="error", message="History synchronization could not start. Restart Studio to retry.")
    sync = getattr(request.app.state, "history_sync", None)
    if sync is None:
        return SyncStatus(state="pending", message="History sync has not started.")
    return SyncStatus.model_validate(sync.status(slug))


@router.get("", response_model=HistoryList)
def list_history(request: Request, slug: Slug, offset: Offset = 0, limit: Limit = 20) -> HistoryList:
    # Invalid config must be an error, not permission to select a different store.
    storage = archives.storage_info()
    if not storage.archive_configured:
        result = sessions.list_sessions(slug, offset=offset, limit=limit)
        return HistoryList(
            agent_slug=slug, source="file", source_exists=result.source_exists,
            issue_counts=result.issue_counts, sync=SyncStatus(state="disabled"),
            sessions=Page(items=[HistorySession(**item.model_dump()) for item in result.sessions.items],
                          total=result.sessions.total, offset=offset, limit=limit,
                          next_offset=result.sessions.next_offset),
        )
    archives._require_agent(slug)
    try:
        total, rows = archives._archive().session_page(slug, limit=limit, offset=offset)
        records = []
        for row in rows:
            session = _public_session(row["session"])
            if session.agent_slug != slug:
                raise ArchiveError("History ownership does not match")
            records.append(HistorySession(
                **session.model_dump(), capture_id=row["capture_id"], archived_at=row["archived_at"],
                version_count=row["version_count"], selection_warning=row.get("selection_warning"),
            ))
        return HistoryList(
            agent_slug=slug, source="archive", source_exists=True, issue_counts={},
            sync=_sync_status(request, slug),
            sessions=Page(items=records, total=total, offset=offset, limit=limit,
                          next_offset=offset + len(records) if records and offset + len(records) < total else None),
        )
    except (ArchiveError, ValidationError):
        raise HTTPException(503, "History storage is unavailable. Check the database and retry; no file fallback was used.") from None


@router.get("/versions", response_model=HistoryVersions)
def list_versions(
    slug: Slug, session_id: Annotated[str, Query(min_length=1, max_length=512, pattern=r"^\S+$")],
    offset: Offset = 0, limit: Limit = 20,
) -> HistoryVersions:
    if not archives.storage_info().archive_configured:
        raise HTTPException(409, "Saved versions require configured history storage")
    archives._require_agent(slug)
    try:
        total, rows = archives._archive().versions_page(slug, session_id, limit=limit, offset=offset)
        records = []
        for row in rows:
            session = _public_session(row["session"])
            if (session.agent_slug, session.session_id) != (slug, session_id):
                raise ArchiveError("History version ownership does not match")
            records.append(HistoryVersion(capture_id=row["capture_id"], archived_at=row["archived_at"],
                                          session=session))
        return HistoryVersions(
            agent_slug=slug, session_id=session_id,
            versions=Page(items=records, total=total, offset=offset, limit=limit,
                          next_offset=offset + len(records) if records and offset + len(records) < total else None),
        )
    except (ArchiveError, ValidationError):
        raise HTTPException(503, "Saved history versions are unavailable. Check the database and retry.") from None
