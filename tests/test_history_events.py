"""No pytest-asyncio dependency: every test owns and closes its asyncio loop."""

import asyncio
import hashlib
import json
import threading

import pytest

from co_studio.history_events import HistoryEventHub, HubClosed, SSEEvent, SubscriptionLimit


CAPTURE = "a" * 64


def test_initial_reset_snapshots_before_subsequent_publications():
    async def exercise():
        hub = HistoryEventHub()
        hub.publish_status("alpha", "ok")
        sub = hub.subscribe("alpha")
        hub.publish_capture("alpha", "session-1", CAPTURE)
        initial = await sub.next(timeout=0)
        assert initial.event == "history.reset"
        assert initial.data == {"agent_slug": "alpha", "reason": "initial"}
        assert initial.id.endswith(":1")
        changed = await sub.next(timeout=0)
        assert changed.event == "history.changed"
        assert changed.id.endswith(":2")
        assert changed.data == {"agent_slug": "alpha", "session_id": "session-1", "capture_id": CAPTURE}
        assert await sub.next(timeout=0.001) is None
        sub.close()
        hub.close()
    asyncio.run(exercise())


def test_replay_is_scoped_and_advances_over_unrelated_notifications():
    async def exercise():
        hub = HistoryEventHub(max_events=3)
        first = hub.subscribe("alpha")
        cursor = (await first.next()).id
        first.close()
        hub.publish_capture("beta", "private-session", "b" * 64)
        hub.publish_status("alpha", "syncing")
        hub.publish_status("beta", "error")
        sub = hub.subscribe("alpha", cursor)
        event = await sub.next(timeout=0)
        assert event.event == "history.sync"
        assert event.data == {"agent_slug": "alpha", "state": "syncing"}
        assert event.id.endswith(":2")
        assert await sub.next(timeout=0) is None
        # The beta event advanced this reader to sequence 3. Evicting sequence
        # 1 and 2 therefore must not manufacture an overflow reset.
        hub.publish_status("beta", "ok")
        hub.publish_status("alpha", "ok")
        assert (await sub.next(timeout=0)).data["state"] == "ok"
        sub.close()
        hub.close()
    asyncio.run(exercise())


def test_ring_buffer_is_bounded_and_slow_reader_recovers_after_reset():
    async def exercise():
        hub = HistoryEventHub(max_events=2)
        sub = hub.subscribe("alpha")
        await sub.next()
        for index in range(1000):
            hub.publish_capture("alpha", f"session-{index}", CAPTURE)
        assert hub.buffered_event_count == 2
        assert hub.latest_sequence == 1000
        event = await sub.next(timeout=0)
        assert event.data == {"agent_slug": "alpha", "reason": "cursor_expired"}
        assert event.id.endswith(":1000")
        hub.publish_status("alpha", "ok")
        assert (await sub.next(timeout=0)).data["state"] == "ok"
        sub.close()
        hub.close()
    asyncio.run(exercise())


@pytest.mark.parametrize("kind,reason", [("scope", "cursor_invalid"), ("ahead", "cursor_invalid"), ("epoch", "cursor_expired"), ("evicted", "cursor_expired")])
def test_unusable_cursor_resets_without_cross_agent_metadata(kind, reason):
    async def exercise():
        hub = HistoryEventHub(max_events=1)
        sub = hub.subscribe("alpha")
        epoch, scope, sequence = (await sub.next()).id.split(":")
        sub.close()
        if kind == "scope":
            scope = hashlib.sha256(b"beta").hexdigest()
        elif kind == "ahead":
            sequence = "9999"
        elif kind == "epoch":
            epoch = "0" * 32 if epoch != "0" * 32 else "1" * 32
        else:
            hub.publish_capture("beta", "private-session", "b" * 64)
            hub.publish_status("beta", "error")
        sub = hub.subscribe("alpha", f"{epoch}:{scope}:{sequence}")
        event = await sub.next(timeout=0)
        assert event.event == "history.reset"
        assert event.data == {"agent_slug": "alpha", "reason": reason}
        assert "beta" not in event.encode().decode()
        assert "private-session" not in event.encode().decode()
        sub.close()
        hub.close()
    asyncio.run(exercise())


@pytest.mark.parametrize("cursor", ["garbage", "a:b:1", "\n", "a" * 10000, f"{'a' * 32}:{'b' * 64}:-1", f"{'a' * 32}:{'b' * 64}:01", f"{'a' * 32}:{'b' * 64}:1\n", f"{'a' * 32}:{'b' * 64}:1\r\ndata: secret", 1])
def test_malformed_cursor_fails_before_reserving_capacity(cursor):
    async def exercise():
        hub = HistoryEventHub(max_subscribers=1)
        with pytest.raises(ValueError, match="Malformed"):
            hub.subscribe("alpha", cursor)
        assert hub.subscriber_count == 0
        hub.close()
    asyncio.run(exercise())


def test_empty_event_id_behaves_like_initial_connection():
    async def exercise():
        hub = HistoryEventHub()
        sub = hub.subscribe("alpha", "")
        assert (await sub.next(timeout=0)).data["reason"] == "initial"
        sub.close()
        hub.close()
    asyncio.run(exercise())


def test_capacity_cleanup_and_shutdown_wake_waiters():
    async def exercise():
        hub = HistoryEventHub(max_subscribers=1)
        sub = hub.subscribe("alpha")
        await sub.next()
        with pytest.raises(SubscriptionLimit):
            hub.subscribe("beta")
        waiting = asyncio.create_task(sub.next())
        await asyncio.sleep(0)
        sub.close()
        sub.close()
        with pytest.raises(HubClosed):
            await asyncio.wait_for(waiting, 1)
        assert hub.subscriber_count == 0
        replacement = hub.subscribe("beta")
        await replacement.next()
        waiting = asyncio.create_task(replacement.next())
        await asyncio.sleep(0)
        hub.close()
        hub.close()
        with pytest.raises(HubClosed):
            await asyncio.wait_for(waiting, 1)
        assert hub.subscriber_count == 0
        with pytest.raises(HubClosed):
            hub.subscribe("alpha")
        with pytest.raises(HubClosed):
            hub.publish_status("alpha", "ok")
        replacement.close()
    asyncio.run(exercise())


def test_worker_thread_publication_wakes_a_waiting_reader():
    async def exercise():
        hub = HistoryEventHub()
        sub = hub.subscribe("alpha")
        await sub.next()
        waiting = asyncio.create_task(sub.next(timeout=1))
        await asyncio.sleep(0)
        publisher = threading.Thread(target=hub.publish_capture, args=("alpha", "session-1", CAPTURE))
        publisher.start()
        publisher.join()
        event = await asyncio.wait_for(waiting, 1)
        assert event is not None
        assert event.data["session_id"] == "session-1"
        sub.close()
        hub.close()
    asyncio.run(exercise())


def test_concurrent_publishers_have_global_monotonic_sequences():
    async def exercise():
        hub = HistoryEventHub(max_events=400)
        sub = hub.subscribe("alpha")
        await sub.next()
        def publish():
            for _ in range(100):
                hub.publish_status("alpha", "ok")
        publishers = [threading.Thread(target=publish) for _ in range(4)]
        for publisher in publishers:
            publisher.start()
        for publisher in publishers:
            publisher.join()
        events = [await sub.next(timeout=0) for _ in range(400)]
        assert [int(event.id.rsplit(":", 1)[1]) for event in events] == list(range(1, 401))
        assert hub.buffered_event_count == 400
        sub.close()
        hub.close()
    asyncio.run(exercise())


def test_burst_publications_coalesce_threadsafe_wake_callbacks(monkeypatch):
    async def exercise():
        hub = HistoryEventHub(max_events=2)
        sub = hub.subscribe("alpha")
        await sub.next()
        loop = asyncio.get_running_loop()
        original = loop.call_soon_threadsafe
        callbacks = []
        def tracked(callback, *args, **kwargs):
            callbacks.append(callback)
            return original(callback, *args, **kwargs)
        monkeypatch.setattr(loop, "call_soon_threadsafe", tracked)
        def publish():
            for _ in range(5000):
                hub.publish_status("alpha", "ok")
        publisher = threading.Thread(target=publish)
        publisher.start()
        publisher.join()
        assert len(callbacks) == 1
        assert hub.buffered_event_count == 2
        await asyncio.sleep(0)
        hub.publish_status("alpha", "ok")
        assert len(callbacks) == 2
        sub.close()
        hub.close()
    asyncio.run(exercise())


def test_periodic_resync_snapshots_highwater_without_losing_next_publication():
    async def exercise():
        hub = HistoryEventHub()
        sub = hub.subscribe("alpha")
        await sub.next()
        hub.publish_status("alpha", "syncing")
        event = sub.resync(reason="reconcile")
        assert event.data == {"agent_slug": "alpha", "reason": "reconcile"}
        assert event.id.endswith(":1")
        hub.publish_status("alpha", "ok")
        assert (await sub.next(timeout=0)).data["state"] == "ok"
        sub.close()
        hub.close()
    asyncio.run(exercise())


def test_cancelled_read_can_be_retried_and_cleaned_up():
    async def exercise():
        hub = HistoryEventHub()
        sub = hub.subscribe("alpha")
        await sub.next()
        waiting = asyncio.create_task(sub.next())
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        hub.publish_status("alpha", "ok")
        assert (await sub.next(timeout=0)).data["state"] == "ok"
        sub.close()
        assert hub.subscriber_count == 0
        hub.close()
    asyncio.run(exercise())


def test_encoder_escapes_data_and_rejects_sse_field_injection():
    cursor = f"{'a' * 32}:{'b' * 64}:1"
    data = {"agent_slug": "alpha", "session_id": "中文\n\rid: forged"}
    event = SSEEvent("history.changed", cursor, data)
    data["session_id"] = "modified"
    encoded = event.encode().decode("utf-8")
    assert len(encoded.splitlines()) == 4
    assert json.loads(encoded.splitlines()[2].removeprefix("data: "))["session_id"] == "中文\n\rid: forged"
    with pytest.raises(ValueError):
        SSEEvent("history.changed\ndata: injected", cursor, {})
    with pytest.raises(ValueError):
        SSEEvent("history.changed", cursor + "\nid: injected", {})


def test_publication_accepts_only_bounded_identifier_metadata_and_fixed_states():
    async def exercise():
        hub = HistoryEventHub()
        for slug in ("../alpha", "alpha\n", "", "a" * 256, "Alpha", "alpha_beta", "alpha--beta", "-alpha", "alpha-"):
            with pytest.raises(ValueError):
                hub.subscribe(slug)
            with pytest.raises(ValueError):
                hub.publish_status(slug, "ok")
        for session in ("", "a" * 513, "中" * 513, "a\nb", "a\x00b", "a b", "a\u0080b", {}):
            with pytest.raises(ValueError):
                hub.publish_capture("alpha", session, CAPTURE)
        for capture in ("a" * 63, "g" * 64, "A" * 64, "a" * 64 + "\n", {}):
            with pytest.raises(ValueError):
                hub.publish_capture("alpha", "session", capture)
        for state in ("secret error body", "ok\n", {"error": "secret"}):
            with pytest.raises(ValueError):
                hub.publish_status("alpha", state)
        assert hub.latest_sequence == 0
        hub.publish_capture("alpha", "a" * 512, CAPTURE)
        hub.publish_capture("alpha", "中" * 512, CAPTURE)
        hub.publish_capture("alpha", "🧅" * 512, CAPTURE)
        long_slug = "a" * 255
        sub = hub.subscribe(long_slug)
        await sub.next()
        hub.publish_capture(long_slug, "session", CAPTURE)
        assert (await sub.next(timeout=0)).data["agent_slug"] == long_slug
        sub.close()
        for state in ("pending", "syncing", "ok", "warning", "error", "stopped", "disabled"):
            hub.publish_status("alpha", state)
        assert hub.latest_sequence == 11
        hub.close()
    asyncio.run(exercise())
