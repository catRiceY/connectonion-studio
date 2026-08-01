"""Skills store: the bundled catalog + per-agent install/uninstall for co-ai agents."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import registry, skills_catalog
from ..registry import AgentMeta
from ..supervisor import SUPERVISOR

router = APIRouter(prefix="/skills", tags=["skills"])


def _catalog_payload() -> list[dict[str, object]]:
    return [
        {
            "name": s.name,
            "title": s.title,
            "description": s.description,
            "category": s.category,
            "tools": s.tools,
        }
        for s in skills_catalog.catalog()
    ]


def _get_meta(slug: str) -> AgentMeta:
    meta = registry.load(slug)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"no agent named '{slug}'")
    return meta


def _agent_state(slug: str, meta: AgentMeta) -> dict[str, object]:
    """Everything the store UI needs for one agent in a single response."""
    return {
        "slug": slug,
        "is_co_ai": meta.preset == "co-ai",
        "installed": skills_catalog.installed_names(slug),
        "running": SUPERVISOR.state_of(slug) in ("online", "starting"),
        "catalog": _catalog_payload(),
    }


@router.get("/catalog")
def get_catalog() -> dict[str, object]:
    """GET /api/skills/catalog — the full bundled catalog (agent-independent)."""
    return {"skills": _catalog_payload()}


def _coai_agents() -> list[dict[str, object]]:
    """Every co-ai agent with its installed-skill names (skills apply to co-ai only)."""
    return [
        {
            "slug": m.slug,
            "name": m.name,
            "running": SUPERVISOR.state_of(m.slug) in ("online", "starting"),
            "installed": skills_catalog.installed_names(m.slug),
        }
        for m in registry.load_all()
        if m.preset == "co-ai"
    ]


@router.get("/market")
def market() -> dict[str, object]:
    """GET /api/skills/market — one payload for the sidebar market: catalog + co-ai agents + who has what."""
    return {"catalog": _catalog_payload(), "agents": _coai_agents()}


class BulkInstallBody(BaseModel):
    """POST body for installing one skill onto several agents at once."""

    name: str = Field(min_length=1, max_length=64)
    slugs: list[str] = Field(min_length=1)


@router.post("/install-bulk")
def install_bulk(body: BulkInstallBody) -> dict[str, object]:
    """Install one catalog skill onto several co-ai agents. Restart each to apply."""
    if not skills_catalog.is_catalog_skill(body.name):
        raise HTTPException(status_code=404, detail=f"unknown skill: {body.name!r}")
    installed, skipped = [], []
    for slug in body.slugs:
        meta = registry.load(slug)
        if meta is None or meta.preset != "co-ai":
            skipped.append(slug)
            continue
        skills_catalog.install(slug, body.name)
        installed.append(slug)
    return {"name": body.name, "installed": installed, "skipped": skipped, "agents": _coai_agents()}


@router.get("/agent/{slug}")
def agent_skills(slug: str) -> dict[str, object]:
    """GET /api/skills/agent/{slug} — catalog + which skills this agent has installed."""
    return _agent_state(slug, _get_meta(slug))


class SkillBody(BaseModel):
    """POST body for install/uninstall."""

    name: str = Field(min_length=1, max_length=64)


@router.post("/agent/{slug}/install")
def install_skill(slug: str, body: SkillBody) -> dict[str, object]:
    """Install a catalog skill into the agent's .co/skills/. Restart the agent to load it."""
    meta = _get_meta(slug)
    if meta.preset != "co-ai":
        raise HTTPException(status_code=400, detail="skills apply to co-ai agents only")
    try:
        skills_catalog.install(slug, body.name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _agent_state(slug, meta)


@router.post("/agent/{slug}/uninstall")
def uninstall_skill(slug: str, body: SkillBody) -> dict[str, object]:
    """Remove an installed skill from the agent's .co/skills/. Restart the agent to apply."""
    meta = _get_meta(slug)
    try:
        skills_catalog.uninstall(slug, body.name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _agent_state(slug, meta)
