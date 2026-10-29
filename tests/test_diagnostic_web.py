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
from co_studio.diagnostic import DEFAULT_MAX_OUTPUT_TOKENS

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
    assert worker.call_args.args[1] == {**BODY, "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("extra", [{"model": ""}, {"model": " bad "}, {"query": " "}, {"base_url": "https://evil.invalid"}, {"k": 20}, {"model": "a" * 129}, {"collection": "../secret"}, {"max_output_tokens": 255}, {"max_output_tokens": 8193}, {"max_output_tokens": "4096"}, {"max_output_tokens": True}, {"max_output_tokens": 4096.0}])
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


@pytest.mark.parametrize("budget", [256, 2048, 4096, 8192])
def test_api_preserves_explicit_budget_without_automatic_increase(client, budget):
    local, worker = client
    response = local.post("/api/knowledge/generate", json={
        **BODY, "allow_model_call": True, "max_output_tokens": budget})
    assert response.status_code == 200
    assert worker.call_count == 1
    assert worker.call_args.args[1]["max_output_tokens"] == budget


def test_validation_envelope_never_echoes_raw_fields_or_provider_input():
    failure = RuntimeError("private-key")
    failure.failure_phase = "schema_validation"
    failure.validation_errors = [
        {"type": "literal_error", "loc": ["status"]},
        {"type": "private-token", "loc": ["secret-user-query", -1, {"private": "key"}]},
        {"type": "missing", "loc": ["hypotheses"], "input": "private-source"},
    ]
    result = web._failure_envelope(failure)
    assert result == {"error": "schema_validation", "issues": [
        {"type": "literal_error", "loc": ["status"]},
        {"type": "validation_error", "loc": ["<field>", "<field>", "<field>"]},
    ]}
    assert "private" not in json.dumps(result)


def test_validation_envelope_transport_only_exposes_safe_issue_summary():
    children, req = [], request()
    payload = {"error": "schema_validation", "issues": [
        {"type": "literal_error", "loc": ["status"]},
        {"type": "private-provider-response", "loc": ["private-key"]}]}
    code = "import sys; print(" + repr(json.dumps(payload)) + "); sys.exit(1)"
    with pytest.raises(HTTPException) as failure:
        asyncio.run(web.run(req, BODY, spawn=spawned(code, children)))
    assert failure.value.status_code == 502
    assert "status: literal_error" in failure.value.detail
    assert "private" not in failure.value.detail
    assert_reaped(children, req)


@pytest.mark.parametrize("budget", [2048, 4096])
def test_worker_main_forwards_exact_budget_and_calls_once(monkeypatch, budget):
    import io
    from co_studio import diagnostic, knowledge
    from unittest.mock import Mock
    generate = Mock(return_value=RESULT)
    monkeypatch.setattr(diagnostic, "generate", generate)
    monkeypatch.setattr(knowledge, "configured_store", lambda: "isolated-store")
    output = io.BytesIO()
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps({**BODY, "max_output_tokens": budget}).encode())))
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=output))
    assert web.main() == 0
    generate.assert_called_once_with("isolated-store", BODY["query"], model=BODY["model"],
                                    collection="public", mode="hybrid", max_output_tokens=budget)
    assert json.loads(output.getvalue()) == RESULT


def result_v2(status="answered"):
    from co_studio.diagnostic import generate
    hit = {"chunk_id": "a" * 64, "source_uri": "studio://qa/document", "version": "1",
           "content": "Reconnect with the saved session ID.", "line_start": 1, "line_end": 1,
           "collection": "public"}
    other = {**hit, "chunk_id": "b" * 64, "source_uri": "studio://qa/other",
             "content": "This client reconnects automatically."}
    class Store:
        def search(self, *args, **kwargs):
            return {"hits": [hit, other] if status == "conflicting_evidence" else [hit]}
        def validate_citations(self, *args):
            return {"valid": True}
    def complete(text, **kwargs):
        units = json.loads(text)["untrusted_evidence"]
        return {"status": status,
                "claims": [{"text": "Documented condition", "evidence_ids": [unit["evidence_id"]]}
                           for unit in units] if status in {"answered", "hypotheses", "conflicting_evidence"} else [],
                "limitations": [],
                "verification_steps": [{"kind": "inspect_session_identifier", "evidence_ids": [units[0]["evidence_id"]]}]
                                      if status in {"answered", "hypotheses"} else [],
                "questions": ["Which client and version are affected?"] if status == "needs_clarification" else []}
    return generate(Store(), "question", model=BODY["model"], complete=complete)


@pytest.mark.parametrize("status", ["answered", "hypotheses", "needs_clarification", "refused", "insufficient_evidence", "conflicting_evidence"])
def test_v2_worker_transport_accepts_all_valid_states_and_exact_server_quotes(status):
    children, req = [], request()
    expected = result_v2(status)
    code = "print(" + repr(json.dumps(expected)) + ")"
    actual = asyncio.run(web.run(req, BODY, spawn=spawned(code, children)))
    assert actual == expected
    assert_reaped(children, req)


@pytest.mark.parametrize("change", ["quote", "unit", "scope", "budget", "model", "protocol", "message",
                                   "missing-field", "action", "kind", "verification-policy", "legacy-v2"])
def test_v2_worker_transport_withholds_forged_or_mismatched_results(change):
    children, req = [], request()
    result = result_v2()
    if change == "quote":
        result["claims"][0]["evidence"][0]["quote"] = "invented quote"
    elif change == "unit":
        result["claims"][0]["evidence_ids"] = ["ev_" + "f" * 32]
    elif change == "scope":
        result["collection"] = "another-agent"
    elif change == "budget":
        result["max_output_tokens"] = 2048
    elif change == "model":
        result["model"] = "different-paid-model"
    elif change == "protocol":
        result["protocol_version"] = "unknown-version"
    elif change == "message":
        result["message"] = "provider injected private content"
    elif change == "action":
        result["verification_steps"][0]["action"] = "Run co init to verify."
    elif change == "kind":
        result["verification_steps"][0]["kind"] = "run_command"
    elif change == "verification-policy":
        result["verification_policy"] = "unknown-version"
    elif change == "legacy-v2":
        del result["verification_policy"]
    else:
        del result["claims"]
    with pytest.raises(HTTPException) as failure:
        asyncio.run(web.run(req, BODY, spawn=spawned("print(" + repr(json.dumps(result)) + ")", children)))
    assert failure.value.status_code == 502
    assert "private" not in failure.value.detail
    assert_reaped(children, req)
