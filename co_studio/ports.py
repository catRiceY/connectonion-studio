"""Allocate agent HTTP ports in 8000-8099 by socket probing."""

from __future__ import annotations

import os
import socket

from . import config


def is_free(port: int) -> bool:
    """Whether the server can bind without taking an active listener's port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        # Match Host/uvicorn on POSIX: a stopped server's TIME_WAIT connections
        # do not prevent reuse. REUSEADDR alone cannot take an active listener.
        # On Windows it can permit competing binds, so keep the non-reusing probe.
        if os.name == "posix":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", port))
        except OSError:
            return False
        return True


def allocate(reserved: set[int]) -> int:
    """First free, unreserved port in the range; raises RuntimeError when exhausted."""
    for port in config.AGENT_PORT_RANGE:
        if port in reserved:
            continue
        if is_free(port):
            return port
    raise RuntimeError("no free agent port left in 8000-8099")
