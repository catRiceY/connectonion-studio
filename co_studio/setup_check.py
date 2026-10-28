"""Environment doctor: framework, identity, and API-key checks."""

from __future__ import annotations

import importlib.metadata
import importlib
import inspect
import re
from typing import Any

from . import config

# One uncommented, non-empty OPENONION_API_KEY=<value> assignment (value on the same line).
_KEY_RE = re.compile(r"(?m)^[ \t]*OPENONION_API_KEY[ \t]*=[ \t]*\S")


def _has_managed_key() -> bool:
    """True iff keys.env holds a real OPENONION_API_KEY; unreadable/missing/empty → False."""
    try:
        text = config.KEYS_ENV.read_text(errors="replace")
    except OSError:  # missing, a directory, or unreadable — mirror the other guarded checks
        return False
    return _KEY_RE.search(text) is not None


def _studio_version() -> str:
    """Own version, for the app footer."""
    try:
        return importlib.metadata.version("connectonion-studio")
    except Exception:  # noqa: BLE001 — not installed as a dist (dev checkout)
        return "0.1.0"

_FRAMEWORK_CHECKS = (
    "import connectonion",
    "connectonion.host exists",
    "host() accepts co_dir kwarg",
    "connectonion.network.announce.get_ips exists",
)
_IDENTITY_CHECK = "identity in ~/.co"
_KEY_CHECK = "~/.co/keys.env has OPENONION_API_KEY"
_RELAY_DEPS_CHECK = "relay transport supports SOCKS proxies"


def run_doctor() -> list[dict[str, Any]]:
    """Run every startup assertion; each entry is {check, ok, detail}."""
    checks: list[dict[str, Any]] = []

    def add(check: str, ok: bool, detail: str) -> None:
        checks.append({"check": check, "ok": ok, "detail": detail})

    try:
        import connectonion
    except Exception as exc:  # noqa: BLE001 — report, don't crash the studio
        add(_FRAMEWORK_CHECKS[0], False, repr(exc))
        for check in _FRAMEWORK_CHECKS[1:]:
            add(check, False, "framework import failed")
    else:
        add(_FRAMEWORK_CHECKS[0], True, f"version {getattr(connectonion, '__version__', '?')}")
        host_fn = getattr(connectonion, "host", None)
        if callable(host_fn):
            add(_FRAMEWORK_CHECKS[1], True, "host() is exported")
            try:
                ok = "co_dir" in inspect.signature(host_fn).parameters
                add(_FRAMEWORK_CHECKS[2], ok, "per-agent identity supported" if ok else "co_dir kwarg missing — framework too old/refactored")
            except (TypeError, ValueError) as exc:
                add(_FRAMEWORK_CHECKS[2], False, repr(exc))
        else:
            add(_FRAMEWORK_CHECKS[1], False, "connectonion.host missing — PyPI 0.4.x install? use the git source")
            add(_FRAMEWORK_CHECKS[2], False, "no host()")
        try:
            from connectonion.network import announce

            ok = callable(getattr(announce, "get_ips", None))
            add(_FRAMEWORK_CHECKS[3], ok, "patch target present" if ok else "get_ips missing — the runner patch has no target")
        except Exception as exc:  # noqa: BLE001
            add(_FRAMEWORK_CHECKS[3], False, repr(exc))

    try:
        transport = importlib.import_module("websockets.asyncio.client")
        supported = callable(getattr(transport, "SocksProxy", None))
        add(_RELAY_DEPS_CHECK, supported,
            "SOCKS dependency loaded; relay connectivity checked separately" if supported else
            "sync Studio dependencies (websockets>=15.0.1,<18 and python-socks[asyncio]); restart Studio/Agents")
    except Exception:
        add(_RELAY_DEPS_CHECK, False,
            "sync Studio dependencies (websockets>=15.0.1,<18 and python-socks[asyncio]); restart Studio/Agents")

    try:
        from connectonion import address as co_address

        ok = co_address.load(config.MAIN_CO_DIR) is not None
        add(_IDENTITY_CHECK, ok, "agent identity present" if ok else "run `co auth` to create one")
    except Exception as exc:  # noqa: BLE001
        add(_IDENTITY_CHECK, False, repr(exc))

    key_ok = _has_managed_key()
    add(_KEY_CHECK, key_ok, "managed model key found" if key_ok else "run `co auth` to fetch the managed key")
    return checks


def status() -> dict[str, Any]:
    """Response body for GET /api/setup/status."""
    doctor = run_doctor()
    ok_by_check = {entry["check"]: bool(entry["ok"]) for entry in doctor}
    return {
        "co_auth_ok": ok_by_check.get(_IDENTITY_CHECK, False),
        "key_ok": ok_by_check.get(_KEY_CHECK, False),
        "framework_ok": all(ok_by_check.get(check, False) for check in (*_FRAMEWORK_CHECKS, _RELAY_DEPS_CHECK)),
        "studio_version": _studio_version(),
        "agents_dir": str(config.AGENTS_DIR),
        # Read at request time so it reflects the REAL bound port (e.g. a --free-port desktop launch),
        # letting the settings UI render the manager URL instead of the hardcoded 9900.
        "manager_url": f"http://{config.STUDIO_HOST}:{config.STUDIO_PORT}",
        "doctor": doctor,
    }
