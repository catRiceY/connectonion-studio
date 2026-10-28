from types import SimpleNamespace as NS
from unittest.mock import Mock

from co_studio import knowledge_sync as ks


def capture(identity, result):
    return NS(capture_id=identity, session=NS(session_id="session/one"), events=[], turns=[
        NS(turn_no=1, prompt="Please diagnose the original request", result=result, finish_reason="natural"),
        NS(turn_no=2, prompt="still running", result=None, finish_reason=None),
    ])


def test_queued_old_capture_indexes_current_preferred_version(monkeypatch):
    meta = NS(name="Demo", model="test", capabilities=["read"], preset="custom", secret="DO NOT INDEX")
    monkeypatch.setattr(ks.registry, "load", lambda slug: meta)
    old, new = capture("old", "Old obsolete result"), capture("new", "Current completed result")
    archive = Mock()
    archive.get_owned.side_effect = lambda slug, identity: {"capture": {"old": old, "new": new}[identity]}
    archive.versions_page.return_value = (2, [{"capture_id": "new"}])
    store = Mock()
    ks.index_capture(store, archive, "agent-one", "old")
    documents = [args.args[0] for args in store.import_document.call_args_list]
    assert len(documents) == 2
    assert documents[1].version == "new"
    assert "Current completed result" in documents[1].text
    assert "Old obsolete result" not in documents[1].text
    assert "DO NOT INDEX" not in documents[0].text
    assert all(doc.collection == ks.collection_for("agent-one") for doc in documents)
    assert "session%2Fone" in documents[1].source_uri
    archive.versions_page.assert_called_once_with("agent-one", "session/one", limit=1)
    store.prune_session.assert_called_once_with(ks.collection_for("agent-one"),
        "studio://agents/agent-one/sessions/session%2Fone/turns/", [documents[1].source_uri])


def test_missing_or_unowned_capture_cannot_be_indexed(monkeypatch):
    monkeypatch.setattr(ks.registry, "load", lambda slug: NS())
    archive, store = Mock(), Mock()
    archive.get_owned.return_value = None
    ks.index_capture(store, archive, "someone-else", "id")
    store.import_document.assert_not_called()
    assert ks.collection_for("one") != ks.collection_for("two")


def test_queue_is_bounded_and_shutdown_is_safe():
    worker = ks.KnowledgeSync(Mock())
    for index in range(100):
        worker.notify("one", "session", str(index))
    assert worker.queue.qsize() == 64
    assert worker.state == "warning"
    worker.stop()
    assert worker.stop_event.is_set()
