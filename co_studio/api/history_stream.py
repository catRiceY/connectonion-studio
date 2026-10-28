"""Local history invalidations, not token streaming or a durable event log.

Subscribers read authoritative History GETs after a notification. Only the
background sync worker publishes committed capture metadata; reconnect gaps and
periodic reconciliation request a fresh read instead of claiming lossless replay.
"""
from __future__ import annotations

import asyncio
import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

from ..history_events import HistoryEventHub, HubClosed, SubscriptionLimit
from . import archives
from .sessions import Slug, _local_history_request


HEARTBEAT_SECONDS = 15.0
RECONCILE_SECONDS = 60.0
SEND_TIMEOUT_SECONDS = 5.0
router = APIRouter(prefix="/agents/{slug}/history", tags=["history"],
                   dependencies=[Depends(_local_history_request)])


def _validate_stream_agent(slug: str) -> None:
    if not archives.storage_info().archive_configured:
        raise HTTPException(409, "Live history notifications require configured history storage; file history remains readable.")
    archives._require_agent(slug)


class HistoryStreamResponse(StreamingResponse):
    """Release the slot even if headers fail, the peer is idle, or sends stall.

    Always listen for disconnects (including ASGI 2.4+) rather than waiting for
    the next heartbeat to discover them. A bounded send timeout prevents a slow
    socket from retaining a subscription indefinitely. No per-peer event queue.
    """

    def __init__(self, content, subscription):
        super().__init__(content, media_type="text/event-stream", headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "X-Accel-Buffering": "no",
        })
        self.subscription = subscription

    async def __call__(self, scope, receive, send):
        async def bounded_send(message):
            with anyio.fail_after(SEND_TIMEOUT_SECONDS):
                await send(message)
            # A writable socket and buffered notification can both complete
            # without suspending. Let disconnect/shutdown tasks run in bursts.
            await anyio.lowlevel.checkpoint()

        try:
            async with anyio.create_task_group() as group:
                async def write():
                    try:
                        await self.stream_response(bounded_send)
                    except (TimeoutError, OSError):
                        # Expected peer failure; never log arbitrary transport data.
                        pass
                    finally:
                        group.cancel_scope.cancel()

                group.start_soon(write)
                await self.listen_for_disconnect(receive)
                group.cancel_scope.cancel()
        finally:
            self.subscription.close()
            # Async generators may be suspended at yield when sending fails.
            with anyio.CancelScope(shield=True):
                await self.body_iterator.aclose()


async def _notifications(subscription, slug: str):
    loop = asyncio.get_running_loop()
    reconcile_at = loop.time() + RECONCILE_SECONDS
    try:
        yield b"retry: 2000\n\n"
        while True:
            remaining = reconcile_at - loop.time()
            if remaining <= 0:
                # Registry/config changes must also revoke an idle subscription.
                # This does not read JSONL, query SQL, or launch synchronization.
                try:
                    await run_in_threadpool(_validate_stream_agent, slug)
                except HTTPException:
                    return
                yield subscription.resync(reason="reconcile").encode()
                reconcile_at = loop.time() + RECONCILE_SECONDS
                continue
            event = await subscription.next(timeout=min(HEARTBEAT_SECONDS, remaining))
            if event is None:
                yield b": keep-alive\n\n"
            else:
                yield event.encode()
    except HubClosed:
        return
    finally:
        subscription.close()


@router.get("/stream", response_class=StreamingResponse)
async def stream_history(request: Request, slug: Slug):
    await run_in_threadpool(_validate_stream_agent, slug)
    hub: HistoryEventHub | None = getattr(request.app.state, "history_events", None)
    if hub is None or getattr(request.app.state, "history_sync_error", False):
        raise HTTPException(503, "Live history notifications are unavailable; retry after restarting Studio.")
    ids = request.headers.getlist("last-event-id")
    if len(ids) > 1:
        raise HTTPException(400, "Provide one Last-Event-ID header")
    try:
        subscription = hub.subscribe(slug, last_event_id=ids[0] if ids else None)
    except ValueError:
        raise HTTPException(400, "Invalid Last-Event-ID") from None
    except SubscriptionLimit:
        raise HTTPException(429, "Too many history streams; close unused connections and retry.",
                            headers={"Retry-After": "2"}) from None
    except HubClosed:
        raise HTTPException(503, "Live history notifications are shutting down") from None
    try:
        return HistoryStreamResponse(_notifications(subscription, slug), subscription)
    except BaseException:
        subscription.close()
        raise
