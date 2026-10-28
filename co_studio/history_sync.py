"""Best-effort offline source-to-PostgreSQL ingestion in one daemon thread.

Only the app's explicitly configured PostgreSQL factory enables this worker.
There is no Agent/runtime integration, schema initialization, source mutation,
or alternate storage backend. Signatures suppress repeated parsing; periodic
idempotent saves recover even if the archive database was restored or cleared.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import re
import stat
import threading
import time
from typing import BinaryIO, Callable, Iterator

from . import config
from .api.sessions import _history_source
from .execution_adapter import HistoryRead, read_execution_stream
from .history_archive import PostgresArchive, prepare_capture


logger = logging.getLogger(__name__)
MAX_SOURCE_BYTES = 64 * 1024 * 1024
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_UNSET = object()


@dataclass
class _AgentState:
    state: str = "pending"
    checked_at: str | None = None
    synced_at: str | None = None
    message: str | None = None
    signature: tuple | None = None
    success_time: float | None = None
    success_message: str | None = None
    source_seen: bool = False
    failures: int = 0
    retry_at: float = 0.0


class _SourceChanged(Exception):
    pass


class _SourceTooLarge(Exception):
    pass


class HistorySync:
    """A single-use lifecycle; construction and status reads do no source I/O.

    ``scan_once`` also works before ``start`` for deterministic offline checks.
    ``stop`` prevents further save calls after the current in-flight save; its
    bounded join never waits for database retries. Every SQL save is admitted
    only after checking the stop event. No status lock is held across I/O.
    Optional callbacks report new commits and meaningful status changes outside
    that lock; notification failures never affect the archive outcome.

    Secure opening requires POSIX dir_fd/O_NOFOLLOW (macOS and Linux). Other
    platforms report an error instead of silently following linked paths.
    """

    def __init__(
        self,
        archive_factory: Callable[[], PostgresArchive],
        *,
        poll_interval: float = 2.0,
        reconcile_interval: float = 60.0,
        retry_max: float = 60.0,
        max_source_bytes: int = MAX_SOURCE_BYTES,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        reader: Callable[..., HistoryRead] = read_execution_stream,
        on_capture: Callable[[str, str, str], None] | None = None,
        on_status: Callable[[str, str], None] | None = None,
    ) -> None:
        if min(poll_interval, reconcile_interval, retry_max, max_source_bytes) <= 0:
            raise ValueError("History synchronization limits must be positive")
        self._archive_factory = archive_factory
        self._poll_interval = poll_interval
        self._reconcile_interval = reconcile_interval
        self._retry_max = retry_max
        self._max_source_bytes = max_source_bytes
        self._monotonic = monotonic
        self._clock = clock
        self._reader = reader
        self._on_capture = on_capture
        self._on_status = on_status
        self._states: dict[str, _AgentState] = {}
        self._lock = threading.Lock()
        self._scan_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._scan_error: dict | None = None
        self._registry_root: Path | None = None

    def start(self) -> None:
        """Schedule immediate catch-up without blocking startup on files/SQL."""
        with self._lifecycle_lock:
            if self._thread is not None or self._stop.is_set():
                return
            self._thread = threading.Thread(
                target=self._run, name="studio-history-sync", daemon=True,
            )
            try:
                self._thread.start()
            except Exception:
                # Failed startup must not leave an unstarted Thread for stop
                # to join, which would raise again during application shutdown.
                self._thread = None
                raise

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        with self._lock:
            changed = []
            for slug, entry in self._states.items():
                if entry.state != "stopped":
                    changed.append(slug)
                entry.state = "stopped"
        for slug in changed:
            self._notify(self._on_status, slug, "stopped")
        with self._lifecycle_lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, timeout))

    def status(self, slug: str) -> dict:
        with self._lock:
            entry = self._states.get(slug)
            if entry is None:
                result = dict(self._scan_error or {
                    "state": "pending", "checked_at": None,
                    "synced_at": None, "message": None,
                })
            else:
                result = {key: getattr(entry, key) for key in (
                    "state", "checked_at", "synced_at", "message",
                )}
            if self._stop.is_set():
                result["state"] = "stopped"
            return result

    def _timestamp(self) -> str:
        return self._clock().isoformat()

    @staticmethod
    def _notify(callback: Callable[..., None] | None, *args: str) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:
            # Delivery is best effort and must not change archive outcomes.
            # Never log callback arguments, exception text, or traceback.
            logger.warning("History sync notification failed")

    def _set_status(self, slug: str, state: str, message: str | None = None,
                    *, synced_at=_UNSET) -> None:
        timestamp = self._timestamp()
        with self._lock:
            entry = self._states.setdefault(slug, _AgentState())
            state = "stopped" if self._stop.is_set() else state
            changed = entry.state != state or entry.message != message
            entry.state = state
            entry.checked_at = timestamp
            entry.message = message
            if synced_at is not _UNSET:
                entry.synced_at = synced_at
        if changed:
            self._notify(self._on_status, slug, state)

    def _failure(self, slug: str, message: str) -> None:
        now = self._monotonic()
        with self._lock:
            entry = self._states.setdefault(slug, _AgentState())
            changed = entry.state != "error" or entry.message != message
            entry.failures = min(entry.failures + 1, 32)
            entry.retry_at = now + min(
                self._retry_max, self._poll_interval * (2 ** (entry.failures - 1)),
            )
        self._set_status(slug, "error", message)
        if changed:
            # Deliberately omit exceptions, paths, SQL, DSNs and source content.
            logger.warning("History sync failed for agent %s: %s", slug, message)

    def _global_failure(self) -> None:
        timestamp = self._timestamp()
        message = "Unable to scan the agent registry; synchronization will retry"
        with self._lock:
            changed = self._scan_error is None
            self._scan_error = {
                "state": "error", "checked_at": timestamp,
                "synced_at": None, "message": message,
            }
            state = "stopped" if self._stop.is_set() else "error"
            affected = []
            for slug, entry in self._states.items():
                if entry.state != state or entry.message != message:
                    affected.append(slug)
                entry.state = state
                entry.checked_at = timestamp
                entry.message = message
        for slug in affected:
            self._notify(self._on_status, slug, state)
        if changed:
            logger.warning(message)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception:
                # A directory iterator or unexpected adapter failure must not
                # terminate the only worker and silently stop future catch-up.
                self._global_failure()
            self._stop.wait(self._poll_interval)

    def scan_once(self) -> None:
        """Check each registered directory independently, with no network setup."""
        if self._stop.is_set():
            return
        # Serialize manual diagnostic scans with the worker, without blocking
        # API status reads or allowing two batches to issue duplicate SQL.
        if not self._scan_lock.acquire(blocking=False):
            return
        try:
            try:
                root = config.AGENTS_DIR.resolve()
                with self._lock:
                    if self._registry_root != root:
                        # Slugs only identify agents within the active
                        # registry. A storage switch must not inherit another
                        # root's timestamps, warnings, or retry backoff.
                        self._states.clear()
                        self._scan_error = None
                        self._registry_root = root
                directories = sorted(root.iterdir()) if root.exists() else []
            except Exception:
                self._global_failure()
                return
            with self._lock:
                self._scan_error = None
            for directory in directories:
                if self._stop.is_set():
                    break
                slug = directory.name
                if not _SLUG.fullmatch(slug):
                    continue
                try:
                    # Do not follow directory symlinks, or use load_all(): one
                    # corrupt metadata file must not prevent other agents.
                    mode = directory.lstat().st_mode
                    if not (stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
                        continue
                    self._scan_agent(slug)
                except Exception:
                    self._failure(slug, "Unable to synchronize agent history")
        finally:
            self._scan_lock.release()

    @staticmethod
    def _signature(source: Path, info: os.stat_result) -> tuple:
        return (str(source), info.st_dev, info.st_ino, info.st_size,
                info.st_mtime_ns, info.st_ctime_ns)

    def _source_signature(self, source: Path) -> tuple | None:
        try:
            info = source.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            raise OSError("History source must be a regular file")
        if info.st_size > self._max_source_bytes:
            raise _SourceTooLarge
        return self._signature(source, info)

    @contextmanager
    def _open_source(self, source: Path) -> Iterator[BinaryIO]:
        if (os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW")
                or not hasattr(os, "O_DIRECTORY")):
            raise OSError("Secure history reads are unavailable")
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptors: list[int] = []
        try:
            # The configured root is canonicalized by _history_source; every
            # descendant is opened relative to a pinned, non-linked directory.
            parent = os.open(source.parents[2], directory_flags)
            descriptors.append(parent)
            for component in (source.parents[1].name, source.parent.name):
                parent = os.open(component, directory_flags, dir_fd=parent)
                descriptors.append(parent)
            descriptor = os.open(
                source.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=parent,
            )
            with os.fdopen(descriptor, "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise OSError("History source must be a regular file")
                if info.st_size > self._max_source_bytes:
                    raise _SourceTooLarge
                yield handle
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    def _stable_read(self, slug: str, source: Path, signature: tuple) -> HistoryRead:
        verifier = None

        def verify_provenance(raw: dict, owner: str, session_id: str) -> bool:
            nonlocal verifier
            if verifier is None:
                # Legacy traces never read or create provenance storage. Load
                # one read-only verifier only if the adapter sees a new event.
                verifier = lambda *_: False
                from .context_report import history_event_verifier
                verifier = history_event_verifier(source.parent.parent.resolve())
            return verifier(raw, owner, session_id)

        with self._open_source(source) as handle:
            if self._signature(source, os.fstat(handle.fileno())) != signature:
                raise _SourceChanged
            try:
                history = self._reader(
                    handle, agent_slug=slug, max_bytes=self._max_source_bytes,
                    provenance_verifier=verify_provenance,
                )
            except ValueError:
                # The file can grow between our first fstat and the adapter's
                # own bounded read check. Keep size failures explicit as well.
                if os.fstat(handle.fileno()).st_size > self._max_source_bytes:
                    raise _SourceTooLarge from None
                raise
            if self._signature(source, os.fstat(handle.fileno())) != signature:
                raise _SourceChanged
        if self._source_signature(_history_source(slug)) != signature:
            raise _SourceChanged
        return history

    def _scan_agent(self, slug: str) -> None:
        try:
            # Safety and registry membership are revalidated even on a cache
            # hit or during database backoff. No successful signature bypasses
            # checks for a newly introduced symlink or corrupt metadata.
            source = _history_source(slug)
            signature = self._source_signature(source)
        except _SourceTooLarge:
            with self._lock:
                self._states.setdefault(slug, _AgentState()).source_seen = True
            self._failure(slug, f"History source exceeds the {self._max_source_bytes:,}-byte archive limit")
            return
        except Exception:
            self._failure(slug, "Agent metadata or history source is unavailable or unsafe")
            return
        if signature is None:
            with self._lock:
                source_seen = self._states.setdefault(slug, _AgentState()).source_seen
            self._set_status(
                slug, "warning" if source_seen else "ok",
                "History source is missing; existing captures are retained" if source_seen else None,
            )
            return
        now = self._monotonic()
        with self._lock:
            entry = self._states.setdefault(slug, _AgentState())
            entry.source_seen = True
            retry_at = entry.retry_at
            cached = (
                entry.signature == signature and entry.success_time is not None
                and now - entry.success_time < self._reconcile_interval
                and entry.failures == 0
            )
            previous_state, previous_message = entry.state, entry.message
            success_message = entry.success_message
        if now < retry_at:
            self._set_status(slug, previous_state, previous_message)
            return
        if cached:
            self._set_status(slug, "warning" if success_message else "ok", success_message)
            return
        if self._stop.is_set():
            return
        self._set_status(slug, "syncing")
        try:
            history = self._stable_read(slug, source, signature)
        except _SourceChanged:
            self._set_status(slug, "pending", "History source changed during reading; synchronization will retry")
            return
        except _SourceTooLarge:
            self._failure(slug, f"History source exceeds the {self._max_source_bytes:,}-byte archive limit")
            return
        except Exception:
            self._failure(slug, "Unable to read history source")
            return
        if self._stop.is_set():
            return
        try:
            # Lazily create the archive for each attempted agent batch; a
            # failed configuration/connection cannot silently switch storage.
            archive = self._archive_factory() if history.sessions else None
            for converted in history.sessions:
                if self._stop.is_set():
                    return
                capture = prepare_capture(converted)
                if self._stop.is_set():
                    return
                if archive.save(capture):
                    # save() returns only after its transaction has committed.
                    # Each committed session is notified even if a later save
                    # fails or the source changes before the batch completes.
                    self._notify(
                        self._on_capture, slug, capture.session.session_id,
                        capture.capture_id,
                    )
        except Exception:
            self._failure(slug, "Unable to write history archive")
            return
        if self._stop.is_set():
            return
        try:
            if self._source_signature(_history_source(slug)) != signature:
                raise _SourceChanged
        except Exception:
            self._set_status(slug, "pending", "History source changed during synchronization; synchronization will retry")
            return
        issue_count = len(history.issues)
        message = (
            f"History contains {issue_count} read issue(s); {len(history.sessions)} valid session(s) archived"
            if issue_count else None
        )
        with self._lock:
            entry = self._states[slug]
            entry.signature = signature
            entry.success_time = self._monotonic()
            entry.success_message = message
            entry.failures = 0
            entry.retry_at = 0.0
        self._set_status(slug, "warning" if issue_count else "ok", message, synced_at=self._timestamp())
