"""Read-only HTTP contracts against the real app and synthetic agent storage.

TestClient is deliberately NOT entered as a context manager: the desktop
lifespan may authenticate, migrate user data, or adopt running Agent processes.
"""

import json
from pathlib import Path
from urllib.parse import quote

from fastapi.testclient import TestClient
import pytest

from co_studio import config, registry
from co_studio.app import create_app
from co_studio.api import sessions as sessions_api


def event(kind, identity, **fields):
    return {"type": kind, "id": identity, "ts": 100.0, **fields}


def snapshot(session_id="session-1", **overrides):
    raw = {
        "session_id": session_id, "status": "done", "prompt": "second question",
        "result": "second answer", "created": 100.0, "updated_at": 103.0,
        "revision": 4, "duration_ms": 25,
        "session": {
            "session_id": session_id, "turn": 2,
            "trace": [
                event("user_input", "u1", turn=1, content="first question"),
                event("llm_call", "l1", model="offline", status="running"),
                event("llm_result", "l1", model="offline", duration_ms=10,
                      status="success", usage={"input_tokens": 8, "output_tokens": 3}),
                event("turn_result", "r1", turn=1, reason="natural"),
                event("user_input", "u2", turn=2, content="second question"),
                event("tool_call", "t1", tool_id="tool-1", name="read_file",
                      args={"path": "example.txt"}),
                event("tool_result", "t2", tool_id="tool-1", name="read_file",
                      status="success", timing_ms=2.5, result="hello"),
                event("turn_result", "r2", turn=2, reason="natural"),
            ],
        },
    }
    raw.update(overrides)
    return raw


@pytest.fixture
def agents_root(tmp_path, monkeypatch):
    root = tmp_path / "agents"
    root.mkdir()
    monkeypatch.setattr(config, "AGENTS_DIR", root)
    return root


@pytest.fixture
def agent(agents_root):
    def register(slug="demo"):
        directory = agents_root / slug
        directory.mkdir()
        registry.save(registry.AgentMeta(
            slug=slug, name="Synthetic demo", address="0xsynthetic", port=9911,
            model="offline", capabilities=[], created_at="2026-10-27T00:00:00Z",
        ))
        return directory
    return register


def write_history(directory, *rows):
    source = directory / ".co" / "session_results.jsonl"
    source.parent.mkdir(exist_ok=True)
    source.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return source


@pytest.fixture
def client(agents_root):
    client = TestClient(create_app(), base_url="http://127.0.0.1:9900",
                        client=("127.0.0.1", 50000))
    try:
        yield client
    finally:
        client.close()


def test_registered_app_exposes_list_and_detail_without_lifespan(client, agent, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("History requests must not start or mutate runtime state")

    directory = agent()
    write_history(directory, snapshot())
    monkeypatch.setattr(registry, "ensure_dirs", forbidden)
    monkeypatch.setattr(registry, "migrate_capability_aliases", forbidden)
    monkeypatch.setattr(registry, "save", forbidden)
    listed = client.get("/api/agents/demo/sessions")
    detailed = client.get("/api/agents/demo/sessions/session-1")
    assert listed.status_code == detailed.status_code == 200
    assert listed.json()["agent_slug"] == "demo"
    assert listed.json()["source_exists"] is True
    assert listed.json()["issue_counts"] == {}
    assert listed.json()["sessions"]["total"] == 1
    body = detailed.json()
    assert body["session"]["source_revision"] == 4
    assert body["turns"]["total"] == 2
    assert body["events"]["total"] == 8
    assert [turn["turn_no"] for turn in body["turns"]["items"]] == [1, 2]
    assert body["turns"]["items"][0]["result"] is None
    assert body["turns"]["items"][1]["result"] == "second answer"
    assert body["events"]["items"][6]["duration_ms"] == 2.5
    assert body["events"]["items"][5]["call_id"] == body["events"]["items"][6]["call_id"]
    for response in (listed, detailed):
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"


def test_restored_answer_is_filtered_paginated_and_redacted_like_latest_answer(client, agent):
    first = snapshot(prompt="first question", result="password=earlier-secret",
                     duration_ms=0, revision=2, updated_at=101.0)
    first["session"]["turn"] = 1
    first["session"]["trace"] = first["session"]["trace"][:4]
    path = write_history(agent(), first, snapshot())
    before = path.read_bytes(), path.stat().st_mtime_ns
    response = client.get("/api/agents/demo/sessions/session-1?turn_no=1&event_limit=2")
    assert response.status_code == 200
    body = response.json()
    assert body["issue_counts"] == {}
    assert body["session"]["source_revision"] == 4
    assert body["turns"]["total"] == 1
    turn = body["turns"]["items"][0]
    assert turn["turn_no"] == 1
    assert turn["result"] == "password=[REDACTED]"
    assert turn["duration_ms"] == 0
    assert "earlier-secret" not in response.text
    assert body["events"]["total"] == 4
    assert body["events"]["next_offset"] == 2
    assert response.headers["cache-control"] == "no-store"
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_list_uses_latest_snapshot_and_sorts_known_time_first(client, agent):
    directory = agent()
    write_history(directory,
                  snapshot("tie-b", updated_at=105),
                  snapshot("unknown", created=None, updated_at=None),
                  snapshot("latest", updated_at=101, revision=50),
                  snapshot("tie-a", updated_at=105),
                  snapshot("fallback", created=104, updated_at=None),
                  snapshot("latest", updated_at=106, revision=1))
    body = client.get("/api/agents/demo/sessions?limit=2").json()
    page = body["sessions"]
    assert [item["session_id"] for item in page["items"]] == ["latest", "tie-a"]
    assert page["items"][0]["source_revision"] == 1
    assert (page["total"], page["offset"], page["limit"], page["next_offset"]) == (5, 0, 2, 2)
    tail = client.get("/api/agents/demo/sessions?offset=2&limit=10").json()["sessions"]
    assert [item["session_id"] for item in tail["items"]] == ["tie-b", "fallback", "unknown"]
    assert tail["next_offset"] is None


def test_detail_has_independent_turn_and_event_pagination(client, agent):
    write_history(agent(), snapshot())
    body = client.get("/api/agents/demo/sessions/session-1?turn_offset=1&turn_limit=1"
                      "&event_offset=3&event_limit=2").json()
    assert [turn["turn_no"] for turn in body["turns"]["items"]] == [2]
    assert body["turns"]["next_offset"] is None
    assert body["turns"]["total"] == 2
    assert [item["sequence"] for item in body["events"]["items"]] == [3, 4]
    assert body["events"]["next_offset"] == 5
    assert body["events"]["total"] == 8


def test_turn_filter_applies_before_pagination_to_both_collections(client, agent):
    write_history(agent(), snapshot())
    body = client.get("/api/agents/demo/sessions/session-1?turn_no=2&event_offset=1&event_limit=2").json()
    assert [turn["turn_no"] for turn in body["turns"]["items"]] == [2]
    assert body["turns"]["total"] == 1
    assert [item["sequence"] for item in body["events"]["items"]] == [5, 6]
    assert body["events"]["total"] == 4
    assert body["events"]["next_offset"] == 3
    empty = client.get("/api/agents/demo/sessions/session-1?turn_no=99")
    assert empty.status_code == 200
    for key in ("turns", "events"):
        assert empty.json()[key]["items"] == []
        assert empty.json()[key]["total"] == 0
        assert empty.json()[key]["next_offset"] is None


def test_offsets_beyond_end_return_empty_pages(client, agent):
    write_history(agent(), snapshot())
    page = client.get("/api/agents/demo/sessions?offset=50").json()["sessions"]
    assert page["items"] == [] and page["total"] == 1 and page["next_offset"] is None
    body = client.get("/api/agents/demo/sessions/session-1?turn_offset=50&event_offset=50").json()
    for key in ("turns", "events"):
        assert body[key]["items"] == [] and body[key]["next_offset"] is None


@pytest.mark.parametrize("suffix", [
    "?offset=-1", "?limit=0", "?limit=101", "?limit=abc",
    "/session-1?turn_offset=-1", "/session-1?turn_limit=0", "/session-1?turn_limit=101",
    "/session-1?event_offset=-1", "/session-1?event_limit=0", "/session-1?event_limit=501",
    "/session-1?turn_no=0", "/session-1?turn_no=-1", "/session-1?turn_no=abc",
])
def test_invalid_pagination_and_turn_parameters_are_rejected(client, agent, suffix):
    agent()
    assert client.get("/api/agents/demo/sessions" + suffix).status_code == 422


@pytest.mark.parametrize("slug", ["UPPER", "bad_slug", "bad..slug", "-prefix", "suffix-"])
def test_invalid_slug_does_not_reach_reader(client, monkeypatch, slug):
    monkeypatch.setattr(sessions_api, "read_execution_history", lambda *a, **k: pytest.fail("invalid slug read"))
    assert client.get(f"/api/agents/{slug}/sessions").status_code == 422


def test_unregistered_agent_is_404_before_history_read(client, agents_root, monkeypatch):
    directory = agents_root / "unregistered"
    directory.mkdir()
    write_history(directory, snapshot())
    monkeypatch.setattr(sessions_api, "read_execution_history", lambda *a, **k: pytest.fail("unregistered read"))
    assert client.get("/api/agents/unregistered/sessions").status_code == 404
    assert client.get("/api/agents/missing/sessions/session-1").status_code == 404


def test_missing_history_is_empty_and_does_not_create_anything(client, agent):
    directory = agent()
    before = sorted(path.relative_to(directory) for path in directory.rglob("*"))
    response = client.get("/api/agents/demo/sessions")
    assert response.status_code == 200
    assert response.json()["source_exists"] is False
    assert response.json()["sessions"]["items"] == []
    assert response.json()["issue_counts"] == {}
    assert client.get("/api/agents/demo/sessions/missing").status_code == 404
    assert sorted(path.relative_to(directory) for path in directory.rglob("*")) == before
    assert not (directory / ".co").exists()


def test_existing_history_without_requested_session_is_404(client, agent):
    write_history(agent(), snapshot())
    assert client.get("/api/agents/demo/sessions/missing").status_code == 404


@pytest.mark.parametrize("route, headers, status", [
    ("/api/agents/missing/sessions", {}, 404),
    ("/api/agents/demo/sessions?limit=0", {}, 422),
    ("/api/agents/demo/sessions", {"Origin": "https://attacker.example"}, 403),
])
def test_error_responses_are_not_cached(client, route, headers, status):
    response = client.get(route, headers=headers)
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_agents_with_same_session_id_never_mix_data(client, agent):
    write_history(agent("one"), snapshot(result="one-only answer"))
    write_history(agent("two"), snapshot(result="two-only answer"))
    one = client.get("/api/agents/one/sessions/session-1").json()
    two = client.get("/api/agents/two/sessions/session-1").json()
    assert one["turns"]["items"][1]["result"] == "one-only answer"
    assert two["turns"]["items"][1]["result"] == "two-only answer"
    assert one["events"]["items"][0]["event_id"] != two["events"]["items"][0]["event_id"]
    assert all(item["agent_slug"] == "two" for item in two["events"]["items"])


def test_opaque_session_id_with_encoded_slash_is_lookup_not_path(client, agent):
    session_id = "tenant/order:1"
    write_history(agent(), snapshot(session_id))
    response = client.get("/api/agents/demo/sessions/" + quote(session_id, safe=""))
    assert response.status_code == 200
    assert response.json()["session"]["session_id"] == session_id
    assert client.get("/api/agents/demo/sessions/%2E%2E%2Fmeta.json").status_code == 404


def test_caller_cannot_select_source_path(client, agent, tmp_path):
    write_history(agent(), snapshot("safe"))
    outside = tmp_path / "outside.jsonl"
    outside.write_text(json.dumps(snapshot("outside-private")) + "\n")
    response = client.get("/api/agents/demo/sessions", params={"path": str(outside)})
    assert response.status_code == 200
    assert [item["session_id"] for item in response.json()["sessions"]["items"]] == ["safe"]
    assert "outside-private" not in response.text


def test_response_redacts_credentials_and_excludes_raw_envelope(client, agent):
    raw = snapshot(result="password=answer-secret")
    raw["prompt"] = raw["session"]["trace"][4]["content"] = "password=prompt-secret"
    raw["session"]["trace"][5]["args"] = {
        "api_key": "structured-secret", "message": "password=argument-secret", "input_tokens": 42,
    }
    raw["session"]["trace"][6]["result"] = "password=tool-secret"
    raw["session"]["messages"] = [{"role": "system", "content": "raw-system-secret"}]
    raw["session"]["requester"] = {"address": "raw-requester-secret"}
    raw["metadata"] = {"secret": "raw-metadata-secret"}
    raw["session"]["trace"][6]["unknown_extra"] = "raw-extra-secret"
    source = write_history(agent(), raw)
    before = source.read_bytes()
    before_mtime = source.stat().st_mtime_ns
    response = client.get("/api/agents/demo/sessions/session-1")
    assert response.status_code == 200
    for secret in ("answer-secret", "prompt-secret", "structured-secret", "argument-secret",
                   "tool-secret", "raw-system-secret", "raw-requester-secret",
                   "raw-metadata-secret", "raw-extra-secret", str(source)):
        assert secret not in response.text
    body = response.json()
    assert body["turns"]["items"][1]["prompt"] == "password=[REDACTED]"
    assert body["turns"]["items"][1]["result"] == "password=[REDACTED]"
    args = body["events"]["items"][5]["payload"]["args"]
    assert args == {"api_key": "[REDACTED]", "message": "password=[REDACTED]", "input_tokens": 42}
    assert source.read_bytes() == before
    assert source.stat().st_mtime_ns == before_mtime
    assert set(body) == {"session", "turns", "events", "issue_counts"}


def test_list_contains_no_conversation_payload(client, agent):
    raw = snapshot(result="private-result", prompt="private-prompt")
    write_history(agent(), raw)
    response = client.get("/api/agents/demo/sessions")
    assert response.status_code == 200
    for text in ("private-result", "private-prompt", "first question", "example.txt", "trace", "payload"):
        assert text not in response.text


def test_corrupt_rows_report_safe_counts_and_keep_valid_sessions(client, agent):
    raw = snapshot()
    raw["session"]["trace"].append(event("future-kind", "unknown"))
    source = write_history(agent(), raw)
    source.write_text(source.read_text() + '{"password":"raw-corrupt-secret"\n' + 'null\n')
    response = client.get("/api/agents/demo/sessions")
    assert response.status_code == 200
    assert response.json()["sessions"]["total"] == 1
    assert response.json()["issue_counts"] == {"unknown_event_kind": 1, "invalid_snapshot": 2}
    assert "raw-corrupt-secret" not in response.text
    assert "line_no" not in response.text
    assert str(source) not in response.text


@pytest.mark.parametrize("target", ["agent", "metadata", "co", "history"])
def test_symlink_descendants_are_refused(client, agent, agents_root, tmp_path, target):
    directory = agent()
    source = write_history(directory, snapshot())
    if target == "agent":
        moved = tmp_path / "linked-agent"
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
    elif target == "metadata":
        metadata = directory / "meta.json"
        moved = tmp_path / "linked-meta.json"
        metadata.rename(moved)
        metadata.symlink_to(moved)
    elif target == "co":
        moved = tmp_path / "linked-co"
        source.parent.rename(moved)
        source.parent.symlink_to(moved, target_is_directory=True)
    else:
        moved = tmp_path / "linked-history.jsonl"
        source.rename(moved)
        source.symlink_to(moved)
    response = client.get("/api/agents/demo/sessions")
    assert response.status_code == 403
    assert str(tmp_path) not in response.text


def test_mismatched_metadata_identity_is_conflict(client, agent):
    directory = agent()
    metadata = directory / "meta.json"
    raw = json.loads(metadata.read_text())
    raw["slug"] = "someone-else"
    metadata.write_text(json.dumps(raw))
    assert client.get("/api/agents/demo/sessions").status_code == 409


@pytest.mark.parametrize("content", ['{"secret":"metadata-secret"', "[]", '{}'])
def test_corrupt_metadata_returns_generic_server_error(client, agent, content):
    directory = agent()
    (directory / "meta.json").write_text(content)
    response = client.get("/api/agents/demo/sessions")
    assert response.status_code == 500
    assert "metadata-secret" not in response.text
    assert str(directory) not in response.text


def test_history_io_failure_is_not_misrepresented_as_empty(client, agent, monkeypatch):
    source = write_history(agent(), snapshot())
    original_open = Path.open

    def denied(path, *args, **kwargs):
        if path == source:
            raise PermissionError(f"private-error-detail: {source}")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    response = client.get("/api/agents/demo/sessions")
    assert response.status_code == 500
    assert "private-error-detail" not in response.text
    assert str(source) not in response.text


@pytest.mark.parametrize("origin", [
    "https://attacker.example", "null", "http://127.0.0.1:9999",
    "http://localhost:9900", "http://127.0.0.1:9900/path",
])
def test_foreign_origin_is_rejected_before_read(client, monkeypatch, origin):
    monkeypatch.setattr(sessions_api, "read_execution_history", lambda *a, **k: pytest.fail("foreign origin read"))
    response = client.get("/api/agents/demo/sessions", headers={"Origin": origin})
    assert response.status_code == 403


@pytest.mark.parametrize("fetch_site", ["cross-site", "same-site"])
def test_foreign_fetch_site_is_rejected(client, fetch_site):
    assert client.get("/api/agents/demo/sessions", headers={"Sec-Fetch-Site": fetch_site}).status_code == 403


@pytest.mark.parametrize("headers", [
    {}, {"Origin": "http://127.0.0.1:9900", "Sec-Fetch-Site": "same-origin"},
    {"Sec-Fetch-Site": "none"},
])
def test_same_origin_and_local_nonbrowser_clients_are_allowed(client, agent, headers):
    agent()
    assert client.get("/api/agents/demo/sessions", headers=headers).status_code == 200


@pytest.mark.parametrize("base_url, peer, expected", [
    ("http://attacker.example:9900", "127.0.0.1", 400),
    ("http://127.0.0.1:9900", "192.0.2.3", 403),
])
def test_untrusted_host_or_peer_cannot_spoof_local_access(agent, base_url, peer, expected):
    agent()
    client = TestClient(create_app(), base_url=base_url, client=(peer, 50000))
    try:
        response = client.get("/api/agents/demo/sessions", headers={
            "X-Forwarded-For": "127.0.0.1", "X-Forwarded-Host": "localhost:9900",
        })
        assert response.status_code == expected
    finally:
        client.close()


@pytest.mark.parametrize("base_url, peer", [
    ("http://localhost:9900", "127.0.0.1"), ("http://[::1]:9900", "::1"),
])
def test_localhost_and_ipv6_loopback_clients_work(agent, base_url, peer):
    agent()
    client = TestClient(create_app(), base_url=base_url, client=(peer, 50000))
    try:
        assert client.get("/api/agents/demo/sessions", headers={"Origin": base_url}).status_code == 200
    finally:
        client.close()
