"""Unified History is a read view, never an ingestion trigger or file fallback."""
import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from co_studio import config, registry
from co_studio.api import archives, sessions
from co_studio.app import create_app
from co_studio.execution_models import SessionRecord
from co_studio.history_archive import ArchiveError


def forbidden(*args, **kwargs):
    raise AssertionError("Unexpected side effect, source read, or real connection")


class MemoryStore:
    def __init__(self):
        self.rows = []
        self.calls = []

    def session_page(self, slug, *, offset, limit):
        self.calls.append(("sessions", slug, offset, limit))
        rows = [row for row in self.rows if row["session"].agent_slug == slug]
        return len(rows), rows[offset:offset + limit]

    def versions_page(self, slug, session_id, *, offset, limit):
        self.calls.append(("versions", slug, session_id, offset, limit))
        rows = [row for row in self.rows if (row["session"].agent_slug, row["session"].session_id) == (slug, session_id)]
        return len(rows), rows[offset:offset + limit]

    save = initialize = forbidden


@pytest.fixture
def harness(tmp_path, monkeypatch):
    root = tmp_path / "agents"
    (root / "demo").mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTS_DIR", root)
    monkeypatch.delenv("STUDIO_HISTORY_DATABASE_URL", raising=False)
    monkeypatch.delenv("STUDIO_HISTORY_CONFIG", raising=False)
    registry.save(registry.AgentMeta(slug="demo", name="Synthetic", address="0xsynthetic", port=8001,
                                    model="offline", capabilities=[], created_at="2026-10-28"))
    store = MemoryStore()
    monkeypatch.setattr(archives, "_archive", lambda: store)
    from co_studio import history_archive
    monkeypatch.setattr(history_archive, "_driver", forbidden)
    app = create_app()
    client = TestClient(app, base_url="http://127.0.0.1:9900", client=("127.0.0.1", 50000))
    yield SimpleNamespace(root=root, app=app, client=client, store=store)
    client.close()  # No desktop lifespan/authentication/Agent processes.


def configure(monkeypatch):
    monkeypatch.setenv("STUDIO_HISTORY_DATABASE_URL", "postgresql://fixture:private@invalid.example/test")


def row(session_id="opaque/session?x=1&y=2", slug="demo", **overrides):
    item = {"session": SessionRecord(agent_slug=slug, session_id=session_id, source_status="done",
                                     source_revision=3, updated_at=200),
            "capture_id": "a" * 64, "archived_at": "2026-10-28T00:00:00+00:00",
            "version_count": 2, "selection_warning": None}
    item.update(overrides)
    return item


def test_without_config_uses_files_without_database(harness, monkeypatch):
    monkeypatch.setattr(archives, "_archive", forbidden)
    response = harness.client.get("/api/agents/demo/history")
    assert response.status_code == 200
    data = response.json()
    assert data["source"] == "file" and data["sync"]["state"] == "disabled"
    assert data["source_exists"] is False and data["sessions"]["total"] == 0
    assert response.headers["cache-control"] == "no-store"


def test_configured_sessions_use_database_not_source(harness, monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(sessions, "_history", forbidden)
    harness.store.rows = [row(), row("second", capture_id="b" * 64)]
    response = harness.client.get("/api/agents/demo/history?limit=1")
    assert response.status_code == 200
    data = response.json()
    assert data["source"] == "archive" and data["sessions"]["total"] == 2
    assert data["sessions"]["next_offset"] == 1
    assert data["sessions"]["items"][0]["version_count"] == 2
    assert data["sync"]["state"] == "pending"  # No lifecycle in this isolated test.
    assert "private" not in response.text and "invalid.example" not in response.text
    assert harness.store.calls == [("sessions", "demo", 0, 1)]


@pytest.mark.parametrize("state", ["pending", "syncing", "ok", "warning", "error", "stopped"])
def test_sync_status_is_visible_and_never_starts_work(harness, monkeypatch, state):
    configure(monkeypatch)
    payload = {"state": state, "checked_at": "2026-10-28T00:00:00Z", "synced_at": None,
               "message": "Safe sync status"}
    harness.app.state.history_sync = SimpleNamespace(status=lambda slug: payload, start=forbidden, scan_once=forbidden)
    response = harness.client.get("/api/agents/demo/history")
    assert response.status_code == 200 and response.json()["sync"] == payload
    assert response.json()["sessions"]["total"] == 0


def test_selection_warning_is_kept(harness, monkeypatch):
    configure(monkeypatch)
    harness.store.rows = [row(selection_warning="Showing an earlier readable version.")]
    response = harness.client.get("/api/agents/demo/history")
    assert response.json()["sessions"]["items"][0]["selection_warning"]


def test_unconfigured_versions_do_not_call_database(harness, monkeypatch):
    monkeypatch.setattr(archives, "_archive", forbidden)
    response = harness.client.get("/api/agents/demo/history/versions", params={"session_id": "s"})
    assert response.status_code == 409


def test_version_query_preserves_opaque_identity_and_pagination(harness, monkeypatch):
    configure(monkeypatch)
    record = row()
    harness.store.rows = [record, row(capture_id="b" * 64)]
    response = harness.client.get("/api/agents/demo/history/versions",
                                  params={"session_id": record["session"].session_id, "limit": 1, "offset": 1})
    assert response.status_code == 200
    data = response.json()
    assert data["versions"]["total"] == 2 and data["versions"]["next_offset"] is None
    assert data["versions"]["items"][0]["capture_id"] == "b" * 64
    assert harness.store.calls == [("versions", "demo", record["session"].session_id, 1, 1)]


@pytest.mark.parametrize("endpoint", ["", "/versions?session_id=s"])
def test_database_failure_is_safe_and_never_falls_back(harness, monkeypatch, endpoint):
    configure(monkeypatch)
    monkeypatch.setattr(sessions, "_history", forbidden)
    def fail():
        raise ArchiveError("password=private-location")
    monkeypatch.setattr(archives, "_archive", fail)
    response = harness.client.get("/api/agents/demo/history" + endpoint)
    assert response.status_code == 503
    assert "private-location" not in response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("contents", [" ", "{bad", "null", '{"database_url":" "}'])
def test_bad_config_is_not_file_mode(harness, monkeypatch, tmp_path, contents):
    path = tmp_path / "bad-config.json"
    path.write_text(contents)
    monkeypatch.setenv("STUDIO_HISTORY_CONFIG", str(path))
    monkeypatch.setattr(sessions, "_history", forbidden)
    response = harness.client.get("/api/agents/demo/history")
    assert response.status_code == 503 and str(path) not in response.text


@pytest.mark.parametrize("endpoint", ["", "/versions?session_id=s"])
def test_missing_agent_is_rejected_before_database(harness, monkeypatch, endpoint):
    configure(monkeypatch)
    monkeypatch.setattr(archives, "_archive", forbidden)
    assert harness.client.get("/api/agents/missing/history" + endpoint).status_code == 404


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1", "offset=text"])
def test_invalid_pagination(harness, query):
    assert harness.client.get("/api/agents/demo/history?" + query).status_code == 422


@pytest.mark.parametrize("session_id", ["", "has space", "x" * 513])
def test_invalid_version_query(harness, session_id):
    assert harness.client.get("/api/agents/demo/history/versions", params={"session_id": session_id}).status_code == 422


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
def test_no_public_write_routes(harness, method):
    assert harness.client.request(method, "/api/agents/demo/history").status_code == 405


def test_cross_site_requests_are_rejected(harness, monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(archives, "_archive", forbidden)
    response = harness.client.get("/api/agents/demo/history", headers={"Origin": "https://unrelated.invalid"})
    assert response.status_code == 403 and response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("versions", [False, True])
def test_store_ownership_is_rechecked(harness, monkeypatch, versions):
    configure(monkeypatch)
    bad = row(slug="other")
    method = "versions_page" if versions else "session_page"
    monkeypatch.setattr(harness.store, method, lambda *a, **k: (1, [bad]))
    path = "/api/agents/demo/history" + ("/versions?session_id=s" if versions else "")
    assert harness.client.get(path).status_code == 503


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("fault", [None, "start", "stop"])
def test_lifecycle_starts_optional_worker_and_stops_it_without_agents(monkeypatch, configured, fault):
    # Exercise the actual lifespan with all unrelated desktop side effects replaced.
    from co_studio import app as app_module
    calls = []
    monkeypatch.delenv("STUDIO_HISTORY_DATABASE_URL", raising=False)
    monkeypatch.delenv("STUDIO_HISTORY_CONFIG", raising=False)
    if configured:
        configure(monkeypatch)
    for target, name, result in [
        (app_module.registry, "ensure_dirs", None), (app_module.registry, "migrate_capability_aliases", 0),
        (app_module.creator, "refresh_generated_workspace_scripts", 0),
        (app_module.creator, "remediate_invite_policies", 0), (app_module.setup_check, "run_doctor", []),
    ]:
        monkeypatch.setattr(target, name, lambda result=result: result)
    monkeypatch.setattr(app_module, "_auto_authenticate", lambda: None)
    async def poll():
        try:
            await asyncio.Event().wait()
        finally:
            calls.append("poll-stopped")
    monkeypatch.setattr(app_module, "SUPERVISOR", SimpleNamespace(adopt_orphans=lambda: None, run=poll))
    class Worker:
        def __init__(self, factory, **callbacks):
            self.factory = factory
            assert set(callbacks) == {"on_capture", "on_status"}
        def start(self):
            calls.append("start")
            if fault == "start":
                raise RuntimeError("private startup details")
        def stop(self):
            calls.append("stop")
            if fault == "stop":
                raise RuntimeError("private shutdown details")
    monkeypatch.setattr(app_module, "HistorySync", Worker)
    async def exercise():
        app = create_app()
        async with app_module._lifespan(app):
            await asyncio.sleep(0)
            assert hasattr(app.state, "history_sync") is configured
            assert app.state.history_sync_error is (configured and fault == "start")
    asyncio.run(exercise())
    assert calls == (["start", "stop", "poll-stopped"] if configured else ["poll-stopped"])
