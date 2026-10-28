"""Studio-owned observations of hosted custom Agent model inputs.

This is deliberately not a model-request audit log: the hash covers the ordered
system/developer messages passed to the provider's ``complete`` interface, not
the provider-specific HTTP body. No prompt text or signing key enters a trace.
Only a new, unmodified system seed or a verified prior binding is attributed to
a revision. A matching string alone is never used to identify an old revision.
"""
from __future__ import annotations

from copy import deepcopy
from contextvars import ContextVar
import functools
import hashlib
import hmac
import inspect
import json
from pathlib import Path
import re
import secrets
import threading
import time
import uuid

from filelock import FileLock

from .prompt_versions import PromptError, PromptUnsupported, _new_file, _read, _safe

_BINDING = "_studio_prompt_binding"
_SIGNATURE = "_studio_signature"
_KEY_FILE = "provenance.key"
_HEX = re.compile(r"^[a-f0-9]{64}$")
_VERSION = re.compile(r"^[a-f0-9]{32}$")
_CALL_CONTEXT = ContextVar("studio_prompt_call", default=None)


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def system_sha256(messages) -> str | None:
    """Hash the canonical ordered system/developer projection, excluding CO ids."""
    if not isinstance(messages, list):
        return None
    selected = []
    for message in messages:
        if not isinstance(message, dict):
            return None
        if message.get("role") in ("system", "developer"):
            selected.append({key: value for key, value in message.items() if key != "id"})
    try:
        return hashlib.sha256(_canonical(selected)).hexdigest()
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None


def _key(agent_dir: Path, *, create: bool = False) -> bytes:
    """Read a local signing key; only runtime setup may create one, under lock."""
    agent_dir = _safe(Path(agent_dir).absolute())
    root = _safe(agent_dir / ".studio-prompts")
    path = _safe(root / _KEY_FILE)
    if create:
        if not root.is_dir():
            raise PromptError("Prompt provenance storage is unavailable")
        try:
            with FileLock(str(_safe(agent_dir / ".studio-prompts.lock")), timeout=10):
                if not path.exists():
                    _new_file(path, secrets.token_hex(32))
        except (OSError, TimeoutError):
            raise PromptError("Prompt provenance storage is unavailable") from None
    text = _read(path, 128)
    try:
        if not _HEX.fullmatch(text) or path.stat().st_mode & 0o077:
            raise PromptError("Prompt provenance key failed validation")
    except OSError:
        raise PromptError("Prompt provenance storage is unavailable") from None
    return bytes.fromhex(text)


def _sign(key: bytes, domain: str, payload: dict) -> str:
    return hmac.new(key, _canonical({"domain": domain, "payload": payload}), hashlib.sha256).hexdigest()


def _verify(key: bytes, domain: str, value: dict) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get(_SIGNATURE), str):
        return False
    try:
        expected = _sign(key, domain, {k: v for k, v in value.items() if k != _SIGNATURE})
        return hmac.compare_digest(value[_SIGNATURE], expected)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return False


def verify_event(entry: dict, *, agent_slug: str, session_id: str, agent_dir: Path) -> bool:
    """Read-only authentication for the adapter; missing/corrupt proof is unknown.

    The adapter must also check that turn and llm_call_id refer to the surrounding
    actual call. Signing prevents a client from fabricating or moving observations
    across Agents/sessions; it does not turn client-supplied trace ordering into
    an authoritative execution sequence.
    """
    return event_verifier(agent_dir)(entry, agent_slug, session_id)


def event_verifier(agent_dir: Path):
    """Load one read-only verification key for a single snapshot-read lifecycle."""
    try:
        key = _key(agent_dir)
    except (PromptError, OSError, TypeError, ValueError):
        key = None

    def verify(entry: dict, agent_slug: str, session_id: str) -> bool:
        if (key is None or not isinstance(entry, dict)
                or entry.get("type") != "prompt_provenance"
                or entry.get("provenance_version") != 1 or Path(agent_dir).name != agent_slug):
            return False
        try:
            signature = entry.get(_SIGNATURE)
            payload = {"agent_slug": agent_slug, "session_id": session_id,
                       "event": {k: v for k, v in entry.items() if k != _SIGNATURE}}
            return isinstance(signature, str) and hmac.compare_digest(
                signature, _sign(key, "studio-prompt-event-v1", payload))
        except (TypeError, ValueError, UnicodeError, RecursionError):
            return False

    return verify


class _ProviderView:
    """Do not mutate a provider that another Agent/factory may be sharing."""
    def __init__(self, provider, observer):
        self._provider = provider
        self._observer = observer

    def __getattr__(self, name):
        return getattr(self._provider, name)

    def __setattr__(self, name, value):
        if name in {"_provider", "_observer"}:
            object.__setattr__(self, name, value)
        else:
            setattr(self._provider, name, value)

    def __delattr__(self, name):
        if name in {"_provider", "_observer"}:
            raise AttributeError("Provider observation internals cannot be removed")
        delattr(self._provider, name)

    def complete(self, messages, *args, **kwargs):
        self._observer.observe(messages)
        return self._provider.complete(messages, *args, **kwargs)


class _Observer:
    def __init__(self, agent, agent_dir: Path, revision: dict, key: bytes):
        self.agent = agent
        self.agent_slug = agent_dir.name
        self.revision = {name: revision[name] for name in ("id", "text", "sha256")}
        self.key = key
        self.lock = threading.RLock()
        self.active = False
        self.pending = None
        self.binding = None
        self.reason = "unverified_session"
        self.session_id = None
        self.record = type(agent)._record_trace
        self.input_signature = inspect.signature(agent.input)

    def _valid_binding(self, candidate, session_id, system_hash):
        if not isinstance(candidate, dict):
            return False
        return (candidate.get("provenance_version") == 1
                and candidate.get("agent_slug") == self.agent_slug
                and candidate.get("session_id") == session_id
                and isinstance(candidate.get("version_id"), str)
                and bool(_VERSION.fullmatch(candidate["version_id"]))
                and isinstance(candidate.get("base_sha256"), str)
                and bool(_HEX.fullmatch(candidate["base_sha256"]))
                and candidate.get("system_sha256") == system_hash
                and _verify(self.key, "studio-prompt-binding-v1", candidate))

    def run_input(self, original, *args, **kwargs):
        bound = self.input_signature.bind(*args, **kwargs)
        source = bound.arguments.get("session")
        if source is None:
            source = self.agent.current_session
        with self.lock:
            if self.active:
                raise PromptError("Concurrent input on one Agent is not supported")
            self.active = True
            self.pending = None
            self.binding = None
            self.reason = "unverified_session"
            self.session_id = source.get("session_id") if isinstance(source, dict) else None
            messages = source.get("messages", []) if isinstance(source, dict) else []
            seeded = isinstance(messages, list) and (not messages or isinstance(messages[0], dict)
                                                     and messages[0].get("role") != "system")
            prospective = ([{"role": "system", "content": self.agent.system_prompt}] + messages
                           if seeded else messages)
            actual_hash = system_sha256(prospective)
            expected_hash = system_sha256([{"role": "system", "content": self.revision["text"]}])
            if not isinstance(self.session_id, str) or not self.session_id:
                self.reason = "missing_session"
            elif seeded and actual_hash == expected_hash:
                self.binding = {
                    "provenance_version": 1, "agent_slug": self.agent_slug,
                    "session_id": self.session_id, "version_id": self.revision["id"],
                    "base_sha256": self.revision["sha256"], "system_sha256": actual_hash,
                }
                self.binding[_SIGNATURE] = _sign(self.key, "studio-prompt-binding-v1", self.binding)
                self.reason = "new_session"
            elif not seeded and isinstance(source, dict) and self._valid_binding(
                    source.get(_BINDING), self.session_id, actual_hash):
                self.binding = deepcopy(source[_BINDING])
                self.reason = "restored_session"
            elif seeded or isinstance(source, dict) and source.get(_BINDING) is not None:
                self.reason = "system_modified" if seeded else "unverified_session"

            # Never mutate the caller's restoration object. CO copies arbitrary
            # session keys, so an unauthenticated binding is explicitly removed.
            if isinstance(source, dict):
                session = dict(source)
                session.pop(_BINDING, None)
                if self.binding is not None:
                    session[_BINDING] = deepcopy(self.binding)
                bound.arguments["session"] = session
        context_token = _CALL_CONTEXT.set(None)
        try:
            return original(*bound.args, **bound.kwargs)
        finally:
            with self.lock:
                self.active = False
                self.pending = None
            _CALL_CONTEXT.reset(context_token)

    def trace(self, original, entry, *, wire_extras=None):
        with self.lock:
            kind = entry.get("type")
            # A timed-out/abandoned provider worker must never append an event
            # after its terminal result or into a subsequent turn.
            if kind == "turn_result" or (kind == "llm_result" and self.pending is not None
                                         and entry.get("id") == self.pending["id"]):
                self.pending = None
            result = original(entry, wire_extras=wire_extras)
            if kind == "llm_call" and self.active:
                current = self.agent.current_session
                self.pending = {"id": entry.get("id"), "model": entry.get("model"),
                                "session": current, "turn": current.get("turn"), "observed": False}
                # run_interruptible copies ContextVars into its worker. The
                # identity prevents an abandoned old worker from observing a
                # newer pending call, even if a reusable Agent advances turns.
                _CALL_CONTEXT.set(self.pending)
            return result

    def observe(self, messages):
        with self.lock:
            pending = self.pending
            current = self.agent.current_session
            if (not self.active or pending is None or pending["observed"]
                    or _CALL_CONTEXT.get() is not pending
                    or current is not pending["session"] or current.get("turn") != pending["turn"]
                    or current.get("session_id") != self.session_id):
                return
            pending["observed"] = True
            system_hash = system_sha256(messages)
            matched = self.binding is not None and system_hash == self.binding["system_sha256"]
            reason = self.reason if self.binding is None or matched else "system_modified"
            event = {
                "type": "prompt_provenance", "id": uuid.uuid4().hex, "ts": time.time(),
                "turn": pending["turn"], "provenance_version": 1,
                "status": "bound" if matched else "unknown",
                "version_id": self.binding["version_id"] if matched else None,
                "base_sha256": self.binding["base_sha256"] if matched else None,
                "system_sha256": system_hash, "launch_version_id": self.revision["id"],
                "llm_call_id": pending["id"], "model": pending["model"], "reason": reason,
            }
            event[_SIGNATURE] = _sign(self.key, "studio-prompt-event-v1", {
                "agent_slug": self.agent_slug, "session_id": self.session_id, "event": event})
            self.record(self.agent, event)


def attach_provenance(agent, *, agent_dir: Path, revision: dict):
    """Instrument one version-enabled generated Agent, not the upstream class.

    CO shallow-copies an Agent for cancellable tool execution. Instance-bound
    closures would make those copies write to the original session. An isolated
    subclass keeps descriptor binding correct; copied instances use the original
    methods and never participate in the root Agent's observation lifecycle.
    """
    if (not callable(getattr(agent, "input", None))
            or not callable(getattr(agent, "_record_trace", None))
            or not callable(getattr(getattr(agent, "llm", None), "complete", None))):
        raise PromptUnsupported("Selected Prompt runtime does not support provenance")
    observer = _Observer(agent, Path(agent_dir), revision, _key(agent_dir, create=True))
    base = type(agent)

    class StudioPromptAgent(base):
        @functools.wraps(base.input)
        def input(self, *args, **kwargs):
            original = super().input
            return observer.run_input(original, *args, **kwargs) if self is agent else original(*args, **kwargs)

        @functools.wraps(base._record_trace)
        def _record_trace(self, entry, *, wire_extras=None):
            original = super()._record_trace
            if self is not agent:
                return original(entry, wire_extras=wire_extras)
            return observer.trace(original, entry, wire_extras=wire_extras)

    agent.__class__ = StudioPromptAgent
    agent.llm = _ProviderView(agent.llm, observer)
    return agent
