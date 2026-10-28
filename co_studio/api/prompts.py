"""Local Prompt configuration only. Saving never starts or restarts an Agent."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ..prompt_versions import (PromptVersions, PromptError, PromptConflict,
                               PromptInvalid, PromptNotFound, PromptUnsupported)
from .sessions import Slug, _local_history_request, _registered_agent

router = APIRouter(prefix="/agents/{slug}/prompts", tags=["prompts"],
                   dependencies=[Depends(_local_history_request)])


class SavePrompt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    base_version: str = Field(min_length=1, max_length=32, pattern=r"^(baseline|[a-f0-9]{32})$")
    text: str = Field(min_length=1, max_length=65536)


class RestorePrompt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    base_version: str = Field(min_length=1, max_length=32, pattern=r"^(baseline|[a-f0-9]{32})$")
    version_id: str = Field(min_length=1, max_length=32, pattern=r"^(baseline|[a-f0-9]{32})$")


def _call(slug, action):
    directory = _registered_agent(slug)
    try:
        return action(PromptVersions(directory))
    except PromptConflict as exc:
        raise HTTPException(409, str(exc)) from None
    except (PromptInvalid, PromptUnsupported) as exc:
        raise HTTPException(422, str(exc)) from None
    except PromptNotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except (PromptError, OSError):
        raise HTTPException(503, "Prompt versions are unavailable; check local storage and retry") from None


@router.get("")
def list_prompts(slug: Slug):
    return _call(slug, lambda store: store.state())


@router.post("")
def save_prompt(slug: Slug, body: SavePrompt):
    return _call(slug, lambda store: store.save(body.base_version, body.text))


@router.post("/restore")
def restore_prompt(slug: Slug, body: RestorePrompt):
    return _call(slug, lambda store: store.save(body.base_version, restore=body.version_id))


@router.get("/{version_id}")
def get_prompt(slug: Slug, version_id: str):
    return _call(slug, lambda store: store.get(version_id))
