"""Create a new agent: slug, port, identity, key copy, rendered agent.py, meta.json."""

from __future__ import annotations

import datetime
import re
import shutil
import string

from . import config, identity, ports, registry, workspaces
from .registry import AgentMeta
from .toolkits import HINTS, prompt_guides, required_trust, validate


DEFAULT_MODEL = "co/gemini-3.5-flash"


def slugify(name: str) -> str:
    """Lowercase, hyphenated, filesystem-safe slug ("Kitchen Sink!" → "kitchen-sink")."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "agent"


def _unique_slug(name: str) -> str:
    """First free slug derived from the name (kitchen-sink, kitchen-sink-2, ...)."""
    base = slugify(name)
    candidate, counter = base, 2
    while registry.agent_dir(candidate).exists():
        candidate = f"{base}-{counter}"
        counter += 1
    return candidate


def name_conflicts(name: str, *, exclude_slug: str | None = None) -> bool:
    """Whether another registered agent already uses the same normalized display name.

    Callers performing a mutation must hold ``registry.locked()`` so the check and
    subsequent write are one atomic operation.
    """
    key = slugify(name)
    return any(
        meta.slug != exclude_slug and slugify(meta.name) == key
        for meta in registry.load_all()
    )


def _system_prompt(name: str, capabilities: list[str]) -> str:
    """Compose a general contract plus operating guidance for selected capabilities."""
    abilities = "; ".join(HINTS[t] for t in capabilities) or "plain conversation"
    guide = prompt_guides(capabilities)
    prompt = f"""You are {name}, a capable general-purpose assistant.

Available capabilities: {abilities}.

## Response style
- Answer directly and naturally. Skip greetings, filler, and repeated conclusions.
- Match the user's level of detail: concise by default, more thorough when the task needs it.
- Use plain language and structure longer answers so they are easy to scan.
- Keep a calm, friendly, professional tone. Do not use emojis unless the user asks.

## Accuracy
- Prioritize correct, useful information over agreeing with the user.
- If information is missing or uncertain, say what is unknown instead of inventing an answer.
- Ask a focused clarification only when it materially changes the result.

## Tool use
- Use an available tool when it provides evidence or completes the request more reliably.
- Do not claim a tool action succeeded unless its result confirms success.
- Explain tool results in user-facing language, without exposing internal implementation details."""
    return f"{prompt}\n\n{guide}" if guide else prompt


TRUST_LEVELS = ("open", "careful", "strict")
PRESETS = ("custom", "co-ai")
_INVITE_CODE_RE = re.compile(r"[A-Za-z0-9_-]{4,64}")


def validate_trust(trust: str) -> str:
    """Ensure the requested trust level is one host() understands."""
    if trust not in TRUST_LEVELS:
        raise ValueError(f"unknown trust level: {trust!r} (expected one of {', '.join(TRUST_LEVELS)})")
    return trust


def validate_preset(preset: str) -> str:
    """Ensure the requested Studio template is supported."""
    if preset not in PRESETS:
        raise ValueError(f"unknown preset: {preset!r} (expected one of {', '.join(PRESETS)})")
    return preset


def validate_invite_code(invite_code: str | None) -> str:
    """Return an explicitly supplied, policy-safe invite code."""
    if invite_code is None or not invite_code.strip():
        raise ValueError("invite code is required for invite-only access")
    code = invite_code.strip()
    if not _INVITE_CODE_RE.fullmatch(code):
        raise ValueError("invite code must be 4-64 letters, numbers, hyphens, or underscores")
    return code


def _invite_trust_policy(invite_code: str, *, subject: str = "agent") -> str:
    """Deny-by-default policy shared by co-ai and sensitive Custom Agents."""
    # Single-quote the code. yaml.safe_load would otherwise coerce a numeric ("9900"),
    # boolean-like ("true"/"no"/"off") or null-like ("null") code to a non-str, so the runtime
    # `request_code in valid_codes` check (str vs int/bool/None) never matches and every such
    # invite is silently rejected at connect time. validate_invite_code() forbids quotes and
    # backslashes, so single-quoting is injection-safe — the regex, not this quoting, is the
    # real boundary. Do NOT drop the quotes.
    return (
        "---\n"
        "allow: [whitelisted, contact]\n"
        "deny: [blocked]\n"
        "onboard:\n"
        f"  invite_code: ['{invite_code}']\n"
        "default: deny\n"
        "---\n"
        f"Only clients admitted with the configured invite code may use this {subject}."
    )


def normalize_custom_policy(
    capabilities: list[str],
    invite_code: str | None,
    *,
    requested_trust: str = "open",
) -> tuple[list[str], str, str | None]:
    """Derive access from capability minimums plus the user's optional invite choice."""
    selection = validate(capabilities)
    requested_trust = validate_trust(requested_trust)
    minimum_trust = required_trust(selection)
    supplied_code = bool(invite_code and invite_code.strip())

    if minimum_trust == "strict":
        trust = "strict"
    elif minimum_trust == "careful":
        trust = "careful"
    else:
        # Standard capabilities may remain open or opt into the least restrictive
        # invite-only policy. A supplied code is itself an explicit invite-only choice.
        trust = "careful" if requested_trust != "open" or supplied_code else "open"

    if trust == "open":
        return selection, trust, None
    return selection, trust, validate_invite_code(invite_code)


def render(
    name: str,
    model: str,
    port: int,
    toolkits: list[str],
    trust: str,
    *,
    preset: str = "custom",
    invite_code: str | None = None,
    work_dir: str | None = None,
) -> str:
    """Render agent.py from the template (the get_ips patch is inlined by the template itself)."""
    preset = validate_preset(preset)
    if preset == "co-ai":
        code = validate_invite_code(invite_code)
        template = string.Template(config.CO_AI_TEMPLATE_PATH.read_text())
        return template.substitute(
            DOC_NAME=" ".join(name.split()).replace("\\", "").replace('"', "'"),
            NAME_LITERAL=repr(name),
            MODEL_LITERAL=repr(model),
            PORT=str(port),
            TRUST_POLICY_LITERAL=repr(_invite_trust_policy(code, subject="coding agent")),
            WORK_DIR_LITERAL=repr(work_dir),
        )

    selection, forced_trust, code = normalize_custom_policy(
        toolkits, invite_code, requested_trust=trust
    )
    trust_policy = _invite_trust_policy(code, subject="agent") if code else forced_trust
    template = string.Template(config.TEMPLATE_PATH.read_text())
    return template.substitute(
        # DOC_NAME lands RAW inside a """...""" docstring, so collapse whitespace and
        # strip backslashes / double-quotes: no `"""` or escape can form to break out.
        DOC_NAME=" ".join(name.split()).replace("\\", "").replace('"', "'"),
        NAME_LITERAL=repr(name),
        MODEL_LITERAL=repr(model),
        PORT=str(port),
        CAPABILITIES_LITERAL=repr(selection),
        SYSTEM_PROMPT_LITERAL=repr(_system_prompt(name, selection)),
        TRUST_LITERAL=repr(trust_policy),
        WORK_DIR_LITERAL=repr(work_dir),
    )


def create(
    name: str,
    model: str,
    toolkits: list[str],
    trust: str = "open",
    *,
    preset: str = "custom",
    invite_code: str | None = None,
    work_dir: str | None = None,
) -> AgentMeta:
    """Create an agent directory with identity + QR-ready address; does NOT start it."""
    preset = validate_preset(preset)
    if preset == "co-ai":
        selection = []
        trust = "strict"
        invite_code = validate_invite_code(invite_code)
    else:
        selection, trust, invite_code = normalize_custom_policy(
            toolkits, invite_code, requested_trust=trust
        )
    with registry.locked():
        slug = _unique_slug(name)
        agent_dir = registry.agent_dir(slug)
        _workspace, stored_work_dir = workspaces.prepare(
            slug,
            work_dir,
            require_write=preset == "co-ai" or any(
                capability in {"file-write", "shell", "browser"} for capability in selection
            ),
        )
        (agent_dir / ".co").mkdir(parents=True, exist_ok=True)
        reserved = {meta.port for meta in registry.load_all()}
        port = ports.allocate(reserved)
        address = identity.create(agent_dir / ".co")
        if config.KEYS_ENV.exists():  # this is how the co/gemini model key flows to the agent
            shutil.copy(config.KEYS_ENV, agent_dir / ".env")
        (agent_dir / "agent.py").write_text(
            render(
                name,
                model,
                port,
                selection,
                trust,
                preset=preset,
                invite_code=invite_code,
                work_dir=stored_work_dir,
            )
        )
        meta = AgentMeta(
            slug=slug,
            name=name,
            address=address,
            port=port,
            model=model,
            capabilities=selection,
            created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            trust=trust,
            preset=preset,
            invite_code=invite_code,
            work_dir=stored_work_dir,
        )
        registry.save(meta)
    return meta


def refresh_generated_workspace_scripts() -> int:
    """Upgrade older generated agents to the workspace-aware templates.

    Only Studio-generated scripts without the new marker are rewritten. A user-owned
    standalone script is left untouched rather than silently overwriting custom code.
    """
    refreshed = 0
    with registry.locked():
        for meta in registry.load_all():
            try:
                workspaces.ensure(meta)
            except ValueError as exc:
                print(
                    f"[co-studio] workspace migration skipped {meta.slug}: {exc}",
                    flush=True,
                )
                continue
            script = registry.agent_dir(meta.slug) / "agent.py"
            try:
                current = script.read_text()
            except OSError:
                continue
            if "WORK_DIR_VALUE =" in current:
                continue
            if "generated by ConnectOnion Studio" not in current:
                print(
                    f"[co-studio] workspace migration skipped custom script for {meta.slug}",
                    flush=True,
                )
                continue
            try:
                script.write_text(
                    render(
                        meta.name,
                        meta.model,
                        meta.port,
                        meta.capabilities,
                        meta.trust,
                        preset=meta.preset,
                        invite_code=meta.invite_code,
                        work_dir=meta.work_dir,
                    )
                )
            except ValueError as exc:
                print(
                    f"[co-studio] workspace script migration skipped {meta.slug}: {exc}",
                    flush=True,
                )
                continue
            refreshed += 1
    return refreshed


def remediate_invite_policies() -> int:
    """Re-render invite-gated agents whose baked-in policy predates the invite-code quoting fix.

    Agents created before the fix carry an unquoted ``invite_code: [<code>]`` in their generated
    ``agent.py``; YAML coerces numeric / boolean-like / null-like codes to non-strings, so the code
    never matches at connect time. ``meta.json`` still holds the authoritative raw string code, so
    re-rendering bakes a quoted, string-typed policy. Idempotent (writes only on a real change),
    Studio-marker guarded (never rewrites an ejected/custom script), and gated on ``invite_code``
    presence so keyword codes are covered too — deliberately unlike
    ``refresh_generated_workspace_scripts`` (whose ``WORK_DIR_VALUE =`` gate skips exactly these
    already-workspace-aware agents). The affected agent must be restarted to reparse its policy.
    """
    remediated = 0
    with registry.locked():
        for meta in registry.load_all():
            if not meta.invite_code:
                continue
            script = registry.agent_dir(meta.slug) / "agent.py"
            try:
                current = script.read_text()
            except OSError:
                continue
            if "generated by ConnectOnion Studio" not in current:
                continue  # user-owned / ejected script — never clobber it
            try:
                new_text = render(
                    meta.name,
                    meta.model,
                    meta.port,
                    meta.capabilities,
                    meta.trust,
                    preset=meta.preset,
                    invite_code=meta.invite_code,
                    work_dir=meta.work_dir,
                )
            except ValueError as exc:
                print(f"[co-studio] invite policy remediation skipped {meta.slug}: {exc}", flush=True)
                continue
            if new_text != current:
                script.write_text(new_text)
                remediated += 1
    return remediated
