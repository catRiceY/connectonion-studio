"""Session selection SQL regressions using only synthetic in-memory records.

SQLite executes the shared JSON/window-query subset so offline tests exercise
the actual selection, ownership and pagination SQL. The thin adapter only
converts placeholders and returned JSON/timestamps; PostgreSQL contracts also
run in the explicitly opted-in disposable integration database.
"""
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from types import SimpleNamespace

import pytest

from co_studio import history_archive as archive_module
from co_studio.execution_adapter import ConvertedSession, ReadIssue
from co_studio.execution_models import ExecutionEvent, SessionRecord, TurnRecord


class MemoryResult:
    def __init__(self, cursor):
        self.cursor = cursor

    def _decode(self, row):
        if row is None:
            return None
        values = []
        for column, value in zip(self.cursor.description, row):
            if column[0] in {"session_data", "issue_counts", "data"}:
                value = json.loads(value)
            elif column[0] == "imported_at":
                value = datetime.fromisoformat(value)
            values.append(value)
        return tuple(values)

    def fetchone(self):
        return self._decode(self.cursor.fetchone())

    def fetchall(self):
        return [self._decode(row) for row in self.cursor.fetchall()]


class MemoryConnection:
    def __init__(self):
        self.sql = sqlite3.connect(":memory:")
        self.sql.executescript("""
            ATTACH DATABASE ':memory:' AS studio_history;
            CREATE TABLE studio_history.schema_version (singleton boolean,version integer);
            INSERT INTO studio_history.schema_version VALUES (1,1);
            CREATE TABLE studio_history.captures (
                capture_id text,agent_slug text,session_id text,imported_at text,
                session_data text,issue_counts text,turn_count integer,event_count integer
            );
            CREATE TABLE studio_history.turns (capture_id text,turn_no integer,data text);
            CREATE TABLE studio_history.events (capture_id text,sequence integer,data text);
        """)
        self.calls = []
        self.inserted = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query, params=()):
        self.calls.append((query, params))
        if query.startswith("SET TRANSACTION"):
            assert query == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            return None
        return MemoryResult(self.sql.execute(query.replace("%s", "?"), params))

    def add(self, value):
        self.inserted += 1
        imported = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=self.inserted)
        self.sql.execute("INSERT INTO studio_history.captures VALUES (?,?,?,?,?,?,?,?)", (
            value.capture_id, value.session.agent_slug, value.session.session_id, imported.isoformat(),
            json.dumps(value.session.model_dump()), json.dumps(value.issue_counts),
            len(value.turns), len(value.events),
        ))
        self.sql.executemany("INSERT INTO studio_history.turns VALUES (?,?,?)", [
            (value.capture_id, turn.turn_no, json.dumps(turn.model_dump())) for turn in value.turns
        ])
        self.sql.executemany("INSERT INTO studio_history.events VALUES (?,?,?)", [
            (value.capture_id, event.sequence, json.dumps(event.model_dump())) for event in value.events
        ])
        return value


@pytest.fixture
def store(monkeypatch):
    conn = MemoryConnection()
    monkeypatch.setattr(archive_module, "_driver", lambda: SimpleNamespace(Error=sqlite3.Error))
    value = archive_module.PostgresArchive("synthetic-only")
    monkeypatch.setattr(value, "_connect", lambda: conn)
    yield value, conn
    conn.sql.close()


def capture(*, agent="agent-one", session="session-one", revision=1, updated=100.0,
            event_count=3, turn_count=1, issues=(), result="saved answer"):
    owner = {"agent_slug": agent, "session_id": session}
    return archive_module.prepare_capture(ConvertedSession(
        session=SessionRecord(**owner, source_revision=revision, updated_at=updated),
        turns=tuple(TurnRecord(**owner, turn_no=index + 1, result=result)
                    for index in range(turn_count)),
        events=tuple(ExecutionEvent(**owner, event_id=f"event-{index}", sequence=index,
                                    kind="user_input", turn_no=1 if turn_count else None)
                     for index in range(event_count)),
        issues=tuple(ReadIssue(code) for code in issues),
    ))


def selected(store):
    total, rows = store.session_page("agent-one")
    assert total == 1 and len(rows) == 1
    return rows[0]


def test_one_item_per_session_and_owner_with_all_versions_retained(store):
    archive, conn = store
    old = conn.add(capture())
    current = conn.add(capture(revision=2, updated=101.0))
    other_session = conn.add(capture(session="session-two", updated=102.0))
    conn.add(capture(agent="other-agent", revision=999))
    total, rows = archive.session_page("agent-one")
    assert total == 2
    assert [row["capture_id"] for row in rows] == [other_session.capture_id, current.capture_id]
    assert [row["version_count"] for row in rows] == [1, 2]
    assert all(row["selection_warning"] is None for row in rows)
    assert set(rows[1]) == {"capture_id", "archived_at", "session", "version_count", "selection_warning"}
    total, versions = archive.versions_page("agent-one", "session-one")
    assert total == 2
    assert [row["capture_id"] for row in versions] == [current.capture_id, old.capture_id]
    assert all(row["version_count"] == 2 and "selection_warning" not in row for row in versions)
    assert archive.session_page("' OR true; --") == (0, [])
    assert archive.versions_page("other-agent", "session-two") == (0, [])
    assert archive.versions_page("agent-one", "' OR true; --") == (0, [])
    assert archive.get_owned("other-agent", current.capture_id) is None


def test_late_import_of_an_old_source_does_not_replace_current_session(store):
    archive, conn = store
    latest = conn.add(capture(revision=11, updated=110.0))
    stale = conn.add(capture(revision=20, updated=100.0, event_count=8))
    assert selected(archive)["capture_id"] == latest.capture_id
    assert archive.archive_page("agent-one")[1][0]["capture_id"] == stale.capture_id
    assert archive.versions_page("agent-one", "session-one")[1][0]["capture_id"] == latest.capture_id


@pytest.mark.parametrize("import_old_last", [False, True])
def test_newer_source_time_survives_revision_reset_and_late_old_import(store, import_old_last):
    archive, conn = store
    old = capture(revision=20, updated=101.0, result="old answer")
    latest = capture(revision=1, updated=102.0, result="new answer")
    for value in ((latest, old) if import_old_last else (old, latest)):
        conn.add(value)
    row = selected(archive)
    assert row["capture_id"] == latest.capture_id and row["version_count"] == 2
    assert row["selection_warning"] is None
    total, versions = archive.versions_page("agent-one", "session-one")
    assert total == 2
    assert [item["capture_id"] for item in versions] == [latest.capture_id, old.capture_id]
    assert archive.get_owned("agent-one", old.capture_id)["capture"].turns[0].result == "old answer"


@pytest.mark.parametrize("issue", ["invalid_trace", "invalid_event", "invalid_turn"])
def test_newer_structurally_broken_snapshot_does_not_replace_readable_version(store, issue):
    archive, conn = store
    readable = conn.add(capture())
    broken = conn.add(capture(revision=2, updated=101.0, issues=(issue,)))
    row = selected(archive)
    assert row["capture_id"] == readable.capture_id
    assert row["version_count"] == 2
    assert "earlier readable version" in row["selection_warning"]
    assert {row["capture_id"] for row in archive.versions_page("agent-one", "session-one")[1]} == {
        readable.capture_id, broken.capture_id,
    }


def test_older_broken_capture_does_not_generate_a_stale_selection_warning(store):
    archive, conn = store
    conn.add(capture(revision=1, issues=("invalid_trace",)))
    readable = conn.add(capture(revision=2))
    assert selected(archive)["capture_id"] == readable.capture_id
    assert selected(archive)["selection_warning"] is None


def test_equal_source_freshness_does_not_claim_broken_capture_is_newer(store):
    archive, conn = store
    readable = conn.add(capture())
    conn.add(capture(issues=("invalid_event",)))
    row = selected(archive)
    assert row["capture_id"] == readable.capture_id and row["selection_warning"] is None


def test_all_broken_versions_still_show_latest_source_and_warning(store):
    archive, conn = store
    latest = conn.add(capture(revision=1, updated=200.0, issues=("invalid_event",)))
    conn.add(capture(revision=20, updated=100.0, issues=("invalid_trace",)))
    row = selected(archive)
    assert row["capture_id"] == latest.capture_id
    assert row["selection_warning"].startswith("All archived versions have structural issues")


def test_unknown_event_kind_does_not_disqualify_a_newer_version(store):
    archive, conn = store
    conn.add(capture())
    latest = conn.add(capture(revision=2, issues=("unknown_event_kind",)))
    row = selected(archive)
    assert row["capture_id"] == latest.capture_id and row["selection_warning"] is None


@pytest.mark.parametrize("older,newer", [
    ({"revision": None}, {"revision": 0}),
    ({"updated": None}, {"updated": 0.0}),
    ({"updated": None, "revision": 100}, {"updated": 0.0, "revision": None}),
    ({"updated": 100.0}, {"updated": 101.0}),
    ({"event_count": 2}, {"event_count": 3}),
    ({"turn_count": 1}, {"turn_count": 2}),
])
def test_source_selection_uses_known_time_revision_and_child_count_tiebreaks(store, older, newer):
    archive, conn = store
    expected = conn.add(capture(**newer))
    conn.add(capture(**older))
    assert selected(archive)["capture_id"] == expected.capture_id


def test_final_tiebreak_is_capture_identity_and_not_import_order(store):
    archive, conn = store
    values = sorted([capture(result="answer one"), capture(result="answer two")],
                    key=lambda value: value.capture_id)
    for value in values:
        conn.add(value)
    assert selected(archive)["capture_id"] == values[0].capture_id


def test_compacted_new_revision_is_selected_without_joining_old_events(store):
    archive, conn = store
    before = conn.add(capture(event_count=5, turn_count=2, result="original answer"))
    compacted = conn.add(capture(revision=2, event_count=1, result="compacted answer"))
    assert selected(archive)["capture_id"] == compacted.capture_id
    assert archive.get_owned("agent-one", compacted.capture_id)["capture"] == compacted
    assert archive.get_owned("agent-one", before.capture_id)["capture"] == before
    assert len(archive.get(before.capture_id).events) == 5
    assert len(archive.get(compacted.capture_id).events) == 1


def test_session_and_version_pagination_are_stable_and_count_unique_sessions(store):
    archive, conn = store
    for session_no in range(3):
        for revision in range(4):
            conn.add(capture(session=f"session-{session_no}", revision=revision,
                             updated=float(100 + session_no)))
    total, sessions = archive.session_page("agent-one")
    assert total == 3
    assert archive.session_page("agent-one", limit=1, offset=1) == (3, sessions[1:2])
    assert archive.session_page("agent-one", offset=3) == (3, [])
    total, versions = archive.versions_page("agent-one", "session-1")
    assert total == 4
    assert archive.versions_page("agent-one", "session-1", limit=2, offset=1) == (4, versions[1:3])
    assert archive.versions_page("agent-one", "session-1", offset=4) == (4, [])
    for query, params in conn.calls:
        if "FROM studio_history.captures" in query:
            assert "WHERE agent_slug=%s" in query
            assert params[0] == "agent-one"
    assert conn.calls[0][0].startswith("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")


@pytest.mark.parametrize("method,args", [("session_page", ("agent-one",)),
                                         ("versions_page", ("agent-one", "session-one"))])
@pytest.mark.parametrize("pagination", [{"limit": 0}, {"limit": 101}, {"limit": True},
                                       {"offset": -1}, {"offset": False}, {"offset": 1.5}])
def test_invalid_pagination_is_rejected_before_querying(store, method, args, pagination):
    archive, conn = store
    with pytest.raises(archive_module.ArchiveError, match="Invalid archive pagination"):
        getattr(archive, method)(*args, **pagination)
    assert conn.calls == []


@pytest.mark.parametrize("method,args", [("session_page", ("agent-one",)),
                                         ("versions_page", ("agent-one", "session-one"))])
def test_stored_json_owner_must_match_scoped_columns(store, method, args):
    archive, conn = store
    value = conn.add(capture())
    tampered = value.session.model_copy(update={"agent_slug": "different-owner"}).model_dump()
    conn.sql.execute("UPDATE studio_history.captures SET session_data=?", (json.dumps(tampered),))
    with pytest.raises(archive_module.ArchiveError, match="ownership"):
        getattr(archive, method)(*args)


@pytest.mark.parametrize("method,args", [("session_page", ("agent-one",)),
                                         ("versions_page", ("agent-one", "session-one"))])
def test_database_errors_remain_safe(store, monkeypatch, method, args):
    archive, conn = store
    def fail(*args, **kwargs):
        raise sqlite3.DatabaseError("private-credential SQL detail")
    monkeypatch.setattr(conn, "execute", fail)
    with pytest.raises(archive_module.ArchiveError, match="Unable to list") as error:
        getattr(archive, method)(*args)
    assert "private-credential" not in str(error.value)
