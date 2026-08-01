"""FastAPI application factory: API routers, websockets, and the static frontend."""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager, suppress
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config, creator, registry, setup_check
from .api import agents, settings_api, setup, ws
from .supervisor import SUPERVISOR


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
    threading.Thread(target=_auto_authenticate, name="co-studio-auth", daemon=True).start()
    for entry in setup_check.run_doctor():
        mark = "ok  " if entry["ok"] else "FAIL"
        print(f"[co-studio] doctor {mark} {entry['check']} — {entry['detail']}", flush=True)
    SUPERVISOR.adopt_orphans()
    poller = asyncio.create_task(SUPERVISOR.run())
    yield
    # DESIGN: the studio is a cockpit, not a daemon — shutdown NEVER kills agents (they're independent
    # host() servers, re-adopted next launch; only an explicit Stop does). So there's nothing to flush
    # here: just drop the health poll. Don't add kill-agents-on-quit without also flushing their logs.
    poller.cancel()
    with suppress(asyncio.CancelledError):
        await poller


def create_app() -> FastAPI:
    """Build the studio app: /api/*, /ws/*, and the zero-build frontend at /."""
    app = FastAPI(title="ConnectOnion Studio", lifespan=_lifespan)

    @app.middleware("http")
    async def _always_revalidate(request, call_next):
        """No heuristic caching: the browser must revalidate every asset each load,
        so edits to the zero-build frontend show up on a plain reload (still cheap —
        unchanged files come back as a 304 via StaticFiles' ETag/Last-Modified)."""
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-cache"
        return response

    app.include_router(agents.router, prefix="/api")
    app.include_router(setup.router, prefix="/api")
    app.include_router(settings_api.router, prefix="/api")
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
