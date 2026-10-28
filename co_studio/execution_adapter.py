"""Read ConnectOnion 1.9.2 Host snapshots without invoking its storage/runtime.

The source is session_results.jsonl, NOT stdout or the HTTP /input envelope.
Each row is a full snapshot; the last valid row owns session/trace state. Earlier
completed snapshots can supply answers for exactly matching retained turns.
This is an internal reader, not a durable archive: compacted-away data cannot
be recovered, and missing values are never inferred from model messages.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
from typing import BinaryIO, Callable

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from .execution_models import ExecutionEvent, Identifier, SessionRecord, TurnRecord
from .context_report import FIELDS as CONTEXT_REPORT_FIELDS, unknown_payload, valid_payload as valid_context_payload


@dataclass(frozen=True)
class ReadIssue:
    """Safe diagnostic location; never includes source text or validation input."""

    code: str
    line_no: int | None = None
    sequence: int | None = None


@dataclass(frozen=True)
class ConvertedSession:
    session: SessionRecord
    turns: tuple[TurnRecord, ...]
    events: tuple[ExecutionEvent, ...]
    issues: tuple[ReadIssue, ...] = ()


@dataclass(frozen=True)
class HistoryRead:
    sessions: tuple[ConvertedSession, ...]
    issues: tuple[ReadIssue, ...] = ()
    source_exists: bool = True


@dataclass(frozen=True)
class _CompletedTurn:
    """A Host answer bound to one unambiguous, fully recorded execution."""

    turn: TurnRecord
    event_ids: tuple[str, ...]
    created: float | None
    updated_at: float | None
    revision: int | None
    line_no: int


class _HostSnapshot(BaseModel):
    # Match the pinned Host envelope, but do not coerce corrupt source values.
    # Missing optional legacy fields stay unknown, not synthesized as revision 1.
    model_config = ConfigDict(extra="ignore", strict=True, allow_inf_nan=False)

    session_id: Identifier
    status: str
    prompt: str
    result: str | None = None
    session: dict[str, JsonValue] | None = None
    created: float | None = Field(default=None, ge=0)
    updated_at: float | None = Field(default=None, ge=0)
    duration_ms: float | None = Field(default=None, ge=0)
    revision: int | None = Field(default=None, ge=0)


_IDENTIFIER = TypeAdapter(Identifier)
ProvenanceVerifier = Callable[[dict, str, str], bool]
_PAYLOAD_FIELDS = {
    "user_input": (
        "content", "source", "watch_event_id", "watch_event_ids",
        "iteration", "runtime_input", "internal",
    ),
    "turn_result": ("reason", "usage", "error_type"),
    "llm_call": ("model", "iteration", "status", "timeout_seconds"),
    "llm_result": (
        "model", "iteration", "status", "usage", "error_type", "finish_reason",
        "tool_calls_count", "context_percent", "source",
    ),
    "tool_call": ("name", "args", "summary"),
    "tool_result": (
        "name", "args", "summary", "status", "result", "error", "error_type",
        "declined", "class",
    ),
    "plan": ("entries",),
    "files_received": ("files",),
    "studio_approval": ("approval_id", "action", "name", "status"),
    "studio_mcp_attempt": (
        "name", "attempt", "max_attempts", "status", "retry_scheduled",
        "error_type", "elapsed_ms",
    ),
    "studio_runtime_manifest": ("run_id", "manifest_sha256", "config_revision"),
    "prompt_provenance": (
        "provenance_version", "status", "version_id", "base_sha256", "system_sha256",
        "launch_version_id", "llm_call_id", "model", "reason",
    ),
    "context_report": CONTEXT_REPORT_FIELDS,
}
_SECRET_KEYS = {
    "apikey", "accesstoken", "refreshtoken", "token", "password", "passwd",
    "secret", "clientsecret", "authorization", "cookie", "setcookie",
    "privatekey", "invitecode",
}


def _select_value(value: JsonValue) -> JsonValue:
    """Detach JSON and mask known credential keys, including nested tool args.

    This is NOT free-text/PII sanitization: prompts, results and errors may still
    contain private text. Future API/export code needs its own access policy.
    """
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.replace("_", "").replace("-", "").lower()
            in _SECRET_KEYS else _select_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_select_value(item) for item in value]
    return value


def _turn_number(value: object) -> int | None:
    return value if type(value) is int and value >= 1 else None


def _parse_snapshot(raw: object) -> _HostSnapshot:
    snapshot = _HostSnapshot.model_validate(raw)
    inner = snapshot.session or {}
    if "session_id" in inner and inner["session_id"] != snapshot.session_id:
        raise ValueError("Conflicting session identity")
    return snapshot


def convert_trace_event(
    raw: dict,
    *,
    agent_slug: str,
    session_id: str,
    event_id: str,
    sequence: int,
    turn_no: int | None = None,
    provenance_verifier: ProvenanceVerifier | None = None,
) -> ExecutionEvent:
    """Map one native trace entry; the caller supplies identity and ownership.

    This replaces the teaching-only tool_result example with shared mapping for
    native event kinds. Unknown kinds retain metadata, NOT arbitrary payloads.
    Required/malformed fields raise; convert_snapshot records these as issues.
    """
    kind = _IDENTIFIER.validate_python(raw.get("type"), strict=True)
    if kind in ("tool_call", "tool_result", "studio_approval", "studio_mcp_attempt"):
        call_id = raw.get("tool_id")
    elif kind in ("llm_call", "llm_result"):
        call_id = raw.get("id")
    else:
        call_id = None
    duration_key = "timing_ms" if kind == "tool_result" else "duration_ms"
    payload = {key: _select_value(raw[key])
               for key in _PAYLOAD_FIELDS.get(kind, ()) if key in raw}
    if kind == "prompt_provenance":
        # Framework traces can contain client-supplied entries. Only a trusted,
        # local verifier can attest that this metadata came from the runtime.
        try:
            verified = provenance_verifier is not None and provenance_verifier(raw, agent_slug, session_id) is True
        except Exception:
            verified = False
        if not verified:
            payload.update(status="unknown", version_id=None, base_sha256=None, reason="unverified_event")
    elif kind == "context_report":
        try:
            verified = (provenance_verifier is not None
                        and provenance_verifier(raw, agent_slug, session_id) is True
                        and valid_context_payload(payload)
                        and _turn_number(raw.get("turn")) == turn_no and turn_no is not None)
        except Exception:
            verified = False
        if verified:
            call_id = payload["llm_call_id"]
        else:
            payload = unknown_payload()
    return ExecutionEvent(
        agent_slug=agent_slug, session_id=session_id,
        event_id=event_id, sequence=sequence, turn_no=turn_no,
        kind=kind, source_id=raw.get("id"), call_id=call_id,
        occurred_at=raw.get("ts"), duration_ms=raw.get(duration_key),
        payload=payload,
    )


def convert_snapshot(
    raw: dict, *, agent_slug: str, provenance_verifier: ProvenanceVerifier | None = None,
) -> ConvertedSession:
    """Convert one Host JSON row without changing it or inferring missing facts."""
    _IDENTIFIER.validate_python(agent_slug, strict=True)
    try:
        snapshot = _parse_snapshot(raw)
    except (ValidationError, ValueError):
        # Pydantic's full error can include credentials from the input.
        raise ValueError("Invalid ConnectOnion Host snapshot") from None
    return _convert_snapshot(snapshot, agent_slug, provenance_verifier=provenance_verifier)


def _convert_snapshot(
    snapshot: _HostSnapshot, agent_slug: str, *, provenance_verifier: ProvenanceVerifier | None = None,
) -> ConvertedSession:
    owner = {"agent_slug": agent_slug, "session_id": snapshot.session_id}
    session = SessionRecord(
        **owner, source_status=snapshot.status, source_revision=snapshot.revision,
        created_at=snapshot.created, updated_at=snapshot.updated_at,
    )
    state = snapshot.session or {}
    trace = state.get("trace", [])
    issues: list[ReadIssue] = []
    if not isinstance(trace, list):
        issues.append(ReadIssue("invalid_trace"))
        trace = []

    events: list[ExecutionEvent] = []
    turns: dict[int, TurnRecord] = {}
    active_turn = None
    occurrences: dict[str, int] = {}
    for sequence, raw in enumerate(trace):
        if not isinstance(raw, dict):
            issues.append(ReadIssue("invalid_event", sequence=sequence))
            active_turn = None  # A missing boundary must not leak the prior turn.
            continue
        kind = raw.get("type")
        explicit_turn = _turn_number(raw.get("turn"))
        if "turn" in raw and explicit_turn is None:
            issues.append(ReadIssue("invalid_turn", sequence=sequence))
            active_turn = None
        if kind == "user_input":
            active_turn = explicit_turn
        # A terminal event without its own turn can close an observed open turn.
        turn_no = explicit_turn if explicit_turn is not None else active_turn

        # Content identity, not row number/revision/trace.id. Appending a snapshot
        # keeps IDs stable; rewriting an event produces a different ID even at
        # the same position. Exact duplicate entries get an occurrence suffix.
        # Without source IDs, identical duplicates across truncated traces cannot
        # be distinguished; warn rather than claim durable exactly-once import.
        encoded = json.dumps(
            [agent_slug, snapshot.session_id, raw], sort_keys=True,
            ensure_ascii=True, allow_nan=False, separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        occurrence = occurrences.get(digest, 0)
        occurrences[digest] = occurrence + 1
        if occurrence:
            issues.append(ReadIssue("duplicate_event_content", sequence=sequence))
        try:
            event = convert_trace_event(
                raw, **owner, event_id=f"co192:{digest}:{occurrence}",
                sequence=sequence, turn_no=turn_no, provenance_verifier=provenance_verifier,
            )
        except (ValidationError, ValueError):
            issues.append(ReadIssue("invalid_event", sequence=sequence))
            active_turn = None
            continue
        events.append(event)
        if event.kind not in _PAYLOAD_FIELDS:
            issues.append(ReadIssue("unknown_event_kind", sequence=sequence))
        if turn_no is not None:
            turn = turns.setdefault(turn_no, TurnRecord(**owner, turn_no=turn_no))
            # runtime_input/watch_events emit additional user_input records in
            # the SAME turn. Preserve them as events, not as the primary prompt.
            if (kind == "user_input" and not raw.get("runtime_input")
                    and not raw.get("internal") and turn.prompt is None
                    and isinstance(raw.get("content"), str)):
                turn.prompt = raw["content"]
            if kind == "turn_result" and isinstance(raw.get("reason"), str):
                turn.finish_reason = raw["reason"]
        if kind == "turn_result":
            active_turn = None

    # Host running/failed snapshots can still contain the PREVIOUS turn's trace.
    # Only attach the Host's latest answer/duration to a matching completed turn.
    # Historical answers are NOT reconstructed from possibly compacted messages.
    latest = turns.get(_turn_number(state.get("turn")))
    if (snapshot.status == "done" and latest is not None
            and latest.finish_reason is not None and latest.prompt == snapshot.prompt):
        latest.result = snapshot.result
        latest.duration_ms = snapshot.duration_ms
    elif snapshot.result is not None:
        issues.append(ReadIssue("unattributed_host_result"))
    # A signature authenticates an observation, not a client-supplied trace's
    # ordering. Require one surrounding actual call and at most one report.
    calls_by_key, reports_by_key = defaultdict(list), defaultdict(list)
    call_closed, turn_closed = {}, {}
    for event in events:
        key = event.turn_no, event.call_id
        if event.kind == "llm_call":
            calls_by_key[key].append(event)
        elif event.kind == "context_report" and event.payload.get("status") != "unknown":
            reports_by_key[key].append(event)
        elif event.kind == "llm_result":
            call_closed.setdefault(key, event.sequence)
        elif event.kind == "turn_result":
            turn_closed.setdefault(event.turn_no, event.sequence)
    for index, event in enumerate(events):
        if event.kind != "context_report" or event.payload.get("status") == "unknown":
            continue
        key = event.turn_no, event.call_id
        calls, reports = calls_by_key[key], reports_by_key[key]
        closed = (call_closed.get(key, event.sequence) < event.sequence
                  or turn_closed.get(event.turn_no, event.sequence) < event.sequence)
        if len(calls) != 1 or calls[0].sequence >= event.sequence or len(reports) != 1 or closed:
            events[index] = event.model_copy(update={"payload": unknown_payload(), "call_id": None})
    return ConvertedSession(session, tuple(turns[n] for n in sorted(turns)),
                            tuple(events), tuple(issues))


def read_execution_history(path: Path, *, agent_slug: str) -> HistoryRead:
    """Read only the file supplied by a trusted Studio caller, never scan home.

    Last VALID row wins even if revision counters are absent or non-monotonic.
    Earlier done rows only fill missing answers/durations for identical turns;
    they never resurrect removed turns/events or override the final Host result.
    Invalid rows/events produce content-free diagnostics; I/O errors other than
    file-not-found propagate (a permission error must not look like no history).
    Reads stop at the initial file size, so concurrent appends cannot run forever.
    No SessionStorage instance, directory, lock or sync-epoch file is created.
    """
    _IDENTIFIER.validate_python(agent_slug, strict=True)
    try:
        handle = Path(path).open("rb")
    except FileNotFoundError:
        return HistoryRead((), source_exists=False)

    verifier = None

    def verify_provenance(raw, slug, session_id):
        nonlocal verifier
        if verifier is None:
            # Old traces never import the runtime helper or read the local key.
            # This trusted source path is selected by Studio, not a request body.
            verifier = lambda *_: False
            from .context_report import history_event_verifier
            verifier = history_event_verifier(Path(path).parent.parent.resolve())
        return verifier(raw, slug, session_id)

    with handle:
        return read_execution_stream(handle, agent_slug=agent_slug, provenance_verifier=verify_provenance)


def read_execution_stream(
    handle: BinaryIO, *, agent_slug: str, max_bytes: int | None = None,
    provenance_verifier: ProvenanceVerifier | None = None,
) -> HistoryRead:
    """Normalize an already-open, seekable file without reopening its path.

    The caller owns the handle and any path/symlink checks. Read from the start
    through the initial size only; reject an oversized file instead of returning
    a truncated history. Callers that require a stable snapshot must compare
    file metadata before and after reading (the Host writes without our lock).
    """
    _IDENTIFIER.validate_python(agent_slug, strict=True)
    remaining = os.fstat(handle.fileno()).st_size
    if max_bytes is not None and remaining > max_bytes:
        raise ValueError("History source exceeds the archive size limit")
    handle.seek(0)

    latest: dict[str, tuple[_HostSnapshot, int]] = {}
    completed: dict[str, dict[int, _CompletedTurn]] = {}
    issues: list[ReadIssue] = []
    line_no = 0
    while remaining > 0:
        line = handle.readline(remaining)
        if not line:
            break
        remaining -= len(line)
        line_no += 1
        if not line.strip():
            continue
        try:
            raw = json.loads(line.decode("utf-8"))
            snapshot = _parse_snapshot(raw)
        except (UnicodeDecodeError, ValueError, RecursionError):
            issues.append(ReadIssue("invalid_snapshot", line_no=line_no))
            continue
        latest[snapshot.session_id] = (snapshot, line_no)
        if snapshot.status == "done":
            converted = _convert_snapshot(snapshot, agent_slug, provenance_verifier=provenance_verifier)
            turn_no = _turn_number((snapshot.session or {}).get("turn"))
            turn = next((item for item in converted.turns if item.turn_no == turn_no), None)
            if turn is not None and turn.prompt == snapshot.prompt:
                fingerprint = _completion_fingerprint(converted, turn.turn_no)
                if fingerprint is not None:
                    # Last completed observation wins, including explicit
                    # null/empty values. Do not fish older answers out of a
                    # previous attempt after a corrected completion.
                    completed.setdefault(snapshot.session_id, {})[turn.turn_no] = _CompletedTurn(
                        turn, fingerprint, snapshot.created, snapshot.updated_at,
                        snapshot.revision, line_no,
                    )

    sessions = []
    for session_id in sorted(latest):
        snapshot, line_no = latest[session_id]
        converted = _convert_snapshot(snapshot, agent_slug, provenance_verifier=provenance_verifier)
        converted = _restore_completed_turns(
            converted, snapshot, line_no, completed.get(session_id, {}),
        )
        converted = replace(converted, issues=tuple(
            replace(issue, line_no=line_no) for issue in converted.issues
        ))
        sessions.append(converted)
        issues.extend(converted.issues)
    return HistoryRead(tuple(sessions), tuple(issues))


def _completion_fingerprint(converted: ConvertedSession, turn_no: int) -> tuple[str, ...] | None:
    """Require exact execution identity, not just a reused turn number/prompt.

    Event IDs hash the entire raw entry before privacy projection. Including all
    events catches changed tool results, runtime inputs and terminal metadata.
    Missing/ambiguous boundaries or conversion issues are conservatively skipped.
    """
    if converted.issues:
        return None
    events = [event for event in converted.events if event.turn_no == turn_no]
    primary = [event for event in events if event.kind == "user_input"
               and not event.payload.get("runtime_input") and not event.payload.get("internal")]
    terminal = [event for event in events if event.kind == "turn_result"]
    if (len(primary) != 1 or len(terminal) != 1
            or events[0] is not primary[0] or events[-1] is not terminal[0]
            or any(right.sequence != left.sequence + 1 for left, right in zip(events, events[1:]))
            or primary[0].source_id is None or terminal[0].source_id is None
            or not isinstance(primary[0].payload.get("content"), str)
            or not isinstance(terminal[0].payload.get("reason"), str)):
        return None
    return tuple(event.event_id for event in events)


def _restore_completed_turns(
    converted: ConvertedSession, snapshot: _HostSnapshot, line_no: int,
    completed: dict[int, _CompletedTurn],
) -> ConvertedSession:
    turns = []
    for turn in converted.turns:
        saved = completed.get(turn.turn_no)
        if (saved is not None and saved.line_no < line_no
                and (turn.result is None or turn.duration_ms is None)
                and saved.turn.prompt == turn.prompt
                and saved.event_ids == _completion_fingerprint(converted, turn.turn_no)
                and (saved.created is None or snapshot.created is None
                     or saved.created == snapshot.created)
                and (saved.revision is None or snapshot.revision is None
                     or saved.revision < snapshot.revision)
                and (saved.updated_at is None or snapshot.updated_at is None
                     or saved.updated_at <= snapshot.updated_at)):
            turn = turn.model_copy(update={
                "result": turn.result if turn.result is not None else saved.turn.result,
                "duration_ms": turn.duration_ms if turn.duration_ms is not None else saved.turn.duration_ms,
            })
        turns.append(turn)
    return replace(converted, turns=tuple(turns))
