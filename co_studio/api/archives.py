"""Local read-only views of explicit PostgreSQL archive captures.

No writes, imports, schema initialization, framework-file fallback or Agent
execution. Database configuration comes from the server environment only.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path as PathParam, Query
from pydantic import ValidationError

from ..execution_models import SessionRecord
from ..history_archive import ArchiveError, PostgresArchive, _configured_dsn
from .sessions import (
    EventLimit, Limit, Offset, Page, SessionDetail, Slug, _ResponseModel,
    _local_history_request, _page, _public_event, _public_session, _public_turns,
    _registered_agent,
)


storage_router = APIRouter(prefix="/history", tags=["history"],
                           dependencies=[Depends(_local_history_request)])
archive_router = APIRouter(prefix="/agents/{slug}/archives", tags=["history"],
                           dependencies=[Depends(_local_history_request)])
CaptureId = Annotated[str, PathParam(pattern=r"^[0-9a-f]{64}$")]


class StorageInfo(_ResponseModel):
    archive_configured: bool
    default_source: Literal["archive", "file"]


class ArchivedSession(SessionRecord):
    capture_id: str
    archived_at: str


class ArchiveList(_ResponseModel):
    agent_slug: str
    source: Literal["archive"] = "archive"
    source_exists: bool = True
    issue_counts: dict[str, int]
    sessions: Page[ArchivedSession]


class ArchiveDetail(SessionDetail):
    source: Literal["archive"] = "archive"
    capture_id: str
    archived_at: str


def _archive_config_path() -> Path | None:
    value = os.environ.get("STUDIO_HISTORY_CONFIG")
    return Path(value) if value else None


def _archive() -> PostgresArchive:
    return PostgresArchive(_configured_dsn(_archive_config_path()))


def _require_agent(slug: str) -> None:
    try:
        _registered_agent(slug)
    except (OSError, RuntimeError):
        raise HTTPException(500, "Agent metadata is unavailable") from None


@storage_router.get("/storage", response_model=StorageInfo)
def storage_info() -> StorageInfo:
    # Availability is checked when querying data. This endpoint never opens a
    # database connection, exposes a DSN/path, or auto-starts a database service.
    configured = bool(os.environ.get("STUDIO_HISTORY_DATABASE_URL") or _archive_config_path())
    if configured:
        try:
            _configured_dsn(_archive_config_path())
        except ArchiveError:
            raise HTTPException(503, "Archive configuration is unavailable. Check the Studio server configuration.") from None
    return StorageInfo(archive_configured=configured, default_source="archive" if configured else "file")


@archive_router.get("", response_model=ArchiveList)
def list_archives(slug: Slug, offset: Offset = 0, limit: Limit = 20) -> ArchiveList:
    _require_agent(slug)
    try:
        total, items = _archive().archive_page(slug, limit=limit, offset=offset)
        records = []
        for item in items:
            session = _public_session(item["session"])
            if session.agent_slug != slug:
                raise ArchiveError("Archive ownership does not match")
            records.append(ArchivedSession(**session.model_dump(), capture_id=item["capture_id"],
                                           archived_at=item["archived_at"]))
        return ArchiveList(
            agent_slug=slug, issue_counts={},
            sessions=Page(items=records, total=total, offset=offset, limit=limit,
                          next_offset=offset + len(records) if records and offset + len(records) < total else None),
        )
    except (ArchiveError, ValidationError):
        raise HTTPException(503, "PostgreSQL archive is unavailable. Check its configuration and service, or select Files.") from None


@archive_router.get("/{capture_id}", response_model=ArchiveDetail)
def get_archive(
    slug: Slug, capture_id: CaptureId,
    turn_offset: Offset = 0, turn_limit: Limit = 20,
    event_offset: Offset = 0, event_limit: EventLimit = 100,
    turn_no: Annotated[int | None, Query(ge=1)] = None,
) -> ArchiveDetail:
    _require_agent(slug)
    try:
        result = _archive().get_owned(slug, capture_id)
        if result is None:
            raise HTTPException(404, "Archived snapshot not found for this Agent")
        capture = result["capture"]
        if capture.session.agent_slug != slug or capture.capture_id != capture_id:
            raise HTTPException(404, "Archived snapshot not found for this Agent")
        turns = [item for item in capture.turns if turn_no is None or item.turn_no == turn_no]
        events = [item for item in capture.events if turn_no is None or item.turn_no == turn_no]
        turn_page = _page(turns, turn_offset, turn_limit)
        event_page = _page(events, event_offset, event_limit)
        turn_page.items = _public_turns(turn_page.items, capture.events)
        event_page.items = [_public_event(item) for item in event_page.items]
        return ArchiveDetail(
            capture_id=capture_id, archived_at=result["archived_at"],
            session=_public_session(capture.session), turns=turn_page, events=event_page,
            issue_counts=capture.issue_counts,
        )
    except (ArchiveError, ValidationError):
        raise HTTPException(503, "Archived snapshot is unavailable. Check the archive service and integrity, or select Files.") from None
