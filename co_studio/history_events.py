"""Bounded, process-local notifications for authoritative history reloads.

The hub retains metadata only. One global ring buffer serves all readers, so a
slow or disconnected reader never creates an unbounded queue. Subscriptions
belong to the constructing asyncio loop; publishers may run on any thread.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import secrets
import threading
from collections import deque
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_CAPTURE_ID = re.compile(r"[0-9a-f]{64}\Z")
_CURSOR = re.compile(r"([0-9a-f]{32}):([0-9a-f]{64}):(0|[1-9][0-9]{0,19})\Z")
_EVENTS = frozenset({"history.changed", "history.sync", "history.reset"})
_STATES = frozenset({"pending", "syncing", "ok", "warning", "error", "stopped", "disabled"})
_RESET_REASONS = frozenset({"initial", "cursor_expired", "cursor_invalid", "reconcile"})


class HubClosed(RuntimeError):
    """The hub or the subscription is closed."""


class SubscriptionLimit(RuntimeError):
    """The process has reached its concurrent subscription limit."""


def _validate_slug(slug: str) -> None:
    if not isinstance(slug, str) or len(slug) > 255 or not _SLUG.fullmatch(slug):
        raise ValueError("Invalid agent slug")


def _parse_cursor(value: str) -> tuple[str, str, int]:
    if not isinstance(value, str) or len(value) > 118:
        raise ValueError("Malformed Last-Event-ID")
    match = _CURSOR.fullmatch(value)
    if match is None:
        raise ValueError("Malformed Last-Event-ID")
    return match[1], match[2], int(match[3])


@dataclass(frozen=True, slots=True)
class SSEEvent:
    """A safely encoded SSE frame containing only notification metadata."""

    event: str
    id: str
    data: Mapping[str, str]

    def __post_init__(self) -> None:
        if self.event not in _EVENTS:
            raise ValueError("Invalid history event type")
        _parse_cursor(self.id)
        # Freeze a private copy: callers cannot mutate retained event metadata.
        object.__setattr__(self, "data", MappingProxyType(dict(self.data)))

    def encode(self) -> bytes:
        payload = json.dumps(dict(self.data), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        return f"event: {self.event}\nid: {self.id}\ndata: {payload}\n\n".encode("utf-8")


@dataclass(frozen=True, slots=True)
class _Notification:
    sequence: int
    slug: str
    event: str
    data: Mapping[str, str]


class HistoryEventHub:
    """Thread-safe metadata ring buffer with loop-owned subscribers.

    A cursor is opaque to callers and is scoped to a process epoch and agent.
    Syntactically malformed cursors fail synchronously in ``subscribe``. Valid
    but unusable cursors produce a reset, prompting an authoritative reload.
    """

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
        max_events: int = 256,
        max_subscribers: int = 32,
    ) -> None:
        for name, value in (("max_events", max_events), ("max_subscribers", max_subscribers)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self._loop = loop if loop is not None else asyncio.get_running_loop()
        if self._loop.is_closed():
            raise HubClosed("History event loop is closed")
        self._epoch = secrets.token_hex(16)
        self._sequence = 0
        self._events: deque[_Notification] = deque(maxlen=max_events)
        self._max_subscribers = max_subscribers
        self._subscribers: set[HistorySubscription] = set()
        self._lock = threading.Lock()
        self._wake_scheduled = False
        self._closed = False

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    @property
    def latest_sequence(self) -> int:
        with self._lock:
            return self._sequence

    @property
    def buffered_event_count(self) -> int:
        with self._lock:
            return len(self._events)

    def _require_loop(self) -> None:
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("History subscriptions must run on their owning event loop")

    def _id(self, scope: str, sequence: int) -> str:
        return f"{self._epoch}:{scope}:{sequence}"

    def subscribe(self, slug: str, last_event_id: str | None = None) -> HistorySubscription:
        """Reserve a reader synchronously, before starting an HTTP response."""
        self._require_loop()
        _validate_slug(slug)
        parsed = _parse_cursor(last_event_id) if last_event_id is not None and last_event_id != "" else None
        scope = hashlib.sha256(slug.encode("utf-8")).hexdigest()
        with self._lock:
            if self._closed:
                raise HubClosed("History event hub is closed")
            if len(self._subscribers) >= self._max_subscribers:
                raise SubscriptionLimit("History subscriber limit reached")
            cursor = self._sequence
            reason = "initial"
            if parsed is not None:
                epoch, cursor_scope, sequence = parsed
                if cursor_scope != scope or sequence > self._sequence:
                    reason = "cursor_invalid"
                elif epoch != self._epoch:
                    reason = "cursor_expired"
                elif sequence < self._oldest_sequence_locked() - 1:
                    reason = "cursor_expired"
                else:
                    cursor, reason = sequence, None
            subscription = HistorySubscription(self, slug, scope, cursor, reason)
            self._subscribers.add(subscription)
            return subscription

    def _oldest_sequence_locked(self) -> int:
        return self._events[0].sequence if self._events else self._sequence + 1

    def publish_capture(self, slug: str, session_id: str, capture_id: str) -> None:
        """Publish a persisted capture identifier, never its history payload."""
        _validate_slug(slug)
        if (
            not isinstance(session_id, str)
            or not session_id
            or len(session_id) > 512
            or len(session_id.encode("utf-8")) > 2048
            or any(char.isspace() or ord(char) < 32 or 127 <= ord(char) <= 159 for char in session_id)
        ):
            raise ValueError("Invalid history session identifier")
        if not isinstance(capture_id, str) or not _CAPTURE_ID.fullmatch(capture_id):
            raise ValueError("Invalid history capture identifier")
        self._publish(slug, "history.changed", {"agent_slug": slug, "session_id": session_id, "capture_id": capture_id})

    def publish_status(self, slug: str, state: str) -> None:
        """Publish only a fixed status value; errors and credentials stay out."""
        _validate_slug(slug)
        if not isinstance(state, str) or state not in _STATES:
            raise ValueError("Invalid history sync state")
        self._publish(slug, "history.sync", {"agent_slug": slug, "state": state})

    def _publish(self, slug: str, event: str, data: dict[str, str]) -> None:
        with self._lock:
            if self._closed:
                raise HubClosed("History event hub is closed")
            self._sequence += 1
            self._events.append(_Notification(self._sequence, slug, event, MappingProxyType(data)))
            if self._subscribers and not self._wake_scheduled:
                self._wake_scheduled = True
                try:
                    self._loop.call_soon_threadsafe(self._wake_readers)
                except RuntimeError as error:
                    self._wake_scheduled = False
                    self._closed = True
                    raise HubClosed("History event loop is closed") from error

    def _wake_readers(self) -> None:
        with self._lock:
            self._wake_scheduled = False
            readers = tuple(self._subscribers)
        for reader in readers:
            reader._ready.set()

    def close(self) -> None:
        """Stop publishing and wake all waiting readers; safe on any thread."""
        with self._lock:
            if self._closed and not self._subscribers:
                return
            self._closed = True
            readers = tuple(self._subscribers)
            self._subscribers.clear()
            self._events.clear()
        if readers:
            try:
                on_owner_loop = asyncio.get_running_loop() is self._loop
            except RuntimeError:
                on_owner_loop = False
            if on_owner_loop:
                self._wake_closed_readers(readers)
                return
            try:
                self._loop.call_soon_threadsafe(self._wake_closed_readers, readers)
            except RuntimeError:
                # A stopped/closed event loop cannot have live waiting tasks.
                pass

    @staticmethod
    def _wake_closed_readers(readers: tuple[HistorySubscription, ...]) -> None:
        for reader in readers:
            reader._ready.set()


class HistorySubscription:
    """A bounded reader position and wake flag, with no per-reader event queue."""

    def __init__(self, hub: HistoryEventHub, slug: str, scope: str, cursor: int, reason: str | None) -> None:
        self._hub = hub
        self._slug = slug
        self._scope = scope
        self._cursor = cursor
        self._pending_reset = reason
        self._ready = asyncio.Event()
        self._closed = False
        self._reading = False

    def _check_open_locked(self) -> None:
        if self._closed or self._hub._closed:
            raise HubClosed("History subscription is closed")

    def _reset_event(self, reason: str) -> SSEEvent:
        return SSEEvent("history.reset", self._hub._id(self._scope, self._cursor), {"agent_slug": self._slug, "reason": reason})

    def resync(self, reason: str = "reconcile") -> SSEEvent:
        """Snapshot the high-water mark before an authoritative client reload."""
        self._hub._require_loop()
        if not isinstance(reason, str) or reason not in _RESET_REASONS:
            raise ValueError("Invalid history reset reason")
        with self._hub._lock:
            self._check_open_locked()
            self._cursor = self._hub._sequence
            self._pending_reset = None
            return self._reset_event(reason)

    async def next(self, timeout: float | None = None) -> SSEEvent | None:
        """Return the next scoped event, or ``None`` for a heartbeat timeout."""
        self._hub._require_loop()
        if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
            raise ValueError("History timeout must be finite and nonnegative")
        if self._reading:
            raise RuntimeError("Only one history read may be pending per subscription")
        self._reading = True
        deadline = None if timeout is None else self._hub._loop.time() + timeout
        try:
            while True:
                with self._hub._lock:
                    self._check_open_locked()
                    if self._pending_reset is not None:
                        reason, self._pending_reset = self._pending_reset, None
                        return self._reset_event(reason)
                    if self._cursor < self._hub._oldest_sequence_locked() - 1:
                        self._cursor = self._hub._sequence
                        return self._reset_event("cursor_expired")
                    for record in self._hub._events:
                        if record.sequence <= self._cursor:
                            continue
                        self._cursor = record.sequence
                        if record.slug == self._slug:
                            return SSEEvent(record.event, self._hub._id(self._scope, record.sequence), record.data)
                    self._cursor = self._hub._sequence
                    # Clearing under the publisher lock prevents a lost wakeup.
                    self._ready.clear()
                remaining = None if deadline is None else deadline - self._hub._loop.time()
                if remaining is not None and remaining <= 0:
                    return None
                try:
                    if remaining is None:
                        await self._ready.wait()
                    else:
                        await asyncio.wait_for(self._ready.wait(), remaining)
                except TimeoutError:
                    return None
        finally:
            self._reading = False

    def close(self) -> None:
        """Release capacity exactly once and wake a pending read."""
        with self._hub._lock:
            if self._closed:
                return
            self._closed = True
            self._hub._subscribers.discard(self)
            reading = self._reading
        if not reading:
            return
        try:
            on_owner_loop = asyncio.get_running_loop() is self._hub._loop
        except RuntimeError:
            on_owner_loop = False
        if on_owner_loop:
            self._ready.set()
            return
        try:
            self._hub._loop.call_soon_threadsafe(self._ready.set)
        except RuntimeError:
            pass
