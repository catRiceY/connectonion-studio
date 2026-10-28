"""Bounded, optional projection of saved history into per-Agent knowledge.

Only standard archived prompt/result fields and whitelisted config are read.
No .env files, arbitrary filesystem traversal, or model calls. A full queue may
drop notifications; startup/cyclic reconciliation retries current snapshots.
"""
import hashlib
import json
import queue
import threading
from urllib.parse import quote

from . import registry
from .knowledge import Document, configured_store
from .execution_privacy import redact_text


def collection_for(slug):
    return "agent-" + hashlib.sha256(slug.encode()).hexdigest()[:24]


def index_capture(store, archive, slug, capture_id):
    meta = registry.load(slug)
    if meta is None:
        return
    owned = archive.get_owned(slug, capture_id)
    if owned is None:
        return
    # Notifications may be delayed or arrive out of source order. Select the
    # same preferred version as History, rather than replacing a current
    # document with an older queued capture. Import time/hash is not freshness.
    _, versions = archive.versions_page(slug, owned["capture"].session.session_id, limit=1)
    if not versions:
        return
    if versions[0]["capture_id"] != capture_id:
        owned = archive.get_owned(slug, versions[0]["capture_id"])
        if owned is None:
            return
    capture = owned["capture"]
    collection = collection_for(slug)
    config = {key: getattr(meta, key) for key in ("name", "model", "capabilities", "preset")}
    store.import_document(Document(f"studio://agents/{slug}/configuration", f"Agent {slug} configuration",
        json.dumps(config, ensure_ascii=False, indent=2), hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
        "Private local user data", collection, "configuration"))
    prefix = f"studio://agents/{slug}/sessions/{quote(capture.session.session_id, safe='')}/turns/"
    keep = []
    for turn in capture.turns:
        if turn.finish_reason is None:
            continue
        observations = [{"kind": event.kind, "call_id": event.call_id, "duration_ms": event.duration_ms,
                         **{key: event.payload[key] for key in ("name", "model", "status", "error", "error_type", "result", "declined") if key in event.payload}}
                        for event in capture.events if event.turn_no == turn.turn_no and event.kind in
                        {"tool_call", "tool_result", "llm_result", "studio_approval"}]
        content = redact_text(f"Agent: {slug}\nSession: {capture.session.session_id}\nTurn: {turn.turn_no}\n"
                              f"User request:\n{turn.prompt or 'Not recorded'}\nAnswer:\n{turn.result or 'Not recorded'}\n"
                              f"Finish reason: {turn.finish_reason}\nRecorded execution observations:\n{json.dumps(observations, ensure_ascii=False)}\n")
        # Omit oversized source rather than silently truncating its evidence.
        if len(content.encode()) > 512000:
            continue
        source = prefix + str(turn.turn_no)
        store.import_document(Document(source, f"{slug} turn {turn.turn_no}", content, capture.capture_id,
                                       "Private local user data", collection, "execution"))
        keep.append(source)
    # A preferred source may have reset/removed a turn. Old indexed text must
    # not remain eligible as current evidence just because its row still exists.
    store.prune_session(collection, prefix, keep)


class KnowledgeSync:
    def __init__(self, archive_factory, *, store_factory=configured_store):
        self.archive_factory, self.store_factory = archive_factory, store_factory
        self.queue = queue.Queue(maxsize=64)
        self.stop_event = threading.Event()
        self.thread = None
        self.state = "pending"

    def notify(self, slug, session_id, capture_id):
        try:
            self.queue.put_nowait((slug, capture_id))
        except queue.Full:
            self.state = "warning"

    def reconcile(self):
        archive = self.archive_factory()
        for meta in registry.load_all():
            offset = 0
            # Bounded per sweep; later new-capture notifications cover active work.
            while offset < 1000 and not self.stop_event.is_set():
                total, rows = archive.session_page(meta.slug, limit=100, offset=offset)
                for row in rows:
                    if self.stop_event.is_set():
                        return
                    index_capture(self.store_factory(), archive, meta.slug, row["capture_id"])
                offset += len(rows)
                if not rows or offset >= total:
                    break

    def _run(self):
        reconcile_after = 0
        import time
        while not self.stop_event.is_set():
            try:
                if time.monotonic() >= reconcile_after:
                    self.reconcile()
                    reconcile_after = time.monotonic() + 120
                try:
                    slug, capture = self.queue.get(timeout=1)
                except queue.Empty:
                    continue
                index_capture(self.store_factory(), self.archive_factory(), slug, capture)
                self.state = "ok"
            except Exception:
                self.state = "error"
                reconcile_after = time.monotonic() + 30
                self.stop_event.wait(2)

    def start(self):
        self.thread = threading.Thread(target=self._run, name="studio-knowledge-sync", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)
