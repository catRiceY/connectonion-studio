"""Immutable startup identities; synthetic models, paths and function sources."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from co_studio.approvals import ToolsConflict, ToolsInvalid
from co_studio.runtime_manifest import attach_runtime_manifest, save_manifest, snapshot


def example(value: str) -> str:
    return value


def fake_agent(function=example):
    function.name = "example"
    function.get_parameters_schema = lambda: {"type": "object", "properties": {"value": {"type": "string"}}}
    trace = []
    return SimpleNamespace(tools=[function], llm=SimpleNamespace(model="offline-scripted-model"),
        events={}, current_session={"turn": 1}, _record_trace=trace.append, trace=trace)


def test_manifest_contains_hashes_not_prompts_credentials_or_arguments(tmp_path):
    (tmp_path / "agent.py").write_text('SYSTEM_PROMPT = "do-not-serialize-this-prompt"\n')
    (tmp_path / ".env").write_text("API_KEY=do-not-read-this-credential")
    agent = fake_agent()
    config = {"revision": 7, "context_budget": 4096}
    attach_runtime_manifest(agent, agent_dir=tmp_path, configuration=config, run_id="run-test")
    path = tmp_path / ".studio-tools/runtime/run-test.json"
    raw = path.read_bytes()
    manifest = json.loads(raw)
    assert manifest["model"] == "offline-scripted-model"
    assert manifest["connectonion_version"] == "1.9.2"
    assert manifest["config_revision"] == 7
    assert manifest["tools"][0]["source_status"] == "known"
    assert len(manifest["tools"][0]["source_sha256"]) == 64
    assert "do-not-serialize" not in raw.decode() and "do-not-read" not in raw.decode()
    assert "SYSTEM_PROMPT" not in raw.decode() and "arguments" not in manifest
    agent.events["after_user_input"][0](agent)
    assert agent.trace == [{"type": "studio_runtime_manifest", "run_id": "run-test",
        "manifest_sha256": hashlib.sha256(raw).hexdigest(), "config_revision": 7, "turn": 1}]
    assert path.stat().st_mode & 0o777 == 0o600
    assert save_manifest(tmp_path, manifest) == hashlib.sha256(raw).hexdigest()
    with pytest.raises(ToolsConflict):
        save_manifest(tmp_path, manifest | {"model": "different"})
    assert path.read_bytes() == raw


def test_unreadable_source_is_unknown_not_invented(tmp_path):
    namespace = {}
    exec(compile("def generated(value): return value", "<generated>", "exec"), namespace)
    item = snapshot(fake_agent(namespace["generated"]), tmp_path, {"revision": 0}, "run-test")
    assert item["script_status"] == "unknown" and item["script_sha256"] is None
    assert item["tools"][0]["source_status"] == "unknown"
    assert item["tools"][0]["source_sha256"] is None


def test_changed_model_or_script_not_silently_attributed(tmp_path):
    agent = fake_agent()
    attach_runtime_manifest(agent, agent_dir=tmp_path, configuration={}, run_id="run-test")
    agent.llm.model = "different"
    with pytest.raises(ToolsConflict):
        agent.events["after_user_input"][0](agent)
    assert agent.trace == []


def test_manifest_refuses_path_alias_and_invalid_run_id(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / ".studio-tools").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ToolsInvalid):
        attach_runtime_manifest(fake_agent(), agent_dir=agent_dir, configuration={}, run_id="run-test")
    with pytest.raises(ToolsInvalid):
        snapshot(fake_agent(), tmp_path, {}, "../../escape")
    assert not list(outside.iterdir())
