"""Local owner configuration and one-call approval decisions."""
from typing import Literal
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from ..approvals import (ApprovalStore, ToolsConflict, ToolsError, ToolsInvalid, ToolsNotFound,
                         config_view, save_tool_config)
from .sessions import Slug, _local_history_request, _registered_agent

router = APIRouter(prefix="/agents/{slug}", tags=["tool-approvals"],
                   dependencies=[Depends(_local_history_request)])


class ConfigureTools(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    expected_revision: int = Field(ge=0)
    mcp_enabled: bool
    approvals_enabled: bool
    approval_timeout_seconds: int = Field(ge=30, le=300)
    context_budget: int | None = Field(default=None, ge=128, le=2_000_000)


class DecideApproval(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    decision: Literal["approve", "reject"]
    binding: str = Field(pattern=r"^[a-f0-9]{64}$")


def _call(slug, operation):
    directory = _registered_agent(slug)
    try:
        return operation(directory)
    except ToolsConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except ToolsInvalid as exc:
        raise HTTPException(422, str(exc)) from None
    except ToolsNotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except (ToolsError, OSError, sqlite3.Error):
        raise HTTPException(503, "Tool configuration or approvals are unavailable") from None


@router.get("/tools/config")
def get_tool_config(slug: Slug):
    return _call(slug, config_view)


@router.put("/tools/config")
def put_tool_config(slug: Slug, body: ConfigureTools):
    return _call(slug, lambda directory: save_tool_config(directory, **body.model_dump(exclude_unset=True)))


@router.get("/approvals")
def list_approvals(slug: Slug, status: Literal["pending", "all"] = "pending",
                   limit: int = Query(default=50, ge=1, le=100)):
    return _call(slug, lambda directory: ApprovalStore(directory).list(status=status, limit=limit))


@router.get("/approvals/{approval_id}")
def get_approval(slug: Slug, approval_id: str):
    return _call(slug, lambda directory: ApprovalStore(directory).get(approval_id))


@router.post("/approvals/{approval_id}/decision")
def decide_approval(slug: Slug, approval_id: str, body: DecideApproval):
    return _call(slug, lambda directory: ApprovalStore(directory).decide(approval_id, **body.model_dump()))
