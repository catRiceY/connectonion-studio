"""Exercise SSE over real ASGI messages, without buffering an infinite response.

No sockets, Agent execution, authentication, or user database is involved.
"""
import asyncio
import json

import pytest

from co_studio import config, registry
from co_studio.api import archives, history_stream, sessions
from co_studio.app import create_app
from co_studio.history_events import HistoryEventHub


def forbidden(*args, **kwargs):
    raise AssertionError("SSE must not read source files, query/write SQL, or start Agents")


@pytest.fixture
def app(tmp_path, monkeypatch):
    root = tmp_path / "agents"
    (root / "demo").mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTS_DIR", root)
    registry.save(registry.AgentMeta(slug="demo", name="Synthetic", address="offline", port=8001,
                                    model="offline", capabilities=[], created_at="2026-10-28"))
    monkeypatch.setenv("STUDIO_HISTORY_DATABASE_URL", "postgresql://synthetic:secret@invalid.example/test")
    monkeypatch.delenv("STUDIO_HISTORY_CONFIG", raising=False)
    monkeypatch.setattr(archives, "_archive", forbidden)
    monkeypatch.setattr(sessions, "_history", forbidden)
    return create_app()  # Deliberately no desktop lifespan.


class Connection:
    def __init__(self, app, *, path="/api/agents/demo/history/stream", headers=(),
                 client="127.0.0.1", spec="2.3", method="GET"):
        self.app = app
        self.scope = {
            "type": "http", "asgi": {"version": "3.0", "spec_version": spec},
            "http_version": "1.1", "scheme": "http", "method": method,
            "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
            "headers": [(b"host", b"127.0.0.1:9900"), *headers],
            "client": (client, 50000), "server": ("127.0.0.1", 9900),
        }
        self.messages = []
        self.incoming = asyncio.Queue()
        self.incoming.put_nowait({"type": "http.request", "body": b"", "more_body": False})
        self.changed = asyncio.Event()
        self.task = None

    async def send(self, message):
        self.messages.append(message)
        self.changed.set()

    def start(self):
        self.task = asyncio.create_task(self.app(self.scope, self.incoming.get, self.send))
        return self

    @property
    def body(self):
        return b"".join(m.get("body", b"") for m in self.messages).decode()

    @property
    def status(self):
        return next((m["status"] for m in self.messages if m["type"] == "http.response.start"), None)

    async def until(self, condition):
        async def wait():
            while not condition():
                self.changed.clear()
                if self.task.done():
                    await self.task
                    raise AssertionError(f"Response ended before condition: {self.status}")
                await self.changed.wait()
        await asyncio.wait_for(wait(), 2)

    async def disconnect(self):
        self.incoming.put_nowait({"type": "http.disconnect"})
        await asyncio.wait_for(self.task, 2)


def events(body):
    parsed = []
    for frame in body.split("\n\n"):
        fields = dict(line.split(": ", 1) for line in frame.splitlines() if ": " in line)
        if "event" in fields:
            fields["data"] = json.loads(fields["data"])
            parsed.append(fields)
    return parsed


@pytest.mark.parametrize("spec", ["2.3", "2.4"])
def test_headers_initial_change_heartbeat_and_idle_disconnect(app, monkeypatch, spec):
    monkeypatch.setattr(history_stream, "HEARTBEAT_SECONDS", 0.01)
    async def exercise():
        hub = app.state.history_events = HistoryEventHub()
        conn = Connection(app, spec=spec).start()
        try:
            await conn.until(lambda: "history.reset" in conn.body)
            assert conn.status == 200
            headers = dict(conn.messages[0]["headers"])
            assert headers[b"content-type"] == b"text/event-stream; charset=utf-8"
            assert headers[b"cache-control"] == b"no-store"
            assert headers[b"x-content-type-options"] == b"nosniff"
            assert headers[b"x-accel-buffering"] == b"no"
            assert b"content-length" not in headers
            assert "retry: 2000\n\n" in conn.body
            assert events(conn.body)[0]["data"] == {"agent_slug": "demo", "reason": "initial"}
            # Publication really comes from a different thread, like HistorySync.
            await asyncio.to_thread(hub.publish_capture, "demo", "opaque/session?x=1", "a" * 64)
            await conn.until(lambda: "history.changed" in conn.body)
            await conn.until(lambda: ": keep-alive" in conn.body)
            data = events(conn.body)[1]["data"]
            assert data == {"agent_slug": "demo", "session_id": "opaque/session?x=1", "capture_id": "a" * 64}
            assert hub.subscriber_count == 1
            assert "secret" not in conn.body and "invalid.example" not in conn.body
        finally:
            await conn.disconnect()
            hub.close()
        assert hub.subscriber_count == 0
    asyncio.run(exercise())


def test_reconnect_replay_and_other_agent_isolation(app):
    async def exercise():
        hub = app.state.history_events = HistoryEventHub()
        first = Connection(app).start()
        await first.until(lambda: "history.reset" in first.body)
        cursor = events(first.body)[0]["id"]
        await first.disconnect()
        hub.publish_capture("other", "must-not-leak", "b" * 64)
        hub.publish_capture("demo", "session-1", "a" * 64)
        second = Connection(app, headers=[(b"last-event-id", cursor.encode())]).start()
        try:
            await second.until(lambda: "history.changed" in second.body)
            assert "must-not-leak" not in second.body
            assert len(events(second.body)) == 1
            assert events(second.body)[0]["data"]["session_id"] == "session-1"
        finally:
            await second.disconnect()
            hub.close()
        assert hub.subscriber_count == 0
    asyncio.run(exercise())


@pytest.mark.parametrize("headers,client,path,method,expected", [
    ([(b"origin", b"https://evil.example")], "127.0.0.1", "/api/agents/demo/history/stream", "GET", 403),
    ([(b"sec-fetch-site", b"cross-site")], "127.0.0.1", "/api/agents/demo/history/stream", "GET", 403),
    ([], "192.0.2.1", "/api/agents/demo/history/stream", "GET", 403),
    ([], "127.0.0.1", "/api/agents/absent/history/stream", "GET", 404),
    ([], "127.0.0.1", "/api/agents/demo/history/stream", "POST", 405),
    ([(b"last-event-id", b"invalid")], "127.0.0.1", "/api/agents/demo/history/stream", "GET", 400),
    ([(b"last-event-id", b"x" * 2048)], "127.0.0.1", "/api/agents/demo/history/stream", "GET", 400),
    ([(b"last-event-id", b"one"), (b"last-event-id", b"two")], "127.0.0.1", "/api/agents/demo/history/stream", "GET", 400),
])
def test_rejected_requests_allocate_no_subscription(app, headers, client, path, method, expected):
    async def exercise():
        hub = app.state.history_events = HistoryEventHub()
        conn = Connection(app, headers=headers, client=client, path=path, method=method).start()
        await asyncio.wait_for(conn.task, 2)
        assert conn.status == expected
        assert hub.subscriber_count == 0
        assert "secret" not in conn.body and "invalid.example" not in conn.body
        assert dict(conn.messages[0]["headers"])[b"cache-control"] == b"no-store"
        hub.close()
    asyncio.run(exercise())


@pytest.mark.parametrize("mode,expected", [("files", 409), ("missing-hub", 503),
                                         ("startup-failure", 503), ("closed", 503), ("capacity", 429)])
def test_optional_storage_lifecycle_and_capacity_errors(app, monkeypatch, mode, expected):
    async def exercise():
        hub = app.state.history_events = HistoryEventHub(max_subscribers=1)
        held = None
        if mode == "files":
            monkeypatch.delenv("STUDIO_HISTORY_DATABASE_URL")
        elif mode == "missing-hub":
            app.state.history_events = None
        elif mode == "startup-failure":
            app.state.history_sync_error = True
        elif mode == "closed":
            hub.close()
        elif mode == "capacity":
            held = hub.subscribe("demo")
        conn = Connection(app).start()
        await asyncio.wait_for(conn.task, 2)
        assert conn.status == expected
        if held:
            assert dict(conn.messages[0]["headers"])[b"retry-after"] == b"2"
            held.close()
        assert hub.subscriber_count == 0
        hub.close()
    asyncio.run(exercise())


def test_periodic_reconciliation_and_hub_shutdown(app, monkeypatch):
    monkeypatch.setattr(history_stream, "RECONCILE_SECONDS", 0.025)
    monkeypatch.setattr(history_stream, "HEARTBEAT_SECONDS", 0.01)
    async def exercise():
        hub = app.state.history_events = HistoryEventHub()
        conn = Connection(app).start()
        try:
            await conn.until(lambda: any(e["data"].get("reason") == "reconcile" for e in events(conn.body)))
            hub.close()
            await asyncio.wait_for(conn.task, 2)
            assert hub.subscriber_count == 0
        finally:
            await conn.disconnect()
    asyncio.run(exercise())


def test_agent_removal_revokes_idle_stream(app, monkeypatch):
    monkeypatch.setattr(history_stream, "RECONCILE_SECONDS", 0.02)
    async def exercise():
        hub = app.state.history_events = HistoryEventHub()
        conn = Connection(app).start()
        await conn.until(lambda: "history.reset" in conn.body)
        from fastapi import HTTPException
        def removed(slug):
            raise HTTPException(404, "Agent not found")
        monkeypatch.setattr(archives, "_require_agent", removed)
        await asyncio.wait_for(conn.task, 2)
        assert hub.subscriber_count == 0
        hub.close()
    asyncio.run(exercise())


@pytest.mark.parametrize("blocked_at", ["http.response.start", "http.response.body"])
def test_full_app_send_timeout_is_not_hidden_by_middleware(app, monkeypatch, blocked_at):
    monkeypatch.setattr(history_stream, "SEND_TIMEOUT_SECONDS", 0.02)
    async def exercise():
        hub = app.state.history_events = HistoryEventHub()
        conn = Connection(app)
        normal_send = conn.send
        async def blocked_send(message):
            if message["type"] == blocked_at:
                await asyncio.Event().wait()
            await normal_send(message)
        conn.send = blocked_send
        conn.start()
        await asyncio.wait_for(conn.task, 0.5)
        assert hub.subscriber_count == 0
        hub.close()
    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["headers", "body", "timeout", "cancel"])
def test_response_releases_slot_on_transport_failures(monkeypatch, failure):
    monkeypatch.setattr(history_stream, "SEND_TIMEOUT_SECONDS", 0.02)
    async def exercise():
        hub = HistoryEventHub()
        sub = hub.subscribe("demo")
        async def content():
            yield b"retry: 2000\n\n"
            await asyncio.Event().wait()
        response = history_stream.HistoryStreamResponse(content(), sub)
        reached = asyncio.Event()
        async def send(message):
            is_header = message["type"] == "http.response.start"
            if (failure == "headers" and is_header) or (failure == "body" and not is_header):
                raise OSError("private transport details")
            if failure in {"timeout", "cancel"}:
                reached.set()
                await asyncio.Event().wait()
        async def receive():
            await asyncio.Event().wait()
        task = asyncio.create_task(response({"type": "http"}, receive, send))
        if failure == "cancel":
            await asyncio.wait_for(reached.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await asyncio.wait_for(task, 1)
        assert hub.subscriber_count == 0
        hub.close()
    asyncio.run(exercise())
