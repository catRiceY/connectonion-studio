"""Automatic capture regressions using only synthetic files and an in-memory store."""

from datetime import datetime, timezone
import json
from pathlib import Path
import threading

import pytest

from co_studio import config, history_sync, registry
from co_studio.execution_adapter import read_execution_history, read_execution_stream
from co_studio.history_archive import ArchiveError
from co_studio.history_sync import HistorySync


class Clock:
    value = 0.0

    def monotonic(self):
        return self.value

    def now(self):
        return datetime.fromtimestamp(self.value, timezone.utc)


class MemoryArchive:
    def __init__(self):
        self.calls = []
        self.captures = {}
        self.error = None
        self.on_save = None

    def save(self, capture):
        self.calls.append(capture)
        if self.on_save:
            self.on_save(capture)
        if self.error:
            raise self.error
        created = capture.capture_id not in self.captures
        self.captures[capture.capture_id] = capture
        return created


def snapshot(session_id="session-1", *, revision=1, prompt="synthetic question", result="synthetic answer"):
    return {
        "session_id": session_id, "status": "done", "prompt": prompt,
        "result": result, "revision": revision, "created": 100.0,
        "updated_at": 100.0 + revision, "duration_ms": 25.0,
        "session": {"session_id": session_id, "turn": 1, "trace": [
            {"type": "user_input", "id": "user-1", "turn": 1, "content": prompt},
            {"type": "turn_result", "id": "result-1", "turn": 1, "reason": "natural"},
        ]},
    }


@pytest.fixture
def agents(tmp_path, monkeypatch):
    root = tmp_path / "agents"
    root.mkdir()
    monkeypatch.setattr(config, "AGENTS_DIR", root)

    def create(slug="demo", rows=None):
        directory = root / slug
        directory.mkdir()
        registry.save(registry.AgentMeta(
            slug=slug, name="Synthetic agent", address="synthetic", port=9911,
            model="offline", capabilities=[], created_at="2026-10-27T00:00:00Z",
        ))
        source = directory / ".co" / "session_results.jsonl"
        if rows is not None:
            source.parent.mkdir()
            source.write_text("".join(json.dumps(row) + "\n" for row in rows))
        return source

    return create


@pytest.fixture
def setup_sync():
    archive, clock = MemoryArchive(), Clock()
    sync = HistorySync(lambda: archive, monotonic=clock.monotonic, clock=clock.now)
    yield sync, archive, clock
    sync.stop()


def test_startup_catchup_caches_then_captures_changed_source(agents, setup_sync):
    source = agents(rows=[snapshot(), snapshot(revision=2)])
    before = source.read_bytes(), source.stat().st_mtime_ns
    sync, archive, clock = setup_sync
    sync.scan_once()
    assert len(archive.calls) == 1
    assert archive.calls[0].session.source_revision == 2
    assert sync.status("demo") == {
        "state": "ok", "checked_at": clock.now().isoformat(),
        "synced_at": clock.now().isoformat(), "message": None,
    }
    sync.scan_once()
    assert len(archive.calls) == 1
    assert (source.read_bytes(), source.stat().st_mtime_ns) == before
    source.write_text(json.dumps(snapshot(revision=3)) + "\n")
    clock.value = 2
    sync.scan_once()
    assert len(archive.calls) == len(archive.captures) == 2
    assert archive.calls[-1].session.source_revision == 3


def test_sync_verifies_prompt_proof_before_archiving_and_tamper_is_unknown(agents, setup_sync):
    from co_studio import prompt_provenance
    from co_studio.api.sessions import _public_turns

    source = agents(rows=[])
    prompt_dir = source.parent.parent / ".studio-prompts"
    prompt_dir.mkdir()
    key = bytes.fromhex("1f" * 32)
    key_path = prompt_dir / "provenance.key"
    key_path.write_text(key.hex())
    key_path.chmod(0o600)
    event = {
        "type": "prompt_provenance", "id": "proof-1", "turn": 1,
        "provenance_version": 1, "status": "bound", "version_id": "a" * 32,
        "base_sha256": "b" * 64, "system_sha256": "c" * 64,
        "launch_version_id": "a" * 32, "llm_call_id": "call-1", "model": "offline",
        "reason": "new_session",
    }
    event["_studio_signature"] = prompt_provenance._sign(key, "studio-prompt-event-v1", {
        "agent_slug": "demo", "session_id": "session-1", "event": event,
    })
    row = snapshot()
    row["session"]["trace"][1:1] = [
        {"type": "llm_call", "id": "call-1", "model": "offline"}, event,
        {"type": "llm_result", "id": "call-1", "model": "offline", "status": "success"},
    ]
    source.write_text(json.dumps(row) + "\n")
    original = source.read_bytes()
    sync, archive, clock = setup_sync
    sync.scan_once()
    first = archive.calls[0]
    proof = _public_turns(first.turns, first.events)[0].prompt_provenance
    assert proof.status == "bound" and proof.version_id == "a" * 32
    assert "_studio_signature" not in repr(first)
    assert key.hex() not in repr(first)
    assert source.read_bytes() == original
    sync.scan_once()
    assert len(archive.calls) == 1

    event["version_id"] = "d" * 32  # Valid shape, but not an authentic observation.
    source.write_text(json.dumps(row) + "\n")
    clock.value = 2
    sync.scan_once()
    second = archive.calls[-1]
    assert len(archive.calls) == 2
    assert _public_turns(second.turns, second.events)[0].prompt_provenance.status == "unknown"
    assert _public_turns(first.turns, first.events)[0].prompt_provenance.version_id == "a" * 32


def test_legacy_sync_never_reads_or_creates_prompt_key(agents, setup_sync, monkeypatch):
    from co_studio import prompt_provenance

    def forbidden(*args):
        raise AssertionError("Legacy traces must not inspect prompt storage")

    source = agents(rows=[snapshot()])
    monkeypatch.setattr(prompt_provenance, "event_verifier", forbidden)
    sync, archive, _ = setup_sync
    sync.scan_once()
    assert len(archive.calls) == 1 and sync.status("demo")["state"] == "ok"
    assert not (source.parent.parent / ".studio-prompts").exists()


def test_unchanged_success_skips_parser_but_periodic_capture_recovers_database(agents, setup_sync):
    agents(rows=[snapshot()])
    sync, archive, clock = setup_sync
    reads = []

    def reader(*args, **kwargs):
        reads.append(1)
        return read_execution_stream(*args, **kwargs)

    sync._reader = reader
    sync.scan_once()
    archive.captures.clear()  # A restored/recreated database needs catch-up.
    clock.value = 59
    sync.scan_once()
    assert len(reads) == len(archive.calls) == 1
    clock.value = 60
    sync.scan_once()
    assert len(reads) == len(archive.calls) == 2
    assert len(archive.captures) == 1
    clock.value = 120
    sync.scan_once()
    assert len(archive.calls) == 3 and len(archive.captures) == 1


def test_database_failure_retries_unchanged_source_with_capped_backoff(agents, setup_sync, caplog):
    agents(rows=[snapshot()])
    sync, archive, clock = setup_sync
    archive.error = ArchiveError("postgres://secret:password@private.example/secret-database")
    previous_synced = None
    for index, (when, delay) in enumerate([(0, 2), (2, 4), (6, 8), (14, 16), (30, 32), (62, 60), (122, 60)]):
        clock.value = when
        sync.scan_once()
        assert len(archive.calls) == index + 1
        assert sync.status("demo")["state"] == "error"
        assert sync.status("demo")["synced_at"] == previous_synced
        clock.value = when + delay - 0.1
        sync.scan_once()
        assert len(archive.calls) == index + 1
    archive.error = None
    clock.value = 182
    sync.scan_once()
    assert sync.status("demo")["state"] == "ok"
    assert len(archive.calls) == 8 and len(archive.captures) == 1
    assert "secret" not in caplog.text
    assert "postgres" not in str(sync.status("demo"))


def test_factory_failure_is_visible_and_never_marks_success(agents, setup_sync):
    agents(rows=[snapshot()])
    sync, archive, clock = setup_sync

    def bad_factory():
        raise ValueError("sensitive config body")

    sync._archive_factory = bad_factory
    sync.scan_once()
    assert sync.status("demo")["state"] == "error"
    assert sync.status("demo")["synced_at"] is None
    assert "sensitive" not in str(sync.status("demo"))
    sync._archive_factory = lambda: archive
    clock.value = 2
    sync.scan_once()
    assert len(archive.captures) == 1


def test_agents_are_isolated_from_invalid_metadata_and_write_failure(agents, setup_sync):
    bad = agents("bad-meta", rows=[snapshot()])
    (bad.parents[1] / "meta.json").write_text("[]")
    agents("bad-write", rows=[snapshot()])
    agents("good", rows=[snapshot()])
    sync, archive, _ = setup_sync

    def per_agent_error(capture):
        if capture.session.agent_slug == "bad-write":
            raise RuntimeError("do not expose captured private text")

    archive.on_save = per_agent_error
    sync.scan_once()
    assert sync.status("bad-meta")["state"] == "error"
    assert sync.status("bad-write")["state"] == "error"
    assert sync.status("good")["state"] == "ok"
    assert {item.session.agent_slug for item in archive.captures.values()} == {"good"}


def test_corrupt_tail_warns_while_valid_session_is_captured(agents, setup_sync, caplog):
    source = agents(rows=[snapshot()])
    source.write_bytes(source.read_bytes() + b'{"secret-tail":')
    sync, archive, _ = setup_sync
    sync.scan_once()
    assert len(archive.captures) == 1
    assert sync.status("demo")["state"] == "warning"
    assert "1 read issue" in sync.status("demo")["message"]
    assert "secret-tail" not in str(sync.status("demo")) + caplog.text
    sync.scan_once()
    assert len(archive.calls) == 1
    assert sync.status("demo")["state"] == "warning"


def test_source_missing_does_not_delete_archived_data(agents, setup_sync):
    source = agents(rows=[snapshot()])
    sync, archive, _ = setup_sync
    sync.scan_once()
    before = dict(archive.captures)
    source.unlink()
    sync.scan_once()
    assert sync.status("demo")["state"] == "warning"
    assert archive.captures == before and len(archive.calls) == 1


def test_absent_source_does_not_create_files_or_archive(agents, setup_sync):
    source = agents()
    sync, archive, _ = setup_sync
    sync.scan_once()
    assert sync.status("demo")["state"] == "ok"
    assert sync.status("demo")["synced_at"] is None
    assert sync.status("demo")["message"] is None
    assert not source.parent.exists() and archive.calls == []


def test_source_disappearing_after_failed_first_archive_still_warns(agents, setup_sync):
    source = agents(rows=[snapshot()])
    sync, archive, _ = setup_sync
    archive.error = ArchiveError("synthetic database failure")
    sync.scan_once()
    assert sync.status("demo")["state"] == "error"
    assert sync.status("demo")["synced_at"] is None
    assert not archive.captures
    source.unlink()
    sync.scan_once()
    assert sync.status("demo")["state"] == "warning"
    assert "source is missing" in sync.status("demo")["message"]
    assert sync.status("demo")["synced_at"] is None
    assert len(archive.calls) == 1


def test_oversized_file_is_rejected_explicitly_without_reading(agents, setup_sync):
    source = agents(rows=[snapshot()])
    with source.open("r+b") as handle:
        handle.truncate(history_sync.MAX_SOURCE_BYTES + 1)
    sync, archive, _ = setup_sync
    sync._reader = lambda *args, **kwargs: pytest.fail("oversized history must not be parsed")
    sync.scan_once()
    assert archive.calls == []
    assert sync.status("demo")["state"] == "error"
    assert "67,108,864-byte" in sync.status("demo")["message"]


@pytest.mark.parametrize("change", ["append", "replace", "remove"])
def test_source_changes_during_read_remain_pending_until_a_stable_retry(agents, setup_sync, change):
    source = agents(rows=[snapshot()])
    sync, archive, _ = setup_sync

    def racing_reader(*args, **kwargs):
        result = read_execution_stream(*args, **kwargs)
        if change == "append":
            with source.open("a") as handle:
                handle.write(json.dumps(snapshot(revision=2)) + "\n")
        elif change == "replace":
            replacement = source.with_suffix(".replacement")
            replacement.write_text(json.dumps(snapshot(revision=2)) + "\n")
            replacement.replace(source)
        else:
            source.unlink()
        return result

    sync._reader = racing_reader
    sync.scan_once()
    assert archive.calls == []
    assert sync.status("demo")["state"] == "pending"
    assert sync.status("demo")["synced_at"] is None
    sync._reader = read_execution_stream
    if change == "remove":
        source.write_text(json.dumps(snapshot(revision=2)) + "\n")
    sync.scan_once()
    assert sync.status("demo")["state"] == "ok"
    assert archive.calls[0].session.source_revision == 2


def test_change_during_save_does_not_cache_success_signature(agents, setup_sync):
    source = agents(rows=[snapshot()])
    sync, archive, _ = setup_sync
    archive.on_save = lambda capture: source.write_text(json.dumps(snapshot(revision=2)) + "\n")
    sync.scan_once()
    assert len(archive.captures) == 1
    assert sync.status("demo")["state"] == "pending"
    archive.on_save = None
    sync.scan_once()
    assert len(archive.captures) == 2
    assert sync.status("demo")["state"] == "ok"


@pytest.mark.parametrize("component", ["source", "co", "agent", "metadata"])
def test_linked_paths_are_rechecked_even_after_success_cache(agents, setup_sync, component, tmp_path):
    source = agents(rows=[snapshot()])
    sync, archive, _ = setup_sync
    sync.scan_once()
    candidate = {"source": source, "co": source.parent,
                 "agent": source.parents[1], "metadata": source.parents[1] / "meta.json"}[component]
    moved = tmp_path / ("outside-" + component)
    candidate.rename(moved)
    candidate.symlink_to(moved, target_is_directory=moved.is_dir())
    sync.scan_once()
    assert sync.status("demo")["state"] == "error"
    assert len(archive.calls) == 1


def test_nofollow_blocks_a_source_swapped_after_validation(agents, setup_sync, monkeypatch, tmp_path):
    source = agents(rows=[snapshot()])
    sync, archive, _ = setup_sync
    original_signature = sync._source_signature
    outside = tmp_path / "sensitive-source"
    outside.write_text(json.dumps(snapshot(result="must not be read")))

    def swap_after_signature(path):
        signature = original_signature(path)
        source.unlink()
        source.symlink_to(outside)
        return signature

    monkeypatch.setattr(sync, "_source_signature", swap_after_signature)
    sync._reader = lambda *args, **kwargs: pytest.fail("linked source must not be parsed")
    sync.scan_once()
    assert sync.status("demo")["state"] == "error"
    assert archive.calls == []


def test_stop_during_batch_never_starts_the_next_save(agents, setup_sync):
    agents(rows=[snapshot("first"), snapshot("second")])
    sync, archive, _ = setup_sync
    archive.on_save = lambda capture: sync.stop()
    sync.scan_once()
    assert len(archive.calls) == 1
    assert sync.status("demo")["state"] == "stopped"
    sync.scan_once()
    sync.start()
    assert len(archive.calls) == 1
    assert sync._thread is None


def test_failed_thread_start_can_be_stopped_safely(setup_sync, monkeypatch):
    sync, archive, _ = setup_sync

    def failed_start(thread):
        raise RuntimeError("synthetic thread startup failure")

    monkeypatch.setattr(threading.Thread, "start", failed_start)
    with pytest.raises(RuntimeError, match="startup failure"):
        sync.start()
    assert sync._thread is None
    sync.stop()
    assert sync.status("demo")["state"] == "stopped"
    assert archive.calls == []


def test_daemon_lifecycle_status_and_stop_are_independent_of_inflight_sql(agents, setup_sync):
    agents(rows=[snapshot("first"), snapshot("second")])
    sync, archive, _ = setup_sync
    entered, release = threading.Event(), threading.Event()

    def blocked_save(capture):
        entered.set()
        assert release.wait(2)

    archive.on_save = blocked_save
    try:
        sync.start()
        assert entered.wait(2)
        thread = sync._thread
        sync.start()
        assert sync._thread is thread and thread.daemon
        assert sync.status("demo")["state"] == "syncing"
        sync.stop(timeout=0)
        assert sync.status("demo")["state"] == "stopped"
    finally:
        release.set()
        sync.stop()
    assert not thread.is_alive()
    assert len(archive.calls) == 1


def test_daemon_automatically_archives_source_appended_after_startup(agents, setup_sync):
    source = agents(rows=[snapshot(revision=1)])
    sync, archive, _ = setup_sync
    sync._poll_interval = 0.01
    initial_saved, appended_saved = threading.Event(), threading.Event()

    def observe_capture(capture):
        if capture.session.source_revision == 1:
            initial_saved.set()
        elif capture.session.source_revision == 2:
            appended_saved.set()

    archive.on_save = observe_capture
    try:
        sync.start()
        assert initial_saved.wait(2), "startup did not archive the retained source"
        with source.open("a") as handle:
            handle.write(json.dumps(snapshot(revision=2)) + "\n")
        # Only the daemon polls here: there are no manual scan_once calls,
        # timing sleeps, real connections, or framework/Agent startup.
        assert appended_saved.wait(2), "daemon did not notice the appended snapshot"
    finally:
        sync.stop()
    assert not sync._thread.is_alive()
    assert [capture.session.source_revision for capture in archive.calls] == [1, 2]
    assert len(archive.captures) == 2
    assert [json.loads(line)["revision"] for line in source.read_text().splitlines()] == [1, 2]


def test_registry_scan_failure_is_safe_and_recoverable(agents, setup_sync, monkeypatch, caplog):
    agents(rows=[snapshot()])
    sync, archive, _ = setup_sync
    original = Path.iterdir

    def failed_scan(path):
        raise OSError("private source contents and database credentials")

    monkeypatch.setattr(Path, "iterdir", failed_scan)
    sync.scan_once()
    assert sync.status("demo")["state"] == "error"
    assert "credentials" not in caplog.text + str(sync.status("demo"))
    monkeypatch.setattr(Path, "iterdir", original)
    sync.scan_once()
    assert sync.status("demo")["state"] == "ok"
    assert len(archive.calls) == 1


def test_cached_success_recovers_status_after_a_global_scan_error(agents, setup_sync, monkeypatch):
    agents(rows=[snapshot()])
    sync, archive, clock = setup_sync
    sync.scan_once()
    original = Path.iterdir
    monkeypatch.setattr(Path, "iterdir", lambda path: (_ for _ in ()).throw(OSError("private")))
    clock.value = 2
    sync.scan_once()
    assert sync.status("demo")["state"] == "error"
    monkeypatch.setattr(Path, "iterdir", original)
    clock.value = 4
    sync.scan_once()
    assert sync.status("demo")["state"] == "ok"
    assert len(archive.calls) == 1


def test_registry_switch_clears_prior_status_and_backoff_without_deleting_captures(
    agents, setup_sync, monkeypatch, tmp_path,
):
    source = agents(rows=[snapshot()])
    agents("old-only", rows=[snapshot()])
    sync, archive, clock = setup_sync
    sync.scan_once()
    previous_captures = dict(archive.captures)
    archive.error = ArchiveError("synthetic outage")
    clock.value = 60
    sync.scan_once()
    assert sync.status("demo")["state"] == "error"
    assert sync.status("old-only")["state"] == "error"

    new_root = tmp_path / "new-agents"
    new_agent = new_root / "demo"
    new_agent.mkdir(parents=True)
    (new_agent / "meta.json").write_bytes((source.parents[1] / "meta.json").read_bytes())
    monkeypatch.setattr(config, "AGENTS_DIR", new_root)
    clock.value = 60.5  # Still within the former registry's failure backoff.
    sync.scan_once()
    assert sync.status("demo")["state"] == "ok"
    assert sync.status("demo")["synced_at"] is None
    assert sync.status("old-only")["state"] == "pending"
    assert sync.status("old-only")["checked_at"] is None
    assert archive.captures == previous_captures

    new_source = new_agent / ".co" / "session_results.jsonl"
    new_source.parent.mkdir()
    new_source.write_text(json.dumps(snapshot(revision=2)) + "\n")
    archive.error = None
    sync.scan_once()
    assert sync.status("demo")["state"] == "ok"
    assert sync.status("demo")["synced_at"] == clock.now().isoformat()
    assert archive.calls[-1].session.source_revision == 2
    assert all(archive.captures[key] == value for key, value in previous_captures.items())
    assert len(archive.captures) == len(previous_captures) + 1


def test_read_size_race_still_reports_an_explicit_size_error(agents, setup_sync):
    source = agents(rows=[snapshot()])
    sync, archive, _ = setup_sync
    sync._max_source_bytes = source.stat().st_size

    def grow_before_parser(*args, **kwargs):
        with source.open("ab") as handle:
            handle.write(b"\n")
        return read_execution_stream(*args, **kwargs)

    sync._reader = grow_before_parser
    sync.scan_once()
    assert archive.calls == []
    assert sync.status("demo")["state"] == "error"
    assert "archive limit" in sync.status("demo")["message"]


def test_worker_survives_unexpected_global_scan_exception(agents, setup_sync, monkeypatch):
    agents(rows=[snapshot()])
    sync, archive, _ = setup_sync
    sync._poll_interval = 0.01
    scanned = threading.Event()
    original = sync.scan_once
    calls = []

    def faulty_first_scan():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("secret exception body")
        original()
        scanned.set()

    monkeypatch.setattr(sync, "scan_once", faulty_first_scan)
    sync.start()
    assert scanned.wait(2)
    sync.stop()
    assert len(calls) >= 2 and len(archive.captures) == 1


def test_open_stream_preserves_existing_reader_and_does_not_close_handle(agents):
    source = agents(rows=[snapshot(), snapshot(revision=2)])
    expected = read_execution_history(source, agent_slug="demo")
    with source.open("rb") as handle:
        handle.seek(3)
        assert read_execution_stream(handle, agent_slug="demo") == expected
        assert not handle.closed
        with pytest.raises(ValueError, match="size limit"):
            read_execution_stream(handle, agent_slug="demo", max_bytes=1)


def test_archive_is_redacted_before_automatic_persistence(agents, setup_sync):
    agents(rows=[snapshot(prompt="api_key=synthetic-secret", result="password=synthetic-password")])
    sync, archive, _ = setup_sync
    sync.scan_once()
    capture = archive.calls[0]
    assert "synthetic-secret" not in str(capture)
    assert "synthetic-password" not in str(capture)
    assert "[REDACTED]" in capture.turns[0].prompt


def test_status_returns_detached_state_without_io(setup_sync, monkeypatch):
    sync, _, _ = setup_sync
    monkeypatch.setattr(Path, "iterdir", lambda *args: pytest.fail("status must not scan"))
    status = sync.status("new-agent")
    status["state"] = "modified externally"
    assert sync.status("new-agent")["state"] == "pending"
    sync.stop()
    assert sync.status("new-agent")["state"] == "stopped"


def test_capture_callback_runs_after_commit_and_not_for_duplicates(agents):
    source = agents(rows=[snapshot()])
    before = source.read_bytes(), source.stat().st_mtime_ns
    archive, clock = MemoryArchive(), Clock()
    notifications, observed_commits, events_during_save = [], [], []

    def on_capture(slug, session_id, capture_id):
        notifications.append((slug, session_id, capture_id))
        observed_commits.append(capture_id in archive.captures)

    archive.on_save = lambda capture: events_during_save.append(list(notifications))
    sync = HistorySync(
        lambda: archive, monotonic=clock.monotonic, clock=clock.now,
        on_capture=on_capture,
    )
    sync.scan_once()
    capture = archive.calls[0]
    assert notifications == [("demo", "session-1", capture.capture_id)]
    assert observed_commits == [True]
    assert events_during_save == [[]]
    clock.value = 60
    sync.scan_once()  # Reconciliation attempts the same committed capture.
    assert len(archive.calls) == 2
    assert notifications == [("demo", "session-1", capture.capture_id)]
    assert (source.read_bytes(), source.stat().st_mtime_ns) == before


def test_capture_callback_does_not_run_for_failed_writes(agents, setup_sync):
    agents(rows=[snapshot()])
    sync, archive, _ = setup_sync
    notifications = []
    sync._on_capture = lambda *args: notifications.append(args)
    archive.error = ArchiveError("synthetic rollback")
    sync.scan_once()
    assert len(archive.calls) == 1 and not archive.captures
    assert notifications == []
    assert sync.status("demo")["state"] == "error"


def test_each_partial_commit_is_notified_before_later_failure(agents, setup_sync):
    source = agents(rows=[snapshot("first"), snapshot("second"), snapshot("third")])
    before = source.read_bytes(), source.stat().st_mtime_ns
    sync, archive, clock = setup_sync
    notifications = []
    sync._on_capture = lambda *args: notifications.append(args)

    def fail_third(capture):
        if capture.session.session_id == "third":
            raise ArchiveError("synthetic rollback")

    archive.on_save = fail_third
    sync.scan_once()
    assert [event[1] for event in notifications] == ["first", "second"]
    assert {event[2] for event in notifications} == set(archive.captures)
    assert sync.status("demo")["state"] == "error"
    assert sync.status("demo")["synced_at"] is None
    archive.on_save = None
    clock.value = 2
    sync.scan_once()
    assert [event[1] for event in notifications] == ["first", "second", "third"]
    assert len(archive.calls) == 6 and len(archive.captures) == 3
    assert sync.status("demo")["state"] == "ok"
    assert (source.read_bytes(), source.stat().st_mtime_ns) == before


def test_notification_failures_do_not_change_archival_outcome(agents, caplog):
    agents("private-agent", rows=[snapshot("private-first"), snapshot("private-second")])
    archive = MemoryArchive()
    attempts = []

    def failing_notification(*args):
        attempts.append(args)
        raise RuntimeError("private payload, SQL and database credentials")

    sync = HistorySync(
        lambda: archive, on_capture=failing_notification,
        on_status=failing_notification,
    )
    sync.scan_once()
    assert len(archive.calls) == len(archive.captures) == 2
    assert sync.status("private-agent")["state"] == "ok"
    assert sync.status("private-agent")["message"] is None
    assert sync._states["private-agent"].failures == 0
    assert len(attempts) == 4  # syncing, two commits, ok
    assert [record.message for record in caplog.records] == [
        "History sync notification failed",
    ] * 4
    assert all(record.exc_info is None for record in caplog.records)


def test_status_notifications_deduplicate_timestamps_but_include_message_changes(setup_sync):
    sync, _, clock = setup_sync
    notifications = []
    sync._on_status = lambda *args: notifications.append(args)
    sync._set_status("demo", "ok")
    clock.value = 2
    sync._set_status("demo", "ok", synced_at=clock.now().isoformat())
    assert notifications == [("demo", "ok")]
    assert sync.status("demo")["checked_at"] == clock.now().isoformat()
    sync._set_status("demo", "warning", "first synthetic warning")
    sync._set_status("demo", "warning", "first synthetic warning")
    sync._set_status("demo", "warning", "second synthetic warning")
    sync._set_status("demo", "ok")
    assert notifications == [
        ("demo", "ok"), ("demo", "warning"),
        ("demo", "warning"), ("demo", "ok"),
    ]


def test_status_callbacks_run_outside_lock_and_global_errors_recover(
    agents, setup_sync, monkeypatch,
):
    agents("first", rows=[snapshot()])
    agents("second")
    sync, archive, clock = setup_sync
    notifications, acquired, observed = [], [], []

    def on_status(slug, state):
        notifications.append((slug, state))
        unlocked = sync._lock.acquire(blocking=False)
        acquired.append(unlocked)
        if unlocked:
            sync._lock.release()
            observed.append(sync.status(slug)["state"])

    sync._on_status = on_status
    sync.scan_once()
    assert notifications == [("first", "syncing"), ("first", "ok"), ("second", "ok")]
    sync._set_status("first", "error", "A synthetic per-agent failure")
    original = Path.iterdir

    def failed_scan(path):
        raise OSError("private registry details")

    monkeypatch.setattr(Path, "iterdir", failed_scan)
    clock.value = 2
    sync.scan_once()
    # The first agent was already in error; the changed message still matters.
    assert notifications[-2:] == [("first", "error"), ("second", "error")]
    count = len(notifications)
    clock.value = 4
    sync.scan_once()
    assert len(notifications) == count
    monkeypatch.setattr(Path, "iterdir", original)
    clock.value = 6
    sync.scan_once()
    assert notifications[-2:] == [("first", "ok"), ("second", "ok")]
    assert len(archive.calls) == 1  # Cached archive success needs no new save.
    sync.stop()
    assert notifications[-2:] == [("first", "stopped"), ("second", "stopped")]
    count = len(notifications)
    sync.stop()
    assert len(notifications) == count
    assert acquired == [True] * len(notifications)
    assert observed == [state for _, state in notifications]


def test_committed_capture_is_notified_when_stop_occurs_inside_save(agents, setup_sync):
    agents(rows=[snapshot("first"), snapshot("second")])
    sync, archive, _ = setup_sync
    notifications = []
    sync._on_capture = lambda *args: notifications.append(args)
    archive.on_save = lambda capture: sync.stop()
    sync.scan_once()
    assert len(archive.calls) == len(archive.captures) == len(notifications) == 1
    assert notifications[0] == ("demo", "first", archive.calls[0].capture_id)
    assert sync.status("demo")["state"] == "stopped"


def test_notification_hooks_preserve_no_io_construction_and_status_reads(monkeypatch):
    notifications = []

    def unexpected_io(*args, **kwargs):
        pytest.fail("construction and status reads must not perform I/O")

    monkeypatch.setattr(Path, "iterdir", unexpected_io)
    sync = HistorySync(
        unexpected_io, reader=unexpected_io,
        on_capture=lambda *args: notifications.append(args),
        on_status=lambda *args: notifications.append(args),
    )
    assert sync.status("demo")["state"] == "pending"
    assert sync.status("demo")["state"] == "pending"
    sync.stop()
    assert sync.status("demo")["state"] == "stopped"
    assert notifications == []
