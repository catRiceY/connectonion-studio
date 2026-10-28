"""Explicit, immutable PostgreSQL captures of Studio's normalized history.

Storage and explicit CLI only; background ingestion lives in history_sync.
The CLI reads a registered Agent's fixed source, applies the API privacy
projection, and stores one capture transactionally. New captures never overwrite
prior observations; no Agent runtime or upstream files are modified.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import sys

from pydantic import ValidationError

from .execution_adapter import ConvertedSession
from .execution_models import ExecutionEvent, SessionRecord, TurnRecord
from .execution_privacy import redact_text, redact_value


class ArchiveError(Exception):
    """Safe public error: never include credentials, SQL or captured content."""


@dataclass(frozen=True)
class Capture:
    capture_id: str
    session: SessionRecord
    turns: tuple[TurnRecord, ...]
    events: tuple[ExecutionEvent, ...]
    issue_counts: dict[str, int]


def prepare_capture(converted: ConvertedSession) -> Capture:
    """Validate ownership/order, redact BEFORE persistence, hash canonical data."""
    session = SessionRecord.model_validate(converted.session.model_dump())
    owner = session.agent_slug, session.session_id
    turns = tuple(TurnRecord.model_validate(item.model_dump()) for item in converted.turns)
    events = tuple(ExecutionEvent.model_validate(item.model_dump()) for item in converted.events)
    if any((item.agent_slug, item.session_id) != owner for item in (*turns, *events)):
        raise ArchiveError("History ownership does not match")
    numbers = [turn.turn_no for turn in turns]
    sequences = [event.sequence for event in events]
    if numbers != sorted(set(numbers)) or sequences != sorted(set(sequences)):
        raise ArchiveError("History order is not unique and increasing")
    if len({event.event_id for event in events}) != len(events):
        raise ArchiveError("History event identity is not unique")
    if any(event.turn_no is not None and event.turn_no not in numbers for event in events):
        raise ArchiveError("History event references an unknown turn")
    session = session.model_copy(update={"source_status": redact_text(session.source_status)
                                        if session.source_status is not None else None})
    turns = tuple(turn.model_copy(update={
        key: redact_text(value) if value is not None else None
        for key in ("prompt", "result", "finish_reason")
        for value in (getattr(turn, key),)
    }) for turn in turns)
    events = tuple(event.model_copy(update={"payload": redact_value(event.payload)}) for event in events)
    return _build_capture(session, turns, events, dict(Counter(issue.code for issue in converted.issues)))


def _build_capture(session, turns, events, counts) -> Capture:
    if not isinstance(counts, dict) or any(
        not isinstance(code, str) or not 1 <= len(code) <= 64
        or not code.replace("_", "").isascii() or not code.replace("_", "").isalnum()
        or type(count) is not int or not 1 <= count <= 100000
        for code, count in counts.items()
    ):
        raise ArchiveError("Invalid archive issue summary")
    counts = dict(counts)
    body = {
        "format_version": 1, "redaction_version": 1,
        "session": session.model_dump(), "turns": [turn.model_dump() for turn in turns],
        "events": [event.model_dump() for event in events], "issue_counts": counts,
    }
    encoded = _canonical_json(body).encode()
    return Capture(hashlib.sha256(encoded).hexdigest(), session, turns, events, counts)


def _canonical_json(value) -> str:
    """Hash JSON numeric values, not spellings changed by PostgreSQL JSONB.

    JSONB expands positive exponents and normalizes negative zero. Keep integers
    exact (never cast them to float) and avoid Decimal.normalize's precision cap.
    This is a local v1 fingerprint format, not a claim of RFC 8785 compliance.
    """
    if value is None or isinstance(value, (str, bool)):
        return json.dumps(value, ensure_ascii=True)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ArchiveError("Non-finite archive value")
        if value == 0:
            return "0"
        decimal = format(Decimal(str(value)), "f")
        return decimal.rstrip("0").rstrip(".") if "." in decimal else decimal
    if isinstance(value, list):
        return "[" + ",".join(_canonical_json(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(key, ensure_ascii=True) + ":" + _canonical_json(value[key])
                              for key in sorted(value)) + "}"
    raise ArchiveError("Invalid archive JSON value")


def _validated_capture(capture: Capture) -> Capture:
    """Revalidate public inputs and stored JSON; never trust a supplied digest."""
    clean = prepare_capture(ConvertedSession(capture.session, capture.turns, capture.events))
    clean = _build_capture(clean.session, clean.turns, clean.events, capture.issue_counts)
    if clean.capture_id != capture.capture_id:
        raise ArchiveError("Capture fingerprint does not match its content")
    return clean


def _driver():
    try:
        import psycopg
    except ImportError:
        raise ArchiveError("Install the history extra to use PostgreSQL") from None
    return psycopg


# A capture owns one observed session snapshot. Per-capture keys deliberately
# keep changed/compacted source snapshots separate rather than merging by turn.
_SCHEMA = """
CREATE SCHEMA IF NOT EXISTS studio_history;
CREATE TABLE IF NOT EXISTS studio_history.schema_version (
    singleton boolean PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    version integer NOT NULL CHECK (version > 0)
);
INSERT INTO studio_history.schema_version VALUES (TRUE, 1) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS studio_history.captures (
    capture_id text PRIMARY KEY CHECK (capture_id ~ '^[0-9a-f]{64}$'),
    agent_slug text NOT NULL,
    session_id text NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    format_version integer NOT NULL CHECK (format_version = 1),
    redaction_version integer NOT NULL CHECK (redaction_version = 1),
    source_kind text NOT NULL CHECK (source_kind = 'connectonion-jsonl'),
    session_data jsonb NOT NULL,
    issue_counts jsonb NOT NULL,
    turn_count integer NOT NULL CHECK (turn_count >= 0),
    event_count integer NOT NULL CHECK (event_count >= 0)
);
CREATE INDEX IF NOT EXISTS captures_by_agent_session
    ON studio_history.captures(agent_slug, session_id, imported_at DESC, capture_id);
CREATE INDEX IF NOT EXISTS captures_by_agent_import
    ON studio_history.captures(agent_slug, imported_at DESC, capture_id);
CREATE TABLE IF NOT EXISTS studio_history.turns (
    capture_id text NOT NULL REFERENCES studio_history.captures(capture_id),
    turn_no integer NOT NULL CHECK (turn_no >= 1),
    data jsonb NOT NULL,
    PRIMARY KEY (capture_id, turn_no)
);
CREATE TABLE IF NOT EXISTS studio_history.events (
    capture_id text NOT NULL REFERENCES studio_history.captures(capture_id),
    event_id text NOT NULL,
    sequence integer NOT NULL CHECK (sequence >= 0),
    turn_no integer,
    data jsonb NOT NULL,
    PRIMARY KEY (capture_id, event_id),
    UNIQUE (capture_id, sequence),
    FOREIGN KEY (capture_id, turn_no) REFERENCES studio_history.turns(capture_id, turn_no)
);
"""


# These are source versions, so importing an older snapshot later must not
# promote it. Source update time precedes revision because counters can reset.
# A valid compacted snapshot may have fewer children, so child counts only break
# ties. Unknown event kinds do not make a readable capture less eligible.
_SOURCE_VERSION_FRESHNESS = """source_updated_at DESC NULLS LAST,
    source_revision DESC NULLS LAST,event_count DESC,turn_count DESC"""
_SOURCE_VERSION_ORDER = _SOURCE_VERSION_FRESHNESS + ",capture_id"
_CAPTURE_SOURCE = """SELECT capture_id,imported_at,session_data,session_id,
    event_count,turn_count,
    CAST(session_data->>'source_revision' AS numeric) AS source_revision,
    CAST(session_data->>'updated_at' AS numeric) AS source_updated_at,
    (COALESCE(CAST(issue_counts->>'invalid_trace' AS integer),0)>0
     OR COALESCE(CAST(issue_counts->>'invalid_event' AS integer),0)>0
     OR COALESCE(CAST(issue_counts->>'invalid_turn' AS integer),0)>0)
        AS severe_structure_issue
    FROM studio_history.captures WHERE agent_slug=%s"""


class PostgresArchive:
    """Short transactions, bounded waits, no silent source-file fallback."""

    def __init__(self, dsn: str):
        if not isinstance(dsn, str) or not dsn.strip():
            raise ArchiveError("Archive database is not configured")
        self._dsn = dsn

    def _connect(self):
        psycopg = _driver()
        try:
            return psycopg.connect(
                self._dsn, connect_timeout=5,
                options="-c statement_timeout=10000 -c lock_timeout=5000",
                application_name="co-studio-history",
            )
        except psycopg.Error:
            raise ArchiveError("Cannot connect to the archive database") from None

    @staticmethod
    def _version(conn):
        row = conn.execute("SELECT version FROM studio_history.schema_version WHERE singleton").fetchone()
        if row != (1,):
            raise ArchiveError("Unsupported archive schema version")

    def initialize(self) -> None:
        psycopg = _driver()
        try:
            with self._connect() as conn:
                # Serialize concurrent explicit initialization, not global app startup.
                conn.execute("SELECT pg_advisory_xact_lock(672914018)")
                exists = conn.execute("SELECT to_regclass('studio_history.schema_version')").fetchone()[0]
                if exists is not None:
                    self._version(conn)
                conn.execute(_SCHEMA)
                self._version(conn)
        except psycopg.Error:
            raise ArchiveError("Archive initialization failed; transaction rolled back") from None

    def save(self, capture: Capture) -> bool:
        """Return True for a new immutable capture, False for an exact repeat."""
        psycopg = _driver()
        from psycopg.types.json import Jsonb
        # Rebuild the public capture so callers cannot bypass privacy/identity
        # checks by hand-constructing Capture or supplying a mismatched digest.
        clean = _validated_capture(capture)
        try:
            with self._connect() as conn:
                self._version(conn)
                row = conn.execute(
                    """INSERT INTO studio_history.captures
                    (capture_id,agent_slug,session_id,format_version,redaction_version,
                     source_kind,session_data,issue_counts,turn_count,event_count)
                    VALUES (%s,%s,%s,1,1,'connectonion-jsonl',%s,%s,%s,%s)
                    ON CONFLICT (capture_id) DO NOTHING RETURNING capture_id""",
                    (clean.capture_id, clean.session.agent_slug, clean.session.session_id,
                     Jsonb(clean.session.model_dump()), Jsonb(clean.issue_counts),
                     len(clean.turns), len(clean.events)),
                ).fetchone()
                if row is None:
                    return False
                with conn.cursor() as cursor:
                    cursor.executemany("INSERT INTO studio_history.turns VALUES (%s,%s,%s)", [
                        (clean.capture_id, turn.turn_no, Jsonb(turn.model_dump())) for turn in clean.turns
                    ])
                    cursor.executemany("INSERT INTO studio_history.events VALUES (%s,%s,%s,%s,%s)", [
                        (clean.capture_id, event.event_id, event.sequence, event.turn_no,
                         Jsonb(event.model_dump())) for event in clean.events
                    ])
            return True
        except psycopg.Error:
            raise ArchiveError("Archive write failed; transaction rolled back") from None

    def get(self, capture_id: str) -> Capture | None:
        result = self._read_capture(capture_id)
        return None if result is None else result["capture"]

    def get_owned(self, agent_slug: str, capture_id: str) -> dict | None:
        """Filter ownership in SQL before loading any conversation content."""
        return self._read_capture(capture_id, agent_slug)

    def _read_capture(self, capture_id: str, agent_slug: str | None = None) -> dict | None:
        psycopg = _driver()
        if len(capture_id) != 64 or any(char not in "0123456789abcdef" for char in capture_id):
            raise ArchiveError("Invalid capture identifier")
        try:
            with self._connect() as conn:
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                self._version(conn)
                where = "capture_id=%s" if agent_slug is None else "capture_id=%s AND agent_slug=%s"
                args = (capture_id,) if agent_slug is None else (capture_id, agent_slug)
                row = conn.execute(
                    "SELECT session_data,issue_counts,turn_count,event_count,agent_slug,session_id,imported_at "
                    "FROM studio_history.captures WHERE " + where, args,
                ).fetchone()
                if row is None:
                    return None
                turns = conn.execute("SELECT data FROM studio_history.turns WHERE capture_id=%s ORDER BY turn_no",
                                     (capture_id,)).fetchall()
                events = conn.execute("SELECT data FROM studio_history.events WHERE capture_id=%s ORDER BY sequence",
                                      (capture_id,)).fetchall()
                if (len(turns), len(events)) != (row[2], row[3]):
                    raise ArchiveError("Archive capture is incomplete")
                session = SessionRecord.model_validate(row[0])
                if (session.agent_slug, session.session_id) != (row[4], row[5]):
                    raise ArchiveError("Archive ownership does not match")
                capture = _validated_capture(Capture(
                    capture_id, session, tuple(TurnRecord.model_validate(item[0]) for item in turns),
                    tuple(ExecutionEvent.model_validate(item[0]) for item in events), row[1],
                ))
                return {"capture": capture, "archived_at": row[6].isoformat()}
        except (psycopg.Error, ValidationError):
            raise ArchiveError("Unable to read archive capture") from None

    def archive_page(self, agent_slug: str, *, limit: int = 20, offset: int = 0) -> tuple[int, list[dict]]:
        """Page immutable versions, not unique sessions; no child payload loading.

        A repeatable-read transaction keeps the count and this page consistent.
        Across requests a new import may shift offsets; callers deduplicate IDs.
        Detail requests always pin a capture ID and cannot mix source versions.
        """
        psycopg = _driver()
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ArchiveError("Invalid archive pagination")
        try:
            with self._connect() as conn:
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                self._version(conn)
                total = conn.execute("SELECT count(*) FROM studio_history.captures WHERE agent_slug=%s",
                                     (agent_slug,)).fetchone()[0]
                rows = conn.execute("""SELECT capture_id,imported_at,session_data,session_id
                    FROM studio_history.captures WHERE agent_slug=%s
                    ORDER BY imported_at DESC,capture_id LIMIT %s OFFSET %s""",
                    (agent_slug, limit, offset)).fetchall()
                items = []
                for identity, imported_at, data, session_id in rows:
                    session = SessionRecord.model_validate(data)
                    if (session.agent_slug, session.session_id) != (agent_slug, session_id):
                        raise ArchiveError("Archive ownership does not match")
                    items.append({"capture_id": identity, "archived_at": imported_at.isoformat(), "session": session})
                return total, items
        except (psycopg.Error, ValidationError):
            raise ArchiveError("Unable to list archive captures") from None

    def session_page(self, agent_slug: str, *, limit: int = 20, offset: int = 0) -> tuple[int, list[dict]]:
        """Page one readable capture per owned session, keeping versions intact.

        Selection prefers structurally readable snapshots, then source update
        time, revision and child counts. Import time is never freshness. Equal
        or missing source times leave a preference, not proof of source order.
        Pagination orders the chosen sessions by source update time. Both count
        and selection see the same database snapshot and load no child payloads.
        """
        psycopg = _driver()
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ArchiveError("Invalid archive pagination")
        try:
            with self._connect() as conn:
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                self._version(conn)
                total = conn.execute(
                    "SELECT count(DISTINCT session_id) FROM studio_history.captures WHERE agent_slug=%s",
                    (agent_slug,),
                ).fetchone()[0]
                rows = conn.execute(f"""WITH source AS ({_CAPTURE_SOURCE}), ranked AS (
                    SELECT *,count(*) OVER (PARTITION BY session_id) AS version_count,
                        row_number() OVER (PARTITION BY session_id ORDER BY
                            severe_structure_issue,{_SOURCE_VERSION_ORDER}) AS selection_rank,
                        rank() OVER (PARTITION BY session_id ORDER BY
                            {_SOURCE_VERSION_FRESHNESS}) AS source_rank
                    FROM source
                )
                SELECT capture_id,imported_at,session_data,session_id,
                    version_count,severe_structure_issue,source_rank
                FROM ranked WHERE selection_rank=1
                ORDER BY source_updated_at DESC NULLS LAST,
                    source_revision DESC NULLS LAST,capture_id LIMIT %s OFFSET %s""",
                    (agent_slug, limit, offset)).fetchall()
                items = []
                for identity, imported_at, data, session_id, count, damaged, source_rank in rows:
                    item = self._session_item(agent_slug, session_id, identity, imported_at, data, count)
                    warning = None
                    if damaged:
                        warning = "All archived versions have structural issues; showing the latest available version."
                    elif source_rank > 1:
                        warning = "A newer archived version has structural issues; showing an earlier readable version."
                    items.append({**item, "selection_warning": warning})
                return total, items
        except (psycopg.Error, ValidationError):
            raise ArchiveError("Unable to list archived sessions") from None

    def versions_page(self, agent_slug: str, session_id: str, *, limit: int = 20,
                      offset: int = 0) -> tuple[int, list[dict]]:
        """Page all owned versions in selection order without merging snapshots."""
        psycopg = _driver()
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ArchiveError("Invalid archive pagination")
        try:
            with self._connect() as conn:
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                self._version(conn)
                total = conn.execute(
                    "SELECT count(*) FROM studio_history.captures WHERE agent_slug=%s AND session_id=%s",
                    (agent_slug, session_id),
                ).fetchone()[0]
                rows = conn.execute(f"""WITH source AS ({_CAPTURE_SOURCE} AND session_id=%s)
                    SELECT capture_id,imported_at,session_data,session_id FROM source
                    ORDER BY severe_structure_issue,{_SOURCE_VERSION_ORDER} LIMIT %s OFFSET %s""",
                    (agent_slug, session_id, limit, offset)).fetchall()
                return total, [self._session_item(agent_slug, session_id, identity, imported_at, data, total)
                               for identity, imported_at, data, session_id in rows]
        except (psycopg.Error, ValidationError):
            raise ArchiveError("Unable to list archived session versions") from None

    @staticmethod
    def _session_item(agent_slug, session_id, identity, imported_at, data, count) -> dict:
        session = SessionRecord.model_validate(data)
        if (session.agent_slug, session.session_id) != (agent_slug, session_id):
            raise ArchiveError("Archive ownership does not match")
        return {"capture_id": identity, "archived_at": imported_at.isoformat(),
                "session": session, "version_count": count}

    def list_captures(self, agent_slug: str, *, limit: int = 20, offset: int = 0) -> list[dict]:
        psycopg = _driver()
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ArchiveError("Invalid archive pagination")
        try:
            with self._connect() as conn:
                self._version(conn)
                rows = conn.execute("""SELECT capture_id,session_id,imported_at,session_data,
                    turn_count,event_count FROM studio_history.captures WHERE agent_slug=%s
                    ORDER BY imported_at DESC,capture_id LIMIT %s OFFSET %s""",
                    (agent_slug, limit, offset)).fetchall()
                return [{"capture_id": row[0], "session_id": row[1], "imported_at": row[2].isoformat(),
                         "source_revision": row[3].get("source_revision"),
                         "turn_count": row[4], "event_count": row[5]} for row in rows]
        except psycopg.Error:
            raise ArchiveError("Unable to list archive captures") from None


def _configured_dsn(config_path: Path | None) -> str:
    value = os.environ.get("STUDIO_HISTORY_DATABASE_URL")
    if value:
        if not value.strip():
            raise ArchiveError("Archive database is not configured")
        return value
    if config_path is not None:
        try:
            data = json.loads(config_path.read_text())
            value = data.get("database_url")
            if isinstance(value, str) and value.strip():
                return value
        except (OSError, ValueError, AttributeError):
            pass
    raise ArchiveError("Set STUDIO_HISTORY_DATABASE_URL or supply --config with database_url")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Explicit local history archive; no Agent execution")
    parser.add_argument("--config", type=Path, help="Local JSON database config (never commit credentials)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Create the versioned archive schema")
    importer = sub.add_parser("import-session", help="Archive one currently readable registered Agent session")
    importer.add_argument("agent_slug")
    importer.add_argument("session_id")
    listing = sub.add_parser("list", help="List immutable captures, newest import first")
    listing.add_argument("agent_slug")
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--offset", type=int, default=0)
    show = sub.add_parser("show", help="Read an archived capture without its source file")
    show.add_argument("capture_id")
    args = parser.parse_args(argv)
    try:
        archive = PostgresArchive(_configured_dsn(args.config))
        if args.command == "init":
            archive.initialize()
            output = {"schema_version": 1, "initialized": True}
        elif args.command == "import-session":
            from fastapi import HTTPException
            from .api.sessions import _history
            try:
                history = _history(args.agent_slug)
            except HTTPException:
                raise ArchiveError("Cannot read registered Agent history") from None
            selected = next((item for item in history.sessions if item.session.session_id == args.session_id), None)
            if selected is None:
                raise ArchiveError("Session is not available in the current source")
            capture = prepare_capture(selected)
            inserted = archive.save(capture)
            output = {"capture_id": capture.capture_id, "inserted": inserted,
                      "turns": len(capture.turns), "events": len(capture.events),
                      "issue_counts": capture.issue_counts,
                      "source_issue_counts": dict(Counter(issue.code for issue in history.issues))}
        elif args.command == "list":
            output = archive.list_captures(args.agent_slug, limit=args.limit, offset=args.offset)
        else:
            capture = archive.get(args.capture_id)
            if capture is None:
                raise ArchiveError("Archive capture not found")
            output = {"capture_id": capture.capture_id, "session": capture.session.model_dump(),
                      "turns": [turn.model_dump() for turn in capture.turns],
                      "events": [event.model_dump() for event in capture.events],
                      "issue_counts": capture.issue_counts}
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except (ArchiveError, ValidationError) as error:
        print(str(error) if isinstance(error, ArchiveError) else "Invalid normalized history", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
