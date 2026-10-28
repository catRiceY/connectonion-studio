"""Read-only archive HTTP contracts, with synthetic metadata and an in-memory store.

No browser, database, model, real user data, or application lifespan is used.
The actual app/router/local guard/privacy projection are exercised by TestClient.
"""

from copy import deepcopy
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from co_studio import config, history_archive, registry
from co_studio.api import archives as archive_api, sessions as sessions_api
from co_studio.app import create_app
from co_studio.execution_adapter import ConvertedSession
from co_studio.execution_models import ExecutionEvent, SessionRecord, TurnRecord
from co_studio.history_archive import ArchiveError, Capture, prepare_capture


def forbidden(*args, **kwargs):
    pytest.fail("Read-only archive requests must not connect, write, start, or read source history")


def make_capture(slug="demo", revision=3, session_id="opaque/session-1"):
    owner = {"agent_slug": slug, "session_id": session_id}
    return prepare_capture(ConvertedSession(
        SessionRecord(**owner, source_status="done", source_revision=revision,
                      created_at=100.0, updated_at=105.0),
        (
            TurnRecord(**owner, turn_no=1, prompt="first question", result="first answer",
                       duration_ms=0, finish_reason="natural"),
            TurnRecord(**owner, turn_no=3, prompt="third question", result=None),
        ),
        (
            ExecutionEvent(**owner, event_id="event-0", sequence=0, kind="plan",
                           turn_no=None, payload={"entries": []}),
            ExecutionEvent(**owner, event_id="event-2", sequence=2, kind="user_input",
                           turn_no=1, payload={"content": "first question"}),
            ExecutionEvent(**owner, event_id="event-4", sequence=4, kind="llm_result",
                           turn_no=1, duration_ms=0,
                           payload={"usage": {"input_tokens": 8, "output_tokens": 0}}),
            ExecutionEvent(**owner, event_id="event-6", sequence=6, kind="tool_result",
                           turn_no=3, source_id="source-3", call_id="call-3",
                           payload={"name": "lookup_order", "status": "error",
                                    "error": "Synthetic timeout"}),
        ),
    ))


class MemoryArchive:
    def __init__(self):
        self.rows = []
        self.calls = []

    def add(self, capture, archived_at="2026-10-28T09:00:00+00:00"):
        self.rows.append({"capture": capture, "archived_at": archived_at})
        return capture

    def archive_page(self, slug, *, limit, offset):
        self.calls.append(("list", slug, limit, offset))
        rows = [row for row in self.rows if row["capture"].session.agent_slug == slug]
        return len(rows), [{"capture_id": row["capture"].capture_id,
                            "archived_at": row["archived_at"],
                            "session": row["capture"].session}
                           for row in rows[offset:offset + limit]]

    def get_owned(self, slug, capture_id):
        self.calls.append(("get", slug, capture_id))
        return next((row for row in self.rows
                     if row["capture"].capture_id == capture_id
                     and row["capture"].session.agent_slug == slug), None)

    initialize = save = forbidden


@pytest.fixture(autouse=True)
def isolated_archive_config(monkeypatch):
    monkeypatch.delenv("STUDIO_HISTORY_DATABASE_URL", raising=False)
    monkeypatch.delenv("STUDIO_HISTORY_CONFIG", raising=False)
    monkeypatch.setattr(history_archive, "_driver", forbidden)


@pytest.fixture
def agents_root(tmp_path, monkeypatch):
    root = tmp_path / "agents"
    root.mkdir()
    monkeypatch.setattr(config, "AGENTS_DIR", root)
    return root


@pytest.fixture
def register(agents_root):
    def add(slug="demo"):
        directory = agents_root / slug
        directory.mkdir()
        registry.save(registry.AgentMeta(
            slug=slug, name="Synthetic registered Agent", address="0xsynthetic", port=8099,
            model="offline", capabilities=[], created_at="2026-10-28T00:00:00Z",
        ))
        return directory
    return add


@pytest.fixture
def store(monkeypatch):
    memory = MemoryArchive()
    monkeypatch.setattr(archive_api, "_archive", lambda: memory)
    return memory


@pytest.fixture
def client(agents_root):
    client = TestClient(create_app(), base_url="http://127.0.0.1:9900",
                        client=("127.0.0.1", 50000))
    try:
        yield client
    finally:
        client.close()


def assert_private(response):
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_unconfigured_storage_defaults_to_files_without_database_probe(client, monkeypatch):
    monkeypatch.setattr(archive_api, "_archive", forbidden)
    response = client.get("/api/history/storage")
    assert response.status_code == 200
    assert response.json() == {"archive_configured": False, "default_source": "file"}
    assert_private(response)


@pytest.mark.parametrize("source", ["environment", "config-file"])
def test_configured_storage_defaults_to_archive_without_probe_or_dsn_disclosure(
    client, monkeypatch, tmp_path, source,
):
    dsn = "postgresql://fixture:database-private@invalid.example/history"
    if source == "environment":
        monkeypatch.setenv("STUDIO_HISTORY_DATABASE_URL", dsn)
    else:
        path = tmp_path / "server-private-config.json"
        path.write_text(json.dumps({"database_url": dsn}))
        monkeypatch.setenv("STUDIO_HISTORY_CONFIG", str(path))
    monkeypatch.setattr(archive_api, "_archive", forbidden)
    response = client.get("/api/history/storage")
    assert response.status_code == 200
    assert response.json() == {"archive_configured": True, "default_source": "archive"}
    assert "database-private" not in response.text and str(tmp_path) not in response.text
    assert_private(response)


@pytest.mark.parametrize("contents", ["{private-config-secret", "null", "[]", "{}",
                                      '{"database_url":12}', '{"database_url":" "}'])
def test_invalid_server_configuration_returns_safe_503(client, monkeypatch, tmp_path, contents):
    path = tmp_path / "private-config-path.json"
    path.write_text(contents)
    monkeypatch.setenv("STUDIO_HISTORY_CONFIG", str(path))
    response = client.get("/api/history/storage")
    assert response.status_code == 503
    assert "private-config" not in response.text and str(path) not in response.text
    assert_private(response)


def test_missing_server_config_returns_503_but_environment_takes_precedence(
    client, monkeypatch, tmp_path,
):
    path = tmp_path / "does-not-exist-private.json"
    monkeypatch.setenv("STUDIO_HISTORY_CONFIG", str(path))
    assert client.get("/api/history/storage").status_code == 503
    monkeypatch.setenv("STUDIO_HISTORY_DATABASE_URL", "postgresql://fixture:secret@invalid.example/db")
    original_read = Path.read_text

    def no_config_read(candidate, *args, **kwargs):
        if candidate == path:
            pytest.fail("Environment DSN must take priority without reading config file")
        return original_read(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", no_config_read)
    assert client.get("/api/history/storage").json()["default_source"] == "archive"


def test_whitespace_environment_dsn_is_not_advertised_as_configured(client, monkeypatch):
    monkeypatch.setenv("STUDIO_HISTORY_DATABASE_URL", " \t ")
    response = client.get("/api/history/storage")
    assert response.status_code == 503
    assert_private(response)


def test_browser_cannot_choose_database_or_config_by_query(client, tmp_path):
    supplied = tmp_path / "browser-selected-config.json"
    supplied.write_text(json.dumps({"database_url": "postgresql://invalid/browser-secret"}))
    response = client.get("/api/history/storage", params={
        "config": str(supplied), "path": str(supplied), "database_url": "postgresql://invalid/query-secret",
    })
    assert response.json() == {"archive_configured": False, "default_source": "file"}
    assert "secret" not in response.text


def test_archive_factory_uses_only_server_environment(client, register, monkeypatch, tmp_path):
    register()
    selected = []
    memory = MemoryArchive()
    monkeypatch.setattr(archive_api, "PostgresArchive", lambda dsn: selected.append(dsn) or memory)
    path = tmp_path / "server.json"
    path.write_text(json.dumps({"database_url": "postgresql://server-config"}))
    monkeypatch.setenv("STUDIO_HISTORY_CONFIG", str(path))
    response = client.get("/api/agents/demo/archives", params={"database_url": "postgresql://caller"})
    assert response.status_code == 200 and selected == ["postgresql://server-config"]
    monkeypatch.setenv("STUDIO_HISTORY_DATABASE_URL", "postgresql://server-env")
    assert client.get("/api/agents/demo/archives").status_code == 200
    assert selected[-1] == "postgresql://server-env"


def test_unconfigured_archive_is_503_not_empty_or_file_fallback(client, register, monkeypatch):
    register()
    monkeypatch.setattr(sessions_api, "_history", forbidden)
    for suffix in ("", "/" + "a" * 64):
        response = client.get("/api/agents/demo/archives" + suffix)
        assert response.status_code == 503
        assert_private(response)


def test_list_keeps_multiple_captures_of_same_session_and_paginates_metadata_only(client, register, store):
    register()
    captures = [store.add(make_capture(revision=revision)) for revision in (3, 2, 1)]
    response = client.get("/api/agents/demo/archives?limit=2")
    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "archive" and body["source_exists"] is True and body["issue_counts"] == {}
    assert body["agent_slug"] == "demo"
    page = body["sessions"]
    assert (page["total"], page["offset"], page["limit"], page["next_offset"]) == (3, 0, 2, 2)
    assert [item["capture_id"] for item in page["items"]] == [item.capture_id for item in captures[:2]]
    assert [item["session_id"] for item in page["items"]] == ["opaque/session-1"] * 2
    assert all(item["archived_at"] == "2026-10-28T09:00:00+00:00" for item in page["items"])
    assert all(word not in response.text for word in ("first answer", "first question", "Synthetic timeout", "payload"))
    tail = client.get("/api/agents/demo/archives?offset=2&limit=2").json()["sessions"]
    assert tail["items"][0]["capture_id"] == captures[2].capture_id and tail["next_offset"] is None
    beyond = client.get("/api/agents/demo/archives?offset=99").json()["sessions"]
    assert beyond["items"] == [] and beyond["total"] == 3 and beyond["next_offset"] is None
    assert store.calls[:2] == [("list", "demo", 2, 0), ("list", "demo", 2, 2)]
    assert_private(response)


def test_registered_agent_without_captures_has_empty_archive(client, register, store):
    register()
    response = client.get("/api/agents/demo/archives")
    assert response.status_code == 200
    assert response.json()["sessions"] == {"items": [], "total": 0, "offset": 0, "limit": 20, "next_offset": None}
    assert response.json()["source_exists"] is True


def test_detail_preserves_unknown_zero_and_unassigned_values_with_independent_pages(client, register, store):
    register()
    capture = store.add(make_capture())
    path = f"/api/agents/demo/archives/{capture.capture_id}"
    response = client.get(path)
    assert response.status_code == 200
    body = response.json()
    assert body["capture_id"] == capture.capture_id and body["source"] == "archive"
    assert body["session"]["session_id"] == "opaque/session-1"
    assert body["turns"]["items"][0]["duration_ms"] == 0
    assert body["turns"]["items"][1]["duration_ms"] is None
    assert body["turns"]["items"][1]["result"] is None
    assert body["events"]["items"][0]["turn_no"] is None
    assert body["events"]["items"][2]["payload"]["usage"]["output_tokens"] == 0
    assert body["events"]["items"][3]["call_id"] == "call-3"
    page = client.get(path + "?turn_offset=1&turn_limit=1&event_offset=1&event_limit=2").json()
    assert [item["turn_no"] for item in page["turns"]["items"]] == [3]
    assert page["turns"]["total"] == 2 and page["turns"]["next_offset"] is None
    assert [item["sequence"] for item in page["events"]["items"]] == [2, 4]
    assert page["events"]["total"] == 4 and page["events"]["next_offset"] == 3
    assert_private(response)


def test_turn_filter_precedes_pagination_and_unknown_turn_is_empty(client, register, store):
    register()
    capture = store.add(make_capture())
    path = f"/api/agents/demo/archives/{capture.capture_id}"
    body = client.get(path + "?turn_no=1&event_offset=1&event_limit=1").json()
    assert [item["turn_no"] for item in body["turns"]["items"]] == [1]
    assert body["events"]["total"] == 2
    assert [item["sequence"] for item in body["events"]["items"]] == [4]
    for suffix in ("?turn_no=99", "?turn_offset=99&event_offset=99"):
        empty = client.get(path + suffix)
        assert empty.status_code == 200
        assert all(empty.json()[key]["items"] == [] and empty.json()[key]["next_offset"] is None
                   for key in ("turns", "events"))


def test_public_archive_projection_masks_credentials_without_mutating_stored_capture(client, register, store):
    register()
    base = make_capture()
    capture = Capture(
        "a" * 64, base.session.model_copy(update={"source_status": "password=status-secret"}),
        (base.turns[0].model_copy(update={"prompt": "password=prompt-secret", "result": "password=answer-secret",
                                        "finish_reason": "password=reason-secret"}), base.turns[1]),
        (base.events[0], base.events[1], base.events[2], base.events[3].model_copy(update={"payload": {
            "api_key": "nested-secret", "result": "password=tool-secret", "usage": {"input_tokens": 8},
        }})),
        {"invalid_event": 2},
    )
    store.add(capture)
    before = deepcopy(capture)
    listed = client.get("/api/agents/demo/archives")
    response = client.get(f"/api/agents/demo/archives/{capture.capture_id}")
    assert listed.status_code == response.status_code == 200
    assert listed.json()["sessions"]["items"][0]["source_status"] == "password=[REDACTED]"
    for secret in ("status-secret", "prompt-secret", "answer-secret", "reason-secret", "nested-secret", "tool-secret"):
        assert secret not in response.text
    body = response.json()
    assert body["turns"]["items"][0]["result"] == "password=[REDACTED]"
    assert body["events"]["items"][-1]["payload"]["api_key"] == "[REDACTED]"
    assert body["issue_counts"] == {"invalid_event": 2}
    assert capture == before


@pytest.mark.parametrize("source_state", ["missing", "corrupt", "directory", "symlink"])
def test_archive_survives_unusable_source_history_and_gets_never_write(
    client, register, store, monkeypatch, tmp_path, source_state,
):
    directory = register()
    source = directory / ".co" / "session_results.jsonl"
    if source_state != "missing":
        source.parent.mkdir()
        if source_state == "corrupt":
            source.write_text('{"password":"source-private"')
        elif source_state == "directory":
            source.mkdir()
        else:
            target = tmp_path / "unrelated.jsonl"
            target.write_text("source-private")
            source.symlink_to(target)
    capture = store.add(make_capture())
    before = [(path.relative_to(directory), path.lstat().st_mtime_ns)
              for path in sorted(directory.rglob("*"))]
    for name in ("_history", "_history_source", "read_execution_history"):
        monkeypatch.setattr(sessions_api, name, forbidden)
    for name in ("save", "ensure_dirs", "migrate_capability_aliases"):
        monkeypatch.setattr(registry, name, forbidden)
    for path in ("/api/agents/demo/archives", f"/api/agents/demo/archives/{capture.capture_id}"):
        response = client.get(path)
        assert response.status_code == 200 and "source-private" not in response.text
    assert [(path.relative_to(directory), path.lstat().st_mtime_ns)
            for path in sorted(directory.rglob("*"))] == before


def test_unknown_or_cross_agent_capture_is_404_without_payload_leak(client, register, store):
    register("one"); register("two")
    capture = store.add(make_capture(slug="one"))
    assert client.get(f"/api/agents/one/archives/{capture.capture_id}").status_code == 200
    for path in (f"/api/agents/two/archives/{capture.capture_id}", "/api/agents/one/archives/" + "b" * 64):
        response = client.get(path)
        assert response.status_code == 404 and "first answer" not in response.text
        assert_private(response)


@pytest.mark.parametrize("violation", ["owner", "capture-id"])
def test_api_defends_against_misowned_store_detail(client, register, store, monkeypatch, violation):
    register()
    wanted = make_capture()
    wrong = make_capture(slug="another") if violation == "owner" else make_capture(revision=4)
    monkeypatch.setattr(store, "get_owned", lambda *args: {"capture": wrong, "archived_at": "2026-10-28T00:00:00Z"})
    response = client.get(f"/api/agents/demo/archives/{wanted.capture_id}")
    assert response.status_code == 404 and "first answer" not in response.text


def test_api_rejects_list_records_belonging_to_another_agent(client, register, store, monkeypatch):
    register()
    wrong = make_capture(slug="another")
    monkeypatch.setattr(store, "archive_page", lambda *a, **k: (1, [{
        "capture_id": wrong.capture_id, "session": wrong.session, "archived_at": "2026-10-28T00:00:00Z",
    }]))
    response = client.get("/api/agents/demo/archives")
    assert response.status_code == 503 and "another" not in response.text
    assert_private(response)


@pytest.mark.parametrize("target", ["factory", "list", "detail"])
def test_database_errors_are_safe_503_not_file_fallback(client, register, store, monkeypatch, target):
    register()

    def failed(*args, **kwargs):
        raise ArchiveError("postgresql://private-user:database-secret@private-host/db SQL SELECT private")

    if target == "factory":
        monkeypatch.setattr(archive_api, "_archive", failed)
    else:
        monkeypatch.setattr(store, "archive_page" if target == "list" else "get_owned", failed)
    monkeypatch.setattr(sessions_api, "_history", forbidden)
    path = "/api/agents/demo/archives" + ("/" + "a" * 64 if target == "detail" else "")
    response = client.get(path)
    assert response.status_code == 503
    assert all(secret not in response.text for secret in ("private", "database-secret", "SELECT", "postgresql://"))
    assert_private(response)


@pytest.mark.parametrize("suffix", ["", "/" + "a" * 64])
def test_unregistered_agent_is_rejected_before_store_access(client, monkeypatch, suffix):
    monkeypatch.setattr(archive_api, "_archive", forbidden)
    response = client.get("/api/agents/missing/archives" + suffix)
    assert response.status_code == 404
    assert_private(response)


@pytest.mark.parametrize("slug", ["UPPER", "bad_slug", "bad..slug", "-prefix", "suffix-"])
def test_invalid_slug_is_422_before_store_access(client, monkeypatch, slug):
    monkeypatch.setattr(archive_api, "_archive", forbidden)
    response = client.get(f"/api/agents/{slug}/archives")
    assert response.status_code == 422
    assert_private(response)


@pytest.mark.parametrize("violation, expected", [("mismatch", 409), ("corrupt", 500), ("linked", 403)])
def test_archive_still_requires_valid_registered_metadata(
    client, register, monkeypatch, tmp_path, violation, expected,
):
    directory = register()
    metadata = directory / "meta.json"
    if violation == "mismatch":
        body = json.loads(metadata.read_text())
        body["slug"] = "another-agent"
        metadata.write_text(json.dumps(body))
    elif violation == "corrupt":
        metadata.write_text('{"secret":"metadata-secret"')
    else:
        relocated = tmp_path / "linked-private-metadata.json"
        metadata.rename(relocated)
        metadata.symlink_to(relocated)
    monkeypatch.setattr(archive_api, "_archive", forbidden)
    response = client.get("/api/agents/demo/archives")
    assert response.status_code == expected
    assert "metadata-secret" not in response.text and str(tmp_path) not in response.text
    assert_private(response)


@pytest.mark.parametrize("identity", ["a" * 63, "a" * 65, "g" * 64, "A" * 64, "not-a-capture", "%20"])
def test_invalid_capture_identifier_is_422(client, register, monkeypatch, identity):
    register()
    monkeypatch.setattr(archive_api, "_archive", forbidden)
    response = client.get("/api/agents/demo/archives/" + identity)
    assert response.status_code == 422
    assert_private(response)


@pytest.mark.parametrize("suffix", [
    "?offset=-1", "?limit=0", "?limit=101", "?limit=abc",
    "/{capture}?turn_offset=-1", "/{capture}?turn_limit=0", "/{capture}?turn_limit=101",
    "/{capture}?event_offset=-1", "/{capture}?event_limit=0", "/{capture}?event_limit=501",
    "/{capture}?turn_no=0", "/{capture}?turn_no=-1", "/{capture}?turn_no=abc",
])
def test_invalid_pagination_is_422_before_store_access(client, register, monkeypatch, suffix):
    register()
    monkeypatch.setattr(archive_api, "_archive", forbidden)
    response = client.get("/api/agents/demo/archives" + suffix.format(capture="a" * 64))
    assert response.status_code == 422
    assert_private(response)


@pytest.mark.parametrize("route", ["/api/history/storage", "/api/agents/demo/archives",
                                    "/api/agents/demo/archives/" + "a" * 64])
@pytest.mark.parametrize("headers", [
    {"Origin": "https://attacker.example"}, {"Origin": "null"},
    {"Origin": "http://localhost:9900"}, {"Origin": "http://127.0.0.1:9900/path"},
    {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
])
def test_foreign_browser_requests_fail_local_guard_before_store(client, monkeypatch, route, headers):
    monkeypatch.setattr(archive_api, "_archive", forbidden)
    response = client.get(route, headers=headers)
    assert response.status_code == 403
    assert_private(response)


@pytest.mark.parametrize("base_url, peer, expected", [
    ("http://attacker.example:9900", "127.0.0.1", 400),
    ("http://127.0.0.1:9900", "192.0.2.3", 403),
])
def test_untrusted_host_or_peer_cannot_use_forwarded_headers(agents_root, monkeypatch, base_url, peer, expected):
    monkeypatch.setattr(archive_api, "_archive", forbidden)
    client = TestClient(create_app(), base_url=base_url, client=(peer, 50000))
    try:
        for route in ("/api/history/storage", "/api/agents/demo/archives"):
            response = client.get(route, headers={"X-Forwarded-For": "127.0.0.1", "X-Forwarded-Host": "localhost"})
            assert response.status_code == expected
            assert_private(response)
    finally:
        client.close()


@pytest.mark.parametrize("headers", [{}, {"Origin": "http://127.0.0.1:9900", "Sec-Fetch-Site": "same-origin"},
                                      {"Sec-Fetch-Site": "none"}])
def test_local_ui_and_nonbrowser_clients_are_allowed(client, register, store, headers):
    register()
    assert client.get("/api/agents/demo/archives", headers=headers).status_code == 200
    assert client.get("/api/history/storage", headers=headers).status_code == 200


@pytest.mark.parametrize("route", ["/api/history/storage", "/api/agents/demo/archives",
                                    "/api/agents/demo/archives/" + "a" * 64])
@pytest.mark.parametrize("method", ["post", "put", "delete"])
def test_archive_routes_offer_no_mutation_methods(client, monkeypatch, route, method):
    monkeypatch.setattr(archive_api, "_archive", forbidden)
    response = getattr(client, method)(route)
    assert response.status_code == 405
    assert_private(response)
