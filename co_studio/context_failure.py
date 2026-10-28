"""Narrow CO 1.9.2 Host failure-snapshot adaptation, without storage writes.

Host hands input() a detached, server-claimed snapshot, but saves that old
snapshot on exceptions. Only an explicitly enabled generated Host factory may
refresh its execution fields before Host performs its normal failed commit.
Never copy permission/mode fields from the running Agent back into the claim.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import logging
from pathlib import Path

logger = logging.getLogger(__name__)
_EXECUTION_FIELDS = ("messages", "trace", "turn", "iteration", "user_prompt", "updated")


@dataclass
class HostFailureSnapshot:
    storage: object
    claimed: dict
    baseline: dict
    session_id: str
    prompt: str
    revision: int
    created: float | None
    expires: float | None

    def recover(self, current: object) -> bool:
        """Copy observed execution, not a result or fabricated terminal event.

        Any changed claim, session, turn, trace prefix, or durable ownership
        boundary fails closed. Persistence is still Host's job; disk failure,
        process death and failures before a terminal trace remain limitations.
        """
        try:
            if (not isinstance(current, dict) or self.claimed != self.baseline
                    or current.get("session_id") != self.session_id
                    or current.get("turn") != self.baseline.get("turn", 0) + 1
                    or current.get("user_prompt") != self.prompt):
                return False
            trace, previous = current.get("trace"), self.baseline.get("trace", [])
            if (not isinstance(trace, list) or not isinstance(previous, list)
                    or len(trace) <= len(previous) or trace[:len(previous)] != previous):
                return False
            terminal = trace[-1]
            if (not isinstance(terminal, dict) or terminal.get("type") != "turn_result"
                    or terminal.get("turn") != current["turn"] or terminal.get("reason") != "error"):
                return False
            latest = self.storage.get(self.session_id)
            if (latest is None or latest.status != "running" or latest.prompt != self.prompt
                    or latest.revision < self.revision or latest.created != self.created
                    or latest.expires != self.expires or not isinstance(latest.session, dict)):
                return False
            # A connection may have persisted a prefix already. It must belong
            # to this exact execution, not another writer or a later claim.
            durable_trace = latest.session.get("trace", [])
            if (not isinstance(durable_trace, list)
                    or trace[:len(durable_trace)] != durable_trace
                    or latest.session.get("requester") != self.baseline.get("requester")):
                return False
            detached = deepcopy({name: current[name] for name in _EXECUTION_FIELDS if name in current})
            self.claimed.update(detached)
            # An earlier turn's answer must not become this failed turn's one.
            self.claimed.pop("result", None)
            return True
        except Exception:
            # Do not mask the original model/IO/context exception or log data.
            logger.warning("Studio could not prepare the Host failure snapshot")
            return False


def capture_host_claim(agent, supplied, prompt, *, agent_dir: Path) -> HostFailureSnapshot | None:
    """Validate the generated Host contract; direct input() remains untouched."""
    from connectonion.network.host.session import SessionStorage

    try:
        storage = getattr(agent, "storage", None)
        if (not isinstance(storage, SessionStorage) or not isinstance(supplied, dict)
                or not isinstance(prompt, str)
                or Path(storage.path).absolute() != Path(agent_dir).absolute() / ".co" / "session_results.jsonl"):
            return None
        session_id = supplied.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return None
        record = storage.get(session_id)
        if (record is None or record.status != "running" or record.prompt != prompt
                or record.session != supplied or type(supplied.get("turn", 0)) is not int
                or supplied.get("turn", 0) < 0):
            return None
        return HostFailureSnapshot(storage, supplied, deepcopy(supplied), session_id,
                                   prompt, record.revision, record.created, record.expires)
    except Exception:
        logger.warning("Studio could not validate the Host failure snapshot boundary")
        return None
