"""Signed, content-free observations of Studio's provider input preparation.

These are estimates, not provider usage or proof of model/task success. Runtime
setup alone creates the key; history readers only verify existing local proof.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import secrets

from filelock import FileLock

from .context_policy import ContextError, ESTIMATOR
from .prompt_versions import PromptError, _new_file, _read, _safe

SIGNATURE = "_studio_context_signature"
COUNTS = ("input_budget", "full_estimated_tokens", "estimated_tokens", "source_messages",
          "retained_messages", "input_messages", "task_fields", "tool_references",
          "omitted_tool_references")
FIELDS = ("report_version", "status", "llm_call_id", "policy_version", "estimator",
          *COUNTS, "compacted", "summary_kind")
_STATUSES = {"prepared", "budget_exceeded", "invalid_context", "unknown"}
# Policy evolution must not invalidate authenticated, immutable old captures.
_POLICY_VERSIONS = ("studio-extractive-v1", "studio-extractive-v2", "studio-extractive-v3")


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def signing_key(agent_dir: Path, *, create=False) -> bytes:
    try:
        directory = _safe(Path(agent_dir).absolute())
        root = _safe(directory / ".studio-context")
        path = _safe(root / "reports.key")
        if create:
            root.mkdir(mode=0o700, exist_ok=True)
            with FileLock(str(_safe(root / "reports.lock")), timeout=10):
                if not path.exists():
                    _new_file(path, secrets.token_hex(32))
        text = _read(path, 128)
        if len(text) != 64 or any(char not in "0123456789abcdef" for char in text) or path.stat().st_mode & 0o077:
            raise ContextError("Context report key failed validation")
        return bytes.fromhex(text)
    except (PromptError, OSError, TimeoutError, ValueError):
        raise ContextError("Context report storage is unavailable") from None


def sign_event(key: bytes, event: dict, agent_slug: str, session_id: str) -> str:
    body = {"domain": "studio-context-report-v1", "agent_slug": agent_slug,
            "session_id": session_id, "event": {k: v for k, v in event.items() if k != SIGNATURE}}
    return hmac.new(key, _canonical(body), hashlib.sha256).hexdigest()


def identifier(value) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 512 and not any(char.isspace() or ord(char) < 32 for char in value)


def unknown_payload(llm_call_id=None) -> dict:
    return {"report_version": 1, "status": "unknown", "llm_call_id": llm_call_id if identifier(llm_call_id) else None,
            "policy_version": None, "estimator": None, **{key: None for key in COUNTS},
            "compacted": None, "summary_kind": None}


def valid_payload(payload: dict) -> bool:
    if (set(payload) != set(FIELDS) or type(payload["report_version"]) is not int
            or payload["report_version"] != 1 or payload["status"] not in _STATUSES
            or not identifier(payload["llm_call_id"])):
        return False
    if payload["policy_version"] not in _POLICY_VERSIONS or payload["estimator"] != ESTIMATOR:
        return False
    if any(value is not None and (type(value) is not int or value < 0) for value in (payload[key] for key in COUNTS)):
        return False
    if type(payload["input_budget"]) is not int or not 128 <= payload["input_budget"] <= 2_000_000:
        return False
    if payload["status"] == "prepared":
        if (any(payload[key] is None for key in COUNTS) or type(payload["compacted"]) is not bool
                or payload["summary_kind"] != ("deterministic_extract_not_semantic" if payload["compacted"] else "none")):
            return False
        if (payload["estimated_tokens"] > payload["input_budget"]
                or payload["retained_messages"] > payload["source_messages"]
                or payload["input_messages"] != payload["retained_messages"] + int(payload["compacted"])):
            return False
        return payload["compacted"] or payload["retained_messages"] == payload["source_messages"]
    return (payload["status"] in {"budget_exceeded", "invalid_context"}
            and payload["estimated_tokens"] is None and payload["compacted"] is None
            and payload["summary_kind"] is None
            and all(payload[key] is None for key in ("retained_messages", "input_messages", "task_fields", "tool_references", "omitted_tool_references")))


def event_verifier(agent_dir: Path):
    try:
        key = signing_key(agent_dir)
    except (ContextError, TypeError, ValueError):
        key = None

    def verify(event: dict, agent_slug: str, session_id: str) -> bool:
        if key is None or not isinstance(event, dict) or event.get("type") != "context_report":
            return False
        try:
            proof = event.get(SIGNATURE)
            return (type(event.get("turn")) is int and event["turn"] >= 1
                    and valid_payload({name: event.get(name) for name in FIELDS})
                    and isinstance(proof, str)
                    and hmac.compare_digest(proof, sign_event(key, event, agent_slug, session_id)))
        except (TypeError, ValueError, UnicodeError, RecursionError):
            return False
    return verify


def history_event_verifier(agent_dir: Path):
    """Lazy dispatch keeps legacy/prompt-only reads unchanged and read-only."""
    verifiers = {}

    def verify(event, agent_slug, session_id):
        kind = event.get("type")
        if kind not in {"prompt_provenance", "context_report"}:
            return False
        if kind not in verifiers:
            if kind == "prompt_provenance":
                from .prompt_provenance import event_verifier as prompt_verifier
                verifiers[kind] = prompt_verifier(agent_dir)
            else:
                verifiers[kind] = event_verifier(agent_dir)
        return verifiers[kind](event, agent_slug, session_id)
    return verify
