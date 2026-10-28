"""Opt-in real PostgreSQL contract tests, NEVER the user's archive database.

Set STUDIO_TEST_HISTORY_CONFIG to a local JSON config for a disposable database
named studio_history_test_*. Without that explicit opt-in these tests skip.
No model, Agent or real history files are accessed. libpq's C networking is not
covered by the offline suite's Python socket patch; this module gates it here.
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
from uuid import uuid4
import threading
import time

import pytest

from co_studio.execution_adapter import ReadIssue, convert_snapshot, read_execution_history
from co_studio.history_archive import ArchiveError, PostgresArchive, prepare_capture


def test_background_sync_to_real_postgres_and_unified_api(archive, slug, tmp_path, monkeypatch):
    """Actual worker polling + safe file adapter + PostgreSQL + read-only HTTP.

    Only this fixture's source and disposable DB are used; model and Agent
    execution are absent. A controlled save failure checks automatic retry.
    """
    from fastapi.testclient import TestClient
    from co_studio import config, registry
    from co_studio.api import archives
    from co_studio.app import create_app
    from co_studio.history_sync import HistorySync

    root = tmp_path / "sync-agents"
    directory = root / slug
    (directory / ".co").mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTS_DIR", root)
    registry.save(registry.AgentMeta(slug=slug, name="Synthetic", address="0xtest", port=9911,
                                    model="offline", capabilities=[], created_at="2026-10-28"))
    source = directory / ".co" / "session_results.jsonl"
    original = json.dumps(raw_snapshot()) + "\n"
    source.write_text(original)
    monkeypatch.setenv("STUDIO_HISTORY_DATABASE_URL", "postgresql://synthetic-config-is-never-connected")
    monkeypatch.delenv("STUDIO_HISTORY_CONFIG", raising=False)
    monkeypatch.setattr(archives, "_archive", lambda: archive)
    fail_writes = threading.Event()
    failed_write_seen = threading.Event()
    class ControlledWriter:
        def save(self, value):
            if fail_writes.is_set():
                failed_write_seen.set()
                raise ArchiveError("synthetic write outage")
            return archive.save(value)
    worker = HistorySync(lambda: ControlledWriter(), poll_interval=0.02, retry_max=0.1,
                         reconcile_interval=10)
    app = create_app()
    app.state.history_sync = worker
    client = TestClient(app, base_url="http://127.0.0.1:9900", client=("127.0.0.1", 50000))

    def eventually(predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        pytest.fail("Background sync did not reach its expected state")

    def list_rows():
        response = client.get(f"/api/agents/{slug}/history")
        assert response.status_code == 200
        return response.json()

    worker.start()
    try:
        eventually(lambda: worker.status(slug)["state"] == "ok")
        first = list_rows()
        assert first["sessions"]["total"] == 1
        first_id = first["sessions"]["items"][0]["capture_id"]
        assert source.read_text() == original
        assert list((directory / ".co").iterdir()) == [source]

        # Simulate a source update while the archive writer is unavailable.
        fail_writes.set()
        later = json.dumps(raw_snapshot(revision=4, updated_at=102.0, result="400")) + "\n"
        with source.open("a") as handle:
            handle.write(later)
        assert failed_write_seen.wait(5)
        eventually(lambda: worker.status(slug)["state"] == "error")
        during_failure = list_rows()
        assert during_failure["sync"]["state"] == "error"
        assert during_failure["sessions"]["items"][0]["capture_id"] == first_id

        # No manual scan or new source write: retry itself catches up.
        fail_writes.clear()
        eventually(lambda: worker.status(slug)["state"] == "ok")
        updated = list_rows()
        assert updated["sessions"]["total"] == 1
        current = updated["sessions"]["items"][0]
        assert current["version_count"] == 2 and current["capture_id"] != first_id
        assert current["source_revision"] == 4
        versions = client.get(f"/api/agents/{slug}/history/versions", params={"session_id": "session-1"})
        assert versions.json()["versions"]["total"] == 2
        for identity, answer in ((first_id, "391"), (current["capture_id"], "400")):
            detail = client.get(f"/api/agents/{slug}/archives/{identity}").json()
            assert detail["turns"]["items"][0]["result"] == answer
        assert source.read_text() == original + later

        # A subsequent process-style catch-up is idempotent; deleting only the
        # synthetic source does not destroy stored history.
        worker.stop()
        restarted = HistorySync(lambda: archive)
        restarted.scan_once()
        assert restarted.status(slug)["state"] == "ok"
        assert archive.session_page(slug)[1][0]["version_count"] == 2
        source.unlink()
        restarted.scan_once()
        assert restarted.status(slug)["state"] == "warning"
        assert archive.get_owned(slug, first_id)["capture"].turns[0].result == "391"
        restarted.stop()
    finally:
        fail_writes.clear()
        worker.stop()
        client.close()


def test_context_report_real_host_jsonl_sync_postgres_api(archive, slug, tmp_path, monkeypatch):
    """Signed runtime observation survives the actual persistence/read pipeline."""
    from connectonion import Agent
    from connectonion.core.llm import LLMResponse
    from connectonion.network.host.session import SessionStorage
    from connectonion.network.host.session.turn import input_handler
    from fastapi.testclient import TestClient
    from co_studio import config, registry
    from co_studio.api import archives
    from co_studio.app import create_app
    from co_studio.context_runtime import attach_context_policy
    from co_studio.context_policy import ContextPolicy
    from co_studio.context_report import SIGNATURE
    from co_studio.history_sync import HistorySync

    root = tmp_path / "agents"
    directory = root / slug
    co_dir = directory / ".co"
    co_dir.mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTS_DIR", root)
    registry.save(registry.AgentMeta(slug=slug, name="Context report integration", address="0xtest", port=9911,
                                    model="offline", capabilities=[], created_at="2026-10-28"))

    class ScriptedModel:
        model = "offline-context-report"

        def complete(self, messages, **kwargs):
            return LLMResponse(content="Offline answer", tool_calls=[], raw_response=None, usage=None)

    def factory():
        agent = Agent("Context report", llm=ScriptedModel(), system_prompt="Rules", quiet=True, log=False,
                      co_dir=co_dir, state_dir=co_dir)
        return attach_context_policy(agent, agent_slug=slug, policy=ContextPolicy(1100), agent_dir=directory)

    storage = SessionStorage(co_dir / "session_results.jsonl")
    input_handler(factory, storage, "Continue", result_ttl=3600, session={"session_id": "context-session"})
    original = storage.path.read_bytes()
    worker = HistorySync(lambda: archive)
    try:
        worker.scan_once()
        assert worker.status(slug)["state"] == "ok"
        _, page = archive.session_page(slug)
        identity = page[0]["capture_id"]
        stored = archive.get_owned(slug, identity)["capture"]
        observation, = [event for event in stored.events if event.kind == "context_report"]
        assert observation.payload["status"] == "prepared"
        assert observation.payload["compacted"] is False
        assert observation.payload["source_messages"] == observation.payload["retained_messages"] == 2
        assert SIGNATURE not in repr(stored)
        monkeypatch.setattr(archives, "_archive", lambda: archive)
        client = TestClient(create_app(), base_url="http://127.0.0.1:9900", client=("127.0.0.1", 50000))
        try:
            response = client.get(f"/api/agents/{slug}/archives/{identity}")
            assert response.status_code == 200
            visible, = [event for event in response.json()["events"]["items"] if event["kind"] == "context_report"]
            assert visible["payload"] == observation.payload
            assert "reports.key" not in response.text and SIGNATURE not in response.text
        finally:
            client.close()
        assert storage.path.read_bytes() == original
    finally:
        worker.stop()


pytestmark = pytest.mark.skipif(not (os.environ.get("STUDIO_TEST_HISTORY_CONFIG") or os.environ.get("STUDIO_TEST_HISTORY_ADMIN_CONFIG")),
                                reason="Requires explicit disposable PostgreSQL config")


@pytest.fixture(scope="module")
def archive():
    psycopg = pytest.importorskip("psycopg")
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    admin_path = os.environ.get("STUDIO_TEST_HISTORY_ADMIN_CONFIG")
    if admin_path:
        from psycopg.sql import SQL, Identifier
        admin_dsn = json.loads(Path(admin_path).read_text())["database_url"]
        admin_settings = conninfo_to_dict(admin_dsn)
        assert (admin_settings.get("host") == "127.0.0.1" and
                admin_settings.get("hostaddr", "127.0.0.1") == "127.0.0.1" and
                admin_settings.get("port") in {"55432", "55433"} and "service" not in admin_settings)
        name = "studio_history_test_" + uuid4().hex
        with psycopg.connect(make_conninfo(admin_dsn, hostaddr="127.0.0.1"), autocommit=True) as admin:
            admin.execute(SQL("CREATE DATABASE {}").format(Identifier(name)))
        config = {"database_url": make_conninfo(admin_dsn, dbname=name, hostaddr="127.0.0.1")}
    else:
        config = json.loads(Path(os.environ["STUDIO_TEST_HISTORY_CONFIG"]).read_text())
    connection = conninfo_to_dict(config["database_url"])
    if (not connection.get("dbname", "").startswith("studio_history_test_")
            or connection.get("host") != "127.0.0.1"
            or connection.get("hostaddr", "127.0.0.1") != "127.0.0.1"
            or "service" in connection):
        pytest.fail("Integration tests require a disposable loopback test database")
    # Explicit hostaddr defeats accidental PGHOSTADDR environment overrides.
    store = PostgresArchive(make_conninfo(config["database_url"], hostaddr="127.0.0.1"))
    store.initialize()
    try:
        yield store
    finally:
        if admin_path:
            with psycopg.connect(make_conninfo(admin_dsn, hostaddr="127.0.0.1"), autocommit=True) as admin:
                admin.execute(SQL("DROP DATABASE {}").format(Identifier(name)))


@pytest.fixture
def slug():
    return "test-" + uuid4().hex


def raw_snapshot(**overrides):
    result = {
        "session_id": "session-1", "status": "done", "prompt": "17 * 23",
        "result": "391", "created": 100.0, "updated_at": 101.0,
        "revision": 3, "duration_ms": 20,
        "session": {"turn": 1, "trace": [
            {"type": "user_input", "id": "u1", "turn": 1, "content": "17 * 23"},
            {"type": "tool_result", "id": "t1", "tool_id": "call-1", "name": "calculate",
             "result": "391", "status": "success", "timing_ms": 1},
            {"type": "turn_result", "id": "r1", "turn": 1, "reason": "natural"},
        ]},
    }
    result.update(overrides)
    return result


def capture(slug, **overrides):
    return prepare_capture(convert_snapshot(raw_snapshot(**overrides), agent_slug=slug))


def test_init_and_import_are_idempotent(archive, slug):
    archive.initialize()
    value = capture(slug)
    assert archive.save(value) is True
    assert archive.save(value) is False
    assert archive.get(value.capture_id) == value
    rows = archive.list_captures(slug)
    assert len(rows) == 1
    assert (rows[0]["turn_count"], rows[0]["event_count"]) == (1, 3)
    assert archive.get("0" * 64) is None


def test_agents_and_sessions_are_isolated(archive, slug):
    values = [capture(slug), capture(slug + "-other"), capture(slug, session_id="session-2")]
    for value in values:
        assert archive.save(value)
    assert len({value.capture_id for value in values}) == 3
    assert {row["session_id"] for row in archive.list_captures(slug)} == {"session-1", "session-2"}
    assert len(archive.list_captures(slug + "-other")) == 1
    assert archive.list_captures("' OR true; --") == []


def test_read_is_independent_of_missing_source(archive, slug, tmp_path, monkeypatch):
    source = tmp_path / "synthetic-source.jsonl"
    source.write_text(json.dumps(raw_snapshot()) + "\n")
    value = prepare_capture(read_execution_history(source, agent_slug=slug).sessions[0])
    assert archive.save(value)
    source.unlink()  # Only the synthetic tmp_path fixture, never user data.
    def no_source(*args, **kwargs):
        raise AssertionError("Archive reads must not fall back to framework history")
    monkeypatch.setattr("co_studio.api.sessions._history", no_source)
    assert archive.get(value.capture_id) == value


def test_new_snapshot_cannot_erase_previous_answer(archive, slug):
    before = capture(slug)
    later = capture(slug, revision=4, result=None, updated_at=102.0)
    assert archive.save(before) and archive.save(later)
    assert archive.get(before.capture_id).turns[0].result == "391"
    assert archive.get(later.capture_id).turns[0].result is None
    assert len(archive.list_captures(slug)) == 2


def test_redaction_is_present_in_actual_database(archive, slug):
    value = capture(slug, result="password=secret-archive-value")
    assert archive.save(value)
    with archive._connect() as conn:
        row = conn.execute("SELECT data FROM studio_history.turns WHERE capture_id=%s",
                           (value.capture_id,)).fetchone()
    assert "secret-archive-value" not in json.dumps(row)
    assert "[REDACTED]" in json.dumps(row)


def test_child_write_failure_rolls_back_entire_capture(archive, slug):
    converted = convert_snapshot(raw_snapshot(), agent_slug=slug)
    # Valid normalized non-negative sequence, but beyond PostgreSQL integer.
    # The DB rejects the child insert AFTER the parent and turn inserts.
    events = (*converted.events[:-1], converted.events[-1].model_copy(update={"sequence": 2**40}))
    bad = prepare_capture(replace(converted, events=events))
    with pytest.raises(ArchiveError, match="transaction rolled back"):
        archive.save(bad)
    assert archive.get(bad.capture_id) is None
    assert archive.list_captures(slug) == []
    with archive._connect() as conn:
        assert conn.execute("SELECT count(*) FROM studio_history.turns WHERE capture_id=%s",
                            (bad.capture_id,)).fetchone()[0] == 0
    assert archive.save(capture(slug))  # Error did not poison a pooled/global connection.


def test_concurrent_repeat_imports_insert_only_once(archive, slug):
    value = capture(slug)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: archive.save(value), range(4)))
    assert results.count(True) == 1 and results.count(False) == 3
    assert archive.get(value.capture_id) == value
    assert len(archive.list_captures(slug)) == 1


def test_tampered_json_is_not_silently_returned(archive, slug):
    value = capture(slug)
    archive.save(value)
    with archive._connect() as conn:
        conn.execute("""UPDATE studio_history.turns
            SET data=jsonb_set(data, '{result}', '"tampered"'::jsonb)
            WHERE capture_id=%s""", (value.capture_id,))
    with pytest.raises(ArchiveError, match="fingerprint"):
        archive.get(value.capture_id)


def test_incomplete_capture_is_reported(archive, slug):
    value = capture(slug)
    archive.save(value)
    with archive._connect() as conn:
        conn.execute("DELETE FROM studio_history.events WHERE capture_id=%s AND sequence=1",
                     (value.capture_id,))
    with pytest.raises(ArchiveError, match="incomplete"):
        archive.get(value.capture_id)


def test_pagination_has_stable_explicit_boundaries(archive, slug):
    for revision in range(3):
        archive.save(capture(slug, revision=revision))
    all_rows = archive.list_captures(slug)
    assert len(all_rows) == 3
    assert archive.list_captures(slug, limit=1, offset=1) == all_rows[1:2]
    assert archive.list_captures(slug, offset=3) == []


@pytest.mark.parametrize("number", [-0.0, 1e20, 1.2345678901234567e20, 1e-7, 2**80 + 1])
def test_jsonb_numeric_spelling_does_not_break_fingerprint(archive, slug, number):
    raw = raw_snapshot()
    raw["session"]["trace"][1]["result"] = {"nested": [number, True, "1"]}
    value = prepare_capture(convert_snapshot(raw, agent_slug=slug))
    assert archive.save(value)
    restored = archive.get(value.capture_id)
    assert restored.capture_id == value.capture_id
    assert archive.save(restored) is False


def test_archive_page_counts_versions_and_scopes_owned_reads(archive, slug):
    first = capture(slug)
    later = capture(slug, revision=4, result="updated")
    other = capture(slug + "-other")
    for value in (first, later, other):
        archive.save(value)
    total, items = archive.archive_page(slug, limit=1)
    assert total == 2 and len(items) == 1
    assert items[0]["capture_id"] == later.capture_id
    assert items[0]["session"] == later.session
    assert items[0]["archived_at"]
    total, next_items = archive.archive_page(slug, limit=1, offset=1)
    assert total == 2 and next_items[0]["capture_id"] == first.capture_id
    assert archive.archive_page(slug, offset=2) == (2, [])
    assert archive.get_owned(slug + "-other", first.capture_id) is None
    assert archive.get_owned(slug, first.capture_id)["capture"] == first
    assert archive.get_owned(slug, first.capture_id)["archived_at"] == next_items[0]["archived_at"]


def test_session_page_aggregates_versions_and_never_promotes_a_late_old_import(archive, slug):
    current = capture(slug, revision=12, updated_at=112.0)
    late_old = capture(slug, revision=20, updated_at=100.0)
    second_session = capture(slug, session_id="session-2", updated_at=300.0)
    foreign = capture(slug + "-other", revision=999)
    for value in (current, late_old, second_session, foreign):
        archive.save(value)
    total, rows = archive.session_page(slug)
    assert total == 2
    assert [row["capture_id"] for row in rows] == [second_session.capture_id, current.capture_id]
    assert [row["version_count"] for row in rows] == [1, 2]
    assert all(row["selection_warning"] is None for row in rows)
    total, versions = archive.versions_page(slug, "session-1")
    assert total == 2 and [row["capture_id"] for row in versions] == [current.capture_id, late_old.capture_id]
    assert all(row["version_count"] == 2 and "selection_warning" not in row for row in versions)
    assert archive.session_page("' OR true; --") == (0, [])
    assert archive.versions_page(slug, "' OR true; --") == (0, [])
    assert archive.versions_page(slug + "-other", "session-2") == (0, [])
    assert archive.get_owned(slug, foreign.capture_id) is None


@pytest.mark.parametrize("import_old_last", [False, True])
def test_session_page_prefers_newer_source_time_after_revision_reset(archive, slug, import_old_last):
    old = capture(slug, revision=20, updated_at=101.0, result="old answer")
    latest = capture(slug, revision=1, updated_at=102.0, result="new answer")
    for value in ((latest, old) if import_old_last else (old, latest)):
        archive.save(value)
    total, rows = archive.session_page(slug)
    assert total == 1 and rows[0]["capture_id"] == latest.capture_id
    assert rows[0]["version_count"] == 2 and rows[0]["selection_warning"] is None
    total, versions = archive.versions_page(slug, "session-1")
    assert total == 2
    assert [row["capture_id"] for row in versions] == [latest.capture_id, old.capture_id]
    assert archive.get_owned(slug, old.capture_id)["capture"].turns[0].result == "old answer"


@pytest.mark.parametrize("issue", ["invalid_trace", "invalid_event", "invalid_turn"])
def test_session_page_selects_earlier_readable_capture_and_preserves_broken_version(archive, slug, issue):
    readable = capture(slug)
    converted = convert_snapshot(raw_snapshot(revision=4, updated_at=102.0), agent_slug=slug)
    broken = prepare_capture(replace(converted, issues=(ReadIssue(issue),)))
    archive.save(readable)
    archive.save(broken)
    total, rows = archive.session_page(slug)
    assert total == 1 and rows[0]["capture_id"] == readable.capture_id
    assert rows[0]["version_count"] == 2
    assert "earlier readable version" in rows[0]["selection_warning"]
    total, versions = archive.versions_page(slug, "session-1")
    assert total == 2
    assert {row["capture_id"] for row in versions} == {readable.capture_id, broken.capture_id}
    assert archive.get_owned(slug, broken.capture_id)["capture"] == broken


def test_session_page_keeps_latest_when_every_version_has_structural_issues(archive, slug):
    converted = convert_snapshot(raw_snapshot(revision=1, updated_at=102.0), agent_slug=slug)
    latest = prepare_capture(replace(converted, issues=(ReadIssue("invalid_event"),)))
    converted = convert_snapshot(raw_snapshot(revision=20, updated_at=101.0), agent_slug=slug)
    older = prepare_capture(replace(converted, issues=(ReadIssue("invalid_trace"),)))
    archive.save(latest)
    archive.save(older)
    total, rows = archive.session_page(slug)
    assert total == 1 and rows[0]["capture_id"] == latest.capture_id
    assert rows[0]["selection_warning"].startswith("All archived versions have structural issues")


def test_session_page_accepts_unknown_event_kind_and_compaction_without_merging_events(archive, slug):
    original = capture(slug)
    raw = raw_snapshot(revision=4, updated_at=102.0, result="compacted answer")
    raw["session"]["trace"] = raw["session"]["trace"][:1]
    converted = convert_snapshot(raw, agent_slug=slug)
    compacted = prepare_capture(replace(converted, issues=(ReadIssue("unknown_event_kind"),)))
    archive.save(original)
    archive.save(compacted)
    total, rows = archive.session_page(slug)
    assert total == 1 and rows[0]["capture_id"] == compacted.capture_id
    assert rows[0]["version_count"] == 2 and rows[0]["selection_warning"] is None
    restored_old = archive.get_owned(slug, original.capture_id)["capture"]
    restored_new = archive.get_owned(slug, compacted.capture_id)["capture"]
    assert restored_old == original and restored_new == compacted
    assert len(restored_old.events) == 3 and len(restored_new.events) == 1
    assert restored_old.turns[0].result == "391"
    # Without the matching completion event, the compacted capture cannot
    # establish an answer; neither its top-level result nor an older capture
    # may fill in missing turn evidence.
    assert restored_new.turns[0].result is None


def test_session_and_version_pages_have_separate_stable_boundaries(archive, slug):
    for session_no in range(3):
        for revision in range(4):
            archive.save(capture(slug, session_id=f"session-{session_no}", revision=revision,
                                 updated_at=float(100 + session_no)))
    total, sessions = archive.session_page(slug)
    assert total == 3 and len(sessions) == 3
    assert archive.session_page(slug, limit=1, offset=1) == (3, sessions[1:2])
    assert archive.session_page(slug, offset=3) == (3, [])
    total, versions = archive.versions_page(slug, "session-1")
    assert total == 4 and len(versions) == 4
    assert archive.versions_page(slug, "session-1", limit=2, offset=1) == (4, versions[1:3])
    assert archive.versions_page(slug, "session-1", offset=4) == (4, [])


def test_real_postgres_http_reads_stay_pinned_without_a_source_file(archive, slug, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from co_studio import config, registry
    from co_studio.app import create_app
    from co_studio.api import archives, sessions

    root = tmp_path / "agents"
    root.mkdir()
    (root / slug).mkdir()
    monkeypatch.setattr(config, "AGENTS_DIR", root)
    registry.save(registry.AgentMeta(slug=slug, name="Synthetic archive", address="0xsynthetic",
                                    port=9911, model="offline", capabilities=[], created_at="2026-10-27"))
    monkeypatch.setattr(archives, "_archive", lambda: archive)
    def no_file(*args, **kwargs):
        raise AssertionError("Archive HTTP request must not read framework history")
    monkeypatch.setattr(sessions, "_history", no_file)
    first = capture(slug)
    archive.save(first)
    client = TestClient(create_app(), base_url="http://127.0.0.1:9900", client=("127.0.0.1", 50000))
    try:
        listed = client.get(f"/api/agents/{slug}/archives").json()
        identity = listed["sessions"]["items"][0]["capture_id"]
        archive.save(capture(slug, revision=4, result="newer answer"))
        detail = client.get(f"/api/agents/{slug}/archives/{identity}?event_offset=1&event_limit=1")
        assert detail.status_code == 200
        data = detail.json()
        assert data["source"] == "archive" and data["capture_id"] == first.capture_id
        assert data["turns"]["items"][0]["result"] == "391"
        assert data["events"]["total"] == 3 and data["events"]["items"][0]["sequence"] == 1
        assert data["events"]["next_offset"] == 2
        assert detail.headers["cache-control"] == "no-store"
        assert not (root / slug / ".co" / "session_results.jsonl").exists()
    finally:
        client.close()  # No lifespan or real Agent processes.


def test_real_postgres_unified_history_reads_sessions_versions_and_pinned_details(
    archive, slug, tmp_path, monkeypatch,
):
    from types import SimpleNamespace

    from fastapi.testclient import TestClient
    from co_studio import config, registry
    from co_studio.app import create_app
    from co_studio.api import archives, sessions

    root = tmp_path / "agents"
    root.mkdir()
    monkeypatch.setattr(config, "AGENTS_DIR", root)
    for agent_slug in (slug, slug + "-other"):
        (root / agent_slug).mkdir()
        registry.save(registry.AgentMeta(
            slug=agent_slug, name="Synthetic history", address="0xsynthetic", port=9911,
            model="offline", capabilities=[], created_at="2026-10-28",
        ))
    # The env selects archive mode, but the injected factory is the only store
    # used. No production database settings or framework source files are read.
    monkeypatch.delenv("STUDIO_HISTORY_CONFIG", raising=False)
    monkeypatch.setenv("STUDIO_HISTORY_DATABASE_URL", "postgresql://synthetic.invalid/unused")
    monkeypatch.setattr(archives, "_archive", lambda: archive)
    seed_capture = archive.save
    first = capture(slug)
    current = capture(slug, revision=4, updated_at=102.0, result="current answer")
    foreign = capture(slug + "-other", session_id="foreign-session")
    for value in (first, current, foreign):
        seed_capture(value)

    def forbidden(*args, **kwargs):
        raise AssertionError("History GET must not read source files, write, initialize, or start sync")

    for name in ("_history", "_history_source", "read_execution_history"):
        monkeypatch.setattr(sessions, name, forbidden)
    monkeypatch.setattr(archive, "save", forbidden)
    monkeypatch.setattr(archive, "initialize", forbidden)
    status_calls = []

    def sync_status(agent_slug):
        status_calls.append(agent_slug)
        return {"state": "ok", "checked_at": "2026-10-28T10:00:00+00:00",
                "synced_at": "2026-10-28T09:59:00+00:00", "message": None}

    app = create_app()
    app.state.history_sync = SimpleNamespace(status=sync_status, start=forbidden, stop=forbidden)
    # Do not enter TestClient as a context manager: this test owns no app
    # lifespan, background sync, Agent process, browser, or real history source.
    client = TestClient(app, base_url="http://127.0.0.1:9900", client=("127.0.0.1", 50000))
    try:
        response = client.get(f"/api/agents/{slug}/history", params={"limit": 1, "source": "file"})
        assert response.status_code == 200
        listed = response.json()
        assert listed["source"] == "archive" and listed["source_exists"] is True
        assert listed["sync"]["state"] == "ok"
        assert listed["sync"]["synced_at"] == "2026-10-28T09:59:00+00:00"
        assert listed["sessions"]["total"] == 1 and listed["sessions"]["next_offset"] is None
        assert len(listed["sessions"]["items"]) == 1
        selected = listed["sessions"]["items"][0]
        assert selected["capture_id"] == current.capture_id
        assert selected["session_id"] == "session-1" and selected["version_count"] == 2
        assert selected["selection_warning"] is None
        assert response.headers["cache-control"] == "no-store"

        response = client.get(f"/api/agents/{slug}/history/versions",
                              params={"session_id": "session-1", "limit": 1})
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        versions = response.json()["versions"]
        assert versions["total"] == 2 and versions["next_offset"] == 1
        assert versions["items"][0]["capture_id"] == current.capture_id
        response = client.get(f"/api/agents/{slug}/history/versions",
                              params={"session_id": "session-1", "limit": 1, "offset": 1})
        assert response.status_code == 200
        assert response.json()["versions"]["items"][0]["capture_id"] == first.capture_id
        assert response.json()["versions"]["next_offset"] is None
        response = client.get(f"/api/agents/{slug}/history/versions", params={"session_id": "foreign-session"})
        assert response.status_code == 200 and response.json()["versions"]["total"] == 0
        assert client.get(f"/api/agents/{slug}/archives/{foreign.capture_id}").status_code == 404

        # Simulate another completed background import between list and detail;
        # the HTTP-accessible save method remains forbidden throughout.
        newest = capture(slug, revision=5, updated_at=103.0, result="newest answer")
        seed_capture(newest)
        refreshed = client.get(f"/api/agents/{slug}/history").json()["sessions"]
        assert refreshed["total"] == 1
        assert refreshed["items"][0]["capture_id"] == newest.capture_id
        assert refreshed["items"][0]["version_count"] == 3

        detail = client.get(f"/api/agents/{slug}/archives/{selected['capture_id']}",
                            params={"event_offset": 1, "event_limit": 1})
        assert detail.status_code == 200 and detail.headers["cache-control"] == "no-store"
        body = detail.json()
        assert body["capture_id"] == current.capture_id and body["source"] == "archive"
        assert body["session"]["source_revision"] == 4
        assert body["turns"]["items"][0]["result"] == "current answer"
        assert body["events"]["total"] == 3 and body["events"]["items"][0]["sequence"] == 1
        assert body["events"]["next_offset"] == 2
        original = client.get(f"/api/agents/{slug}/archives/{first.capture_id}").json()
        assert original["turns"]["items"][0]["result"] == "391"
        assert status_calls == [slug, slug]
        assert not (root / slug / ".co").exists()
    finally:
        client.close()


@pytest.fixture
def sse_postgres(archive, slug, tmp_path, monkeypatch):
    """A synthetic registry and real disposable archive, without app lifespan."""
    from types import SimpleNamespace

    from fastapi.testclient import TestClient
    from co_studio import config, registry
    from co_studio.api import archives, sessions
    from co_studio.app import create_app

    root = tmp_path / "sse-agents"
    directory = root / slug
    (directory / ".co").mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTS_DIR", root)
    registry.save(registry.AgentMeta(
        slug=slug, name="Synthetic SSE history", address="0xsynthetic", port=9911,
        model="offline", capabilities=[], created_at="2026-10-28",
    ))
    source = directory / ".co" / "session_results.jsonl"
    source.write_text(json.dumps(raw_snapshot()) + "\n")
    monkeypatch.delenv("STUDIO_HISTORY_CONFIG", raising=False)
    monkeypatch.setenv("STUDIO_HISTORY_DATABASE_URL", "postgresql://synthetic.invalid/unused")
    # The fixture's gated test database is the only connection factory used.
    monkeypatch.setattr(archives, "_archive", lambda: archive)

    def no_fallback(*args, **kwargs):
        raise AssertionError("Archive History GET must not read source history")

    monkeypatch.setattr(sessions, "_history", no_fallback)
    app = create_app()
    client = TestClient(app, base_url="http://127.0.0.1:9900", client=("127.0.0.1", 50000))
    try:
        yield SimpleNamespace(archive=archive, slug=slug, source=source, app=app, client=client)
    finally:
        client.close()


async def _drain_history_notifications(subscription):
    """No producer remains active when this helper is called."""
    result = []
    while (event := await subscription.next(timeout=0.02)) is not None:
        result.append(event)
    return result


def test_postgres_notifications_follow_commit_skip_duplicates_and_replay(sse_postgres):
    import asyncio

    from co_studio.history_events import HistoryEventHub
    from co_studio.history_sync import HistorySync

    fixture = sse_postgres

    async def exercise():
        hub = HistoryEventHub()
        subscription = hub.subscribe(fixture.slug)
        observations = []
        current_time = [0.0]

        def publish_committed(agent_slug, session_id, capture_id):
            # A separate HTTP/DB read must see the committed transaction before
            # the notification enters the hub. Callback exceptions are isolated
            # by HistorySync, so assertions about these observations run below.
            listed = fixture.client.get(f"/api/agents/{agent_slug}/history")
            detail = fixture.client.get(f"/api/agents/{agent_slug}/archives/{capture_id}")
            observations.append((session_id, capture_id, listed.status_code, listed.json(),
                                 detail.status_code, detail.json()))
            hub.publish_capture(agent_slug, session_id, capture_id)

        worker = HistorySync(
            lambda: fixture.archive, on_capture=publish_committed, on_status=hub.publish_status,
            monotonic=lambda: current_time[0], reconcile_interval=1.0,
        )
        fixture.app.state.history_sync = worker
        fixture.app.state.history_events = hub
        replay = resumed = None
        try:
            initial = await subscription.next(timeout=0.1)
            assert initial.event == "history.reset" and initial.data["reason"] == "initial"
            original = fixture.source.read_bytes()
            await asyncio.to_thread(worker.scan_once)
            first_events = await _drain_history_notifications(subscription)
            first_changes = [event for event in first_events if event.event == "history.changed"]
            assert len(first_changes) == 1
            first = first_changes[0]
            assert dict(first.data) == {
                "agent_slug": fixture.slug, "session_id": "session-1",
                "capture_id": capture(fixture.slug).capture_id,
            }
            assert worker.status(fixture.slug)["state"] == "ok"
            assert fixture.source.read_bytes() == original

            # Force the worker's periodic idempotent save into PostgreSQL, not
            # merely its unchanged-file fast path. Duplicate inserts are silent.
            current_time[0] = 1.0
            await asyncio.to_thread(worker.scan_once)
            duplicate_events = await _drain_history_notifications(subscription)
            assert not any(event.event == "history.changed" for event in duplicate_events)
            assert len(observations) == 1
            assert fixture.archive.session_page(fixture.slug)[1][0]["version_count"] == 1

            # The client disconnects; a later committed version remains in the
            # bounded hub and can be resumed with the first capture's cursor.
            subscription.close()
            updated = raw_snapshot(revision=4, updated_at=102.0, result="400")
            fixture.source.write_text(json.dumps(updated) + "\n")
            current_time[0] = 2.0
            await asyncio.to_thread(worker.scan_once)
            replay = hub.subscribe(fixture.slug, last_event_id=first.id)
            replayed = await _drain_history_notifications(replay)
            assert not any(event.event == "history.reset" for event in replayed)
            changes = [event for event in replayed if event.event == "history.changed"]
            assert len(changes) == 1
            later_id = capture(fixture.slug, revision=4, updated_at=102.0, result="400").capture_id
            assert changes[0].data["capture_id"] == later_id
            assert changes[0].id != first.id
            assert len(observations) == 2
            for index, expected_id in enumerate((first.data["capture_id"], later_id)):
                session_id, capture_id, list_status, listed, detail_status, detail = observations[index]
                assert session_id == "session-1" and capture_id == expected_id
                assert list_status == detail_status == 200
                assert listed["sessions"]["total"] == 1
                assert listed["sessions"]["items"][0]["capture_id"] == expected_id
                assert listed["sessions"]["items"][0]["version_count"] == index + 1
                assert detail["capture_id"] == expected_id
                assert detail["turns"]["items"][0]["result"] == ("391", "400")[index]

            cursor = replayed[-1].id
            replay.close()
            resumed = hub.subscribe(fixture.slug, last_event_id=cursor)
            assert await resumed.next(timeout=0.02) is None
        finally:
            worker.stop()
            subscription.close()
            if replay is not None:
                replay.close()
            if resumed is not None:
                resumed.close()
            hub.close()
            assert hub.subscriber_count == 0

    asyncio.run(exercise())


def test_postgres_rolled_back_save_emits_no_change_and_retry_recovers(sse_postgres):
    import asyncio

    from co_studio.execution_adapter import read_execution_stream
    from co_studio.history_events import HistoryEventHub
    from co_studio.history_sync import HistorySync

    fixture = sse_postgres

    async def exercise():
        hub = HistoryEventHub()
        subscription = hub.subscribe(fixture.slug)
        corrupt_child = [True]
        failed_ids = []
        current_time = [0.0]

        def reader(handle, **kwargs):
            history = read_execution_stream(handle, **kwargs)
            if not corrupt_child[0]:
                return history
            converted = history.sessions[0]
            # PostgreSQL rejects this integer AFTER parent/turn inserts. This
            # exercises a real rollback, not a mocked unavailable writer.
            events = (*converted.events[:-1],
                      converted.events[-1].model_copy(update={"sequence": 2**40}))
            invalid = replace(converted, events=events)
            failed_ids.append(prepare_capture(invalid).capture_id)
            return replace(history, sessions=(invalid,))

        worker = HistorySync(
            lambda: fixture.archive, reader=reader, monotonic=lambda: current_time[0],
            on_capture=hub.publish_capture, on_status=hub.publish_status,
        )
        fixture.app.state.history_sync = worker
        fixture.app.state.history_events = hub
        try:
            assert (await subscription.next(timeout=0.1)).event == "history.reset"
            original = fixture.source.read_bytes()
            await asyncio.to_thread(worker.scan_once)
            events = await _drain_history_notifications(subscription)
            assert not any(event.event == "history.changed" for event in events)
            assert any(event.event == "history.sync" and event.data["state"] == "error"
                       for event in events)
            assert worker.status(fixture.slug)["state"] == "error"
            assert len(failed_ids) == 1 and fixture.archive.get(failed_ids[0]) is None
            with fixture.archive._connect() as conn:
                assert conn.execute("SELECT count(*) FROM studio_history.turns WHERE capture_id=%s",
                                    (failed_ids[0],)).fetchone()[0] == 0
            listed = fixture.client.get(f"/api/agents/{fixture.slug}/history")
            assert listed.status_code == 200
            assert listed.json()["sessions"]["total"] == 0
            assert listed.json()["sync"]["state"] == "error"
            assert fixture.source.read_bytes() == original

            # Retry the same synthetic source after backoff. A successful real
            # commit produces exactly one change and is immediately readable.
            corrupt_child[0] = False
            current_time[0] = 2.0
            await asyncio.to_thread(worker.scan_once)
            recovered = await _drain_history_notifications(subscription)
            changed = [event for event in recovered if event.event == "history.changed"]
            assert len(changed) == 1 and worker.status(fixture.slug)["state"] == "ok"
            expected = capture(fixture.slug).capture_id
            assert changed[0].data["capture_id"] == expected
            detail = fixture.client.get(f"/api/agents/{fixture.slug}/archives/{expected}")
            assert detail.status_code == 200
            assert detail.json()["turns"]["items"][0]["result"] == "391"
            assert fixture.archive.session_page(fixture.slug)[1][0]["version_count"] == 1
        finally:
            worker.stop()
            subscription.close()
            hub.close()
            assert hub.subscriber_count == 0

    asyncio.run(exercise())
