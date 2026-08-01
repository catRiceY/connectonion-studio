"""The bundled skills catalog + per-agent install for the co-ai skills store.

A co-ai agent auto-discovers skills from ``<agent_dir>/.co/skills/<name>/SKILL.md`` — ConnectOnion's
skills plugin keys discovery off the agent's ``co_dir`` (``co_base/skills``). Installing a skill is
therefore just copying a bundled catalog folder there; the plugin loads skills at agent start
(``on_agent_ready``), so a running agent must be **restarted** to pick up a change.

The catalog ships inside the package at ``co_studio/skills/<name>/SKILL.md`` (see ``config.SKILLS_DIR``).
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml  # always present: connectonion (a hard dependency) requires PyYAML

from . import config, registry

# Skill names are directory names on both sides; keep them to a safe, traversal-proof charset.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class CatalogSkill:
    name: str
    title: str
    description: str
    category: str
    tools: list[str]


def _frontmatter(text: str) -> dict:
    """Parse the leading ``---``…``---`` YAML frontmatter block (empty dict if absent/invalid)."""
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not match:
        return {}
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _title_from(name: str) -> str:
    return name.replace("-", " ").replace("_", " ").strip().title()


def _read_skill(skill_dir: Path) -> CatalogSkill | None:
    md = skill_dir / "SKILL.md"
    if not md.is_file():
        return None
    try:
        fm = _frontmatter(md.read_text(encoding="utf-8"))
    except OSError:
        return None
    name = str(fm.get("name") or skill_dir.name)
    tools = fm.get("tools")
    return CatalogSkill(
        name=name,
        title=str(fm.get("title") or _title_from(name)),
        description=str(fm.get("description") or ""),
        category=str(fm.get("category") or "general"),
        tools=[str(t) for t in tools] if isinstance(tools, list) else [],
    )


def catalog() -> list[CatalogSkill]:
    """Every bundled skill, sorted by category then name."""
    if not config.SKILLS_DIR.is_dir():
        return []
    found = (_read_skill(d) for d in sorted(config.SKILLS_DIR.iterdir()) if d.is_dir())
    return sorted((s for s in found if s), key=lambda s: (s.category, s.name))


def is_catalog_skill(name: str) -> bool:
    """Whether ``name`` is a real, safe catalog skill (guards against path traversal)."""
    return bool(_NAME_RE.match(name or "")) and (config.SKILLS_DIR / name / "SKILL.md").is_file()


def _agent_skills_dir(slug: str) -> Path:
    return registry.agent_dir(slug) / ".co" / "skills"


def installed_names(slug: str) -> list[str]:
    """Skill names currently installed for one agent (subdirs holding a SKILL.md)."""
    root = _agent_skills_dir(slug)
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir() if d.is_dir() and (d / "SKILL.md").is_file())


def install(slug: str, name: str) -> None:
    """Copy a catalog skill into the agent's ``.co/skills/``. Idempotent (replaces any existing copy)."""
    if not is_catalog_skill(name):
        raise ValueError(f"unknown skill: {name!r}")
    dest = _agent_skills_dir(slug) / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(config.SKILLS_DIR / name, dest)


def uninstall(slug: str, name: str) -> None:
    """Remove one installed skill from the agent's ``.co/skills/`` (name is validated, so no traversal)."""
    if not _NAME_RE.match(name or ""):
        raise ValueError(f"invalid skill name: {name!r}")
    shutil.rmtree(_agent_skills_dir(slug) / name, ignore_errors=True)
