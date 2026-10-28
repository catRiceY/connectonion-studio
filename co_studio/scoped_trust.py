"""Bind Studio agents to ConnectOnion's native per-agent trust storage.

ConnectOnion 1.9.2 accepts ``co_dir`` in ``TrustAgent`` and applies it to both
policy evaluation and trust-state operations. Studio keeps its runner adapter
and public class name, but no longer reimplements the framework's security
policy, onboarding checks, or file storage.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any, Callable

from connectonion.network.trust.trust_agent import TrustAgent


class ScopedTrustAgent(TrustAgent):
    """Compatibility name for a native ``TrustAgent`` bound to one agent."""

    def __init__(self, trust: str, *, co_dir: Path) -> None:
        # Keep the public attribute used by Studio callers; the framework binds
        # its own storage path at construction and owns every trust operation.
        self.co_dir = Path(co_dir).resolve()
        super().__init__(trust, co_dir=self.co_dir)


def scope_host(host_fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap ConnectOnion's ``host`` so string policies use agent-scoped state."""
    if getattr(host_fn, "_co_studio_scoped_trust", False):
        return host_fn

    @functools.wraps(host_fn)
    def wrapped(agent: Any, *args: Any, **kwargs: Any) -> Any:
        co_dir = kwargs.get("co_dir")
        trust = kwargs.get("trust", "careful")
        if co_dir is not None and isinstance(trust, str):
            kwargs["trust"] = ScopedTrustAgent(trust, co_dir=Path(co_dir))
            print(f"[co-studio] trust state scoped to {Path(co_dir).resolve()}", flush=True)
        return host_fn(agent, *args, **kwargs)

    wrapped._co_studio_scoped_trust = True  # type: ignore[attr-defined]
    return wrapped
