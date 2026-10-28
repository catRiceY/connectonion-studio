"""Reject browser cross-origin access to local control and WebSocket endpoints.

Not authentication: the process must still bind loopback, and sensitive history
routes retain their stricter peer/host checks. Local CLI clients may omit Origin.
"""
from urllib.parse import urlsplit
from starlette.datastructures import Headers
from starlette.responses import JSONResponse


class LocalOriginGuard:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"} or not scope.get("path", "").startswith(("/api/", "/ws/")):
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        allowed = headers.get("sec-fetch-site") in {None, "none", "same-origin"}
        origin = headers.get("origin")
        if origin is not None:
            try:
                parsed = urlsplit(origin)
                scheme = {"ws": "http", "wss": "https"}.get(scope["scheme"], scope["scheme"])
                allowed = (allowed and parsed.scheme == scheme
                           and parsed.netloc.lower() == headers.get("host", "").lower()
                           and bool(parsed.hostname) and parsed.username is None and parsed.password is None
                           and not parsed.path and not parsed.query and not parsed.fragment)
            except ValueError:
                allowed = False
        if not allowed:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await JSONResponse({"detail": "Studio control requires the same origin"}, status_code=403,
                                   headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})(scope, receive, send)
            return
        await self.app(scope, receive, send)
