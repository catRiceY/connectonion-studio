"""FastAPI application factory: API routers, websockets, and the static frontend."""

from __future__ import annotations

import asyncio
import os
import threading
from contextlib import asynccontextmanager, suppress
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import config, creator, registry, setup_check
from .api import agents, approvals, archives, history, history_stream, knowledge, prompts, sessions, settings_api, setup, skills as skills_api, ws
from .history_events import HistoryEventHub, HubClosed
from .history_sync import HistorySync
from .knowledge_sync import KnowledgeSync
from .origin_guard import LocalOriginGuard
from .supervisor import SUPERVISOR


class _CacheHeadersMiddleware:
    """Set headers without buffering or relaying streaming response bodies.

    A pure ASGI wrapper keeps SSE send timeouts attached to the actual socket;
    BaseHTTPMiddleware would insert another task/channel between the two.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        parts = path.split("/", 5)
        is_history = (path.startswith(("/api/history/", "/api/knowledge/")) or
                      (len(parts) >= 5 and parts[:3] == ["", "api", "agents"]
                       and parts[4] in {"sessions", "archives", "history", "prompts", "tools", "approvals"}))

        async def with_headers(message):
            if message["type"] == "http.response.start":
                message = dict(message, headers=list(message.get("headers", [])))
                headers = MutableHeaders(scope=message)
                if is_history:
                    headers["Cache-Control"] = "no-store"
                    headers["X-Content-Type-Options"] = "nosniff"
                else:
                    headers.setdefault("Cache-Control", "no-cache")
            await send(message)

        await self.app(scope, receive, with_headers)


def _auto_authenticate() -> None:
    """Best-effort: mint THIS user's own managed key on first run, so no manual `co auth` is needed.

    connectonion's auth is signature-based — no login/password/browser: it signs with the local
    ~/.co Ed25519 key and the backend auto-creates the account. The key is always PER-USER, never a
    shared/bundled secret. Silent no-op on any failure (offline, backend down, or a refactor of these
    CLI internals); the onboarding screen still covers those cases and re-polls /api/setup/status.
    """
    if setup_check._has_managed_key():  # already activated → nothing to do
        return
    try:
        from connectonion.cli.commands.auth_commands import authenticate
        from connectonion.cli.commands.project_cmd_lib import ensure_global_config

        if not (config.MAIN_CO_DIR / "keys" / "agent.key").exists():
            ensure_global_config()  # auth needs a keypair to sign — create the ~/.co identity first
        ok = authenticate(config.MAIN_CO_DIR, save_to_project=False, quiet=True)
        state = "ok — managed key ready" if ok else "skipped (offline?); run `co auth` when online"
        print(f"[co-studio] auto-auth {state}", flush=True)
    except Exception as exc:  # noqa: BLE001 — never block startup on framework internals / network
        print(f"[co-studio] auto-auth unavailable ({exc!r}) — run `co auth` manually if needed", flush=True)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run the doctor, kick off first-run auth, adopt orphan agents, and drive the health poll."""
    registry.ensure_dirs()
    migrated = registry.migrate_capability_aliases()
    if migrated:
        print(f"[co-studio] metadata compatibility updated for {migrated} agent(s)", flush=True)
    refreshed = creator.refresh_generated_workspace_scripts()
    if refreshed:
        print(f"[co-studio] workspace sandbox updated for {refreshed} agent(s)", flush=True)
    remediated = creator.remediate_invite_policies()
    if remediated:
        print(f"[co-studio] invite-code policy fixed for {remediated} agent(s) — restart them to apply", flush=True)
    # Best-effort, on a daemon thread so the server binds immediately AND a slow first-run auth
    # call can never delay shutdown. The key lands within a few seconds; the onboarding screen
    # (2s poll) dismisses itself once /api/setup/status flips to key_ok.
    if os.environ.get("STUDIO_AUTO_AUTH", "1") != "0":
        threading.Thread(target=_auto_authenticate, name="co-studio-auth", daemon=True).start()
    for entry in setup_check.run_doctor():
        mark = "ok  " if entry["ok"] else "FAIL"
        print(f"[co-studio] doctor {mark} {entry['check']} — {entry['detail']}", flush=True)
    SUPERVISOR.adopt_orphans()
    poller = asyncio.create_task(SUPERVISOR.run())
    # Optional, one bounded worker; never on an Agent's execution/request path.
    # Invalid config still starts the worker so its retries/status remain visible.
    sync = None
    knowledge_sync = None
    events = None
    app.state.history_sync_error = False
    app.state.history_events = None
    try:
        if os.environ.get("STUDIO_HISTORY_DATABASE_URL") or os.environ.get("STUDIO_HISTORY_CONFIG"):
            try:
                events = HistoryEventHub()
                app.state.history_events = events

                def publish_capture(slug, session_id, capture_id):
                    # An already-running SQL transaction may finish after the
                    # shutdown join deadline; its commit remains valid, but no
                    # closed stream needs to be notified.
                    with suppress(HubClosed):
                        events.publish_capture(slug, session_id, capture_id)
                    if knowledge_sync is not None:
                        knowledge_sync.notify(slug, session_id, capture_id)

                def publish_status(slug, state):
                    with suppress(HubClosed):
                        events.publish_status(slug, state)

                sync = HistorySync(archives._archive, on_capture=publish_capture,
                                   on_status=publish_status)
                app.state.history_sync = sync
                sync.start()
                if os.environ.get("STUDIO_KNOWLEDGE_DATABASE_URL") or os.environ.get("STUDIO_KNOWLEDGE_CONFIG"):
                    knowledge_sync = KnowledgeSync(archives._archive)
                    knowledge_sync.start()
            except Exception:
                # An optional worker must not prevent the cockpit from opening.
                # Do not log arbitrary exceptions (they may contain credentials).
                app.state.history_sync_error = True
                print("[co-studio] history synchronization could not start", flush=True)
        yield
    finally:
        if events is not None:
            events.close()
        try:
            if knowledge_sync is not None:
                await asyncio.to_thread(knowledge_sync.stop)
            if sync is not None:
                # Stop prevents new captures; an already in-flight bounded DB write
                # may finish. Agent processes remain independent and are not stopped.
                await asyncio.to_thread(sync.stop)
        except Exception:
            print("[co-studio] history synchronization shutdown was incomplete", flush=True)
        finally:
            poller.cancel()
            with suppress(asyncio.CancelledError):
                await poller
    # DESIGN: shutdown NEVER kills Agents (independent host() servers, re-adopted
    # next launch). The sync worker stops with Studio; there is no guarantee that
    # unobserved source snapshots survive framework cleanup while Studio is shut.


def create_app() -> FastAPI:
    """Build the studio app: /api/*, /ws/*, and the zero-build frontend at /."""
    app = FastAPI(title="ConnectOnion Studio", lifespan=_lifespan)

    # Enforce the same loopback-only Host policy for every entry point, including
    # direct ASGI factory launches. A matching Origin alone cannot stop DNS rebinding.
    # Host matching ignores the port, so the native shell can still pick a free one.
    app.add_middleware(TrustedHostMiddleware,
                       allowed_hosts=["127.0.0.1", "localhost", "[::1]"],
                       www_redirect=False)
    app.add_middleware(_CacheHeadersMiddleware)
    app.add_middleware(LocalOriginGuard)

    app.include_router(agents.router, prefix="/api")
    app.include_router(sessions.router, prefix="/api")
    app.include_router(archives.storage_router, prefix="/api")
    app.include_router(archives.archive_router, prefix="/api")
    app.include_router(history.router, prefix="/api")
    app.include_router(history_stream.router, prefix="/api")
    app.include_router(prompts.router, prefix="/api")
    app.include_router(knowledge.router, prefix="/api")
    app.include_router(approvals.router, prefix="/api")
    app.include_router(setup.router, prefix="/api")
    app.include_router(settings_api.router, prefix="/api")
    app.include_router(skills_api.router, prefix="/api")
    app.include_router(ws.router)

    for sub in ("assets", "css", "js"):
        directory = config.FRONTEND_DIR / sub
        if directory.is_dir():
            app.mount(f"/{sub}", StaticFiles(directory=directory), name=sub)

    @app.get("/", include_in_schema=False)
    def index() -> Response:
        """Serve the SPA (or a plain notice while the frontend is absent)."""
        page = config.FRONTEND_DIR / "index.html"
        if page.exists():
            # Appearance cannot live only in localStorage: the macOS shell intentionally chooses a
            # fresh free port each launch, and browser storage is isolated by port. Inject the saved
            # Studio-wide choice before first paint; an empty value lets old localStorage migrate once.
            appearance = config.configured_appearance() or ""
            html = page.read_text().replace("__CO_STUDIO_APPEARANCE__", appearance)
            return HTMLResponse(html)
        return HTMLResponse("<h1>ConnectOnion Studio</h1><p>frontend/ not built yet — the API is live under /api.</p>")

    return app
