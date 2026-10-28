"""Local, read-only history endpoints backed by Studio's execution adapter.

No Agent startup, upstream requests, database writes, or caller-selected paths.
The local guard is not authentication for a shared or remotely proxied service.
"""

from __future__ import annotations

from collections import Counter
import ipaddress
from pathlib import Path
import re
import stat
from typing import Annotated, Generic, Sequence, TypeVar
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Path as PathParam, Query, Request, Response
from pydantic import BaseModel, ConfigDict

from .. import config, registry
from ..execution_adapter import HistoryRead, ReadIssue, read_execution_history
from ..execution_models import ExecutionEvent, SessionRecord, TurnRecord
from ..execution_privacy import redact_text, redact_value
from ..prompt_provenance_view import PromptProvenanceView, project_prompt_provenance


def _local_history_request(request: Request, response: Response) -> None:
    """Permit this machine's UI/CLI, reject remote peers and cross-site browsers."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    try:
        local_peer = request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
        local_host = request.url.hostname in {"localhost", "127.0.0.1", "::1"}
        origin = request.headers.get("origin")
        if origin is not None:
            parsed = urlsplit(origin)
            same_origin = (
                parsed.scheme == request.url.scheme
                and parsed.netloc.lower() == request.url.netloc.lower()
                and not parsed.path and not parsed.query and not parsed.fragment
                and parsed.username is None and parsed.password is None
            )
        else:
            same_origin = True  # Local non-browser clients need no Origin header.
    except ValueError:
        local_peer = local_host = same_origin = False
    fetch_site = request.headers.get("sec-fetch-site")
    if not (local_peer and local_host and same_origin) or fetch_site not in {None, "none", "same-origin"}:
        raise HTTPException(403, "History is available only to the local Studio client")


router = APIRouter(
    prefix="/agents/{slug}/sessions", tags=["sessions"],
    dependencies=[Depends(_local_history_request)],
)

_SLUG_PATTERN = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
Slug = Annotated[str, PathParam(min_length=1, max_length=255, pattern=_SLUG_PATTERN)]
SessionId = Annotated[str, PathParam(min_length=1, max_length=512, pattern=r"^\S+$")]
Offset = Annotated[int, Query(ge=0)]
Limit = Annotated[int, Query(ge=1, le=100)]
EventLimit = Annotated[int, Query(ge=1, le=500)]
T = TypeVar("T")


class _ResponseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Page(_ResponseModel, Generic[T]):
    items: list[T]
    total: int
    offset: int
    limit: int
    next_offset: int | None


class SessionList(_ResponseModel):
    agent_slug: str
    sessions: Page[SessionRecord]
    source_exists: bool
    issue_counts: dict[str, int]


class TurnView(TurnRecord):
    """API projection only; never written into a v1 archive capture."""

    prompt_provenance: PromptProvenanceView


class SessionDetail(_ResponseModel):
    session: SessionRecord
    turns: Page[TurnView]
    events: Page[ExecutionEvent]
    issue_counts: dict[str, int]


def _page(items: Sequence[T], offset: int, limit: int) -> Page[T]:
    end = min(offset + limit, len(items))
    return Page(
        items=list(items[offset:end]), total=len(items), offset=offset, limit=limit,
        next_offset=end if end < len(items) else None,
    )


def _issue_counts(issues: Sequence[ReadIssue]) -> dict[str, int]:
    return dict(Counter(issue.code for issue in issues))


def _registered_agent(slug: str) -> Path:
    """Validate the registry boundary without requiring a readable history file."""
    # Defense at the filesystem boundary as well as FastAPI's parameter check.
    if not re.fullmatch(_SLUG_PATTERN, slug):
        raise HTTPException(422, "Invalid agent identifier")
    root = config.AGENTS_DIR.resolve()
    agent_dir = root / slug
    metadata = agent_dir / "meta.json"
    # The configured root may itself be a user-chosen symlink, but descendants
    # must not alias another Agent, its keys, or an unrelated file/directory.
    for candidate in (agent_dir, metadata):
        if candidate.resolve() != candidate:
            raise HTTPException(403, "Linked history paths are not supported")
    try:
        metadata_stat = metadata.stat()
    except FileNotFoundError:
        raise HTTPException(404, "Agent not found") from None
    if not stat.S_ISREG(metadata_stat.st_mode):
        raise HTTPException(500, "Agent metadata is unavailable")
    try:
        meta = registry.load(slug)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(500, "Agent metadata is unavailable") from None
    if meta is None:
        raise HTTPException(500, "Agent metadata is unavailable")
    if meta.slug != slug:
        raise HTTPException(409, "Agent metadata does not match its directory")
    return agent_dir


def _history_source(slug: str) -> Path:
    source = _registered_agent(slug) / ".co" / "session_results.jsonl"
    for candidate in (source.parent, source):
        if candidate.resolve() != candidate:
            raise HTTPException(403, "Linked history paths are not supported")
    try:
        source_stat = source.stat()
    except FileNotFoundError:
        return source  # The reader returns an empty result without creating it.
    if not stat.S_ISREG(source_stat.st_mode):
        raise HTTPException(409, "History source must be a regular file")
    return source


def _history(slug: str) -> HistoryRead:
    try:
        return read_execution_history(_history_source(slug), agent_slug=slug)
    except (OSError, RuntimeError):
        # Paths, raw validation input, and OS error messages may contain secrets.
        raise HTTPException(500, "Unable to read agent history") from None


def _session_sort_key(session: SessionRecord) -> tuple[bool, float, str]:
    timestamp = session.updated_at if session.updated_at is not None else session.created_at
    return timestamp is None, -(timestamp or 0), session.session_id


def _public_session(session: SessionRecord) -> SessionRecord:
    return session.model_copy(update={
        "source_status": redact_text(session.source_status) if session.source_status is not None else None,
    })


def _public_turn(turn: TurnRecord, provenance: PromptProvenanceView | None = None) -> TurnView:
    public = turn.model_copy(update={
        field: redact_text(value) if value is not None else None
        for field in ("prompt", "result", "finish_reason")
        for value in (getattr(turn, field),)
    })
    return TurnView(**public.model_dump(), prompt_provenance=provenance or PromptProvenanceView())


def _public_turns(turns: Sequence[TurnRecord], events: Sequence[ExecutionEvent]) -> list[TurnView]:
    if not turns:
        return []
    provenance = project_prompt_provenance(
        events, agent_slug=turns[0].agent_slug, session_id=turns[0].session_id,
    )
    return [_public_turn(turn, provenance.get(turn.turn_no)) for turn in turns]


def _public_event(event: ExecutionEvent) -> ExecutionEvent:
    return event.model_copy(update={"payload": redact_value(event.payload)})


@router.get("", response_model=SessionList)
def list_sessions(slug: Slug, offset: Offset = 0, limit: Limit = 20) -> SessionList:
    """List only normalized metadata, newest first, without conversation text."""
    history = _history(slug)
    records = sorted((item.session for item in history.sessions), key=_session_sort_key)
    page = _page(records, offset, limit)
    page.items = [_public_session(record) for record in page.items]
    return SessionList(
        agent_slug=slug, sessions=page, source_exists=history.source_exists,
        issue_counts=_issue_counts(history.issues),
    )


@router.get("/{session_id:path}", response_model=SessionDetail)
def get_session(
    slug: Slug, session_id: SessionId,
    turn_offset: Offset = 0, turn_limit: Limit = 20,
    event_offset: Offset = 0, event_limit: EventLimit = 100,
    turn_no: Annotated[int | None, Query(ge=1)] = None,
) -> SessionDetail:
    """Read one session; optional turn filter applies before independent paging.

    session_id is an opaque lookup key, NEVER a filesystem path. The path
    converter also permits encoded slashes in framework-supplied session IDs.
    """
    history = _history(slug)
    selected = next((item for item in history.sessions if item.session.session_id == session_id), None)
    if selected is None:
        raise HTTPException(404, "Session not found")
    turns = [turn for turn in selected.turns if turn_no is None or turn.turn_no == turn_no]
    events = [event for event in selected.events if turn_no is None or event.turn_no == turn_no]
    turn_page = _page(turns, turn_offset, turn_limit)
    event_page = _page(events, event_offset, event_limit)
    turn_page.items = _public_turns(turn_page.items, selected.events)
    event_page.items = [_public_event(event) for event in event_page.items]
    # Whole-file read issues can explain missing data; locations/bodies are not
    # exposed and no raw snapshot/session context is copied into this response.
    return SessionDetail(
        session=_public_session(selected.session), turns=turn_page, events=event_page,
        issue_counts=_issue_counts(history.issues),
    )
