"""Entry point: serve the studio (127.0.0.1:9900 by default) and open the dashboard in the browser.

Port selection, in order:
  * ``--free-port``      — bind a free OS-assigned loopback port (used by the macOS .app so two
                           launches never collide on a fixed port); the server is the sole port
                           authority and confirms the port before anyone else can observe it.
  * ``--port N`` / ``CO_STUDIO_PORT`` — an explicit port (env is the fallback default for --port).
  * otherwise the fixed 9900, so the published CLI behaves exactly as before.
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import sys
import threading
import webbrowser
from pathlib import Path

import uvicorn
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import config
from .app import create_app


def _print_banner(url: str) -> None:
    """A concise, Jupyter-style startup line pointing at the dashboard URL."""
    tty = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    bold = "\033[1m" if tty else ""
    dim = "\033[2m" if tty else ""
    reset = "\033[0m" if tty else ""
    print(f"\n  {bold}ConnectOnion Studio{reset}  →  {url}   {dim}(Ctrl+C to stop){reset}\n", flush=True)


def _default_port() -> int:
    """The --port default: CO_STUDIO_PORT env when set and valid, else the fixed 9900."""
    raw = os.environ.get("CO_STUDIO_PORT")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass  # ignore a garbage env value and fall back to the constant
    return config.STUDIO_PORT


def _bind_listen_socket(host: str, port: int) -> socket.socket:
    """Bind and listen on (host, port) — port 0 lets the OS pick a free one — and return the live
    socket to hand straight to uvicorn.

    Binding here (rather than probing is_free() and letting uvicorn bind later) closes the TOCTOU
    window in which another process could grab the port between the probe and the bind.

    The address family follows `host` (via getaddrinfo), so an IPv6 loopback (--host ::1) binds an
    AF_INET6 socket instead of crashing an IPv4-only one; the default 127.0.0.1 path is unchanged.
    """
    family, socktype, proto, _, sockaddr = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0]
    sock = socket.socket(family, socktype, proto)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(sockaddr)
    sock.listen()
    return sock


def _write_port_file(path: str, url: str) -> None:
    """Publish the confirmed base URL to a handshake file, atomically (temp + rename), so a watcher
    (the macOS app) reads either nothing or the complete URL — never a half-written path."""
    target = Path(path)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    tmp.write_text(url + "\n")
    os.replace(tmp, target)  # atomic when src/dst share a directory (POSIX + Windows)


def main(argv: list[str] | None = None) -> None:
    """Start the loopback-only studio server; pop the dashboard unless --no-browser."""
    parser = argparse.ArgumentParser(prog="co-studio", description="ConnectOnion Studio — local agent test cockpit.")
    parser.add_argument("--host", default=config.STUDIO_HOST, help="interface to bind (default: 127.0.0.1)")
    parser.add_argument(
        "--port", type=int, default=_default_port(),
        help="port to bind (default: CO_STUDIO_PORT env, else 9900)",
    )
    parser.add_argument(
        "--free-port", action="store_true",
        help="let the OS pick a free port and bind it; overrides --port (used by the desktop app)",
    )
    parser.add_argument(
        "--port-file", metavar="PATH", default=None,
        help="atomically write the confirmed base URL to PATH once bound (desktop-app handshake)",
    )
    parser.add_argument("--no-browser", action="store_true", help="do not open the dashboard in a browser (headless/CI)")
    args = parser.parse_args(argv)

    host = args.host
    # In --free-port mode we bind the socket ourselves (OS-assigned port) and hand the live socket to
    # uvicorn, so there is no scan→bind gap. Otherwise uvicorn binds host:port as before.
    sock: socket.socket | None = None
    if args.free_port:
        sock = _bind_listen_socket(host, 0)
        port = sock.getsockname()[1]
    else:
        port = args.port

    # Thread the REAL bound host/port through runtime state: config.STUDIO_PORT is a module constant
    # read by the banner and surfaced via /api/setup/status ('manager_url'), so a dynamic port must
    # be reflected here for the settings UI to render it correctly.
    config.STUDIO_HOST = host
    config.STUDIO_PORT = port

    url = f"http://{host}:{port}"

    # Announce only after the socket is bound+listening (free-port mode), so a watcher never probes a
    # not-yet-bound port. The readiness probe still gates on the first HTTP 200.
    if args.port_file:
        _write_port_file(args.port_file, url)

    _print_banner(url)
    if not args.no_browser:
        threading.Timer(1.2, webbrowser.open, args=(url,)).start()
    # access_log=False silences uvicorn's per-request spam (every /css, /js, /assets 304 and
    # WebSocket accept); the startup / doctor / shutdown lines stay. timeout_graceful_shutdown
    # stops it waiting forever for a lingering browser WebSocket so Ctrl+C / close exits promptly.
    application = create_app()
    # Prevent DNS-rebinding Host names from reaching local control endpoints.
    # Container deployments apply the same fixed host policy in their proxy.
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"], www_redirect=False)
    server = uvicorn.Server(uvicorn.Config(
        application, host=host, port=port,
        log_level="info", access_log=False, timeout_graceful_shutdown=5,
    ))
    # Closing the terminal (SIGHUP) should stop the studio cleanly, not leave an orphan that
    # lingers, serves nothing, and blocks the port on the next launch. uvicorn only handles
    # SIGINT/SIGTERM, so map SIGHUP onto its graceful-exit flag.
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, lambda *_: setattr(server, "should_exit", True))
    # Ctrl+C already triggered uvicorn's graceful shutdown by the time server.run() returns; its
    # asyncio.Runner then re-raises KeyboardInterrupt once the loop is closed. We call server.run()
    # programmatically (no uvicorn CLI wrapper to absorb it), so swallow it here — an intended,
    # orderly stop should exit cleanly, not dump a traceback for what was already a clean shutdown.
    try:
        if sock is not None:
            # Hand uvicorn the socket we already bound+listened on (TOCTOU-free); it will not re-bind.
            server.run(sockets=[sock])
        else:
            server.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
