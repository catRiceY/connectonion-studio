import asyncio
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from co_studio import diagnostic_web as web
from co_studio.api import knowledge as api
from co_studio.app import create_app

BODY = {"query": "为什么无法连接？", "model": "co/test-model", "collection": "public", "mode": "hybrid"}
RESULT = {"status": "insufficient_evidence", "model_called": False, "hypotheses": [], "evidence": []}


def request(disconnected=None):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()),
                           is_disconnected=disconnected or AsyncMock(return_value=False))


def spawned(script, children):
    async def launch(*args, **kwargs):
        assert args == (sys.executable, "-m", "co_studio.diagnostic_web")
        child = await asyncio.create_subprocess_exec(sys.executable, "-c", script, **kwargs)
        children.append(child)
        return child
    return launch


def assert_reaped(children, req):
    assert children and all(child.returncode is not None for child in children)
    for child in children:
        with pytest.raises(ProcessLookupError):
            os.kill(child.pid, 0)
    assert req.app.state.diagnosis_active == 0


def test_real_worker_transport_success_never_loads_provider():
    children, req = [], request()
    code = "import json,sys; body=json.load(sys.stdin); assert body['model']=='co/test-model'; print(" + repr(json.dumps(RESULT)) + ")"
    result = asyncio.run(web.run(req, BODY, spawn=spawned(code, children)))
    assert result == RESULT
    assert_reaped(children, req)


@pytest.mark.parametrize("code", ["print('invalid json')", "raise SystemExit(1)", "print('{}')"])
def test_invalid_or_failed_worker_withheld(code):
    children, req = [], request()
    with pytest.raises(HTTPException) as failure:
        asyncio.run(web.run(req, BODY, spawn=spawned(code, children)))
    assert failure.value.status_code == 502
    assert_reaped(children, req)


@pytest.mark.parametrize("category,expected", list(web.PROVIDER_FAILURES.items()))
def test_known_provider_failure_is_actionable_and_worker_reaped(category, expected):
    children, req = [], request()
    script = "import sys; print(" + repr(json.dumps({"error": category})) + "); sys.exit(1)"
    with pytest.raises(HTTPException) as failure:
        asyncio.run(web.run(req, BODY, spawn=spawned(script, children)))
    assert (failure.value.status_code, failure.value.detail) == expected
    assert_reaped(children, req)


@pytest.mark.parametrize("payload", [
    {"error": "unknown-secret-provider-body"},
    {"error": "insufficient_credits", "message": "private-key-must-not-leak"},
    {"error": ["insufficient_credits"]},
    {"status": "hypotheses", "hypotheses": []},
])
def test_failed_worker_unknown_or_extra_fields_never_leak(payload):
    children, req = [], request()
    script = "import sys; print(" + repr(json.dumps(payload)) + "); sys.exit(1)"
    with pytest.raises(HTTPException) as failure:
        asyncio.run(web.run(req, BODY, spawn=spawned(script, children)))
    assert failure.value.status_code == 502
    assert "private-key" not in failure.value.detail
    assert "secret-provider" not in failure.value.detail
    assert_reaped(children, req)


def test_success_exit_cannot_spoof_provider_error():
    children, req = [], request()
    with pytest.raises(HTTPException) as failure:
        asyncio.run(web.run(req, BODY, spawn=spawned('print(\'{"error":"insufficient_credits"}\')', children)))
    assert failure.value.status_code == 502
    assert_reaped(children, req)


@pytest.mark.parametrize("kind,category", list(web._ERROR_CATEGORIES.items()))
def test_failure_envelope_keeps_only_allowlisted_category(kind, category):
    error = type(kind, (Exception,), {})("private credential / raw provider response")
    assert web._failure_envelope(error) == {"error": category}
    wrapped = RuntimeError("private user input")
    wrapped.provider_error_type = kind
    assert web._failure_envelope(wrapped) == {"error": category}


def test_failure_envelope_does_not_guess_from_message():
    assert web._failure_envelope(ValueError("InsufficientCreditsError: private data")) is None
    error = RuntimeError("private data")
    error.provider_error_type = {"error": "InsufficientCreditsError"}
    assert web._failure_envelope(error) is None


def test_bounded_output_kills_actual_worker(monkeypatch):
    monkeypatch.setattr(web, "MAX_RESULT_BYTES", 100)
    children, req = [], request()
    with pytest.raises(HTTPException) as failure:
        asyncio.run(web.run(req, BODY, spawn=spawned("import sys,time; print('x'*1000,flush=True); time.sleep(10)", children)))
    assert failure.value.status_code == 502
    assert_reaped(children, req)


def test_deadline_terminates_worker_before_releasing_capacity(monkeypatch):
    monkeypatch.setattr(web, "TIMEOUT_SECONDS", .1)
    children, req = [], request()
    with pytest.raises(HTTPException) as failure:
        asyncio.run(web.run(req, BODY, spawn=spawned("import time; time.sleep(10)", children)))
    assert failure.value.status_code == 504
    assert_reaped(children, req)


def test_disconnect_terminates_worker_without_returning_result():
    children, req = [], request(AsyncMock(side_effect=[False, False, True]))
    with pytest.raises(HTTPException) as failure:
        asyncio.run(web.run(req, BODY, spawn=spawned("import time; time.sleep(10)", children)))
    assert failure.value.status_code == 499
    assert_reaped(children, req)


def test_cancel_task_reaps_worker_and_restores_capacity():
    children, req = [], request()
    async def check():
        task = asyncio.create_task(web.run(req, BODY, spawn=spawned("import time; time.sleep(10)", children)))
        while not children:
            await asyncio.sleep(.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(check())
    assert_reaped(children, req)


def test_cancellation_during_spawn_still_reaps_owned_child():
    children, req = [], request()
    async def check():
        async def slow_spawn(*args, **kwargs):
            child = await spawned("import time; time.sleep(10)", children)(*args, **kwargs)
            await asyncio.sleep(.05)
            return child
        task = asyncio.create_task(web.run(req, BODY, spawn=slow_spawn))
        while not children:
            await asyncio.sleep(.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(check())
    assert_reaped(children, req)


def test_capacity_and_pre_dispatch_cancel_never_spawn():
    for active, disconnected, status in [(2, False, 429), (0, True, 499)]:
        req = request(AsyncMock(return_value=disconnected)); req.app.state.diagnosis_active = active
        launch = AsyncMock()
        with pytest.raises(HTTPException) as failure:
            asyncio.run(web.run(req, BODY, spawn=launch))
        assert failure.value.status_code == status
        launch.assert_not_called()
        assert req.app.state.diagnosis_active == active


@pytest.fixture
def client(monkeypatch):
    fake = AsyncMock(return_value=RESULT)
    monkeypatch.setattr(web, "run", fake)
    client = TestClient(create_app(), base_url="http://127.0.0.1:9900", client=("127.0.0.1", 40000))
    return client, fake


def test_api_requires_explicit_boolean_consent_before_dispatch(client):
    client, worker = client
    for consent in [None, False, 1, "true"]:
        body = {**BODY, **({} if consent is None else {"allow_model_call": consent})}
        assert client.post("/api/knowledge/generate", json=body).status_code == 422
    worker.assert_not_called()
    response = client.post("/api/knowledge/generate", json={**BODY, "allow_model_call": True})
    assert response.status_code == 200
    assert worker.call_args.args[1] == BODY
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("extra", [{"model": ""}, {"model": " bad "}, {"query": " "}, {"base_url": "https://evil.invalid"}, {"k": 20}, {"model": "a" * 129}, {"collection": "../secret"}])
def test_api_invalid_requests_never_dispatch(client, extra):
    client, worker = client
    assert client.post("/api/knowledge/generate", json={**BODY, "allow_model_call": True, **extra}).status_code == 422
    worker.assert_not_called()


def test_api_origin_remote_and_unknown_scope_blocked(client, monkeypatch):
    from co_studio import registry
    monkeypatch.setattr(registry, "load_all", lambda: [])
    local, worker = client
    body = {**BODY, "allow_model_call": True}
    assert local.post("/api/knowledge/generate", json=body, headers={"Origin": "https://evil.invalid"}).status_code == 403
    assert local.post("/api/knowledge/generate", json={**body, "collection": "agent-" + "a" * 24}).status_code == 404
    remote = TestClient(create_app(), base_url="http://127.0.0.1:9900", client=("192.0.2.1", 40000))
    assert remote.post("/api/knowledge/generate", json=body).status_code == 403
    worker.assert_not_called()


def test_api_keeps_registered_agent_scope_without_public_fallback(client, monkeypatch):
    from co_studio import registry
    from co_studio.knowledge_sync import collection_for
    monkeypatch.setattr(registry, "load_all", lambda: [SimpleNamespace(slug="alpha")])
    local, worker = client
    collection = collection_for("alpha")
    response = local.post("/api/knowledge/generate", json={**BODY, "allow_model_call": True, "collection": collection})
    assert response.status_code == 200
    assert worker.call_args.args[1]["collection"] == collection


def test_spawn_failure_still_restores_capacity():
    req = request()
    launch = AsyncMock(side_effect=OSError("unable to spawn"))
    with pytest.raises(OSError):
        asyncio.run(web.run(req, BODY, spawn=launch))
    assert req.app.state.diagnosis_active == 0
