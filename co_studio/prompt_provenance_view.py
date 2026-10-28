"""Read-only prompt attribution from recorded events, never current settings.

These response models are deliberately separate from the persisted v1 execution
models: adding defaults to those models would change old capture fingerprints.
"""
from __future__ import annotations

from collections import defaultdict
import re
from typing import Literal, Sequence

from pydantic import BaseModel, ConfigDict

from .execution_models import ExecutionEvent


class PromptProvenanceView(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    status: Literal["bound", "unknown"] = "unknown"
    version_id: str | None = None
    base_sha256: str | None = None
    reason: Literal["recorded", "not_recorded", "incomplete", "invalid", "unbound", "conflict"] = "not_recorded"


_VERSION = re.compile(r"(?:baseline|[a-f0-9]{32})\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_BOUND_REASONS = frozenset({"new_session", "restored_session"})
_REASONS = _BOUND_REASONS | {"unverified_session", "missing_session", "system_modified", "unverified_event"}
_FIELDS = frozenset({
    "provenance_version", "status", "version_id", "base_sha256", "system_sha256",
    "launch_version_id", "llm_call_id", "model", "reason",
})


def _identifier(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 512 and not any(char.isspace() or ord(char) < 32 for char in value)


def _matches(pattern, value: object) -> bool:
    return isinstance(value, str) and bool(pattern.fullmatch(value))


def _valid_payload(payload: dict) -> bool:
    if (set(payload) != _FIELDS or type(payload["provenance_version"]) is not int
            or payload["provenance_version"] != 1 or payload["status"] not in ("bound", "unknown")
            or not _identifier(payload["llm_call_id"]) or not isinstance(payload["reason"], str)
            or payload["reason"] not in _REASONS):
        return False
    for field in ("version_id", "launch_version_id"):
        if payload[field] is not None and not _matches(_VERSION, payload[field]):
            return False
    for field in ("base_sha256", "system_sha256"):
        if payload[field] is not None and not _matches(_SHA, payload[field]):
            return False
    model = payload["model"]
    if model is not None and (not isinstance(model, str) or not model or len(model) > 512):
        return False
    return payload["status"] != "bound" or (
        payload["reason"] in _BOUND_REASONS
        and all(payload[field] is not None for field in ("version_id", "base_sha256", "system_sha256"))
    )


def _turn_view(events: Sequence[ExecutionEvent], owner: tuple[str, str]) -> PromptProvenanceView:
    def unknown(reason):
        return PromptProvenanceView(reason=reason)

    provenance = [event for event in events if event.kind == "prompt_provenance"]
    if not provenance:
        return unknown("not_recorded")
    if any((event.agent_slug, event.session_id) != owner for event in events):
        return unknown("invalid")
    calls = [event for event in events if event.kind == "llm_call"]
    call_ids = [event.call_id for event in calls]
    if not calls or any(not _identifier(identity) for identity in call_ids):
        return unknown("incomplete")
    identities = set(call_ids)
    if len(identities) != len(call_ids):
        return unknown("conflict")
    # An orphan result is evidence of a call omitted from the retained trace.
    # A missing result is allowed: prompt attribution is not a success verdict.
    if any(event.kind == "llm_result" and event.call_id not in identities for event in events):
        return unknown("incomplete")
    if any(not _valid_payload(event.payload) for event in provenance):
        return unknown("invalid")
    by_call = {}
    for event in provenance:
        identity = event.payload["llm_call_id"]
        if identity in by_call:
            return unknown("conflict")
        by_call[identity] = event.payload
    if set(by_call) != identities:
        return unknown("incomplete")
    if any(payload["status"] != "bound" for payload in by_call.values()):
        return unknown("unbound")
    for call in calls:
        model = call.payload.get("model")
        if isinstance(model, str) and model != by_call[call.call_id]["model"]:
            return unknown("invalid")
    # Effective system messages can change between calls (e.g. runtime context)
    # while the same recorded base prompt is used. Keep their hashes per event.
    bindings = {(payload["version_id"], payload["base_sha256"]) for payload in by_call.values()}
    if len(bindings) != 1:
        return unknown("conflict")
    version_id, base_sha256 = next(iter(bindings))
    return PromptProvenanceView(status="bound", version_id=version_id, base_sha256=base_sha256, reason="recorded")


def project_prompt_provenance(
    events: Sequence[ExecutionEvent], *, agent_slug: str, session_id: str,
) -> dict[int, PromptProvenanceView]:
    """Aggregate complete per-turn call coverage before response pagination."""
    grouped = defaultdict(list)
    for event in events:
        if event.turn_no is not None and event.kind in {"llm_call", "llm_result", "prompt_provenance"}:
            grouped[event.turn_no].append(event)
    return {number: _turn_view(items, (agent_slug, session_id)) for number, items in grouped.items()}
