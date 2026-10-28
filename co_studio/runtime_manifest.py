"""Immutable, credential-free startup fingerprints for opt-in custom Agents."""
from __future__ import annotations

import hashlib
from importlib.metadata import version
import inspect
import json
import os
from pathlib import Path
import re
import uuid

from filelock import FileLock

from .approvals import ToolsConflict, ToolsInvalid, _root, _safe
from .execution_privacy import redact_text


def _encoded(value):
    try:
        data = json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError):
        raise ToolsInvalid("Runtime manifest must contain finite JSON") from None
    if len(data) > 131072:
        raise ToolsInvalid("Runtime manifest exceeds 128 KiB")
    return data


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def snapshot(agent, agent_dir, configuration, run_id):
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id):
        raise ToolsInvalid("Invalid hosted run identifier")
    tools = []
    for tool in agent.tools:
        source_hash = None
        try:
            source_hash = _sha(inspect.getsource(inspect.unwrap(tool)).encode("utf-8"))
        except (OSError, TypeError, UnicodeError):
            pass  # Native/builtin/dynamic functions have no readable source.
        tools.append({"name": tool.name, "schema_sha256": _sha(_encoded(tool.get_parameters_schema())),
                      "source_sha256": source_hash, "source_status": "known" if source_hash else "unknown"})
    path = _safe(Path(agent_dir) / "agent.py")
    script_hash = None
    if path.exists():
        with path.open("rb") as handle:
            data = handle.read(1_048_577)
        if len(data) > 1_048_576:
            raise ToolsInvalid("Generated Agent script exceeds snapshot limit")
        script_hash = _sha(data)
    model = getattr(agent.llm, "model", None)
    model = redact_text(model[:256]) if isinstance(model, str) else "unknown"
    return {"schema_version": 1, "run_id": run_id, "model": model,
            "connectonion_version": version("connectonion"),
            "script_sha256": script_hash, "script_status": "known" if script_hash else "unknown",
            "config_revision": configuration.get("revision"),
            "config_sha256": _sha(_encoded(configuration)),
            "tools": sorted(tools, key=lambda tool: tool["name"])}


def save_manifest(agent_dir, manifest):
    root = _root(Path(agent_dir), create=True)
    directory = _safe(root / "runtime")
    directory.mkdir(mode=0o700, exist_ok=True)
    data = _encoded(manifest)
    run_id = manifest["run_id"]
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id):
        raise ToolsInvalid("Invalid hosted run identifier")
    path = _safe(directory / f"{run_id}.json")
    with FileLock(str(_safe(root / "runtime.lock")), timeout=5):
        if path.exists():
            with path.open("rb") as handle:
                existing = handle.read(131073)
            if existing != data:
                raise ToolsConflict("Runtime changed within one hosted process; restart required")
        else:
            temporary = directory / f"{uuid.uuid4().hex}.tmp"
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
    return _sha(data)


def attach_runtime_manifest(agent, *, agent_dir, configuration, run_id):
    manifest = snapshot(agent, agent_dir, configuration, run_id)
    digest = save_manifest(agent_dir, manifest)

    def record(runtime_agent):
        # Hooks may change the model or registry: do not silently attribute that
        # turn to the original startup fingerprint. Restart creates a new run.
        current = snapshot(runtime_agent, agent_dir, configuration, run_id)
        if current != manifest:
            raise ToolsConflict("Runtime changed after startup; restart required")
        runtime_agent._record_trace({"type": "studio_runtime_manifest", "run_id": run_id,
            "manifest_sha256": digest, "config_revision": manifest["config_revision"],
            "turn": runtime_agent.current_session.get("turn")})
    agent.events.setdefault("after_user_input", []).append(record)
    return agent
