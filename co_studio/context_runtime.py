"""Opt-in CO 1.9.2 provider-only context policy; never rewrite session History."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
import functools
import inspect
import os
from pathlib import Path
import threading
import time
import uuid

from .context_policy import (ContextBudgetExceeded, ContextError, ContextPolicy, ContextShapeError,
                             ESTIMATOR, POLICY_VERSION, build_context, estimate_tokens)
from .context_report import COUNTS, SIGNATURE, sign_event, signing_key

_SCOPE = ContextVar("studio_context_scope", default=None)


@dataclass
class _CallScope:
    owner: object
    agent: object
    agent_slug: str
    session: dict
    key: bytes
    session_id: str = field(init=False)
    turn: int = field(init=False)
    prompt: object = field(init=False)
    llm_call_id: str | None = None
    active: bool = True
    reported: bool = False
    lock: object = field(default_factory=threading.RLock)

    def __post_init__(self):
        self.session_id = self.session.get("session_id")
        self.turn = self.session.get("turn")
        self.prompt = self.session.get("user_prompt")

    def report(self, payload):
        with self.lock:
            current = self.agent.current_session
            if (not self.active or self.reported or self.llm_call_id is None
                    or current is not self.session or current.get("session_id") != self.session_id
                    or current.get("turn") != self.turn):
                raise ContextShapeError("Context preparation belongs to an inactive model call")
            event = {"type": "context_report", "id": uuid.uuid4().hex, "ts": time.time(),
                     "turn": self.turn, "report_version": 1, "llm_call_id": self.llm_call_id,
                     "policy_version": POLICY_VERSION, "estimator": ESTIMATOR, **payload}
            event[SIGNATURE] = sign_event(self.key, event, self.agent_slug, self.session_id)
            self.reported = True
            self.agent._record_trace(event)


def policy_from_env(environ=None) -> ContextPolicy | None:
    """No variable means disabled. Invalid opt-in fails launch, not silently off.

    The limit is an estimated input budget, including tool schemas; callers must
    reserve output capacity themselves when choosing it for a specific model.
    """
    value = (os.environ if environ is None else environ).get("CO_STUDIO_CONTEXT_BUDGET", "")
    if not value:
        return None
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        raise ContextError("CO_STUDIO_CONTEXT_BUDGET must be an integer from 128 to 2000000")
    try:
        return ContextPolicy(input_budget=int(value))
    except ValueError:
        raise ContextError("CO_STUDIO_CONTEXT_BUDGET must be an integer from 128 to 2000000") from None


class _ProviderView:
    def __init__(self, provider, policy, owner):
        object.__setattr__(self, "_provider", provider)
        object.__setattr__(self, "_policy", policy)
        object.__setattr__(self, "_owner", owner)

    def __getattr__(self, name):
        return getattr(self._provider, name)

    def __setattr__(self, name, value):
        if name in {"_provider", "_policy", "_owner"}:
            raise AttributeError("Context provider internals are immutable")
        setattr(self._provider, name, value)

    def __delattr__(self, name):
        if name in {"_provider", "_policy", "_owner"}:
            raise AttributeError("Context provider internals are immutable")
        delattr(self._provider, name)

    def complete(self, messages, *args, **kwargs):
        scope = _SCOPE.get()
        if scope is None or scope.owner is not self._owner:
            raise ContextShapeError("Context provider requires an active scoped Agent call")
        # CO 1.9.2 passes tools by keyword. Support its provider's first optional
        # positional argument too, so schemas can never evade the estimate.
        tools = kwargs.get("tools", args[0] if args else None)
        # Plugins can append user-role reminders after the actual task input.
        # Pin that task (when CO exposes its exact text), not just the reminder.
        prompt = scope.prompt
        starts = []
        if isinstance(prompt, str):
            for index, message in enumerate(messages):
                if not isinstance(message, dict) or message.get("role") != "user":
                    continue
                content = message.get("content")
                first_text = (content[0].get("text") if isinstance(content, list) and content
                              and isinstance(content[0], dict) else content)
                if first_text == prompt:
                    starts.append(index)
        try:
            view = build_context(messages, self._policy, agent_slug=scope.agent_slug, session_id=scope.session_id, tools=tools,
                                 preserve_from=starts[-1] if starts else None)
        except ContextError as error:
            full_estimate = None
            if isinstance(error, ContextBudgetExceeded):
                full_estimate = estimate_tokens(messages, tools)
            scope.report({**{name: None for name in COUNTS}, "input_budget": self._policy.input_budget,
                          "full_estimated_tokens": full_estimate,
                          "source_messages": len(messages) if isinstance(messages, list) else None,
                          "status": "budget_exceeded" if isinstance(error, ContextBudgetExceeded) else "invalid_context",
                          "compacted": None, "summary_kind": None})
            raise
        report = view.report
        scope.report({**{name: report[name] for name in COUNTS if name not in {"retained_messages", "input_messages"}},
                      "retained_messages": len(report["retained_message_indices"]),
                      "input_messages": len(view.messages), "status": "prepared",
                      "compacted": report["compacted"], "summary_kind": report["summary_kind"]})
        return self._provider.complete(view.messages, *args, **kwargs)


def attach_context_policy(agent, *, agent_slug: str, policy: ContextPolicy | None, agent_dir: Path | None = None,
                          host_failure_snapshot: bool = False):
    """Attach once, to a factory-created Agent, before any input calls.

    Does not patch upstream globals/shared providers or use a cross-session
    cache. CO's cancellable workers copy ContextVars. A call-local lock and
    lifetime guard prevent late reports after cancellation or terminal events.
    """
    if policy is None:
        return agent
    if (not isinstance(policy, ContextPolicy) or not isinstance(agent_slug, str) or not agent_slug
            or type(host_failure_snapshot) is not bool):
        raise ContextError("Invalid Studio context policy configuration")
    if getattr(agent, "_studio_context_attached", False):
        raise ContextError("Studio context policy is already attached")
    if (not callable(getattr(agent, "_get_llm_decision", None))
            or not callable(getattr(agent, "_record_trace", None))
            or not callable(getattr(getattr(agent, "llm", None), "complete", None))
            or not isinstance(getattr(agent, "events", None), dict)):
        raise ContextError("This Agent does not support the Studio context policy")
    from connectonion.useful_plugins.auto_compact import check_and_compact

    owner, base = object(), type(agent)
    if agent_dir is None:
        co_dir = Path(agent.co_dir).absolute()
        agent_dir = co_dir.parent if co_dir.name == ".co" else co_dir
    key = signing_key(agent_dir, create=True)
    input_signature = inspect.signature(base.input)

    class StudioContextAgent(base):
        @functools.wraps(base.input)
        def input(self, *args, **kwargs):
            claim = None
            if host_failure_snapshot:
                from .context_failure import capture_host_claim

                bound = input_signature.bind(self, *args, **kwargs)
                claim = capture_host_claim(self, bound.arguments.get("session"),
                                           bound.arguments.get("prompt"), agent_dir=agent_dir)
            try:
                return super().input(*args, **kwargs)
            except Exception:
                if claim is not None:
                    claim.recover(self.current_session)
                raise

        @functools.wraps(base._get_llm_decision)
        def _get_llm_decision(self, *args, **kwargs):
            session = self.current_session
            session_id = session.get("session_id") if isinstance(session, dict) else None
            if not isinstance(session_id, str) or not session_id:
                raise ContextShapeError("Studio context policy requires a session identifier")
            scope = _CallScope(owner, self, agent_slug, session, key)
            token = _SCOPE.set(scope)
            try:
                return super()._get_llm_decision(*args, **kwargs)
            finally:
                with scope.lock:
                    scope.active = False
                _SCOPE.reset(token)

        @functools.wraps(base._record_trace)
        def _record_trace(self, entry, *, wire_extras=None):
            scope = _SCOPE.get()
            if scope is None or scope.owner is not owner or scope.agent is not self:
                return super()._record_trace(entry, wire_extras=wire_extras)
            with scope.lock:
                kind = entry.get("type")
                if kind == "turn_result" or (kind == "llm_result" and entry.get("id") == scope.llm_call_id):
                    scope.active = False
                result = super()._record_trace(entry, wire_extras=wire_extras)
                if kind == "llm_call" and scope.active:
                    scope.llm_call_id = entry.get("id")
                return result

    # Copy registries too: a shallow-copied Agent must not modify another
    # instance's registration, and only the known history-mutating plugin goes.
    events = {name: list(handlers) for name, handlers in agent.events.items()}
    events["after_llm"] = [handler for handler in events.get("after_llm", []) if handler is not check_and_compact]
    agent.__class__ = StudioContextAgent
    agent.events = events
    agent.llm = _ProviderView(agent.llm, policy, owner)
    agent._studio_context_attached = True
    return agent


def wrap_host_for_context(host_fn, script: Path, *, policy: ContextPolicy | None = None):
    """Runner helper for generated custom factories; disabled is a true no-op.

    Install this wrapper BEFORE wrap_host_for_prompt: Prompt then validates the
    original generated function and this inner wrapper unwraps its decorator.
    Wrapping alone does not access user metadata, configuration or model keys.
    On factory invocation, attachment creates the private report key; subsequent
    scoped model calls append content-free context reports to the normal trace.
    """
    if policy is None:
        return host_fn
    from .prompt_versions import _baseline

    script = Path(script).absolute()
    _baseline(script)  # Fail closed for co-ai/arbitrary scripts when opted in.

    @functools.wraps(host_fn)
    def wrapped(factory, *args, **kwargs):
        original = inspect.unwrap(factory)
        co_dir = kwargs.get("co_dir")
        if (not inspect.isfunction(original) or original.__name__ != "create_agent"
                or Path(original.__code__.co_filename).absolute() != script
                or co_dir is None or Path(co_dir).absolute() != script.parent / ".co"):
            raise ContextError("Studio context policy requires a generated custom Agent factory")

        @functools.wraps(factory)
        def context_factory():
            return attach_context_policy(factory(), agent_slug=script.parent.name, policy=policy,
                                         agent_dir=script.parent, host_failure_snapshot=True)

        return host_fn(context_factory, *args, **kwargs)

    return wrapped
