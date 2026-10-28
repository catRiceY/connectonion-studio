import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from co_studio.origin_guard import LocalOriginGuard
from co_studio.app import create_app


@pytest.fixture
def guarded():
    app = FastAPI()
    app.add_middleware(LocalOriginGuard)
    calls = []
    @app.post("/api/agents/fixture/start")
    def start():
        calls.append("start")
        return {"ok": True}
    @app.websocket("/ws/status")
    async def status(ws: WebSocket):
        await ws.accept()
        await ws.send_text("ok")
        await ws.close()
    return TestClient(app, base_url="http://127.0.0.1:9900"), calls


@pytest.mark.parametrize("headers", [
    {"Origin": "https://evil.invalid"}, {"Origin": "null"},
    {"Origin": "http://127.0.0.1:9901"}, {"Origin": "http://localhost:9900"},
    {"Origin": "http://127.0.0.1:9900/"}, {"Origin": "http://user@127.0.0.1:9900"},
    {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
])
def test_cross_site_control_does_not_execute(guarded, headers):
    client, calls = guarded
    assert client.post("/api/agents/fixture/start", headers=headers).status_code == 403
    assert calls == []


@pytest.mark.parametrize("headers", [{}, {"Origin": "http://127.0.0.1:9900", "Sec-Fetch-Site": "same-origin"}])
def test_local_cli_and_browser_work(guarded, headers):
    client, calls = guarded
    assert client.post("/api/agents/fixture/start", headers=headers).status_code == 200
    assert calls == ["start"]


def test_websocket_origin_cannot_read_status(guarded):
    client, _ = guarded
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect("ws://127.0.0.1:9900/ws/status", headers={"Origin": "https://evil.invalid"}):
            pytest.fail("cross-site socket was accepted")
    assert error.value.code == 1008
    with client.websocket_connect("ws://127.0.0.1:9900/ws/status", headers={"Origin": "http://127.0.0.1:9900"}) as ws:
        assert ws.receive_text() == "ok"


@pytest.mark.parametrize("host,expected", [("127.0.0.1:9900", 200), ("localhost:9900", 200), ("[::1]:9900", 200), ("evil.invalid", 400)])
def test_cli_host_policy(host, expected):
    from starlette.middleware.trustedhost import TrustedHostMiddleware
    app = FastAPI()
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"], www_redirect=False)
    @app.get("/")
    def index(): return {"ok": True}
    assert TestClient(app, base_url="http://localhost:9900").get("/", headers={"host": host}).status_code == expected


@pytest.mark.parametrize("host", ["rebind.invalid:9900", "localhost.evil.invalid:9900", "testserver"])
@pytest.mark.parametrize("browser", [False, True])
def test_factory_rejects_untrusted_host_before_control_handler(host, browser):
    app = create_app()
    calls = []

    @app.post("/api/fixture-control")
    def control():
        calls.append("executed")
        return {"ok": True}

    headers = {"X-Forwarded-Host": "localhost:9900", "X-Forwarded-For": "127.0.0.1"}
    if browser:
        headers.update({"Origin": f"http://{host}", "Sec-Fetch-Site": "same-origin"})
    # Do not enter the application lifespan: this regression needs no local data,
    # authentication, Agent processes, or database services.
    client = TestClient(app, base_url=f"http://{host}", client=("127.0.0.1", 50000))
    try:
        assert client.post("/api/fixture-control", headers=headers).status_code == 400
        assert calls == []
    finally:
        client.close()


@pytest.mark.parametrize("host", ["localhost:49152", "127.0.0.1:8080", "[::1]:9900"])
def test_factory_allows_loopback_hosts_with_dynamic_ports(host):
    app = create_app()

    @app.get("/api/fixture-control")
    def control():
        return {"ok": True}

    client = TestClient(app, base_url=f"http://{host}", client=("127.0.0.1", 50000))
    try:
        assert client.get("/api/fixture-control", headers={
            "Origin": f"http://{host}", "Sec-Fetch-Site": "same-origin",
        }).status_code == 200
    finally:
        client.close()
