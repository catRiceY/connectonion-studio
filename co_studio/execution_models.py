"""Studio's v1 read models for conversations and execution history.

These are data contracts only: no framework imports, file reads, persistence,
or execution. They do not replace AgentMeta, ProcRecord, or ConnectOnion's
session storage. execution_adapter supplies identities and known facts;
missing timestamps, durations, and outcomes must not be invented.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, JsonValue


Identifier = Annotated[str, Field(min_length=1, pattern=r"^\S+$")]


class _ExecutionModel(BaseModel):
    # Catch misspelled fields and accidental coercion (e.g. True -> turn 1).
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    schema_version: int = Field(default=1, ge=1, le=1)


class SessionRecord(_ExecutionModel):
    """One conversation, identified by (agent_slug, session_id).

    source_status is the Host's raw state, not a business-success verdict.
    created_at/updated_at are Unix seconds; source_revision is not a turn count.
    """

    agent_slug: Identifier
    session_id: Identifier
    source_status: str | None = None
    source_revision: int | None = Field(default=None, ge=0)
    created_at: float | None = Field(default=None, ge=0)
    updated_at: float | None = Field(default=None, ge=0)


class TurnRecord(_ExecutionModel):
    """One user-input round, identified by (agent_slug, session_id, turn_no).

    finish_reason is the observed terminal reason (e.g. natural, max_iterations),
    not a success flag. None means unknown, not necessarily still running.
    Historical prompt/result may be unavailable; missing text stays None.
    """

    agent_slug: Identifier
    session_id: Identifier
    turn_no: int = Field(ge=1)
    prompt: str | None = None
    result: str | None = None
    finish_reason: str | None = None
    duration_ms: float | None = Field(default=None, ge=0)


class ExecutionEvent(_ExecutionModel):
    """One observed event, with an explicit Studio identity and source order.

    event_id must be stable and unique within (agent_slug, session_id).
    sequence is zero-based within the session trace, NOT a JSONL line number.
    source_id preserves trace.id, which can be shared by LLM call/result.
    call_id pairs starts/results within the same session and call family
    (LLM vs tool); it is not an event identity or a global identifier.

    turn_no can be unknown when the source gives no reliable turn boundary.
    occurred_at uses Unix seconds; duration_ms uses milliseconds.
    payload carries JSON details, not an unrestricted raw-session dump. The
    adapter selects fields; an API must still enforce access and privacy policy.
    """

    agent_slug: Identifier
    session_id: Identifier
    event_id: Identifier
    sequence: int = Field(ge=0)
    kind: Identifier
    turn_no: int | None = Field(default=None, ge=1)
    source_id: Identifier | None = None
    call_id: Identifier | None = None
    occurred_at: float | None = Field(default=None, ge=0)
    duration_ms: float | None = Field(default=None, ge=0)
    payload: dict[str, JsonValue] = Field(default_factory=dict)
